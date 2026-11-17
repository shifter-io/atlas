"""CALC-SCALE-3 step 1 (#1307): a census-pulse window's geo breakdown rows summed in the database
(rollups.ProductGeo.sums(), calculated/geo_sums.py) give exactly the breakdowns Python makes from
the window's geo rows, for every window and cutoff (on the hour, inside one, at midnight), also
merged with IP combinations and with rows read in Python: a country takes its smallest name over
every part and a country only fallbacks reach takes its fallback name.
"""

import random
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.rows import dict_row
from test_calc_rollups import compare, fixture
from test_calculated_hourly_reads import HOUR, floor_hour

from atlas.calculated import rollups
from atlas.calculated.aggregate import breakdowns
from atlas.names import NETWORK, PLACE
from atlas.partitions import partition
from atlas.storage import connect

pytest_plugins = ["test_storage"]

MINUTES = timedelta(minutes=1)


def two_names(conn, rng):
    """Geo rows of every pulse bucket at two new locations of one country, named "Zeta" and
    "Alpha" (the smaller name wins in Python's and COLLATE "C"'s order, not "Zeta")."""
    locations = []
    for name in ("Zeta", "Alpha"):
        place = conn.execute(
            "INSERT INTO place_names(country_code,country,region_code,region,city) "
            "VALUES ('QQ',%s,'Q1','Quarter','Qtown') RETURNING place_id",
            (name,),
        ).fetchone()[0]
        locations.append(
            conn.execute(
                "INSERT INTO enrichment_locations(place_id,latitude,longitude) "
                "VALUES (%s,1,2) RETURNING location_id",
                (place,),
            ).fetchone()[0]
        )
    network = conn.execute("SELECT min(network_id) FROM enrichment_networks").fetchone()[0]
    buckets = conn.execute(
        "SELECT b.bucket_start,b.run_key FROM run_buckets b JOIN test_runs r USING(run_key) "
        "JOIN test_batches t USING(batch_id) WHERE t.kind='pulse'"
    ).fetchall()
    for bucket, run_key in buckets:
        partition(conn, "run_geo_buckets", bucket)
        for location in locations:
            conn.execute(
                "INSERT INTO run_geo_buckets VALUES (%s,%s,%s,%s,%s,%s,0,0,0,0,0,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (bucket, run_key, location, network, 5, rng.randint(1, 5), [50], [2]),
            )


def weighted(rows):
    """IP combinations (sqlwindow's weighted entries) labelled like some geo rows, one of them
    with a country name smaller than the rows' own."""
    found = {}
    for n, g in enumerate(rows[:5]):
        place = list(g["location"] or (None,) * 5)
        if n == 0 and place[0] is not None:
            place[1] = " first"  # sorts before every real name
        row = dict(zip(PLACE, place, strict=True)) | dict(zip(NETWORK, g["network"], strict=True))
        found[n] = {"row": row, "count": 3 + n, "ips": 1 + n}
    return found


EXTRA = [  # rows Python reads (requested_geo()'s): a fallback country, and one placed too
    {"hours": 24, "location": None, "network": None, "fallback": ("ZZ", "Zedland")}
    | {"completed": 4, "successes": 3, "challenges": 1, "blocks": 0, "histogram": [(70, 3)]},
    {"hours": 24, "location": None, "network": None, "fallback": ("QQ", "Qland")}
    | {"completed": 2, "successes": 2, "challenges": 0, "blocks": 0, "histogram": [(90, 1)]},
]


@pytest.mark.asyncio
async def test_summed_census_pulse_rows_give_the_python_breakdowns(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await fixture(database, tmp_path, end)
    with connect(database) as conn:
        two_names(conn, random.Random(1307))
    midnight = end.replace(hour=0)
    checked = 0
    for as_of in (end, end - 20 * HOUR + 25 * MINUTES, midnight):
        compare(database, as_of)  # the rollups up to as_of, equal to the full mode's
        with connect(database) as conn:
            conn.row_factory = dict_row
            geo = rollups.scheduled_inputs(conn, as_of, "generic_http", geo=rollups.ProductGeo)
            geo = geo["geo"]
            products = [r["product_id"] for r in conn.execute("SELECT product_id FROM products")]
            for product in products:
                census_pulse = [g for g in geo.product(product) if g["scope_id"] is None]
                assert [g for g in geo.scoped(product) if g["scope_id"] is None] == []
                for hours in (24, 168, 720):
                    rows = [g for g in census_pulse if g["hours"] <= hours]
                    summed = geo.sums(product, hours)
                    assert breakdowns({}, [], summed) == breakdowns({}, rows), (as_of, hours)
                    ips = weighted(rows)
                    assert breakdowns(ips, EXTRA, summed) == breakdowns(ips, [*rows, *EXTRA])
                    checked += bool(rows)
                    if hours == 720 and rows:
                        country = breakdowns({}, [], summed)["country"]
                        assert {r["country"] for r in country if r["country_code"] == "QQ"} == {
                            "Alpha"
                        }
    assert checked >= 6  # pulses in every window of every cutoff
