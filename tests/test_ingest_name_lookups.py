"""CALC-SCALE-1 (#1125): an import finds the ids of its locations and networks through the name
lists' unique keys, one index entry a label, never by reading a list whole. The jsonb equality
it used read and hashed every row of place_names, asn_names, enrichment_locations and
enrichment_networks for each pulse: 2.2 s at production's 320,000 list rows, under the network's
lock, so an hour's pulses took 16 minutes to store and the lists keep growing.
"""

import time

import ingest_cases
from ingest_cases import pulse

from atlas.storage import connect, ingest

pytest_plugins = ["test_storage"]

LISTS = ("place_names", "asn_names", "enrichment_locations", "enrichment_networks")
# Every kind of missing field: the unknown member (null), a place without a region or a
# coordinate, without a city, a country alone, an ASN without a name.
PLACES = [
    None,
    {"country_code": "US", "country": "United States", "region_code": "CA", "region": "California"}
    | {"city": "Fresno", "latitude": 36.75, "longitude": -119.75},
    {"country_code": "US", "country": "United States", "city": "Fresno"},
    {"country_code": "US", "country": "United States", "region": "California", "latitude": 36.5},
    {"country_code": "US"},
    {"country_code": "US", "country": "United States", "region_code": "CA", "longitude": 1.0},
    {"country": "United States", "city": "Fresno"},
    {"latitude": 36.75, "longitude": -119.75},
    {"country_code": "DE", "country": "Germany", "region_code": "BE", "region": "Berlin"}
    | {"city": "Berlin", "latitude": 52.5, "longitude": 13.25},
    {"country_code": "DE", "country": "Germany", "region_code": "BE", "region": "Berlin"}
    | {"city": "Berlin"},
]
NETWORKS = [
    None,
    {"asn": 64500, "asn_organization": "AS One", "isp": "One", "organization": "One Org"}
    | {"connection_type": "Cable", "usage_type": "residential"},
    {"asn": 64500, "asn_organization": "AS One"},
    {"asn": 64500},
    {"asn_organization": "AS One", "usage_type": "hosting"},
    {"isp": "One", "connection_type": "Cable"},
    {"asn": 64501, "asn_organization": "AS Two", "organization": "Two Org", "usage_type": "isp"},
]


def labelled(monkeypatch):
    monkeypatch.setattr(ingest_cases, "CITIES", PLACES)
    monkeypatch.setattr(ingest_cases, "NETWORKS", NETWORKS)


def test_every_pattern_of_missing_fields_finds_its_own_location_and_network(
    database, tmp_path, monkeypatch
):
    labelled(monkeypatch)
    for n in range(2):  # the second pulse finds every label the first one added
        assert ingest(pulse(tmp_path / f"p{n}", n), database, ip_rows=False)["imported"]
    location = "(SELECT to_jsonb(l)-'location_id' FROM enrichment_location_fields l"
    network = "(SELECT to_jsonb(n)-'network_id' FROM enrichment_network_fields n"
    with connect(database) as conn:
        stored = conn.execute(
            f"SELECT DISTINCT g.location_id,{location} WHERE l.location_id=g.location_id),"
            f"g.network_id,{network} WHERE n.network_id=g.network_id) FROM run_geo_buckets g"
        ).fetchall()
        counts = conn.execute(
            "SELECT count(*),count(DISTINCT location_id),count(DISTINCT network_id) "
            "FROM run_geo_buckets"
        ).fetchone()
    fields = ("country_code", "country", "region_code", "region", "city", "latitude", "longitude")
    places = {lid: tuple(found[f] for f in fields) for lid, found, _, _ in stored}
    nets = ("asn", "asn_organization", "isp", "organization", "connection_type", "usage_type")
    networks = {nid: tuple(found[f] for f in nets) for _, _, nid, found in stored}
    assert sorted(places.values(), key=str) == sorted(
        (tuple((p or {}).get(f) for f in fields) for p in PLACES), key=str
    )
    assert sorted(networks.values(), key=str) == sorted(
        (tuple((w or {}).get(f) for f in nets) for w in NETWORKS), key=str
    )
    assert counts == (140, len(PLACES), len(NETWORKS))  # both pulses' rows, one id a label


def grow(database, rows=20_000):
    """Fill every name list with `rows` more rows, without reading any of them, and analyze."""
    with connect(database) as conn:
        conn.execute(
            "WITH p AS (INSERT INTO place_names(country_code,country,region,city) SELECT 'ZZ',"
            "'Grown','Region '||n,'City '||n FROM generate_series(1,%s) n RETURNING place_id) "
            "INSERT INTO enrichment_locations(place_id,latitude,longitude) "
            "SELECT place_id,1.5,2.5 FROM p",
            (rows,),
        )
        conn.execute(
            "WITH a AS (INSERT INTO asn_names(asn,name) SELECT 4200000000+n,'Grown '||n "
            "FROM generate_series(1,%s) n RETURNING asn_name_id) INSERT INTO enrichment_networks"
            "(asn_name_id,isp,usage_type) SELECT asn_name_id,'Grown','hosting' FROM a",
            (rows,),
        )
        conn.execute("ANALYZE " + ",".join(LISTS))
    flushed(database, "place_names", rows)


def flushed(database, table, inserted):
    """Wait until the statistics count `inserted` rows into `table` (a closed session's counters
    reach the views once its backend has flushed them)."""
    for _ in range(200):
        with connect(database) as conn:
            found = conn.execute(
                "SELECT n_tup_ins FROM pg_stat_user_tables WHERE relname=%s", (table,)
            ).fetchone()[0]
        if found >= inserted:
            return
        time.sleep(0.05)
    raise AssertionError(f"the statistics of {table} were not flushed")


def scans(database):
    with connect(database) as conn:
        found = conn.execute(
            "SELECT relname,seq_scan FROM pg_stat_user_tables WHERE relname=ANY(%s)", (list(LISTS),)
        )
        return dict(found.fetchall())


def test_a_pulse_reads_no_name_list_whole(database, tmp_path):
    assert ingest(pulse(tmp_path / "p0", 0), database, ip_rows=False)["imported"]
    flushed(database, "test_runs", 1)
    grow(database)
    before = scans(database)
    assert ingest(pulse(tmp_path / "p1", 1), database, ip_rows=False)["imported"]
    flushed(database, "test_runs", 2)
    assert scans(database) == before
