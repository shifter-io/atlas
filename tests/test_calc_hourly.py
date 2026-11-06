"""CALC-INC-2: the values that move with the cutoff come from the K6 memos (calculated/hourly.py)
and equal a fresh count every hour.

The fixture holds measured daily runs moved back in time (test_calculated_hourly_reads.History:
seeded observations of documentation IPs in two DB-IP releases): a test split across a slice edge
by the 5-minute cutoffs, two tests in one 5-minute bucket, a run of more than 12 buckets and a
peer product. The memo bodies are compared at every 5-minute cutoff with a fresh count of the same
slices and points (the full-mode bodies they replaced: window_inputs() and pool_figures(), and
the series points counted in Python), and whole builds with the full calculation (the oracle).
"""

from collections import defaultdict
from datetime import UTC, datetime

import pytest
from calculated_cases import lenient_policy
from psycopg.rows import dict_row
from test_calculated_hourly_reads import (
    BUCKET,
    DAY,
    HOUR,
    PRODUCT_IPS,
    History,
    daily_history,
    floor_hour,
    statements,
)
from test_calculated_incremental import assert_equal

from proxy_index.calculated import hourly, sqlwindow
from proxy_index.calculated.calculator import calculate
from proxy_index.calculated.poolfig import pool_figures
from proxy_index.calculated.sqlwindow import WindowSpec, distinct_ips, window_inputs
from proxy_index.estimates import window as estimates
from proxy_index.partitions import partition
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]

# Statements of hourly.py and the sqlwindow counts it calls that read observations.
COUNTING = ("ip_observations",)


async def stretched(h, provider, country, back, buckets):
    """A daily run with `buckets` observation buckets from `back` before end."""
    run_key = await h.daily(provider, country, back)
    with connect(h.database) as conn:
        first = conn.execute(
            "SELECT min(bucket_start) FROM run_buckets WHERE run_key=%s", (run_key,)
        ).fetchone()[0]
        conn.execute(
            "CREATE TEMP TABLE stretch AS SELECT * FROM run_buckets WHERE run_key=%s", (run_key,)
        )
        for n in range(1, buckets):
            partition(conn, "run_buckets", first + n * BUCKET)
            conn.execute("UPDATE stretch SET bucket_start=bucket_start+interval '5 minutes'")
            conn.execute("INSERT INTO run_buckets SELECT * FROM stretch")
        conn.execute("DROP TABLE stretch")
        conn.execute(
            "UPDATE test_runs SET finished_at=%s WHERE run_key=%s",
            (first + buckets * BUCKET, run_key),
        )
        conn.execute("DELETE FROM run_enrichments WHERE run_key=%s", (run_key,))
        h.observe(conn, run_key)  # observations in every bucket, and its releases again
        conn.execute(PRODUCT_IPS)
    return run_key


async def fixture(database, tmp_path, end):
    h = History(database, tmp_path, end, seed_value=11)
    edge = 24 * HOUR + 6 * BUCKET
    runs = [await h.daily("alpha", "us", edge, split=True)]  # crossed by the 24-hour edge
    runs += [await h.daily("alpha", "us", 5 * HOUR), await h.daily("alpha", "us", 5 * HOUR)]
    runs.append(await stretched(h, "alpha", "us", 48 * HOUR + 8 * BUCKET, 14))  # 14 buckets
    runs.append(await h.daily("alpha", "us", 70 * HOUR))
    await h.daily("beta", "us", 30 * HOUR)  # another product: not in the group
    return h, runs


def product_of(conn, run_key):
    return conn.execute(
        "SELECT s.product_id FROM test_runs JOIN scope_stats s USING(scope_id) WHERE run_key=%s",
        (run_key,),
    ).fetchone()[0]


def slices_at(as_of):
    days = [(as_of - (d + 1) * DAY, as_of - d * DAY) for d in range(30)]
    widest = [(as_of - h * HOUR, as_of, "first_seen") for h in (720, 168, 48, 24)]
    return days + widest


def points_at(as_of):
    return {h: (as_of - h * HOUR, as_of, BUCKET if h == 24 else DAY) for h in (24, 168, 720)}


def reference_slices(conn, product, asked, slices):
    """The full-mode body: each slice counted afresh."""
    specs = [WindowSpec(n, product, tuple(asked), s[0], s[1], asked) for n, s in enumerate(slices)]
    found = window_inputs(conn, specs, release_mode="per_run", release_id=None)
    result = []
    for spec, bounds in zip(specs, slices, strict=True):
        inputs = found[spec.key]
        if len(bounds) == 3:
            result.append({"first_seen": inputs.first_seen})
        elif inputs.unique_ips:
            hours = (spec.until - spec.since) // HOUR
            figures = pool_figures(inputs, spec.since, hours)
            result.append(figures | {"first_seen": inputs.first_seen})
        else:
            result.append(None)
    return result


def reference_points(conn, runs, start, until, step):
    seen = defaultdict(set)
    rows = conn.execute(
        "SELECT bucket_start,ip_id FROM ip_observations WHERE run_key=ANY(%s) "
        "AND bucket_start>=%s AND bucket_start<%s",
        (runs, start, until),
    )
    for at, ip in rows:
        seen[start + (at - start) // step * step].add(ip)
    return {at: len(ips) for at, ips in seen.items()}


def assert_slices_equal(found, expected, slices):
    for n, (got, want, (since, *_)) in enumerate(zip(found, expected, slices, strict=True)):
        if want is None:
            assert got is None, n
            continue
        new = {at: ips for at, ips in want["first_seen"].items() if at >= since}
        assert got["first_seen"] == new, (n, since)
        assert hourly.new_ips(got, since) == hourly.new_ips(want, since), n
        rest = {k: v for k, v in want.items() if k != "first_seen"}
        assert {k: v for k, v in got.items() if k != "first_seen"} == rest, n


def check(conn, product, asked, as_of):
    """slice_figures() and bucket_ips() at as_of equal a fresh count."""
    slices = slices_at(as_of)
    found = hourly.slice_figures(conn, {"g": (product, asked)}, {"g": slices})["g"]
    assert_slices_equal(found, reference_slices(conn, product, asked, slices), slices)
    points = points_at(as_of)
    counted = hourly.bucket_ips(conn, dict.fromkeys(points, list(asked)), points)
    for key, (start, until, step) in points.items():
        assert counted[key] == reference_points(conn, list(asked), start, until, step), key
    return found


@pytest.mark.asyncio
async def test_memos_equal_a_fresh_count_at_every_five_minute_cutoff(
    database, tmp_path, monkeypatch
):
    """Cutoffs every 5 minutes move the 24-hour edge across a split test (cutoff at hh:05, test
    over hh:00-hh:10), through a 14-bucket run's 2-day edge and past two tests in one bucket."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    h, runs = await fixture(database, tmp_path, end)
    with connect(database) as conn:
        product = product_of(conn, runs[0])
        asked = dict.fromkeys(runs, "us")
        for n in range(13, -1, -1):
            check(conn, product, asked, end - n * BUCKET)
        conn.commit()
        # The memos hold every range a slice or point used; counting again reads no observation.
        log = statements(monkeypatch)
        check_at = end - 7 * BUCKET
        hourly.slice_figures(conn, {"g": (product, asked)}, {"g": slices_at(check_at)})
        hourly.bucket_ips(conn, dict.fromkeys(points_at(check_at), runs), points_at(check_at))
        monkeypatch.undo()
        assert not [q for q, _ in log if any(t in q for t in COUNTING)], log
        ranges = conn.execute(
            "SELECT run_key,count(*) FROM calculated_run_ranges GROUP BY 1 ORDER BY 1"
        ).fetchall()
    # Single buckets, recorded first; a run's other ranges only when a slice holds it alone.
    # The split run never has a daily slice of its own here (other runs share each one).
    expected = {runs[0]: 2, runs[1]: 1, runs[2]: 1, runs[3]: 14, runs[4]: 1}
    assert {k: n for k, n in ranges if k in expected} == expected


@pytest.mark.asyncio
async def test_incremental_equals_full_across_a_test_split_by_the_cutoff(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await fixture(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    for n in (8, 7, 6, 5, 0):  # 6: the edge falls between the split test's two buckets
        assert_equal(database, policy, end - n * BUCKET)
    assert_equal(database, policy, end + HOUR)


@pytest.mark.asyncio
async def test_an_hour_without_new_tests_counts_none_of_them_again(database, tmp_path, monkeypatch):
    """The regression: the incremental build counted the daily pools, new IPs and series IPs in
    the database from the observations every hour (on main too); from the memos, an hour
    without a new test counts none of them."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    await daily_history(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    for hours in (2, 1):  # its lists, then the first-seen bins of the windows that reuse them
        calculate(database, policy, as_of=end - hours * HOUR, mode="incremental")
    log = statements(monkeypatch)
    calculate(database, policy, as_of=end, mode="incremental")
    monkeypatch.undo()
    counting = (hourly.BUCKET_IPS, hourly.FIRST_SEEN, sqlwindow.WINDOW, sqlwindow.RUN_BUCKETS)
    assert [q for q, _ in log if any(c in q for c in counting)] == []


def stamps(conn, run_key):
    """When each of the run's recorded ranges was counted: [(first, last, created_at)]."""
    return conn.execute(
        "SELECT first_bucket,last_bucket,created_at FROM calculated_run_ranges WHERE run_key=%s "
        "ORDER BY 1,2",
        (run_key,),
    ).fetchall()


def share_ip(conn, source, target):
    """Let target observe, in its first bucket, an IP its product first saw in source, and
    rebuild product_ips."""
    conn.execute(
        "INSERT INTO ip_observations SELECT t.bucket_start,t.run_key,s.ip_id,1,t.bucket_start,"
        "t.bucket_start FROM (SELECT o.ip_id FROM ip_observations o JOIN test_runs r "
        "USING(run_key) JOIN scope_stats p USING(scope_id) JOIN product_ips m "
        "ON m.product_id=p.product_id AND m.ip_id=o.ip_id WHERE o.run_key=%s "
        "AND m.first_seen>=o.bucket_start ORDER BY o.ip_id LIMIT 1) s,"
        "(SELECT min(bucket_start) AS bucket_start,%s::bigint AS run_key FROM run_buckets "
        "WHERE run_key=%s) t ON CONFLICT DO NOTHING",
        (source, target, target),
    )
    conn.execute(PRODUCT_IPS)


@pytest.mark.asyncio
async def test_a_late_run_lowering_first_seen_fires_the_rule_and_an_in_order_run_does_not(
    database, tmp_path, monkeypatch
):
    end = floor_hour(datetime.now(UTC)) + HOUR
    h = History(database, tmp_path, end, seed_value=5)
    old, later = await h.daily("beta", "us", 50 * HOUR), await h.daily("beta", "us", 26 * HOUR)
    await h.daily("alpha", "us", 26 * HOUR)
    policy = lenient_policy(tmp_path)
    calculate(database, policy, as_of=end, mode="incremental")  # its lists and memos
    with connect(database) as conn:
        product = product_of(conn, later)
        before = {k: stamps(conn, k) for k in (old, later)}
        binned = "SELECT count(*) FROM calculated_lists WHERE figures ? 'first_seen'"
        assert conn.execute(binned).fetchone()[0]  # CALC-INC-1b's histograms: none is returned
    # An in-order run that sees an IP of an earlier one: first seen before it, so no rule.
    in_order = await h.daily("beta", "us", 3 * HOUR)
    with connect(database) as conn:
        share_ip(conn, later, in_order)
        assert hourly.stale_lists(conn, [old, later, in_order]) == frozenset()
        assert {k: stamps(conn, k) for k in (old, later)} == before and stamps(conn, in_order)
        asked = dict.fromkeys((old, later, in_order), "us")
        check(conn, product, asked, end)
    # A late run, 60 hours back, sees an IP of the 26-hour run first: its first sighting moves
    # from that run to before the slice [end-48h, end-24h), which loses new IPs.
    late = await h.daily("beta", "us", 60 * HOUR)
    with connect(database) as conn:
        share_ip(conn, later, late)
        asked = dict.fromkeys((old, later, in_order, late), "us")
        with conn.transaction(force_rollback=True), monkeypatch.context() as patch:
            never = "SELECT 0 AS product_id,now() AS since WHERE cardinality(%s::bigint[])<0"
            patch.setattr(hourly, "LATE", never + " AND cardinality(%s::int[])<0")
            stale = hourly.slice_figures(conn, {"g": (product, asked)}, {"g": slices_at(end)})
            fresh = reference_slices(conn, product, asked, slices_at(end))
            since = end - 2 * DAY  # without the rule the memo overcounts
            assert hourly.new_ips(stale["g"][1], since) > hourly.new_ips(fresh[1], since)
        assert hourly.stale_lists(conn, sorted(asked)) == frozenset()
        # Every recorded run of the product with a bucket since the late run's is counted again.
        after = {k: stamps(conn, k) for k in (old, later)}
        assert all(after[k] and after[k] != before[k] for k in (old, later)), (before, after)
        assert stamps(conn, late)
        check(conn, product, asked, end)
        conn.commit()
        again = {k: stamps(conn, k) for k in (old, later, in_order, late)}
        assert hourly.stale_lists(conn, sorted(asked)) == frozenset()  # once: recorded now
        assert {k: stamps(conn, k) for k in again} == again
    assert_equal(database, policy, end)


@pytest.mark.asyncio
async def test_a_late_run_a_full_build_records_first_is_still_checked(database, tmp_path):
    """The S1 regression: a full build records the late run's ranges (store.write() ->
    network.attach() -> bucket_ips()); the late-run rule must still run, so the next incremental
    build, whose 24h window is its widest hit, equals full mode."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    h = History(database, tmp_path, end, seed_value=5)
    recent = await h.daily("beta", "us", 5 * HOUR)
    await h.daily("beta", "us", 50 * HOUR)
    await h.daily("alpha", "us", 26 * HOUR)
    policy = lenient_policy(tmp_path)
    calculate(database, policy, as_of=end, mode="incremental")
    late = await h.daily("beta", "us", 60 * HOUR)
    with connect(database) as conn:
        share_ip(conn, recent, late)  # the late run sees an IP the 5-hour run saw first
    calculate(database, policy, as_of=end, mode="full")  # records the late run's ranges
    extra = await h.daily("beta", "us", 72 * HOUR)  # changes the 7d and 30d windows only
    with connect(database) as conn:  # it sees no IP another run saw: no rule of its own
        conn.execute(
            "DELETE FROM ip_observations WHERE run_key=%s AND ip_id IN "
            "(SELECT ip_id FROM ip_observations WHERE run_key<>%s)",
            (extra, extra),
        )
        conn.execute(PRODUCT_IPS)
    assert_equal(database, policy, end)


@pytest.mark.asyncio
async def test_a_run_backfilled_outside_every_window_is_checked_too(database, tmp_path):
    """A run 40 days back (no window holds it) that saw an IP of a recent run first lowers its
    first sighting: the next incremental build, every window a hit, equals full mode."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    h = History(database, tmp_path, end, seed_value=5)
    recent = await h.daily("beta", "us", 5 * HOUR)
    await h.daily("beta", "us", 50 * HOUR)
    policy = lenient_policy(tmp_path)
    calculate(database, policy, as_of=end - HOUR, mode="incremental")
    calculate(database, policy, as_of=end, mode="incremental")  # every window's first-seen bins
    old = await h.daily("beta", "us", 40 * DAY)
    with connect(database) as conn:
        share_ip(conn, recent, old)
    assert_equal(database, policy, end)
    with connect(database) as conn:
        assert stamps(conn, old)  # recorded once, so not checked again


@pytest.mark.asyncio
async def test_estimate_distinct_counts_are_counted_once_per_run_set(
    database, tmp_path, monkeypatch
):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await daily_history(database, tmp_path, end)
    with connect(database) as conn:
        conn.row_factory = dict_row
        found = estimates.windows_with(*_estimate_groups(conn, end))
        assert found
        sets = [{(r["run_key"], r["country_code"]) for r in rows} for _, rows in found]
        for by_country in (True, False):
            assert hourly.window_distinct(conn, sets, by_country) == distinct_ips(
                conn, sets, by_country
            )
        assert estimates.distinct(conn, found) == distinct_ips(conn, sets)
        assert hourly.window_distinct(conn, [set()]) == [{}]
        log = statements(monkeypatch)
        assert hourly.window_distinct(conn, sets) == distinct_ips(conn, sets)
        monkeypatch.undo()
        assert len([q for q, _ in log if "ip_observations" in q]) == 1  # the reference's only
        stored = conn.execute("SELECT count(*) AS n FROM calculated_distinct").fetchone()["n"]
    assert stored == 2 * len({frozenset(s) for s in sets})


def _estimate_groups(conn, as_of):
    """(groups, run_group, hours) as estimates.attach() gives windows_with() for one build."""
    hours = estimates.fetch(conn, as_of)
    run_group = {r["run_key"]: (r["product_id"], r["country_code"]) for r in hours}
    groups = {
        key: {"windows": {"24h": {"period": {"start": as_of - DAY}}}}
        for key in set(run_group.values())
    }
    return groups, run_group, hours


@pytest.mark.asyncio
async def test_memos_unused_for_two_days_and_ranges_of_old_runs_are_collected(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    h = History(database, tmp_path, end)
    old = await h.daily("alpha", "us", 40 * DAY)
    recent = await h.daily("alpha", "us", 2 * HOUR)
    with connect(database) as conn:
        product = product_of(conn, recent)
        asked = {old: "us", recent: "us"}
        hourly.slice_figures(conn, {"g": (product, asked)}, {"g": [(end - 41 * DAY, end)]})
        ranged = "SELECT DISTINCT run_key FROM calculated_run_ranges"
        assert {k for (k,) in conn.execute(ranged)} == {old, recent}
        memos = (("calculated_slot_figures", "figures"), ("calculated_distinct", "counts"))
        for table, column in memos:
            conn.execute(
                f"INSERT INTO {table}(digest,{column},last_used) VALUES "
                "('hourly-v1-stale','{}',now()-interval '3 days'),"
                "('hourly-v1-kept','{}',now()-interval '1 day'),"
                "('other','{}',now()-interval '3 days')"
            )
        hourly.slice_figures(conn, {}, {})  # collected once per transaction: not again yet
        assert {k for (k,) in conn.execute(ranged)} == {old, recent}
        conn.commit()
        hourly.bucket_ips(conn, {}, {})  # every caller collects, full mode's network.attach() too
        for table, _ in memos:
            found = {d for (d,) in conn.execute(f"SELECT digest FROM {table}")}
            assert {"hourly-v1-kept", "other"} <= found and "hourly-v1-stale" not in found, table
        assert {k for (k,) in conn.execute(ranged)} == {recent}
