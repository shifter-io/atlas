"""The one-pass window statement (calculated.sqlwindow.onepass) against the per-window statement
it replaced, kept here as the reference: the same WindowInputs for every window of
test_sqlwindow's scenario and of random synthetic groups, and a plan that hash-aggregates each
observation row once."""

import random
from datetime import timedelta

import psycopg
import pytest
from psycopg.rows import dict_row
from test_sqlwindow import MINUTE, seeded, specs_of
from test_sqlwindow_ranges import _scans

from proxy_index.calculated import sqlwindow
from proxy_index.calculated.sqlwindow import (
    _LABELS,
    _RUN_RELEASE,
    LABELS,
    MAX_WINDOWS,
    WindowSpec,
    window_inputs,
    window_query,
)
from proxy_index.storage import connect

pytest_plugins = ["test_storage", "calculated_template"]

# The statement before CALC-WINDOW-ONEPASS-1: one row per (window, run), so each window joins,
# sorts and labels its observation rows on its own.
OLD = f"""WITH k AS (
 SELECT k.slot,k.run_key,k.since,k.until,k.product_id,k.asked,{_RUN_RELEASE} AS release_id
 FROM unnest(%(slots)s::int[],%(keys)s::bigint[],%(since)s::timestamptz[],
  %(until)s::timestamptz[],%(products)s::int[],%(asked)s::text[])
  AS k(slot,run_key,since,until,product_id,asked)
), ip AS (
 SELECT k.slot,k.product_id,o.ip_id,sum(o.count)::bigint AS observations,
  count(DISTINCT o.bucket_start::date) AS days,count(DISTINCT k.asked) AS asking,
  min(k.asked) AS asked,(array_agg(k.release_id ORDER BY o.run_key))[1] AS release_id
 FROM k JOIN ip_observations o ON o.run_key=k.run_key AND o.bucket_start>=k.since
  AND o.bucket_start<k.until
 GROUP BY k.slot,k.product_id,o.ip_id
), labelled AS (
 SELECT ip.slot,ip.observations,ip.days,e.matched,
  {",".join(f"e.{f}" for f in LABELS)},
  coalesce(e.country_code,'')<>'' AND ip.asking>0 AS located,
  coalesce(e.country_code,'')<>'' AND ip.asking=1 AND ip.asked=lower(e.country_code) AS geo,
  date_bin('5 minutes',m.first_seen,'1970-01-01 00:00:00+00'::timestamptz) AS bin
 FROM ip JOIN product_ips m ON m.product_id=ip.product_id AND m.ip_id=ip.ip_id
 LEFT JOIN ip_enrichment_fields e ON e.ip_id=ip.ip_id AND e.release_id=ip.release_id
)
SELECT slot,GROUPING(country_code,bin) AS part,{_LABELS},bin,count(*) AS ips,
 sum(observations)::bigint AS observations,count(*) FILTER (WHERE located) AS located,
 count(*) FILTER (WHERE geo) AS geo_matched,count(*) FILTER (WHERE days>=2) AS recurring,
 count(*) FILTER (WHERE matched) AS matched
FROM labelled GROUP BY GROUPING SETS ((slot,{_LABELS}),(slot,bin),(slot))
ORDER BY slot,part,{_LABELS},bin"""

MODES = [("newest", True), ("per_run", True), ("per_run", False), ("newest", False)]


@pytest.fixture
def database(calculated_database):
    """Every test here works on the current calculated schema, copied from its template."""
    return calculated_database


@pytest.fixture
async def scenario(database, tmp_path):
    """test_sqlwindow's seeded observations."""
    return await seeded(database, tmp_path)


def old_inputs(conn, specs, mode, release):
    """The per-window statement's WindowInputs of each spec."""
    params = {name: [] for name in ("slots", "keys", "since", "until", "products", "asked")}
    for slot, spec in enumerate(specs):
        for run_key in sorted(set(spec.runs)):
            asked = spec.asked.get(run_key)
            for name, value in zip(
                params,
                (slot, run_key, spec.since, spec.until, spec.product_id),
                strict=False,
            ):
                params[name].append(value)
            params["asked"].append(asked.lower() if asked else None)
    params |= {"per_run": mode == "per_run", "release": release}
    parts = [{"combos": [], "first_seen": {}, "totals": {}} for _ in specs]
    for row in conn.cursor(row_factory=dict_row).execute(OLD, params):
        sqlwindow._add(parts[row["slot"]], row)
    return {spec.key: sqlwindow._inputs(part) for spec, part in zip(specs, parts, strict=True)}


def _compare(conn, specs, mode, release):
    new = window_inputs(conn, specs, release_mode=mode, release_id=release)
    old = old_inputs(conn, specs, mode, release)
    assert list(new) == list(old)
    for key in old:
        assert new[key] == old[key], key
    return new


def _windows(scenario, runs, asked, hours, product=None, end=None):
    """24h, 7d and 30d (or `hours`) specs of one group, ending at `end` (default as_of)."""
    end = end or scenario["as_of"]
    product = product or scenario["runs"][0][1]
    return [
        WindowSpec((tuple(runs), h), product, tuple(runs), end - timedelta(hours=h), end, asked)
        for h in hours
    ]


def _groups(scenario):
    """Groups of several windows: a cohort of five runs with three requested countries, a run
    whose data is shorter than 30 days, and windows that neither nest nor reach as_of."""
    (r0, p), (r1, _), (r2, _), (r3, _), (r4, _), (r5, q) = scenario["runs"]
    asked = {r0: "us", r1: "us", r2: "de", r3: None, r4: "US"}
    specs = _windows(scenario, (r0, r1, r2, r3, r4), asked, (24, 168, 720))
    specs += _windows(scenario, (r1,), asked, (24, 168, 720))
    specs += _windows(scenario, (r0, r2), asked, (1, 6, 30, 200, 744))
    specs += _windows(scenario, (r5,), {r5: "us"}, (24, 168, 720), product=q)
    edge = scenario["as_of"] - timedelta(hours=23)
    specs += _windows(scenario, (r0, r1, r2, r3, r4), asked, (1, 25), end=edge)
    return specs


@pytest.mark.parametrize(("mode", "with_release"), MODES)
async def test_onepass_equals_the_per_window_statement(scenario, database, mode, with_release):
    release = scenario["releases"][-1] if with_release else None
    specs = specs_of(scenario) + _groups(scenario)
    with connect(database) as conn:
        found = _compare(conn, specs, mode, release)
    assert sum(1 for inputs in found.values() if inputs.unique_ips) >= len(specs) - 1


async def test_the_groups_cover_the_cases(scenario, database):
    """Several requested countries, runs with different releases in one window, IPs on one day
    and on several, NULL and empty labels, unmatched IPs and a 30-day window longer than the
    data."""
    specs = _groups(scenario)
    with connect(database) as conn:
        found = window_inputs(conn, specs, release_mode="per_run", release_id=None)
        releases = dict(
            conn.execute("SELECT run_key,max(release_id) FROM run_enrichments GROUP BY 1")
        )
    cohort = [found[s.key] for s in specs[:3]]
    assert all(0 < w.recurring_ips < w.unique_ips for w in cohort[1:])
    assert all(w.enrichment_matched < w.unique_ips for w in cohort)
    places = {c.place for w in cohort for c in w.combos}
    assert (None,) * 5 in places and any(p[0] == "" for p in places)
    located = sum(c.located for c in cohort[2].combos)
    assert 0 < sum(c.geo_matched for c in cohort[2].combos) < located
    runs = specs[0].runs
    assert len({releases.get(r) for r in runs}) == 3  # two releases, and runs with none
    short = [found[s.key] for s in specs[3:6]]  # one run of one day, every window covers it
    assert short[0].unique_ips and short[0] == short[1] == short[2]


async def test_more_windows_than_one_pass_counts(scenario, database):
    """A group with more than MAX_WINDOWS windows is counted in several passes."""
    (r2, p) = scenario["runs"][2]
    as_of = scenario["as_of"]
    specs = [
        WindowSpec(("day", d), p, (r2,), as_of - timedelta(days=d + 2), as_of - timedelta(days=d))
        for d in range(2 * MAX_WINDOWS + 1)
    ]
    statement, params, slots = window_query(specs, release_mode="newest", release_id=None)
    assert len(set(params["groups"])) == 3 and sorted(slots.values()) == list(range(len(specs)))
    assert "s7" in statement and "s8" not in statement
    with connect(database) as conn:
        found = _compare(conn, specs, "newest", scenario["releases"][0])
    assert all(found[s.key].unique_ips for s in specs[:14])


async def test_random_groups_equal_the_per_window_statement(scenario, database):
    """Property: random batches of groups (run subsets, requested countries, windows on and
    off the 5-minute grid) give the old statement's inputs."""
    rng = random.Random(779)
    runs = scenario["runs"]
    as_of = scenario["as_of"]
    with connect(database) as conn:
        for _ in range(25):
            specs = []
            for g in range(rng.randint(1, 4)):
                product = rng.choice([p for _, p in runs])
                own = [k for k, p in runs if p == product]
                chosen = tuple(rng.sample(own, rng.randint(1, len(own))))
                asked = {k: rng.choice(["us", "US", "de", "", None]) for k in chosen}
                for w in range(rng.randint(1, 10)):
                    end = as_of - rng.randint(0, 30 * 288) * 5 * MINUTE
                    if rng.random() < 0.3:
                        end -= rng.randint(1, 299) * timedelta(seconds=1)
                    since = end - rng.randint(1, 31 * 288) * 5 * MINUTE
                    specs.append(WindowSpec((g, w), product, chosen, since, end, asked))
            mode, with_release = rng.choice(MODES)
            release = rng.choice(scenario["releases"]) if with_release else None
            _compare(conn, specs, mode, release)


def _nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


def _raw_scans(node):
    """The scans of ip_observations under node that no aggregate stands between."""
    for child in node.get("Plans", []):
        if child.get("Relation Name", "").startswith("ip_observations"):
            yield child
        elif child["Node Type"] != "Aggregate":
            yield from _raw_scans(child)


async def test_the_plan_hashes_each_observation_once(scenario, database):
    """Each observation partition is scanned once, by primary key; the day and per-IP aggregates
    are hashed, and no Sort stands between them and the scans."""
    specs = _windows(scenario, [k for k, _ in scenario["runs"][:5]], {}, (24, 168, 720))
    statement, params, _ = window_query(specs, release_mode="per_run", release_id=None)
    with connect(database) as conn, conn.transaction():
        conn.execute("SET LOCAL enable_seqscan = off")
        conn.execute("SET LOCAL enable_sort = off")
        cursor = psycopg.ClientCursor(conn)
        plan = cursor.execute("EXPLAIN (FORMAT JSON) " + statement, params).fetchone()[0]
    root = plan[0]["Plan"]
    scans = list(_scans(root))
    assert scans and all(name.endswith("_pkey") for name in scans), scans
    relations = [
        n["Relation Name"]
        for n in _nodes(root)
        if n.get("Relation Name", "").startswith("ip_observations")
    ]
    assert len(relations) == len(set(relations)), relations
    # The day and per-IP levels; the output's GROUPING SETS have "Grouping Sets" instead.
    grouped = [n for n in _nodes(root) if n["Node Type"] == "Aggregate" and "Group Key" in n]
    assert len(grouped) == 2 and all(n["Strategy"] == "Hashed" for n in grouped), grouped
    assert any(list(_raw_scans(n)) for n in grouped)  # the day level reads the scans
    assert not [n for n in _nodes(root) if n["Node Type"] == "Sort" and any(_raw_scans(n))]
