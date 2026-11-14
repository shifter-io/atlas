"""PULSE-NODE-RETIRE-1: the offline job and on-demand pulse nodes (orchestrator/node_offline.py,
pulse_hour.retire_nodes). An hour's nodes removed on purpose raise no alert; nodes lost before
their hour ends share one alert. The database is the test's own."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from orchestrator_cases import bearer, prepare, query, set_setting

from atlas.orchestrator import nodework
from atlas.orchestrator.nodes import issue_token
from atlas.orchestrator.pulse_hour import Armed, open_hour, retire_nodes
from atlas.storage import connect

pytest_plugins = ["test_storage", "orchestrator_cases"]

HOUR = datetime(2026, 10, 1, 12, tzinfo=UTC)


async def hour_of(database, db, count):
    """`count` nodes (p000/res … in fra1) armed for HOUR, online, with their pulses opened."""
    prepare(database, ("DE",))
    with connect(database) as conn:
        conn.execute("UPDATE targets SET pulse_interval_minutes=60 WHERE engine='http'")
        conn.execute(
            "INSERT INTO providers SELECT 'p'||lpad(i::text,3,'0') FROM generate_series(1,%s) i",
            (count,),
        )
        conn.execute("INSERT INTO products(provider,name) SELECT id,'res' FROM providers")
        conn.execute(
            "INSERT INTO nodes(region_id,provider,product,token_hash) SELECT r.region_id,"
            "p.id,'res','t'||p.id FROM providers p, runner_regions r WHERE r.slug='fra1'"
        )
    await set_setting(db, "pulse.on_demand", True)
    cells = [Armed(f"p{i:03}", "res", "fra1", ("DE",)) for i in range(1, count + 1)]
    opened = await open_hour(db, HOUR, cells, HOUR + timedelta(minutes=1))
    assert opened["runs"] == 2 * count
    beat(database, HOUR + timedelta(minutes=2))
    return cells


def beat(database, at, providers=None):
    """The nodes' last heartbeat (all, or those providers') was at `at`."""
    with connect(database) as conn:
        conn.execute(
            "UPDATE nodes SET status='online',last_heartbeat=%s "
            "WHERE %s::text[] IS NULL OR provider=ANY(%s)",
            (at, providers, providers),
        )


def close_hour(database):
    with connect(database) as conn:
        conn.execute("UPDATE test_batches SET status='done' WHERE kind='pulse'")


async def test_an_hour_of_144_nodes_retired_at_its_end_raises_no_alert(database, db, client):
    cells = await hour_of(database, db, 144)
    close_hour(database)
    assert await retire_nodes(db, cells) == 144
    assert await retire_nodes(db, cells) == 0  # already retired
    # A heartbeat before the droplet is gone keeps the node retired.
    token = issue_token(database, provider="p001", product="res", region="fra1")["token"]
    with connect(database) as conn:
        conn.execute("UPDATE nodes SET status='retired' WHERE provider='p001'")
    assert (await client.post("/v1/heartbeat", json={}, headers=bearer(token))).status == 200
    later = HOUR + timedelta(hours=2)
    assert await nodework.mark_offline(db, later) == {"offline": [], "retired": []}
    assert query(database, "SELECT DISTINCT status FROM nodes") == [("retired",)]
    assert query(database, "SELECT 1 FROM alerts") == []
    assert query(database, "SELECT 1 FROM test_events") == []
    # The next hour's token re-arms a retired node: offline until its first heartbeat.
    issue_token(database, provider="p002", product="res", region="fra1")
    assert query(database, "SELECT status FROM nodes WHERE provider='p002'") == [("offline",)]


async def test_three_nodes_lost_mid_hour_raise_one_grouped_alert(database, db):
    await hour_of(database, db, 5)
    beat(database, HOUR + timedelta(minutes=20), ["p001", "p004"])
    now = HOUR + timedelta(minutes=10)
    gone = await nodework.mark_offline(db, now)
    assert len(gone["offline"]) == 3 and gone["retired"] == []
    alerts = query(database, "SELECT kind,severity,message,dedupe_key,occurrences FROM alerts")
    assert alerts == [
        (
            "pulse_nodes_lost",
            "major",
            "pulse hour 2026-10-01 12:00 UTC: 3 of 5 nodes lost before the end: "
            "p002/res fra1, p003/res fra1, p005/res fra1",
            "pulse_nodes_lost:20261001T12",
            1,
        )
    ]
    events = query(database, "SELECT kind,signal FROM test_events")
    assert events == [("node_offline", -1)] * 3
    # One more lost later in the hour updates the same alert: Slack posts it once.
    gone = await nodework.mark_offline(db, HOUR + timedelta(minutes=30))
    assert len(gone["offline"]) == 2
    message, occurrences = query(database, "SELECT message,occurrences FROM alerts")[0]
    assert message.startswith("pulse hour 2026-10-01 12:00 UTC: 5 of 5 nodes lost")
    assert occurrences == 2 and len(query(database, "SELECT 1 FROM alerts")) == 1


async def test_stale_nodes_of_a_closed_hour_are_retired_without_an_alert(database, db):
    await hour_of(database, db, 3)
    beat(database, HOUR + timedelta(minutes=57, seconds=30), ["p003"])
    close_hour(database)
    gone = await nodework.mark_offline(db, HOUR + timedelta(minutes=58))
    assert gone["offline"] == [] and len(gone["retired"]) == 2
    assert query(database, "SELECT 1 FROM alerts") == []
    # Silent past its hour: a node seen after the hour, with no run of the new hour yet, is
    # not one of the closed hour's and keeps its own alert.
    beat(database, HOUR + timedelta(hours=1, minutes=1), ["p003"])
    gone = await nodework.mark_offline(db, HOUR + timedelta(hours=1, minutes=5))
    assert len(gone["offline"]) == 1
    assert query(database, "SELECT kind FROM alerts") == [("node_offline",)]


async def test_without_on_demand_every_node_keeps_its_own_alert(database, db):
    await hour_of(database, db, 2)
    await set_setting(db, "pulse.on_demand", False)
    gone = await nodework.mark_offline(db, HOUR + timedelta(minutes=10))
    assert len(gone["offline"]) == 2 and gone["retired"] == []
    assert query(database, "SELECT kind FROM alerts") == [("node_offline",)] * 2

