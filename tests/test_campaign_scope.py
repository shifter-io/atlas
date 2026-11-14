"""The safety core: only droplets this campaign created can ever be deleted or moved.

The fake account is seeded like the real one: 1,125 production droplets in other projects, plus
every near-miss of our ownership markers. Every destructive path runs against it. The scope
suite (`tests/test_campaign_scope*.py`) and tests/test_campaign_api*.py must keep scope.py and
do_api.py at 100% line and branch coverage (`test_scope_coverage_gate`). This file covers
`guarded_delete`; the client's allowlist, project assignment and the static scans have their own.
"""

from __future__ import annotations

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
    PROD_PROJECT,
    SID,
    SPEC_TIME,
    FakeDigitalOcean,
    Fault,
    running,
)
from campaign_scope_cases import (
    NAME,
    NEAR_MISSES,
    OURS,
    ROOT,
    SCOPE,
    audit_lines,
    droplets,
    seeded,
)

from atlas.campaign import scope
from atlas.campaign.contracts import WORKER_TAG, Scope
from atlas.campaign.do_api import ApiError
from atlas.campaign.scope import Ledger, OwnershipError, guarded_delete


async def test_every_destructive_path_over_the_whole_account_deletes_only_ours(
    tmp_path, monkeypatch
):
    # Durability is not under test here, and the audit's ~3,400 fsync calls were most of its
    # time (CI-90-3). Every audit line is still written and read back below.
    monkeypatch.setattr(os, "fsync", lambda fd: None)
    fake, near, owned = seeded()
    everything = set(fake.droplets)  # worst case: a corrupted ledger lists every droplet
    audit = tmp_path / "ledger" / "delete-audit.jsonl"
    early = Scope(CID, SID, CAMPAIGN_PROJECT, MANAGER_ID, datetime.fromisoformat(EARLY_TIME))
    results = {"worker": set(), "manager": set(), "builder": set()}
    async with running(fake, per_minute=10**6) as cloud:
        for role, role_scope in (("worker", SCOPE), ("manager", early), ("builder", SCOPE)):
            for droplet_id in sorted(everything):
                try:
                    if (
                        await guarded_delete(
                            cloud,
                            "droplet",
                            droplet_id,
                            role_scope,
                            droplets(everything),
                            audit,
                            role,
                        )
                        == "deleted"
                    ):
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
            await guarded_delete(
                cloud, "droplet", target, SCOPE, droplets({target}), audit, variant[-1]
            )
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
            await guarded_delete(
                cloud,
                "droplet",
                MANAGER_ID,
                SCOPE,
                droplets({MANAGER_ID}),
                tmp_path / "audit.jsonl",
            )
        with pytest.raises(OwnershipError) as as_manager:
            await guarded_delete(
                cloud,
                "droplet",
                MANAGER_ID,
                SCOPE,
                droplets({MANAGER_ID}),
                tmp_path / "audit.jsonl",
                "manager",
            )
    assert refused.value.failed == ["not_manager"]
    assert "tags" in as_manager.value.failed and not fake.deleted


async def test_an_unrecorded_owned_droplet_is_refused(tmp_path):
    fake, _, owned = seeded()
    target = sorted(owned)[0]
    async with running(fake) as cloud:
        with pytest.raises(OwnershipError) as refused:
            await guarded_delete(
                cloud,
                "droplet",
                target,
                SCOPE,
                droplets(owned - {target}),
                tmp_path / "audit.jsonl",
            )
    assert refused.value.failed == ["recorded"] and target in fake.droplets


async def test_a_stale_project_cache_is_refreshed_before_a_refusal(tmp_path, monkeypatch):
    fake = FakeDigitalOcean()
    first = fake.add(NAME, OURS, project=CAMPAIGN_PROJECT)
    late = fake.add(f"atlas-{SID}-h03-fra1-shifter", OURS, project=None)
    moved = fake.add(f"atlas-{SID}-h03-syd1-shifter", OURS, project=CAMPAIGN_PROJECT)
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


async def test_a_project_id_that_is_not_atlas_refuses_every_delete(tmp_path):
    """m6: the scope's project id must be the project named Atlas."""
    fake = FakeDigitalOcean()
    target = fake.add(NAME, OURS, project=PROD_PROJECT)
    wrong = Scope(CID, SID, PROD_PROJECT, MANAGER_ID, datetime.fromisoformat(SPEC_TIME))
    async with running(fake) as cloud:
        with pytest.raises(OwnershipError) as refused:
            await guarded_delete(
                cloud, "droplet", target, wrong, droplets({target}), tmp_path / "audit.jsonl"
            )
    assert refused.value.failed == ["project_id"] and target in fake.droplets


async def test_an_absent_droplet_is_audited_and_not_deleted(tmp_path):
    fake, audit = FakeDigitalOcean(), tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        assert (
            await guarded_delete(cloud, "droplet", "123", SCOPE, droplets({"123"}), audit)
            == "absent"
        )
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
        ["failed"] if method == "GET" else ["attempt", "failed"]
    )


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
        ("ssh_key", "41", {"name": "atlas-camp-worker-20270101"}, True, None),
        ("ssh_key", "41", {"name": "deploy-key"}, True, "name"),
        ("ssh_key", "41", {"name": "atlas-camp-worker-20270101"}, False, "recorded"),
        ("snapshot", "42", {"name": "atlas-camp-worker-0.9.5-abc-20270101"}, True, None),
        ("snapshot", "42", {"name": "prod-backup"}, True, "name"),
        (
            "firewall",
            FIREWALL,
            {"name": "atlas-camp-worker-fw", "tags": [WORKER_TAG], "droplet_ids": [7]},
            True,
            None,
        ),
        ("firewall", FIREWALL, {"name": "atlas-camp-worker-fw", "tags": ["prod"]}, True, "targets"),
        ("firewall", FIREWALL, {"name": "atlas-camp-worker-fw", "droplet_ids": [8]}, True, "targets"),
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
    fake.add_resource("ssh_key", "700001", name="atlas-camp-worker-20270101")
    async with running(fake) as cloud:
        with pytest.raises(OwnershipError) as refused:
            await guarded_delete(
                cloud, "ssh_key", "700001", SCOPE, droplets({"700001"}), tmp_path / "audit.jsonl"
            )
    assert refused.value.failed == ["recorded"] and not fake.deleted


async def test_unknown_kind_or_role_is_rejected(tmp_path):
    async with running(FakeDigitalOcean()) as cloud:
        for kind, role in (("volume", "worker"), ("droplet", "admin")):
            with pytest.raises(ValueError):
                await guarded_delete(cloud, kind, "1", SCOPE, droplets({"1"}), tmp_path / "a", role)


def test_scope_for_builds_the_campaign_scope():
    spec = type(
        "Spec",
        (),
        {
            "campaign_id": CID,
            "project_id": CAMPAIGN_PROJECT,
            "manager_droplet_id": int(MANAGER_ID),
            "created_at": SPEC_TIME,
            "environment": "production",
        },
    )
    assert scope.scope_for(spec) == SCOPE


def test_scope_coverage_gate(tmp_path):
    """scope.py and do_api.py (allowlist, DELETE and assignment) stay at 100% line and branch."""
    if os.environ.get("CAMPAIGN_SCOPE_GATE") == "inner":
        pytest.skip("inside the gate run")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COV_CORE", "COVERAGE"))}
    env.update(CAMPAIGN_SCOPE_GATE="inner", COVERAGE_FILE=str(tmp_path / ".coverage"))
    # Three xdist workers, longest test first as in CI (tests/conftest.py): the run takes
    # about as long as its longest test (CI-90-3); pytest-cov combines the workers' data
    # before the 100% check.
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            "-n",
            "3",
            "--dist",
            "worksteal",
            *map(str, sorted(Path(__file__).parent.glob("test_campaign_scope*.py"))),
            *map(str, sorted(Path(__file__).parent.glob("test_campaign_api*.py"))),
            str(Path(__file__).with_name("test_campaign_unassigned.py")),
            str(Path(__file__).with_name("test_campaign_unassigned_scope.py")),
            str(Path(__file__).with_name("test_campaign_assign_race.py")),
            str(Path(__file__).with_name("test_campaign_pulse_role.py")),
            "--cov=atlas.campaign.scope",
            "--cov=atlas.campaign.do_api",
            "--cov-branch",
            "--cov-fail-under=100",
            "--cov-report=term-missing",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert run.returncode == 0, run.stdout[-3000:]
