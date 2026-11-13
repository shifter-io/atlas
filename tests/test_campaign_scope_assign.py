"""The scope suite's project assignment: a droplet is confirmed in Proxy Index or destroyed,
and only our own fresh creates are ever assigned."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from campaign_fakes import (
    CAMPAIGN_PROJECT,
    PROD_PROJECT,
    SID,
    SPEC_TIME,
    FakeDigitalOcean,
    Fault,
    running,
)
from campaign_scope_cases import NAME, OURS, SCOPE, audit_lines, created_worker

from atlas.campaign import scope
from atlas.campaign.contracts import DropletRecord
from atlas.campaign.scope import (
    Ledger,
    OwnershipError,
    assign_and_verify_project,
    guarded_delete,
)


async def test_assignment_is_confirmed_on_the_first_try(tmp_path):
    fake = FakeDigitalOcean()
    async with running(fake) as cloud:
        droplet, records = await created_worker(cloud)
        confirmed, failed = await assign_and_verify_project(
            cloud, SCOPE, [droplet.id], records, tmp_path / "a.jsonl"
        )
        assert await assign_and_verify_project(cloud, SCOPE, [], {}, tmp_path / "a.jsonl") == (
            frozenset(),
            frozenset(),
        )
    assert confirmed == {droplet.id} and not failed
    assert records[droplet.id].status == "assigned" and records[droplet.id].assigned_at
    assert droplet.id in fake.projects[CAMPAIGN_PROJECT]
    assert droplet.id not in fake.projects[PROD_PROJECT]  # moved out of the default project


async def test_assignment_is_confirmed_after_errors_and_a_lagging_project(tmp_path):
    fake, sleeps = FakeDigitalOcean(), []

    async def sleep(seconds):
        sleeps.append(seconds)

    async with running(fake) as cloud:
        droplet, records = await created_worker(cloud)
        fake.faults.append(
            Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 500, times=5)
        )
        fake.assign_delay = 1
        confirmed, failed = await assign_and_verify_project(
            cloud,
            SCOPE,
            [droplet.id],
            records,
            tmp_path / "a.jsonl",
            retry=scope.Retry(sleep=sleep),
        )
    assert confirmed == {droplet.id} and not failed and sleeps == [5.0, 5.0]
    assert scope.ASSIGN_RETRY == scope.Retry(3, 5.0)


async def test_never_confirmed_is_assign_failed_and_destroyed_through_own_unassigned(tmp_path):
    """B3: fail closed; the droplet is deleted, not left running outside Proxy Index."""
    fake, audit = FakeDigitalOcean(), tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        droplet, records = await created_worker(cloud)
        fake.assign_delay = 99
        confirmed, failed = await assign_and_verify_project(
            cloud, SCOPE, [droplet.id], records, audit, retry=scope.Retry(delay=0)
        )
    assert not confirmed and failed == {droplet.id} and droplet.id not in fake.droplets
    record = records[droplet.id]
    assert record.status == "destroyed" and record.error_class == "project_assign_failed"
    attempt, deleted = audit_lines(audit)
    assert attempt["path"] == deleted["path"] == "own_unassigned"
    assert deleted["result"] == "deleted" and "project" not in deleted["checks"]
    assert all(deleted["checks"].values())


@pytest.mark.parametrize("save_fails", [False, True], ids=["saved", "save_failed"])
async def test_the_mark_is_saved_before_the_delete(tmp_path, save_fails):
    """CAMP-1r (a): the caller's save sees the mark and no delete yet; a failing save never
    keeps our own unassigned droplet alive."""
    fake, audit, seen = FakeDigitalOcean(), tmp_path / "audit.jsonl", []
    async with running(fake) as cloud:
        droplet, records = await created_worker(cloud)

        def save():
            record = records[droplet.id]
            seen.append((record.status, bool(record.assign_failed_at), list(fake.deleted)))
            if save_fails:
                raise OSError("disk full")

        fake.assign_delay = 99
        _, failed = await assign_and_verify_project(
            cloud, SCOPE, [droplet.id], records, audit, retry=scope.Retry(delay=0, save=save)
        )
    assert seen == [("assign_failed", True, [])] and failed == {droplet.id}
    assert records[droplet.id].status == "destroyed" and droplet.id not in fake.droplets


def test_settling_refusal_reads_only_the_latest_audit_line_of_the_droplet(tmp_path):
    """CAMP-1r (b): retried only after a refusal on checks that settle after a create."""
    audit = tmp_path / "audit.jsonl"
    assert not scope.settling_refusal(audit, "7")  # no audit yet

    def write(*entries):
        audit.write_text(
            "".join(json.dumps({"kind": "droplet", "id": "7"} | e) + "\n" for e in entries)
            + '{"kind": "drop'
        )  # cut short by a crash

    def refused(*failed):
        checks = dict.fromkeys(["recorded", "tags", "name", "not_manager", "project"], True)
        return {"result": "refused", "checks": checks | dict.fromkeys(failed, False)}

    write({"result": "deleted"})
    assert not scope.settling_refusal(audit, "8")  # no line of this droplet
    for failed, retried in (
        (["tags"], True),
        (["name", "project"], True),
        (["created_after", "project"], True),
        (["project"], False),
        (["tags", "not_manager"], False),
        (["recorded", "tags"], False),
    ):
        write(refused(*failed), {"id": "8", "result": "deleted"})
        assert scope.settling_refusal(audit, "7") is retried, failed
    write(refused("tags"), {"result": "deleted"}, {"kind": "ssh_key", **refused("tags")})
    assert not scope.settling_refusal(audit, "7")


async def test_a_late_arrival_is_destroyed_through_the_normal_path(tmp_path):
    fake, audit = FakeDigitalOcean(), tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        droplet, records = await created_worker(cloud)
        fake.assign_delay = 2  # arrives on the guard's own re-read
        _, failed = await assign_and_verify_project(
            cloud, SCOPE, [droplet.id], records, audit, retry=scope.Retry(attempts=2, delay=0)
        )
    assert failed == {droplet.id} and audit_lines(audit)[-1]["path"] == "normal"
    assert records[droplet.id].status == "destroyed"


@pytest.mark.parametrize("case", ["not_assign_failed", "not_our_create", "too_old"])
async def test_the_own_unassigned_path_needs_every_condition(tmp_path, monkeypatch, case):
    fake, audit = FakeDigitalOcean(), tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        droplet, records = await created_worker(cloud)
        if case == "not_our_create":  # a lookalike: our markers, recorded, but never marked
            droplet_id = fake.add(f"pic-{SID}-h03-fra1-shifter", OURS)
            records = {
                droplet_id: DropletRecord(
                    "shifter", "fra1", 3, "x", droplet_id, status="assign_failed"
                )
            }
        else:
            droplet_id = droplet.id
            records[droplet_id].status = "assign_failed"  # set by hand, not by the function
            if case == "too_old":
                records[droplet_id].assign_failed_at = SPEC_TIME
                monkeypatch.setattr(
                    scope, "utcnow", lambda: datetime.fromisoformat("2027-01-01T22:29:00Z")
                )
        with pytest.raises(OwnershipError) as refused:
            await guarded_delete(
                cloud,
                "droplet",
                droplet_id,
                SCOPE,
                Ledger({"droplet": {droplet_id}}, records),
                audit,
            )
    assert refused.value.failed == ["project"] and droplet_id in fake.droplets
    assert audit_lines(audit)[-1]["path"] == "normal"


async def test_a_refused_or_failed_destroy_is_recorded(tmp_path, monkeypatch):
    fake, audit = FakeDigitalOcean(), tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        first, records = await created_worker(cloud)
        second, more = await created_worker(cloud, f"pic-{SID}-h03-fra1-shifter")
        records |= more
        fake.assign_delay = 99
        fake.faults.append(Fault("DELETE", f"/v2/droplets/{second.id}", 403))
        monkeypatch.setattr(scope, "utcnow", lambda: datetime.fromisoformat("2027-01-01T22:00Z"))
        _, failed = await assign_and_verify_project(
            cloud, SCOPE, [first.id, second.id], records, audit, retry=scope.Retry(attempts=1)
        )
        monkeypatch.setattr(scope, "utcnow", lambda: datetime.fromisoformat("2027-01-02T00:00Z"))
        third, more = await created_worker(cloud, f"pic-{SID}-h03-syd1-shifter")
        await assign_and_verify_project(
            cloud, SCOPE, [third.id], more, audit, retry=scope.Retry(attempts=1)
        )
    assert failed == {first.id, second.id}
    assert records[first.id].status == "destroyed"
    assert records[second.id].status == "destroy_failed" and second.id in fake.droplets
    assert more[third.id].status == "destroy_refused" and third.id in fake.droplets


async def test_an_unexpected_destroy_error_does_not_stop_the_other_destroys(tmp_path, monkeypatch):
    """r2: one id's error is audited and recorded; the remaining ids are still destroyed."""
    fake, audit = FakeDigitalOcean(), tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        first, records = await created_worker(cloud)
        second, more = await created_worker(cloud, f"pic-{SID}-h03-fra1-shifter")
        records |= more
        broken, parse = min(first.id, second.id), scope.parse_droplet

        def parse_or_fail(raw):
            if str(raw["id"]) == broken:
                raise RuntimeError("malformed droplet")
            return parse(raw)

        monkeypatch.setattr(scope, "parse_droplet", parse_or_fail)
        fake.assign_delay = 99
        _, failed = await assign_and_verify_project(
            cloud, SCOPE, [first.id, second.id], records, audit, retry=scope.Retry(attempts=1)
        )
    other = max(first.id, second.id)
    assert failed == {first.id, second.id}
    assert records[broken].status == "destroy_failed" and broken in fake.droplets
    assert records[other].status == "destroyed" and other not in fake.droplets
    assert {"result": "failed", "error": "RuntimeError"}.items() <= audit_lines(audit)[0].items()


async def test_assignment_refuses_foreign_or_spoofed_ids_before_any_post(tmp_path):
    """S2: a fresh GET of each id, our own create, and the ledger record; never a caller object."""
    fake = FakeDigitalOcean()
    production = fake.add("prod-node-1", ["prod"])
    lookalike = fake.add(f"pic-{SID}-h03-ams3-shifter", OURS)  # ours by markers, not our create
    async with running(fake) as cloud:
        droplet, records = await created_worker(cloud)
        spoofed = records | {
            i: DropletRecord("shifter", "lon1", 3, NAME, i) for i in (production, lookalike)
        }
        cases = [
            ([droplet.id, production], spoofed, {"own_create", "tags", "name"}),
            ([lookalike], spoofed, {"own_create"}),
            ([droplet.id], {}, {"recorded"}),
            (
                ["999999"],
                {"999999": DropletRecord("s", "lon1", 3, NAME, "999999")},
                {"own_create", "exists"},
            ),
        ]
        for ids, ledger, failed in cases:
            before = [r for r in fake.requests if r[0] == "POST"]
            with pytest.raises(OwnershipError) as refused:
                await assign_and_verify_project(cloud, SCOPE, ids, ledger, tmp_path / "a")
            assert set(refused.value.failed) == failed
            assert [r for r in fake.requests if r[0] == "POST"] == before
        fake.project_names[CAMPAIGN_PROJECT] = "Something Else"  # m6
        with pytest.raises(OwnershipError) as wrong:
            await assign_and_verify_project(cloud, SCOPE, [droplet.id], records, tmp_path / "a")
    assert wrong.value.failed == ["project_id"]
    assert production in fake.projects[PROD_PROJECT] and lookalike in fake.projects[PROD_PROJECT]
    assert droplet.id in fake.projects[PROD_PROJECT]
