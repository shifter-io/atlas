"""Orchestrator planning: census and pulse slots, load waves, references and census reruns."""

from datetime import UTC, datetime, time, timedelta

import pytest
from helpers import FakeTransport
from orchestrator_cases import FRA1, LON1, NOW, bearer, prepare, product_id, query, set_setting
from test_storage import measurement

from proxy_index.orchestrator import batches, nodework, references
from proxy_index.orchestrator.nodes import issue_token
from proxy_index.storage import connect, ingest

pytest_plugins = ["test_storage", "orchestrator_cases"]


# --- planning -------------------------------------------------------------------------------


async def test_census_plan_due_slots_region_routing_and_no_node(database, db):
    prepare(database, ("DE", "FR", "AT"), DE=time(12), FR=time(12), AT=time(12, 30))
    alpha = issue_token(database, provider="alpha", product="res", region="fra1")
    beta = issue_token(database, provider="beta", product="res", region="prod-lon1-beta")
    assert beta["region"] == "lon1"

    result = await batches.census_plan(db, NOW)
    assert result == {"created": 2, "skipped": 0, "deferred": 0, "no_node": 2}
    rows = query(
        database,
        "SELECT batch_id,country_code,region_id,target_id,scheduled_for,start_at,status "
        "FROM test_batches ORDER BY country_code",
    )
    slot = datetime(2026, 10, 1, 12, tzinfo=UTC)
    # Only the census target, and each country is tested from its own region.
    assert [r[1:] for r in rows] == [
        ("DE", FRA1, "generic_http", slot, slot + timedelta(seconds=30), "scheduled"),
        ("FR", LON1, "generic_http", slot, slot + timedelta(seconds=30), "scheduled"),
    ]
    runs = query(
        database,
        "SELECT b.country_code,p.provider,r.node_id,r.status,r.stop_reason,r.error "
        "FROM batch_runs r JOIN test_batches b USING(batch_id) JOIN products p USING(product_id) "
        "ORDER BY 1,2",
    )
    assert runs == [
        ("DE", "alpha", alpha["node_id"], "queued", None, None),
        ("DE", "beta", None, "failed", "no_node", "no_node"),
        ("FR", "alpha", None, "failed", "no_node", "no_node"),
        ("FR", "beta", beta["node_id"], "queued", None, None),
    ]
    alerts = query(database, "SELECT dedupe_key,occurrences FROM alerts ORDER BY 1")
    assert alerts == sorted(
        [
            (f"no_node:{product_id(database, 'alpha')}:{LON1}", 1),
            (f"no_node:{product_id(database, 'beta')}:{FRA1}", 1),
        ]
    )
    events = query(database, "SELECT kind,region_id,signal,data->>'kind' FROM test_events")
    assert sorted(events) == sorted(
        [("no_node", FRA1, -1, "census"), ("no_node", LON1, -1, "census")]
    )

    # Idempotent: the same tick again creates nothing and writes no second event.
    assert (await batches.census_plan(db, NOW + timedelta(seconds=20)))["created"] == 0
    assert len(query(database, "SELECT 1 FROM test_batches")) == 2
    assert len(query(database, "SELECT 1 FROM test_events")) == 2

    # Half an hour on, Austria's slot is due and nothing else.
    later = await batches.census_plan(db, NOW + timedelta(minutes=30))
    assert later["created"] == 1
    newer = query(
        database, "SELECT country_code FROM test_batches WHERE batch_id>%s", (rows[-1][0],)
    )
    assert newer == [("AT",)]
    # Beta still has no fra1 node: the open alert counts it again, the tape stays quiet.
    assert query(
        database, "SELECT occurrences FROM alerts WHERE dedupe_key LIKE %s", (f"no_node:%:{FRA1}",)
    ) == [(2,)]
    assert len(query(database, "SELECT 1 FROM test_events")) == 2


async def test_census_plan_wraps_midnight_and_skips_without_nodes(database, db):
    prepare(database, ("DE",), DE=time(0, 0, 20))
    evening = datetime(2026, 10, 1, 23, 59, 50, tzinfo=UTC)
    assert (await batches.census_plan(db, evening))["reason"] == "no registered nodes"
    issue_token(database, provider="alpha", product="res", region="fra1")
    assert (await batches.census_plan(db, evening))["created"] == 1
    assert query(database, "SELECT scheduled_for FROM test_batches") == [
        (datetime(2026, 10, 2, 0, 0, 20, tzinfo=UTC),)
    ]


async def test_census_overlap_is_skipped(database, db):
    prepare(database, ("DE",), DE=time(12))
    issue_token(database, provider="alpha", product="res", region="fra1")
    await batches.census_plan(db, NOW)
    # The operator moves the slot while the first census is still open.
    with connect(database) as conn:
        conn.execute("UPDATE countries SET census_time='12:10' WHERE country_code='DE'")
    # Before the new start_at (12:10:30) the first census may still finish: deferred.
    assert await batches.census_plan(db, NOW + timedelta(minutes=10)) == {
        "created": 0,
        "skipped": 0,
        "deferred": 1,
        "no_node": 0,
    }
    assert len(query(database, "SELECT 1 FROM test_batches")) == 1
    # Still open at start_at: skipped.
    assert await batches.census_plan(db, NOW + timedelta(minutes=11)) == {
        "created": 0,
        "skipped": 1,
        "deferred": 0,
        "no_node": 0,
    }
    rows = query(database, "SELECT status,finished_at IS NOT NULL FROM test_batches ORDER BY 1")
    assert rows == [("scheduled", False), ("skipped", True)]
    events = query(database, "SELECT kind,country_code,data->>'reason' FROM test_events")
    assert events == [("census_skipped", "DE", "previous census still scheduled")]
    skipped = query(database, "SELECT batch_id FROM test_batches WHERE status='skipped'")[0][0]
    assert query(database, "SELECT 1 FROM batch_runs WHERE batch_id=%s", (skipped,)) == []


async def test_pulse_plan_intervals_new_targets_and_idempotence(database, db):
    prepare(database, ("DE", "AT"))
    issue_token(database, provider="alpha", product="res", region="fra1")
    with connect(database) as conn:
        conn.execute("UPDATE targets SET pulse_interval_minutes=60 WHERE target_id='generic_https'")

    def slots():
        return query(
            database,
            "SELECT target_id,scheduled_for,count(*),min(start_at),max(start_at) "
            "FROM test_batches WHERE kind='pulse' GROUP BY 1,2 ORDER BY 2,1",
        )

    at = datetime(2026, 10, 1, 12, 31, tzinfo=UTC)

    def at15(minutes, seconds=0):  # at + pulse.lead_seconds (15 s) + the given time
        return at + timedelta(minutes=minutes, seconds=15 + seconds)

    first = await batches.pulse_plan(db, at)
    # One load wave, in fra1's reference country (its default, DE: no history yet).
    assert first == {
        "targets": ["generic_http", "generic_https"],
        "created": 4,
        "load": 1,
        "no_node": 0,
    }
    # Each region's countries are spread over the slot, one at a time: generic_http's slot
    # opened on time (AT, DE 15 minutes apart, none before now + lead); generic_https's half an
    # hour late, so over the rest of it, half a step after generic_http's.
    assert slots() == [
        ("generic_https", datetime(2026, 10, 1, 12, tzinfo=UTC), 2, at15(7, 15), at15(21, 45)),
        ("generic_http", datetime(2026, 10, 1, 12, 30, tzinfo=UTC), 2, at15(0), at15(14)),
    ]
    assert (await batches.pulse_plan(db, at))["created"] == 0
    assert (await batches.pulse_plan(db, at + timedelta(minutes=14)))["created"] == 0
    top = datetime(2026, 10, 1, 13, tzinfo=UTC)
    assert (await batches.pulse_plan(db, top))["targets"] == ["generic_http", "generic_https"]
    # A target added to the table joins at its own interval with no restart.
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO targets(target_id,url,pulse_interval_minutes,pulse_requests) "
            "VALUES ('probe_x','http://example.test/',15,10)"
        )
    nxt = await batches.pulse_plan(db, top + timedelta(minutes=16))
    assert nxt["targets"] == ["probe_x"]
    assert (await batches.pulse_plan(db, top + timedelta(minutes=31)))["targets"] == [
        "probe_x",
        "generic_http",
    ]
    assert len(slots()) == 7
    runs = query(database, "SELECT DISTINCT status FROM batch_runs")
    assert runs == [("queued",)]


async def test_pulse_slots_add_a_load_wave_per_region(database, db, client):
    """Every pulse slot of the census target opens one 'load' batch per runner region, in the
    region's reference country of the day (here the defaults: no history yet), with the same
    scheduled_for, load.delay_seconds after the pulses; its runs fire census_concurrency
    requests at census concurrency, counted like a pulse, and send only the country (the
    node's proxy factory)."""
    prepare(database, ("DE", "AT", "GB"))
    a = issue_token(database, provider="alpha", product="res", region="fra1")
    # Inside the slot's grace, so the slot is spread whole whatever the wall clock says.
    at = datetime(2026, 10, 1, 12, 0, 30, tzinfo=UTC)
    result = await batches.pulse_plan(db, at)
    assert (result["created"], result["load"]) == (6, 2)
    assert query(
        database,
        "SELECT r.slug,l.country_code,l.basis,l.p50_ms,l.candidates FROM load_references l "
        "JOIN runner_regions r USING(region_id) ORDER BY 1",
    ) == [("fra1", "DE", "default", None, 0), ("lon1", "GB", "default", None, 0)]
    loads = query(
        database,
        "SELECT l.country_code,l.target_id,l.scheduled_for=p.scheduled_for,"
        "l.start_at-(SELECT min(f.start_at) FROM test_batches f WHERE f.kind='pulse' "
        "AND f.region_id=l.region_id AND f.target_id=l.target_id) "
        "FROM test_batches l JOIN test_batches p ON p.kind='pulse' "
        "AND p.country_code=l.country_code AND p.target_id=l.target_id WHERE l.kind='load' "
        "ORDER BY 1",
    )
    # Same slot; they start a minute after the region's first pulse so it stays light.
    assert loads == [
        ("DE", "generic_http", True, timedelta(seconds=60)),
        ("GB", "generic_http", True, timedelta(seconds=60)),
    ]
    assert (await batches.pulse_plan(db, at))["load"] == 0  # idempotent
    work = await (await client.get("/v1/work", headers=bearer(a["token"]))).json()
    (load,) = [w for w in work["assignments"] if w["kind"] == "load"]  # GB: no node in lon1
    assert (load["country_code"], load["target_id"]) == ("DE", "generic_http")
    assert (load["concurrency"], load["requests"], load["stop_rule"]) == (1000, 1000, "count")
    assert load["min_sample"] is None and load["stall_seconds"] is None
    pulse = next(w for w in work["assignments"] if w["kind"] == "pulse")
    assert (pulse["concurrency"], pulse["requests"]) == (25, 100)
    # load.enabled switches the wave off.
    await set_setting(db, "load.enabled", False)
    later = await batches.pulse_plan(db, at + timedelta(hours=1))
    assert later["load"] == 0 and later["created"] in (0, 6)
    with pytest.raises(ValueError, match="two-letter"):
        references._countries({"fra1": "Germany"}, "load.reference_defaults")


class SlowTransport(FakeTransport):
    async def execute(self, target, *, context=None):
        from proxy_index.models import Outcome

        return Outcome(30.2, ip="9.9.9.9", status=200, response_body_bytes=8)


async def pulse_history(database, tmp_path, country, at, transport):
    """An ingested census-target pulse of alpha/res in `country`, scheduled at `at`."""
    directory = await measurement(
        tmp_path,
        provider="alpha",
        product="res",
        requested_country=country.lower(),
        transport_factory=transport,
    )
    with connect(database) as conn:
        batch_id = conn.execute(
            "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
            "start_at,status) SELECT 'pulse',%s,region_id,'generic_http',%s,%s,'done' "
            "FROM countries WHERE country_code=%s RETURNING batch_id",
            (country, at, at, country),
        ).fetchone()[0]
    key = ingest(directory, database, batch_id=batch_id)["run_key"]
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO batch_runs(batch_id,product_id,status,started_at,finished_at,run_key,"
            "requests,successes) SELECT %s,product_id,'done',%s,%s,%s,3,3 FROM products "
            "WHERE provider='alpha' AND name='res'",
            (batch_id, at, at + timedelta(minutes=1), key),
        )
    return key


async def test_the_reference_is_the_fastest_clean_country_of_the_region(database, db, tmp_path):
    prepare(database, ("DE", "AT"))
    issue_token(database, provider="alpha", product="res", region="fra1")
    await set_setting(db, "load.reference_min_successes", 1)
    yesterday = datetime.now(UTC) - timedelta(days=1)
    await pulse_history(database, tmp_path, "DE", yesterday, SlowTransport)
    fast = await pulse_history(database, tmp_path, "AT", yesterday, FakeTransport)
    # The runs ran just now (scheduled yesterday): choose for tomorrow, whose trailing week
    # holds them all.
    today = datetime.now(UTC) + timedelta(days=1)
    async with db.transaction() as conn:
        (choice,) = await batches.load_references(conn, today)
    assert (choice["country_code"], choice["basis"], choice["p50_ms"]) == ("AT", "fastest", 11)
    assert (choice["successes"], choice["candidates"]) == (3, 2)
    # Chosen once a day: a new override waits for tomorrow.
    await set_setting(db, "load.reference_overrides", {"fra1": "de"})
    async with db.transaction() as conn:
        assert (await batches.load_references(conn, today))[0]["country_code"] == "AT"
        tomorrow = await batches.load_references(conn, today + timedelta(days=1))
    assert (tomorrow[0]["country_code"], tomorrow[0]["basis"]) == ("DE", "override")
    # A pulse that ran during a census of the same product (another country) never counts.
    (run,) = query(
        database, "SELECT started_at,finished_at FROM test_runs WHERE run_key=%s", (fast,)
    )
    with connect(database) as conn:
        census = conn.execute(
            "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
            "start_at,status) SELECT 'census','GB',region_id,'generic_http',%s,%s,'done' "
            "FROM countries WHERE country_code='GB' RETURNING batch_id",
            (yesterday, yesterday),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO batch_runs(batch_id,product_id,status,started_at,finished_at) "
            "SELECT %s,product_id,'done',%s,%s FROM products WHERE provider='alpha'",
            (census, run[0], run[1]),
        )
    day_start = today.replace(hour=0, minute=0, second=0, microsecond=0)
    async with db.connection() as conn:
        stats = await references._reference_latency(conn, day_start, 7)
    assert stats == {"DE": {"p50": 31, "successes": 3}}


async def finish_pulse(db, node, batch_id, successes, now):
    summary = {
        "status": "completed",
        "completed": 100,
        "successes": successes,
        "stop_reason": "requests",
    }
    return await nodework.finish_run(db, node, batch_id, summary, None, now)


@pytest.mark.parametrize("spoiled", ["no_success", "stalled", "failed"])
async def test_a_spoiled_census_reruns_once_a_day_when_a_pulse_gets_through(database, db, spoiled):
    """A census that ended because the provider was down is rerun for that product alone at the
    first pulse that sees it up again in the country, once per UTC day. A census our side
    failed (here the runner failed) is not the provider's and is not rerun."""
    prepare(database, ("DE",))
    a = issue_token(database, provider="alpha", product="res", region="fra1")
    b = issue_token(database, provider="beta", product="res", region="fra1")
    node = {"node_id": a["node_id"]}
    alpha, beta = product_id(database, "alpha"), product_id(database, "beta")
    day = datetime.now(UTC).replace(hour=3, minute=0, second=0, microsecond=0)
    with connect(database) as conn:
        census = conn.execute(
            "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
            "start_at,status,finished_at) VALUES ('census','DE',%s,'generic_http',%s,%s,'done',%s) "
            "RETURNING batch_id",
            (FRA1, day, day, day + timedelta(minutes=20)),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO batch_runs(batch_id,product_id,node_id,status,stop_reason,error,"
            "successes,finished_at) VALUES (%s,%s,%s,%s,%s,%s,0,%s)",
            (
                census,
                alpha,
                a["node_id"],
                "failed" if spoiled == "failed" else "done",
                None if spoiled == "failed" else spoiled,
                "run failed" if spoiled == "failed" else None,
                day + timedelta(minutes=20),
            ),
        )
        conn.execute(
            "INSERT INTO batch_runs(batch_id,product_id,node_id,status,stop_reason,successes,"
            "finished_at) VALUES (%s,%s,%s,'done','unique_ratio',900,%s)",
            (census, beta, b["node_id"], day + timedelta(minutes=20)),
        )
    pulses = []
    for i in range(3):
        at = day + timedelta(hours=1, minutes=30 * i)
        await batches.pulse_plan(db, at)
        pulses.append(
            query(
                database,
                "SELECT batch_id FROM test_batches WHERE kind='pulse' AND target_id='generic_http' "
                "AND scheduled_for<=%s ORDER BY scheduled_for DESC LIMIT 1",
                (at,),
            )[0][0]
        )
        await nodework.assignments(db, node, at + timedelta(minutes=1))
    # Still down: no rerun. Then up: one rerun for alpha alone. Up again: nothing more.
    await finish_pulse(db, node, pulses[0], 0, day + timedelta(hours=1, minutes=1))
    assert query(database, "SELECT count(*) FROM test_batches WHERE retry_of IS NOT NULL") == [(0,)]
    up = day + timedelta(hours=1, minutes=31)
    await finish_pulse(db, node, pulses[1], 97, up)
    if spoiled == "failed":
        assert query(database, "SELECT count(*) FROM test_batches WHERE retry_of IS NOT NULL") == [
            (0,)
        ]
        return
    retries = query(
        database,
        "SELECT b.batch_id,b.retry_of,b.country_code,b.target_id,b.start_at-b.scheduled_for,"
        "array_agg(br.product_id),array_agg(br.status) FROM test_batches b JOIN batch_runs br "
        "USING(batch_id) WHERE b.retry_of IS NOT NULL GROUP BY 1",
    )
    assert [r[1:] for r in retries] == [
        (census, "DE", "generic_http", timedelta(seconds=5), [alpha], ["queued"])
    ]
    events = query(
        database, "SELECT kind,product_id,data FROM test_events WHERE kind='census_retry'"
    )
    assert [(e[0], e[1], e[2]["retry_of"]) for e in events] == [("census_retry", alpha, census)]
    # The rerun is spoiled too; another pulse gets through the same day: still only one rerun.
    with connect(database) as conn:
        conn.execute(
            "UPDATE batch_runs SET status='done',stop_reason='no_success',finished_at=%s "
            "WHERE batch_id=%s",
            (up + timedelta(minutes=12), retries[0][0]),
        )
        conn.execute("UPDATE test_batches SET status='done' WHERE batch_id=%s", (retries[0][0],))
    await finish_pulse(db, node, pulses[2], 100, day + timedelta(hours=2, minutes=1))
    assert query(database, "SELECT count(*) FROM test_batches WHERE retry_of IS NOT NULL") == [(1,)]
