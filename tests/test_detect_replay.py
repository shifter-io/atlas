"""TAPE-FAST-1: the tape's fast replay (detect_replay) gives the step replay's events and outages,
against an isolated PostgreSQL.

The fixture: 2 days of hourly pulses (and 3 before them) of alpha, beta and gamma in DE, FR, PL
and US, each a run with its own median latency. alpha is down in DE (slots 10 to 13: down, then
recovered) and slower in US (slots 25 and 26: one latency spike); beta is blocked in FR (slots
15 and 16: one block spike) and down in PL from slot 40 to the end (an outage still open); gamma
drops in US (slots 14 to 18: success_drop, recovered) and has a census outage in FR (from
before the span, closed by its next pulse); alpha and beta exit through a hosting ASN once each.
alpha's DE success rate moves each slot (pulse_finished).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from census_pulse_cases import batch, batch_run, outage, product
from orchestrator_cases import prepare, query
from test_storage import measurement

from proxy_index import detect, detect_replay
from proxy_index.daily import tape
from proxy_index.storage import connect, ingest, partition

pytest_plugins = ["test_storage"]

T0 = datetime(2026, 10, 25, tzinfo=UTC)
SLOT = timedelta(hours=1)
SLOTS = 48
END = T0 + SLOTS * SLOT
THIRD = T0 + 16 * SLOT
COUNTRIES = ("DE", "FR", "PL", "US")
EVENTS = (
    "SELECT kind,batch_id,product_id,country_code,target_id,region_id,signal,severity,data,at "
    "FROM test_events ORDER BY event_id"
)
OUTAGES = (
    "SELECT product_id,country_code,region_id,source,batch_id,started_at,ended_at FROM outages "
    "ORDER BY product_id,country_code,started_at"
)
CLONE = (
    "INSERT INTO test_runs(run_id,scope_id,started_at,finished_at,status,summary_sha256,summary) "
    "SELECT gen_random_uuid(),scope_id,%s,%s,status,summary_sha256,summary FROM test_runs "
    "WHERE run_key=%s RETURNING run_key"
)
BUCKET = (
    "INSERT INTO run_buckets(bucket_start,run_key,completed,successes,response_body_bytes,"
    "errors,status_codes,histogram_steps,histogram_counts) VALUES (%s,%s,%s,%s,0,'{}','{}',%s,%s)"
)
GEO = "INSERT INTO run_geo_buckets VALUES (%s,%s,%s,%s,10,10,0,0,0,0,0,%s,%s)"


def pulse(name, country, slot):
    """(successes, blocks, p50 ms) of a 100-request pulse."""
    successes, blocks, p50 = 95, 0, 200 + 10 * COUNTRIES.index(country)
    if name == "alpha" and country == "DE":
        successes = 0 if slot in range(10, 14) else 95 - 7 * (slot % 2)
    if name == "alpha" and country == "US" and slot in (25, 26):
        p50 = 900
    if name == "beta" and country == "FR" and slot in (15, 16):
        successes, blocks = 65, 30
    if name == "beta" and country == "PL" and slot >= 40:
        successes = 0
    if name == "gamma" and country == "US" and slot in range(14, 19):
        successes = 60
    return successes, blocks, p50


def hosting_network(conn):
    """A hosting network and a location for run_geo_buckets."""
    conn.execute("INSERT INTO asn_names(asn,name) VALUES (24940,'Hetzner Online GmbH')")
    network = conn.execute(
        "INSERT INTO enrichment_networks(asn_name_id,usage_type) SELECT asn_name_id,'hosting' "
        "FROM asn_names WHERE asn=24940 RETURNING network_id"
    ).fetchone()[0]
    conn.execute("INSERT INTO place_names DEFAULT VALUES ON CONFLICT DO NOTHING")
    conn.execute(
        "INSERT INTO enrichment_locations(place_id) SELECT place_id FROM place_names "
        "WHERE country_code IS NULL AND city IS NULL ON CONFLICT DO NOTHING"
    )
    location = conn.execute("SELECT min(location_id) FROM enrichment_locations").fetchone()[0]
    return network, location


async def stored(database, tmp_path, *, countries=COUNTRIES, names=("alpha", "beta", "gamma")):
    """The fixture (module docstring) from slot -3 to the end; returns the products' ids."""
    prepare(database, COUNTRIES)
    model = ingest(await measurement(tmp_path), database)["run_key"]
    hosted = {("alpha", "DE", 5), ("alpha", "DE", 20), ("beta", "US", 30)}
    with connect(database) as conn:
        ids = {name: product(conn, name) for name in names}
        network, location = hosting_network(conn)
        for slot in range(-3, SLOTS):
            at = T0 + slot * SLOT
            partition(conn, "run_buckets", at)
            partition(conn, "run_geo_buckets", at)
            for country in countries:
                batch_id = batch(conn, "pulse", at, country=country)
                for name in names:
                    successes, blocks, p50 = pulse(name, country, slot)
                    finished = at + timedelta(minutes=1)
                    key = conn.execute(CLONE, (at, finished, model)).fetchone()[0]
                    conn.execute(BUCKET, (at, key, 100, successes, [p50], [successes or 1]))
                    if (name, country, slot) in hosted:
                        conn.execute(GEO, (at, key, location, network, [p50], [10]))
                    batch_run(conn, batch_id, ids[name], finished, run_key=key)
                    conn.execute(
                        "UPDATE batch_runs SET successes=%s,blocks=%s WHERE run_key=%s",
                        (successes, blocks, key),
                    )
        if "gamma" in ids:
            outage(conn, ids["gamma"], T0 - 2 * SLOT, country="FR")
    return ids


def copy(databases, database):
    return databases(template=database.stem)


def tape_of(database):
    return query(database, EVENTS), query(database, OUTAGES)


def replayed(database, *spans):
    with connect(database) as conn:
        for since, until, fast in spans:
            tape.replay(conn, since, until, fast=fast)
    return tape_of(database)


async def test_the_fast_replay_gives_the_step_replays_events_and_outages(
    database, databases, tmp_path
):
    ids = await stored(database, tmp_path)
    fast, mixed = copy(databases, database), copy(databases, database)
    step = replayed(database, (T0, END, False))
    assert replayed(fast, (T0, END, True)) == step
    assert replayed(mixed, (T0, THIRD, False), (THIRD, END, True)) == step
    events, outages = step
    kinds = {(kind, product) for kind, _, product, *_ in events}
    alpha, beta, gamma = ids["alpha"], ids["beta"], ids["gamma"]
    for kind, product_id in (
        ("down", alpha),
        ("down", beta),
        ("recovered", alpha),
        ("recovered", gamma),
        ("success_drop", gamma),
        ("latency_spike", alpha),
        ("block_spike", beta),
        ("new_hosting_asn", alpha),
        ("new_hosting_asn", beta),
        ("pulse_finished", alpha),
    ):
        assert (kind, product_id) in kinds
    spikes = [e for e in events if e[0] in ("block_spike", "latency_spike")]
    assert len(spikes) == 2  # once per spike
    census = [e for e in events if e[0] == "recovered" and e[8].get("source") == "census"]
    assert [e[3] for e in census] == ["FR"]
    by = {(row[0], row[1].strip(), row[3]): row for row in outages}
    assert by[(alpha, "DE", "pulse")][6] is not None and by[(beta, "PL", "pulse")][6] is None
    assert by[(gamma, "FR", "census")][6] == T0


async def test_run_twice_adds_nothing_and_a_dry_run_writes_nothing(database, tmp_path):
    await stored(database, tmp_path, names=("alpha", "beta"))
    with connect(database) as conn:
        dry = tape.replay(conn, T0, END, dry_run=True)
    assert tape_of(database) == ([], [])
    with connect(database) as conn:
        found = tape.replay(conn, T0, END)
        assert found["events"] == dry["events"] and found["outages"] == dry["outages"]
        assert found["steps"] == SLOTS * 12 and tape.watermark(conn) == END - tape.TICK
    written = tape_of(database)
    with connect(database) as conn:
        again = tape.replay(conn, T0, END)
    assert again["events"] == {} and tape_of(database) == written


async def test_another_detector_moving_the_watermark_stops_the_step_replay(
    database, monkeypatch, tmp_path
):
    """test_daily_tape checks the fast replay (the default) through the command."""
    await stored(database, tmp_path, countries=("DE",), names=("alpha",))
    real = detect.detect

    def orchestrator(conn, now, **options):
        found = real(conn, now, **options)
        detect.set_watermark(conn.cursor(), now + SLOT, now)
        return found

    monkeypatch.setattr(detect, "detect", orchestrator)
    with connect(database) as conn, pytest.raises(tape.TapeError, match="moved the watermark"):
        tape.replay(conn, T0, END, fast=False)


def counted(monkeypatch):
    """Count the statements of each fast hour: a list of counts, one per hour."""
    hours, inside = [], []
    real_execute, real_hour = psycopg.Cursor.execute, detect_replay.Replay.hour

    def execute(self, *args, **options):
        if inside:
            hours[-1] += 1
        return real_execute(self, *args, **options)

    def hour(self, upto, end, step):
        hours.append(0)
        inside.append(True)
        try:
            return real_hour(self, upto, end, step)
        finally:
            inside.pop()

    monkeypatch.setattr(psycopg.Cursor, "execute", execute)
    monkeypatch.setattr(detect_replay.Replay, "hour", hour)
    return hours


@pytest.mark.parametrize("countries", [("DE",), COUNTRIES])
async def test_an_hours_round_trips_do_not_grow_with_its_runs(
    database, monkeypatch, tmp_path, countries
):
    """Steady pulses (no event after the first): the same statements an hour for 2 runs or 12."""
    await stored(database, tmp_path, countries=countries, names=("gamma", "omega"))
    hours = counted(monkeypatch)
    with connect(database) as conn:
        for name in ("gamma", "omega"):
            conn.execute(
                "UPDATE batch_runs SET successes=95,blocks=0 WHERE product_id=%s",
                (product(conn, name),),
            )
        conn.execute("DELETE FROM outages")
        partition(conn, "test_events", T0)
        tape.replay(conn, T0 + 20 * SLOT, T0 + 24 * SLOT)
    # An hour's batches, runs, slots, medians, hosting, partition and watermark: one each.
    assert hours == [7] * 4, json.dumps(hours)
