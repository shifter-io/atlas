"""CALC-INC-1a: the incremental calculation (calculated/incremental.py) equals the full one, and a
window whose runs did not change reads no observation.

The 3-day fixture holds measured runs moved back in time in the database, each with seeded random
observations of documentation IPs labelled in two DB-IP releases (test_sqlwindow's labels:
unenriched IPs, IPs in two countries of one network, empty and NULL labels) and released
estimated hours: daily tests of two providers in one cohort, two runs in one 24-hour slice, a run
split by an hour (so by a window and a slice edge), a run older than the window (its group has
empty windows), census and pulse batches (census-pulse groups), a browser batch, and a late run
added halfway through. At every hourly cutoff of the 3 days the incremental build, made first so
it counts its own misses, and the full build (the oracle) read back byte-identical, and the full
build's verify-on-hit recounts every list the incremental build wrote.
"""

import json
from datetime import UTC, datetime

import psycopg
import pytest
from test_calculated import lenient_policy, payload_mismatches
from test_calculated_hourly_reads import (
    BUCKET,
    DAY,
    HOUR,
    History,
    daily_history,
    floor_hour,
    observation_rows,
    statements,
)
from test_calculated_lists import owned  # noqa: F401 (fixture)

from proxy_index.calculated import incremental, store
from proxy_index.calculated.calculator import calculate
from proxy_index.storage import connect, migrate

pytest_plugins = ["test_storage"]

VOLATILE = {"snapshot_id", "generated_at"}
MEMOS = (
    "calculated_run_ranges",
    "calculated_slot_figures",
    "calculated_distinct",
    "calculated_bucket_ips",
    "calculated_ip_last_seen",
)


async def history(database, tmp_path, end):
    """The 3-day fixture, without the late run the sweep adds."""
    h = History(database, tmp_path, end)
    await h.daily("alpha", "us", 70 * HOUR)
    await h.daily("alpha", "us", 46 * HOUR)
    await h.daily("alpha", "us", 22 * HOUR)
    await h.daily("alpha", "us", 21 * HOUR + 30 * BUCKET)  # a second run in that 24-hour slice
    await h.daily("beta", "us", 55 * HOUR)  # alpha's peer in the US cohort
    await h.daily("beta", "us", 23 * HOUR)
    await h.daily("alpha", "de", 50 * HOUR + BUCKET, split=True)  # split by an hour edge
    await h.daily("alpha", "de", 3 * HOUR)
    await h.daily("gamma", "us", 40 * DAY)  # only older than the window: empty windows
    await h.scheduled("census", "alpha", 30 * HOUR)
    await h.scheduled("pulse", "alpha", 5 * HOUR)
    await h.browser("alpha", 2)
    return h


def without(payload):
    return {k: v for k, v in payload.items() if k not in VOLATILE}


def first_difference(a, b, path="$"):
    """The first path where two JSON values differ, for a short failure report."""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if a.get(key) != b.get(key):
                return first_difference(a.get(key), b.get(key), f"{path}.{key}")
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            if x != y:
                return first_difference(x, y, f"{path}[{i}]")
    return path, a, b


def assert_equal(database, policy, as_of):
    """The incremental build at as_of, then the full one: equal as returned and as read back."""
    new = calculate(database, policy, as_of=as_of, mode="incremental")
    full = calculate(database, policy, as_of=as_of, mode="full")
    assert new["snapshot_id"] != full["snapshot_id"]
    a, b = (without(store.read(database, p["snapshot_id"])) for p in (new, full))
    assert json.dumps(a) == json.dumps(b), (as_of, first_difference(a, b))
    assert without(json.loads(json.dumps(new, default=str))) == without(
        json.loads(json.dumps(full, default=str))
    ), as_of
    assert payload_mismatches(database, int(new["snapshot_id"])) == []
    return a


def lists_count(database):
    with connect(database) as conn:
        return conn.execute("SELECT count(*) FROM calculated_lists").fetchone()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("day", [0, 1, 2])
async def test_incremental_equals_full_at_every_hourly_cutoff(day, database, tmp_path, monkeypatch):
    """Each day of the 3 is its own case (they run in parallel), from no stored list."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    h = await history(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    counted, original = {"hit": 0, "miss": 0}, incremental.Incremental.window

    def window(self, *args):
        found = original(self, *args)
        counted["miss" if "breakdowns" in found[3] else "hit"] += 1
        return found

    monkeypatch.setattr(incremental.Incremental, "window", window)
    seen = set()
    for hours in range(72 - 24 * day, 48 - 24 * day, -1):
        if hours == min(36, 72 - 24 * day):  # a late run: earlier than runs counted before
            await h.daily("beta", "us", 60 * HOUR)
        payload = assert_equal(database, policy, end - hours * HOUR)
        for p in payload["products"]:
            for w in p["windows"].values():
                seen.add(("data", bool(w["metrics"]["completed"])))
                seen.add(("estimated", "estimated" in w))
                seen.add(("overlap", w["overlap"]["compared_products"]))
        seen |= {("scheduled", p["criteria"].get("schedule")) for p in payload["products"]}
    # Every case is exercised: empty and measured windows, estimates, peers, census-pulse
    # groups, and windows the incremental builds reused and ones they counted.
    assert {("data", True), ("estimated", True), ("overlap", 1)} <= seen
    assert ("data", False) in seen  # gamma's windows
    assert ("scheduled", "census_pulse") in seen and ("scheduled", None) in seen
    assert counted["hit"] > counted["miss"] > 0, counted


@pytest.mark.asyncio
async def test_a_hit_window_reads_no_observation(database, tmp_path, monkeypatch):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await daily_history(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    calculate(database, policy, as_of=end - HOUR, mode="incremental")
    lists = lists_count(database)
    log = statements(monkeypatch)
    payload = calculate(database, policy, as_of=end, mode="incremental")
    monkeypatch.undo()
    # Every window reused its list (none written) and no observation row came back.
    assert lists_count(database) == lists and observation_rows(log) == 0
    assert all(incremental.RUN_OBSERVATIONS not in query for query, _ in log)
    assert payload["products"][0]["windows"]["30d"]["breakdowns"]["country"]
    assert payload_mismatches(database, int(payload["snapshot_id"])) == []
    # The windows equal the full calculation's.
    full = calculate(database, policy, as_of=end, mode="full")
    a, b = (without(store.read(database, p["snapshot_id"])) for p in (payload, full))
    assert json.dumps(a) == json.dumps(b), first_difference(a, b)


def test_unknown_mode_is_refused(database):
    store.initialize(database)
    with pytest.raises(ValueError, match="calculation mode"):
        calculate(database, "configs/methodology.toml", mode="partial")


@pytest.mark.asyncio
async def test_v9_layer_migrates_to_the_memo_tables_keeping_its_lists(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await daily_history(database, tmp_path, end)
    calculate(database, lenient_policy(tmp_path), as_of=end, mode="incremental")
    with connect(database) as conn:
        before = conn.execute("SELECT * FROM calculated_lists ORDER BY list_id").fetchall()
        # Back to calculated v9: the same schema without the memo tables.
        for table in MEMOS:
            conn.execute(f"DROP TABLE {table}")
        conn.execute("UPDATE calculation_schema SET version=9,checksum=%s", (store.V9_CHECKSUM,))
    result = migrate(database)
    assert (result["from_calculated_schema_version"], result["calculated_schema_version"]) == (
        9,
        10,
    )
    assert set(store.V9_CHECKS) <= set(result["verified"])
    with connect(database) as conn:
        assert conn.execute("SELECT * FROM calculated_lists ORDER BY list_id").fetchall() == before
        for table in MEMOS:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        # The check counts a list the migration would have changed.
        assert before
        conn.execute(store.V9_SNAPSHOT)
        conn.execute(
            "UPDATE calculated_lists SET content_hash='x' WHERE list_id=%s", (before[0][0],)
        )
        assert conn.execute(store.V9_CHECKS["lists kept"]).fetchone() == (1,)
        conn.rollback()


def test_memo_tables_are_written_by_runtime_and_read_by_api_roles(owned):  # noqa: F811
    roles, files = owned.roles, owned.files
    # The admin role migrates (v8 → v9 → v10); the memo tables get the default privileges only.
    assert migrate(files[roles.admin])["calculated_schema_version"] == 10
    now = datetime.now(UTC)
    rows = {
        "calculated_run_ranges": (1, 0, now, now, "{}", now),
        "calculated_slot_figures": ("d", "{}", now),
        "calculated_distinct": ("d", "{}", now),
        "calculated_bucket_ips": (now, "d", 1),
        "calculated_ip_last_seen": (1, now),
    }
    with connect(files[roles.runtime]) as conn:
        for table, row in rows.items():
            marks = ",".join(["%s"] * len(row))
            conn.execute(f"INSERT INTO {table} VALUES ({marks})", row)
    with connect(files[roles.api]) as conn:
        for table in MEMOS:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (1,)
    for table in MEMOS:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), connect(files[roles.api]) as c:
            c.execute(f"DELETE FROM {table}")
    with connect(files[roles.runtime]) as conn:
        for table in MEMOS:
            conn.execute(f"DELETE FROM {table}")
