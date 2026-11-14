"""MUT-1: the mutants of the guard (scope.py and the DigitalOcean delete and project paths) that
the rest of the suite let survive, each killed here by the behaviour it would change.

Line coverage was already 100%; mutation testing (`mutmut`, on a disposable server) showed which
changed comparisons, keys, messages and arguments no test noticed. The survivors left are
equivalent (the PR lists them with their reasons). Offline: the fake DigitalOcean only.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from campaign_fakes import (
    CAMPAIGN_PROJECT,
    CREATE_TIME,
    EARLY_TIME,
    MANAGER_ID,
    OTHER_SID,
    PROD_PROJECT,
    SID,
    FakeDigitalOcean,
    Fault,
    running,
)
from campaign_scope_cases import NAME, OURS, PROD_FIREWALL, SCOPE, audit_lines, created_worker

from atlas.campaign import scope
from atlas.campaign.contracts import (
    BUILDER_TAG,
    MANAGER_TAG,
    PULSE_TAG,
    WORKER_TAG,
    Droplet,
    DropletRecord,
    Scope,
)
from atlas.campaign.do_api import DigitalOcean, ScopeError
from atlas.campaign.scope import Ledger, OwnershipError, guarded_delete

FAST = scope.Retry(delay=0, settle=())
RESOURCES = f"/v2/projects/{CAMPAIGN_PROJECT}/resources"


def refusal(error: pytest.ExceptionInfo) -> tuple[str, str, list[str]]:
    return error.value.kind, error.value.resource_id, error.value.failed


def test_the_refusals_say_exactly_what_was_refused():
    error = OwnershipError("droplet", "7", ["tags", "name"])
    assert str(error) == "refused droplet 7: failed tags, name"
    assert (error.kind, error.resource_id, error.failed) == ("droplet", "7", ["tags", "name"])
    with pytest.raises(ScopeError, match=r"^only scope\.py may delete or assign to a project$"):
        scope.require_guard(object())
    with pytest.raises(ValueError, match=r"^unknown resource kind or role$"):
        scope._require_known("volume", SCOPE, "worker")
    with pytest.raises(ValueError, match=r"^a pulse node is removed only by retire_pulse_node$"):
        scope._require_known("droplet", SCOPE, "pulse")
    with pytest.raises(ValueError, match=r"^the pulse role needs the pulse scope, and only the"):
        scope._same_kind(SCOPE, "pulse")


def test_an_unconfirmed_detail_lists_its_failed_checks():
    detail = scope.error_detail(DigitalOcean(lambda: "unused"), ["1"], failed=["tags", "name"])
    assert detail == {
        "error": "Unconfirmed",
        "message": "not confirmed: failed tags, name",
        "failed": ["tags", "name"],
        "http": [],
    }


def test_the_pulse_scope_is_exactly_the_pulse_marker():
    assert scope.pulse_scope("p1") == Scope(
        "pulse", "pulse", "p1", "", datetime.min.replace(tzinfo=UTC)
    )


def droplet(name, tags, droplet_id="700001", created=CREATE_TIME):
    when = datetime.fromisoformat(created)
    return Droplet(droplet_id, name, "lon1", "c-2", "active", when, tuple(tags))


MANAGER_NAME = "atlas-camp-manager-20270101-ab12"
PULSE_NAME = "atlas-pulse-lon1-shifter"
ALL_PASS = {"tags": True, "name": True, "not_manager": True, "created_after": True}


@pytest.mark.parametrize(
    ("node", "role", "failed"),
    [
        (droplet(NAME, OURS), "worker", ()),
        (droplet(NAME, [*OURS, MANAGER_TAG]), "worker", ("tags",)),
        (droplet(NAME, [WORKER_TAG]), "worker", ("tags",)),
        (droplet(f"atlas-{OTHER_SID}-h03-lon1-shifter", OURS), "worker", ("name",)),
        (droplet("atlas-01234567-lon1", OURS), "worker", ("name",)),
        (droplet(NAME, OURS, droplet_id=MANAGER_ID), "worker", ("not_manager",)),
        (droplet(NAME, OURS, created=EARLY_TIME), "worker", ("created_after",)),
        (droplet(MANAGER_NAME, [MANAGER_TAG], droplet_id=MANAGER_ID), "manager", ()),
        (droplet(MANAGER_NAME, [MANAGER_TAG]), "manager", ("not_manager",)),
        (droplet("atlas-camp-builder-20270101-cd34", [BUILDER_TAG]), "builder", ()),
    ],
)
def test_each_role_check_fails_alone(node, role, failed):
    assert scope.role_checks(node, SCOPE, role) == ALL_PASS | dict.fromkeys(failed, False)


@pytest.mark.parametrize(
    ("tags", "name", "expected"),
    [
        ([PULSE_TAG], PULSE_NAME, {"tags": True, "name": True}),
        ([PULSE_TAG, "prod"], PULSE_NAME, {"tags": False, "name": True}),
        (["atlas-pulse-shifter"], PULSE_NAME, {"tags": False, "name": True}),
        ([PULSE_TAG], NAME, {"tags": True, "name": False}),
    ],
)
def test_a_pulse_node_has_only_the_tags_and_name_checks(tags, name, expected):
    node = droplet(name, tags)
    assert scope.role_checks(node, scope.pulse_scope("p1"), "pulse") == expected


def test_the_pulse_role_may_remove_only_a_droplet_of_the_pulse_scope():
    assert scope._require_known("droplet", scope.pulse_scope("p1"), "pulse") is None
    with pytest.raises(ValueError):
        scope._require_known("firewall", scope.pulse_scope("p1"), "pulse")


def test_an_audit_line_is_sorted_strict_json_in_a_private_file(tmp_path):
    path, old = tmp_path / "audit" / "a.jsonl", os.umask(0)
    try:
        scope._audit(path, {"b": 1, "a": 2})
    finally:
        os.umask(old)
    assert path.read_text() == '{"a": 2, "b": 1}\n'
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        scope._audit(path, {"x": float("nan")})
    assert path.read_text() == '{"a": 2, "b": 1}\n'


async def test_a_delete_and_an_absent_resource_audit_every_field(tmp_path):
    fake, audit = FakeDigitalOcean(), tmp_path / "audit.jsonl"
    rid = fake.add(NAME, OURS, project=CAMPAIGN_PROJECT)
    async with running(fake) as cloud:
        ledger = Ledger({"droplet": [rid]})
        assert await guarded_delete(cloud, "droplet", rid, SCOPE, ledger, audit) == "deleted"
        assert await guarded_delete(cloud, "droplet", rid, SCOPE, ledger, audit) == "absent"
    attempt, deleted, absent = audit_lines(audit)
    shared = {"time", "kind", "id", "role", "campaign", "path", "result"}
    assert set(attempt) == shared | {"name", "checks"} and attempt["name"] == NAME
    assert set(deleted) == shared | {"name", "checks", "http"} and deleted["http"] == 204
    assert set(absent) == shared | {"http"} and absent["http"] == 404
    assert datetime.fromisoformat(deleted["time"]).tzinfo is not None
    assert list(json.loads(audit.read_text().splitlines()[1])) == sorted(deleted)


async def test_a_refused_delete_names_its_kind_and_id(tmp_path):
    fake = FakeDigitalOcean()
    fake.add_resource("firewall", PROD_FIREWALL, name="prod-web", tags=[], droplet_ids=[])
    async with running(fake) as cloud:
        with pytest.raises(OwnershipError) as refused:
            await guarded_delete(
                cloud,
                "firewall",
                PROD_FIREWALL,
                SCOPE,
                Ledger({"firewall": [PROD_FIREWALL]}),
                tmp_path / "audit.jsonl",
            )
    assert refusal(refused) == ("firewall", PROD_FIREWALL, ["name"])


def own_unassigned(fake: FakeDigitalOcean) -> tuple[str, Ledger]:
    """A worker of ours outside the project, with the ledger mark of a failed assignment."""
    rid = fake.add(NAME, OURS, project=None)
    record = DropletRecord("shifter", "lon1", 3, NAME, rid, assign_failed_at="2027-01-01T22:00Z")
    return rid, Ledger({"droplet": [rid]}, {rid: record})


OWN_UNASSIGNED_CHECKS = {
    "recorded",
    "tags",
    "name",
    "not_manager",
    "created_after",
    "project_id",
    "assign_failed",
    "own_create",
    "exact_name",
    "fresh",
}


@pytest.mark.parametrize(("age", "deleted"), [(1800, True), (1801, False)])
async def test_the_own_unassigned_path_holds_for_exactly_thirty_minutes(
    tmp_path, monkeypatch, age, deleted
):
    fake, audit = FakeDigitalOcean(), tmp_path / "audit.jsonl"
    rid, ledger = own_unassigned(fake)
    now = datetime.fromisoformat(CREATE_TIME) + timedelta(seconds=age)
    monkeypatch.setattr(scope, "utcnow", lambda: now)
    async with running(fake) as cloud:
        if deleted:
            assert await guarded_delete(cloud, "droplet", rid, SCOPE, ledger, audit) == "deleted"
        else:
            with pytest.raises(OwnershipError) as refused:
                await guarded_delete(cloud, "droplet", rid, SCOPE, ledger, audit)
            assert refused.value.failed == ["project"]
    first = audit_lines(audit)[0]
    if deleted:  # the own-unassigned checks replace the project check, every one recorded
        assert first["path"] == "own_unassigned" and set(first["checks"]) == OWN_UNASSIGNED_CHECKS
    else:
        assert first["path"] == "normal" and first["checks"]["project"] is False
    assert (rid in fake.droplets) is not deleted


def resource_reads(fake: FakeDigitalOcean) -> int:
    return sum(1 for method, path, _, _ in fake.requests if (method, path) == ("GET", RESOURCES))


async def test_project_membership_is_cached_for_under_sixty_seconds(monkeypatch):
    fake, clock = FakeDigitalOcean(), [1000.0]
    rid = fake.add(NAME, OURS, project=CAMPAIGN_PROJECT)
    monkeypatch.setattr(scope, "monotonic", lambda: clock[0])
    async with running(fake) as cloud:
        reads = []
        for now in (1000.0, 1059.9, 1060.0):
            clock[0] = now
            assert await scope._in_project(cloud, CAMPAIGN_PROJECT, rid)
            reads.append(resource_reads(fake))
    assert reads == [1, 1, 2]


async def test_a_confirmed_assignment_fills_the_membership_cache(tmp_path, monkeypatch):
    fake = FakeDigitalOcean()
    monkeypatch.setattr(scope, "monotonic", lambda: 1000.0)
    async with running(fake) as cloud:
        droplet, records = await created_worker(cloud)
        confirmed, _ = await scope.assign_and_verify_project(
            cloud, SCOPE, [droplet.id], records, tmp_path / "a", retry=FAST
        )
        before = resource_reads(fake)
        assert await scope._in_project(cloud, CAMPAIGN_PROJECT, droplet.id)
    assert confirmed == {droplet.id} and resource_reads(fake) == before


async def test_an_assignment_never_listed_is_recorded_as_failing_project(tmp_path):
    fake = FakeDigitalOcean()
    fake.assign_delay = 10**6  # the project never lists it
    async with running(fake) as cloud:
        droplet, records = await created_worker(cloud)
        _, failed = await scope.assign_and_verify_project(
            cloud, SCOPE, [droplet.id], records, tmp_path / "a", retry=FAST
        )
    assert failed == {droplet.id} and records[droplet.id].error_detail["failed"] == ["project"]


async def test_a_project_that_is_not_atlas_is_refused_by_its_id(tmp_path):
    fake = FakeDigitalOcean()
    fake.project_names[CAMPAIGN_PROJECT] = fake.project_names[PROD_PROJECT]
    async with running(fake) as cloud:
        droplet, records = await created_worker(cloud)
        with pytest.raises(OwnershipError) as refused:
            await scope.assign_and_verify_project(
                cloud, SCOPE, [droplet.id], records, tmp_path / "a", retry=FAST
            )
    assert refusal(refused) == ("project", CAMPAIGN_PROJECT, ["project_id"])


async def test_a_fresh_read_refuses_by_droplet_and_id(tmp_path):
    """Unreadable and not ours, or readable and failing a check that never settles."""
    fake = FakeDigitalOcean()
    foreign = fake.add(NAME, OURS, project=None)
    fake.faults.append(Fault("GET", f"/v2/droplets/{foreign}", 500, times=5))
    async with running(fake) as cloud:
        records = {foreign: DropletRecord("shifter", "lon1", 3, NAME, foreign)}
        with pytest.raises(OwnershipError) as unreadable:
            await scope.assign_and_verify_project(
                cloud, SCOPE, [foreign], records, tmp_path / "a", retry=FAST
            )
        droplet, _ = await created_worker(cloud)
        with pytest.raises(OwnershipError) as unrecorded:
            await scope.assign_and_verify_project(
                cloud, SCOPE, [droplet.id], {}, tmp_path / "a", retry=FAST
            )
    assert refusal(unreadable) == ("droplet", foreign, ["own_create"])
    assert refusal(unrecorded) == ("droplet", droplet.id, ["recorded"])


async def test_a_shown_droplet_is_read_once_and_a_lagging_one_until_shown():
    fake, sleeps = FakeDigitalOcean(), []

    async def sleep(seconds):
        sleeps.append(seconds)

    retry = scope.Retry(settle=(1.0, 2.0), sleep=sleep)
    async with running(fake) as cloud:
        shown, records = await created_worker(cloud)
        lagging, more = await created_worker(cloud, f"atlas-{SID}-h03-fra1-shifter")
        claim = scope._Claim(cloud, SCOPE, "worker")
        assert await scope._shown(claim, [shown.id], records, retry) == {shown.id: []}
        assert sleeps == []
        fake.stale[lagging.id] = [{"tags": []}]
        assert await scope._shown(claim, [lagging.id], more, retry) == {lagging.id: []}
        assert sleeps == [1.0]
        del fake.droplets[lagging.id]
        assert await scope._fresh_owned(claim, lagging.id, more) == ["exists"]


async def test_an_unassigned_builder_is_removed_as_a_builder(tmp_path, monkeypatch):
    fake, name = FakeDigitalOcean(), "atlas-camp-builder-20270101-cd34"
    monkeypatch.setattr(scope, "utcnow", lambda: datetime.fromisoformat(CREATE_TIME))  # fresh
    async with running(fake) as cloud:
        [builder] = await cloud.create(
            [name], region="lon1", size="c-2", image="1", tags=[BUILDER_TAG], ssh_keys=["9"]
        )
        records = {builder.id: DropletRecord("shifter", "lon1", 3, name, builder.id)}
        await scope.remove_unassigned(
            cloud, SCOPE, records, tmp_path / "a", {builder.id: {}}, "builder"
        )
    assert records[builder.id].status == "destroyed" and builder.id not in fake.droplets


async def test_marking_a_droplet_not_created_here_is_refused_by_its_id(tmp_path):
    fake = FakeDigitalOcean()
    rid = fake.add(NAME, OURS, project=None)
    async with running(fake) as cloud:
        records = {rid: DropletRecord("shifter", "lon1", 3, NAME, rid)}
        with pytest.raises(OwnershipError) as refused:
            await scope.remove_unassigned(cloud, SCOPE, records, tmp_path / "a", {rid: {}})
    assert refusal(refused) == ("droplet", rid, ["own_create"])
    assert records[rid].status == "requested" and records[rid].assign_failed_at is None


async def test_retries_sleep_only_between_attempts():
    sleeps, steps = [], []

    async def sleep(seconds):
        sleeps.append(seconds)

    async def step():
        steps.append(len(sleeps))
        return False

    await scope._retrying(scope.Retry(attempts=3, delay=5.0, sleep=sleep), step)
    assert steps == [0, 1, 2] and sleeps == [5.0, 5.0]


def audited(path, *entries):
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def test_a_settling_refusal_may_fail_every_settling_check_and_follow_a_torn_line(tmp_path):
    every = {name: False for name in (*scope.SETTLING, "project")}
    entry = {"kind": "droplet", "id": "7", "result": "refused", "checks": every}
    assert scope.settling_refusal(audited(tmp_path / "a", entry), "7")
    torn = tmp_path / "b"
    torn.write_text('{"kind": "drop\n' + json.dumps(entry) + "\n")
    assert scope.settling_refusal(torn, "7")


async def test_the_client_refuses_with_exact_messages():
    async with running(FakeDigitalOcean()) as cloud:
        for ids in ([], ["70000x"]):
            with pytest.raises(ScopeError, match=r"^project assignment needs droplet ids$"):
                await cloud.assign_project(CAMPAIGN_PROJECT, ids, guard=scope._GUARD)
        with pytest.raises(ScopeError, match=r"^unknown create flag$"):
            await cloud.create(
                [NAME], region="lon1", size="c-2", image="1", tags=OURS, ssh_keys=["9"], x=True
            )


async def test_a_create_answer_without_droplets_creates_none():
    fake = FakeDigitalOcean()
    fake.faults.append(Fault("POST", "/v2/droplets", 202))
    async with running(fake) as cloud:
        found = await cloud.create(
            [NAME], region="lon1", size="c-2", image="1", tags=OURS, ssh_keys=["9"]
        )
    assert found == [] and not cloud.created_ids


class UnnamedResource(FakeDigitalOcean):
    """A project that also lists a resource without a URN."""

    async def _project_resources(self, request):
        items = [{"urn": f"do:droplet:{i}"} for i in sorted(self.projects[CAMPAIGN_PROJECT])]
        return self._page(request, "resources", [{"id": "x"}, *items])


async def test_project_members_skip_a_resource_without_a_urn():
    fake = UnnamedResource()
    rid = fake.add(NAME, OURS, project=CAMPAIGN_PROJECT)
    async with running(fake) as cloud:
        assert await cloud.project_droplet_ids(CAMPAIGN_PROJECT) == frozenset({rid})
