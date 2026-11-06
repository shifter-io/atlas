"""CAMP-1k: `assign_and_verify_project` and the guard's own-unassigned path, called directly.

An unconfirmed worker of ours is `assign_failed` with a ledger mark that a new client accepts;
a foreign droplet that merely looks unassigned is still refused. The launch and reconcile tests
are in `test_campaign_unassigned.py`. Offline: the fake DigitalOcean only.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from campaign_fakes import (
    CAMPAIGN_PROJECT,
    CID,
    PROD_PROJECT,
    SID,
    SPEC_TIME,
    FakeDigitalOcean,
    Fault,
    running,
)
from campaign_unassigned_cases import (
    NAME,
    NO_WAIT,
    OURS,
    READS,
    SCOPE,
    TRIES,
    audit_lines,
    created,
)

from proxy_index.campaign import scope
from proxy_index.campaign.contracts import DropletRecord, HourRecord
from proxy_index.campaign.scope import (
    Ledger,
    OwnershipError,
    assign_and_verify_project,
    guarded_delete,
)


async def test_a_persistent_read_error_is_unconfirmed_not_raised(tmp_path):
    """Read again over about 30 s (CAMP-1l), then fail closed: assign_failed, marked and
    removed, with the read failures kept."""
    fake, audit, sleeps = FakeDigitalOcean(), tmp_path / "audit.jsonl", []

    async def sleep(seconds):
        sleeps.append(seconds)

    async with running(fake) as cloud:
        droplet, records = await created(cloud)
        fake.faults.append(Fault("GET", f"/v2/droplets/{droplet.id}", 500, times=READS * TRIES))
        confirmed, failed = await assign_and_verify_project(
            cloud, SCOPE, [droplet.id], records, audit, retry=scope.Retry(sleep=sleep)
        )
    record = records[droplet.id]
    assert not confirmed and failed == {droplet.id} and sleeps == list(scope.SETTLE_DELAYS)
    assert sum(sleeps) == 30.0 and record.error_detail["failed"] == ["readable"]
    assert record.error_detail["http"].count(f"GET /v2/droplets/{droplet.id} 500") == READS * TRIES
    assert record.status == "destroyed" and record.assign_failed_at
    assert not [r for r in fake.requests if r[0] == "POST" and "/projects/" in r[1]]
    assert audit_lines(audit)[-1]["path"] == "own_unassigned"


async def test_a_droplet_not_yet_readable_and_a_project_read_error_are_retried(tmp_path):
    """A 404 just after the create and a failed project check are retried, then confirmed."""
    fake = FakeDigitalOcean()
    async with running(fake) as cloud:
        droplet, records = await created(cloud)
        fake.faults += [
            Fault("GET", f"/v2/droplets/{droplet.id}", 404),
            Fault("GET", f"/v2/projects/{CAMPAIGN_PROJECT}", 500, times=TRIES),
        ]
        confirmed, failed = await assign_and_verify_project(
            cloud, SCOPE, [droplet.id], records, tmp_path / "a", retry=NO_WAIT
        )
    assert confirmed == {droplet.id} and not failed and records[droplet.id].status == "assigned"
    assert records[droplet.id].assign_failed_at is None


async def test_a_droplet_that_never_appears_ends_absent(tmp_path):
    fake = FakeDigitalOcean()
    async with running(fake) as cloud:
        droplet, records = await created(cloud)
        fake.droplets.pop(droplet.id)
        _, failed = await assign_and_verify_project(
            cloud, SCOPE, [droplet.id], records, tmp_path / "a", retry=NO_WAIT
        )
    assert failed == {droplet.id} and records[droplet.id].status == "destroyed"
    assert audit_lines(tmp_path / "a")[-1]["result"] == "absent"


@pytest.mark.parametrize("failures", [TRIES, 3 * TRIES], ids=["transient", "persistent"])
async def test_an_unreadable_foreign_id_is_refused_before_any_post(tmp_path, failures):
    """Retrying is only for our own ids: a foreign id with a failing GET is still refused, even
    when every read fails (m1)."""
    fake = FakeDigitalOcean()
    lookalike = fake.add(NAME, OURS, project=None)
    fake.faults.append(Fault("GET", f"/v2/droplets/{lookalike}", 500, times=failures))
    records = {lookalike: DropletRecord("shifter", "nyc3", 3, NAME, lookalike)}
    async with running(fake) as cloud:
        with pytest.raises(OwnershipError) as refused:
            await assign_and_verify_project(
                cloud, SCOPE, [lookalike], records, tmp_path / "a", retry=scope.Retry(delay=0)
            )
    assert refused.value.failed == ["own_create"] and lookalike in fake.droplets
    assert records[lookalike].assign_failed_at is None and not fake.deleted
    assert not [r for r in fake.requests if r[0] == "POST"]


async def test_the_mark_writer_refuses_an_id_it_did_not_create(tmp_path, monkeypatch):
    """m1: even if the read step let a foreign id through, the writer itself refuses to mark it."""
    fake = FakeDigitalOcean()
    lookalike = fake.add(NAME, OURS, project=None)
    records = {lookalike: DropletRecord("shifter", "nyc3", 3, NAME, lookalike)}

    async def unreadable(*_args):
        return ["readable"]

    monkeypatch.setattr(scope, "_fresh_owned", unreadable)
    async with running(fake) as cloud:
        with pytest.raises(OwnershipError) as refused:
            await assign_and_verify_project(
                cloud, SCOPE, [lookalike], records, tmp_path / "a", retry=NO_WAIT
            )
    assert refused.value.failed == ["own_create"] and records[lookalike].assign_failed_at is None
    assert records[lookalike].status == "requested" and not fake.deleted


async def test_the_ledger_mark_survives_a_new_client(tmp_path):
    """The mark written by assign_and_verify_project is the proof a restarted process uses."""
    fake, audit = FakeDigitalOcean(), tmp_path / "audit.jsonl"
    fake.faults.append(Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 400, times=3))
    async with running(fake) as cloud:
        droplet, records = await created(cloud)
        fake.faults.append(Fault("DELETE", f"/v2/droplets/{droplet.id}", 500, times=TRIES))
        await assign_and_verify_project(
            cloud, SCOPE, [droplet.id], records, audit, retry=scope.Retry(delay=0)
        )
    assert records[droplet.id].status == "destroy_failed"
    reloaded = {droplet.id: DropletRecord(**json.loads(json.dumps(vars(records[droplet.id]))))}
    async with running(fake) as cloud:
        result = await guarded_delete(
            cloud, "droplet", droplet.id, SCOPE, Ledger({"droplet": [droplet.id]}, reloaded), audit
        )
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
        "default_project": DropletRecord(
            "shifter", "nyc3", 3, NAME, found["default_project"], status="assign_failed"
        ),  # set by hand, not by scope
        "no_project": DropletRecord(
            "shifter", "nyc3", 3, NAME, found["no_project"], status="running"
        ),  # an adopted orphan's record
        # Our own marked record under the lookalike's id: the name differs (exact name check).
        "other_name": DropletRecord(
            "shifter",
            "nyc3",
            3,
            NAME,
            found["other_name"],
            status="assign_failed",
            assign_failed_at=SPEC_TIME,
        ),
    }
    return found, {found[key]: record for key, record in records.items()} | {
        ours_id: DropletRecord("shifter", "nyc3", 3, NAME, ours_id)
    }


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
        await assign_and_verify_project(
            cloud, SCOPE, [droplet.id], records, audit, retry=scope.Retry(attempts=1)
        )
        with pytest.raises(OwnershipError):  # recorded id, but no record to carry the mark
            await guarded_delete(
                cloud, "droplet", droplet.id, SCOPE, Ledger({"droplet": [droplet.id]}), audit
            )
        monkeypatch.setattr(scope, "utcnow", lambda: datetime.fromisoformat("2027-01-01T22:29Z"))
        with pytest.raises(OwnershipError):
            await guarded_delete(
                cloud,
                "droplet",
                droplet.id,
                SCOPE,
                Ledger({"droplet": [droplet.id]}, records),
                audit,
            )
    assert droplet.id in fake.droplets


def test_an_hour_written_before_the_mark_still_loads():
    assert (
        HourRecord.from_dict(
            {
                "campaign_id": CID,
                "hour": 0,
                "scheduled_at": SPEC_TIME,
                "droplets": [{"network": "n", "region": "nyc3", "hour": 0, "name": NAME}],
            }
        )
        .droplets[0]
        .assign_failed_at
        is None
    )
