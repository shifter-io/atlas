"""CALC-SCALE-1 (#1125): the countries tested at each point of a series
(outage_time.tested_by_point(), which series() reads) are exactly what each point found by
testing every run, as series() did: a run counts at a point when it started before the point ends
and ended after it starts. One pass over the runs instead of every run at each of the 288 points
of a 24h series (a production product has ~7,000 tested runs a day, 31 days of them)."""

import random
from datetime import UTC, datetime, timedelta

from atlas.calculated.outage_time import tested_by_point

START = datetime(2026, 11, 14, tzinfo=UTC)


def scanned(runs, start, end, step):
    """series()'s former scan: every run at each point; {i: countries}, empty points left out."""
    found, t, i = {}, start, 0
    while t < end:
        stop = min(t + step, end)
        countries = {r["country"] for r in runs if r["started_at"] < stop and r["last"] > t}
        if countries:
            found[i] = countries
        t, i = t + step, i + 1
    return found


def runs(rng, start, end, step):
    """Random runs around the series, of every length (none, inside a point, over many), and runs
    on the points' edges."""
    minutes = int((end - start).total_seconds() // 60)
    found = []
    for _ in range(500):
        begin = start + timedelta(minutes=rng.randint(-3000, minutes + 3000))
        length = timedelta(minutes=rng.choice([0, 1, 4, 5, 7, 60, 1440, 3000]))
        country = rng.choice("ABCDEFGH")
        found.append({"country": country, "started_at": begin, "last": begin + length})
    edges = ((start + step, start + 2 * step), (end, end + step), (start - step, start))
    edges += ((start, start), (end - step, end), (start + 3 * step, start + 3 * step))
    for n, (a, b) in enumerate(edges):
        found.append({"country": "XYZ"[n % 3], "started_at": a, "last": b})
    return found


def test_each_point_holds_the_countries_of_the_runs_that_overlap_it():
    rng = random.Random(1125)
    cases = (
        (timedelta(minutes=5), START + timedelta(hours=24)),  # a 24h series
        (timedelta(days=1), START + timedelta(days=7)),  # a 7d series
        (timedelta(days=1), START + timedelta(days=6, hours=5, minutes=35)),  # a partial last point
        (timedelta(minutes=5), START + timedelta(minutes=5)),  # one point
    )
    for step, end in cases:
        found = runs(rng, START, end, step)
        assert tested_by_point(found, START, end, step) == scanned(found, START, end, step)


def test_no_runs_no_points():
    assert tested_by_point([], START, START + timedelta(hours=24), timedelta(minutes=5)) == {}
