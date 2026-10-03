"""A provider that is down for two hours is data, not a gap.

24-hour window, 30-minute pulses on both targets in DE, a load wave in the last slot. The
provider is down for the pulses of 18:00, 18:30 and 19:00 (one of them a run that failed), and a
census during the outage gets no success. The score stays computed, uptime is about 91.7% (the
outage runs from the last healthy pulse to the first healthy one, two hours), the outage is
recorded, and the census is rerun once, at the first pulse that gets through.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_census_pulse import batch, batch_run, product
from test_storage import enrichment, measurement

from milf_framework.calculated import store
from milf_framework.calculated.calculator import calculate
from milf_framework.detect import detect
from milf_framework.orchestrator import batches
from milf_framework.orchestrator.db import Database
from milf_framework.orchestrator.nodes import issue_token
from milf_framework.storage import connect, ingest, seed

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
            for kind, target in (("pulse", "generic_http"), ("pulse", "generic_https"),
                                 ("load", "generic_http"))
        }
    for (kind, target), batch_id in last.items():
        directory = await measurement(
            tmp_path, provider="alpha", target=target, requested_country="de", requests=40,
            concurrency=1000 if kind == "load" else 25,
        )
        if target == "generic_http":
            enrichment(directory, attributes=GERMANY)
        key = ingest(directory, database, batch_id=batch_id)["run_key"]
        with connect(database) as conn:
            batch_run(conn, batch_id, alpha, slots[47] + timedelta(minutes=1), run_key=key,
                      requests=40, successes=40)
    census_at = slots[41] + timedelta(minutes=5)
    with connect(database) as conn:
        for i, at in enumerate(slots[:47]):
            for target in TARGETS:
                if i in (43, 44) and target == "generic_http":
                    continue  # these finish through the orchestrator below
                b = batch(conn, "pulse", at, target=target)
                batch_run(conn, b, alpha, at + timedelta(minutes=1),
                          successes=0 if i in DOWN else 100)
                if i == 41 and target == "generic_https":
                    # The provider broke the run outright: a failed run is down too.
                    conn.execute(
                        "UPDATE batch_runs SET status='failed',error='run failed' "
                        "WHERE batch_id=%s",
                        (b,),
                    )
        census = batch(conn, "census", census_at, minutes=15)
        batch_run(conn, census, alpha, census_at + timedelta(minutes=15), requests=5000,
                  successes=0, stop_reason="no_success")
        # The two pulses the orchestrator still has running on alpha's node.
        running = {}
        for i in (43, 44):
            running[i] = batch(conn, "pulse", slots[i], status="running")
            conn.execute(
                "INSERT INTO batch_runs(batch_id,product_id,node_id,status,started_at) "
                "VALUES (%s,%s,%s,'running',%s)",
                (running[i], alpha, node["node_id"], slots[i]),
            )
            conn.execute("UPDATE test_batches SET finished_at=NULL WHERE batch_id=%s",
                         (running[i],))
    db = Database(database)
    try:
        # The tape runs every minute; replay it slot by slot, finishing 43 and 44 on time.
        for i, at in enumerate(slots):
            if i in running:
                summary = {"status": "completed", "completed": 100, "successes": 100,
                           "stop_reason": "requests"}
                await batches.finish_run(db, {"node_id": node["node_id"]}, running[i],
                                         summary, None, at + timedelta(minutes=1))
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
        tape = [r[0] for r in conn.execute(
            "SELECT kind FROM test_events WHERE kind IN ('down','recovered','census_retry') "
            "ORDER BY event_id"
        )]
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
    payload = calculate(database, policy_file(tmp_path), as_of=end)
    (alpha_product,) = [p for p in payload["products"] if p["provider"] == "alpha"]
    w = alpha_product["windows"]["24h"]
    # Uptime: 2 h minus a minute down out of 24 h in the one country tested.
    assert w["uptime"]["value"] == pytest.approx(1 - 7140 / 86400)
    assert round(100 * w["uptime"]["value"], 1) == 91.7
    assert w["uptime"]["downtime_s"] == 7140
    # Down slots are filled, never gaps: coverage stays at 100%.
    http = w["targets"]["generic_http"]["metrics"]
    assert http["coverage_slots"] == {"due": 48, "filled": 48, "down": 0}
    assert w["targets"]["generic_https"]["metrics"]["coverage_slots"] == {
        "due": 48, "filled": 48, "down": 1,
    }
    assert http["uptime"] == pytest.approx(45 / 48)
    # Time-weighted success: 45 of 48 slots succeeded, the 3 down slots vote 0.
    assert w["metrics"]["success_rate"] == pytest.approx(45 / 48)
    score = w["scores"]["balanced"]
    assert score["state"] == "ready" and score["score"] is not None
    assert all(c["state"] == "ready" for c in score["components"].values())
    assert score["components"]["uptime"]["inputs"]["value"] == pytest.approx(
        100 * (1 - 7140 / 86400)
    )
    assert alpha_product["pricing"]["lowest_per_gb"] == 0.75
    assert alpha_product["pricing"]["entry_per_gb"] == 1.0
    assert w["latency"]["reference"]["country"] == "DE"
    assert w["metrics"]["under_load"]["own_factor"] == 1
    assert json.dumps(payload)
