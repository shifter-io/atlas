"""PULSE-STEP-1/2: a budgeted network's late pulse slot gives each droplet its next run when one of
its RUNS_AT_ONCE slots is free (never sooner than the step over the targets, none after HH:48,
PULSE-STEP-3, or HH:46 at 2 connections a run, PULSE-ROOM-1), so no run waits past the start
grace whatever its droplet's run time and none is still running at the HH:52 run limit. The
simulation runs each droplet as node.py does; the database tests use the test's own PostgreSQL."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from orchestrator_cases import prepare, query, set_setting

from atlas.campaign import pulse_late, pulse_ondemand, pulse_wait
from atlas.daily import budget
from atlas.node_pool import RUNS_AT_ONCE, ConnectionPool
from atlas.orchestrator.nodes import issue_token
from atlas.orchestrator.pulse_hour import Armed
from atlas.regions import load_regions
from atlas.storage import connect

pytest_plugins = ["test_storage", "orchestrator_cases"]

HOUR = datetime(2026, 11, 16, 18, tzinfo=UTC)
GATE = timedelta(minutes=20)  # the daily runs of the hour done (they end about HH:15 to HH:20)
GRACE = timedelta(seconds=180)  # batch.start_grace_seconds: the reaper fails a run not started
GAP = timedelta(seconds=15)  # proxy-cheap's 30 s step over its two hourly targets
LAST = HOUR + pulse_late.last_start(budget.pulse_step(["proxy-cheap"]))  # HH:48 (PULSE-STEP-3)
# PULSE-ROOM-1: a run of 2 connections (PC-BUDGET-1) starts by HH:46
NARROW_LAST = HOUR + pulse_late.last_start(GAP * 2, pulse_late.NARROW_ROOM)
NODE_POLL = 5  # node.py's work poll, seconds
# 100 requests at 5 connections per region, 16 Nov 2026 07:00 (median, median, p90 in turn).
SECONDS = {
    "fra1": (22, 22, 30),
    "ams3": (19, 19, 25),
    "lon1": (28, 28, 35),
    "tor1": (30, 30, 36),
    "nyc3": (36, 36, 45),
    "blr1": (47, 47, 65),
    "sgp1": (49, 49, 76),
    "syd1": (63, 63, 79),
}


def _clock(at: datetime):
    return SimpleNamespace(now=lambda: at.timestamp())


def _hour_cells() -> list[Armed]:
    """8 droplets, one per region, holding about 7 in 10 of its countries (the low-availability
    skip, LOWAVAIL-1: about 175 countries, 350 runs)."""
    cells = []
    for region in load_regions().regions:
        keep = region.countries[: max(1, round(len(region.countries) * 0.7))]
        cells.append(Armed("proxy-cheap", "res", region.slug, keep))
    return cells


# --- the droplets simulated -----------------------------------------------------------------


@dataclass
class _Run:
    node: str
    start_at: datetime  # its start as the orchestrator has it: the reaper's grace counts from it
    seconds: int
    started: datetime | None = None


def _simulate(
    counts: dict[str, int],
    seconds: dict,
    *,
    clock: bool = False,
    gate: timedelta = GATE,
    last: datetime = LAST,
) -> list[_Run]:
    """Each droplet's runs (`counts`), at its region's run times (`seconds`, in turn), from the
    gate on, none given after `last`."""
    found = []
    for node, count in counts.items():
        found += _droplet(node, count, seconds[node], clock, (HOUR + gate, last))
    return found


def _droplet(node: str, count: int, seconds: tuple, clock: bool, times: tuple) -> list[_Run]:
    """One droplet as node.py runs it: it sees a run at its next work poll (a clock's runs are
    claimed minutes ahead), starts it at its start_at once it holds fewer than RUNS_AT_ONCE (its
    connection pool, oldest first) and ends it after its run time. The runs are given by the
    Puller every PULL_SECONDS (none after `times`' last), or (`clock`) every GAP from the gate's
    15 s (PULSE-STEP-1). `times`: (the gate, the last start)."""
    begin, last = times
    runs = []
    if clock:
        first = begin + timedelta(seconds=15)
        runs = [_Run(node, first + GAP * i, seconds[i % 3]) for i in range(count)]
    queue = {} if clock else {node: list(range(count))}
    puller = pulse_late.Puller(queue, GAP, last)
    for tick in range(40 * 60):
        now = begin + timedelta(seconds=tick)
        if tick % int(pulse_late.PULL_SECONDS) == 0:
            busy = sum(1 for r in runs if r.started is None or now < _end(r))
            for _, i in puller.due(now, Counter({node: busy})):
                runs.append(_Run(node, now, seconds[i % 3]))
        running = sum(1 for r in runs if r.started and now < _end(r))
        for run in [r for r in runs if r.started is None and r.start_at <= now]:
            if running < RUNS_AT_ONCE and (clock or tick % NODE_POLL == 0):
                run.started, running = now, running + 1
    return runs


def _end(run: _Run) -> datetime:
    return run.started + timedelta(seconds=run.seconds)


def _counts() -> dict[str, int]:
    """Each droplet's runs of the hour: its countries over the two hourly targets."""
    return {cell.region: 2 * len(cell.countries) for cell in _hour_cells()}


def _late(runs: list[_Run]) -> list[_Run]:
    """The runs the reaper fails: not started within the grace of their start."""
    return [r for r in runs if r.started is None or r.started - r.start_at > GRACE]


def test_a_fixed_30_s_clock_leaves_the_far_droplets_runs_past_the_grace():
    runs = _simulate(_counts(), SECONDS, clock=True)
    assert {r.node for r in _late(runs)} >= {"blr1", "sgp1", "syd1"}
    assert not [r for r in _late(runs) if r.node in ("fra1", "ams3", "lon1", "tor1")]


def test_8_droplets_with_20_to_80_s_runs_start_every_run_in_its_grace_and_end_by_hh52():
    counts = _counts()
    assert len(counts) == 8 and 340 <= sum(counts.values()) <= 360
    runs = _simulate(counts, SECONDS)
    assert len(runs) == sum(counts.values())  # every country of every droplet, both targets
    assert _late(runs) == []
    assert max(_end(r) for r in runs) <= HOUR + pulse_late.FINISH_BY
    assert max(r.start_at for r in runs) <= LAST
    for node in counts:  # never more than RUNS_AT_ONCE at once: no run waits for connections
        mine = [r for r in runs if r.node == node]
        for run in mine:
            at_once = sum(1 for o in mine if o.started <= run.started < _end(o))
            assert at_once <= RUNS_AT_ONCE
            assert run.started - run.start_at <= timedelta(seconds=NODE_POLL)


def test_a_droplet_whose_runs_take_80_s_starts_fewer_runs_and_fails_none():
    counts = {"fra1": 82}  # fra1's 41 countries at 80 s: about 3 runs a minute, not 4
    runs = _simulate(counts, {"fra1": (80, 80, 80)})
    assert 40 < len(runs) < 82 and _late(runs) == []
    assert max(r.start_at for r in runs) <= LAST
    assert max(_end(r) for r in runs) <= HOUR + pulse_late.FINISH_BY
    starts = sorted(r.start_at for r in runs)
    assert min(b - a for a, b in zip(starts, starts[1:], strict=False)) >= GAP


def test_a_fast_droplet_keeps_a_country_every_30_s():
    runs = _simulate({"fra1": 82}, {"fra1": (22, 22, 25)})  # fra1's median 21.6 s
    starts = sorted(r.start_at for r in runs)
    assert len(starts) == 82 and _late(runs) == []
    assert {b - a for a, b in zip(starts, starts[1:], strict=False)} == {GAP}
    assert starts[-1] - starts[0] == GAP * 81  # about 20 minutes, as PULSE-STEP-1's clock


def test_a_slow_droplet_near_the_end_gets_no_run_it_cannot_finish_by_the_run_limit():
    # 16 Nov 12:00 (PULSE-STEP-3): nyc3, about a minute a run, was given one at 12:49:51 (a slot
    # had freed) that was still running at the HH:52 run limit, over 131 s later.
    puller = pulse_late.Puller({"nyc3": [1, 2]}, GAP, LAST)
    assert puller.due(HOUR + timedelta(minutes=49, seconds=51), Counter()) == []
    assert (puller.not_opened, puller.pulling) == (2, set())
    runs = _simulate({"nyc3": 40}, {"nyc3": (140, 140, 140)}, gate=timedelta(minutes=40))
    assert 6 <= len(runs) < 40 and _late(runs) == []
    for run in runs:  # even the last one, given a slot at HH:48, ends by HH:52
        assert run.start_at <= LAST and _end(run) <= HOUR + pulse_late.FINISH_BY
    assert LAST + pulse_late.PACED_ROOM == HOUR + pulse_late.FINISH_BY
    assert pulse_late.PACED_ROOM >= timedelta(seconds=NODE_POLL + 140)


@pytest.mark.parametrize("gate", [18, 32])
def test_at_two_connections_per_run_every_droplet_ends_by_hh52(gate):
    """PC-BUDGET-1: a pulse droplet of 4 connections runs each pulse on 2, about 2.5 times as
    long as on 5 (syd1's p90 79 s: about 198 s), inside NARROW_ROOM (PULSE-ROOM-1): no run is
    late or still running at HH:52; fewer start."""
    slow = {region: tuple(round(2.5 * s) for s in times) for region, times in SECONDS.items()}
    assert pulse_late.room({"proxy-cheap": 4}) == pulse_late.NARROW_ROOM
    assert pulse_late.NARROW_ROOM >= timedelta(seconds=NODE_POLL + max(slow["syd1"]))
    runs = _simulate(_counts(), slow, gate=timedelta(minutes=gate), last=NARROW_LAST)
    assert _late(runs) == [] and max(r.start_at for r in runs) <= NARROW_LAST
    assert max(_end(r) for r in runs) <= HOUR + pulse_late.FINISH_BY
    assert len(runs) < sum(_counts().values())


def test_from_the_latest_gate_a_fast_droplet_has_16_minutes_of_starts():
    # BUDGET-STOP-EARLIER-1 (#1293): the gate's latest is HH:30, so a droplet armed then (about
    # 2 minutes with its boot) starts runs from HH:32 to HH:48, not HH:40 to HH:48 (about 32).
    assert pulse_late.GATE_LATEST == timedelta(minutes=30)
    runs = _simulate({"fra1": 82}, {"fra1": (22, 22, 25)}, gate=timedelta(minutes=32))
    assert 60 <= len(runs) < 82 and _late(runs) == []
    assert max(r.start_at for r in runs) <= LAST
    assert max(_end(r) for r in runs) <= HOUR + pulse_late.FINISH_BY


def test_the_puller_drops_what_is_left_at_the_last_start_and_lets_its_node_go():
    last = HOUR + pulse_late.LAST_START
    puller = pulse_late.Puller({"a": [1, 2, 3], "b": [], "c": [4]}, GAP, last)
    assert puller.pulling == {"a", "c"}
    assert puller.due(last, Counter({"c": RUNS_AT_ONCE})) == [("a", 1)]
    assert puller.due(last + GAP, Counter()) == []  # past the last start: a's 2 and 3, c's 4
    assert (puller.not_opened, puller.pulling) == (3, set())


@pytest.mark.parametrize(("total", "reserve", "found"), [(100, 10, (11, 5)), (40, 8, (4, 2))])
def test_connection_use_per_droplet_never_exceeds_the_networks_share(
    monkeypatch, total, reserve, found
):
    monkeypatch.setattr(budget, "CONNECTION_BUDGET", {"proxy-cheap": total})
    monkeypatch.setattr(budget, "BROWSER_RESERVE", reserve)
    limit = budget.share("proxy-cheap", len(_hour_cells()), least=RUNS_AT_ONCE)
    each = ConnectionPool(limit).share(25)
    assert (limit, each) == found and RUNS_AT_ONCE * each <= limit


def test_the_largest_region_starts_every_country_by_hh48_from_a_gate_at_hh18():
    assert pulse_late.FINISH_BY == pulse_late.STOP - pulse_ondemand.REMOVE_LEAD
    step = budget.pulse_step(["proxy-cheap"])
    largest = max(len(region.countries) for region in load_regions().regions)  # fra1, 59
    assert timedelta(minutes=18) + step * largest <= pulse_late.last_start(step)
    assert pulse_late.last_start(step) == timedelta(minutes=48)
    assert pulse_late.last_start(timedelta(minutes=5)) == timedelta(minutes=47)
    assert pulse_late.LAST_START == timedelta(minutes=50)
    assert pulse_late.last_start(None) == pulse_late.LAST_START


# --- the database ---------------------------------------------------------------------------


async def _ready(database, db, cells: list[Armed]) -> None:
    prepare(database, tuple(code for cell in cells for code in cell.countries))
    with connect(database) as conn:
        conn.execute("UPDATE targets SET pulse_interval_minutes=60 WHERE engine='http'")
    await set_setting(db, "pulse.on_demand", True)
    for cell in cells:
        issue_token(database, provider=cell.provider, product=cell.product, region=cell.region)


def _starts(database) -> dict[str, list[datetime]]:
    """Each droplet's run starts (its own, else its batch's), in order."""
    rows = query(
        database,
        "SELECT r.slug,coalesce(br.start_at,b.start_at) FROM batch_runs br "
        "JOIN test_batches b USING(batch_id) JOIN nodes n USING(node_id) "
        "JOIN runner_regions r ON r.region_id=n.region_id ORDER BY 1,2",
    )
    found: dict[str, list[datetime]] = {}
    for slug, start in rows:
        found.setdefault(slug, []).append(start)
    return found


async def test_a_network_without_a_pulse_step_keeps_pulse_step_seconds(database, db):
    cells = [Armed("webshare", "res", "fra1", ("AT", "DE"))]
    await _ready(database, db, cells)
    opened = await pulse_late.open_late(db, _clock(HOUR + GATE), HOUR, cells)
    assert "step_seconds" not in opened and opened["runs"] == 4
    begin = HOUR + GATE + timedelta(seconds=15)
    assert _starts(database)["fra1"] == [begin + timedelta(seconds=s) for s in (0, 6, 12, 18)]
    assert budget.pulse_step(["webshare", "decodo"]) is None
    assert budget.pulse_step(["webshare", "proxy-cheap"]) == timedelta(seconds=30)


class _Clock:
    """The test's wall clock: each sleep lets the other task run, and ends the runs that started
    `run` seconds ago (the nodes ran them)."""

    def __init__(self, database, at: datetime, run: float = 20.0):
        self.t, self.database, self.run = at.timestamp(), database, run

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds
        cut = datetime.fromtimestamp(self.t - self.run, UTC)
        with connect(self.database) as conn:
            conn.execute(
                "UPDATE batch_runs br SET status='done' FROM test_batches b WHERE "
                "b.batch_id=br.batch_id AND coalesce(br.start_at,b.start_at)<=%s",
                (cut,),
            )
            conn.execute(
                "UPDATE test_batches b SET status='done' WHERE NOT EXISTS (SELECT 1 FROM "
                "batch_runs br WHERE br.batch_id=b.batch_id AND br.status<>'done')"
            )
        await asyncio.sleep(0)


async def test_the_late_phase_pulls_each_droplets_runs_and_removes_it_when_done(
    database, db, monkeypatch
):
    cells = [
        Armed("proxy-cheap", "res", "fra1", ("AT", "CH", "DE")),
        Armed("proxy-cheap", "res", "ams3", ("NL",)),
    ]
    await _ready(database, db, cells)
    await set_setting(db, "pulse.step_seconds", 40)  # the step is never faster than the setting
    removed = []

    async def remove(hour_root, done, s):
        removed.extend((name, len(puller_left())) for name in done)
        return dict.fromkeys(done)

    monkeypatch.setattr(pulse_wait, "remove_done", remove)
    pullers = []
    monkeypatch.setattr(pulse_late, "Puller", _recording(pulse_late.Puller, pullers))

    def puller_left():
        return pullers[0].pulling

    nodes = {f"n-{cell.region}": cell for cell in cells}
    s = SimpleNamespace(db=db, clock=_Clock(database, HOUR + GATE))
    found = await pulse_late.open_and_wait(None, HOUR, (nodes, cells), s, {"proxy-cheap": 4})
    opened = found["opened"]
    assert (opened["created"], opened["runs"], opened["not_opened"]) == (8, 8, 0)
    assert opened["step_seconds"] == 40 and opened["slot"] == (HOUR + GATE).isoformat()
    assert pullers[0].last == HOUR + timedelta(minutes=46)  # 2 connections a run: PULSE-ROOM-1
    assert opened["last_start"] == pullers[0].last.isoformat()
    assert found["still_open"] == 0 and sorted(found["removed"]) == ["n-ams3", "n-fra1"]
    assert removed[0] == ("n-ams3", 1)  # ams3 went while fra1 still had runs to be given
    starts = _starts(database)
    assert len(starts["fra1"]) == 6 and len(starts["ams3"]) == 2
    for chain in starts.values():  # a run at most every 40 / 2 s per droplet
        assert min(b - a for a, b in zip(chain, chain[1:], strict=False)) >= timedelta(seconds=20)
    assert query(database, "SELECT DISTINCT scheduled_for FROM test_batches") == [(HOUR + GATE,)]


async def test_an_error_while_pulling_ends_the_wait_and_reaches_the_job_as_it_is(
    database, db, monkeypatch
):
    cells = [Armed("proxy-cheap", "res", "fra1", ("DE",))]
    await _ready(database, db, cells)
    waits = []

    async def wait(hour_root, hour, nodes, s, limits, pulling=frozenset()):
        waits.append("started")
        await asyncio.Event().wait()  # until cancelled

    async def broken(*args):
        await asyncio.sleep(0)
        raise RuntimeError("the database went away")

    monkeypatch.setattr(pulse_late, "wait_and_remove", wait)
    monkeypatch.setattr(pulse_late, "open_run", broken)
    s = SimpleNamespace(db=db, clock=_Clock(database, HOUR + GATE))
    with pytest.raises(RuntimeError, match="went away"):
        await pulse_late.open_and_wait(None, HOUR, ({}, cells), s, {"proxy-cheap": 4})
    assert waits == ["started"]


def _recording(cls, found: list):
    def make(*args):
        found.append(cls(*args))
        return found[-1]

    return make


async def test_a_pulled_run_joins_another_nodes_open_batch_with_its_own_start(database, db):
    cells = [Armed("proxy-cheap", "res", "fra1", ("DE",)), Armed("beta", "res", "fra1", ("DE",))]
    await _ready(database, db, cells)
    mine = ("proxy-cheap", "res", "fra1")
    runs, _, targets = await pulse_late.queues(db, cells[:1])
    assert targets == 2 and [run[0] for run in runs[mine]] == ["DE", "DE"]
    slot, later = HOUR + GATE, HOUR + GATE + timedelta(seconds=40)
    first = await pulse_late.open_run(db, slot, mine, runs[mine][0], slot)
    second = await pulse_late.open_run(db, slot, ("beta", "res", "fra1"), runs[mine][0], later)
    assert (first, second) == ((1, 1), (0, 1))
    rows = query(
        database,
        "SELECT p.provider,br.start_at,b.start_at FROM batch_runs br JOIN test_batches b "
        "USING(batch_id) JOIN products p USING(product_id) ORDER BY 1",
    )
    assert rows == [("beta", later, slot), ("proxy-cheap", None, slot)]
    await set_setting(db, "pulse.on_demand", False)
    assert (await pulse_late.queues(db, cells))[0] == {}
