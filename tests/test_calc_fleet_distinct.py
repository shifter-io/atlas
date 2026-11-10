"""CALC-FLEET-DISTINCT-1: the fleet's cohort counts (fleetsql.COUNTS), its window totals
(fleetsql.LAST_SEEN) and the network overlaps (network_scores.OVERLAP) against the statements
they replaced, kept here as the reference: the same counts for seeded random cohorts, cutoffs
and networks over test_calc_fleet's history, batched or not, and through the cohort memo.

That a build's fleet entries, overlaps and network scores are unchanged is test_calc_fleet's
sweep (every incremental build equals the full one, which counts IP sets in Python, first build
and later ones) and test_calculated_network_scores' (the overlaps equal the IP sets' rule).
"""

import random
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from calculated_cases import lenient_policy
from psycopg.rows import dict_row
from sweep_prefix import prefix
from test_calc_fleet import SWEEP
from test_calculated_hourly_reads import DAY, HOUR, floor_hour

from proxy_index.calculated import fleetsql, network_scores
from proxy_index.orchestrator.control import write_settings
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]

# The statements before CALC-FLEET-DISTINCT-1.
OLD_COUNTS = """WITH k AS (
 SELECT * FROM unnest(%(slots)s::int[],%(members)s::int[],%(providers)s::text[],
  %(keys)s::bigint[],%(since)s::timestamptz[]) AS k(slot,member,provider,run_key,since)
), ip AS (
 SELECT k.slot,o.ip_id,array_agg(DISTINCT k.member) AS members,
  count(DISTINCT k.provider) AS providers
 FROM k JOIN ip_observations o ON o.run_key=k.run_key AND o.bucket_start>=k.since
  AND o.bucket_start<%(as_of)s GROUP BY 1,2
)
SELECT slot,a,b,count(*) AS ips,count(*) FILTER (WHERE providers>1) AS shared
FROM ip CROSS JOIN LATERAL unnest(members) a CROSS JOIN LATERAL unnest(members) b WHERE a<=b
GROUP BY 1,2,3
UNION ALL SELECT slot,NULL,NULL,count(*),0 FROM ip GROUP BY 1"""
OLD_LAST_SEEN = """SELECT s AS since,(SELECT count(*) FROM calculated_ip_last_seen
WHERE last_seen>=s) AS ips FROM unnest(%s::timestamptz[]) s"""
OLD_OVERLAP = """WITH k AS (SELECT * FROM unnest(%s::int[],%s::int[],%s::bigint[],%s::timestamptz[],
 %s::timestamptz[]) AS k(slot,kind,run_key,since,until)),
s AS (SELECT DISTINCT k.slot,k.kind,o.ip_id FROM k JOIN ip_observations o
 ON o.run_key=k.run_key AND o.bucket_start>=k.since AND o.bucket_start<k.until),
c AS (SELECT kind,ip_id,count(*) AS networks FROM s GROUP BY 1,2),
m AS (SELECT s.* FROM s JOIN c USING(kind,ip_id) WHERE c.networks>1)
SELECT s.slot,NULL::int AS peer,count(*) AS ips,count(*) FILTER (WHERE c.networks>1) AS shared
 FROM s JOIN c USING(kind,ip_id) GROUP BY 1
UNION ALL
SELECT a.slot,b.slot,count(*),NULL FROM m a JOIN m b ON b.kind=a.kind AND b.ip_id=a.ip_id
 AND b.slot<>a.slot GROUP BY 1,2"""
PROVIDERS = ("alpha", "beta", "gamma")
MINUTE = timedelta(minutes=1)


async def history(database_template, databases, tmp_path):
    """A copy of test_calc_fleet's history (3 networks in 5 countries, IPs shared across groups)
    with its end, run keys and the times of its observations."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    policy = lenient_policy(tmp_path)
    database, _ = await prefix(SWEEP, 0, end, database_template, tmp_path, policy, databases)
    with connect(database) as conn:
        keys = [r[0] for r in conn.execute("SELECT DISTINCT run_key FROM ip_observations")]
        times = [r[0] for r in conn.execute("SELECT DISTINCT bucket_start FROM ip_observations")]
    return database, end, sorted(keys), sorted(times)


def cohorts(rng, keys, times, n):
    """n seeded cohort sets [(since, [(group, run keys) per member])]: 1 to 5 members of 1 to 3
    providers, some without runs, some members sharing runs, cutoffs on and off the buckets (in
    the first half of the observations' times)."""
    sets = []
    for _ in range(n):
        since = rng.choice(times[: len(times) // 2]) + rng.choice([timedelta(0), 2 * MINUTE])
        members = [
            ({"provider": rng.choice(PROVIDERS)}, rng.sample(keys, rng.randint(0, 6)))
            for _ in range(rng.randint(1, 5))
        ]
        sets.append((since, members))
    return sets


def old_counts(conn, as_of, sets):
    """The counts of sets by OLD_COUNTS, as _counts() gave them."""
    rows = [
        (slot, member, group["provider"], run_key, since)
        for slot, (since, members) in enumerate(sets)
        for member, (group, keys) in enumerate(members)
        for run_key in sorted(set(keys))
    ]
    names = ("slots", "members", "providers", "keys", "since")
    params = {name: [row[n] for row in rows] for n, name in enumerate(names)}
    found = [{} for _ in sets]
    for row in conn.cursor(row_factory=dict_row).execute(OLD_COUNTS, params | {"as_of": as_of}):
        found[row["slot"]][row["a"], row["b"]] = (row["ips"], row["shared"])
    return found


@pytest.mark.asyncio
async def test_cohort_counts_equal_the_distinct_aggregates(database_template, databases, tmp_path):
    """Seeded cohorts counted in one statement, in batches on snapshot connections, and the old
    way: the same counts per pair and per set."""
    database, end, keys, times = await history(database_template, databases, tmp_path)
    rng = random.Random(790)
    sets = cohorts(rng, keys, times, 40)
    as_of = rng.choice(times[len(times) * 3 // 4 :])
    with connect(database) as conn:
        expected = old_counts(conn, as_of, sets)
        assert sum(1 for counts in expected if counts.get((None, None))) > 20
        assert any(s for counts in expected for (a, b), (_, s) in counts.items() if a != b)
        _, rows = fleetsql._digests(conn, as_of, dict(enumerate(sets)))
        assert fleetsql._counts(conn, as_of, sets, [rows[i] for i in range(len(sets))]) == expected
        assert fleetsql._counts(conn, as_of, [], []) == []
    with connect(database) as conn:
        write_settings(conn, {"calculate.heavy_rows": max(rows.values()) // 2 or 1})
    opened, original = [], psycopg.connect
    with connect(database) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        estimated = [rows[i] for i in range(len(sets))]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(psycopg, "connect", lambda *a, **k: opened.append(1) or original(*a, **k))
            assert fleetsql._counts(conn, as_of, sets, estimated) == expected
    assert len(opened) == fleetsql.PARALLEL  # more batches than helpers


@pytest.mark.asyncio
async def test_cohorts_counted_through_the_memo_equal_the_distinct_aggregates(
    database_template, databases, tmp_path
):
    """_cohorts() (as fleet_entries() calls it) with cohorts of one to five members, none
    remembered yet, then all remembered: the old counts both times."""
    database, end, keys, times = await history(database_template, databases, tmp_path)
    rng = random.Random(31)
    as_of = times[-1] + timedelta(minutes=5)
    cohort_members, sets = {}, []
    for n, (_, members) in enumerate(cohorts(rng, keys, times, 12)):
        hours = rng.choice([24, 168, 720])
        window = {"windows": {"w": {"period": {"hours": hours}, "pool": {"unique_ips": 0}}}}
        members = [(group | window, runs) for group, runs in members if len(members) > 1]
        if members:
            cohort_members[n, "w"] = (None, members)
            sets.append((as_of - timedelta(hours=hours), members))
    with connect(database) as conn:
        expected = old_counts(conn, as_of, sets)
        for _ in range(2):  # counted, then from the memo
            assert fleetsql._cohorts(conn, as_of, cohort_members) == expected
    assert len(sets) > 5 and any(counts for counts in expected)


@pytest.mark.asyncio
async def test_window_totals_equal_one_count_per_cutoff(database_template, databases, tmp_path):
    """Last-seen times on, between and after the cutoffs; cutoffs repeated, out of order, before
    and after every row: the same IPs per cutoff."""
    database, end, _, _ = await history(database_template, databases, tmp_path)
    rng = random.Random(7)
    start = end - 31 * DAY
    with connect(database) as conn:
        cuts = [end - timedelta(hours=h) for h in (24, 168, 720, 24, 1, 0)]
        times = [start + timedelta(seconds=rng.randrange(32 * 86400)) for _ in range(500)]
        times += cuts
        conn.execute(
            "INSERT INTO calculated_ip_last_seen SELECT -n,t FROM unnest(%s::timestamptz[]) "
            "WITH ORDINALITY u(t,n)",
            (times,),
        )
        for labels in ([0, 1, 2], [3, 1], [2, 0, 4, 5], [5]):
            since = {f"w{n}": cuts[n] for n in labels} | {"older": start - DAY}
            rows = conn.execute(OLD_LAST_SEEN, (list(since.values()),)).fetchall()
            expected = {label: ips for label, (_, ips) in zip(since, rows, strict=True)}
            assert fleetsql.last_seen(conn, since) == expected
            assert 0 < min(expected.values()) and expected["older"] == 500 + len(cuts)


def overlap_members(rng, keys, times, n):
    """n seeded network windows ((entry, window) by slot) of two kinds, each with 0 to 6 runs and
    its own range of the observations."""
    members = []
    for _ in range(n):
        start, until = sorted(rng.sample(times, 2))
        runs = rng.sample(keys, rng.randint(0, 6))
        window = {"_runs": runs, "period": {"start": start, "end": until + timedelta(minutes=5)}}
        members.append(({"kind": rng.choice(["residential", "mobile"])}, window))
    return members


def old_overlap(conn, members):
    kinds = {kind: n for n, kind in enumerate(sorted({e["kind"] for e, _ in members}))}
    rows = [
        (slot, kinds[entry["kind"]], run_key, w["period"]["start"], w["period"]["end"])
        for slot, (entry, w) in enumerate(members)
        for run_key in w["_runs"]
    ]
    own, pairs = {}, defaultdict(dict)
    columns = [[row[n] for row in rows] for n in range(5)]
    for slot, peer, ips, shared in conn.execute(OLD_OVERLAP, columns):
        if peer is None:
            own[slot] = (ips, shared)
        else:
            pairs[slot][peer] = ips
    return own, pairs


@pytest.mark.asyncio
async def test_network_overlaps_equal_the_distinct_count(database_template, databases, tmp_path):
    database, _, keys, times = await history(database_template, databases, tmp_path)
    rng = random.Random(11)
    with connect(database) as conn:
        for n in (1, 2, 5, 12, 30):
            members = overlap_members(rng, keys, times, n)
            expected = old_overlap(conn, members)
            assert network_scores._counted(conn, members) == expected
            if n >= 12:
                assert expected[1] and any(s for _, s in expected[0].values())
