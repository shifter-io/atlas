"""Shared runs, rows and checks of the browser-in-the-calculated-snapshot tests
(test_calculated_browser*.py): K-BP2 tries, a database of Generic and browser runs, and the
payload comparison."""

import copy
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from statistics import mean

import pytest
from census_pulse_cases import batch, batch_run, product
from helpers import FakeTransport
from test_storage import ALL_ATTRIBUTES, enrichment, measurement
from test_storage_v6 import census, load, write_geo

from proxy_index.browser import contracts
from proxy_index.calculated import browser, store
from proxy_index.models import Outcome
from proxy_index.storage import GEO_COUNTERS, connect, ingest, seed
from proxy_index.targets import BrowserTarget, registry

PROVIDERS = ("alpha", "beta", "gamma")
GROUP = contracts.AI_ANSWERS
SOURCES = list(contracts.AI_SOURCES)
# One try of each K-BP2 outcome (two successes first); a run takes the first n of them.
TRIES = (
    Outcome(120.0, status=200, bytes_down=4000),
    Outcome(130.0, status=200, bytes_down=4000),
    Outcome(900.0, error="challenge", verdict="challenge", status=403),
    Outcome(800.0, error="blocked", verdict="block", status=429),
    Outcome(700.0, error="login_wall", verdict="error", status=200),
    Outcome(45000.0, error="timeout", verdict="timeout"),
    Outcome(50.0, error="proxy_connect", verdict="proxy_error"),
    Outcome(600.0, error="no_content", verdict="error", status=200),
    Outcome(650.0, error="no_answer", verdict="error", status=200),
    Outcome(500.0, error="browser_error", verdict="error"),
    Outcome(400.0, error="tls", verdict="error"),
)
HOURS = (1, 2)


def outcome(o):
    """The K-BP6 outcome of one try, by hand."""
    if o.error is None:
        return "ok"
    if o.verdict in browser.COUNTERS:
        return o.verdict
    return o.error if o.error in browser.CODES else "other_error"


def tries(target, hours):
    return TRIES[: 4 + (contracts.RUN_TARGETS.index(target) + hours) % 8]


def row(at, completed, successes, bins=(), errors=None, *, target="amazon_pdp", **counters):
    """A bucket row as build() makes it, of a browser run (of `engine`, when given; none is a
    run from before the engine choice)."""
    engine = counters.pop("engine", None)
    return ({"engine": engine} if engine else {}) | {
        "bucket_start": at,
        "slot": at,
        "last_observed": at,
        "target": target,
        "test": counters.pop("test", "browser"),
        "counters": {
            "completed": completed,
            "successes": successes,
            "errors": errors or {},
            "response_body_bytes": 0,
            "latency_histogram": {"format": "ceil_ms_v1", "bins": [list(b) for b in bins]},
        }
        | counters,
    }


def browser_transport(selection):
    class Fake(FakeTransport):
        async def execute(self, target, *, context=None):
            return selection[context.request_index % len(selection)]

    return Fake


@pytest.fixture
def ai_sources(monkeypatch):
    """The three AI sources as runnable browser targets (BP-AI-TARGETS registers the real ones)."""
    for target in contracts.AI_SOURCES:
        if target not in {t.id for t in registry.all()}:
            source = BrowserTarget(target, "https://example.invalid/")
            monkeypatch.setitem(registry._targets, target, source)


def shifted(text, delta):
    return (datetime.fromisoformat(text) - delta).isoformat(timespec="microseconds")


async def browser_run(tmp_path, provider, target, hours):
    """A browser run of `tries(target, hours)`, moved `hours` back, with a geo artifact (unknown
    exits: a browser try records none) that must stay out of every breakdown."""
    selection = tries(target, hours)
    directory = await measurement(
        tmp_path,
        requests=len(selection),
        target=target,
        provider=provider,
        concurrency=1,
        requested_country="us",
        transport_factory=browser_transport(selection),
    )
    summary = load(directory)
    delta = timedelta(hours=hours)
    for key in ("started_at", "finished_at"):
        summary[key] = shifted(summary[key], delta)
    for bucket in summary["buckets"]:
        bucket["bucket_start"] = shifted(bucket["bucket_start"], delta)
    geo = [
        {"bucket_start": b["bucket_start"], "location": None, "network": None}
        | {k: b[k] for k in GEO_COUNTERS}
        | {"latency_histogram": b["latency_histogram"]}
        for b in summary["buckets"]
    ]
    return directory, write_geo(directory, summary, geo)


async def generic(database, tmp_path):
    """Census, pulse (both targets) and load runs of three products in one US slot, each with
    its batch run. Returns the cutoff, on the 5-minute grid after every run."""
    store.initialize(database)
    seed(database)
    slot = datetime.fromtimestamp(datetime.now(UTC).timestamp() // 1800 * 1800, UTC)
    with connect(database) as conn:
        batches = {
            (kind, target): batch(conn, kind, slot, country="US", target=target)
            for kind, target in (
                ("census", "generic_http"),
                ("pulse", "generic_http"),
                ("pulse", "generic_https"),
                ("load", "generic_http"),
            )
        }
    last = slot
    for provider in PROVIDERS:
        for (kind, target), batch_id in batches.items():
            if kind == "census":
                directory, summary = await census(tmp_path, provider=provider)
                enrichment(directory, attributes=ALL_ATTRIBUTES)
            else:
                directory = await measurement(
                    tmp_path,
                    provider=provider,
                    target=target,
                    requested_country="us",
                    concurrency=1000 if kind == "load" else 100,
                )
                if target == "generic_http":
                    enrichment(directory, attributes=ALL_ATTRIBUTES)
                summary = load(directory)
            key = ingest(directory, database, batch_id=batch_id)["run_key"]
            finished = datetime.fromisoformat(summary["finished_at"])
            with connect(database) as conn:
                batch_run(
                    conn,
                    batch_id,
                    product(conn, provider),
                    finished,
                    run_key=key,
                    requests=summary["completed"],
                    successes=summary["successes"],
                    bytes_up=summary.get("bytes_up", 0),
                    bytes_down=summary.get("bytes_down", 0),
                )
            last = max(last, finished)
    return datetime.fromtimestamp((last + timedelta(minutes=5)).timestamp() // 300 * 300 + 300, UTC)


async def add_browser_runs(database, tmp_path):
    """Every provider runs all eight run targets in each of the last two hours. Returns their
    summaries by (provider, target, hours)."""
    summaries = {}
    for hours in HOURS:
        for target in contracts.RUN_TARGETS:
            for provider in PROVIDERS:
                directory, summary = await browser_run(tmp_path, provider, target, hours)
                summaries[provider, target, hours] = summary
                started = datetime.fromisoformat(summary["started_at"])
                with connect(database) as conn:
                    batch_id = batch(conn, "browser", started, country="US", target=target)
                key = ingest(directory, database, batch_id=batch_id)["run_key"]
                with connect(database) as conn:
                    batch_run(
                        conn,
                        batch_id,
                        product(conn, provider),
                        datetime.fromisoformat(summary["finished_at"]),
                        run_key=key,
                        requests=summary["completed"],
                        successes=summary["successes"],
                        bytes_down=summary["bytes_down"],
                    )
    return summaries


def network_only(payload):
    """The payload without its browser entries and the per-build values."""
    payload = copy.deepcopy(payload)
    for key in ("generated_at", "snapshot_id"):
        del payload[key]
    payload["traffic"]["by_test"].pop("browser", None)
    for p in [*payload["products"], *payload["networks"]]:
        for w in p["windows"].values():
            for target in contracts.BROWSER_TARGETS:
                w["targets"].pop(target, None)
    for n in payload["networks"]:  # each build writes its own network lists (NET-SUM-1)
        for w in n["windows"].values():
            w["lists"] = w["lists"]["entries"]
    return payload


def first_difference(a, b, path="$"):
    """The path of the first difference of two JSON values, with both sides (shortened)."""
    if isinstance(a, dict) and isinstance(b, dict):
        if list(a) != list(b):
            return f"{path}: keys {list(a)} != {list(b)}"
        pairs = [(f"{path}.{k}", a[k], b[k]) for k in a]
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        pairs = [(f"{path}[{i}]", x, y) for i, (x, y) in enumerate(zip(a, b, strict=True))]
    elif a == b and type(a) is type(b):
        return None
    else:
        return f"{path}: {json.dumps(a)[:300]} != {json.dumps(b)[:300]}"
    return next((d for at, x, y in pairs if (d := first_difference(x, y, at))), None)


def check_browser_target(entry, target):
    expected = Counter(outcome(o) for hours in HOURS for o in tries(target, hours))
    completed = sum(expected.values())
    rates = entry["browser"]["outcome_rates"]
    assert abs(sum(rates.values()) - 1) < 1e-9
    assert rates == {k: expected[k] / completed for k in browser.OUTCOMES}
    assert entry["metrics"]["completed"] == completed
    # Page loads are the successes' only (120 and 130 ms; failures took up to 45 s).
    assert entry["browser"]["page_load_ms"] == {"p50": 120, "p90": 130, "p95": 130, "p99": 130}
    source = target in SOURCES
    assert entry["browser"]["group"] == (GROUP if source else None)
    assert entry["browser"]["sources"] is None
    # Camoufox runs only: its one engine holds every try.
    assert entry["browser"]["engine"] == "camoufox" and list(entry["browser"]["engines"]) == [
        "camoufox"
    ]
    assert entry["browser"]["engines"]["camoufox"]["completed"] == completed


def check_group(found, rate):
    group = found[GROUP]
    assert group["browser"]["sources"] == SOURCES and group["browser"]["group"] is None
    union = Counter()
    for target in SOURCES:
        union += Counter(outcome(o) for hours in HOURS for o in tries(target, hours))
    total = sum(union.values())
    assert group["metrics"]["completed"] == total
    assert group["browser"]["outcome_rates"] == {k: union[k] / total for k in browser.OUTCOMES}
    # Its success rate is time-weighted over the union of its sources' rows.
    assert group["metrics"]["success_rate"] == pytest.approx(rate, abs=1e-12)


def union_rate(summaries, provider, minutes):
    """The time-slot success rate of a product's AI source runs together, from their artifacts:
    the mean over time slots of successes / completed (a browser request's slot is its
    bucket's)."""
    slots = defaultdict(Counter)
    for (name, target, _), summary in summaries.items():
        if name == provider and target in SOURCES:
            for b in summary["buckets"]:
                slot = datetime.fromisoformat(b["bucket_start"]).timestamp() // (minutes * 60)
                slots[slot].update(completed=b["completed"], successes=b["successes"])
    return mean(s["successes"] / s["completed"] for s in slots.values())
