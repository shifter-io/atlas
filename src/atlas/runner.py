"""Bounded worker pool callable by scripts, future rules or schedulers."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import math
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from . import __version__
from .answers import Answers
from .models import Outcome, RequestContext, RunConfig, RunSummary, protocol_of
from .process_transport import ProcessTransport, process_count
from .sinks import JsonSink, ResultSink
from .stats import LOCATION_FIELDS, NETWORK_FIELDS, Counters, bucket_of, iso
from .targets import EchoIpTarget, Target, TargetRegistry, https_only, registry
from .transport import HttpTransport, Transport

log = logging.getLogger(__name__)

GeoKey = tuple[tuple[Any, ...] | None, tuple[Any, ...] | None]

# A census has no request cap. One that has had no success after this many
# completions, or this long, is a broken provider and stops as 'no_success'.
# One that had successes and then none for RunConfig.stall_seconds lost its
# network mid-run and stops as 'stalled'.
NO_SUCCESS_REQUESTS = 5_000
NO_SUCCESS_SECONDS = 600.0
# One that has found no unique IP after this many answers stops (LOWAVAIL-1, the owner's rule): as
# 'no_ips', or as 'no_success' when nothing succeeded either. A test cannot repeat what it never
# found, so a country with no exits costs a few hundred requests, not thousands.
ZERO_IPS_STOP = 300
# A run with its own pace (RunConfig.start_concurrency) may raise its in-flight limit every
# GROW_SECONDS to PACE_SHARE of the unique IPs it has found so far, never above its concurrency:
# a country where a network added exits speeds up by itself (LOWAVAIL-1, the owner's rule).
GROW_SECONDS = 5.0
PACE_SHARE = 0.5
# LOWAVAIL-PACE-3: a paced run without a low-availability cap ramps faster: every RAMP_SECONDS
# its limit may double, up to the same PACE_SHARE of its IPs; a low one grows as above.
RAMP_SECONDS = 2.0
# A run with RunConfig.low_requests or low_seconds (a low-availability country's daily test,
# LOWAVAIL-PACE-2) stops as 'cap' at either while it has found fewer than LOW_IPS unique IPs; one
# that finds LOW_IPS or more runs on under its other rules (and grows its pace as above).
LOW_IPS = 10


class Enricher(Protocol):
    """enrichment.LocalEnricher, or anything with the same lookup()."""

    def lookup(self, address: str) -> dict[str, Any]: ...


def _part(info: dict[str, Any], fields: tuple[str, ...]) -> tuple[Any, ...] | None:
    values = tuple(info.get(name) for name in fields)
    # An unmatched address is the same "unknown" member as a missing one.
    return values if any(value is not None for value in values) else None


@dataclass(frozen=True)
class RunOptions:
    """run()'s optional inputs, each with its default.

    `progress` receives cumulative counters at most every `progress_interval`
    seconds and once at the end; it may be a coroutine function. `enricher`
    adds a per-bucket location x network breakdown (geo.jsonl.gz).
    `observation_consumer` receives every successful exit, including duplicates.
    Its errors are fatal and its awaits apply backpressure. `deadline` is an
    absolute Unix timestamp: no new requests or observations at/after it.
    Disabling `aggregate_ips` leaves exact uniqueness and all counters intact.
    """

    stop: asyncio.Event | None = None
    targets: TargetRegistry = registry
    transport_factory: Callable[[RunConfig], Transport] = HttpTransport
    progress: Callable[[dict[str, Any]], Any] | None = None
    progress_interval: float = 30.0
    enricher: Enricher | None = None
    observation_consumer: Callable[[str, float], Awaitable[None]] | None = None
    deadline: float | None = None
    aggregate_ips: bool = True
    wall_clock: Callable[[], float] | None = None


async def run(
    config: RunConfig,
    result_sink: ResultSink | None = None,
    options: RunOptions | None = None,
) -> RunSummary:
    """Keep `concurrency` requests in flight until the stop rule ends the run.

    `options` (RunOptions) holds the optional inputs; None means all defaults.
    """
    options = options or RunOptions()
    transport_factory = options.transport_factory
    target, protocol = _resolve_target(config, options.targets, transport_factory)
    _check_timing(options.progress, options.progress_interval, options.deadline)
    state = _RunState(
        config=config,
        target=target,
        protocol=protocol,
        sink=result_sink if result_sink is not None else JsonSink(),
        wall_now=options.wall_clock if options.wall_clock is not None else time.time,
        deadline=options.deadline,
        stop=options.stop,
        progress=options.progress,
        progress_interval=options.progress_interval,
        enricher=options.enricher,
        observation_consumer=options.observation_consumer,
        aggregate_ips=options.aggregate_ips,
        parallel=transport_factory is HttpTransport and process_count(config) > 1,
    )
    state.sink.start(state.run_id)
    state.writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ip-export")
    status, cancelled, failure = await _execute(state, transport_factory)
    status, stop_reason = _outcome(state, status)
    elapsed = time.perf_counter() - state.began
    summary = _summary(state, status, stop_reason, elapsed)
    await _finish(state, summary)
    if cancelled:
        raise asyncio.CancelledError
    if failure:
        # Preserve a useful error type without publishing arbitrary exception text.
        message = f"Run failed ({type(failure).__name__}); partial results saved"
        raise RuntimeError(message) from None
    return summary


def _resolve_target(
    config: RunConfig,
    targets: TargetRegistry,
    transport_factory: Callable[[RunConfig], Transport],
) -> tuple[Target, str]:
    """The target this run measures, and the network's test protocol."""
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
    return target, protocol


def _check_timing(
    progress: Callable[[dict[str, Any]], Any] | None,
    progress_interval: float,
    deadline: float | None,
) -> None:
    if progress is not None and (
        type(progress_interval) not in (int, float)
        or not math.isfinite(progress_interval)
        or progress_interval <= 0
    ):
        raise ValueError("progress_interval must be finite and positive")
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise ValueError("deadline must be a finite Unix timestamp")


@dataclass(eq=False)
class _RunState:
    """One run's settings and the mutable state its steps share."""

    config: RunConfig
    target: Target
    protocol: str
    sink: ResultSink
    wall_now: Callable[[], float]
    deadline: float | None
    stop: asyncio.Event | None
    progress: Callable[[dict[str, Any]], Any] | None
    progress_interval: float
    enricher: Enricher | None
    observation_consumer: Callable[[str, float], Awaitable[None]] | None
    aggregate_ips: bool
    parallel: bool
    run_id: str = field(init=False)
    began_wall: float = field(init=False)
    began: float = field(init=False)
    deadline_mono: float | None = field(init=False)
    answers: Answers = field(init=False)
    execution: dict[str, Any] = field(init=False)
    # Created once the sink has started.
    writer: ThreadPoolExecutor = field(init=False)
    pending: deque[Future] = field(default_factory=deque)
    totals: Counters = field(default_factory=Counters)
    buckets: dict[int, Counters] = field(default_factory=dict)
    # Only current-bucket observations (IPs and geo rows) and a lifetime
    # distinct-IP set are held.
    current_bucket: int | None = None
    ip_rows: dict[str, list[float | int]] = field(default_factory=dict)
    geo_rows: dict[GeoKey, Counters] = field(default_factory=dict)
    unique: set[str] = field(default_factory=set)
    started: int = 0
    # Set when a stop rule ends the run: 'unique_ratio', 'no_success', 'no_ips', 'stalled' or
    # 'cap'.
    halted: str | None = None
    # Monotonic and wall time of the latest success (the stall rule's clock).
    last_success: float | None = None
    last_success_wall: float | None = None
    next_start: float = 0.0
    # The in-flight limit of a run with its own pace (RunConfig.start_concurrency), else None;
    # `allowed` is its current size, last raised (or set) at `grown_at` (monotonic).
    gate: asyncio.Semaphore | None = None
    allowed: int = 0
    grown_at: float = 0.0

    def __post_init__(self) -> None:
        self.run_id = str(uuid.uuid4())
        self.began_wall, self.began = self.wall_now(), time.perf_counter()
        self.deadline_mono = (
            self.began + self.deadline - self.began_wall if self.deadline is not None else None
        )
        self.answers = Answers(self.config, self.enricher if self.target.pool_eligible else None)
        self.execution = {
            "execution_processes": process_count(self.config) if self.parallel else 1,
            "execution_backend": "multiprocess" if self.parallel else "inline",
        }
        start = self.config.start_concurrency
        if start is not None and start < self.config.concurrency:
            self.gate, self.allowed, self.grown_at = asyncio.Semaphore(start), start, self.began

    @property
    def limit(self) -> int | None:
        return self.config.requests

    def stopped(self) -> bool:
        return bool(self.stop and self.stop.is_set())

    def expired(self) -> bool:
        return self.deadline is not None and (
            self.wall_now() >= self.deadline or time.perf_counter() >= self.deadline_mono
        )

    def may_start(self) -> bool:
        return (
            not self.halted
            and (self.limit is None or self.answers.started(self.started) < self.limit)
            and not self.stopped()
            and not self.expired()
        )

    def flush(self, *, final: bool = False) -> None:
        geo = self.geo_rows if self.enricher is not None and (self.geo_rows or final) else None
        if self.ip_rows or geo is not None:
            self.pending.append(
                self.writer.submit(_write_bucket, self, self.current_bucket, self.ip_rows, geo)
            )
            self.ip_rows, self.geo_rows = {}, {}

    def snapshot(self) -> dict[str, Any]:
        return (
            {
                "requests": self.totals.completed,
                "successes": self.totals.successes,
                "unique_ips": len(self.unique),
                **self.totals.verdicts(),
                **self.execution,
            }
            | self.answers.progress(self.totals)
            | ({"redirects": self.totals.redirects} if self.protocol == "mixed" else {})
        )


def _write_bucket(
    state: _RunState,
    bucket: int | None,
    observations: dict[str, list[float | int]],
    geo: dict[GeoKey, Counters] | None,
) -> None:
    # Bound temporary serialization memory for large distinct-IP sets.
    rows: list[dict[str, Any]] = []
    for address, (count, first, last) in observations.items():
        rows.append(
            {
                "schema_version": "1",
                "run_id": state.run_id,
                "ip": address,
                "bucket_start": iso(bucket),
                "count": count,
                "first_seen": iso(first),
                "last_seen": iso(last),
            }
        )
        if len(rows) == 1000:
            state.sink.observations(rows)
            rows = []
    if rows:
        state.sink.observations(rows)
    if geo is None:
        return
    rows = []
    for (location, network), counters in geo.items():
        rows.append(
            {
                "bucket_start": iso(bucket),
                "location": dict(zip(LOCATION_FIELDS, location, strict=True)) if location else None,
                "network": dict(zip(NETWORK_FIELDS, network, strict=True)) if network else None,
                **counters.export_geo(),
            }
        )
        if len(rows) == 1000:
            state.sink.geo(rows)
            rows = []
    # Always called with an enricher, so a run without traffic still has the file.
    state.sink.geo(rows)


async def _report(state: _RunState) -> None:
    assert state.progress is not None
    try:
        result = state.progress(state.snapshot())
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # boundary: a progress post never ends the run; type only
        # A lost progress post must not end a measurement; the final
        # artifact carries the authoritative totals.
        log.warning("Progress callback failed (%s)", type(exc).__name__)


async def _tick(state: _RunState) -> None:
    while True:
        await asyncio.sleep(state.progress_interval)
        await _report(state)


def _census_halt(state: _RunState) -> str | None:
    """The census stop rules: the reason the run halts now, or None."""
    config, totals = state.config, state.totals
    sample = state.answers.sample(totals)
    if not totals.successes and (
        sample >= min(config.min_sample, NO_SUCCESS_REQUESTS)
        or time.perf_counter() - state.began >= NO_SUCCESS_SECONDS
    ):
        return "no_success"
    if not state.unique and sample >= ZERO_IPS_STOP:
        return "no_ips" if totals.successes else "no_success"
    if (
        state.last_success is not None
        and time.perf_counter() - state.last_success >= config.stall_seconds
    ):
        return "stalled"
    if sample >= config.min_sample and sample > 2 * len(state.unique):
        return "unique_ratio"
    elapsed = time.perf_counter() - state.began
    if config.cap_seconds is not None and elapsed >= config.cap_seconds:
        return "cap"
    if len(state.unique) < LOW_IPS and _low_spent(config, totals.completed, elapsed):
        return "cap"
    return None


def _low_spent(config: RunConfig, completed: int, elapsed: float) -> bool:
    """Whether a low-availability run has used its requests or its time (RunConfig.low_*)."""
    return (config.low_requests is not None and completed >= config.low_requests) or (
        config.low_seconds is not None and elapsed >= config.low_seconds
    )


def _add_geo(state: _RunState, enricher: Enricher, result: Outcome) -> None:
    if result.ip and result.error is None:
        info = enricher.lookup(result.ip)
        key = (_part(info, LOCATION_FIELDS), _part(info, NETWORK_FIELDS))
    else:
        key = (None, None)
    if key not in state.geo_rows:
        state.geo_rows[key] = Counters()
    state.geo_rows[key].add(result)


def _add_ip(state: _RunState, address: str, observed: float) -> None:
    state.unique.add(address)
    if not state.aggregate_ips:
        return
    if address in state.ip_rows:
        row = state.ip_rows[address]
        row[0] += 1
        row[1] = min(row[1], observed)
        row[2] = max(row[2], observed)
    else:
        state.ip_rows[address] = [1, observed, observed]


def _record(state: _RunState, result: Outcome, completed_at: float, observed: float) -> bool:
    """Count one completed request; True when it is an exit to count and deliver."""
    # IPC from independent processes may arrive slightly out of order.
    # Bucket by coordinator receipt; retain true observation timestamps.
    bucket = bucket_of(state.began_wall + time.perf_counter() - state.began)
    state.totals.add(result)
    kind = state.answers.add(result, completed_at)
    state.halted = state.halted or state.answers.halt(completed_at)
    counted = kind == "answer" and result.ip and result.error is None
    if result.error is None:
        state.last_success = max(state.last_success or completed_at, completed_at)
        state.last_success_wall = max(state.last_success_wall or observed, observed)
    state.buckets.setdefault(bucket, Counters()).add(result)
    if state.current_bucket != bucket:
        state.flush()
        state.current_bucket = bucket
    if state.enricher is not None:
        _add_geo(state, state.enricher, result)
    if state.target.pool_eligible and counted:
        _add_ip(state, result.ip, observed)
    if state.gate is not None:
        _grow(state, time.perf_counter())
    # Preserve every attempt, status, error and byte count. Answer classes (and the
    # opt-in 469 policy) exclude rate limits from the sample and the request goal;
    # this worker then issues a replacement through the normal loop.
    if state.config.stop_rule == "unique_ratio" and not state.halted:
        state.halted = _census_halt(state)
    return bool(counted)


def _grow(state: _RunState, now: float) -> None:
    """Raise a paced run's in-flight limit to PACE_SHARE of its unique IPs so far (at most its
    grow_ceiling, else its concurrency), at most once every GROW_SECONDS; it never shrinks. A run
    without a low-availability cap may instead double it every RAMP_SECONDS (LOWAVAIL-PACE-3)."""
    config = state.config
    low = config.low_requests is not None or config.low_seconds is not None
    if now - state.grown_at < (GROW_SECONDS if low else RAMP_SECONDS):
        return
    state.grown_at = now
    ceiling = config.grow_ceiling or config.concurrency
    target = min(ceiling, int(len(state.unique) * PACE_SHARE))
    if not low:
        target = min(target, 2 * state.allowed)
    for _ in range(target - state.allowed):
        state.gate.release()
    state.allowed = max(state.allowed, target)


async def _make_room(state: _RunState) -> None:
    # Serialization never blocks the event loop or inflates in-flight
    # request latency. Slow storage applies backpressure before a request.
    pending = state.pending
    while pending and pending[0].done():
        pending[0].result()
        pending.popleft()
    if len(pending) >= 2:
        await asyncio.shield(asyncio.wrap_future(pending[0]))


async def _pace(state: _RunState) -> None:
    rate = state.config.requests_per_second
    if rate:
        now = time.perf_counter()
        scheduled = max(now, state.next_start)
        state.next_start = scheduled + 1 / rate
        await asyncio.sleep(max(0, scheduled - now))


async def _deliver(
    consumer: Callable[[str, float], Awaitable[None]], address: str, observed: float
) -> None:
    # A completed response must reach the writer even if a deadline
    # cancels its worker during backpressure. The writer owns draining.
    delivery = asyncio.ensure_future(consumer(address, observed))
    try:
        await asyncio.shield(delivery)
    except asyncio.CancelledError:
        await delivery
        raise


async def _worker(state: _RunState, transport: Transport, worker_index: int) -> None:
    while state.may_start():
        await _make_room(state)
        await _pace(state)
        async with state.gate or contextlib.nullcontext():
            if not state.may_start():
                break
            await _request(state, transport, worker_index)
        # Real I/O yields; injected fast transports must also be cancellable.
        if state.totals.completed % 256 == 0:
            await asyncio.sleep(0)


async def _request(state: _RunState, transport: Transport, worker_index: int) -> None:
    """Start one request, count its answer and deliver its exit."""
    context = RequestContext(state.run_id, state.started, worker_index)
    state.started += 1
    result = await transport.execute(state.target, context=context)
    if state.expired():
        raise asyncio.CancelledError
    # Anchor UTC to the monotonic clock: a clock adjustment cannot
    # reopen an already flushed bucket or reverse first/last seen.
    completed_at = result.completed_at if result.completed_at is not None else time.perf_counter()
    observed = state.began_wall + (completed_at - state.began)
    counted = _record(state, result, completed_at, observed)
    if state.observation_consumer is not None and counted:
        await _deliver(state.observation_consumer, result.ip, observed)


async def _watch_stop(state: _RunState, workers: list[asyncio.Task], engine: Any) -> None:
    stop = state.stop
    while not state.expired() and not state.stopped():
        if stop is not None:
            try:
                await asyncio.wait_for(stop.wait(), 0.05)
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(0.05)
    for task in workers:
        task.cancel()
    if state.parallel:
        engine.abort()


async def _drive(state: _RunState, engine: Any, transport: Transport) -> None:
    limit = state.limit
    workers = [
        asyncio.create_task(_worker(state, transport, index))
        for index in range(
            state.config.concurrency if limit is None else min(state.config.concurrency, limit)
        )
    ]
    helpers: list[asyncio.Task] = []
    if state.stop or state.deadline is not None:
        helpers.append(asyncio.create_task(_watch_stop(state, workers, engine)))
    if state.progress is not None:
        helpers.append(asyncio.create_task(_tick(state)))
    try:
        await asyncio.gather(*workers)
    finally:
        for task in (*workers, *helpers):
            task.cancel()
        await asyncio.gather(*workers, *helpers, return_exceptions=True)


async def _execute(
    state: _RunState, transport_factory: Callable[[RunConfig], Transport]
) -> tuple[str, bool, BaseException | None]:
    """Run the workers; the status, whether to re-raise cancellation, and any failure."""
    try:
        engine = (
            ProcessTransport(state.config, state.target, deadline=state.deadline_mono)
            if state.parallel
            else transport_factory(state.config)
        )
        async with engine as transport:
            if state.parallel:
                state.execution["worker_pids"] = list(transport.pids)
            await _drive(state, engine, transport)
    except asyncio.CancelledError:
        return "cancelled", not state.stopped() and not state.expired(), None
    except Exception as exc:  # boundary: saved as a failed run; run() raises only its type
        return "failed", False, exc
    return "completed", False, None


def _outcome(state: _RunState, status: str) -> tuple[str, str]:
    """The final status and the stop reason."""
    limit, answers, totals = state.limit, state.answers, state.totals
    started, halted = state.started, state.halted
    reached = limit is not None and answers.started(totals.completed) >= limit
    if state.stopped() and (started > totals.completed or not (reached or halted)):
        status = "cancelled"
    if state.expired() and status != "failed":
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
    return status, stop_reason


def _summary(state: _RunState, status: str, stop_reason: str, elapsed: float) -> RunSummary:
    target, totals, answers, enricher = state.target, state.totals, state.answers, state.enricher
    limit, started, unique, buckets = state.limit, state.started, state.unique, state.buckets
    began_wall, last_success_wall = state.began_wall, state.last_success_wall
    summary = RunSummary(
        run_id=state.run_id,
        schema_version="1",
        framework_version=__version__,
        target={
            "id": target.id,
            "version": target.version,
            "url": target.build_request().url,
            "engine": target.engine,
        },
        config=state.config.public() | state.execution,
        started_at=iso(began_wall),
        finished_at=iso(began_wall + elapsed),
        duration_seconds=elapsed,
        status=status,
        requested=limit,
        started=started,
        completed=totals.completed,
        cancelled=started - totals.completed,
        # An unbounded run has no unstarted remainder.
        not_started=0 if limit is None else limit - answers.started(started),
        successes=totals.successes,
        total_errors=totals.errors.total(),
        errors=dict(totals.errors),
        status_codes=dict(totals.statuses),
        success_rate=totals.successes / totals.completed if totals.completed else None,
        requests_per_second=totals.completed / elapsed if elapsed else 0,
        unique_ips=len(unique) if target.pool_eligible else None,
        pool_eligible=target.pool_eligible,
        latency_ms=totals.histogram.percentiles(),
        latency_mean_ms=totals.histogram.mean(),
        latency_histogram=totals.histogram.export(),
        response_body_bytes=totals.body_bytes,
        buckets=[
            {"bucket_start": iso(key), **value.export()} for key, value in sorted(buckets.items())
        ],
        **totals.verdicts(),
        **answers.summary(totals),
        stop_reason=stop_reason,
        outage_started_at=iso(last_success_wall)
        if stop_reason == "stalled" and last_success_wall is not None
        else None,
        redirects=totals.redirects if state.protocol == "mixed" else None,
    )
    release = getattr(enricher, "release", None)
    if enricher is not None and isinstance(release, dict):
        # JsonSink adds file, rows and checksum to this entry.
        summary.artifacts = {"geo": {"release": release}}
    return summary


async def _finish(state: _RunState, summary: RunSummary) -> None:
    try:
        if state.progress is not None:
            await _report(state)
        state.flush(final=True)
        for future in state.pending:
            await asyncio.shield(asyncio.wrap_future(future))
        await asyncio.to_thread(state.sink.finish, summary)
    finally:
        # No writer may outlive the stream it owns, including on disk failure.
        state.writer.shutdown(wait=True)
        state.sink.abort()
