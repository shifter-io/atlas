"""ERR-1: the guard's, the probe's, the image build's and the teardown's error boundaries.

Each catches every error on purpose (`# boundary:` in the source): a removal or a fail-closed
assignment must never be skipped by an unexpected error, and one resource's failure never
stops the others. Each records the error's class and never its message. Offline: the fake
DigitalOcean and fake remotes only.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from campaign_deploy_fakes import Shell, clean_report, fast_assign, make_job, owned_world
from campaign_fakes import (
    CAMPAIGN_PROJECT,
    CID,
    MANAGER_ID,
    SID,
    SPEC_TIME,
    FakeDigitalOcean,
    running,
)
from campaign_probe_cases import FakeProbeRemote, probed, world
from test_campaign_image import PUBLIC_KEY, inventory, run_build

from proxy_index.campaign import ops_api, scope, teardown
from proxy_index.campaign import probe as probing
from proxy_index.campaign.contracts import WORKER_TAG, DropletRecord, Scope
from proxy_index.campaign.image import ImageJob

SCOPE = Scope(CID, SID, CAMPAIGN_PROJECT, MANAGER_ID, datetime.fromisoformat(SPEC_TIME))
OURS = [WORKER_TAG, f"pi-camp-{SID}", f"pi-camp-{SID}-h03"]
NO_WAIT = scope.Retry(delay=0, settle=(0.0,) * len(scope.SETTLE_DELAYS))
SECRET = "to 203.0.113.9 with token " + "x" * 40  # neither part may be kept


class Unexpected(Exception):
    """No narrowed handler names this type: only a boundary catches it."""


async def raise_unexpected(*_args, **_kwargs):
    raise Unexpected(SECRET)


def failing(real, times):
    """`real`, raising `Unexpected` on its first `times` calls."""
    calls = []

    async def call(*args, **kwargs):
        calls.append(1)
        if len(calls) <= times:
            raise Unexpected(SECRET)
        return await real(*args, **kwargs)

    return call


async def created(cloud, count=1):
    names = [f"pic-{SID}-h03-{region}-shifter" for region in ("nyc3", "lon1")[:count]]
    droplets = await cloud.create(
        names, region="nyc3", size="c-4", image="1", tags=OURS, ssh_keys=["9"]
    )
    return [d.id for d in droplets], {
        d.id: DropletRecord("shifter", "nyc3", 3, d.name, d.id) for d in droplets
    }


def audit_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize("call", ["project_name", "assign"])
async def test_an_assignment_error_of_any_type_fails_closed_and_keeps_its_class(
    tmp_path, monkeypatch, call
):
    fake, audit = FakeDigitalOcean(), tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        [rid], records = await created(cloud)
        # Every try of the assignment raises; the guard's own later check is answered.
        target = scope if call == "project_name" else cloud
        name = "_project_named" if call == "project_name" else "assign_project"
        monkeypatch.setattr(target, name, failing(getattr(target, name), NO_WAIT.attempts))
        confirmed, failed = await scope.assign_and_verify_project(
            cloud, SCOPE, [rid], records, audit, retry=NO_WAIT
        )
    record = records[rid]
    assert (confirmed, failed) == (frozenset(), frozenset({rid}))
    assert (record.status, record.error_class) == ("destroyed", "project_assign_failed")
    assert record.error_detail["assign_error"] == "Unexpected"
    last = audit_lines(audit)[-1]
    assert (last["path"], last["result"]) == ("own_unassigned", "deleted")
    assert last["cause"]["assign_error"] == "Unexpected" and "x" * 40 not in audit.read_text()


async def test_an_assignment_without_an_error_records_no_error_class(tmp_path):
    fake = FakeDigitalOcean()
    async with running(fake) as cloud:
        [rid], records = await created(cloud)
        fake.assign_delay = 99  # the project never lists it: unconfirmed, nothing raised
        _, failed = await scope.assign_and_verify_project(
            cloud, SCOPE, [rid], records, tmp_path / "a", retry=NO_WAIT
        )
    assert failed == {rid} and "assign_error" not in records[rid].error_detail


async def test_a_failed_mark_save_never_keeps_the_delete_from_running(tmp_path):
    fake, audit = FakeDigitalOcean(), tmp_path / "audit.jsonl"

    def save():
        raise Unexpected(SECRET)

    async with running(fake) as cloud:
        [rid], records = await created(cloud)
        await scope.remove_unassigned(
            cloud, SCOPE, records, audit, {rid: {"error": "Unconfirmed"}}, save=save
        )
    record = records[rid]
    assert record.status == "destroyed" and fake.deleted == [("droplet", rid)]
    assert record.error_detail == {"error": "Unconfirmed", "save_error": "Unexpected"}
    assert audit_lines(audit)[-1]["cause"]["save_error"] == "Unexpected"


async def test_a_failed_removal_of_any_type_never_stops_the_other_ids(tmp_path, monkeypatch):
    fake, real = FakeDigitalOcean(), scope.guarded_delete
    async with running(fake) as cloud:
        ids, records = await created(cloud, count=2)

        async def first_fails(cloud, kind, rid, *args):
            if rid == ids[0]:
                raise Unexpected(SECRET)
            return await real(cloud, kind, rid, *args)

        monkeypatch.setattr(scope, "guarded_delete", first_fails)
        causes = {rid: {"error": "Unconfirmed"} for rid in ids}
        await scope.remove_unassigned(cloud, SCOPE, records, tmp_path / "a", causes)
    assert [records[rid].status for rid in ids] == ["destroy_failed", "destroyed"]
    assert fake.deleted == [("droplet", ids[1])]


async def test_a_probe_error_of_any_type_is_reported_and_the_worker_still_handled(
    tmp_path, monkeypatch
):
    """The run's boundary reports the class and a scrubbed message; the release's boundary
    leaves `destroy_failed` and its class for `campaign probe --remove`."""
    monkeypatch.setattr(probing.scope, "assign_and_verify_project", raise_unexpected)
    fake = FakeDigitalOcean()
    inputs, _ = world(tmp_path)
    result = await probed(fake, FakeProbeRemote({}), inputs)
    assert result["error"].startswith("Unexpected: to [removed] with token [removed]")
    assert "203.0.113.9" not in json.dumps(result) and "x" * 40 not in json.dumps(result)
    assert (result["worker_status"], result["worker"]["error_class"]) == (
        "destroy_failed",
        "Unexpected",
    )
    assert result["recover"].startswith("campaign probe --remove") and fake.deleted == []


async def test_a_build_cleanup_error_of_any_type_still_removes_the_other_resource(
    tmp_path, monkeypatch
):
    wheel = tmp_path / "proxy_index-0.9.5-py3-none-any.whl"
    wheel.write_bytes(b"wheel bytes")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("aiohttp==3.14.3 --hash=sha256:00\n")
    key = tmp_path / "keys" / "proxy-index-campaign"
    key.parent.mkdir()
    key.write_text("private part, never read by the build\n")
    key.with_name(key.name + ".pub").write_text(PUBLIC_KEY + "\n")
    job = ImageJob(wheel, requirements, key, tmp_path / "secrets" / "campaign-images.json")
    real = ops_api.release

    async def droplet_fails(cloud, kind, *args):
        if kind == "droplet":
            raise Unexpected(SECRET)
        return await real(cloud, kind, *args)

    monkeypatch.setattr(ops_api, "release", droplet_fails)
    fake = FakeDigitalOcean()
    with pytest.raises(ops_api.OpsError, match="left"):
        await run_build(fake, job)
    entry = inventory(job)
    assert entry["leftovers"] == [f"droplet {entry['builder_id']}: Unexpected"]
    assert not fake.resources["ssh_key"] and SECRET not in job.inventory.read_text()


async def test_a_teardown_release_error_of_any_type_never_stops_the_others(tmp_path, monkeypatch):
    fast_assign(monkeypatch)
    job = make_job(tmp_path)
    fake, saved, _ = await owned_world(job)
    real = teardown.guarded_delete

    async def key_fails(cloud, kind, *args):
        if kind == "ssh_key":
            raise Unexpected(SECRET)
        return await real(cloud, kind, *args)

    monkeypatch.setattr(teardown, "guarded_delete", key_fails)
    async with running(fake) as cloud:
        done = await teardown.teardown(
            cloud,
            job.inventory,
            [clean_report(tmp_path)],
            shell=Shell(),
            remove_agent=lambda: False,
        )
    results = {(r["kind"], r["id"]): r["result"] for r in done["released"]}
    assert results[("ssh_key", saved["worker_key_id"])] == "failed: Unexpected"
    assert results[("droplet", saved["manager_id"])] == "deleted" and done["clean"] is False
