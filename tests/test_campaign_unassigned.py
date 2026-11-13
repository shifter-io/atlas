"""CAMP-1k: a worker we created whose project assignment is not confirmed is never left alive.

The c-4 run's nyc3 worker was created, its assignment step raised, and it was handed to the
guard as `start_failed`; the guard refused it (project=false) and it stayed up, billing. Here
the assignment is retried and verified, an unconfirmed worker is `assign_failed` with a ledger
mark, and the guard's own-unassigned path accepts that mark after a collector restart (a new
client). These tests run the launch and the collector's reconcile; the direct
`assign_and_verify_project` and guard tests are in `test_campaign_unassigned_scope.py`.
Offline: the fake DigitalOcean and fake remotes only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from campaign_fakes import (
    CAMPAIGN_PROJECT,
    PROD_PROJECT,
    SPEC_TIME,
    FakeDigitalOcean,
    Fault,
    running,
)
from campaign_hour_cases import launched
from campaign_unassigned_cases import (
    FIRST,
    LATE,
    READS,
    TRIES,
    audit_lines,
    no_replacement_spec,
    nyc3_spec,
)

from atlas.campaign import collector as collector_module
from atlas.campaign import harvest, launch, scope
from atlas.campaign.contracts import DropletRecord, audit_path
from atlas.campaign.tick import read_alerts


@pytest.mark.parametrize("failures", [TRIES, READS * TRIES], ids=["transient", "persistent"])
async def test_nyc3_a_failed_read_after_create_never_leaves_the_worker_alive(tmp_path, failures):
    """The c-4 sequence: created, then the assignment's fresh GET fails. Before CAMP-1k the
    error escaped, the worker became start_failed, the guard refused it (project=false) and it
    stayed alive outside Proxy Index. Now a transient failure is retried and the worker is
    confirmed; a persistent one is assign_failed and removed on the own-unassigned path."""
    fake = FakeDigitalOcean()
    fake.faults.append(Fault("GET", f"/v2/droplets/{FIRST}", 500, times=failures))
    record, root, remote, _ = await launched(tmp_path, fake, nyc3_spec())
    first = record.droplets[0]
    kinds = {a["kind"] for a in record.alerts}
    assert first.droplet_id == FIRST and not kinds & {"launch_error", "destroy_refused"}
    if failures == TRIES:
        assert first.status == "armed" and first.assigned_at and FIRST in fake.droplets
        assert FIRST in fake.projects[CAMPAIGN_PROJECT] and not fake.deleted
        return
    assert (first.status, first.error_class) == ("destroyed", "project_assign_failed")
    assert first.assign_failed_at and first.assigned_at is None
    assert FIRST not in fake.droplets and fake.deleted == [("droplet", FIRST)]
    last = audit_lines(audit_path(root))[-1]
    assert (last["path"], last["result"]) == ("own_unassigned", "deleted")
    replacement = record.droplets[-1]
    assert "project_assign_failed" in kinds and replacement.status == "armed"
    assert len(remote.pushes) == 1  # only the replacement was ever pushed


@pytest.mark.parametrize("pass_", ["reconcile", "harvest"])
async def test_a_restarted_collector_removes_an_unconfirmed_worker(tmp_path, pass_):
    """The launch could not confirm the assignment and its guarded removal failed; the next
    process (a new client, no in-memory proof) removes it through the own-unassigned path."""
    fake = FakeDigitalOcean()
    fake.faults += [
        Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 400, times=3),
        Fault("DELETE", f"/v2/droplets/{FIRST}", 500, times=TRIES),
    ]
    spec = no_replacement_spec()
    _, root, remote, clock = await launched(tmp_path, fake, spec, start=LATE)
    record = launch.load_hour(root, 0)  # the ledger on disk, as a restarted process reads it
    [worker] = record.droplets
    assert worker.status == "destroy_failed" and FIRST in fake.droplets
    assert FIRST in fake.projects[PROD_PROJECT]
    async with running(fake) as cloud:
        assert cloud.created_ids == frozenset()
        if pass_ == "reconcile":
            collector = collector_module.Collector(
                root, None, cloud, None, lambda: datetime.now(UTC), collector_module.Pacing()
            )
            await collector.reconcile(spec, {0: record})
        else:
            await harvest.harvest_hour(
                spec, record, root, cloud=cloud, remote_factory=lambda hour: remote, clock=clock
            )
    assert worker.status == "destroyed" and FIRST not in fake.droplets
    last = audit_lines(audit_path(root))[-1]
    assert (last["path"], last["result"]) == ("own_unassigned", "deleted")
    assert all(last["checks"].values())
    assert not {a["code"] for a in read_alerts(root)} & {"foreign_tagged", "destroy_refused"}
    assert worker.assign_failed_at  # the ledger's proof, written by the launch's own scope call


async def test_a_refused_removal_at_launch_is_alerted(tmp_path, monkeypatch):
    """Unconfirmed, and the guard refuses (here: older than 30 min): a high alert, no retry."""
    fake = FakeDigitalOcean()
    fake.faults.append(Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 400, times=3))
    monkeypatch.setattr(scope, "utcnow", lambda: datetime.fromisoformat("2027-01-01T23:00Z"))
    record, *_ = await launched(tmp_path, fake, no_replacement_spec(), start=LATE)
    assert [d.status for d in record.droplets] == ["destroy_refused"] and FIRST in fake.droplets
    assert [a["kind"] for a in record.alerts] == [
        "project_assign_failed",
        "destroy_refused",
        "cell_failed",
    ]


class Crash(BaseException):
    """The manager process dying: nothing after it runs, only what is on disk survives."""


async def test_a_crash_between_the_mark_and_the_delete_keeps_the_mark_on_disk(
    tmp_path, monkeypatch
):
    """CAMP-1r (a): the launch saved the hour only after `assign_and_verify_project` returned,
    which includes the delete. A crash in between left the worker `requested` and unmarked on
    disk: the resumed launch refused it (not this client's create) and it stayed alive. Now the
    mark is saved before the delete, and the restarted collector removes the worker."""
    fake = FakeDigitalOcean()
    fake.faults.append(Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 400, times=3))
    spec, real = no_replacement_spec(), scope.guarded_delete

    async def crash(*_args):
        monkeypatch.setattr(scope, "guarded_delete", real)
        raise Crash

    monkeypatch.setattr(scope, "guarded_delete", crash)
    with pytest.raises(Crash):
        await launched(tmp_path, fake, spec, start=LATE)
    root = tmp_path / "manager" / "campaigns" / spec.campaign_id
    [worker] = launch.load_hour(root, 0).droplets
    assert (worker.status, worker.droplet_id) == ("assign_failed", FIRST)
    assert worker.assign_failed_at and FIRST in fake.droplets
    record, *_ = await launched(tmp_path, fake, spec, start=LATE, record=launch.load_hour(root, 0))
    assert FIRST in fake.droplets and not fake.deleted  # the resumed launch left it alive
    async with running(fake) as cloud:
        collector = collector_module.Collector(
            root, None, cloud, None, lambda: datetime.now(UTC), collector_module.Pacing()
        )
        await collector.reconcile(spec, {0: record})
    assert record.droplets[0].status == "destroyed" and FIRST not in fake.droplets
    assert audit_lines(audit_path(root))[-1]["path"] == "own_unassigned"


@pytest.mark.parametrize("stale", [{"tags": []}, {"name": ""}], ids=["no_tags", "no_name"])
async def test_a_refusal_while_the_create_settles_is_retried_by_reconcile(tmp_path, stale):
    """CAMP-1r (b): DigitalOcean still lagged after the settle, so the guard refused our own
    marked worker and `destroy_refused` was final: it stayed alive until an operator acted. Now
    reconcile hands it to the guard again within 30 minutes, when its audit shows only checks
    that settle; the guard checks everything afresh."""
    fake = FakeDigitalOcean()
    fake.stale[FIRST] = [stale] * (READS + 1)  # the settle's reads and the guard's own GET
    spec = no_replacement_spec()
    _, root, _, _ = await launched(tmp_path, fake, spec, start=LATE)
    record = launch.load_hour(root, 0)
    [worker] = record.droplets
    assert worker.status == "destroy_refused" and worker.assign_failed_at
    refused = audit_lines(audit_path(root))[-1]
    assert refused["result"] == "refused" and FIRST in fake.droplets
    created_at = datetime.fromisoformat(fake.droplets[FIRST]["created_at"])
    async with running(fake) as cloud:
        collector = collector_module.Collector(
            root,
            None,
            cloud,
            None,
            lambda: created_at.replace(minute=59),
            collector_module.Pacing(),
        )
        await collector.reconcile(spec, {0: record})
    assert worker.status == "destroyed" and fake.deleted == [("droplet", FIRST)]
    last = audit_lines(audit_path(root))[-1]
    assert (last["path"], last["result"]) == ("own_unassigned", "deleted")


@pytest.mark.parametrize("case", ["too_old", "not_settling"])
async def test_reconcile_keeps_a_final_refusal_final(tmp_path, case):
    """Past the 30-minute own window, or refused on a check that does not settle, a marked
    `destroy_refused` worker is left to the operator, as before."""
    fake = FakeDigitalOcean()
    fake.stale[FIRST] = [{"tags": []}] * (READS + 1)
    spec = no_replacement_spec()
    _, root, _, _ = await launched(tmp_path, fake, spec, start=LATE)
    record = launch.load_hour(root, 0)
    created_at = datetime.fromisoformat(fake.droplets[FIRST]["created_at"])
    now = created_at + timedelta(minutes=31 if case == "too_old" else 0)
    if case == "not_settling":
        lines = audit_lines(audit_path(root))
        lines[-1]["checks"]["recorded"] = False
        audit_path(root).write_text("".join(json.dumps(line) + "\n" for line in lines))
    async with running(fake) as cloud:
        collector = collector_module.Collector(
            root, None, cloud, None, lambda: now, collector_module.Pacing()
        )
        await collector.reconcile(spec, {0: record})
    assert record.droplets[0].status == "destroy_refused" and FIRST in fake.droplets
    assert not fake.deleted


@pytest.mark.parametrize("status", ["destroy_refused", "running"])
async def test_reconcile_leaves_a_marked_worker_alone_unless_removable(tmp_path, status):
    """m2: a marked worker is retired only in assign_failed/destroy_failed/destroyed; a refused
    or live one is neither retried nor reported as foreign_tagged."""
    fake = FakeDigitalOcean()
    spec = no_replacement_spec()
    _, root, _, _ = await launched(tmp_path, fake, spec, start=LATE)
    record = launch.load_hour(root, 0)
    [worker] = record.droplets
    stray = fake.add(worker.name, fake.droplets[worker.droplet_id]["tags"], project=None)
    marked = DropletRecord(
        worker.network,
        worker.region,
        0,
        worker.name,
        stray,
        status=status,
        assign_failed_at=SPEC_TIME,
    )
    record.droplets.append(marked)
    async with running(fake) as cloud:
        collector = collector_module.Collector(
            root, None, cloud, None, lambda: datetime.now(UTC), collector_module.Pacing()
        )
        await collector.reconcile(spec, {0: record})
    assert stray in fake.droplets and marked.status == status and not fake.deleted
    assert "foreign_tagged" not in {a["code"] for a in read_alerts(root)}
