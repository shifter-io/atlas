"""SIM-CHECK-2: the checks again on a stored load (`simulate.py check`, read only), and its browser
runs stored again with the current model (`simulate.py --rerun browser`), SIMULATE.md ("Checking
and re-storing a stored load")."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import simulate_check
import simulate_model
import simulate_report
import simulate_steps
import wipe_replay
from psycopg import sql
from replay import DISABLE, Refused, now
from replay_report import write_private
from target import REPLAY_MARKER

from proxy_index import storage

KINDS = ("size", "stats")
DRY_RUN = "rerun-browser-dry-run.json"
STEP = "rerun_browser"  # the hour's step (SimSteps.rerun_browser) in its record
# The browser batches of the simulated days that hold a run of the load (marked with its id),
# run by the load's nodes only (a batch planned ahead without a run stays), and their runs.
MINE = f"t.summary->>'{REPLAY_MARKER}'=%(replay)s"
SETS = (
    "CREATE TEMP TABLE wipe_batches ON COMMIT DROP AS SELECT batch_id FROM test_batches b "
    "WHERE kind='browser' AND scheduled_for>=%(first)s AND scheduled_for<%(end)s "
    f"AND EXISTS (SELECT FROM test_runs t WHERE t.batch_id=b.batch_id AND {MINE}) "
    "AND NOT EXISTS (SELECT FROM batch_runs r WHERE r.batch_id=b.batch_id "
    "AND (r.node_id IS NULL OR r.node_id<>ALL(%(ids)s)))",
    "CREATE TEMP TABLE wipe_runs ON COMMIT DROP AS SELECT run_key FROM test_runs t "
    f"WHERE batch_id IN (SELECT batch_id FROM wipe_batches) AND {MINE}",
)
BUCKETS = (
    "CREATE TEMP TABLE wipe_event_buckets ON COMMIT DROP AS SELECT DISTINCT "
    f"{wipe_replay.BUCKET} AS bucket_start FROM test_events WHERE {wipe_replay.BATCHES}"
)
# The load's stored browser runs of the simulated days: their tries and bytes as the calculation
# reads them (run_buckets), with the batch's network, target and hour.
STORED = (
    "SELECT p.provider,b.target_id,date_trunc('hour',b.scheduled_for),sum(k.completed)::bigint,"
    "sum(k.bytes_down)::bigint FROM test_batches b JOIN test_runs t USING(batch_id) "
    "JOIN scope_stats s USING(scope_id) JOIN products p USING(product_id) "
    "JOIN run_buckets k ON k.run_key=t.run_key WHERE b.kind='browser' "
    f"AND b.scheduled_for>=%(first)s AND b.scheduled_for<%(end)s AND {MINE} "
    "GROUP BY t.run_key,1,2,3 HAVING sum(k.completed)>0"
)
RUN_ROWS = ("run_buckets", "run_geo_buckets", "ip_observations", "run_enrichments")
RUN_ROWS += ("estimated_hours", "calculation_roles", "calculated_run_ranges")
# (table, condition) in foreign-key order; a table the database lacks is skipped.
FACTS = (
    *((name, wipe_replay.BATCHES) for name in ("test_events", "outages")),
    *((name, wipe_replay.RUNS) for name in RUN_ROWS),
    ("batch_runs", wipe_replay.BATCHES),
    ("test_runs", wipe_replay.RUNS),
    ("test_batches", wipe_replay.BATCHES),
)


def check(args) -> dict:
    """Both checks of the stored load in --out, from its state.json, and the plan and the stored
    browser runs read in one read-only transaction (the model may have changed since they were
    stored); checks.json and checks.md beside the report. Writes nothing else."""
    out = Path(args.out)
    state, data = json.loads((out / "state.json").read_text()), Path(args.profile).read_bytes()
    if hashlib.sha256(data).hexdigest() != state["profile_sha256"]:
        raise Refused(f"{Path(args.profile).name} is not the load's profile (profile_sha256)")
    profile, start = json.loads(data), date.fromisoformat(state["first_day"])
    with storage.connect(args.database) as conn:
        conn.read_only = True
        with conn.transaction():
            database = conn.execute("SELECT current_database()").fetchone()[0]
            if database != state["database"]:
                raise Refused(f"the state is the load of {state['database']!r}, not {database!r}")
            found = (start, args.formulas, args.browser_settings, state["pulses"])
            plan = simulate_model.read_plan(conn, *found)
            runs = conn.execute(STORED, bounds(state, state["days"] - 1)).fetchall()
    model = simulate_model.Model(profile, replace(plan, days=state["days"]), state["seed"])
    per_day = state["hours_per_day"]
    records = [r for r in state["hours"][: (state["days"] - 1) * per_day] if "figures" in r]
    hours = simulate_report.stored(model, records)
    stored_browser(hours, records, runs, model.plan.browser)
    size = simulate_check.size(profile, hours, model, simulate_check.snapshots(state))
    day = min(2, state["days"] - 1)
    stats = simulate_check.stats(profile, [model.hour(day, h) for h in range(per_day)])
    found = {"replay_id": state["replay_id"], "at": now(), "stats_day": day}
    found |= {"size": size, "stats": stats}
    write_private(out / "checks.json", json.dumps(found, indent=2))
    write_private(out / "checks.md", simulate_check.markdown(found))
    return {name: {key: found[name][key] for key in ("pass", "compared")} for name in KINDS}


def bounds(state: dict, days: int) -> dict:
    """The simulated days' first and end moments, the load's nodes and its id."""
    first = datetime.combine(date.fromisoformat(state["first_day"]), time(), UTC)
    found = {"first": first, "end": first + timedelta(days=days), "ids": state["nodes"]}
    return found | {"replay": state["replay_id"]}


def stored_browser(hours: list, records: list[dict], runs: list, settings) -> None:
    """Each stored hour's browser runs: the database's, not the current model's."""
    found = defaultdict(list)
    for provider, target, hour, loads, size in runs:
        own = SimpleNamespace(network=provider, target=target, engine=settings.engine(target))
        found[hour].append(own)
        own.loads, own.bytes_per_load = loads, size / loads
    for record, hour in zip(records, hours, strict=True):
        hour.browser = found.get(datetime.fromisoformat(record["start"]), [])


def delete(conn, bounds: dict) -> dict:
    """Delete the browser runs of the simulated days with the rows that reference them, in the
    open transaction (the live tape's buckets recounted, as wipe_replay.py does); the rows
    deleted per table."""
    conn.execute("SET LOCAL lock_timeout='60s'")
    conn.execute("SET LOCAL statement_timeout=0")
    for lock in wipe_replay.LOCKS:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))
    for statement in SETS:
        conn.execute(statement, bounds)
    counted = wipe_replay.exists(conn, "test_event_buckets")
    if counted:
        conn.execute(BUCKETS)
    deleted = {}
    for name, condition in FACTS:
        if wipe_replay.exists(conn, name):
            found = sql.SQL(f"DELETE FROM {{}} WHERE {condition}").format(sql.Identifier(name))
            deleted[name] = conn.execute(found).rowcount
    for statement in wipe_replay.RECOUNT if counted else ():
        conn.execute(statement)
    return {name: rows for name, rows in deleted.items() if rows}


def wipe(conn, state: dict, days: int, apply: bool, dry: Path) -> dict:
    """The dry run (deleted, then rolled back; its counts in `dry`) or, with `apply` after a dry
    run, the same delete committed when its counts equal the dry run's."""
    if apply and not dry.exists():
        raise Refused(f"no dry run ({dry.name}): run --rerun browser without --apply first")
    with conn.transaction(force_rollback=not apply):
        found = {"replay_id": state["replay_id"], "deleted": delete(conn, bounds(state, days))}
        if apply and json.loads(dry.read_text()) != found:  # a raise here rolls back
            raise Refused(f"the counts differ from the dry run's ({dry.name}): nothing deleted")
    if apply:
        dry.unlink()  # a later --apply needs a dry run of its own
    else:
        write_private(dry, json.dumps(found, indent=2))
    return found


def browser(sim) -> dict:
    """--rerun browser on a Simulation (simulate.py): the dry run; or, with --apply, the delete
    (once) and every simulated hour's browser runs stored again by the hour's step, resumable.
    The daily tests stay as stored."""
    state, args = sim.state, sim.args
    work = state.get(STEP)
    if not work or work.get("done"):
        found = wipe(sim.conn, state, args.days, args.apply, sim.out / DRY_RUN)
        if not args.apply:
            return found | {"dry_run": True}
        state[STEP] = work = {"deleted": found["deleted"], "at": now(), "done": False}
        for record in state["hours"]:
            record["ingest"]["steps"].pop(STEP, None)
        sim.save()
    try:
        for index in range(sim.total()):
            record = sim.record(index)
            start = datetime.fromisoformat(record["start"])
            hour = sim.model.hour(record["day"] + 1, start.hour)
            sim.steps.current = simulate_steps.Current(hour, {}, None)
            sim.step(index, STEP, record["day"], start)
        work |= {"done": True, "finished": now()}
    finally:
        sim.conn.execute(DISABLE, (state["nodes"],))
        sim.save()
    return {"replay_id": state["replay_id"], "deleted": work["deleted"], "hours": sim.total()}
