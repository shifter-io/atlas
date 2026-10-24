"""CAMP-1k: no region loses its hourly test to a failed worker.

A create, assignment, boot, push or start failure gets a replacement in the same hour, up to
`launch.REPLACEMENTS_PER_CELL` per cell before HH:00 + `replace_until_minutes`; a replacement
adopts its own name before it creates anything. Only a spent budget makes the cell
`failed_ours`, with the high alert `cell_failed`. Offline: fake DigitalOcean and remotes only.
"""

from __future__ import annotations

import json
from datetime import datetime

from aiohttp import web
from campaign_fakes import CAMPAIGN_PROJECT, FakeDigitalOcean, Fault
from test_campaign_hour import (
    NETWORKS,
    ended,
    harvest_setup,
    harvested,
    launched,
    make_spec,
)

from proxy_index.campaign import launch
from proxy_index.campaign.contracts import audit_path

LATE_START = datetime.fromisoformat("2027-01-01T22:01:00+00:00").timestamp()  # past HH:00
CELL = "net00/nyc3"


def nyc3_spec():
    return make_spec(NETWORKS[:1], ("nyc3",))


async def test_a_failed_assignment_is_replaced_and_the_cell_completes(tmp_path):
    """The nyc3 case: never confirmed, removed on the own-unassigned path; the replacement
    runs the round and the harvest completes the cell."""
    fake = FakeDigitalOcean()
    fake.faults.append(Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 400, times=3))
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path, fake=fake)
    first, replacement = record.droplets
    assert (first.status, first.error_class) == ("destroyed", "project_assign_failed")
    assert replacement.replaced_from == 0 and replacement.status == "armed"
    assert record.replacements == {"net00/tor1": {
        "replaced_from": [{"droplet_id": first.droplet_id, "status": "destroyed",
                           "error_class": "project_assign_failed"}],
        "extra_delay_seconds": 0.0}}  # armed before HH:00: the round starts on time
    audit = [json.loads(line) for line in audit_path(root).read_text().splitlines()]
    assert [(a["id"], a["path"]) for a in audit if a["result"] == "deleted"] == [
        (first.droplet_id, "own_unassigned")]
    remote.inspected[replacement.name] = ended()
    await harvested(spec, record, root, fake, remote, clock)
    assert record.cells == {"net00/tor1": "complete"} and record.state == "done"
    assert replacement.status == "destroyed" and replacement.successes == 6
    assert "cell_failed" not in {a["kind"] for a in record.alerts}


async def test_a_create_5xx_is_replaced_and_its_delay_recorded(tmp_path):
    fake = FakeDigitalOcean()
    fake.faults.append(Fault("POST", "/v2/droplets", 500, times=2))  # both create attempts
    record, _, remote, _ = await launched(tmp_path, fake, nyc3_spec(), start=LATE_START)
    first, replacement = record.droplets
    assert (first.status, first.droplet_id) == ("create_failed", None)
    assert replacement.replaced_from == 0 and replacement.status == "armed"
    assert record.cells == {CELL: "running"} and len(fake.droplets) == 1
    assert [name for name, *_ in remote.pushes] == [replacement.name]
    entry = record.replacements[CELL]
    assert entry["replaced_from"] == [
        {"droplet_id": None, "status": "create_failed", "error_class": "create_failed"}]
    assert entry["extra_delay_seconds"] > 60  # launched at HH:01, then 15 s create retry
    assert {a["kind"] for a in record.alerts} == {"create_failed"}


class LandsLate(FakeDigitalOcean):
    """The first create lands but answers 500 and stays unlisted through the first worker's
    lists; the second create is refused outright. Only the replacement's adopt can see it."""

    creates, hidden_lists = 0, 4  # the launch's resume adopt, then three after creates

    async def _create(self, request):
        self.creates += 1
        if self.creates == 1:
            await super()._create(request)
            self.late = set(self.droplets)
            return web.json_response({"id": "server_error"}, status=500)
        if self.creates == 2:
            return web.json_response({"id": "server_error"}, status=500)
        return await super()._create(request)

    async def _list(self, request):
        if self.hidden_lists:
            self.hidden_lists -= 1
            return self._page(request, "droplets", [])
        return await super()._list(request)


async def test_a_replacement_adopts_a_create_that_landed_late(tmp_path):
    """adopt_uncertain before any re-create: the late droplet is used, never duplicated."""
    fake = LandsLate()
    record, *_ = await launched(tmp_path, fake, nyc3_spec())
    first, replacement = record.droplets
    assert first.status == "create_failed" and fake.creates == 2
    assert replacement.droplet_id in fake.late and replacement.status == "armed"
    assert len(fake.droplets) == 1 and record.cells == {CELL: "running"}


async def test_spent_replacements_make_the_cell_failed_ours_with_an_alert(tmp_path):
    fake = FakeDigitalOcean()
    fake.faults.append(Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 400, times=99))
    record, root, remote, _ = await launched(tmp_path, fake, nyc3_spec())
    tries = 1 + launch.REPLACEMENTS_PER_CELL
    assert [(d.status, d.error_class) for d in record.droplets] == [
        ("destroyed", "project_assign_failed")] * tries
    assert [d.replaced_from for d in record.droplets] == [None, *range(tries - 1)]
    assert record.cells == {CELL: "failed_ours"} and not fake.droplets and not remote.pushes
    entry = record.replacements[CELL]
    assert len(entry["replaced_from"]) == launch.REPLACEMENTS_PER_CELL
    assert entry["extra_delay_seconds"] is None
    assert [a["kind"] for a in record.alerts] == ["project_assign_failed", "cell_failed"]
    assert [a["level"] for a in record.alerts] == ["high", "high"]


def test_the_replacement_budget_is_three_per_cell():
    """r1: the owner's bound; the tests above derive their counts from it."""
    assert launch.REPLACEMENTS_PER_CELL == 3
