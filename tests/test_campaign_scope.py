"""The safety core: only droplets this campaign created can ever be deleted or moved.

The fake account is seeded like the real one: 1,125 production droplets in other projects, plus
every near-miss of our ownership markers. Every destructive path runs against it. This file and
tests/test_campaign_api.py must keep scope.py and do_api.py at 100% line and branch coverage
(`test_scope_coverage_gate`).
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from campaign_fakes import (
    CAMPAIGN_PROJECT,
    CID,
    CREATE_TIME,
    EARLY_TIME,
    MANAGER_ID,
    OTHER_SID,
    PROD_PROJECT,
    SID,
    SPEC_TIME,
    FakeDigitalOcean,
    Fault,
    running,
)

from proxy_index.campaign import contracts, do_api, scope
from proxy_index.campaign.contracts import (
    BUILDER_TAG,
    MANAGER_TAG,
    WORKER_TAG,
    DropletRecord,
    Scope,
)
from proxy_index.campaign.do_api import ApiError, ScopeError
from proxy_index.campaign.scope import (
    Ledger,
    OwnershipError,
    assign_and_verify_project,
    guarded_delete,
)

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_SRC = ROOT / "src" / "proxy_index" / "campaign"
SCOPE = Scope(CID, SID, CAMPAIGN_PROJECT, MANAGER_ID, datetime.fromisoformat(SPEC_TIME))
OURS = [WORKER_TAG, f"pi-camp-{SID}", f"pi-camp-{SID}-h03"]
NAME = f"pic-{SID}-h03-lon1-shifter"
SNAPSHOT = "pi-camp-worker-0.9.5-0123456789ab-20270101"
PROD_FIREWALL = "33333333-3333-4333-8333-000000000099"
FIREWALL_RULES = {"name": "pi-camp-x", "inbound_rules": [], "outbound_rules": [], "tags": [],
                  "droplet_ids": []}
# Near-misses: (key, name, tags, project, created, failing check, role).
NEAR_MISSES = [
    ("untagged", "web-legacy", [], PROD_PROJECT, None, "tags", "worker"),
    ("other_tags", "api-01", ["prod", "k8s"], PROD_PROJECT, None, "tags", "worker"),
    ("pic_name_without_tags", NAME, ["prod"], CAMPAIGN_PROJECT, None, "tags", "worker"),
    ("ours_in_other_project", NAME, OURS, PROD_PROJECT, None, "project", "worker"),
    ("ours_in_no_project", NAME, OURS, None, None, "project", "worker"),
    ("worker_without_campaign_tag", NAME, [WORKER_TAG], CAMPAIGN_PROJECT, None, "tags", "worker"),
    ("other_campaign", f"pic-{OTHER_SID}-h03-lon1-shifter",
     [WORKER_TAG, f"pi-camp-{OTHER_SID}"], CAMPAIGN_PROJECT, None, "tags", "worker"),
    ("other_campaign_name", f"pic-{OTHER_SID}-h03-lon1-shifter", OURS, CAMPAIGN_PROJECT, None,
     "name", "worker"),
    ("bad_name", "pic-01234567-lon1", OURS, CAMPAIGN_PROJECT, None, "name", "worker"),
    ("worker_tagged_manager", NAME, [*OURS, MANAGER_TAG], CAMPAIGN_PROJECT, None, "tags",
     "worker"),
    ("created_before_spec", NAME, OURS, CAMPAIGN_PROJECT, EARLY_TIME, "created_after", "worker"),
    ("foreign_manager", "pi-camp-manager-20270101-ee99", [MANAGER_TAG], CAMPAIGN_PROJECT, None,
     "not_manager", "manager"),
    ("foreign_builder", "pi-camp-builder-20270101-ee99", [BUILDER_TAG], PROD_PROJECT, None,
     "project", "builder"),
]


def seeded():
    fake = FakeDigitalOcean()
    fake.seed_production()
    near = {key: fake.add(name, tags, project=project, created=created or CREATE_TIME)
            for key, name, tags, project, created, _, _ in NEAR_MISSES}
    near["manager"] = fake.add("pi-camp-manager-20270101-ab12", [MANAGER_TAG],
                               project=CAMPAIGN_PROJECT, droplet_id=MANAGER_ID)
    near["builder"] = fake.add("pi-camp-builder-20270101-cd34", [BUILDER_TAG],
                               project=CAMPAIGN_PROJECT)
    owned = {fake.add(f"pic-{SID}-h03-{region}-shifter", OURS, project=CAMPAIGN_PROJECT)
             for region in ("lon1", "fra1", "syd1")}
    return fake, near, owned


def droplets(ids):
    return Ledger({"droplet": set(ids)})


def audit_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


async def created_worker(cloud, name=NAME):
    [droplet] = await cloud.create([name], region="lon1", size="c-2", image="1", tags=OURS,
                                   ssh_keys=["9"])
    return droplet, {droplet.id: DropletRecord("shifter", "lon1", 3, name, droplet.id)}


async def test_every_destructive_path_over_the_whole_account_deletes_only_ours(tmp_path):
    fake, near, owned = seeded()
    everything = set(fake.droplets)  # worst case: a corrupted ledger lists every droplet
    audit = tmp_path / "ledger" / "delete-audit.jsonl"
    early = Scope(CID, SID, CAMPAIGN_PROJECT, MANAGER_ID, datetime.fromisoformat(EARLY_TIME))
    results = {"worker": set(), "manager": set(), "builder": set()}
    async with running(fake, per_minute=10**6) as cloud:
        for role, role_scope in (("worker", SCOPE), ("manager", early), ("builder", SCOPE)):
            for droplet_id in sorted(everything):
                try:
                    if await guarded_delete(cloud, "droplet", droplet_id, role_scope,
                                            droplets(everything), audit, role) == "deleted":
                        results[role].add(droplet_id)
                except OwnershipError:
                    pass
    ours = owned | {MANAGER_ID, near["builder"]}
    assert results == {"worker": owned, "manager": {MANAGER_ID}, "builder": {near["builder"]}}
    assert set(fake.droplets) == everything - ours
    assert len([d for d in fake.droplets.values() if d["name"].startswith("prod-")]) == 1125
    deletes = [(m, p, q) for m, p, q, _ in fake.requests if m == "DELETE"]
    assert {p.rsplit("/", 1)[1] for _, p, _ in deletes} == ours and not any(q for *_, q in deletes)
    assert not [entry for entry in fake.deleted if entry[0] == "bulk"] and not fake.unlisted
    lines = audit_lines(audit)
    assert len(lines) == 3 * len(everything) + len(ours)  # one "attempt" line before each DELETE
    assert {line["result"] for line in lines} == {"attempt", "deleted", "refused", "absent"}
    assert {line["path"] for line in lines} == {"normal"}
    assert audit.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("variant", NEAR_MISSES, ids=[item[0] for item in NEAR_MISSES])
async def test_each_near_miss_is_refused_and_audited(tmp_path, variant):
    fake, near, _ = seeded()
    target, audit = near[variant[0]], tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        with pytest.raises(OwnershipError) as refused:
            await guarded_delete(cloud, "droplet", target, SCOPE, droplets({target}), audit,
                                 variant[-1])
    assert variant[-2] in refused.value.failed
    assert target in fake.droplets and not fake.deleted
    [line] = audit_lines(audit)
    assert line["result"] == "refused" and line["checks"][variant[-2]] is False
    assert line["id"] == target and line["campaign"] == SID


async def test_worker_delete_never_targets_the_manager_even_with_worker_markers(tmp_path):
    fake = FakeDigitalOcean()
    fake.add(NAME, OURS, project=CAMPAIGN_PROJECT, droplet_id=MANAGER_ID)
    async with running(fake) as cloud:
        with pytest.raises(OwnershipError) as refused:
            await guarded_delete(cloud, "droplet", MANAGER_ID, SCOPE, droplets({MANAGER_ID}),
                                 tmp_path / "audit.jsonl")
        with pytest.raises(OwnershipError) as as_manager:
            await guarded_delete(cloud, "droplet", MANAGER_ID, SCOPE, droplets({MANAGER_ID}),
                                 tmp_path / "audit.jsonl", "manager")
    assert refused.value.failed == ["not_manager"]
    assert "tags" in as_manager.value.failed and not fake.deleted


async def test_an_unrecorded_owned_droplet_is_refused(tmp_path):
    fake, _, owned = seeded()
    target = sorted(owned)[0]
    async with running(fake) as cloud:
        with pytest.raises(OwnershipError) as refused:
            await guarded_delete(cloud, "droplet", target, SCOPE, droplets(owned - {target}),
                                 tmp_path / "audit.jsonl")
    assert refused.value.failed == ["recorded"] and target in fake.droplets


async def test_a_stale_project_cache_is_refreshed_before_a_refusal(tmp_path, monkeypatch):
    fake = FakeDigitalOcean()
    first = fake.add(NAME, OURS, project=CAMPAIGN_PROJECT)
    late = fake.add(f"pic-{SID}-h03-fra1-shifter", OURS, project=None)
    moved = fake.add(f"pic-{SID}-h03-syd1-shifter", OURS, project=CAMPAIGN_PROJECT)
    audit, now, recorded = tmp_path / "audit.jsonl", [0.0], droplets({first, late, moved})
    monkeypatch.setattr(scope, "monotonic", lambda: now[0])
    async with running(fake) as cloud:
        await guarded_delete(cloud, "droplet", first, SCOPE, recorded, audit)
        fake.projects[CAMPAIGN_PROJECT].add(late)  # assigned after the cache was filled
        assert await guarded_delete(cloud, "droplet", late, SCOPE, recorded, audit) == "deleted"
        fake.projects[CAMPAIGN_PROJECT].discard(moved)  # moved out: the cache expires in 60 s
        now[0] = 61.0
        with pytest.raises(OwnershipError) as refused:
            await guarded_delete(cloud, "droplet", moved, SCOPE, recorded, audit)
    assert refused.value.failed == ["project"]
    project_reads = [p for m, p, _, _ in fake.requests if p.endswith("/resources")]
    assert len(project_reads) == 3


async def test_a_project_id_that_is_not_proxy_index_refuses_every_delete(tmp_path):
    """m6: the scope's project id must be the project named Proxy Index."""
    fake = FakeDigitalOcean()
    target = fake.add(NAME, OURS, project=PROD_PROJECT)
    wrong = Scope(CID, SID, PROD_PROJECT, MANAGER_ID, datetime.fromisoformat(SPEC_TIME))
    async with running(fake) as cloud:
        with pytest.raises(OwnershipError) as refused:
            await guarded_delete(cloud, "droplet", target, wrong, droplets({target}),
                                 tmp_path / "audit.jsonl")
    assert refused.value.failed == ["project_id"] and target in fake.droplets


async def test_an_absent_droplet_is_audited_and_not_deleted(tmp_path):
    fake, audit = FakeDigitalOcean(), tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        assert await guarded_delete(cloud, "droplet", "123", SCOPE, droplets({"123"}),
                                    audit) == "absent"
    assert audit_lines(audit)[0]["result"] == "absent" and not fake.deleted


@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_api_errors_are_audited_and_raised(tmp_path, method):
    fake = FakeDigitalOcean()
    target = fake.add(NAME, OURS, project=CAMPAIGN_PROJECT)
    fake.faults.append(Fault(method, f"/v2/droplets/{target}", 403))
    audit = tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        with pytest.raises(ApiError):
            await guarded_delete(cloud, "droplet", target, SCOPE, droplets({target}), audit)
    line = audit_lines(audit)[-1]
    assert line["result"] == "failed" and line["http"] == 403 and target in fake.droplets
    assert [x["result"] for x in audit_lines(audit)] == (
        ["failed"] if method == "GET" else ["attempt", "failed"])


async def test_unexpected_errors_are_audited_too(tmp_path):
    """m2: a naive created_at (TypeError) still leaves an audit line and deletes nothing."""
    fake = FakeDigitalOcean()
    target = fake.add(NAME, OURS, project=CAMPAIGN_PROJECT, created="2027-01-01T21:58:30")
    audit = tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        with pytest.raises(TypeError):
            await guarded_delete(cloud, "droplet", target, SCOPE, droplets({target}), audit)
    [line] = audit_lines(audit)
    assert line["result"] == "failed" and line["error"] == "TypeError" and not fake.deleted


FIREWALL = "33333333-3333-4333-8333-333333333333"


@pytest.mark.parametrize(
    ("kind", "rid", "values", "recorded", "failed"),
    [
        ("ssh_key", "41", {"name": "pi-camp-worker-20270101"}, True, None),
        ("ssh_key", "41", {"name": "deploy-key"}, True, "name"),
        ("ssh_key", "41", {"name": "pi-camp-worker-20270101"}, False, "recorded"),
        ("snapshot", "42", {"name": "pi-camp-worker-0.9.5-abc-20270101"}, True, None),
        ("snapshot", "42", {"name": "prod-backup"}, True, "name"),
        ("firewall", FIREWALL, {"name": "pi-camp-worker-fw", "tags": [WORKER_TAG],
                                "droplet_ids": [7]}, True, None),
        ("firewall", FIREWALL, {"name": "pi-camp-worker-fw", "tags": ["prod"]}, True, "targets"),
        ("firewall", FIREWALL, {"name": "pi-camp-worker-fw", "droplet_ids": [8]}, True,
         "targets"),
    ],
)
async def test_keys_firewalls_and_snapshots_need_a_record_and_our_prefix(
    tmp_path, kind, rid, values, recorded, failed
):
    fake = FakeDigitalOcean()
    fake.add_resource(kind, rid, **values)
    ledger = Ledger({"droplet": {"7"}, kind: {rid} if recorded else set()})
    async with running(fake) as cloud:
        call = guarded_delete(cloud, kind, rid, SCOPE, ledger, tmp_path / "audit.jsonl")
        if failed is None:
            assert await call == "deleted"
            assert fake.deleted == [(kind, rid)]
        else:
            with pytest.raises(OwnershipError) as refused:
                await call
            assert refused.value.failed == [failed] and not fake.deleted


async def test_recorded_ids_are_per_kind(tmp_path):
    """m3: an SSH key whose id equals a recorded droplet id is not recorded."""
    fake = FakeDigitalOcean()
    fake.add_resource("ssh_key", "700001", name="pi-camp-worker-20270101")
    async with running(fake) as cloud:
        with pytest.raises(OwnershipError) as refused:
            await guarded_delete(cloud, "ssh_key", "700001", SCOPE, droplets({"700001"}),
                                 tmp_path / "audit.jsonl")
    assert refused.value.failed == ["recorded"] and not fake.deleted


async def test_unknown_kind_or_role_is_rejected(tmp_path):
    async with running(FakeDigitalOcean()) as cloud:
        for kind, role in (("volume", "worker"), ("droplet", "admin")):
            with pytest.raises(ValueError):
                await guarded_delete(cloud, kind, "1", SCOPE, droplets({"1"}), tmp_path / "a",
                                     role)


@pytest.mark.parametrize("method", ["DELETE", "delete", "Delete", "dElEtE"])
async def test_no_delete_without_the_guard_in_any_letter_case(method):
    """B1: the method is normalized before the allowlist and the guard check."""
    fake = FakeDigitalOcean()
    target = fake.add(NAME, OURS, project=CAMPAIGN_PROJECT)
    async with running(fake) as cloud:
        attempts = [
            dict(path="/v2/droplets", params={"tag_name": "prod"}),
            dict(path="/v2/droplets", params={"tag_name": OURS[1]}, guard=scope._GUARD),
            dict(path="/v2/droplets", guard=scope._GUARD),
            dict(path=f"/v2/droplets/{target}"),
            dict(path=f"/v2/droplets/{target}", guard=object()),
            dict(path=f"/v2/droplets/{target}", params={"tag_name": "x"}, guard=scope._GUARD),
            dict(path=f"/v2/droplets/{target}?tag_name=x", guard=scope._GUARD),
            dict(path="/v2/volumes/1", guard=scope._GUARD),
        ]
        for attempt in attempts:
            path = attempt.pop("path")
            with pytest.raises(ScopeError):
                await cloud.request(method, path, **attempt)
    assert not fake.requests and target in fake.droplets and not fake.deleted


@pytest.mark.parametrize(
    ("method", "path", "params", "body"),
    [
        ("POST", "/v2/droplets/700001/actions", None, {"type": "rebuild"}),
        ("post", "/v2/droplets/actions", {"tag_name": "prod"}, {"type": "power_off"}),
        ("POST", "/v2/tags/pi-camp-worker/resources", None, {"resources": []}),
        ("DELETE", "/v2/tags/pi-camp-worker/resources", None, {"resources": []}),
        ("DELETE", "/v2/tags/pi-camp-worker", None, None),
        ("GET", "/v2/tags", None, None),
        ("GET", "/v2/tags/prod", None, None),
        ("PUT", f"/v2/projects/{CAMPAIGN_PROJECT}", None, {"name": "x"}),
        ("PATCH", "/v2/droplets/700001", None, {}),
        ("PUT", "/v2/firewalls/33333333-3333-4333-8333-333333333333", None, {}),
        ("GET", "/v2/droplets", None, None),  # listing all droplets
        ("GET", "/v2/droplets", {"tag_name": "prod"}, None),
        ("GET", "/v2/sizes", {"tag_name": "pi-camp-x"}, None),
        ("GET", "/v2/account", {"page": 1}, None),
        ("POST", f"/v2/projects/{PROD_PROJECT}/resources", None, {"resources": []}),
        ("HEAD", "/v2/account", None, None),
        # B1 (CAMP-1f): well-formed writes to a droplet or firewall need the ownership token.
        ("POST", "/v2/droplets/700001/actions", None, {"type": "power_off"}),
        ("POST", "/v2/droplets/700001/actions", None, {"type": "shutdown"}),
        ("POST", "/v2/droplets/700001/actions", None, {"type": "snapshot", "name": SNAPSHOT}),
        ("POST", "/v2/firewalls", None, {**FIREWALL_RULES, "droplet_ids": [700001]}),
        ("PUT", f"/v2/firewalls/{PROD_FIREWALL}", None, FIREWALL_RULES),
        # CAMP-1h: the tag calls too, which only the client's own `_ensure_tag` sends.
        ("GET", "/v2/tags/pi-camp-worker", None, None),
        ("POST", "/v2/tags", None, {"name": "pi-camp-worker"}),
    ],
)
async def test_everything_off_the_allowlist_is_refused_before_the_network(
    method, path, params, body
):
    """B2: only listed method, path and query shapes are ever sent."""
    fake = FakeDigitalOcean()
    async with running(fake) as cloud:
        with pytest.raises(ScopeError):
            await cloud.request(method, path, params=params, body=body)
        with pytest.raises(ScopeError):
            await cloud.assign_project(CAMPAIGN_PROJECT, ["700001"], guard=object())
    assert not fake.requests and not fake.unlisted


@pytest.mark.parametrize("token", [None, object(), "scope"])
async def test_raw_writes_never_reach_a_production_droplet_or_firewall(token):
    """B1 (CAMP-1f): power-off, snapshot and firewall writes need the client's own token,
    which only its ownership-checking methods pass; the scope guard's token does not do."""
    fake = FakeDigitalOcean()
    production = fake.add("prod-node-0001", ["prod"])
    fake.add_resource("firewall", PROD_FIREWALL, name="prod-fw", tags=["prod"],
                      droplet_ids=[int(production)], inbound_rules=[{"protocol": "tcp"}])
    guard = scope._GUARD if token == "scope" else token
    writes = [
        ("POST", f"/v2/droplets/{production}/actions", {"type": "power_off"}),
        ("POST", f"/v2/droplets/{production}/actions", {"type": "snapshot", "name": SNAPSHOT}),
        ("POST", "/v2/firewalls", {**FIREWALL_RULES, "droplet_ids": [int(production)]}),
        ("PUT", f"/v2/firewalls/{PROD_FIREWALL}", FIREWALL_RULES),
        ("POST", "/v2/tags", {"name": "pi-camp-worker"}),
    ]
    async with running(fake) as cloud:
        for method, path, body in writes:
            with pytest.raises(ScopeError):
                await cloud.request(method, path, body=body, guard=guard)
    assert not fake.requests and fake.droplets[production]["status"] == "active"
    assert fake.resources["firewall"][PROD_FIREWALL]["name"] == "prod-fw"
    assert len(fake.resources["firewall"]) == 1 and not fake.bodies


async def test_assignment_is_confirmed_on_the_first_try(tmp_path):
    fake = FakeDigitalOcean()
    async with running(fake) as cloud:
        droplet, records = await created_worker(cloud)
        confirmed, failed = await assign_and_verify_project(cloud, SCOPE, [droplet.id], records,
                                                            tmp_path / "a.jsonl")
        assert await assign_and_verify_project(cloud, SCOPE, [], {}, tmp_path / "a.jsonl") == (
            frozenset(), frozenset())
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
        fake.faults.append(Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 500,
                                 times=5))
        fake.assign_delay = 1
        confirmed, failed = await assign_and_verify_project(
            cloud, SCOPE, [droplet.id], records, tmp_path / "a.jsonl",
            retry=scope.Retry(sleep=sleep))
    assert confirmed == {droplet.id} and not failed and sleeps == [5.0, 5.0]
    assert scope.ASSIGN_RETRY == scope.Retry(3, 5.0)


async def test_never_confirmed_is_assign_failed_and_destroyed_through_own_unassigned(tmp_path):
    """B3: fail closed; the droplet is deleted, not left running outside Proxy Index."""
    fake, audit = FakeDigitalOcean(), tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        droplet, records = await created_worker(cloud)
        fake.assign_delay = 99
        confirmed, failed = await assign_and_verify_project(
            cloud, SCOPE, [droplet.id], records, audit, retry=scope.Retry(delay=0))
    assert not confirmed and failed == {droplet.id} and droplet.id not in fake.droplets
    record = records[droplet.id]
    assert record.status == "destroyed" and record.error_class == "project_assign_failed"
    attempt, deleted = audit_lines(audit)
    assert attempt["path"] == deleted["path"] == "own_unassigned"
    assert deleted["result"] == "deleted" and "project" not in deleted["checks"]
    assert all(deleted["checks"].values())


async def test_a_late_arrival_is_destroyed_through_the_normal_path(tmp_path):
    fake, audit = FakeDigitalOcean(), tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        droplet, records = await created_worker(cloud)
        fake.assign_delay = 2  # arrives on the guard's own re-read
        _, failed = await assign_and_verify_project(cloud, SCOPE, [droplet.id], records, audit,
                                                    retry=scope.Retry(attempts=2, delay=0))
    assert failed == {droplet.id} and audit_lines(audit)[-1]["path"] == "normal"
    assert records[droplet.id].status == "destroyed"


@pytest.mark.parametrize("case", ["not_assign_failed", "not_our_create", "too_old"])
async def test_the_own_unassigned_path_needs_every_condition(tmp_path, monkeypatch, case):
    fake, audit = FakeDigitalOcean(), tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        droplet, records = await created_worker(cloud)
        if case == "not_our_create":
            droplet_id = fake.add(f"pic-{SID}-h03-fra1-shifter", OURS)
            records = {droplet_id: DropletRecord("shifter", "fra1", 3, "x", droplet_id,
                                                 status="assign_failed")}
            scope._assign_failed.setdefault(cloud, set()).add(droplet_id)
        else:
            droplet_id = droplet.id
            records[droplet_id].status = "assign_failed"  # set by hand, not by the function
            if case == "too_old":
                scope._assign_failed.setdefault(cloud, set()).add(droplet_id)
                monkeypatch.setattr(scope, "utcnow",
                                    lambda: datetime.fromisoformat("2027-01-01T22:29:00Z"))
        with pytest.raises(OwnershipError) as refused:
            await guarded_delete(cloud, "droplet", droplet_id, SCOPE,
                                 Ledger({"droplet": {droplet_id}}, records), audit)
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
        _, failed = await assign_and_verify_project(cloud, SCOPE, [first.id, second.id], records,
                                                    audit, retry=scope.Retry(attempts=1))
        monkeypatch.setattr(scope, "utcnow", lambda: datetime.fromisoformat("2027-01-02T00:00Z"))
        third, more = await created_worker(cloud, f"pic-{SID}-h03-syd1-shifter")
        await assign_and_verify_project(cloud, SCOPE, [third.id], more, audit,
                                        retry=scope.Retry(attempts=1))
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
        _, failed = await assign_and_verify_project(cloud, SCOPE, [first.id, second.id], records,
                                                    audit, retry=scope.Retry(attempts=1))
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
        spoofed = records | {i: DropletRecord("shifter", "lon1", 3, NAME, i)
                             for i in (production, lookalike)}
        cases = [([droplet.id, production], spoofed, {"own_create", "tags", "name"}),
                 ([lookalike], spoofed, {"own_create"}),
                 ([droplet.id], {}, {"recorded"}),
                 (["999999"], {"999999": DropletRecord("s", "lon1", 3, NAME, "999999")},
                  {"own_create", "exists"})]
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


def test_scope_for_builds_the_campaign_scope():
    spec = type("Spec", (), {"campaign_id": CID, "project_id": CAMPAIGN_PROJECT,
                             "manager_droplet_id": int(MANAGER_ID), "created_at": SPEC_TIME})
    assert scope.scope_for(spec) == SCOPE


def _calls(tree):
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _call_name(node):
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _strings(tree):
    """String constants that are not docstrings."""
    docs = {id(node.body[0].value) for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)}
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
            and isinstance(n.value, str) and id(n) not in docs]


def _names(tree):
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {alias.name.split(".")[0] for alias in node.names}
            names |= {node.module.split(".")[0]} if getattr(node, "module", None) else set()
    return names


# "delete" as a method or command word in any letter case; not inside a name like delete-audit.
DELETE_WORD = re.compile(r"(?<![\w-])delete(?![\w-])", re.IGNORECASE)


PRIVATE_STATE = {"_GUARD", "_OWNED", "_created", "_projects", "_named", "_assign_failed"}
TABLES = {"ALLOWED", "PATHS", "BODIES", "OWNED_WRITES"}
PATCHABLE = TABLES | {"monotonic", "utcnow", "require_guard", "check_allowed",
                      "OWN_UNASSIGNED_SECONDS", "PROJECT_CACHE_SECONDS"}
MUTATORS = {"update", "setdefault", "pop", "popitem", "clear", "__setitem__", "__delitem__"}


def _target_names(node):
    targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
    for target in targets:
        while isinstance(target, ast.Subscript):
            target = target.value
        if isinstance(target, (ast.Attribute, ast.Name)):
            yield target.attr if isinstance(target, ast.Attribute) else target.id


def tampering(source: str, reflective: bool) -> list[str]:
    """r1: touching the guard's private state or patching its tables, from outside the pair.

    `reflective` also refuses vars/setattr/delattr/globals and a getattr that is not a plain
    public attribute of `self` (campaign code only; the rest of src/ uses getattr freely).
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in PRIVATE_STATE | {"__dict__"}:
            found.append(f"private {node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and (
                node.value in PRIVATE_STATE or node.value.startswith("_GU")):
            found.append(f"private name {node.value!r}")
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
            found += [f"patches {t}" for t in _target_names(node) if t in PATCHABLE]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and (
                node.func.attr in MUTATORS) and _call_name(
                ast.Call(func=node.func.value, args=[], keywords=[])) in TABLES:
            found.append(f"mutates {node.func.attr}")
        elif reflective and isinstance(node, ast.Call) and _call_name(node) in {
                "vars", "setattr", "delattr", "globals"}:
            found.append(f"reflection {_call_name(node)}")
        elif reflective and isinstance(node, ast.Call) and _call_name(node) == "getattr" and not (
                isinstance(node.args[0], ast.Name) and node.args[0].id == "self"):
            found.append("reflection getattr")
    return found


def scan_violations(source: str, name: str) -> list[str]:
    """Rule breaks in one campaign source file; scope.py and do_api.py are the guarded pair."""
    tree, guarded = ast.parse(source), name in ("scope.py", "do_api.py")
    strings, names, found = _strings(tree), _names(tree), []
    if not guarded and any(DELETE_WORD.search(v) for v in strings):
        found.append("delete word")
    if not guarded and names & {"_GUARD", "_OWNED", "request", "_attempt", "_session",
                                  "aiohttp"}:
        found.append("raw client access")
    if re.search(r"/v2/tags/[^\n]*resources", source):  # CAMP-1h: never tag or untag
        found.append("tag resources")
    if name not in ("scope.py", "contracts.py") and "assign_failed" in strings:
        found.append("assign_failed set outside scope.py")
    for call in _calls(tree):
        call_name = _call_name(call).lower()
        if call_name != "guarded_delete" and any(
                word in call_name for word in ("delete", "destroy", "remove_droplet")):
            found.append(f"call {call_name}")
        if name != "scope.py" and call_name == "assign_project":
            found.append("assign_project outside scope.py")
    return found + ([] if guarded else tampering(source, reflective=True))


def side_doors(source: str) -> list[str]:
    """Ways around the client from any module or script (m4)."""
    private = {"_GUARD", "_OWNED", "_attempt", "_session", "_created", "_assign_failed",
               "_named"}
    tree, found = ast.parse(source), []
    if "api.digitalocean.com" in source or re.search(r"\bdoctl\b", source):
        found.append("direct API or doctl")
    imports = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
               and (n.module or "").endswith(("do_api", "scope"))]
    if {a.name for n in imports for a in n.names} & private or _names(tree) & {
            "_GUARD", "_OWNED", "_assign_failed"}:
        found.append("private client name")
    return found + tampering(source, reflective=False)


@pytest.mark.parametrize("source", [
    'await cloud.request("delete", "/v2/droplets", params={"tag_name": "prod"})',
    'await cloud._session.request("Delete", url)',
    'run(["doctl", "compute", "droplet", "delete", "1"])',
    'await cloud.assign_project(pid, ids, guard=token)',
    'import aiohttp',
    'record.status = "assign_failed"',
    'await cloud.destroy(droplet)',
])
def test_the_scans_catch_known_bypasses(source):
    """B1 and m4: each of these passed the 53ca3ea scan."""
    assert scan_violations(source, "launch.py")
    assert side_doors('URL = "https://api.digitalocean.com/v2"') and side_doors(
        "from proxy_index.campaign.scope import _GUARD") and side_doors("x = ['doctl']")
    assert side_doors("from proxy_index.campaign.do_api import _OWNED")


@pytest.mark.parametrize("source", [
    'do_api.ALLOWED[("POST", "/v2/droplets/1/actions")] = (frozenset(), False)',
    'do_api.ALLOWED.update({})',
    'scope.PATHS["volume"] = "/v2/volumes/{}"',
    'cloud._created.add("123")',
    'getattr(cloud, "_created")',
    'scope._projects[cloud] = {"p": (0, {"1"})}',
    'scope._named[cloud] = {"p"}',
    'scope.monotonic = lambda: 0.0',
    'token = vars(scope)["_GU" + "ARD"]',
    'del scope.PATHS',
    'await cloud.request("POST", "/v2/firewalls", body=rules, guard=do_api._OWNED)',
    'token = vars(do_api)["_OWNED"]',
    'do_api.OWNED_WRITES = {}',
    'do_api.BODIES.clear()',
])
def test_the_scans_catch_tampering_with_private_state(source):
    """r1: each of these passed both scans at the reviewed head."""
    assert scan_violations(source, "launch.py") and side_doors(source)


@pytest.mark.parametrize("name", ["do_api.py", "scope.py", "launch.py"])
def test_the_scan_refuses_tagging_resources_even_in_the_guarded_pair(name):
    """CAMP-1h: tags are read and created only; nothing may put one on or off a resource."""
    for source in ('PATH = f"/v2/tags/{tag}/resources"',
                   'await self._session.request("DELETE", "/v2/tags/" + tag + "/resources")'):
        assert "tag resources" in scan_violations(source, name)


def test_tags_are_only_read_and_created_behind_the_token():
    """CAMP-1h: exactly these two tag calls, both needing the client's own token."""
    tag_calls = {key for key in do_api.ALLOWED if "/tags" in key[1]}
    assert tag_calls == {("GET", f"/v2/tags/{do_api.TAG}"), ("POST", "/v2/tags")}
    assert all(do_api._lookup(do_api.OWNED_WRITES, m, p) for m, p in (
        ("GET", "/v2/tags/pi-camp-worker"), ("POST", "/v2/tags")))
    for method, path in (("GET", "/v2/tags"), ("GET", "/v2/tags/prod"),
                         ("GET", "/v2/tags/pi-camp-worker/resources"),
                         ("POST", "/v2/tags/pi-camp-worker/resources"),
                         ("DELETE", "/v2/tags/pi-camp-worker/resources"),
                         ("DELETE", "/v2/tags/pi-camp-worker"), ("PUT", "/v2/tags/pi-camp-x")):
        with pytest.raises(ScopeError):
            do_api.check_allowed(method, path, None, do_api._OWNED, {"resources": []})


def test_the_tables_are_read_only():
    for table, key in ((do_api.ALLOWED, ("POST", "/v2/droplets/1/actions")),
                       (scope.PATHS, "volume"), (do_api.BODIES, ("PUT", "/v2/x")),
                       (do_api.OWNED_WRITES, ("PUT", "/v2/x"))):
        with pytest.raises(TypeError):
            table[key] = None


def test_static_scan_allows_delete_only_behind_the_guard():
    """Covers every file in src/proxy_index/campaign/, including later tasks' files."""
    files = sorted(CAMPAIGN_SRC.rglob("*.py"))
    assert {"scope.py", "do_api.py", "contracts.py"} <= {f.name for f in files}
    for path in files:
        assert scan_violations(path.read_text(), path.name) == [], path.name
    scope_text = (CAMPAIGN_SRC / "scope.py").read_text()
    assert "tag_name" not in scope_text
    deletes = [c for c in _calls(ast.parse(scope_text)) if any(
        isinstance(a, ast.Constant) and str(a.value).upper() == "DELETE" for a in c.args)]
    assert len(deletes) == 1 and {k.arg for k in deletes[0].keywords} == {"guard"}


def test_static_scan_of_all_sources_finds_no_side_door():
    for path in sorted([*(ROOT / "src").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]):
        if not (path.parent == CAMPAIGN_SRC and path.name in ("do_api.py", "scope.py")):
            assert side_doors(path.read_text()) == [], path


def test_cloud_and_client_expose_no_delete():
    for owner in (contracts.Cloud, do_api.DigitalOcean):
        public = [name for name in dir(owner) if not name.startswith("_")]
        assert not [name for name in public if any(
            word in name.lower() for word in ("delete", "destroy", "remove", "purge"))]


def test_scope_coverage_gate(tmp_path):
    """scope.py and do_api.py (allowlist, DELETE and assignment) stay at 100% line and branch."""
    if os.environ.get("CAMPAIGN_SCOPE_GATE") == "inner":
        pytest.skip("inside the gate run")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COV_CORE", "COVERAGE"))}
    env.update(CAMPAIGN_SCOPE_GATE="inner", COVERAGE_FILE=str(tmp_path / ".coverage"))
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", __file__,
         str(Path(__file__).with_name("test_campaign_api.py")),
         "--cov=proxy_index.campaign.scope", "--cov=proxy_index.campaign.do_api",
         "--cov-branch", "--cov-fail-under=100", "--cov-report=term-missing"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=240, check=False,
    )
    assert run.returncode == 0, run.stdout[-3000:]
