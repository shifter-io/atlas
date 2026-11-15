"""PULSE-STATE-1 (3/3): a node never opens its pulse runs together, and no pulse run outlives
PULSE_RUN_SECONDS.

A region's pulses are chained back to back, two runs every pulse.step_seconds (12 s: the two
targets half a step apart), so a node on time starts one run every 6 s. On 2026-11-15 the last
fra1 droplet to arm (11:00 and 12:00 webshare, node 71; 13:00 proxyempire, node 69) fetched its
first work 28 to 50 s after the first start: every run already due came in one answer and, their
start_at past, all opened in the same second (5 to 8 runs). That burst from a fresh address
tripped the network's burst limit; the node never recovered within the hour: its requests timed
out (10,038 of about 11,400), about 1,200 connections stayed open and runs that had all their
answers never ended. The same nodes on time in other hours ran clean.

So a node plans each pulse run's start (StartPacer): its start_at, but never before now and
never within PULSE_GAP of another planned start; a late node's backlog then starts one run every
PULSE_GAP from its first fetch, while a chain on time (6 s apart) starts exactly on schedule. A
run that starts later than its batch keeps within batch.start_grace_seconds (180 s) as long as
the node is less than about 3 minutes late. Each pulse run also gets a hard deadline (runner `deadline`, run_seconds after it starts: its
waves, requests over the concurrency it runs at, times its request timeout plus DEADLINE_MARGIN,
at least PULSE_RUN_SECONDS; 100 requests at 25 with a 10 s timeout take at most about 40 s): its
hung requests are cancelled, their connections closed, and what it measured is uploaded (the run
ends `cancelled`, stop_reason `deadline`). A paced start never lags its start_at by more than
MAX_LAG (PULSE-STATE-2), so a node with more than one run every PULSE_GAP overlaps some instead
of losing them to the reaper.
"""

from __future__ import annotations

import bisect
from datetime import datetime, timedelta

PULSE_GAP = timedelta(seconds=3)
PULSE_RUN_SECONDS = 120.0
# PULSE-STATE-2: a paced start never lags its start_at by more than this (the reaper fails a run
# not started batch.start_grace_seconds, 180 s, after it): a node given more than one pulse every
# PULSE_GAP (three chains, or pulse.step_seconds under 6) overlaps some rather than lose them.
MAX_LAG = timedelta(seconds=120)
DEADLINE_MARGIN = 30.0  # seconds past the waves a pulse run may need


def run_seconds(assignment: dict, concurrency: int) -> float:
    """A pulse run's hard deadline from its start: its waves (requests over the concurrency it
    runs at) times its request timeout plus DEADLINE_MARGIN, at least PULSE_RUN_SECONDS."""
    requests = int(assignment.get("requests") or 0)
    waves = -(-requests // max(1, concurrency))
    timeout = float(assignment.get("timeout_ms") or 0) / 1000
    return max(PULSE_RUN_SECONDS, waves * timeout + DEADLINE_MARGIN)


class StartPacer:
    """The starts a node has planned, and the next free one."""

    def __init__(self, gap: timedelta = PULSE_GAP) -> None:
        self.gap = gap
        self.planned: list[datetime] = []

    def start(self, scheduled: datetime, now: datetime) -> datetime:
        """The earliest moment from max(scheduled, now) at least `gap` from every start already
        planned, but never more than MAX_LAG after `scheduled` (nor before now); it is planned
        from then on."""
        self.planned = [moment for moment in self.planned if moment > now - self.gap]
        start = max(scheduled, now)
        for planned in self.planned:
            if planned + self.gap <= start:
                continue
            if planned - self.gap >= start:
                break
            start = planned + self.gap
        start = max(now, min(start, scheduled + MAX_LAG))
        bisect.insort(self.planned, start)
        return start
