"""Browser targets in the calculated snapshot (BP-CALC, browser pulse K-BP6 and D9).

The main gate is test_browser_runs_leave_every_network_figure_byte_identical: one database of
Generic census, pulse and load runs for three products, calculated before and after browser
runs of all eight run targets (every outcome, two hours) are added, gives the same snapshot
byte for byte once the browser-only entries are removed.
"""

import copy
import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from statistics import mean

import pytest
from helpers import FakeTransport
from psycopg.rows import dict_row
from test_calculated import lenient_policy
from test_census_pulse import batch, batch_run, product
from test_storage import ALL_ATTRIBUTES, enrichment, measurement
from test_storage_v6 import census, load, save, write_geo

from proxy_index import traffic
from proxy_index.browser import contracts
from proxy_index.calculated import browser, store
from proxy_index.calculated.aggregate import timed
from proxy_index.calculated.calculator import calculate
from proxy_index.calculated.inputs import cutoff
from proxy_index.calculated.policy import load as load_policy
from proxy_index.calculated.policy import validate
from proxy_index.models import Outcome
from proxy_index.stats import Histogram
from proxy_index.storage import GEO_COUNTERS, connect, ingest, seed
from proxy_index.targets import BrowserTarget, registry

pytest_plugins = ["test_storage"]

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
    """A bucket row as build() makes it, of a browser run."""
    return {
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


# --- pure -------------------------------------------------------------------------------------


def test_split_by_batch_kind_then_engine_keeps_order():
    rows = [
        {"target": "generic_http", "test": "pulse"},
        {"target": "amazon_pdp", "test": "browser"},
        {"target": "generic_https", "test": None},
        {"target": "google_search", "test": None},  # an unbatched run: the target's engine
        {"target": "ai_gemini", "test": None},  # K-BP1 browser target, not registered yet
        {"target": "generic_http", "test": "browser"},  # a browser batch, whatever the target
        {"target": "custom_echo", "test": "load"},  # unknown: http
    ]
    network, found = browser.split(rows)
    assert network == [rows[0], rows[2], rows[6]]
    assert found == [rows[1], rows[3], rows[4], rows[5]]
    assert browser.engine("ai_answers") == "browser" and browser.engine("custom_echo") == "http"


def test_outcome_rates_sum_to_one_and_match_the_counters():
    t = datetime(2026, 11, 4, tzinfo=UTC)
    rows = [
        row(
            t,
            20,
            8,
            errors={
                "challenge": 2,
                "blocked": 1,
                "login_wall": 3,
                "timeout": 1,
                "dns": 1,
                "tls": 4,
            },
            challenges=2,
            blocks=1,
            timeouts=1,
            proxy_errors=1,
        ),
        row(
            t,
            10,
            2,
            errors={"no_content": 2, "no_answer": 1, "tls": 3, "browser_error": 2},
        ),
    ]
    rates = browser.outcome_rates(rows)
    assert list(rates) == list(browser.OUTCOMES)
    assert rates == {
        "ok": 10 / 30,
        "challenge": 2 / 30,
        "block": 1 / 30,
        "login_wall": 3 / 30,
        "timeout": 1 / 30,
        "proxy_error": 1 / 30,
        "no_content": 2 / 30,
        "no_answer": 1 / 30,
        "other_error": 9 / 30,
    }
    assert abs(sum(rates.values()) - 1) < 1e-9
    # Nothing completed (no rows, or rows of no try): every rate is null.
    assert set(browser.outcome_rates([]).values()) == {None}
    assert set(browser.outcome_rates([row(t, 0, 0)]).values()) == {None}


def test_page_load_merges_the_success_histograms():
    t = datetime(2026, 11, 4, tzinfo=UTC)
    rows = [row(t, 5, 3, [(100, 2), (400, 1)]), row(t, 2, 1, [(1000, 1)]), row(t, 4, 0)]
    expected = Histogram(Counter({100: 2, 400: 1, 1000: 1})).percentiles()
    assert browser.page_load(rows) == expected
    assert expected == {"p50": 100, "p90": 1000, "p95": 1000, "p99": 1000}
    assert browser.page_load([row(t, 3, 0)]) == dict.fromkeys(("p50", "p90", "p95", "p99"))


def test_browser_targets_order_window_and_group():
    since = datetime(2026, 11, 4, tzinfo=UTC)
    end = since + timedelta(hours=24)
    rows = [
        row(since - timedelta(minutes=5), 1, 1, target="amazon_pdp"),  # before the window
        row(since, 2, 1, [(10, 1)], target="ai_gemini"),
        row(since, 3, 3, [(20, 3)], target="ai_overview"),
        row(since + timedelta(hours=1), 4, 0, target="zz_custom"),
        row(since + timedelta(hours=2), 5, 2, [(30, 2)], target="ai_gemini"),
        row(since + timedelta(hours=3), 6, 6, [(40, 6)], target="google_search"),
        row(end, 7, 7, target="google_search"),  # the window is half-open
    ]
    groups = {GROUP: SOURCES}
    found = browser.browser_targets(rows, since, end, groups)
    assert list(found) == ["google_search", GROUP, "ai_overview", "ai_gemini", "zz_custom"]
    assert found["google_search"] == [rows[5]]
    assert found["ai_gemini"] == [rows[1], rows[4]]
    # The group: its sources' rows in their original order (ai_chatgpt has none).
    assert found[GROUP] == browser.group_target(rows[1:6], SOURCES) == [rows[1], rows[2], rows[4]]
    assert browser.browser_targets(rows, since, end, {}).keys() == {
        "google_search",
        "ai_overview",
        "ai_gemini",
        "zz_custom",
    }
    assert browser.browser_targets(rows[:1], since, end, groups) == {}
    group = browser.fields(GROUP, found[GROUP], groups)
    assert (group["group"], group["sources"]) == (None, SOURCES)
    assert group["outcome_rates"]["ok"] == 6 / 10
    source = browser.fields("ai_gemini", found["ai_gemini"], groups)
    assert (source["group"], source["sources"]) == (GROUP, None)
    page = browser.fields("google_search", found["google_search"], groups)
    assert (page["group"], page["sources"]) == (None, None)
    assert page["page_load_ms"]["p50"] == 40


def test_targets_lists_network_targets_first_then_browser_targets():
    since = datetime(2026, 11, 4, tzinfo=UTC)
    network = [row(since, 1, 1, target="generic_https", test="pulse")]
    found = [row(since, 2, 1, target="ai_chatgpt")]
    config = {"required_targets": ["generic_http", "generic_https"], "target_groups": {}}
    listed = browser.targets(config, network, found, since, since + timedelta(hours=1))
    assert [(t, own, sorted(extra)) for t, own, extra in listed] == [
        ("generic_http", [], []),
        ("generic_https", network, []),
        ("ai_chatgpt", found, ["browser"]),
    ]
    assert listed[2][2]["browser"]["group"] is None  # no [target_groups]: no group


def test_methodology_target_groups():
    config, _ = load_policy("configs/methodology.toml")
    assert config["target_groups"] == {GROUP: SOURCES}
    assert not set(config["required_targets"]) & set(contracts.BROWSER_TARGETS)
    for groups in (
        [],
        {GROUP: []},
        {GROUP: "ai_overview"},
        {GROUP: ["ai_overview", "ai_overview"]},
        {GROUP: ["ai_overview"], "other": ["ai_overview"]},
        {GROUP: [GROUP]},
        {GROUP: ["ai_overview"], "other": [GROUP]},
        {GROUP: [""]},
        {GROUP: [3]},
    ):
        with pytest.raises(ValueError, match="Invalid target groups"):
            validate(copy.deepcopy(config) | {"target_groups": groups})
    # Browser, group and source targets are per-target only: never required (D9 by construction).
    for target in ("amazon_pdp", GROUP, "ai_chatgpt"):
        required = copy.deepcopy(config)
        required["required_targets"] = [*config["required_targets"], target]
        required["benchmark_weights"] = config["benchmark_weights"] | {target: 1}
        with pytest.raises(ValueError, match="cannot be a required target"):
            validate(required)
    del config["target_groups"]
    assert validate(config)  # optional


def test_traffic_snapshot_keeps_the_browser_flag():
    by_test = {
        "pulse": traffic.totals([{"runs": 1, "requests": 2, "bytes_up": 3, "bytes_down": 4}]),
        "browser": traffic.totals([{"runs": 5, "requests": 6, "bytes_up": 7, "bytes_down": 8}])
        | {"bytes_exact": False},
    }
    assert traffic.snapshot(by_test) == {
        "pulse": {"runs": 1, "requests": 2, "bytes_up": 3, "bytes_down": 4},
        "browser": {
            "runs": 5,
            "requests": 6,
            "bytes_up": 7,
            "bytes_down": 8,
            "bytes_exact": False,
        },
    }
    with pytest.raises(ValueError, match="census, pulse, load or browser"):
        traffic.summary(None, traffic.TrafficInputs(test="other"))


# --- PostgreSQL: the equality gate ------------------------------------------------------------


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


@pytest.mark.asyncio
async def test_browser_runs_leave_every_network_figure_byte_identical(
    database, tmp_path, ai_sources
):
    """Fixture: 3 products x (census + 2 pulses + load), then 3 x 8 targets x 2 hours = 48
    browser runs (4 to 11 tries each, every K-BP2 outcome) with batch runs and geo rows."""
    as_of = await generic(database, tmp_path)
    policy = lenient_policy(tmp_path)
    before = calculate(database, policy, as_of=as_of)
    summaries = await add_browser_runs(database, tmp_path)
    after = calculate(database, policy, as_of=as_of)
    assert after["snapshot_id"] != before["snapshot_id"]
    a, b = network_only(before), network_only(after)
    # Byte-identical serializations (hashes keep a failure's report small: the first difference).
    digest = [hashlib.sha256(json.dumps(x).encode()).hexdigest() for x in (a, b)]
    assert digest[0] == digest[1], first_difference(a, b)
    # The comparison is not empty: three products with scores, uptime and breakdowns.
    assert [p["provider"] for p in a["products"]] == list(PROVIDERS)
    day = a["products"][0]["windows"]["24h"]
    assert day["metrics"]["completed"] and day["breakdowns"]["country"]
    assert day["scores"]["balanced"]["state"] and day["uptime"]["value"] is not None
    assert "browser" not in before["traffic"]["by_test"]
    assert after["traffic"]["by_test"]["browser"]["bytes_exact"] is False
    assert after["traffic"]["by_test"]["browser"]["runs"] == 48
    assert [n["provider"] for n in a["networks"]] == list(PROVIDERS)
    for p in [*after["products"], *after["networks"]]:  # networks: per target too (NET-SUM-1)
        found = p["windows"]["24h"]["targets"]
        listed = [t for t in found if t in contracts.BROWSER_TARGETS]
        assert listed == list(contracts.BROWSER_TARGETS)  # K-BP1 order, after the network ones
        assert list(found)[: -len(listed)] == ["generic_http", "generic_https"]
        for target in contracts.RUN_TARGETS:
            check_browser_target(found[target], target)
        minutes = p["windows"]["24h"]["metrics"]["time_slots"]["minutes"]
        check_group(found, union_rate(summaries, p["provider"], minutes))


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


@pytest.mark.asyncio
async def test_a_window_without_browser_runs_has_no_browser_entries(database, tmp_path, ai_sources):
    as_of = await generic(database, tmp_path)
    payload = calculate(database, lenient_policy(tmp_path), as_of=as_of)
    for p in payload["products"]:
        for w in p["windows"].values():
            assert list(w["targets"]) == ["generic_http", "generic_https"]
            assert all("browser" not in t for t in w["targets"].values())


def test_group_rate_is_timed_over_the_union():
    t = datetime(2026, 11, 4, tzinfo=UTC)
    rows = [
        row(t, 10, 10, [(1, 10)], target="ai_overview"),
        row(t + timedelta(hours=1), 10, 0, target="ai_chatgpt"),
        row(t, 30, 0, target="ai_gemini"),
        row(t, 50, 50, [(1, 50)], target="amazon_pdp"),  # no source: not in the group
    ]
    union = browser.group_target(rows, SOURCES)
    m = timed(union, 24, 1, slot_minutes=30)
    # Slot one: 10 of 40 succeeded; slot two: none of 10. Each slot weighs the same.
    assert m["success_rate"] == (10 / 40 + 0) / 2
    assert m["request_success_rate"] == 10 / 50


@pytest.mark.asyncio
async def test_a_browser_run_in_the_cutoff_bucket_never_moves_the_cutoff(
    database, tmp_path, ai_sources
):
    """An off-grid cutoff whose bucket holds only a finished browser run stays at the bucket's
    start, as without it (else every network window would shift by 5 minutes), for an explicit
    cutoff and for a calculation of everything stored (latest)."""
    grid = await generic(database, tmp_path)  # after every network run's buckets
    requested = grid + timedelta(minutes=2, seconds=30)
    policy = lenient_policy(tmp_path)
    before = calculate(database, policy, as_of=requested)
    selection = tries("amazon_pdp", 1)
    directory = await measurement(
        tmp_path,
        requests=len(selection),
        target="amazon_pdp",
        provider=PROVIDERS[0],
        concurrency=1,
        requested_country="us",
        transport_factory=browser_transport(selection),
    )
    summary = load(directory)
    summary["started_at"] = (grid + timedelta(seconds=10)).isoformat(timespec="microseconds")
    summary["finished_at"] = (grid + timedelta(seconds=60)).isoformat(timespec="microseconds")
    assert len(summary["buckets"]) == 1
    summary["buckets"][0]["bucket_start"] = grid.isoformat(timespec="microseconds")
    save(directory, summary)
    with connect(database) as conn:
        batch_id = batch(conn, "browser", grid, country="US", target="amazon_pdp")
    key = ingest(directory, database, batch_id=batch_id)["run_key"]
    with connect(database) as conn:
        batch_run(
            conn,
            batch_id,
            product(conn, PROVIDERS[0]),
            grid + timedelta(seconds=60),
            run_key=key,
            requests=summary["completed"],
            successes=summary["successes"],
            bytes_down=summary["bytes_down"],
        )
        conn.row_factory = dict_row
        assert cutoff(conn, requested, True) == cutoff(conn, requested, False) == grid
    after = calculate(database, policy, as_of=requested)
    assert before["as_of"] == after["as_of"] == grid.isoformat()
    assert first_difference(network_only(before), network_only(after)) is None
