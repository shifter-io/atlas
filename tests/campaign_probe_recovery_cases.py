"""Fakes for the `campaign probe` worker and recovery tests: the POSTs a fake cloud saw (both
files), and for the recovery a cloud whose tag listings miss a lost create, a recorded sleep
and a probe process that dies at a given stage.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from campaign_fakes import FakeDigitalOcean, no_sleep, running
from campaign_probe_cases import NOW, FakeProbeRemote

from atlas.campaign import probe as probing
from atlas.campaign.probe import ProbeError


def posts(fake, path):
    return [r for r in fake.requests if r[0] == "POST" and r[1] == path]


class Hidden(FakeDigitalOcean):
    """The create answer is lost and the first `hide` tag listings do not show it yet."""

    def __init__(self, hide):
        super().__init__()
        self.hide, self.create_then_fail = hide, 1

    async def _list(self, request):
        if self.hide and request.query.get("tag_name"):
            self.hide -= 1
            return web.json_response({"droplets": [], "meta": {"total": 0}})
        return await super()._list(request)


def sleeps_into(seen):
    async def sleep(seconds):
        seen.append(seconds)

    return sleep


async def died(fake, inputs, monkeypatch, stage):
    """A probe process that stops for good (SIGKILL, OOM, reboot) at `stage`: no cleanup."""
    if stage == "before_assignment":

        async def gone(*args, **kwargs):
            raise RuntimeError("killed")

        monkeypatch.setattr(probing.scope, "assign_and_verify_project", gone)
    async with running(fake) as cloud:
        run = probing.Probe(cloud, FakeProbeRemote({}).factory, inputs, NOW, no_sleep)
        with pytest.raises(RuntimeError if stage == "before_assignment" else ProbeError):
            await run.create()
            raise ProbeError("killed after the assignment")
    monkeypatch.undo()
    return inputs.output / run.probe_id
