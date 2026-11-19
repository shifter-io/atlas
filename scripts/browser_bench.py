#!/usr/bin/env python3
"""Bench one browser engine on a browser host: N tries of one target, B at a time
(docs/browser.html, Capture: Bench).

Run from the host's installed app checkout, for the owner's approved engine comparison only:

    uv run python scripts/browser_bench.py --settings STATE/settings.toml --state STATE \
        --host browser-1 --network provider/product --target amazon_pdp --tries 100 \
        --engine chrome --browsers 8 --country us --out DIR

Each try is the host's own, built as scripts/browser_capture.py builds it (network files,
settings, bound target, pool and transport), on Camoufox (driver.launch) or the host's Google
Chrome (driver.launch_chrome). Up to --browsers tries run at once, each on a new context or
profile; the next starts as soon as one ends. Into DIR/<engine>/<target>/ it writes
tries.jsonl (the host's try records with the engine, start, end and duration), samples.jsonl
(procstats: CPU % and RSS of this process's tree, once a second) and summary.json. Nothing
holds cookies, headers, credentials, proxy usernames, exit IPs or screenshots. It uses no
orchestrator and no database: nothing reaches production figures.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import statistics
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from browser_capture import _append, _json, _write, camoufox_launch, prepare, scrub

from atlas.browser import chrome, driver, procstats
from atlas.browser.node import try_seconds
from atlas.browser.pool import BrowserPool
from atlas.browser.settings import MAX_BROWSERS_PER_HOST, load_settings
from atlas.browser.transport import BrowserTransport
from atlas.campaign.sampler import percentile
from atlas.errors import UserError
from atlas.models import Outcome, RequestContext, RunConfig

ENGINES = ("camoufox", "chrome")
MAX_TRIES = 500
MAX_BROWSERS = MAX_BROWSERS_PER_HOST
OUTCOMES = ("ok", "challenge", "block", "timeout", "proxy_error")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Recorder:
    """The transport's recorder: each try record, with the engine, its span and the relay's
    exact wire bytes (`wire_up`, `wire_down`, `wire_exact`), appended to tries.jsonl as it
    ends. Screenshots are dropped."""

    def __init__(self, path: Path, engine: str) -> None:
        self.path, self.engine = path, engine
        self.started: dict[int, str] = {}
        self.pending: dict[int, dict[str, Any]] = {}
        self.records: list[dict[str, Any]] = []

    def try_done(self, record: dict[str, Any], screenshot: bytes | None) -> None:
        self.pending[record["i"]] = record | {
            "final_url": scrub(record["final_url"]),
            "engine": self.engine,
            "started_at": self.started[record["i"]],
            "ended_at": _now(),
            "duration_ms": record["timings_ms"]["total"],
        }

    def finish(self, index: int, outcome: Outcome) -> None:
        """The try's record with its outcome's wire bytes, written once execute() returned."""
        wire = {"wire_up": outcome.bytes_up, "wire_down": outcome.bytes_down}
        record = self.pending.pop(index) | wire | {"wire_exact": outcome.bytes_exact}
        self.records.append(record)
        _append(self.path, (_json(record) + "\n").encode())


def _stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return dict.fromkeys(("average", "median", "p95", "max"))
    average = round(sum(values) / len(values), 1)
    median = statistics.median(values)
    return {
        "average": average,
        "median": median,
        "p95": percentile(values, 0.95),
        "max": max(values),
    }


def summarize(records: list[dict], samples: list[dict], run: dict[str, Any]) -> dict[str, Any]:
    """summary.json: what ran (`run`), the outcome counts, request time, bytes, and the CPU %
    and RSS of the samples."""
    verdicts = Counter(record["verdict"] for record in records)
    outcomes = {verdict: verdicts[verdict] for verdict in OUTCOMES}
    outcomes["other"] = len(records) - sum(outcomes.values())
    others = Counter(r["error"] for r in records if r["verdict"] not in OUTCOMES)
    sizes = [record["bytes_down_est"] or 0 for record in records]
    down = [record["wire_down"] for record in records if record.get("wire_down") is not None]
    up = [record["wire_up"] for record in records if record.get("wire_up") is not None]
    machine = procstats.summary(samples)
    return run | {
        "outcomes": outcomes,
        "other_errors": dict(sorted(others.items(), key=lambda item: str(item[0]))),
        "request_ms": _stats([record["duration_ms"] for record in records]),
        "bytes": {
            "total": sum(sizes),
            "per_try": round(sum(sizes) / len(sizes)) if sizes else None,
        },
        "wire": {
            "down_total": sum(down),
            "down": _stats(down),
            "up_per_try": round(sum(up) / len(up)) if up else None,
        },
        "cpu_percent": machine["cpu_percent"],
        "rss_bytes": machine["rss_bytes"],
        "cpu_seconds": samples[-1]["cpu_seconds"] if samples else None,
        "samples": machine["samples"],
    }


def engine_launch(args: argparse.Namespace, plan: Any) -> Callable[[], Any]:
    """The chosen engine's launch with the host's settings; a missing or too old Chrome is
    refused (chrome.ChromeRefused) before any try."""
    if args.engine == "camoufox":
        return camoufox_launch(args.state, plan.browser)
    executable = load_settings(args.settings).chrome.executable
    chrome.detect(executable)
    return partial(
        driver.launch_chrome, executable, country=plan.country, headless=plan.browser.headless
    )


def _transport(plan: Any, args: argparse.Namespace, pool: BrowserPool, recorder: Recorder):
    config = RunConfig(
        target=plan.target.id,
        proxy=plan.proxy,
        requests=plan.tries,
        concurrency=args.browsers,
        provider=plan.provider,
        product=plan.product,
    )
    browser = replace(plan.browser, browsers_per_host=args.browsers)
    return BrowserTransport(
        config, pool=pool, browser=browser, settings=plan.entry, recorder=recorder
    )


async def _run(plan: Any, transport: BrowserTransport, recorder: Recorder, browsers: int) -> None:
    """The tries, at most `browsers` at once; the next starts as soon as one ends."""
    seconds = try_seconds(plan.target, transport.timeout_s)
    run_id, slots = str(uuid.uuid4()), asyncio.Semaphore(browsers)

    async def one(index: int) -> None:
        async with slots:
            recorder.started[index] = _now()
            # execute() takes its budget from the deadline before its first await.
            transport.deadline = time.time() + seconds
            outcome = await transport.execute(plan.target, context=RequestContext(run_id, index, 0))
            recorder.finish(index, outcome)

    try:
        async with asyncio.TaskGroup() as group:
            for index in range(plan.tries):
                group.create_task(one(index))
    except BaseExceptionGroup as failed:  # an engine refusal (UserAgentRefused) stops all
        raise failed.exceptions[0] from None


async def bench(plan: Any, directory: Path, launch: Callable, args: argparse.Namespace) -> dict:
    """Run the bench and write its files; returns the summary."""
    recorder, identities = Recorder(directory / "tries.jsonl", args.engine), []

    async def launching() -> Any:
        browser = await launch()
        identities.append(getattr(browser, "identity", {}))
        return browser

    recycle = args.recycle_after or plan.browser.recycle_after
    pool = BrowserPool(args.browsers, launching, recycle_after=recycle)
    transport = _transport(plan, args, pool, recorder)
    began, start = _now(), time.monotonic()
    descriptor = os.open(directory / "samples.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as out:
        async with procstats.TreeSampler(os.getpid(), out=out, read=args.read) as sampler, pool:
            await _run(plan, transport, recorder, args.browsers)
    run = {
        "host": args.host,
        "network": f"{plan.provider}/{plan.product}",
        "target": plan.target.id,
        "country": plan.country,
        "engine": args.engine,
        "identity": identities[0] if identities else {},
        "browsers": args.browsers,
        "recycle_after": recycle,
        "tries": len(recorder.records),
        "started_at": began,
        "wall_s": round(time.monotonic() - start, 1),
    }
    result = summarize(recorder.records, sampler.lines, run)
    _write(directory / "summary.json", _json(result, indent=2).encode())
    return result


def output_dir(out: Path, engine: str, target: str) -> Path:
    """<out>/<engine>/<target>/, made now (mode 700): an existing one is refused."""
    directory = out / engine / target
    directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        raise UserError(f"{directory} exists: one bench per directory") from None
    return directory


def parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--settings", type=Path, required=True, help="Host settings file")
    parser.add_argument("--state", type=Path, required=True, help="Host state directory")
    parser.add_argument("--host", required=True, help="Logical host name (browser-<n>)")
    parser.add_argument("--network", required=True, help="provider/product")
    parser.add_argument("--target", required=True, help="A [[target]] id of the settings")
    parser.add_argument("--tries", type=int, required=True, help=f"1 to {MAX_TRIES}")
    parser.add_argument("--engine", choices=ENGINES, required=True)
    parser.add_argument("--browsers", type=int, default=8, help=f"1 to {MAX_BROWSERS}")
    parser.add_argument("--recycle-after", type=int, help="Camoufox tries per process")
    parser.add_argument("--country", required=True, help="Lower-case alpha-2 (us)")
    parser.add_argument("--allow-country", help="Allow this other country (not used now)")
    parser.add_argument("--out", type=Path, required=True, help="Output directory")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, launch: Any = None, read: Any = procstats.read_ps) -> int:
    """Exit 0 when the tries ran (whatever their verdicts), 2 on a refused request, 1 when
    the engine was refused during the run."""
    args = parse(argv)
    args.state, args.read = args.state.expanduser(), read
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        if not 1 <= args.browsers <= MAX_BROWSERS:
            raise UserError(f"--browsers must be 1 to {MAX_BROWSERS}")
        if args.recycle_after is not None and args.recycle_after < 1:
            raise UserError("--recycle-after must be at least 1")
        plan = prepare(args, MAX_TRIES)
        launch = launch or engine_launch(args, plan)
        directory = output_dir(args.out, args.engine, plan.target.id)
    except UserError as error:
        print(f"browser_bench: {error}", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(bench(plan, directory, launch, args))
    except UserError as error:
        print(f"browser_bench: {error}", file=sys.stderr)
        return 1
    print(f"{result['tries']} tries benched into {directory}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
