"""PULSE-STATE-1 (3/3): a node never opens its pulse runs together (node_pace.StartPacer), and
a pulse run has a hard deadline. The case is the real one of 2026-11-15: the last fra1 droplet
to arm fetched its first work late and got its 8 runs already due in one answer."""

from __future__ import annotations

import asyncio
import time
from itertools import pairwise
from datetime import UTC, datetime, timedelta

from test_node import assignment

from atlas.models import ProxyConfig
from atlas.node import NodeAgent, NodeSettings
from atlas.node_pace import PULSE_GAP, PULSE_RUN_SECONDS, StartPacer

HOUR = datetime(2026, 11, 15, 12, tzinfo=UTC)
STEP = timedelta(seconds=6)  # a region's chain: two runs every pulse.step_seconds (12 s)


def chain(first: datetime, runs: int) -> list[datetime]:
    return [first + STEP * i for i in range(runs)]


def test_a_late_nodes_backlog_starts_one_run_every_gap_from_its_first_fetch():
    """12:00 node 71: 8 runs due 12:00:19 to 12:01:01, first fetched at 12:01:05."""
    due = chain(HOUR + timedelta(seconds=19), 8)
    fetched = HOUR + timedelta(minutes=1, seconds=5)
    pacer = StartPacer()
    starts = [pacer.start(moment, fetched) for moment in due]
    assert starts == [fetched + PULSE_GAP * i for i in range(8)]  # never two at once
    later = [pacer.start(moment, fetched) for moment in chain(due[-1] + STEP, 12)]
    assert later[0] == starts[-1] + PULSE_GAP  # the chain then catches up, 3 s a run
    assert later[8:] == chain(due[-1] + STEP * 9, 4)  # and runs on schedule again


def test_a_chain_on_time_starts_exactly_on_schedule():
    due = chain(HOUR + timedelta(seconds=15), 20)
    pacer = StartPacer()
    assert [pacer.start(moment, HOUR) for moment in due] == due


def test_two_chains_on_one_node_interleave_a_gap_apart():
    """A helper running a handed or moved region's chain beside its own."""
    own, handed = chain(HOUR, 4), chain(HOUR, 4)
    pacer = StartPacer()
    pairs = zip(own, handed, strict=True)
    starts = sorted(pacer.start(moment, HOUR) for pair in pairs for moment in pair)
    assert all(b - a >= PULSE_GAP for a, b in pairwise(starts))
    assert starts[-1] - own[-1] <= PULSE_GAP  # no growing delay


def agent(tmp_path) -> NodeAgent:
    proxy = ProxyConfig("http", "127.0.0.1", 9, "u", "p")
    settings = NodeSettings(
        "http://orchestrator",
        "t",
        provider="shifter",
        product="residential",
        region="fra1",
        proxy_factory=lambda country: proxy,
        work_dir=tmp_path,
    )
    return NodeAgent(settings)


async def test_a_late_fetch_never_opens_its_overdue_pulses_together(tmp_path, monkeypatch):
    node = agent(tmp_path)
    now = datetime.now(UTC)
    late = [
        assignment(batch_id=i, start_at=(now - timedelta(seconds=50) + STEP * i).isoformat())
        for i in range(8)
    ]
    census = assignment(batch_id=99, kind="census", start_at=now.isoformat())

    async def work(method, path, **options):
        return {"assignments": [*late, census], "kill": []}

    async def held(active):  # the run itself is not this test's
        await asyncio.sleep(0)

    monkeypatch.setattr(node, "_call", work)
    monkeypatch.setattr(node, "execute", held)
    await node.poll()
    await asyncio.sleep(0)
    starts = [node.active[i].start for i in range(8)]
    assert starts[0] >= now and starts[0] - now < timedelta(seconds=2)
    assert all(b - a == PULSE_GAP for a, b in pairwise(starts))
    assert node.active[99].start is None  # a census keeps its synchronized start


def test_a_pulse_run_has_a_hard_deadline_and_a_census_none(tmp_path):
    node = agent(tmp_path)
    deadline = node.run_options(assignment())["deadline"]
    assert abs(deadline - (time.time() + PULSE_RUN_SECONDS)) < 5
    assert node.run_options(assignment(kind="census")) == {}
