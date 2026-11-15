"""CALC-FLEET-1: the counts across groups (calculated/fleetsql.py) from memos equal the full
calculation's, and the fleet's timed() runs on merged rows (calculated/fleet.py).

The fixture is 3 networks in 5 countries (country cohorts of three, two and one group) with
seeded random observations drawn from one small address pool, so groups share IPs: at every
hourly cutoff of a day, and after a late run, the incremental build and the full build read back
byte-identical. An older cutoff counts its window directly and changes no memo; the memos are
collected by age.
"""

import hashlib
import json
import random
from collections import Counter
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from calculated_cases import lenient_policy
from sweep_prefix import Chain, prefix
from test_calculated_hourly_reads import BUCKET, DAY, HOUR, History, floor_hour
from test_calculated_incremental import assert_equal

from atlas.calculated import fleetsql
from atlas.calculated.aggregate import OUTCOMES, SeriesSpec, series, timed
from atlas.calculated.calculator import calculate
from atlas.calculated.fleet import merged
from atlas.storage import connect

pytest_plugins = ["test_storage"]

MEMOS = ("calculated_bucket_ips", "calculated_ip_last_seen", "calculated_distinct")
MINUTE = timedelta(minutes=1)
COUNTRIES = {"us": 3, "de": 3, "fr": 3, "jp": 2, "gb": 1}  # groups (networks) per country
# The sweep's cutoffs (hours before the fixture's end, oldest first), its late run's cutoff, and
# its chunks' starts: each chunk is its own case (they run in parallel), which copies the sweep's
# prefix at its start (sweep_prefix) instead of replaying the cutoffs before it. The first has
# 1 cutoff (from no memo), the others 2 (the last 1). Each counts some cohort set: the builds at
# 20 h, 18 h and 6 h count none, so none of them is a chunk alone.
CUTOFFS = range(24, 0, -2)
LATE = 12
STARTS = (0, *range(1, len(CUTOFFS), 2))


async def fleet_history(database, tmp_path, end):
    h = History(database, tmp_path, end, seed_value=31)
    rng = random.Random(5)
    for country, networks in COUNTRIES.items():
        for provider in ("alpha", "beta", "gamma")[:networks]:
            for _ in range(2):
                # Half past an hour: no bucket at a whole-hour cutoff or window edge.
                await h.daily(provider, country, rng.randrange(3, 70) * HOUR - 30 * MINUTE)
    return h


def bucket_rows(seed, n=400):
    """Seeded bucket rows of several groups, as build() hands them to the fleet."""
    rng, start = random.Random(seed), datetime(2026, 11, 1, tzinfo=UTC)
    rows = []
    for _ in range(n):
        bucket = start + rng.randrange(288) * BUCKET
        outcome = rng.random() < 0.6
        completed = rng.randint(0, 40)
        counters = {
            "completed": completed,
            "successes": rng.randint(0, completed),
            "response_body_bytes": rng.randint(0, 10**6),
            "errors": {k: rng.randint(1, 3) for k in rng.sample(["timeout", "proxy", "tls"], 2)},
            "latency_histogram": {
                "bins": sorted((ms, rng.randint(1, 4)) for ms in rng.sample(range(50, 900), 5))
            },
            "status_codes": {"200": completed},
        }
        if outcome:
            counters |= {k: rng.randint(0, 9) for k in OUTCOMES}
        rows.append(
            {
                "bucket_start": bucket,
                "run_key": rng.randint(1, 50),
                "counters": counters,
                "outcome": outcome,
                "target": rng.choice(["generic_http", "generic_tls"]),
                "light": rng.random() < 0.7,
                "during_census": rng.random() < 0.2,
                "slot": start + (bucket - start) // (6 * BUCKET) * (6 * BUCKET),
                "country": rng.choice(["US", "DE", None]),
                "region": rng.choice(["fra", "nyc"]),
                "last_observed": bucket + rng.randint(1, 5) * timedelta(minutes=1),
            }
        )
    return rows


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_timed_and_series_of_merged_rows_equal_the_rows(seed):
    rows = bucket_rows(seed)
    together = merged(rows)
    assert len(together) < len(rows)
    for minimum in (1, 5):
        assert timed(together, 24, minimum, slot_minutes=30) == timed(
            rows, 24, minimum, slot_minutes=30
        )
    start = datetime(2026, 11, 1, tzinfo=UTC)
    for hours in (24, 168):
        end = start + timedelta(hours=hours)
        spec = SeriesSpec(start, end, hours, latency_target="generic_http", minimum=2)
        assert series(together, (), spec) == series(rows, (), spec)
    # Errors and histograms keep their counts (as Counters), whatever the rows' order.
    assert sum((Counter(r["counters"]["errors"]) for r in together), Counter()) == sum(
        (Counter(r["counters"]["errors"]) for r in rows), Counter()
    )


def counting(monkeypatch):
    """The cohort sets fleetsql counts with COUNTS on each build."""
    calls, original = [], fleetsql._counts

    def counts(conn, as_of, sets, rows):
        calls.append(len(sets))
        return original(conn, as_of, sets, rows)

    monkeypatch.setattr(fleetsql, "_counts", counts)
    return calls


def memo_rows(database):
    with connect(database) as conn:
        return {t: sorted(conn.execute(f"SELECT * FROM {t}").fetchall()) for t in MEMOS}


async def late_run(h):
    await h.daily("beta", "jp", 40 * HOUR - 30 * MINUTE)  # late: before folded buckets


SWEEP = Chain("fleet", 0, fleet_history, CUTOFFS, STARTS, (LATE, late_run))


# No coverage (tests/conftest.py): other tests cover every line and branch it does (CI-90-5).
@pytest.mark.no_cover
@pytest.mark.asyncio
@pytest.mark.parametrize("start", SWEEP.starts)
async def test_fleet_counts_from_memos_equal_full_at_every_hourly_cutoff(
    start, database_template, databases, tmp_path, monkeypatch
):
    """One chunk of the sweep, which starts from no memo: a copy of the sweep's prefix at the
    chunk's start (its cutoffs before the chunk's, replayed), then each of the chunk's is
    checked; the last chunk checks the memos the sweep ends with."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    policy = lenient_policy(tmp_path)
    database, h = await prefix(SWEEP, start, end, database_template, tmp_path, policy, databases)
    own = SWEEP.checked(start)
    calls = counting(monkeypatch)
    seen = set()
    for hours in own:
        if hours == LATE:
            await late_run(h)
        payload = assert_equal(database, policy, end - hours * HOUR)
        for f in payload["fleet"]:
            seen.add((f["cohort"] == "*", f["products"], bool(f["unique_ips"])))
    # The fleet totals and cohorts of three, two and one group were all counted.
    assert {(True, 12, True), (False, 3, True), (False, 2, True), (False, 1, True)} <= seen
    assert sum(calls) > 0
    if own[-1] == CUTOFFS[-1]:
        assert_last_memos(database, policy, end, calls)


def assert_last_memos(database, policy, end, calls):
    """The memos after the sweep's last cutoff (end - 2h), and a build five minutes on."""
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM calculated_ip_last_seen").fetchone()[0] > 0
        assert conn.execute(fleetsql.NEWEST).fetchone()[0] < end - 2 * HOUR
        query = "SELECT count(*) FROM calculated_distinct WHERE starts_with(digest,%s)"
        assert conn.execute(query, (fleetsql.MEMO,)).fetchone()[0] > 0
    # Five minutes on, no cohort's runs changed in its window: nothing is counted again.
    calls.clear()
    assert_equal(database, policy, end - 2 * HOUR + BUCKET)
    assert calls == [0]


# The older cutoffs, in two cases. The second stays traced: only it covers fleet_entries()'s
# direct count of an older cutoff (tests/conftest.py, "No coverage"); 3 h back, no bucket after
# the cutoff is folded yet, so that build counts as the latest.
OLDER = [
    pytest.param((3 * HOUR,), id="3h", marks=pytest.mark.no_cover),
    pytest.param((30 * HOUR, 3 * DAY), id="30h-3d"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("backs", OLDER)
async def test_an_older_cutoff_counts_directly_and_keeps_the_memos(
    backs, database_template, databases, tmp_path
):
    end = floor_hour(datetime.now(UTC)) + HOUR
    policy = lenient_policy(tmp_path)
    database, _ = await prefix(SWEEP, 0, end, database_template, tmp_path, policy, databases)
    assert_equal(database, policy, end)
    before = memo_rows(database)
    assert before["calculated_ip_last_seen"]
    for back in backs:  # older than folded buckets: no last-seen count
        assert_equal(database, policy, end - back)
    after = memo_rows(database)
    for table in ("calculated_bucket_ips", "calculated_ip_last_seen"):
        assert after[table] == before[table], table


@pytest.mark.asyncio
async def test_a_series_day_is_counted_once_and_again_when_a_run_joins_it(
    database, tmp_path, monkeypatch
):
    """CALC-SCALE-1 (#1125): the "*" 7d/30d series' daily slices are remembered
    (fleetsql.remembered_spans()). The build a day on counts only its newest slice, since its
    other slices are the earlier build's; a run added to a slice counts that slice again (and
    only it). Each build equals the full calculation."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    h = await fleet_history(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    counted, original = [], fleetsql.count_spans

    def count_spans(conn, spans):
        counted.append(sorted(set(spans)))
        return original(conn, spans)

    monkeypatch.setattr(fleetsql, "count_spans", count_spans)
    assert_equal(database, policy, end - DAY)
    assert (end - 2 * DAY, end - DAY) in counted[-1]  # the fixture's runs reach 70 h back
    assert_equal(database, policy, end)
    assert counted[-1] == [(end - DAY, end)]
    await late_run(h)  # 40 h back: in the slice before
    assert_equal(database, policy, end)
    assert counted[-1] == [(end - 2 * DAY, end - DAY)]


def test_no_span_and_no_run_are_answered_without_a_query():
    """No span to count, and a cohort whose members have no run in the window: answered without
    a connection. Traced: only the untraced sweeps reach these otherwise."""
    assert fleetsql.count_spans(None, []) == {}
    as_of = datetime(2026, 11, 1, tzinfo=UTC)
    members = [({"provider": "alpha"}, []), ({"provider": "beta"}, [])]
    key = json.dumps([["alpha", []], ["beta", []]])
    digest = fleetsql.MEMO + hashlib.sha256(key.encode()).hexdigest()
    assert fleetsql._digests(None, as_of, {0: (as_of - DAY, members)}) == ({0: digest}, {0: 0})


@pytest.mark.asyncio
async def test_old_memo_rows_are_collected(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await fleet_history(database, tmp_path, end)
    old = end - 40 * DAY
    with connect(database) as conn:
        conn.execute("INSERT INTO calculated_ip_last_seen VALUES (-1,%s)", (old,))
        conn.execute("INSERT INTO calculated_bucket_ips VALUES (%s,'x',3)", (old,))
        for digest in (fleetsql.MEMO + "stale", "other-stale", fleetsql.MEMO + "fresh"):
            conn.execute(
                "INSERT INTO calculated_distinct VALUES (%s,'[]',now()-%s)",
                (digest, timedelta(days=3 if "stale" in digest else 1)),
            )
    payload = calculate(database, lenient_policy(tmp_path), as_of=end, mode="incremental")
    assert payload["fleet"]
    with connect(database) as conn:
        assert conn.execute("SELECT * FROM calculated_ip_last_seen WHERE ip_id=-1").fetchall() == []
        assert conn.execute(
            "SELECT count(*) FROM calculated_bucket_ips WHERE bucket_start<%s", (end - 31 * DAY,)
        ).fetchone() == (0,)
        kept = {r[0] for r in conn.execute("SELECT digest FROM calculated_distinct")}
        assert "other-stale" in kept and fleetsql.MEMO + "fresh" in kept  # not ours, or used
        assert fleetsql.MEMO + "stale" not in kept


@pytest.mark.asyncio
async def test_slices_are_counted_in_parallel_in_the_calculations_snapshot(
    database, tmp_path, monkeypatch
):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await fleet_history(database, tmp_path, end)
    spans = [(end - (d + 1) * DAY, end - d * DAY) for d in range(9)]
    opened, original = [], psycopg.connect

    def connect_counted(*args, **kwargs):
        opened.append(1)
        return original(*args, **kwargs)

    with connect(database) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        expected = {s: conn.execute(fleetsql.SPAN, s).fetchone()[0] for s in spans}
        # A new IP committed after the snapshot is not counted by the slice connections.
        with connect(database) as other:
            bucket, key = other.execute(
                "SELECT bucket_start,run_key FROM ip_observations WHERE bucket_start>=%s LIMIT 1",
                (spans[0][0],),
            ).fetchone()
            ip = other.execute(
                "INSERT INTO ip_addresses(ip) VALUES ('203.0.113.9') RETURNING ip_id"
            ).fetchone()[0]
            other.execute(
                "INSERT INTO ip_observations VALUES (%s,%s,%s,1,%s,%s)",
                (bucket, key, ip, bucket, bucket),
            )
        with connect(database) as other:  # committed: a new snapshot counts it
            assert other.execute(fleetsql.SPAN, spans[0]).fetchone()[0] == expected[spans[0]] + 1
        monkeypatch.setattr(psycopg, "connect", connect_counted)
        assert fleetsql.count_spans(conn, spans) == expected
    assert len(opened) == fleetsql.PARALLEL
