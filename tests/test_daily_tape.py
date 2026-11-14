"""SCORE-DATA-1b: `atlas daily tape` runs the live tape's detector over stored pulse batches,
against an isolated PostgreSQL.

The fixture: half-hourly pulses in DE. alpha has a planned outage (three down pulses from slot
8, healthy again from slot 11) and a success rate that moves each slot (pulse_finished); beta is
steady, then down from slot 13 to the end (an outage still open).
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, timedelta

from census_pulse_cases import batch, batch_run, product
from orchestrator_cases import prepare, query
from psycopg.rows import dict_row

from atlas import detect, detect_replay, storage
from atlas.calculated.aggregate import outage_list
from atlas.calculated.scheduled import window_outages
from atlas.cli import main
from atlas.daily import tape
from atlas.storage import connect

pytest_plugins = ["test_storage"]

T0 = datetime(2026, 10, 25, tzinfo=UTC)
SLOT = timedelta(minutes=30)
SLOTS = 16
END = T0 + SLOTS * SLOT
ALPHA_DOWN, BETA_DOWN = range(8, 11), range(13, SLOTS)
EVENTS = "SELECT kind,product_id,country_code,batch_id,at FROM test_events ORDER BY event_id"


def stored(database) -> dict:
    """The fixture's pulse batches; returns the products' ids."""
    prepare(database, ("DE",))
    with connect(database) as conn:
        ids = {name: product(conn, name) for name in ("alpha", "beta")}
        for slot in range(SLOTS):
            at = T0 + slot * SLOT
            batch_id = batch(conn, "pulse", at)
            good = 0 if slot in ALPHA_DOWN else 100 - 10 * (slot % 2)
            batch_run(conn, batch_id, ids["alpha"], at + timedelta(minutes=1), successes=good)
            good = 0 if slot in BETA_DOWN else 100
            batch_run(conn, batch_id, ids["beta"], at + timedelta(minutes=1), successes=good)
    return ids


def run(database, *options, since=T0, until=END):
    span = ["--from", since.isoformat(), "--to", until.isoformat()]
    return main(["daily", "tape", "--database-file", str(database), *span, *options])


def batch_at(database, slot: int) -> int:
    found = "SELECT batch_id FROM test_batches WHERE scheduled_for=%s"
    return query(database, found, (T0 + slot * SLOT,))[0][0]


def test_the_tape_writes_the_planned_outage_once(database, capsys):
    ids = stored(database)
    assert run(database, "--dry-run") == 0
    dry = json.loads(capsys.readouterr().out)
    assert query(database, EVENTS) == [] and query(database, "SELECT * FROM outages") == []
    assert query(database, "SELECT * FROM settings WHERE key=%s", (detect.WATERMARK,)) == []
    assert run(database) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["events"] == dry["events"] and found["outages"] == dry["outages"]
    assert found["events"]["down"] == {"alpha": 1, "beta": 1}
    assert found["events"]["recovered"] == {"alpha": 1}
    assert found["events"]["pulse_finished"]["alpha"] >= 6 and found["steps"] == 96
    alpha, beta = {"outages": 1, "open": 0}, {"outages": 1, "open": 1}
    assert found["outages"] == {"alpha": alpha, "beta": beta}
    events = query(database, EVENTS)
    down = [e for e in events if e[0] == "down" and e[1] == ids["alpha"]]
    recovered = [e for e in events if e[0] == "recovered"]
    assert [(e[2], e[3]) for e in down] == [("DE", batch_at(database, ALPHA_DOWN[0]))]
    assert [(e[2], e[3]) for e in recovered] == [("DE", batch_at(database, ALPHA_DOWN[-1] + 1))]
    finished = dict(query(database, "SELECT batch_id,finished_at FROM test_batches"))
    started = dict(query(database, "SELECT batch_id,started_at FROM test_batches"))
    opened = dict(query(database, "SELECT batch_id,started_at FROM outages"))
    for kind, _, _, batch_id, at in events:  # each at its own time, not the step (TAPE-TIME-1)
        own = {"down": opened, "recovered": started}.get(kind, finished)[batch_id]
        assert at == own, kind
    with connect(database) as conn:
        assert tape.watermark(conn) == END - tape.TICK
    # Again: the detector's verdicts repeat, nothing is added.
    assert run(database) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["events"] == {} and again["outages"] == found["outages"]
    assert query(database, EVENTS) == events


def test_an_outage_has_what_the_changelogs_incident_needs(database, capsys):
    """#844's incident: start, end or ongoing and countries are in the outage (one row per
    country; an incident over several countries is the rows with the network and start). The
    lowest slot success is not stored: #844 derives it from the pulses of the span (0 here)."""
    ids = stored(database)
    assert run(database) == 0
    with connect(database) as conn:
        rows = window_outages(conn.cursor(row_factory=dict_row), T0, END)
        by = {row["product_id"]: outage_list([row], T0, END)[0] for row in rows}
        lowest = conn.execute(
            "SELECT min(br.successes::float/br.requests) FROM batch_runs br "
            "JOIN test_batches b USING(batch_id) WHERE br.product_id=%s "
            "AND b.started_at>=%s AND b.started_at<%s",
            (ids["alpha"], by[ids["alpha"]]["started_at"], by[ids["alpha"]]["ended_at"]),
        ).fetchone()[0]
    keys = {"country", "region", "source", "started_at", "ended_at", "duration_s"}
    assert all(set(found) == keys for found in by.values())
    alpha, beta = by[ids["alpha"]], by[ids["beta"]]
    assert alpha["country"].strip() == "DE" and alpha["source"] == "pulse"
    assert alpha["started_at"] == T0 + (ALPHA_DOWN[0] - 1) * SLOT + timedelta(minutes=1)
    assert alpha["ended_at"] == T0 + (ALPHA_DOWN[-1] + 1) * SLOT
    assert beta["ended_at"] is None  # ongoing
    assert lowest == 0


def test_another_detector_moving_the_watermark_stops_it(database, monkeypatch, capsys):
    stored(database)
    real = detect_replay.Replay.hour

    def orchestrator(self, upto, end, step):
        found = real(self, upto, end, step)
        detect.set_watermark(self.cur, end + timedelta(hours=1), end)
        return found

    monkeypatch.setattr(detect_replay.Replay, "hour", orchestrator)
    assert run(database) == 2
    assert "another detect() moved the watermark" in capsys.readouterr().err


class Identity:
    """A connection that only answers current_database() and current_user."""

    def execute(self, statement):
        assert statement == "SELECT current_database(),current_user"
        return self

    def fetchone(self):
        return "proxy_reviews", "atlas"


def test_production_and_wrong_ranges_are_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(storage, "connect", lambda _: contextlib.nullcontext(Identity()))
    assert run(tmp_path / "database.toml") == 2
    assert capsys.readouterr().err.startswith("refused: 'proxy_reviews' is production's")
    for options, since in ((["--step-minutes", "0"], T0), ([], END)):
        assert run(tmp_path / "database.toml", *options, since=since) == 2
        assert "--step-minutes is at least 1" in capsys.readouterr().err
