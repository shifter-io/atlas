"""CALC-PAGE-INPUTS-1 (W11): the calculator inputs of the new page answers.

- Per-target wire bytes: every target of every window, of a product group or a network and
  browser targets included, carries metrics.bytes_up and bytes_down, its rows' summed wire bytes
  (aggregate.metrics()). The snapshot already published them; this keeps them there.
- Per-kind fleet entries: one per kind and window, cohort "*:<kind>", the shape of the "*"
  entries over that kind's groups alone (fleet.fleet_totals()). The incremental build counts
  their IPs in the database and equals the full build.
- Nothing else changes: a build with them reads back as one without them but for those entries.

The database tests use the 3-day fixture of test_calculated_incremental (alpha with a browser
run, beta, gamma and delta), with beta's product made a datacenter one: two kinds, sharing IPs.
"""

import json
import random
from datetime import UTC, datetime, timedelta

import pytest
from calculated_cases import lenient_policy
from test_calc_fleet import bucket_rows
from test_calculated_hourly_reads import HOUR, floor_hour
from test_calculated_incremental import assert_equal, first_difference, history, without

from proxy_index.calculated import calculator, store
from proxy_index.calculated.calculator import calculate
from proxy_index.calculated.fleet import KIND, fleet_totals
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]

START = datetime(2026, 11, 1, tzinfo=UTC)
KINDS = {"alpha": "residential", "beta": "datacenter", "gamma": "residential"}
# Distinct IPs and summed wire bytes of some providers' runs in [start, end).
DISTINCT = (
    "SELECT count(DISTINCT o.ip_id) FROM ip_observations o JOIN test_runs r USING(run_key) "
    "JOIN scope_stats s USING(scope_id) JOIN products p USING(product_id) "
    "WHERE p.provider=ANY(%s) AND o.bucket_start>=%s AND o.bucket_start<%s"
)
BYTES = (
    "SELECT sum(b.bytes_up),sum(b.bytes_down) FROM run_buckets b JOIN test_runs r USING(run_key) "
    "JOIN scope_stats s USING(scope_id) JOIN products p USING(product_id) "
    "WHERE p.provider=%s AND s.target=%s AND b.bucket_start>=%s AND b.bucket_start<%s"
)


def members(provider, seed):
    """A synthetic fleet member of provider (its kind from KINDS) in the 24h and 7d windows:
    seeded bucket rows and observations of IPs drawn from 30, so groups share some."""
    rng, rows = random.Random(seed), bucket_rows(seed, n=60)
    seen = [{"ip_id": rng.randrange(30), "bucket_start": r["bucket_start"]} for r in rows]
    windows = {label: {"period": {"hours": h}} for label, h in (("24h", 24), ("7d", 168))}
    p = {"provider": provider, "kind": KINDS[provider], "windows": windows}
    return {label: (p, rows, seen, {}) for label in windows}


def totals(groups):
    return fleet_totals(groups, START + timedelta(days=1), 1, 30, "generic_http")


def test_each_kind_is_the_fleet_totals_of_its_groups_alone():
    """Three cohorts, two of residential groups: the "*" entries come first as before, then
    each kind's (by name), equal to the "*" entries of its groups alone."""
    cohorts = {"a": [("alpha", 1), ("gamma", 2)], "b": [("beta", 3)], "c": [("alpha", 4)]}
    groups = {}
    for cohort, found in cohorts.items():
        for label in ("24h", "7d"):
            groups[cohort, label] = [members(provider, seed)[label] for provider, seed in found]
    every = totals(groups)
    assert [(f["cohort"], f["window"]) for f in every] == [
        (cohort, label)
        for cohort in ("*", "*:datacenter", "*:residential")
        for label in ("24h", "7d")
    ]
    for kind in ("datacenter", "residential"):
        own = {k: ms for k, ms in groups.items() if ms[0][0]["kind"] == kind}
        alone = [f | {"cohort": KIND + kind} for f in totals(own) if f["cohort"] == "*"]
        assert [f for f in every if f["cohort"] == KIND + kind] == alone
    # Each IP once per entry, however many of its groups saw it.
    star, residential = every[0], every[4]
    day = [m for (_, label), ms in groups.items() if label == "24h" for m in ms]
    assert star["unique_ips"] == len({o["ip_id"] for _, _, seen, _ in day for o in seen})
    assert residential["unique_ips"] == len(
        {o["ip_id"] for p, _, seen, _ in day if p["kind"] == "residential" for o in seen}
    )
    assert (residential["products"], residential["providers"]) == (3, 2)
    assert set(residential) == set(star) and residential["series"]
    assert totals({}) == []


def two_kinds(tmp_path):
    """The lenient policy with beta's product a datacenter one."""
    policy = lenient_policy(tmp_path)
    policy.write_text(
        policy.read_text() + '\n[products."beta/residential"]\nkind = "datacenter"\n'
    )
    return policy


def read_back(database, payload):
    return without(store.read(database, payload["snapshot_id"]))


@pytest.mark.asyncio
async def test_kind_entries_and_target_bytes_in_both_modes(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    payload = assert_equal(database, two_kinds(tmp_path), end)
    fleet = {(f["cohort"], f["window"]): f for f in payload["fleet"]}
    providers = {"*:datacenter": ["beta"], "*:residential": ["alpha", "gamma", "delta"]}
    with connect(database) as conn:
        for label, hours in (("24h", 24), ("7d", 168), ("30d", 720)):
            since = end - timedelta(hours=hours)
            star = fleet["*", label]
            kinds = [fleet[cohort, label] for cohort in providers]
            for entry, names in zip(kinds, providers.values(), strict=True):
                count = conn.execute(DISTINCT, (names, since, end)).fetchone()[0]
                assert entry["unique_ips"] == count > 0, (entry["cohort"], label)
                assert len(entry["series"]) == len(star["series"])
            # The kinds split the requests; an IP both kinds saw is counted in each.
            assert sum(k["metrics"]["completed"] for k in kinds) == star["metrics"]["completed"]
            assert sum(k["products"] for k in kinds) == star["products"]
            assert sum(k["unique_ips"] for k in kinds) >= star["unique_ips"]
        browser = assert_target_bytes(conn, payload, end)
    assert browser > 0


def assert_target_bytes(conn, payload, end):
    """Every measured target of every window carries its wire bytes, a network's the sums of
    its provider's runs of the target; returns the browser targets seen."""
    browser = 0
    entries = [(g, False) for g in payload["products"]]
    for entry, network in entries + [(n, True) for n in payload["networks"]]:
        for label, w in entry["windows"].items():
            since = datetime.fromisoformat(w["period"]["start"])
            for target, t in w["targets"].items():
                m = t["metrics"]
                if not m["completed"]:
                    continue
                browser += "browser" in t
                assert type(m["bytes_up"]) is int and type(m["bytes_down"]) is int, target
                if network:
                    sums = conn.execute(BYTES, (entry["provider"], target, since, end)).fetchone()
                    assert (m["bytes_up"], m["bytes_down"]) == sums, (entry["id"], label, target)
    return browser


@pytest.mark.asyncio
async def test_nothing_else_changes(database, tmp_path, monkeypatch):
    """Per mode, a build without the kind entries, then one with them: equal, field by field,
    but for the added entries."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    policy = two_kinds(tmp_path)
    original = calculator.fleet_totals

    def without_kinds(*args):
        return [f for f in original(*args) if not f["cohort"].startswith(KIND)]

    for mode in ("incremental", "full"):
        with monkeypatch.context() as patched:
            patched.setattr(calculator, "fleet_totals", without_kinds)
            before = read_back(database, calculate(database, policy, as_of=end, mode=mode))
        after = read_back(database, calculate(database, policy, as_of=end, mode=mode))
        added = [f for f in after["fleet"] if f["cohort"].startswith(KIND)]
        assert {f["cohort"] for f in added} == {"*:datacenter", "*:residential"}
        kept = after | {"fleet": [f for f in after["fleet"] if f not in added]}
        assert json.dumps(kept) == json.dumps(before), (mode, first_difference(kept, before))
