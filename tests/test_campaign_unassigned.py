"""CAMP-1k: a worker we created whose project assignment is not confirmed is never left alive.

The c-4 run's nyc3 worker was created, its assignment step raised, and it was handed to the
guard as `start_failed`; the guard refused it (project=false) and it stayed up, billing. Here
the assignment is retried and verified, an unconfirmed worker is `assign_failed` with a ledger
mark, and the guard's own-unassigned path accepts that mark after a collector restart (a new
client). A foreign droplet that merely looks unassigned is still refused. Offline: the fake
DigitalOcean and fake remotes only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from campaign_fakes import (
    CAMPAIGN_PROJECT,
    CID,
    MANAGER_ID,
    PROD_PROJECT,
    SID,
    SPEC_TIME,
    FakeDigitalOcean,
    Fault,
    running,
)
from test_campaign_hour import NETWORKS, launched, make_spec

from proxy_index.campaign import collector as collector_module
from proxy_index.campaign import harvest, launch, scope
from proxy_index.campaign.contracts import (
    WORKER_TAG,
    DropletRecord,
    HourRecord,
    Scope,
    audit_path,
)
from proxy_index.campaign.scope import (
    Ledger,
    OwnershipError,
    assign_and_verify_project,
    guarded_delete,
)
from proxy_index.campaign.tick import read_alerts

FIRST = "700001"  # the fake's first droplet id: the hour's only worker
LATE = datetime.fromisoformat("2027-01-01T22:16:00+00:00").timestamp()  # past HH:15: no replacement
TRIES = 5  # one `request` call: the first try and four backoff retries
SCOPE = Scope(CID, SID, CAMPAIGN_PROJECT, MANAGER_ID, datetime.fromisoformat(SPEC_TIME))
OURS = [WORKER_TAG, f"pi-camp-{SID}", f"pi-camp-{SID}-h03"]
NAME = f"pic-{SID}-h03-nyc3-shifter"


def audit_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def nyc3_spec():
    return make_spec(NETWORKS[:1], ("nyc3",))


@pytest.mark.parametrize("failures", [TRIES, 3 * TRIES], ids=["transient", "persistent"])
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
    fake.faults += [Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 400, times=3),
                    Fault("DELETE", f"/v2/droplets/{FIRST}", 500, times=TRIES)]
    spec = nyc3_spec()
    _, root, remote, clock = await launched(tmp_path, fake, spec, start=LATE)
    record = launch.load_hour(root, 0)  # the ledger on disk, as a restarted process reads it
    [worker] = record.droplets
    assert worker.status == "destroy_failed" and FIRST in fake.droplets
    assert FIRST in fake.projects[PROD_PROJECT]
    async with running(fake) as cloud:
        assert cloud.created_ids == frozenset()
        if pass_ == "reconcile":
            collector = collector_module.Collector(
                root, None, cloud, None, lambda: datetime.now(UTC), collector_module.Pacing())
            await collector.reconcile(spec, {0: record})
        else:
            await harvest.harvest_hour(spec, record, root, cloud=cloud,
                                       remote_factory=lambda hour: remote, clock=clock)
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
    record, *_ = await launched(tmp_path, fake, nyc3_spec(), start=LATE)
    assert [d.status for d in record.droplets] == ["destroy_refused"] and FIRST in fake.droplets
    assert [a["kind"] for a in record.alerts] == [
        "project_assign_failed", "destroy_refused", "cell_failed"]


async def created(cloud, name=NAME):
    [droplet] = await cloud.create([name], region="nyc3", size="c-4", image="1", tags=OURS,
                                   ssh_keys=["9"])
    return droplet, {droplet.id: DropletRecord("shifter", "nyc3", 3, name, droplet.id)}


async def test_a_persistent_read_error_is_unconfirmed_not_raised(tmp_path):
    """Retried 3 times 5 s apart, then fail closed: assign_failed, marked and removed."""
    fake, audit, sleeps = FakeDigitalOcean(), tmp_path / "audit.jsonl", []

    async def sleep(seconds):
        sleeps.append(seconds)

    async with running(fake) as cloud:
        droplet, records = await created(cloud)
        fake.faults.append(Fault("GET", f"/v2/droplets/{droplet.id}", 500, times=3 * TRIES))
        confirmed, failed = await assign_and_verify_project(
            cloud, SCOPE, [droplet.id], records, audit, retry=scope.Retry(sleep=sleep))
    record = records[droplet.id]
    assert not confirmed and failed == {droplet.id} and sleeps == [5.0, 5.0]
    assert record.status == "destroyed" and record.assign_failed_at
    assert not [r for r in fake.requests if r[0] == "POST" and "/projects/" in r[1]]
    assert audit_lines(audit)[-1]["path"] == "own_unassigned"


async def test_a_droplet_not_yet_readable_and_a_project_read_error_are_retried(tmp_path):
    """A 404 just after the create and a failed project check are retried, then confirmed."""
    fake = FakeDigitalOcean()
    async with running(fake) as cloud:
        droplet, records = await created(cloud)
        fake.faults += [Fault("GET", f"/v2/droplets/{droplet.id}", 404),
                        Fault("GET", f"/v2/projects/{CAMPAIGN_PROJECT}", 500, times=TRIES)]
        confirmed, failed = await assign_and_verify_project(
            cloud, SCOPE, [droplet.id], records, tmp_path / "a", retry=scope.Retry(delay=0))
    assert confirmed == {droplet.id} and not failed and records[droplet.id].status == "assigned"
    assert records[droplet.id].assign_failed_at is None


async def test_a_droplet_that_never_appears_ends_absent(tmp_path):
    fake = FakeDigitalOcean()
    async with running(fake) as cloud:
        droplet, records = await created(cloud)
        fake.droplets.pop(droplet.id)
        _, failed = await assign_and_verify_project(
            cloud, SCOPE, [droplet.id], records, tmp_path / "a", retry=scope.Retry(delay=0))
    assert failed == {droplet.id} and records[droplet.id].status == "destroyed"
    assert audit_lines(tmp_path / "a")[-1]["result"] == "absent"


async def test_an_unreadable_foreign_id_is_refused_before_any_post(tmp_path):
    """Retrying is only for our own ids: a foreign id with a failing GET is still refused."""
    fake = FakeDigitalOcean()
    lookalike = fake.add(NAME, OURS, project=PROD_PROJECT)
    fake.faults.append(Fault("GET", f"/v2/droplets/{lookalike}", 500, times=TRIES))
    records = {lookalike: DropletRecord("shifter", "nyc3", 3, NAME, lookalike)}
    async with running(fake) as cloud:
        with pytest.raises(OwnershipError) as refused:
            await assign_and_verify_project(cloud, SCOPE, [lookalike], records, tmp_path / "a")
    assert refused.value.failed == ["own_create"] and lookalike in fake.droplets
    assert records[lookalike].assign_failed_at is None
    assert not [r for r in fake.requests if r[0] == "POST"]


async def test_the_ledger_mark_survives_a_new_client(tmp_path):
    """The mark written by assign_and_verify_project is the proof a restarted process uses."""
    fake, audit = FakeDigitalOcean(), tmp_path / "audit.jsonl"
    fake.faults.append(Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 400, times=3))
    async with running(fake) as cloud:
        droplet, records = await created(cloud)
        fake.faults.append(Fault("DELETE", f"/v2/droplets/{droplet.id}", 500, times=TRIES))
        await assign_and_verify_project(cloud, SCOPE, [droplet.id], records, audit,
                                        retry=scope.Retry(delay=0))
    assert records[droplet.id].status == "destroy_failed"
    reloaded = {droplet.id: DropletRecord(**json.loads(json.dumps(vars(records[droplet.id]))))}
    async with running(fake) as cloud:
        result = await guarded_delete(cloud, "droplet", droplet.id, SCOPE,
                                      Ledger({"droplet": [droplet.id]}, reloaded), audit)
    assert result == "deleted" and droplet.id not in fake.droplets
    assert audit_lines(audit)[-1]["path"] == "own_unassigned"


def lookalikes(fake, ours_id):
    """Foreign droplets that look unassigned, each with the ledger state a production fault
    could leave; none carries the mark only assign_and_verify_project writes for our ids."""
    found = {}
    for key, project in (("default_project", PROD_PROJECT), ("no_project", None)):
        found[key] = fake.add(NAME, OURS, project=project)
    found["other_name"] = fake.add(f"pic-{SID}-h03-nyc3-geonode", OURS, project=None)
    records = {
        "default_project": DropletRecord("shifter", "nyc3", 3, NAME, found["default_project"],
                                         status="assign_failed"),  # set by hand, not by scope
        "no_project": DropletRecord("shifter", "nyc3", 3, NAME, found["no_project"],
                                    status="running"),  # an adopted orphan's record
        # Our own marked record under the lookalike's id: the name differs (exact name check).
        "other_name": DropletRecord("shifter", "nyc3", 3, NAME, found["other_name"],
                                    status="assign_failed", assign_failed_at=SPEC_TIME),
    }
    return found, {found[key]: record for key, record in records.items()} | {
        ours_id: DropletRecord("shifter", "nyc3", 3, NAME, ours_id)}


@pytest.mark.parametrize("fresh_client", [False, True], ids=["same_process", "restarted"])
async def test_a_foreign_droplet_that_looks_unassigned_is_refused(tmp_path, fresh_client):
    fake, audit = FakeDigitalOcean(), tmp_path / "audit.jsonl"
    fake.seed_production(200)
    async with running(fake) as cloud:
        droplet, _ = await created(cloud)
        found, records = lookalikes(fake, droplet.id)
        ledger = Ledger({"droplet": list(records)}, records)
        async with running(fake) as other:
            client = other if fresh_client else cloud
            for key, target in sorted(found.items()):
                with pytest.raises(OwnershipError) as refused:
                    await guarded_delete(client, "droplet", target, SCOPE, ledger, audit)
                assert refused.value.failed == ["project"], key
    assert set(found.values()) <= set(fake.droplets) and not fake.deleted
    assert {line["path"] for line in audit_lines(audit)} == {"normal"}
    assert len([d for d in fake.droplets.values() if d["name"].startswith("prod-")]) == 200


async def test_an_old_mark_or_a_lost_record_is_refused(tmp_path, monkeypatch):
    fake, audit = FakeDigitalOcean(), tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        droplet, records = await created(cloud)
        fake.assign_delay = 99
        fake.faults.append(Fault("DELETE", f"/v2/droplets/{droplet.id}", 500, times=TRIES))
        await assign_and_verify_project(cloud, SCOPE, [droplet.id], records, audit,
                                        retry=scope.Retry(attempts=1))
        with pytest.raises(OwnershipError):  # recorded id, but no record to carry the mark
            await guarded_delete(cloud, "droplet", droplet.id, SCOPE,
                                 Ledger({"droplet": [droplet.id]}), audit)
        monkeypatch.setattr(scope, "utcnow", lambda: datetime.fromisoformat("2027-01-01T22:29Z"))
        with pytest.raises(OwnershipError):
            await guarded_delete(cloud, "droplet", droplet.id, SCOPE,
                                 Ledger({"droplet": [droplet.id]}, records), audit)
    assert droplet.id in fake.droplets


def test_an_hour_written_before_the_mark_still_loads():
    assert HourRecord.from_dict(
        {"campaign_id": CID, "hour": 0, "scheduled_at": SPEC_TIME,
         "droplets": [{"network": "n", "region": "nyc3", "hour": 0, "name": NAME}]}
    ).droplets[0].assign_failed_at is None
