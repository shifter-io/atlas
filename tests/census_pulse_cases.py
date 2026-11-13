"""Shared helpers of the census and pulse tests: test batches, products, batch runs, outages and
the snapshot scenario of a census and a pulse run."""

from datetime import datetime, timedelta

from test_storage import enrichment, measurement
from test_storage_v6 import BERLIN, NETWORK, census

from atlas.calculated import store
from atlas.storage import connect, ingest, seed

MUNICH = BERLIN | {"region_code": "BY", "region": "Bavaria", "city": "Munich"}
HOSTING = {
    "isp": "Hetzner",
    "organization": "Hetzner",
    "asn": 24940,
    "asn_organization": "Hetzner Online GmbH",
    "connection_type": "Corporate",
    "usage_type": "hosting",
}


def batch(conn, kind, at, *, country="DE", target="generic_http", status="done", minutes=1):
    return conn.execute(
        "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,start_at,"
        "started_at,finished_at,status) SELECT %s,%s,region_id,%s,%s,%s,%s,%s,%s FROM countries "
        "WHERE country_code=%s RETURNING batch_id",
        (kind, country, target, at, at, at, at + timedelta(minutes=minutes), status, country),
    ).fetchone()[0]


def product(conn, provider="alpha", name="residential"):
    conn.execute("INSERT INTO providers VALUES (%s) ON CONFLICT DO NOTHING", (provider,))
    conn.execute(
        "INSERT INTO products(provider,name) VALUES (%s,%s) ON CONFLICT DO NOTHING",
        (provider, name),
    )
    return conn.execute(
        "SELECT product_id FROM products WHERE provider=%s AND name=%s", (provider, name)
    ).fetchone()[0]


def batch_run(conn, batch_id, product_id, finished, *, run_key=None, **counters):
    values = {
        "requests": 100,
        "successes": 100,
        "unique_ips": 0,
        "challenges": 0,
        "blocks": 0,
        "bytes_up": 0,
        "bytes_down": 0,
        "stop_reason": None,
    } | counters
    conn.execute(
        "INSERT INTO batch_runs(batch_id,product_id,status,started_at,finished_at,run_key,"
        + ",".join(values)
        + ") VALUES (%s,%s,'done',%s,%s,%s,"
        + ",".join(["%s"] * len(values))
        + ")",
        (
            batch_id,
            product_id,
            finished - timedelta(minutes=1),
            finished,
            run_key,
            *values.values(),
        ),
    )


async def scenario(database, tmp_path):
    """alpha: a census (Berlin) and a pulse run (Munich, hosting), pulse batches for uptime and
    an ad-hoc run; beta: one census. Returns the cutoff and ids."""
    store.initialize(database)
    seed(database)
    census_dir, census_summary = await census(tmp_path, provider="alpha")
    enrichment(census_dir, attributes=BERLIN | NETWORK)
    pulse_dir, _ = await census(tmp_path, provider="alpha", location=MUNICH, network=HOSTING)
    beta_dir, _ = await census(tmp_path, provider="beta")
    started = datetime.fromisoformat(census_summary["started_at"])
    slot = started - timedelta(minutes=1)
    with connect(database) as conn:
        census_batch = batch(conn, "census", slot)
        pulse_batch = batch(conn, "pulse", slot)
    keys = {
        "census": ingest(census_dir, database, batch_id=census_batch)["run_key"],
        "pulse": ingest(pulse_dir, database, batch_id=pulse_batch)["run_key"],
        "beta": ingest(beta_dir, database, batch_id=census_batch)["run_key"],
    }
    adhoc = await measurement(tmp_path, provider="alpha")
    ingest(adhoc, database)
    finished = started + timedelta(minutes=2)
    with connect(database) as conn:
        alpha, beta = product(conn, "alpha"), product(conn, "beta")
        batch_run(
            conn,
            census_batch,
            alpha,
            finished,
            run_key=keys["census"],
            requests=4,
            successes=3,
            unique_ips=1,
            bytes_up=999,
            bytes_down=999,
            stop_reason="unique_ratio",
        )
        batch_run(conn, census_batch, beta, finished, run_key=keys["beta"], requests=4)
        batch_run(
            conn, pulse_batch, alpha, finished, run_key=keys["pulse"], requests=4, successes=3
        )
        # Three more pulses without artifacts: two https (one down), one http down.
        for minutes, target, successes in (
            (-30, "generic_https", 50),
            (-60, "generic_https", 0),
            (-90, "generic_http", 0),
        ):
            moment = slot + timedelta(minutes=minutes)
            batch_run(
                conn,
                batch(conn, "pulse", moment, target=target),
                alpha,
                moment + timedelta(minutes=1),
                successes=successes,
                bytes_up=1000,
                bytes_down=4000,
            )
    as_of = finished + timedelta(minutes=10)
    return as_of, {"alpha": alpha, "beta": beta, "census_batch": census_batch}


def outage(conn, product_id, start, end=None, *, country="DE", source="census"):
    conn.execute(
        "INSERT INTO outages(product_id,country_code,region_id,source,started_at,ended_at) "
        "SELECT %s,%s,region_id,%s,%s,%s FROM countries WHERE country_code=%s",
        (product_id, country, source, start, end, country),
    )
