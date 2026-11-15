"""PULSE-STATE-1 (2/2): a pulse droplet that stops working mid-hour has its remaining pulses moved
within the hour (to a sibling of its network in another region, else to its cell's one
replacement); what cannot move is lost and named.

The case is the real one: webshare/fra1 (57 countries, 114 runs) in the 11:00 and 12:00 UTC
pulse hours of 2026-11-15. Its droplet kept beating and kept starting its pulses as they came
due, from about :01 to :09, but from a point on none progressed; the reaper later failed them
stale and the rest stayed running after the droplet was removed. Fake cloud and fake SSH; the
database is the test's own; nothing leaves the machine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import test_pulse_ondemand
from orchestrator_cases import query, set_setting
from pulse_fleet_cases import FakeClock, Remote, ids_named, world
from test_pulse_ondemand import FRA1, HOUR, LON1, NOW, hour, ready

from atlas.campaign import pulse_ondemand, pulse_stall
from atlas.campaign.pulse_fleet import load_nodes
from atlas.orchestrator import pulse_moves
from atlas.orchestrator.pulse_hour import Armed
from atlas.storage import connect

pytest_plugins = ["test_storage", "orchestrator_cases"]

# configs/regions.yaml: fra1's 57 countries (114 runs, as webshare/fra1's) and lon1's 31.
FRA1_CODES = (
    "DE AT CH LI CZ SK PL HU SI HR BA RS ME MK AL BG RO MD UA BY RU IT SM VA MC MT CY GR TR AM AZ "
    "GE IQ IL JO LB PS SY EG LY TN DZ AO BW CD CF CG GA GQ LS NA SZ ZA ZM ZW TD CM ST"
).split()
LON1_CODES = (
    "GB GG IM JE IE IS FO FR AD ES GI PT MA EH MR SN GM GW GN SL LR CI GH TG BJ BF ML NE NG CV SH"
).split()
RUNS = (
    "SELECT br.status,br.error,count(*) FROM batch_runs br JOIN test_batches b USING(batch_id) "
    "JOIN runner_regions r ON r.region_id=b.region_id WHERE r.slug='fra1' GROUP BY 1,2 ORDER BY 1"
)
NODE = "(SELECT node_id FROM nodes n JOIN runner_regions r USING(region_id) WHERE r.slug=%s)"
REGION = "(SELECT region_id FROM runner_regions WHERE slug=%s)"
# Started before :05 (back to back from 12:00:15, 12 s a country, https 6 s after): they finish.
BEFORE_STALL = 48


def fra1_runs(database) -> int:
    """The fra1 region's runs (its countries in the database: 58, one more than regions.yaml)."""
    return sum(n for _, _, n in query(database, RUNS))


def at(minutes: float) -> datetime:
    return HOUR + timedelta(minutes=minutes)


@pytest.fixture
async def real(database, db, monkeypatch):
    """The two cells of shifter: fra1 with fra1's 57 countries, lon1 with lon1's 31."""
    await ready(database, db)
    with connect(database) as conn:
        codes = FRA1_CODES + LON1_CODES
        conn.execute("UPDATE countries SET enabled = country_code = ANY(%s)", (codes,))
    monkeypatch.setattr(test_pulse_ondemand, "SCHEDULES", {"shifter": dict.fromkeys(codes, 0)})


class RealClock(FakeClock):
    """webshare/fra1's droplet as seen on 2026-11-15: it beats and starts its pulses when due up
    to :09, but none it starts from `stall` on progresses, and it starts none after :09. Once
    its cell was replaced (attempts 2) the new droplet runs fra1's pulses when due. lon1's
    droplet beats, runs whatever is moved to it when due, and its own uploads wait behind the
    hourly calculation until `stored`."""

    def __init__(self, database, root, stall, stored):
        super().__init__()
        self.t, self.database, self.root = NOW.timestamp(), database, root
        self.stall, self.stored = stall, stored

    async def sleep(self, seconds):
        self.t += seconds
        if seconds != pulse_ondemand.WAIT_POLL_SECONDS:
            return
        now = datetime.fromtimestamp(self.t, UTC)
        ledger = load_nodes(self.root / "hours" / f"{HOUR:%Y%m%dT%H}")
        replaced = FRA1 in ledger and ledger[FRA1].attempts > 1
        with connect(self.database) as conn:
            conn.execute("UPDATE nodes SET last_heartbeat=%s", (now,))
            self._fra1(conn, now, replaced)
            self._lon1(conn, now)

    def _fra1(self, conn, now, replaced):
        due = "FROM test_batches b WHERE b.batch_id=br.batch_id AND br.node_id=" + NODE
        if replaced:
            conn.execute(
                "UPDATE batch_runs br SET status='done',started_at=b.start_at,finished_at=%s "
                f"{due} AND br.status='queued' AND b.start_at<=%s",
                (now, "fra1", now),
            )
            return
        conn.execute(
            f"UPDATE batch_runs br SET status='running',started_at=b.start_at {due} "
            "AND br.status='queued' AND b.start_at<=%s",
            ("fra1", min(now, at(9))),
        )
        conn.execute(
            "UPDATE batch_runs br SET status='done',last_progress_at=b.start_at+interval '10s',"
            f"finished_at=b.start_at+interval '10s' {due} AND br.status='running' "
            "AND b.start_at<%s AND b.start_at+interval '10s'<=%s",
            ("fra1", self.stall, now),
        )

    def _lon1(self, conn, now):
        due = "FROM test_batches b WHERE b.batch_id=br.batch_id AND br.node_id=" + NODE
        conn.execute(  # moved to it: run when due
            "UPDATE batch_runs br SET status='done',started_at=b.start_at,finished_at=%s "
            f"{due} AND b.region_id={REGION} AND br.status IN ('queued','running') "
            "AND b.start_at<=%s",
            (now, "lon1", "fra1", now),
        )
        status = "done" if now >= self.stored else "uploading"
        conn.execute(  # its own: uploaded, stored once the calculation lets them
            "UPDATE batch_runs br SET status=%s,started_at=b.start_at,last_progress_at=%s "
            f"{due} AND b.region_id={REGION} AND br.status NOT IN ('done','failed') "
            "AND b.start_at<=%s",
            (status, now, "lon1", "lon1", now),
        )


async def test_the_real_stall_moves_fra1s_pulses_to_lon1_and_the_hour_is_done(
    tmp_path, real, database, db
):
    """Progress stops at :05 with 68 runs left (34 countries): found about 3 minutes after its
    last progress, moved to lon1's droplet (still up: its uploads wait for the calculation),
    which runs them before :18. Every country is pulsed; the stall is recorded, not paged."""
    fake = world()
    clock = RealClock(database, tmp_path / "fleet", at(5), at(25))
    _, outcome = await hour(tmp_path, fake, db, clock)
    [stalled] = outcome["stalled"]
    total = fra1_runs(database)
    left = total - BEFORE_STALL
    assert (stalled["cell"], stalled["to"], stalled["moved"], stalled["lost"]) == (
        "shifter/fra1",
        "shifter/lon1",
        left,
        0,
    )
    seen = datetime.fromisoformat(stalled["last_seen"])
    assert at(4.75) <= seen < at(5.25)  # its last finished run
    found = datetime.fromisoformat(stalled["found_at"])
    assert seen + timedelta(minutes=3) <= found <= seen + timedelta(minutes=3, seconds=30)
    assert (outcome["state"], outcome["lost"], outcome["still_open"]) == ("done", [], 0)
    assert query(database, RUNS) == [("done", None, total)]  # every fra1 country pulsed
    assert not ids_named(fake, FRA1) and not ids_named(fake, LON1)
    [(kind, severity, message)] = query(database, "SELECT kind,severity,message FROM alerts")
    assert (kind, severity) == ("pulse_nodes_stalled", "minor")  # recorded, nothing paged
    assert message == (
        "pulse hour 2026-10-01 12:00 UTC: shifter/fra1 stopped at "
        f"{seen:%H:%M}, {left} country pulses: {left} moved to shifter/lon1"
    )


async def test_with_no_sibling_up_the_stalled_cell_is_replaced_once(tmp_path, real, database, db):
    fake = world()
    clock = RealClock(database, tmp_path / "fleet", at(5), HOUR)  # lon1 stored and gone first
    _, outcome = await hour(tmp_path, fake, db, clock)
    [stalled] = outcome["stalled"]
    assert (stalled["to"], stalled["lost"]) == ("replaced", 0) and stalled["moved"] > 0
    total = fra1_runs(database)
    assert outcome["state"] == "done" and query(database, RUNS) == [("done", None, total)]
    assert sorted(kind for kind, _ in fake.deleted) == ["droplet"] * 3  # fra1's old one too
    assert not ids_named(fake, FRA1) and outcome["leftover"] == []


async def test_pulses_with_no_time_left_are_lost_and_the_cell_is_named(
    tmp_path, real, database, db
):
    """Found late (stall_minutes 10, at about :15), only the runs that still start by :18 move;
    the rest fail node_lost and the hour is partial, naming the cell."""
    await set_setting(db, "pulse.stall_minutes", 10)
    clock = RealClock(database, tmp_path / "fleet", at(5), at(25))
    _, outcome = await hour(tmp_path, world(), db, clock)
    [stalled] = outcome["stalled"]
    assert stalled["moved"] > 0 and stalled["lost"] > 0
    assert stalled["moved"] + stalled["lost"] == fra1_runs(database) - BEFORE_STALL
    assert (outcome["state"], outcome["lost"]) == ("partial", ["shifter/fra1"])
    assert dict(((s, e), n) for s, e, n in query(database, RUNS)) == {
        ("done", None): BEFORE_STALL + stalled["moved"],
        ("failed", "node_lost"): stalled["lost"],
    }
    alerts = dict(query(database, "SELECT kind,message FROM alerts"))
    assert alerts["pulse_cells_lost"].endswith(": 1 cell lost: shifter/fra1")
    assert alerts["pulse_nodes_stalled"].endswith(f"{stalled['lost']} lost")


async def test_the_reaper_alerts_of_the_hours_nodes_are_acknowledged_at_its_end(
    tmp_path, real, database, db
):
    fra1 = "SELECT node_id FROM nodes n JOIN runner_regions r USING(region_id) WHERE r.slug='fra1'"
    [(node,)] = query(database, fra1)
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO alerts(kind,severity,message,data,dedupe_key) VALUES "
            "('run_stale','minor','node left a run stale','{}',%s)",
            (f"run_stale:{node}",),
        )
    await hour(tmp_path, world(), db, RealClock(database, tmp_path / "fleet", at(5), at(25)))
    acknowledged = "SELECT acknowledged_at IS NOT NULL FROM alerts WHERE kind='run_stale'"
    assert query(database, acknowledged) == [(True,)]


class LoggingRemote(Remote):
    """A droplet that answers the stall diagnostics; its journal names a URL."""

    def __init__(self):
        super().__init__()
        self.ran = []

    async def run(self, droplet, command, what, *, stdin=None, seconds=120):
        self.ran.append((droplet.name, command[-1]))
        return b"atlas node: progress of batch 7 failed (TimeoutError)\n--- free\n"


async def test_the_stalled_droplets_own_logs_are_kept_before_it_goes(tmp_path, real, database, db):
    remote = LoggingRemote()
    clock = RealClock(database, tmp_path / "fleet", at(5), at(25))
    root, outcome = await hour(tmp_path, world(), db, clock, remote=remote)
    [stalled] = outcome["stalled"]
    assert stalled["log"] == f"stalled/{FRA1}.log" and [n for n, _ in remote.ran] == [FRA1]
    command = remote.ran[0][1]
    assert "journalctl -u atlas-pulse-node -u atlas-dbip-update" in command and "<url>" in command
    log = root / "hours" / f"{HOUR:%Y%m%dT%H}" / stalled["log"]
    assert log.read_bytes().startswith(b"atlas node: progress of batch 7 failed")
    assert log.stat().st_mode & 0o777 == 0o600


async def test_a_droplet_that_cannot_be_read_still_has_its_pulses_moved(
    tmp_path, real, database, db
):
    clock = RealClock(database, tmp_path / "fleet", at(5), at(25))
    _, outcome = await hour(tmp_path, world(), db, clock)  # the plain fake answers no run()
    [stalled] = outcome["stalled"]
    assert stalled["log_error"] == "AttributeError" and stalled["lost"] == 0
    assert outcome["state"] == "done"


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


class StarvedClock(RealClock):
    """PULSE-STATE-2: fra1's droplet starved on disk, as the 14:00 one of 2026-11-15: it starts
    each pulse on time and its counters keep moving, but every one ends 4 minutes later, and its
    host reports I/O pressure `io` in every heartbeat."""

    def __init__(self, database, root, io, late=timedelta(minutes=4)):
        super().__init__(database, root, at(60), at(25))
        self.io, self.late = io, late

    def _fra1(self, conn, now, replaced):
        due = "FROM test_batches b WHERE b.batch_id=br.batch_id AND br.node_id=" + NODE
        conn.execute(f"UPDATE nodes n SET io_pressure=%s WHERE n.node_id={NODE}", (self.io, "fra1"))
        conn.execute(
            f"UPDATE batch_runs br SET status='running',started_at=b.start_at {due} "
            "AND br.status='queued' AND b.start_at<=%s",
            ("fra1", now),
        )
        conn.execute(  # slow, but its counters move: never 3 minutes without progress
            f"UPDATE batch_runs br SET last_progress_at=%s {due} AND br.status='running'",
            (now, "fra1"),
        )
        conn.execute(
            "UPDATE batch_runs br SET status='done',last_progress_at=b.start_at+%s,"
            f"finished_at=b.start_at+%s {due} AND br.status='running' AND b.start_at+%s<=%s",
            (self.late, self.late, "fra1", self.late, now),
        )


async def test_a_disk_starved_node_is_moved_on_its_io_pressure(tmp_path, real, database, db):
    """Still progressing, but every pulse 4 minutes late at 95% I/O pressure: moved once a run
    is 3 minutes overdue, its host's pressure kept in the stall record."""
    clock = StarvedClock(database, tmp_path / "fleet", 95.0)
    _, outcome = await hour(tmp_path, world(), db, clock)
    [stalled] = outcome["stalled"]
    assert (stalled["cell"], stalled["to"], stalled["lost"]) == ("shifter/fra1", "shifter/lon1", 0)
    assert stalled["host"] == {"io_pressure": 95.0, "mem_available_mb": None, "swap_pages": None}
    found = datetime.fromisoformat(stalled["found_at"])
    assert at(3.25) <= found <= at(3.75)  # its first run 3 minutes overdue, not 3 idle minutes
    assert outcome["state"] == "done"


async def test_a_node_falling_behind_is_moved_without_its_pressure(tmp_path, real, database, db):
    """A node too old to report its I/O pressure, 4 minutes late on every pulse: moved once 6
    of its runs are 3 minutes overdue (the 15:00 case of 2026-11-15, found only at 15:15)."""
    clock = StarvedClock(database, tmp_path / "fleet", None)
    _, outcome = await hour(tmp_path, world(), db, clock)
    [stalled] = outcome["stalled"]
    assert stalled["host"]["io_pressure"] is None and stalled["lost"] == 0
    found = datetime.fromisoformat(stalled["found_at"])
    assert at(3.5) <= found <= at(4.25)  # its 6th run 3 minutes overdue, near 12:03:45
    assert outcome["state"] == "done"


async def test_a_slightly_slow_node_under_the_limits_is_left_to_finish(
    tmp_path, real, database, db
):
    clock = StarvedClock(database, tmp_path / "fleet", 10.0, late=timedelta(minutes=2.5))
    _, outcome = await hour(tmp_path, world(), db, clock)
    assert outcome["stalled"] == [] and outcome["state"] == "done"


class TimeoutClock(RealClock):
    """The 15:00 hour of 2026-11-15 (rayobyte fra1, node 68): from its first pulse the droplet
    worked about ten times slower, each pulse 2.5 minutes with 90% of its answers timeouts
    (7,703 timeouts by 15:13), while it kept moving its counters and reported no pressure.
    lon1's droplet of the same network answers clean (1 timeout in 100). With `down`, lon1
    times out as much: the network is down, not one droplet."""

    def __init__(self, database, root, down=False, bad_from=HOUR):
        super().__init__(database, root, at(60), at(25))
        self.down, self.bad_from = down, bad_from

    def _fra1(self, conn, now, replaced):
        due = "FROM test_batches b WHERE b.batch_id=br.batch_id AND br.node_id=" + NODE
        conn.execute(  # before bad_from its pulses are clean: 15 s, no timeout
            "UPDATE batch_runs br SET status='done',started_at=b.start_at,requests=100,"
            "timeouts=0,last_progress_at=b.start_at+interval '15s',"
            f"finished_at=b.start_at+interval '15s' {due} AND br.status IN ('queued','running') "
            "AND b.start_at<%s AND b.start_at+interval '15s'<=%s",
            ("fra1", self.bad_from, now),
        )
        conn.execute(
            f"UPDATE batch_runs br SET status='running',started_at=b.start_at {due} "
            "AND br.status='queued' AND b.start_at<=%s AND b.start_at>=%s",
            ("fra1", now, self.bad_from),
        )
        conn.execute(  # 100 requests over 150 s, 90% timeouts, counters moving
            "UPDATE batch_runs br SET last_progress_at=%s,"
            "requests=least(100,(extract(epoch FROM %s-b.start_at)/1.5)::int),"
            "timeouts=least(90,(extract(epoch FROM %s-b.start_at)/1.5*0.9)::int) "
            f"{due} AND br.status='running'",
            (now, now, now, "fra1"),
        )
        conn.execute(
            "UPDATE batch_runs br SET status='done',finished_at=b.start_at+interval '150s' "
            f"{due} AND br.status='running' AND b.start_at+interval '150s'<=%s",
            ("fra1", now),
        )

    def _lon1(self, conn, now):
        super()._lon1(conn, now)
        timeouts = 95 if self.down else 1
        conn.execute(  # its own pulses' counters, once uploaded
            "UPDATE batch_runs br SET requests=100,timeouts=%s FROM test_batches b "
            f"WHERE b.batch_id=br.batch_id AND br.node_id={NODE} AND br.requests=0 "
            "AND br.status IN ('uploading','done')",
            (timeouts, "lon1"),
        )


async def test_a_node_whose_answers_time_out_is_moved_early(tmp_path, real, database, db):
    """The 15:00 case: found at about 12:02:30 (2 minutes of its pulses, 90% timeouts against
    1% on lon1), not at the 12 minutes it took at 15:00, so none of its pulses are lost."""
    clock = TimeoutClock(database, tmp_path / "fleet")
    _, outcome = await hour(tmp_path, world(), db, clock)
    [stalled] = outcome["stalled"]
    assert (stalled["cell"], stalled["to"], stalled["lost"]) == ("shifter/fra1", "shifter/lon1", 0)
    found = datetime.fromisoformat(stalled["found_at"])
    assert at(2.25) <= found <= at(3)
    assert outcome["state"] == "done"


async def test_a_network_down_everywhere_is_not_a_stalled_droplet(tmp_path, real, database, db):
    clock = TimeoutClock(database, tmp_path / "fleet", down=True)
    _, outcome = await hour(tmp_path, world(), db, clock)
    assert outcome["stalled"] == []  # its pulses measure the outage; nothing to move or replace


def test_timing_out_needs_enough_answers_and_clean_peers():
    node = {"node_id": 1, "provider": "r", "product": "res", "open": True}
    peer = {"node_id": 2, "provider": "r", "product": "res", "open": False}
    slow = node | {"requests": 300, "timeouts": 270}
    assert pulse_moves.timing_out([slow, peer | {"requests": 300, "timeouts": 3}], 0.5)
    assert not pulse_moves.timing_out([slow, peer | {"requests": 300, "timeouts": 160}], 0.5)
    assert not pulse_moves.timing_out([slow, peer | {"requests": 100, "timeouts": 0}], 0.5)
    assert not pulse_moves.timing_out([node | {"requests": 150, "timeouts": 150}, peer], 0.5)
    assert not pulse_moves.timing_out([slow], 0.5)  # no peer: cannot tell it from an outage
    [row] = pulse_moves.timing_out([slow, peer | {"requests": 300, "timeouts": 3}], 0.5)
    assert "requests" not in row and row["node_id"] == 1


async def test_a_droplet_that_turns_bad_mid_hour_is_moved_from_its_recent_answers(
    tmp_path, real, database, db
):
    """Clean until 12:06 (as the 14:00 droplet until 14:05), then ten times slower with 90%
    timeouts: its recent runs, not the whole hour diluted by its good start, find it in time."""
    clock = TimeoutClock(database, tmp_path / "fleet", bad_from=at(6))
    _, outcome = await hour(tmp_path, world(), db, clock)
    [stalled] = outcome["stalled"]
    assert stalled["lost"] == 0 and outcome["state"] == "done"
    assert at(8) <= datetime.fromisoformat(stalled["found_at"]) <= at(11)
