"""The safety core: only droplets this campaign created can ever be deleted.

The fake account is seeded like the real one: 1,125 production droplets in other projects, plus
every near-miss of our ownership markers. Every destructive path runs against it. This file must
keep scope.py and do_api.py at 100% line and branch coverage
(`test_scope_coverage_gate`).
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from campaign_fakes import (
    CAMPAIGN_PROJECT,
    CID,
    EARLY_TIME,
    MANAGER_ID,
    OTHER_SID,
    SID,
    SPEC_TIME,
    FakeDigitalOcean,
    Fault,
    running,
)

from proxy_index.campaign import contracts, do_api, scope
from proxy_index.campaign.contracts import BUILDER_TAG, MANAGER_TAG, WORKER_TAG, Scope
from proxy_index.campaign.do_api import ApiError, ScopeError
from proxy_index.campaign.scope import OwnershipError, assign_or_destroy, guarded_delete

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_SRC = ROOT / "src" / "proxy_index" / "campaign"
SCOPE = Scope(CID, SID, CAMPAIGN_PROJECT, MANAGER_ID, datetime.fromisoformat(SPEC_TIME))
OURS = [WORKER_TAG, f"pi-camp-{SID}", f"pi-camp-{SID}-h03"]
NAME = f"pic-{SID}-h03-lon1-shifter"
# Near-misses of the ownership markers: (key, name, tags, project, created, failing check).
NEAR_MISSES = [
    ("untagged", "web-legacy", [], "prj-production", None, "tags"),
    ("other_tags", "api-01", ["prod", "k8s"], "prj-production", None, "tags"),
    ("pic_name_without_tags", NAME, ["prod"], CAMPAIGN_PROJECT, None, "tags"),
    ("ours_in_other_project", NAME, OURS, "prj-production", None, "project"),
    ("ours_in_no_project", NAME, OURS, None, None, "project"),
    ("worker_without_campaign_tag", NAME, [WORKER_TAG], CAMPAIGN_PROJECT, None, "tags"),
    ("other_campaign", f"pic-{OTHER_SID}-h03-lon1-shifter",
     [WORKER_TAG, f"pi-camp-{OTHER_SID}"], CAMPAIGN_PROJECT, None, "tags"),
    ("other_campaign_name", f"pic-{OTHER_SID}-h03-lon1-shifter", OURS, CAMPAIGN_PROJECT, None,
     "name"),
    ("bad_name", "pic-01234567-lon1", OURS, CAMPAIGN_PROJECT, None, "name"),
    ("worker_tagged_manager", NAME, [*OURS, MANAGER_TAG], CAMPAIGN_PROJECT, None, "tags"),
    ("created_before_spec", NAME, OURS, CAMPAIGN_PROJECT, EARLY_TIME, "created_after"),
]


def seeded():
    fake = FakeDigitalOcean()
    fake.seed_production()
    near = {key: fake.add(name, tags, project=project, created=created or "2027-01-01T21:58:30Z")
            for key, name, tags, project, created, _ in NEAR_MISSES}
    near["manager"] = fake.add("pi-camp-manager-20270101-ab12", [MANAGER_TAG],
                               project=CAMPAIGN_PROJECT, droplet_id=MANAGER_ID)
    near["builder"] = fake.add("pi-camp-builder-20270101-cd34", [BUILDER_TAG],
                               project=CAMPAIGN_PROJECT)
    owned = {fake.add(f"pic-{SID}-h03-{region}-shifter", OURS, project=CAMPAIGN_PROJECT)
             for region in ("lon1", "fra1", "syd1")}
    return fake, near, owned


def audit_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


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
                                            everything, audit, role=role) == "deleted":
                        results[role].add(droplet_id)
                except OwnershipError:
                    pass
    assert results == {"worker": owned, "manager": {MANAGER_ID}, "builder": {near["builder"]}}
    assert set(fake.droplets) == everything - owned - {MANAGER_ID, near["builder"]}
    assert len([d for d in fake.droplets.values() if d["name"].startswith("prod-")]) == 1125
    deletes = [(m, p) for m, p, _, _ in fake.requests if m == "DELETE"]
    assert {p.rsplit("/", 1)[1] for _, p in deletes} == owned | {MANAGER_ID, near["builder"]}
    assert all(not query for m, _, query, _ in fake.requests if m == "DELETE")
    assert not [entry for entry in fake.deleted if entry[0] == "bulk"]
    lines = audit_lines(audit)
    assert len(lines) == 3 * len(everything)
    assert {line["result"] for line in lines} == {"deleted", "refused", "absent"}
    assert audit.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("variant", NEAR_MISSES, ids=[item[0] for item in NEAR_MISSES])
async def test_each_near_miss_is_refused_and_audited(tmp_path, variant):
    fake, near, _ = seeded()
    target, audit = near[variant[0]], tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        with pytest.raises(OwnershipError) as refused:
            await guarded_delete(cloud, "droplet", target, SCOPE, {target}, audit)
    assert variant[-1] in refused.value.failed
    assert target in fake.droplets and not fake.deleted
    [line] = audit_lines(audit)
    assert line["result"] == "refused" and line["checks"][variant[-1]] is False
    assert line["id"] == target and line["campaign"] == SID


async def test_worker_delete_never_targets_the_manager_even_with_worker_markers(tmp_path):
    fake = FakeDigitalOcean()
    fake.add(NAME, OURS, project=CAMPAIGN_PROJECT, droplet_id=MANAGER_ID)
    async with running(fake) as cloud:
        with pytest.raises(OwnershipError) as refused:
            await guarded_delete(cloud, "droplet", MANAGER_ID, SCOPE, {MANAGER_ID},
                                 tmp_path / "audit.jsonl")
        with pytest.raises(OwnershipError) as as_manager:
            await guarded_delete(cloud, "droplet", MANAGER_ID, SCOPE, {MANAGER_ID},
                                 tmp_path / "audit.jsonl", role="manager")
    assert refused.value.failed == ["not_manager"]
    assert "tags" in as_manager.value.failed and not fake.deleted


async def test_an_unrecorded_owned_droplet_is_refused(tmp_path):
    fake, _, owned = seeded()
    target = sorted(owned)[0]
    async with running(fake) as cloud:
        with pytest.raises(OwnershipError) as refused:
            await guarded_delete(cloud, "droplet", target, SCOPE, set(owned) - {target},
                                 tmp_path / "audit.jsonl")
    assert refused.value.failed == ["recorded"] and target in fake.droplets


async def test_a_stale_project_cache_is_refreshed_before_a_refusal(tmp_path):
    fake = FakeDigitalOcean()
    first = fake.add(NAME, OURS, project=CAMPAIGN_PROJECT)
    late = fake.add(f"pic-{SID}-h03-fra1-shifter", OURS, project=None)
    moved = fake.add(f"pic-{SID}-h03-syd1-shifter", OURS, project=CAMPAIGN_PROJECT)
    audit, now = tmp_path / "audit.jsonl", [0.0]
    recorded = {first, late, moved}
    async with running(fake) as cloud:
        await guarded_delete(cloud, "droplet", first, SCOPE, recorded, audit, clock=lambda: now[0])
        fake.projects[CAMPAIGN_PROJECT].add(late)  # assigned after the cache was filled
        assert await guarded_delete(cloud, "droplet", late, SCOPE, recorded, audit,
                                    clock=lambda: now[0]) == "deleted"
        fake.projects[CAMPAIGN_PROJECT].discard(moved)  # moved out: the cache expires in 60 s
        now[0] = 61.0
        with pytest.raises(OwnershipError) as refused:
            await guarded_delete(cloud, "droplet", moved, SCOPE, recorded, audit,
                                 clock=lambda: now[0])
    assert refused.value.failed == ["project"]
    project_reads = [p for m, p, _, _ in fake.requests if p.endswith("/resources")]
    assert len(project_reads) == 3


async def test_an_absent_droplet_is_audited_and_not_deleted(tmp_path):
    fake, audit = FakeDigitalOcean(), tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        assert await guarded_delete(cloud, "droplet", "123", SCOPE, {"123"}, audit) == "absent"
    assert audit_lines(audit)[0]["result"] == "absent" and not fake.deleted


@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_api_errors_are_audited_and_raised(tmp_path, method):
    fake = FakeDigitalOcean()
    target = fake.add(NAME, OURS, project=CAMPAIGN_PROJECT)
    fake.faults.append(Fault(method, f"/v2/droplets/{target}", 403))
    audit = tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        with pytest.raises(ApiError):
            await guarded_delete(cloud, "droplet", target, SCOPE, {target}, audit)
    [line] = audit_lines(audit)
    assert line["result"] == "failed" and line["http"] == 403 and target in fake.droplets


@pytest.mark.parametrize(
    ("kind", "values", "recorded", "failed"),
    [
        ("ssh_key", {"name": "pi-camp-worker-20270101"}, True, None),
        ("ssh_key", {"name": "deploy-key"}, True, "name"),
        ("ssh_key", {"name": "pi-camp-worker-20270101"}, False, "recorded"),
        ("snapshot", {"name": "pi-camp-worker-0.9.5-abc-20270101"}, True, None),
        ("snapshot", {"name": "prod-backup"}, True, "name"),
        ("firewall", {"name": "pi-camp-worker-fw", "tags": [WORKER_TAG], "droplet_ids": [7]},
         True, None),
        ("firewall", {"name": "pi-camp-worker-fw", "tags": ["prod"]}, True, "targets"),
        ("firewall", {"name": "pi-camp-worker-fw", "droplet_ids": [8]}, True, "targets"),
    ],
)
async def test_keys_firewalls_and_snapshots_need_a_record_and_our_prefix(
    tmp_path, kind, values, recorded, failed
):
    fake = FakeDigitalOcean()
    rid = fake.add_resource(kind, "f00d-1", **values)
    known = {rid, "7"} if recorded else {"7"}
    async with running(fake) as cloud:
        call = guarded_delete(cloud, kind, rid, SCOPE, known, tmp_path / "audit.jsonl")
        if failed is None:
            assert await call == "deleted"
            assert fake.deleted == [(kind, rid)]
        else:
            with pytest.raises(OwnershipError) as refused:
                await call
            assert refused.value.failed == [failed] and not fake.deleted


async def test_unknown_kind_or_role_is_rejected(tmp_path):
    async with running(FakeDigitalOcean()) as cloud:
        for kind, role in (("volume", "worker"), ("droplet", "admin")):
            with pytest.raises(ValueError):
                await guarded_delete(cloud, kind, "1", SCOPE, {"1"}, tmp_path / "a", role=role)


async def test_the_client_refuses_every_delete_without_the_guard():
    fake = FakeDigitalOcean()
    target = fake.add(NAME, OURS, project=CAMPAIGN_PROJECT)
    async with running(fake) as cloud:
        attempts = [
            dict(path=f"/v2/droplets/{target}"),
            dict(path=f"/v2/droplets/{target}", guard=object()),
            dict(path="/v2/droplets", params={"tag_name": OURS[1]}, guard=scope._DELETE_TOKEN),
            dict(path="/v2/droplets", guard=scope._DELETE_TOKEN),
            dict(path=f"/v2/droplets/{target}?tag_name=x", guard=scope._DELETE_TOKEN),
            dict(path="/v2/volumes/1", guard=scope._DELETE_TOKEN),
        ]
        for attempt in attempts:
            path = attempt.pop("path")
            with pytest.raises(ScopeError):
                await cloud.request("DELETE", path, **attempt)
    assert not fake.requests and target in fake.droplets


async def test_assignment_moves_only_recorded_owned_droplets_and_verifies(tmp_path):
    fake = FakeDigitalOcean()
    production = fake.add("prod-node-1", ["prod"])
    async with running(fake) as cloud:
        created = await cloud.create([NAME, f"pic-{SID}-h03-fra1-shifter"], region="lon1",
                                     size="c-2", image="1", tags=OURS, ssh_keys=["9"])
        ids = {d.id for d in created}
        fake.faults.append(Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 500,
                                 times=5))
        fake.assign_delay, sleeps = 1, []

        async def sleep(seconds):
            sleeps.append(seconds)

        result = await assign_or_destroy(cloud, SCOPE, created, ids, tmp_path / "a.jsonl",
                                         sleep=sleep)
        assert result.assigned == ids and not result.destroyed and not result.refused
        assert sleeps == [15.0, 15.0] and ids <= fake.projects[CAMPAIGN_PROJECT]
        assert await scope.assign_verified(cloud, SCOPE, [], ids) == frozenset()
        stranger = await cloud.get(production)
        for droplets, recorded in (([stranger], {production}), (created, set())):
            with pytest.raises(OwnershipError):
                await scope.assign_verified(cloud, SCOPE, droplets, recorded)
    assert production in fake.projects["prj-production"]


@pytest.mark.parametrize(("delay", "outcome"), [(2, "destroyed"), (99, "refused")])
async def test_failed_assignment_fails_closed_through_the_guard(tmp_path, delay, outcome):
    fake = FakeDigitalOcean()
    audit = tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        created = await cloud.create([NAME], region="lon1", size="c-2", image="1", tags=OURS,
                                     ssh_keys=["9"])
        fake.assign_delay = delay
        result = await assign_or_destroy(cloud, SCOPE, created, {created[0].id}, audit,
                                         attempts=2, delay=0)
    assert not result.assigned and getattr(result, outcome) == {created[0].id}
    assert (created[0].id in fake.droplets) is (outcome == "refused")
    assert audit_lines(audit)[-1]["result"] == ("deleted" if outcome == "destroyed" else "refused")


def test_scope_for_builds_the_campaign_scope():
    spec = type("Spec", (), {"campaign_id": CID, "project_id": CAMPAIGN_PROJECT,
                             "manager_droplet_id": int(MANAGER_ID), "created_at": SPEC_TIME})
    assert scope.scope_for(spec) == SCOPE


def _calls(tree):
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _call_name(node):
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def test_static_scan_allows_delete_only_behind_the_guard():
    """Covers every file in src/proxy_index/campaign/, including later tasks' files."""
    files = sorted(CAMPAIGN_SRC.rglob("*.py"))
    assert {"scope.py", "do_api.py", "contracts.py"} <= {f.name for f in files}
    for path in files:
        tree, text = ast.parse(path.read_text()), path.read_text()
        strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                   and isinstance(n.value, str)]
        guarded = path.name in ("scope.py", "do_api.py")
        assert guarded or not any("DELETE" in value for value in strings), path.name
        assert guarded or "_DELETE_TOKEN" not in text, path.name
        for call in _calls(tree):
            name = _call_name(call).lower()
            assert name == "guarded_delete" or not any(
                word in name for word in ("delete", "destroy", "remove_droplet")), path.name
    scope_tree = ast.parse((CAMPAIGN_SRC / "scope.py").read_text())
    assert "tag_name" not in (CAMPAIGN_SRC / "scope.py").read_text()
    deletes = [c for c in _calls(scope_tree) if any(
        isinstance(a, ast.Constant) and a.value == "DELETE" for a in c.args)]
    assert len(deletes) == 1 and {k.arg for k in deletes[0].keywords} == {"guard"}


def test_cloud_and_client_expose_no_delete():
    for owner in (contracts.Cloud, do_api.DigitalOcean):
        public = [name for name in dir(owner) if not name.startswith("_")]
        assert not [name for name in public if any(
            word in name.lower() for word in ("delete", "destroy", "remove", "purge"))]


def test_scope_coverage_gate(tmp_path):
    """scope.py and do_api.py (the DELETE refusal, assignment) stay at 100% line and branch."""
    if os.environ.get("CAMPAIGN_SCOPE_GATE") == "inner":
        pytest.skip("inside the gate run")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COV_CORE", "COVERAGE"))}
    env.update(CAMPAIGN_SCOPE_GATE="inner", COVERAGE_FILE=str(tmp_path / ".coverage"))
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", __file__,
         str(Path(__file__).with_name("test_campaign_api.py")),
         "--cov=proxy_index.campaign.scope", "--cov=proxy_index.campaign.do_api",
         "--cov-branch", "--cov-fail-under=100",
         "--cov-report=term-missing"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=240, check=False,
    )
    assert run.returncode == 0, run.stdout[-3000:]
