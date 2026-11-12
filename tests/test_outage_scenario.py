"""A provider that is down for two hours is data, not a gap.

24-hour window, 30-minute pulses on both targets in DE, a load wave in the last slot. The
provider is down for the pulses of 18:00, 18:30 and 19:00 (done runs whose requests all failed),
our node shuts down during one of them (a failed run: ours, it never counts), and a census
during the outage gets no success; it is still running at 19:00. Another census overlaps the
pulses of 05:00: those pulses still count for uptime and as requests, but not for time-slot
success or light-load latency. The score stays computed, uptime is about 91.7% (the
outage runs from the last healthy pulse to the first healthy one, two hours), the outage is
recorded, and the census is rerun once, at the first pulse that gets through.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from census_pulse_cases import batch, batch_run, product
from test_storage import enrichment, measurement

from proxy_index.calculated import store
from proxy_index.calculated.calculator import calculate
from proxy_index.detect import detect
from proxy_index.orchestrator import nodework
from proxy_index.orchestrator.db import Database
from proxy_index.orchestrator.nodes import issue_token
from proxy_index.storage import connect, ingest, seed

pytest_plugins = ["test_storage"]

GERMANY = {
    "matched": True,
    "country_code": "DE",
    "country": "Germany",
    "region_code": "BE",
    "region": "Berlin",
    "city": "Berlin",
    "asn": 3320,
    "asn_organization": "Deutsche Telekom AG",
    "connection_type": "Cable/DSL",
    "usage_type": "residential",
}
DOWN = (40, 41, 42)
TARGETS = ("generic_http", "generic_https")


def policy_file(tmp_path):
    """The shipped methodology (coverage 0.90, uptime from 10 batches) for small artifacts."""
    text = (
        open("configs/methodology.toml")
        .read()
        .replace("minimum_requests = 4000", "minimum_requests = 1")
        .replace("min_latency_successes = 30", "min_latency_successes = 1")
    )
    path = tmp_path / "methodology.toml"
    path.write_text(text)
    return path


async def test_two_hour_outage_keeps_the_score_and_reruns_the_census(database, tmp_path):
    store.initialize(database)
    seed(database)
    with connect(database) as conn:
        conn.execute("UPDATE countries SET enabled = country_code='DE'")
    node = issue_token(database, provider="alpha", product="residential", region="fra1")
    end = datetime.fromtimestamp(datetime.now(UTC).timestamp() // 1800 * 1800, UTC) + timedelta(
        minutes=30
    )
    slots = [end - timedelta(hours=24) + timedelta(minutes=30 * i) for i in range(48)]
    with connect(database) as conn:
        alpha = product(conn, "alpha")
        # The last slot carries artifacts (they ran just now): both pulses and the load wave.
        last = {
            (kind, target): batch(conn, kind, slots[47], target=target)
            for kind, target in (
                ("pulse", "generic_http"),
                ("pulse", "generic_https"),
                ("load", "generic_http"),
            )
        }
    for (kind, target), batch_id in last.items():
        directory = await measurement(
            tmp_path,
            provider="alpha",
            target=target,
            requested_country="de",
            requests=40,
            concurrency=1000 if kind == "load" else 25,
        )
        if target == "generic_http":
            enrichment(directory, attributes=GERMANY)
        key = ingest(directory, database, batch_id=batch_id)["run_key"]
        with connect(database) as conn:
            batch_run(
                conn,
                batch_id,
                alpha,
                slots[47] + timedelta(minutes=1),
                run_key=key,
                requests=40,
                successes=40,
            )
    census_at = slots[41] + timedelta(minutes=5)
    with connect(database) as conn:
        for i, at in enumerate(slots[:47]):
            for target in TARGETS:
                if i in (43, 44) and target == "generic_http":
                    continue  # these finish through the orchestrator below
                b = batch(conn, "pulse", at, target=target)
                batch_run(
                    conn, b, alpha, at + timedelta(minutes=1), successes=0 if i in DOWN else 100
                )
                if i == 41 and target == "generic_https":
                    # Our node shut down mid-run: not the provider's, so not a slot at all,
                    # and the requests it had counted are not invented as failures.
                    conn.execute(
                        "UPDATE batch_runs SET status='failed',error='node_shutdown',"
                        "successes=0 WHERE batch_id=%s",
                        (b,),
                    )
        census = batch(conn, "census", census_at, minutes=15)
        batch_run(
            conn,
            census,
            alpha,
            census_at + timedelta(minutes=15),
            requests=5000,
            successes=0,
            stop_reason="no_success",
        )
        # It ran from 18:35 to 19:02, over the (down) pulses of 19:00.
        conn.execute(
            "UPDATE batch_runs SET started_at=%s,finished_at=%s WHERE batch_id=%s",
            (census_at, slots[42] + timedelta(minutes=2), census),
        )
        # A healthy census over the pulses of slot 10.
        healthy = batch(conn, "census", slots[10] - timedelta(minutes=5))
        batch_run(
            conn,
            healthy,
            alpha,
            slots[10] + timedelta(seconds=90),
            requests=5000,
            successes=4900,
            stop_reason="unique_ratio",
        )
        # The two pulses the orchestrator still has running on alpha's node.
        running = {}
        for i in (43, 44):
            running[i] = batch(conn, "pulse", slots[i], status="running")
            conn.execute(
                "INSERT INTO batch_runs(batch_id,product_id,node_id,status,started_at) "
                "VALUES (%s,%s,%s,'running',%s)",
                (running[i], alpha, node["node_id"], slots[i]),
            )
            conn.execute(
                "UPDATE test_batches SET finished_at=NULL WHERE batch_id=%s", (running[i],)
            )
    db = Database(database)
    try:
        # The tape runs every minute; replay it slot by slot, finishing 43 and 44 on time.
        for i, at in enumerate(slots):
            if i in running:
                summary = {
                    "status": "completed",
                    "completed": 100,
                    "successes": 100,
                    "stop_reason": "requests",
                }
                await nodework.finish_run(
                    db,
                    {"node_id": node["node_id"]},
                    running[i],
                    summary,
                    None,
                    at + timedelta(minutes=1),
                )
            with connect(database) as conn:
                detect(conn, at + timedelta(minutes=4))
    finally:
        await db.close()
    with connect(database) as conn:
        outages = conn.execute(
            "SELECT source,started_at,ended_at FROM outages WHERE product_id=%s", (alpha,)
        ).fetchall()
        retries = conn.execute(
            "SELECT retry_of,scheduled_for FROM test_batches WHERE retry_of IS NOT NULL"
        ).fetchall()
        tape = [
            r[0]
            for r in conn.execute(
                "SELECT kind FROM test_events WHERE kind IN ('down','recovered','census_retry') "
                "ORDER BY event_id"
            )
        ]
    # Outage recorded: from the last healthy pulse (17:31) to the first healthy one (19:30).
    assert outages == [("pulse", slots[39] + timedelta(minutes=1), slots[43])]
    # The rerun is planned as the pulse finishes; the tape marks the recovery a minute later.
    assert tape == ["down", "down", "census_retry", "recovered", "recovered"]
    # The census the outage spoiled ran again once, when the 19:30 pulse got through.
    assert retries == [(census, slots[43] + timedelta(minutes=1))]

    store.evidence(
        database,
        [
            {
                "provider": "alpha",
                "product": "residential",
                "cohort": "*",
                "metric": metric,
                "value": value,
                "period_start": (end - timedelta(days=2)).isoformat(),
                "period_end": (end + timedelta(days=60)).isoformat(),
                "valid_until": (end + timedelta(days=60)).isoformat(),
                "source": "fixture price list",
            }
            for metric, value in (("price_per_gb_lowest", 0.75), ("price_per_gb_entry", 1.0))
        ],
    )
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO load_references(day,region_id,country_code,basis) SELECT %s,region_id,"
            "'DE','default' FROM countries WHERE country_code='DE'",
            (slots[47].date(),),
        )
    payload = calculate(database, policy_file(tmp_path), as_of=end)
    (alpha_product,) = [p for p in payload["products"] if p["provider"] == "alpha"]
    w = alpha_product["windows"]["24h"]
    # Uptime over the measured time: 2 h minus a minute down in the one country tested, over its
    # measured span, from the first pulse's start (slots[0], the window start) to the last
    # pulse's end (a minute after slots[47]): 24 h less 29 min.
    measured = (slots[47] + timedelta(minutes=1) - slots[0]).total_seconds()
    assert measured == 86400 - 29 * 60
    assert w["uptime"]["value"] == pytest.approx(1 - 7140 / measured)
    assert round(100 * w["uptime"]["value"], 1) == 91.6
    assert w["uptime"]["downtime_s"] == 7140
    # Down slots are filled, never gaps: coverage stays at 100%.
    http = w["targets"]["generic_http"]["metrics"]
    assert http["coverage_slots"] == {"due": 48, "filled": 48, "down": 3}
    assert w["targets"]["generic_https"]["metrics"]["coverage_slots"] == {
        "due": 47,
        "filled": 47,
        "down": 2,
    }
    # Pulses during a census still count for uptime and slots.
    assert http["uptime"] == pytest.approx(45 / 48)
    # Time-weighted success: slot 10 had only pulses during a census and does not vote; the 3
    # down slots vote 0 (19:00 too, although a census was running): 44 of 47.
    assert w["metrics"]["success_rate"] == pytest.approx(44 / 47)
    assert w["metrics"]["time_slots"]["minutes"] == 30
    assert w["metrics"]["time_slots"]["skipped_during_census"] == 1
    assert w["metrics"]["time_slots"]["down"] == 3
    assert w["targets"]["generic_https"]["metrics"]["success_rate"] == pytest.approx(44 / 46)
    overlap = w["census_overlap"]
    assert overlap["census_batches"] == sorted([healthy, census])
    assert overlap["pulses"]["during_census"]["runs"] == 4
    assert overlap["pulses"]["during_census"]["requests"] == 400
    assert overlap["pulses"]["during_census"]["successes"] == 200
    assert overlap["pulses"]["clean"]["runs"] == 91
    assert overlap["targets"]["generic_https"]["during_census"]["runs"] == 2
    assert overlap["load"] == {"clean": 1, "during_census": 0}
    score = w["scores"]["balanced"]
    assert score["state"] == "ready" and score["score"] is not None
    assert all(c["state"] == "ready" for c in score["components"].values())
    assert score["components"]["uptime"]["inputs"]["value"] == pytest.approx(
        100 * (1 - 7140 / measured)
    )
    assert alpha_product["pricing"]["lowest_per_gb"] == 0.75
    assert alpha_product["pricing"]["entry_per_gb"] == 1.0
    # fra1's reference country, as the day's choice recorded it.
    (region,) = w["latency"]["regions"]
    assert region["region"] == "fra1" and region["reference"]["country"] == "DE"
    assert region["chosen"] == [
        {
            "day": slots[47].date().isoformat(),
            "country": "DE",
            "basis": "default",
            "p50_ms": None,
            "successes": None,
            "candidates": 0,
        }
    ]
    assert region["factors"]["slots"] == 1
    # Latency is generic_http's alone: the load slot gives its light-load latency its own
    # factor. generic_https has only a display-only light-load latency of its own pulses
    # (HTTPS-LATENCY-1), never one under load.
    assert w["metrics"]["under_load"]["own_factor"] == 1
    assert w["latency"]["target"] == "generic_http"
    https = w["targets"]["generic_https"]["metrics"]
    assert https["latency_ms"]["p50"] is not None and https["latency_reason"] is None
    assert https["latency_under_load_ms"]["p50"] is None
    assert json.dumps(payload)
