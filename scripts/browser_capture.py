#!/usr/bin/env python3
"""Capture N live tries of one browser target on a browser host (docs/browser.html, Capture).

Run from the host's installed app checkout, for the owner's approved live checks only:

    uv run python scripts/browser_capture.py --settings STATE/settings.toml --state STATE \
        --host browser-1 --network provider/product --target amazon_pdp --tries 20 \
        --country us --out DIR

Each try is the host's own: its network files, settings, bound targets, pool and transport, on
one browser. For every try it writes DIR/<provider>/<product>/<target>/NNNN.json (the page view
judge() read, the verdict, the input, timings and bytes) and NNNN.jpg, then tries.jsonl (the
host's try records) and summary.md. An AI source's answered try keeps no screenshot and no
answer text. Nothing holds cookies, headers, credentials, proxy usernames or exit IPs. The
output is private data: it never goes into a product repository as it is.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from proxy_index.browser import driver, release
from proxy_index.browser.contracts import AI_SOURCES
from proxy_index.browser.node import bound_registry, load_network, try_seconds
from proxy_index.browser.pool import BrowserPool
from proxy_index.browser.settings import BrowserOptions, TargetSettings, load_settings
from proxy_index.browser.targets.ai import ANSWER_CHARS
from proxy_index.browser.transport import BrowserTransport
from proxy_index.errors import UserError
from proxy_index.models import ProxySource, RequestContext, RunConfig
from proxy_index.targets import BrowserTarget, PageView

MAX_TRIES = 50  # the owner's cap per target per network
COUNTRIES = ("us",)
# An address a page shows (a block page's "your IP") is the exit's: never written.
_HEX = "[0-9a-f]{1,4}"
_GROUPS = f"{_HEX}(?::{_HEX})*"
IP = re.compile(
    rf"\b(?:\d{{1,3}}\.){{3}}\d{{1,3}}\b"
    rf"|(?<![\w:])(?:(?:{_HEX}:){{3,7}}{_HEX}|{_GROUPS}::(?:{_GROUPS})?|::{_GROUPS})(?![\w:])",
    re.IGNORECASE,
)


def scrub(text: str | None) -> str | None:
    return None if text is None else IP.sub("<ip>", text)


@dataclass(frozen=True, slots=True)
class Plan:
    """One capture: the bound target, its [[target]], the [browser] options and the network."""

    target: BrowserTarget
    entry: TargetSettings
    browser: BrowserOptions
    provider: str
    product: str
    country: str
    tries: int
    proxy: ProxySource = field(repr=False)


def view_fields(target_id: str, view: PageView) -> dict[str, Any]:
    """The fixture "view" shape; an answered AI try's visible text becomes its length."""
    text, answer = view.text_head, view.found.get(ANSWER_CHARS, 0)
    if target_id in AI_SOURCES and answer > 0:
        text = f"<answer: {answer} chars>"
    return {
        "final_url": scrub(view.final_url),
        "status": view.status,
        "title": scrub(view.title),
        "text_head": scrub(text),
        "found": dict(view.found),
    }


class Capture:
    """The transport's recorder and view sink: writes each try once both have arrived."""

    def __init__(self, directory: Path, target_id: str) -> None:
        self.directory, self.target_id = directory, target_id
        self.records: list[dict[str, Any]] = []
        self.views: dict[int, PageView] = {}
        self.shots: dict[int, bytes] = {}
        self.no_view = 0

    def try_done(self, record: dict[str, Any], screenshot: bytes | None) -> None:
        self.records.append(record | {"final_url": scrub(record["final_url"])})
        if screenshot is not None:
            self.shots[record["i"]] = screenshot

    def view(self, index: int, view: PageView) -> None:
        self.views[index] = view

    def write(self, index: int) -> None:
        record = self.records[-1]
        view = self.views.pop(index, None)
        shot = self.shots.pop(index, None)
        self.no_view += view is None
        answered = view is not None and view.found.get(ANSWER_CHARS, 0) > 0
        if shot is not None and not (self.target_id in AI_SOURCES and answered):
            _write(self.directory / f"{index:04d}.jpg", shot)
        document = {
            "view": None if view is None else view_fields(self.target_id, view),
            "judged": {key: record[key] for key in ("verdict", "error", "reason")},
            "input": record["input"],
            "timings_ms": record["timings_ms"],
            "bytes_down_est": record["bytes_down_est"],
        }
        _write(self.directory / f"{index:04d}.json", _json(document, indent=2).encode())
        with (self.directory / "tries.jsonl").open("a") as stream:
            stream.write(_json(record) + "\n")


def _json(value: Any, **options: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, **options)


def _write(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
    path.chmod(0o600)


def summary(plan: Plan, host: str, capture: Capture, began: str) -> str:
    """summary.md: what ran and the count of each verdict, error and reason."""
    records = capture.records
    outcomes = Counter((r["verdict"], r["error"], r["reason"]) for r in records)
    loads = sum(r["timings_ms"]["context"] is not None for r in records)
    lines = [
        f"# Capture: {plan.target.id} on {plan.provider}/{plan.product}",
        "",
        f"- Host: {host}; country: {plan.country}; release: {release.RELEASE}",
        f"- Started: {began}; ended: {datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- Tries: {len(records)} of {plan.tries}; page loads used: {loads}",
        f"- Tries without a view (no page was read, a navigation error): {capture.no_view}",
        "",
        "| verdict | error | reason | tries |",
        "|---|---|---|---|",
    ]
    for (verdict, error, reason), n in sorted(outcomes.items(), key=lambda o: (-o[1], str(o[0]))):
        lines.append(f"| {verdict} | {error or '-'} | {reason or '-'} | {n} |")
    return "\n".join(lines) + "\n"


async def capture(plan: Plan, directory: Path, launch: Any, host: str) -> list[dict[str, Any]]:
    """Run the tries one after another on one browser and write their files."""
    began = datetime.now(UTC).isoformat(timespec="seconds")
    recorder = Capture(directory, plan.target.id)
    config = RunConfig(
        target=plan.target.id,
        proxy=plan.proxy,
        requests=plan.tries,
        concurrency=1,
        provider=plan.provider,
        product=plan.product,
    )
    page_timeout_s = plan.entry.timeout_s or plan.browser.page_timeout_s
    seconds = try_seconds(plan.target, page_timeout_s)
    pool = BrowserPool(1, launch, recycle_after=plan.browser.recycle_after)
    browser = replace(plan.browser, browsers_per_host=1, screenshots="all")
    transport = BrowserTransport(
        config,
        pool=pool,
        browser=browser,
        settings=plan.entry,
        recorder=recorder,
        view_sink=recorder.view,
    )
    run_id = str(uuid.uuid4())
    async with pool:
        for index in range(plan.tries):
            # A backstop per try: the transport records a try past it as timeout/deadline.
            transport.deadline = time.time() + seconds
            await transport.execute(plan.target, context=RequestContext(run_id, index, 0))
            recorder.write(index)
    (directory / "summary.md").write_text(summary(plan, host, recorder, began))
    return recorder.records


def check_request(tries: int, country: str, allowed: str | None) -> None:
    if not 1 <= tries <= MAX_TRIES:
        raise UserError(f"--tries must be 1 to {MAX_TRIES}")
    if country not in (*COUNTRIES, allowed):
        raise UserError(f"--country {country} needs --allow-country {country}")


def prepare(args: argparse.Namespace) -> Plan:
    """The host's own settings view, network files and bound target, read only."""
    check_request(args.tries, args.country, args.allow_country)
    settings = load_settings(args.settings).host(args.host)
    if args.network not in settings.hosts[0].networks:
        raise UserError(f"{args.network} is not a network of {args.host}")
    entry = next((t for t in settings.targets if t.id == args.target), None)
    if entry is None:
        raise UserError(f"{args.target} is not a [[target]] of the settings")
    network = load_network(args.state, args.network, settings)
    country = args.country.upper()
    return Plan(
        target=bound_registry(settings).get(entry.id),
        entry=entry,
        browser=settings.browser,
        provider=network.provider,
        product=network.product,
        country=country,
        tries=args.tries,
        proxy=network.proxy_factory(country),
    )


def output_dir(out: Path, plan: Plan) -> Path:
    """<out>/<provider>/<product>/<target>/, made now: an existing one is refused."""
    directory = out / plan.provider / plan.product / plan.target.id
    directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        raise UserError(f"{directory} exists: one capture per directory") from None
    return directory


def parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--settings", type=Path, required=True, help="Host settings file")
    parser.add_argument("--state", type=Path, required=True, help="Host state directory")
    parser.add_argument("--host", required=True, help="Logical host name (browser-<n>)")
    parser.add_argument("--network", required=True, help="provider/product")
    parser.add_argument("--target", required=True, help="A [[target]] id of the settings")
    parser.add_argument("--tries", type=int, required=True, help=f"1 to {MAX_TRIES}")
    parser.add_argument("--country", required=True, help="Lower-case alpha-2 (us)")
    parser.add_argument("--allow-country", help="Allow this other country (not used now)")
    parser.add_argument("--out", type=Path, required=True, help="Output directory")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, launch: Any = None) -> int:
    """Exit 0 when the tries ran (whatever their verdicts), 2 on a refused request."""
    args = parse(argv)
    args.state = args.state.expanduser()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        plan = prepare(args)
        directory = output_dir(args.out, plan)
    except UserError as error:
        print(f"browser_capture: {error}", file=sys.stderr)
        return 2
    if launch is None:
        executable = release.executable(args.state / "camoufox")
        options = {"locale": plan.browser.locale, "headless": plan.browser.headless}
        launch = partial(driver.launch, executable, **options)
    records = asyncio.run(capture(plan, directory, launch, args.host))
    print(f"{len(records)} tries written to {directory}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
