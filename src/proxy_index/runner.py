"""Bounded worker pool callable by scripts, future rules or schedulers."""
from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from typing import Any, Protocol

from . import __version__
from .answers import Answers
from .models import RequestContext, RunConfig, RunSummary, protocol_of
from .process_transport import ProcessTransport, process_count
from .sinks import JsonSink, ResultSink
from .stats import LOCATION_FIELDS, NETWORK_FIELDS, Counters, bucket_of, iso
from .targets import EchoIpTarget, TargetRegistry, https_only, registry
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
    progress: Callable[[dict[str, Any]], Any] | None = None,
    progress_interval: float = 30.0,
    enricher: Enricher | None = None,
    observation_consumer: Callable[[str, float], Awaitable[None]] | None = None,
    deadline: float | None = None,
    aggregate_ips: bool = True,
    wall_clock: Callable[[], float] | None = None,
) -> RunSummary:
    """Keep `concurrency` requests in flight until the stop rule ends the run.

    `progress` receives cumulative counters at most every `progress_interval`
    seconds and once at the end; it may be a coroutine function. `enricher`
    adds a per-bucket location x network breakdown (geo.jsonl.gz).
    `observation_consumer` receives every successful exit, including duplicates.
    Its errors are fatal and its awaits apply backpressure. `deadline` is an
    absolute Unix timestamp: no new requests or observations at/after it.
    Disabling `aggregate_ips` leaves exact uniqueness and all counters intact.
    """
    target = targets.get(config.target)
    if config.target_url is not None:
        if not isinstance(target, EchoIpTarget):
            raise ValueError("URL overrides require an echo-IP target")
        target = replace(target, url=config.target_url)
    protocol = protocol_of(config.proxy)
    if protocol == "https-only":
        # Before any traffic, so neither transport nor worker process ever sends plain HTTP.
        target = https_only(target)
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
    if deadline is not None and (
        type(deadline) not in (int, float) or not math.isfinite(deadline)
    ):
        raise ValueError("deadline must be a finite Unix timestamp")
    ratio_rule = config.stop_rule == "unique_ratio"
    sink = result_sink if result_sink is not None else JsonSink()
    run_id = str(uuid.uuid4())
    wall_now = wall_clock if wall_clock is not None else time.time
    began_wall, began = wall_now(), time.perf_counter()
    deadline_mono = began + deadline - began_wall if deadline is not None else None

    def expired() -> bool:
        return deadline is not None and (
            wall_now() >= deadline or time.perf_counter() >= deadline_mono
        )
    totals = Counters()
    answers = Answers(config, enricher if target.pool_eligible else None)
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
    parallel = transport_factory is HttpTransport and process_count(config) > 1
    execution = {"execution_processes": process_count(config) if parallel else 1,
                 "execution_backend": "multiprocess" if parallel else "inline"}
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

    def snapshot() -> dict[str, Any]:
        return {
            "requests": totals.completed, "successes": totals.successes,
            "unique_ips": len(unique), **totals.verdicts(), **execution,
        } | answers.progress(totals) | ({"redirects": totals.redirects} if protocol == "mixed"
                                         else {})

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
        return (not halted and (limit is None or answers.started(started) < limit)
                and not (stop and stop.is_set()) and not expired())

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
            if expired():
                raise asyncio.CancelledError
            # Anchor UTC to the monotonic clock: a clock adjustment cannot
            # reopen an already flushed bucket or reverse first/last seen.
            completed_at = (result.completed_at if result.completed_at is not None
                            else time.perf_counter())
            observed = began_wall + (completed_at - began)
            # IPC from independent processes may arrive slightly out of order.
            # Bucket by coordinator receipt; retain true observation timestamps.
            bucket = bucket_of(began_wall + time.perf_counter() - began)
            totals.add(result)
            kind = answers.add(result, completed_at)
            halted = halted or answers.halt(completed_at)
            counted = kind == "answer" and result.ip and result.error is None
            if result.error is None:
                last_success = max(last_success or completed_at, completed_at)
                last_success_wall = max(last_success_wall or observed, observed)
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
            if target.pool_eligible and counted:
                unique.add(result.ip)
                if not aggregate_ips:
                    pass
                elif result.ip in ip_rows:
                    row = ip_rows[result.ip]
                    row[0] += 1
                    row[1] = min(row[1], observed)
                    row[2] = max(row[2], observed)
                else:
                    ip_rows[result.ip] = [1, observed, observed]
            # Preserve every attempt, status, error and byte count. Answer classes (and the
            # opt-in 469 policy) exclude rate limits from the sample and the request goal;
            # this worker then issues a replacement through the normal loop.
            if ratio_rule and not halted:
                sample = answers.sample(totals)
                if not totals.successes and (
                    sample >= min(config.min_sample, NO_SUCCESS_REQUESTS)
                    or time.perf_counter() - began >= NO_SUCCESS_SECONDS
                ):
                    halted = "no_success"
                elif (last_success is not None
                        and time.perf_counter() - last_success >= config.stall_seconds):
                    halted = "stalled"
                elif (sample >= config.min_sample
                        and sample > 2 * len(unique)):
                    halted = "unique_ratio"
            if observation_consumer is not None and counted:
                # A completed response must reach the writer even if a deadline
                # cancels its worker during backpressure. The writer owns draining.
                delivery = asyncio.ensure_future(observation_consumer(result.ip, observed))
                try:
                    await asyncio.shield(delivery)
                except asyncio.CancelledError:
                    await delivery
                    raise
            # Real I/O yields; injected fast transports must also be cancellable.
            if totals.completed % 256 == 0:
                await asyncio.sleep(0)

    async def watch_stop() -> None:
        while not expired() and not (stop and stop.is_set()):
            if stop is not None:
                try:
                    await asyncio.wait_for(stop.wait(), 0.05)
                except TimeoutError:
                    pass
            else:
                await asyncio.sleep(0.05)
        for task in workers:
            task.cancel()
        if parallel:
            engine.abort()

    try:
        engine = (ProcessTransport(config, target, deadline=deadline_mono)
                  if parallel else transport_factory(config))
        async with engine as transport:
            if parallel:
                execution["worker_pids"] = list(transport.pids)
            workers = [asyncio.create_task(worker(transport, index)) for index in range(
                config.concurrency if limit is None else min(config.concurrency, limit)
            )]
            if stop or deadline is not None:
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
        cancelled = not (stop and stop.is_set()) and not expired()
    except Exception as exc:
        status = "failed"
        failure = exc
    reached = limit is not None and answers.started(totals.completed) >= limit
    if stop and stop.is_set() and (started > totals.completed or not (reached or halted)):
        status = "cancelled"
    if expired() and status != "failed":
        status = "cancelled"
        stop_reason = "deadline"
    elif status == "failed":
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
        config=config.public() | execution, started_at=iso(began_wall),
        finished_at=iso(began_wall + elapsed), duration_seconds=elapsed, status=status,
        requested=limit, started=started, completed=totals.completed,
        cancelled=started - totals.completed,
        # An unbounded run has no unstarted remainder.
        not_started=0 if limit is None else limit - answers.started(started),
        successes=totals.successes, total_errors=totals.errors.total(),
        errors=dict(totals.errors), status_codes=dict(totals.statuses),
        success_rate=totals.successes / totals.completed if totals.completed else None,
        requests_per_second=totals.completed / elapsed if elapsed else 0,
        unique_ips=len(unique) if target.pool_eligible else None,
        pool_eligible=target.pool_eligible, latency_ms=totals.histogram.percentiles(),
        latency_histogram=totals.histogram.export(), response_body_bytes=totals.body_bytes,
        buckets=[{"bucket_start": iso(key), **value.export()}
                 for key, value in sorted(buckets.items())],
        **totals.verdicts(), **answers.summary(totals), stop_reason=stop_reason,
        outage_started_at=iso(last_success_wall)
        if stop_reason == "stalled" and last_success_wall is not None else None,
        redirects=totals.redirects if protocol == "mixed" else None,
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
