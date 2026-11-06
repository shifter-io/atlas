"""Shared fakes and helpers of the `campaign shot` tests: a fake remote that answers each
step per (network, region) and records the clock at each start."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

from campaign_fakes import running
from campaign_probe_cases import NOW, FakeProbeRemote, world

from proxy_index.campaign import shot as shooting
from proxy_index.campaign.fanout import RemoteError
from proxy_index.campaign.shot import ShotInputs

START = NOW.timestamp() + shooting.LEAD_SECONDS
SIZES = [{"slug": slug, "available": True, "regions": ["lon1", "nyc3"]} for slug in ("c-2", "c-4")]


def cell_of(droplet):
    """(network, region) from a worker name `pic-<sid>-h00-<region>-<network>`."""
    region, network = droplet.name.split("-h00-")[1].split("-", 1)
    return network, region


class ShotRemote(FakeProbeRemote):
    """Also records the clock at each step and answers per (network, region)."""

    def __init__(self, clock, answers=None, unready=(), setup_fails=()):
        super().__init__({})
        self.clock, self.by_cell = clock, answers or {}
        self.unready, self.setup_fails, self.started = unready, setup_fails, {}

    async def wait_ready(self, droplet, limit_seconds):
        await asyncio.sleep(0)  # SSH yields: every worker gets ready before the first sleeps
        return cell_of(droplet) not in self.unready

    async def run(self, droplet, command, what, *, stdin=None, seconds=120):
        await asyncio.sleep(0)
        cell = cell_of(droplet)
        if command[0] == "python3" and cell in self.setup_fails:
            raise RemoteError("probe setup failed (exit 1)")
        if command[0] != "python3":
            self.started[cell] = self.clock.now()
            answer = self.by_cell.get(cell)
            if answer is not None:
                self.calls.append((command, stdin, what))
                return answer if isinstance(answer, bytes) else (json.dumps(answer) + "\n").encode()
        return await super().run(droplet, command, what, stdin=stdin, seconds=seconds)


def shot_world(tmp_path, **changes):
    probe_inputs, spec = world(tmp_path)
    inputs = ShotInputs(
        probe_inputs.root, tmp_path / "manager" / "shots", size_by_network={"soax": "c-2"}
    )
    return replace(inputs, **changes), spec


async def fired(fake, remote, inputs, clock):
    if fake.sizes[0]["regions"] != ["lon1", "nyc3"] and not getattr(fake, "keep_sizes", False):
        fake.sizes = SIZES
    async with running(fake) as cloud:
        return await shooting.shot(cloud, remote.factory, inputs, clock=clock)
