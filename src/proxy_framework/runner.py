"""Bounded worker pool callable by scripts, future rules or schedulers."""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from typing import Any

from . import __version__
from .models import RequestContext, RunConfig, RunSummary
from .sinks import JsonSink, ResultSink
from .stats import Counters, bucket_of, iso
from .targets import EchoIpTarget, TargetRegistry, registry
from .transport import HttpTransport, Transport


async def run(
    config: RunConfig,
    result_sink: ResultSink | None = None,
    *,
    stop: asyncio.Event | None = None,
    targets: TargetRegistry = registry,
    transport_factory: Callable[[RunConfig], Transport] = HttpTransport,
) -> RunSummary:
    target = targets.get(config.target)
    if config.target_url is not None:
        if not isinstance(target, EchoIpTarget):
            raise ValueError("URL overrides require an echo-IP target")
        target = replace(target, url=config.target_url)
    if target.engine != "http" and transport_factory is HttpTransport:
        raise ValueError("This target requires a different execution adapter")
    sink = result_sink if result_sink is not None else JsonSink()
    run_id = str(uuid.uuid4())
    began_wall, began = time.time(), time.perf_counter()
    totals = Counters()
    buckets: dict[int, Counters] = {}
    # Only current-bucket observations and a lifetime distinct-IP set are held.
    ip_bucket: int | None = None
    ip_rows: dict[str, list[float | int]] = {}
    unique: set[str] = set()
    started = 0
    status = "completed"
    next_start = 0.0
    cancelled = False
    failure: BaseException | None = None
    workers: list[asyncio.Task] = []
    watcher: asyncio.Task | None = None
    sink.start(run_id)
    writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ip-export")
    pending: deque[Future] = deque()

    def write_ips(bucket: int, observations: dict[str, list[float | int]]) -> None:
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

    def flush_ips() -> None:
        nonlocal ip_rows
        if ip_rows:
            pending.append(writer.submit(write_ips, ip_bucket, ip_rows))
            ip_rows = {}

    async def worker(transport: Transport, worker_index: int) -> None:
        nonlocal started, next_start, ip_bucket
        while started < config.requests and not (stop and stop.is_set()):
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
            if started >= config.requests or (stop and stop.is_set()):
                break
            context = RequestContext(run_id, started, worker_index)
            started += 1
            result = await transport.execute(target, context=context)
            # Anchor UTC to the monotonic clock: a clock adjustment cannot
            # reopen an already flushed bucket or reverse first/last seen.
            observed = began_wall + (time.perf_counter() - began)
            bucket = bucket_of(observed)
            totals.add(result)
            buckets.setdefault(bucket, Counters()).add(result)
            if target.pool_eligible and result.ip and result.error is None:
                if ip_bucket != bucket:
                    flush_ips()
                    ip_bucket = bucket
                unique.add(result.ip)
                if result.ip in ip_rows:
                    row = ip_rows[result.ip]
                    row[0] += 1
                    row[2] = observed
                else:
                    ip_rows[result.ip] = [1, observed, observed]
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
                min(config.concurrency, config.requests)
            )]
            if stop:
                watcher = asyncio.create_task(watch_stop())
            try:
                await asyncio.gather(*workers)
            finally:
                for task in workers:
                    task.cancel()
                if watcher:
                    watcher.cancel()
                await asyncio.gather(*workers, *([watcher] if watcher else []),
                                     return_exceptions=True)
    except asyncio.CancelledError:
        status = "cancelled"
        cancelled = not (stop and stop.is_set())
    except Exception as exc:
        status = "failed"
        failure = exc
    if stop and stop.is_set() and totals.completed < config.requests:
        status = "cancelled"
    elapsed = time.perf_counter() - began
    summary = RunSummary(
        run_id=run_id, schema_version="1", framework_version=__version__,
        target={"id": target.id, "version": target.version,
                "url": target.build_request().url, "engine": target.engine},
        config=config.public(), started_at=iso(began_wall),
        finished_at=iso(began_wall + elapsed), duration_seconds=elapsed, status=status,
        requested=config.requests, started=started, completed=totals.completed,
        cancelled=started - totals.completed, not_started=config.requests - started,
        successes=totals.successes, total_errors=totals.errors.total(),
        errors=dict(totals.errors), status_codes=dict(totals.statuses),
        success_rate=totals.successes / totals.completed if totals.completed else None,
        requests_per_second=totals.completed / elapsed if elapsed else 0,
        unique_ips=len(unique) if target.pool_eligible else None,
        pool_eligible=target.pool_eligible, latency_ms=totals.histogram.percentiles(),
        latency_histogram=totals.histogram.export(), response_body_bytes=totals.body_bytes,
        buckets=[{"bucket_start": iso(key), **value.export()}
                 for key, value in sorted(buckets.items())],
    )
    try:
        flush_ips()
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
