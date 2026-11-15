"""PULSE-STATE-1 (2/2): a pulse droplet that stops working mid-hour has its remaining pulses moved
within the hour (to a sibling of its network in another region, else to its cell's one
replacement); what cannot move is lost and named. Fake cloud and fake SSH; the database is the
test's own; nothing leaves the machine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from orchestrator_cases import query, set_setting
from pulse_fleet_cases import FakeClock, ids_named, world
from test_pulse_ondemand import FRA1, HOUR, LON1, NOW, hour, ready

from atlas.campaign import pulse_ondemand, pulse_stall
from atlas.orchestrator.pulse_hour import Armed
from atlas.storage import connect

pytest_plugins = ["test_storage", "orchestrator_cases"]

AT = "SELECT {} FROM batch_runs br JOIN test_batches b USING(batch_id) JOIN runner_regions r ON "
FRA1_RUNS = AT.format("br.status,br.error") + "r.region_id=b.region_id WHERE r.slug='fra1'"


class StallClock(FakeClock):
    """fra1's pulses are due at `start` (late in a long chain); its droplet starts them and stops
    at `stop`: no heartbeat, no progress. lon1's droplet beats and its runs wait for the store
    until `stored`. Work moved after the stop is run by the droplet it went to at the next poll."""

    def __init__(self, database, start, stop, stored):
        super().__init__()
        self.t, self.database = NOW.timestamp(), database
        self.start, self.stop, self.stored, self.shifted = start, stop, stored, False

    async def sleep(self, seconds):
        self.t += seconds
        if seconds != pulse_ondemand.WAIT_POLL_SECONDS:
            return
        now = datetime.fromtimestamp(self.t, UTC)
        with connect(self.database) as conn:
            self._tick(conn, now)

    def _tick(self, conn, now):
        region = "FROM runner_regions r WHERE r.region_id=n.region_id AND r.slug=%s"
        if not self.shifted:
            conn.execute(
                "UPDATE test_batches b SET start_at=%s FROM runner_regions r "
                "WHERE r.region_id=b.region_id AND r.slug='fra1'",
                (self.start,),
            )
            self.shifted = True
        conn.execute(f"UPDATE nodes n SET last_heartbeat=%s {region}", (now, "lon1"))
        if now < self.stop:
            conn.execute(f"UPDATE nodes n SET last_heartbeat=%s {region}", (now, "fra1"))
            conn.execute(
                "UPDATE batch_runs br SET status='running',started_at=b.start_at "
                "FROM test_batches b WHERE b.batch_id=br.batch_id AND b.start_at<=%s "
                "AND br.status='queued'",
                (now,),
            )
        moved = "b.batch_id=br.batch_id AND b.start_at>%s AND b.start_at<=%s"
        conn.execute(
            f"UPDATE batch_runs br SET status='done' FROM test_batches b WHERE {moved}",
            (self.stop, now),
        )
        lon1 = "IN (SELECT n.node_id FROM nodes n JOIN runner_regions r USING(region_id) "
        lon1 += "WHERE r.slug='lon1')"
        status = "done" if now >= self.stored else "uploading"
        conn.execute(
            f"UPDATE batch_runs br SET status=%s FROM test_batches b WHERE b.batch_id=br.batch_id "
            f"AND br.node_id {lon1} AND b.start_at<=%s AND br.status NOT IN ('done','failed')",
            (status, self.stop),
        )


def minutes(n: float) -> datetime:
    return HOUR + timedelta(minutes=n)


async def test_a_node_that_stops_at_minute_12_has_its_pulses_moved_to_a_sibling(
    tmp_path, database, db
):
    await ready(database, db)
    fake = world()
    clock = StallClock(database, minutes(10), minutes(12), minutes(25))
    _, outcome = await hour(tmp_path, fake, db, clock)
    [stalled] = outcome["stalled"]
    assert (stalled["cell"], stalled["to"], stalled["moved"], stalled["lost"]) == (
        "shifter/fra1",
        "shifter/lon1",
        2,
        0,
    )
    assert datetime.fromisoformat(stalled["last_seen"]) == minutes(10)
    assert (outcome["state"], outcome["lost"], outcome["still_open"]) == ("done", [], 0)
    assert query(database, FRA1_RUNS) == [("done", None)] * 2  # run from lon1's droplet
    assert not ids_named(fake, FRA1) and not ids_named(fake, LON1)
    [(kind, severity, message)] = query(database, "SELECT kind,severity,message FROM alerts")
    assert (kind, severity) == ("pulse_nodes_stalled", "minor")  # recorded, nothing paged
    assert message.startswith("pulse hour 2026-10-01 12:00 UTC: shifter/fra1 stopped at 12:10, ")
    assert message.endswith("2 country pulses: 2 moved to shifter/lon1")


async def test_with_no_sibling_up_the_stalled_cell_is_replaced_once(tmp_path, database, db):
    await ready(database, db)
    fake = world()
    clock = StallClock(database, minutes(10), minutes(12), HOUR)  # lon1 stored and gone first
    _, outcome = await hour(tmp_path, fake, db, clock)
    [stalled] = outcome["stalled"]
    assert (stalled["to"], stalled["moved"], stalled["lost"]) == ("replaced", 2, 0)
    assert outcome["state"] == "done" and query(database, FRA1_RUNS) == [("done", None)] * 2
    assert sorted(kind for kind, _ in fake.deleted) == ["droplet"] * 3  # fra1's old one too
    assert not ids_named(fake, FRA1) and outcome["leftover"] == []


async def test_pulses_with_no_time_left_are_lost_and_the_cell_is_named(tmp_path, database, db):
    await ready(database, db)
    await set_setting(db, "pulse.stall_minutes", 4)  # found at about 12:18: past LAST_START
    fake = world()
    clock = StallClock(database, minutes(14), minutes(14.5), minutes(25))
    _, outcome = await hour(tmp_path, fake, db, clock)
    [stalled] = outcome["stalled"]
    assert (stalled["moved"], stalled["lost"]) == (0, 2)
    assert (outcome["state"], outcome["lost"]) == ("partial", ["shifter/fra1"])
    assert query(database, FRA1_RUNS) == [("failed", "node_lost")] * 2
    alerts = dict(query(database, "SELECT kind,message FROM alerts"))
    assert alerts["pulse_cells_lost"].endswith(": 1 cell lost: shifter/fra1")
    assert alerts["pulse_nodes_stalled"].endswith("2 country pulses: 2 lost")


async def test_the_reaper_alerts_of_the_hours_nodes_are_acknowledged_at_its_end(
    tmp_path, database, db
):
    await ready(database, db)
    fra1 = "SELECT node_id FROM nodes n JOIN runner_regions r USING(region_id) WHERE r.slug='fra1'"
    [(node,)] = query(database, fra1)
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO alerts(kind,severity,message,data,dedupe_key) VALUES "
            "('run_stale','minor','node left a run stale','{}',%s)",
            (f"run_stale:{node}",),
        )
    clock = StallClock(database, minutes(10), minutes(12), minutes(25))
    await hour(tmp_path, world(), db, clock)
    acknowledged = "SELECT acknowledged_at IS NOT NULL FROM alerts WHERE kind='run_stale'"
    assert query(database, acknowledged) == [(True,)]


def test_the_helper_is_the_same_networks_droplet_still_up_with_the_fewest_countries():
    cells = {
        "a": Armed("shifter", "residential", "fra1", ("DE",)),
        "b": Armed("shifter", "residential", "lon1", ("GB", "IE")),
        "c": Armed("shifter", "residential", "ams3", ("NL",)),
        "d": Armed("decodo", "residential", "sgp1", ()),
    }
    assert pulse_stall.helper("a", cells, set()) == "c"
    assert pulse_stall.helper("a", cells, {"c"}) == "b"
    assert pulse_stall.helper("a", cells, {"b", "c"}) is None  # never another network's


def test_a_stall_line_says_when_it_stopped_and_where_its_pulses_went():
    entry = {"cell": "webshare/fra1", "last_seen": "2026-11-15T11:10:03+00:00", "to": "replaced"}
    assert pulse_stall._line(entry | {"moved": 80, "lost": 29}) == (
        "webshare/fra1 stopped at 11:10, 109 country pulses: 80 moved to its replacement, 29 lost"
    )
    none = entry | {"last_seen": None, "moved": 0, "lost": 0}
    assert pulse_stall._line(none) == "webshare/fra1 stopped, 0 country pulses: none left"
