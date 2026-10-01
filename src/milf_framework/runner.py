"""Bounded worker pool callable by scripts, future rules or schedulers."""
from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from typing import Any, Protocol

from . import __version__
from .models import RequestContext, RunConfig, RunSummary
from .sinks import JsonSink, ResultSink
from .stats import LOCATION_FIELDS, NETWORK_FIELDS, Counters, bucket_of, iso
from .targets import EchoIpTarget, TargetRegistry, registry
from .transport import HttpTransport, Transport

log = logging.getLogger(__name__)

GeoKey = tuple[tuple[Any, ...] | None, tuple[Any, ...] | None]

# A census has no request cap. One that has had no success after this many
# completions, or this long, is a broken provider and stops as 'no_success'.
# One that had successes and then none for RunConfig.stall_seconds lost its
# network mid-run and stops as 'stalled'.
NO_SUCCESS_REQUESTS = 5_000
NO_SUCCESS_SECONDS = 600.0


class Enricher(Protocol):
    """enrichment.LocalEnricher, or anything with the same lookup()."""

    def lookup(self, address: str) -> dict[str, Any]: ...


def _part(info: dict[str, Any], fields: tuple[str, ...]) -> tuple[Any, ...] | None:
    values = tuple(info.get(name) for name in fields)
    # An unmatched address is the same "unknown" member as a missing one.
    return values if any(value is not None for value in values) else None


async def run(
    config: RunConfig,
    result_sink: ResultSink | None = None,
    *,
    stop: asyncio.Event | None = None,
    targets: TargetRegistry = registry,
    transport_factory: Callable[[RunConfig], Transport] = HttpTransport,
    progress: Callable[[dict[str, int]], Any] | None = None,
    progress_interval: float = 30.0,
    enricher: Enricher | None = None,
) -> RunSummary:
    """Keep `concurrency` requests in flight until the stop rule ends the run.

    `progress` receives cumulative counters at most every `progress_interval`
    seconds and once at the end; it may be a coroutine function. `enricher`
    adds a per-bucket location x network breakdown (geo.jsonl.gz).
    """
    target = targets.get(config.target)
    if config.target_url is not None:
        if not isinstance(target, EchoIpTarget):
            raise ValueError("URL overrides require an echo-IP target")
        target = replace(target, url=config.target_url)
    if target.engine != "http" and transport_factory is HttpTransport:
        raise ValueError("This target requires a different execution adapter")
    if config.stop_rule == "unique_ratio" and not target.pool_eligible:
        raise ValueError("The unique-ratio stop rule requires a pool-eligible target")
    if progress is not None and (
        type(progress_interval) not in (int, float)
        or not math.isfinite(progress_interval) or progress_interval <= 0
    ):
        raise ValueError("progress_interval must be finite and positive")
    limit = config.requests
    ratio_rule = config.stop_rule == "unique_ratio"
    sink = result_sink if result_sink is not None else JsonSink()
    run_id = str(uuid.uuid4())
    began_wall, began = time.time(), time.perf_counter()
    totals = Counters()
    buckets: dict[int, Counters] = {}
    # Only current-bucket observations (IPs and geo rows) and a lifetime
    # distinct-IP set are held.
    current_bucket: int | None = None
    ip_rows: dict[str, list[float | int]] = {}
    geo_rows: dict[GeoKey, Counters] = {}
    unique: set[str] = set()
    started = 0
    # Set when a stop rule ends the run: 'unique_ratio', 'no_success' or 'stalled'.
    halted: str | None = None
    # Monotonic and wall time of the latest success (the stall rule's clock).
    last_success: float | None = None
    last_success_wall: float | None = None
    status = "completed"
    next_start = 0.0
    cancelled = False
    failure: BaseException | None = None
    workers: list[asyncio.Task] = []
    watcher: asyncio.Task | None = None
    ticker: asyncio.Task | None = None
    sink.start(run_id)
    writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ip-export")
    pending: deque[Future] = deque()

    def write_bucket(
        bucket: int | None,
        observations: dict[str, list[float | int]],
        geo: dict[GeoKey, Counters] | None,
    ) -> None:
        # Bound temporary serialization memory for large distinct-IP sets.
        rows: list[dict[str, Any]] = []
        for address, (count, first, last) in observations.items():
            rows.append({
                "schema_version": "1", "run_id": run_id, "ip": address,
                "bucket_start": iso(bucket), "count": count,
                "first_seen": iso(first), "last_seen": iso(last),
            })
            if len(rows) == 1000:
                sink.observations(rows)
                rows = []
        if rows:
            sink.observations(rows)
        if geo is None:
            return
        rows = []
        for (location, network), counters in geo.items():
            rows.append({
                "bucket_start": iso(bucket),
                "location": dict(zip(LOCATION_FIELDS, location, strict=True))
                if location else None,
                "network": dict(zip(NETWORK_FIELDS, network, strict=True)) if network else None,
                **counters.export_geo(),
            })
            if len(rows) == 1000:
                sink.geo(rows)
                rows = []
        # Always called with an enricher, so a run without traffic still has the file.
        sink.geo(rows)

    def flush(*, final: bool = False) -> None:
        nonlocal ip_rows, geo_rows
        geo = geo_rows if enricher is not None and (geo_rows or final) else None
        if ip_rows or geo is not None:
            pending.append(writer.submit(write_bucket, current_bucket, ip_rows, geo))
            ip_rows, geo_rows = {}, {}

    def snapshot() -> dict[str, int]:
        return {
            "requests": totals.completed, "successes": totals.successes,
            "unique_ips": len(unique), **totals.verdicts(),
        }

    async def report() -> None:
        assert progress is not None
        try:
            result = progress(snapshot())
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            # A lost progress post must not end a measurement; the final
            # artifact carries the authoritative totals.
            log.warning("Progress callback failed (%s)", type(exc).__name__)

    async def tick() -> None:
        while True:
            await asyncio.sleep(progress_interval)
            await report()

    def may_start() -> bool:
        return (not halted and (limit is None or started < limit)
                and not (stop and stop.is_set()))

    async def worker(transport: Transport, worker_index: int) -> None:
        nonlocal started, next_start, current_bucket, halted, last_success, last_success_wall
        while may_start():
            # Serialization never blocks the event loop or inflates in-flight
            # request latency. Slow storage applies backpressure before a request.
            while pending and pending[0].done():
                pending[0].result()
                pending.popleft()
            if len(pending) >= 2:
                await asyncio.shield(asyncio.wrap_future(pending[0]))
            if config.requests_per_second:
                now = time.perf_counter()
                scheduled = max(now, next_start)
                next_start = scheduled + 1 / config.requests_per_second
                await asyncio.sleep(max(0, scheduled - now))
            if not may_start():
                break
            context = RequestContext(run_id, started, worker_index)
            started += 1
            result = await transport.execute(target, context=context)
            # Anchor UTC to the monotonic clock: a clock adjustment cannot
            # reopen an already flushed bucket or reverse first/last seen.
            completed_at = time.perf_counter()
            observed = began_wall + (completed_at - began)
            bucket = bucket_of(observed)
            totals.add(result)
            if result.error is None:
                last_success, last_success_wall = completed_at, observed
            buckets.setdefault(bucket, Counters()).add(result)
            if current_bucket != bucket:
                flush()
                current_bucket = bucket
            if enricher is not None:
                if result.ip and result.error is None:
                    info = enricher.lookup(result.ip)
                    key = (_part(info, LOCATION_FIELDS), _part(info, NETWORK_FIELDS))
                else:
                    key = (None, None)
                if key not in geo_rows:
                    geo_rows[key] = Counters()
                geo_rows[key].add(result)
            if target.pool_eligible and result.ip and result.error is None:
                unique.add(result.ip)
                if result.ip in ip_rows:
                    row = ip_rows[result.ip]
                    row[0] += 1
                    row[2] = observed
                else:
                    ip_rows[result.ip] = [1, observed, observed]
            # Census: once a warm-up sample of `min_sample` successes exists,
            # stop issuing as soon as successes (requests that returned an exit
            # IP) outnumber twice the distinct exits, i.e. unique < 50%. At the
            # end of the warm-up that can fire at once. Failures never stop a
            # census. In-flight requests still finish and are counted.
            # A census whose network goes down after it worked stops once no
            # request has succeeded for stall_seconds; the failures up to then
            # stay in its buckets, so that time range's success rate shows it.
            if ratio_rule and not halted:
                if (totals.successes >= config.min_sample
                        and totals.successes > 2 * len(unique)):
                    halted = "unique_ratio"
                elif not totals.successes and (
                    totals.completed >= NO_SUCCESS_REQUESTS
                    or time.perf_counter() - began >= NO_SUCCESS_SECONDS
                ):
                    halted = "no_success"
                elif (last_success is not None
                        and time.perf_counter() - last_success >= config.stall_seconds):
                    halted = "stalled"
            # Real I/O yields; injected fast transports must also be cancellable.
            if totals.completed % 256 == 0:
                await asyncio.sleep(0)

    async def watch_stop() -> None:
        assert stop is not None
        await stop.wait()
        for task in workers:
            task.cancel()

    try:
        async with transport_factory(config) as transport:
            workers = [asyncio.create_task(worker(transport, index)) for index in range(
                config.concurrency if limit is None else min(config.concurrency, limit)
            )]
            if stop:
                watcher = asyncio.create_task(watch_stop())
            if progress is not None:
                ticker = asyncio.create_task(tick())
            try:
                await asyncio.gather(*workers)
            finally:
                helpers = [task for task in (watcher, ticker) if task]
                for task in (*workers, *helpers):
                    task.cancel()
                await asyncio.gather(*workers, *helpers, return_exceptions=True)
    except asyncio.CancelledError:
        status = "cancelled"
        cancelled = not (stop and stop.is_set())
    except Exception as exc:
        status = "failed"
        failure = exc
    reached = limit is not None and totals.completed >= limit
    if stop and stop.is_set() and (started > totals.completed or not (reached or halted)):
        status = "cancelled"
    if status == "failed":
        stop_reason = "error"
    elif halted:
        stop_reason = halted
    elif status == "cancelled":
        stop_reason = "operator"
    else:
        stop_reason = "requests"
    elapsed = time.perf_counter() - began
    summary = RunSummary(
        run_id=run_id, schema_version="1", framework_version=__version__,
        target={"id": target.id, "version": target.version,
                "url": target.build_request().url, "engine": target.engine},
        config=config.public(), started_at=iso(began_wall),
        finished_at=iso(began_wall + elapsed), duration_seconds=elapsed, status=status,
        requested=limit, started=started, completed=totals.completed,
        cancelled=started - totals.completed,
        # An unbounded run has no unstarted remainder.
        not_started=0 if limit is None else limit - started,
        successes=totals.successes, total_errors=totals.errors.total(),
        errors=dict(totals.errors), status_codes=dict(totals.statuses),
        success_rate=totals.successes / totals.completed if totals.completed else None,
        requests_per_second=totals.completed / elapsed if elapsed else 0,
        unique_ips=len(unique) if target.pool_eligible else None,
        pool_eligible=target.pool_eligible, latency_ms=totals.histogram.percentiles(),
        latency_histogram=totals.histogram.export(), response_body_bytes=totals.body_bytes,
        buckets=[{"bucket_start": iso(key), **value.export()}
                 for key, value in sorted(buckets.items())],
        **totals.verdicts(), stop_reason=stop_reason,
        outage_started_at=iso(last_success_wall)
        if stop_reason == "stalled" and last_success_wall is not None else None,
    )
    release = getattr(enricher, "release", None)
    if enricher is not None and isinstance(release, dict):
        # JsonSink adds file, rows and checksum to this entry.
        summary.artifacts = {"geo": {"release": release}}
    try:
        if progress is not None:
            await report()
        flush(final=True)
        for future in pending:
            await asyncio.shield(asyncio.wrap_future(future))
        await asyncio.to_thread(sink.finish, summary)
    finally:
        # No writer may outlive the stream it owns, including on disk failure.
        writer.shutdown(wait=True)
        sink.abort()
    if cancelled:
        raise asyncio.CancelledError
    if failure:
        # Preserve a useful error type without publishing arbitrary exception text.
        message = f"Run failed ({type(failure).__name__}); partial results saved"
        raise RuntimeError(message) from None
    return summary
