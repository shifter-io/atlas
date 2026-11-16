"""CALC-INC-1a and 1b: the incremental calculation (calculated/incremental.py) equals the full one,
a window whose runs did not change returns no observation row to Python, and neither does a
missed window: it is counted in the database and its list written as it is counted.

The 3-day fixture holds measured runs moved back in time in the database, each with seeded random
observations of documentation IPs labelled in two DB-IP releases (test_sqlwindow's labels:
unenriched IPs, IPs in two countries of one network, empty and NULL labels) and released
estimated hours: daily tests of two providers in one cohort, two runs in one 24-hour slice, a run
split by an hour (so by a window and a slice edge), a run older than the window (its group has
empty windows), census and pulse batches (census-pulse groups), a browser batch, and a late run
added halfway through. At every hourly cutoff of the 3 days the incremental build, made first so
it counts its own misses, and the full build (the oracle) read back byte-identical, and the full
build's verify-on-hit recounts every list the incremental build wrote. No incremental build reads
an observation row, and each list it wrote stores its window's first-seen histogram.
"""

import json
from datetime import UTC, datetime

import psycopg
import pytest
from calculated_cases import lenient_policy, payload_mismatches
from sweep_prefix import Chain, prefix
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

from atlas.calculated import incremental, releases, store
from atlas.calculated.calculator import calculate
from atlas.storage import connect, migrate

pytest_plugins = ["test_storage"]

VOLATILE = {"snapshot_id", "generated_at"}
MEMOS = (
    "calculated_run_ranges",
    "calculated_slot_figures",
    "calculated_distinct",
    "calculated_bucket_ips",
    "calculated_ip_last_seen",
    "calculated_scope_ip_last_seen",
    "calculated_ip_vectors",
    "calculated_ip_vector_state",
    "calculated_unique_ips",
    "calculated_latency_days",
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
    # delta's census and pulse runs are all older than the window and its margin, so only its
    # first batch run gives its census-pulse group (in payload order); it also tests daily.
    await h.scheduled("pulse", "delta", 40 * DAY)
    await h.scheduled("census", "delta", 35 * DAY)
    await h.daily("delta", "us", 10 * HOUR)
    return h


def without(payload):
    """payload without its per-build values: ids, generated_at and its networks' list ids
    (every build writes its own network lists)."""
    found = {k: v for k, v in payload.items() if k not in VOLATILE}
    if "networks" in found:
        found["networks"] = [
            n
            | {
                "windows": {
                    k: w | {"lists": w["lists"] and w["lists"]["entries"]}
                    for k, w in n["windows"].items()
                }
            }
            for n in found["networks"]
        ]
    return found


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


def assert_equal(database, policy, as_of, log=None):
    """The incremental build at as_of, then the full one: equal as returned and as read back.
    With log (statements()), the incremental build returns no observation row to Python."""
    start = len(log or ())
    new = calculate(database, policy, as_of=as_of, mode="incremental")
    if log is not None:
        assert observation_rows(log[start:]) == 0, as_of
        assert all(releases.RUN_OBSERVATIONS not in query for query, _ in log[start:])
        assert_histograms(database, int(new["snapshot_id"]))
    full = calculate(database, policy, as_of=as_of, mode="full")
    assert new["snapshot_id"] != full["snapshot_id"]
    a, b = (without(store.read(database, p["snapshot_id"])) for p in (new, full))
    assert json.dumps(a) == json.dumps(b), (as_of, first_difference(a, b))
    assert without(json.loads(json.dumps(new, default=str))) == without(
        json.loads(json.dumps(full, default=str))
    ), as_of
    assert payload_mismatches(database, int(new["snapshot_id"])) == []
    return a


def assert_histograms(database, build_id):
    """Every group list the build wrote stores its window's IPs per first-seen bin from the
    window's start on: they sum to the window's new IPs."""
    query = (
        "SELECT w.new_ips,l.figures->'first_seen' FROM calculated_windows w "
        "JOIN calculated_lists l USING(list_id) WHERE w.build_id=%s AND w.target='' "
        "AND l.created_build=%s"
    )
    with connect(database) as conn:
        for new_ips, bins in conn.execute(query, (build_id, build_id)):
            assert bins is not None and sum(bins.values()) == new_ips, build_id
            assert all(datetime.fromisoformat(at).tzinfo == UTC for at in bins)


def lists_count(database):
    with connect(database) as conn:
        query = "SELECT count(*) FROM calculated_lists WHERE cohort<>'network'"  # group lists
        return conn.execute(query).fetchone()[0]


def cutoffs(day):
    """The day's 24 hourly cutoffs, in hours before the fixture's end, oldest first."""
    return range(72 - 24 * day, 48 - 24 * day, -1)


def count_windows(monkeypatch):
    """The windows the incremental builds reuse (hit) and count (miss), counted from now on."""
    counted = {"hit": 0, "miss": 0}
    window, missed = incremental.Incremental.window, incremental.Incremental._missed

    def counted_window(self, *args):
        counted["hit"] += 1
        return window(self, *args)

    def counted_miss(self, *args):
        counted["hit"], counted["miss"] = counted["hit"] - 1, counted["miss"] + 1
        return missed(self, *args)

    monkeypatch.setattr(incremental.Incremental, "window", counted_window)
    monkeypatch.setattr(incremental.Incremental, "_missed", counted_miss)
    return counted


# Each day's cutoffs in chunks, each its own case (they run in parallel), which copies the day's
# prefix at its start (sweep_prefix) instead of replaying the cutoffs before it. The first chunk
# starts from no stored list, so its first build misses every window: it has the day's first runs
# and the fewest cutoffs whose builds hit more windows than they miss, FIRST[day] (day 2's late
# run misses more). The second has 2, as it builds the first prefix; the others CHUNK (the last
# of day 2 one).
# tests/durations.json lists the cases of later starts as shorter than they take, so they run
# after other slow tests, once the earlier cases have built their prefixes.
FIRST = (5, 5, 6)
CHUNK = 3


def chain(day):
    """The day's sweep: from no stored list, its late run earlier than runs counted before (not
    on day 0)."""
    hours = cutoffs(day)
    late = min(36, hours[0]), lambda h: h.daily("beta", "us", 60 * HOUR)
    starts = (0, FIRST[day], *range(FIRST[day] + 2, len(hours), CHUNK))
    return Chain("incremental", day, history, hours, starts, late)


def chunks():
    """Each case's day and the start of its cutoffs in cutoffs(day)."""
    for day in range(3):
        for start in chain(day).starts:
            yield pytest.param(day, start, id=f"{day}-{start:02d}")


# The cutoffs (hours before the end) with a window that has exactly one peer (overlap 1: two of
# the US cohort's alpha, beta and delta with IPs): from beta's first run in a window to the
# last before delta's run is in every window.
SINGLE_PEER = range(10, 55)


# No coverage (tests/conftest.py): other tests cover every line and branch it does (CI-90-5).
@pytest.mark.no_cover
@pytest.mark.asyncio
@pytest.mark.parametrize(("day", "start"), list(chunks()))
async def test_incremental_equals_full_at_every_hourly_cutoff(
    day, start, database_template, databases, tmp_path, monkeypatch
):
    """One case of a day's sweep, which starts from no stored list: a copy of the day's prefix
    at the case's start (its cutoffs before the case's, replayed), then each of the case's is
    checked."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    policy = lenient_policy(tmp_path)
    days = chain(day)
    database, h = await prefix(days, start, end, database_template, tmp_path, policy, databases)
    late, add_late = days.late
    own = days.checked(start)
    counted = count_windows(monkeypatch)
    log, seen = statements(monkeypatch), set()
    for hours in own:
        if hours == late:
            await add_late(h)
        payload = assert_equal(database, policy, end - hours * HOUR, log)
        for p in payload["products"]:
            for w in p["windows"].values():
                seen.add(("data", bool(w["metrics"]["completed"])))
                seen.add(("estimated", "estimated" in w))
                seen.add(("overlap", w["overlap"]["compared_products"]))
        seen |= {("scheduled", p["criteria"].get("schedule")) for p in payload["products"]}
        seen |= {(p["provider"], p["criteria"].get("schedule")) for p in payload["products"]}
    # Every case is exercised: empty and measured windows, estimates, peers, census-pulse
    # groups, and windows the incremental builds reused and ones they counted.
    assert {("data", True), ("estimated", True)} <= seen
    assert (("overlap", 1) in seen) == any(at in SINGLE_PEER for at in own)
    assert ("data", False) in seen  # gamma's windows
    assert ("scheduled", "census_pulse") in seen and ("scheduled", None) in seen
    assert ("delta", "census_pulse") in seen
    assert counted["hit"] > counted["miss"] > 0, counted


@pytest.mark.asyncio
async def test_a_hit_window_returns_no_observation_row(database, tmp_path, monkeypatch):
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
    assert all(releases.RUN_OBSERVATIONS not in query for query, _ in log)
    assert payload["products"][0]["windows"]["30d"]["breakdowns"]["country"]
    assert payload_mismatches(database, int(payload["snapshot_id"])) == []
    # The windows equal the full calculation's.
    full = calculate(database, policy, as_of=end, mode="full")
    a, b = (without(store.read(database, p["snapshot_id"])) for p in (payload, full))
    assert json.dumps(a) == json.dumps(b), first_difference(a, b)


def test_the_run_filter_is_the_outer_querys():
    """releases.RUN_OBSERVATIONS filters the observations themselves, not the release CTE."""
    query = releases.RUN_OBSERVATIONS
    assert releases.OBSERVATIONS.count("WHERE o.bucket_start>=") == 1
    assert query.count("o.run_key=ANY(%s)") == 1
    assert query.index("o.run_key=ANY(%s)") > query.index("FROM ip_observations o")


def test_unknown_mode_is_refused(database):
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
        conn.execute(
            "DROP TABLE calculated_geo_hours,calculated_geo_rollups,calculated_pulse_buckets,"
            "calculated_pulse_rollups"
        )  # calculated v11 and v12
        conn.execute("UPDATE calculation_schema SET version=9,checksum=%s", (store.V9_CHECKSUM,))
    result = migrate(database)
    assert (result["from_calculated_schema_version"], result["calculated_schema_version"]) == (
        9,
        18,
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
    # The admin role migrates (v8 → v12); the memo tables and rollups get the default privileges:
    # the run-time role writes them, the API role (which reads none of them) cannot read them.
    assert migrate(files[roles.admin])["calculated_schema_version"] == 18
    now = datetime.now(UTC)
    rows = {
        "calculated_run_ranges": (1, 0, now, now, "{}", now),
        "calculated_slot_figures": ("d", "{}", now),
        "calculated_distinct": ("d", "{}", now),
        "calculated_bucket_ips": (now, "d", 1),
        "calculated_ip_last_seen": (1, now),
        "calculated_scope_ip_last_seen": (1, 2, now),
        "calculated_ip_vectors": (24, [2], 1),
        "calculated_ip_vector_state": (now, [24], [1]),
        "calculated_unique_ips": ("p", "k", "hour", now, 1, None, None, None, None, None, True)
        + (None, None, now),
        "calculated_latency_days": ("p", "k", "t", now, 30, 1, [1], [1.0], "{}", "d", now),
        "calculated_geo_hours": (now, None, 1, 1, 1, None, "t", None, 1, 1, 0, 0, [1], [1]),
        "calculated_geo_rollups": (now, 1),
        "calculated_pulse_buckets": (now, now, 1, 1, 1, 0, "{}", [1], [1], True)
        + (0, 0, 0, 0, 0, 0, None),
        "calculated_pulse_rollups": (now, 1, [1]),
    }
    with connect(files[roles.runtime]) as conn:
        for table, row in rows.items():
            marks = ",".join(["%s"] * len(row))
            conn.execute(f"INSERT INTO {table} VALUES ({marks})", row)
    with connect(files[roles.runtime]) as conn:
        for table in rows:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (1,)
    for table in rows:
        for statement in (f"SELECT count(*) FROM {table}", f"DELETE FROM {table}"):
            with connect(files[roles.api]) as c:
                c.execute("SET TRANSACTION READ WRITE")  # the role's default is read-only
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    c.execute(statement)
    with connect(files[roles.runtime]) as conn:
        for table in rows:
            conn.execute(f"DELETE FROM {table}")
