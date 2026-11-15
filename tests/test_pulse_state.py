"""PULSE-STATE-1: a pulse hour whose results only wait behind the hourly calculation to be stored
is done (exit 0, no alert); the job waits for those stores until STORE_BY past the hour, and only
real loss opens its one-line alert. Fake cloud and fake SSH; the database is the test's own."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from orchestrator_cases import query
from pulse_fleet_cases import FakeClock, ids_named, world
from test_pulse_ondemand import FRA1, HOUR, LON1, NOW, HourClock, hour, ready

from atlas.campaign import pulse_alerts, pulse_ondemand
from atlas.storage import connect

pytest_plugins = ["test_storage", "orchestrator_cases"]


class HeldClock(FakeClock):
    """Every run is uploaded at once, and the calculation holds the stores until `stored`
    (None: past the hour's end). Each wait notes which droplets are up."""

    def __init__(self, database, fake, stored=None):
        super().__init__()
        self.t, self.database, self.fake, self.stored = NOW.timestamp(), database, fake, stored
        self.up = []

    async def sleep(self, seconds):
        self.t += seconds
        if seconds != pulse_ondemand.WAIT_POLL_SECONDS:
            return
        self.up.append((self.t, bool(ids_named(self.fake, LON1) or ids_named(self.fake, FRA1))))
        with connect(self.database) as conn:
            if self.stored is not None and self.t >= self.stored.timestamp():
                conn.execute("UPDATE batch_runs SET status='done'")
                conn.execute("UPDATE test_batches SET status='done' WHERE kind='pulse'")
            else:
                conn.execute("UPDATE batch_runs SET status='uploading'")


async def test_stores_held_past_the_run_limit_are_waited_for_and_the_hour_is_done(
    tmp_path, database, db
):
    await ready(database, db)
    fake = world()
    runs_limit = NOW + pulse_ondemand.HARD_STOP - pulse_ondemand.REMOVE_LEAD
    clock = HeldClock(database, fake, stored=HOUR + timedelta(minutes=30))
    _, outcome = await hour(tmp_path, fake, db, clock)
    assert (outcome["state"], outcome["still_open"], outcome["waiting_to_store"]) == ("done", 0, 0)
    # The droplets stayed past the run limit until their runs were stored, then went one by one.
    assert any(up for t, up in clock.up if t > runs_limit.timestamp())
    assert outcome["removed_when_done"] == 2 and outcome["leftover"] == []
    assert "retired" not in outcome and query(database, "SELECT kind FROM alerts") == []


async def test_stores_still_held_at_the_store_limit_are_done_with_their_count(
    tmp_path, database, db
):
    await ready(database, db)
    fake = world()
    clock = HeldClock(database, fake)
    _, outcome = await hour(tmp_path, fake, db, clock)
    assert (outcome["state"], outcome["still_open"], outcome["waiting_to_store"]) == ("done", 0, 4)
    store_by = (HOUR + pulse_ondemand.STORE_BY).timestamp()
    lead = pulse_ondemand.REMOVE_LEAD.total_seconds()  # the removal after the wait
    assert store_by - pulse_ondemand.WAIT_POLL_SECONDS - 60 < clock.t <= store_by + lead
    assert outcome["removed_when_done"] == 0 and outcome["leftover"] == []
    assert not ids_named(fake, LON1) and not ids_named(fake, FRA1)
    assert "retired" not in outcome and query(database, "SELECT kind FROM alerts") == []


async def test_a_signal_after_the_state_is_set_still_fails_the_hour_with_one_alert(
    tmp_path, database, db, monkeypatch
):
    await ready(database, db)

    async def cancelled(*args):  # the signal lands while the hour's alerts are written
        raise asyncio.CancelledError

    monkeypatch.setattr(pulse_ondemand, "hour_alerts", cancelled)
    _, outcome = await hour(tmp_path, world(), db, HourClock(database))
    assert (outcome["state"], outcome["error"]) == ("failed", "cancelled by a signal")
    [(kind, message)] = query(database, "SELECT kind,message FROM alerts")
    assert kind == "pulse_hour_failed" and message.endswith("stopped: cancelled by a signal")


def test_a_message_names_at_most_ten_and_counts_the_rest():
    many = [f"shifter/r{i:02}" for i in range(12)]
    assert pulse_alerts.names(many).endswith("shifter/r09 and 2 more")
    assert pulse_alerts.names(many[:2]) == "shifter/r00, shifter/r01"
    assert (pulse_alerts.plural(1, "cell"), pulse_alerts.plural(3, "cell")) == ("1 cell", "3 cells")


async def test_runs_open_says_the_city_and_falls_back_to_the_region(database, db):
    """PULSE-STATE-2: one line per network and region, in the wording the orchestrator accepted."""
    await pulse_alerts.runs_open(db, HOUR, "webshare/fra1", 1)
    await pulse_alerts.runs_open(db, HOUR, "webshare/xyz9", 3)  # a region without a city
    rows = query(database, "SELECT dedupe_key,message FROM alerts ORDER BY alert_id")
    assert [key for key, _ in rows] == [
        "pulse_runs_open:20261001T12:webshare/fra1",
        "pulse_runs_open:20261001T12:webshare/xyz9",
    ]
    assert rows[0][1].startswith(
        "pulse hour 2026-10-01 12:00 UTC: 1 country pulse of webshare in Frankfurt (fra1) was "
        "not measured this hour"
    )
    assert " 3 country pulses of webshare in xyz9 were " in rows[1][1]


async def test_a_wide_loss_is_one_line_not_a_page_per_network_and_region(database, db):
    """More than ten networks and regions not measured (every node lost mid-run): one alert."""
    nodes = {f"net{i:02}/fra1": 2 for i in range(12)}
    outcome = {"created": 12, "opened": {}, "lost": [], "still_open": 24, "open_nodes": nodes}
    await pulse_alerts.hour_alerts(db, HOUR, 12, outcome)
    [(key, message)] = query(database, "SELECT dedupe_key,message FROM alerts")
    assert key == "pulse_runs_open:20261001T12:wide"
    assert message.startswith(
        "pulse hour 2026-10-01 12:00 UTC: 24 country pulses of 12 networks and regions were not "
        "measured this hour (a gap in their data, not counted as down): net00/fra1, "
    )
    assert message.endswith("and 2 more. Once: nothing to do. Every hour: tell the operator.")
