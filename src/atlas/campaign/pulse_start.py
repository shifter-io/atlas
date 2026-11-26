"""PULSE-ARM-1: starting a pulse node whose SSH session failed (pulse_fleet arming).

A `push_start` that fails over SSH (ssh's own exit 255: a lost session or the time limit; any
RemoteError or OSError) does not say whether the node started. On 2026-11-20 a stall on the
network path to one region lost the reply of a start whose node service ran and heartbeated,
yet the node was marked start_failed and got no work for the hour; on 2026-11-21 one SSH session
lost its path for a moment and the node never started. The daily launcher already treats such a
start as unconfirmed (launch_steps, `start_unconfirmed`).

So an SSH failure is checked: the node is waited for up to HEARTBEAT_WAIT_SECONDS (six of its
10-second heartbeats) for a heartbeat newer than its token, which arming rotated just before, so
only this droplet can send it. A heartbeat seen arms the node (`heartbeat`). With none, the push
and start runs once more on the droplet already up, with the same bundle and token (`retried`
when it answers), and a failed retry is checked the same way; only then is the error raised and
the node start_failed (pulse_replace replaces it). A start that really failed is so failed within
two push time limits and two heartbeat waits. Any other error (a refused token issue, a bad
input) fails at once. Without a heartbeat reader (`pulse-fleet reconcile` has no orchestrator
database) an SSH failure is only retried.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from .contracts import Droplet
from .fanout import RemoteError

HEARTBEAT_WAIT_SECONDS = 60.0
HEARTBEAT_POLL_SECONDS = 5.0
ATTEMPTS = 2  # the first push and start, and one retry
Beats = Callable[[int, datetime], Awaitable[bool]]  # (node id, token issued at) -> heartbeat since
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Start:
    """One node's start: its droplet, the stdin bundle, the command and its token's node row."""

    droplet: Droplet
    payload: bytes
    command: list[str]
    node_id: int | None
    since: datetime  # when its token was issued: an older heartbeat is another droplet's


async def push_start(remote, clock, beats: Beats | None, start: Start) -> str | None:
    """Push and start the node; None when the first start answered, `retried` when the retry
    did, `heartbeat` when a heartbeat confirmed a start whose SSH failed. Raises the last SSH
    error when no start answered and the node never heartbeated."""
    for attempt in range(ATTEMPTS):
        try:
            await remote.push_start(start.droplet, start.payload, start.command)
            return "retried" if attempt else None
        except (RemoteError, OSError) as error:  # ssh or its process: started or not, unknown
            LOG.warning("pulse node %s start unconfirmed (%s), waiting for its heartbeat",
                        start.droplet.name, error)  # fmt: skip
            if await heartbeat(clock, beats, start):
                return "heartbeat"
            if attempt == ATTEMPTS - 1:
                raise
    raise AssertionError("unreachable: the last attempt returns or raises")


async def heartbeat(clock, beats: Beats | None, start: Start) -> bool:
    """Whether the node heartbeats after its token's issue within HEARTBEAT_WAIT_SECONDS."""
    if beats is None or start.node_id is None:
        return False
    deadline = clock.now() + HEARTBEAT_WAIT_SECONDS
    while True:
        if await beats(start.node_id, start.since):
            return True
        if clock.now() + HEARTBEAT_POLL_SECONDS > deadline:
            return False
        await clock.sleep(HEARTBEAT_POLL_SECONDS)
