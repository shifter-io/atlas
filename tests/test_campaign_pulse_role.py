"""PULSE-ROLE-1: the always-on pulse node role. Campaign cleanup never touches a pulse node, and
only `scope.retire_pulse_node` removes one, through the guard (fakes only)."""

from __future__ import annotations

import ast
import json
import re
from datetime import datetime
from pathlib import Path

import pytest
from campaign_deploy_fakes import Shell, clean_report, fast_assign, make_job, owned_world
from campaign_fakes import (
    CAMPAIGN_PROJECT,
    CID,
    EARLY_TIME,
    MANAGER_ID,
    PROD_PROJECT,
    SID,
    SPEC_TIME,
    FakeDigitalOcean,
    no_sleep,
    running,
)

from proxy_index.campaign import contracts, deploy, do_api
from proxy_index.campaign.contracts import (
    BUILDER_TAG,
    MANAGER_TAG,
    PULSE_TAG,
    WORKER_TAG,
    DropletRecord,
    Scope,
)
from proxy_index.campaign.do_api import ScopeError
from proxy_index.campaign.scope import (
    Ledger,
    OwnershipError,
    Retry,
    assign_and_verify_project,
    guarded_delete,
    pulse_scope,
    remove_unassigned,
    retire_pulse_node,
)

DEV_PROJECT = "33333333-3333-4333-8333-333333333333"
SCOPE = Scope(CID, SID, CAMPAIGN_PROJECT, MANAGER_ID, datetime.fromisoformat(SPEC_TIME))
OURS = [WORKER_TAG, f"pi-camp-{SID}", f"pi-camp-{SID}-h03"]
NAME = f"pic-{SID}-h03-lon1-shifter"
PULSE_NAME = "pi-pulse-lon1-shifter"
# Pulse near-misses: (key, name, tags, project, failing checks).
PULSE_MISSES = [
    ("in_campaign_project", PULSE_NAME, [PULSE_TAG], CAMPAIGN_PROJECT, ["project"]),
    ("in_production", PULSE_NAME, [PULSE_TAG], PROD_PROJECT, ["project"]),
    ("with_camp_tag", PULSE_NAME, [PULSE_TAG, f"pi-camp-{SID}"], DEV_PROJECT, ["tags"]),
    ("untagged", PULSE_NAME, ["prod"], DEV_PROJECT, ["tags"]),
    ("bad_name", "pulse-lon1", [PULSE_TAG], DEV_PROJECT, ["name"]),
    ("a_worker", NAME, OURS, DEV_PROJECT, ["tags", "name"]),
]


def world():
    fake = FakeDigitalOcean()
    fake.projects[DEV_PROJECT] = set()
    fake.project_names[DEV_PROJECT] = "Dev Resources"
    fake.seed_production(50)
    pulse = {fake.add(f"pi-pulse-{r}-shifter", [PULSE_TAG], project=DEV_PROJECT) for r in "ab"}
    near = {key: fake.add(n, t, project=p) for key, n, t, p, _ in PULSE_MISSES}
    # Every campaign marker, plus the pulse tag: before PULSE-ROLE-1 a worker delete took it.
    near["worker_with_pulse_tag"] = fake.add(NAME, [*OURS, PULSE_TAG], project=CAMPAIGN_PROJECT)
    manager = "pi-camp-manager-20270101-ab12"
    fake.add(manager, [MANAGER_TAG], project=CAMPAIGN_PROJECT, droplet_id=MANAGER_ID)
    builder = fake.add("pi-camp-builder-20270101-cd34", [BUILDER_TAG], project=CAMPAIGN_PROJECT)
    workers = {fake.add(NAME, OURS, project=CAMPAIGN_PROJECT) for _ in range(2)}
    return fake, pulse, near, workers | {MANAGER_ID, builder}


def everything(fake):
    return Ledger({"droplet": set(fake.droplets)})


def test_the_pulse_role_is_its_own_and_the_campaign_roles_are_unchanged():
    assert contracts.CAMPAIGN_ROLES == {
        "worker": (WORKER_TAG, contracts.WORKER_NAME_RE),
        "manager": (MANAGER_TAG, contracts.MANAGER_NAME_RE),
        "builder": (BUILDER_TAG, contracts.BUILDER_NAME_RE),
    }
    assert contracts.ROLES == {
        **contracts.CAMPAIGN_ROLES,
        "pulse": (PULSE_TAG, contracts.PULSE_NAME_RE),
    }
    assert re.fullmatch(contracts.PULSE_NAME_RE, PULSE_NAME)
    assert not any(re.fullmatch(contracts.PULSE_NAME_RE, n) for n in (NAME, "pi-pulse-lon1"))
    assert not PULSE_TAG.startswith(contracts.RESOURCE_PREFIX)


def test_a_pulse_create_needs_only_pulse_tags_and_its_names():
    do_api.check_create([PULSE_NAME, "pi-pulse-nyc3-geonode"], [PULSE_TAG], ["9"])
    do_api.check_create([PULSE_NAME], [PULSE_TAG, "pi-pulse-shifter"], ["9"])
    for names, tags in (
        ([PULSE_NAME], [PULSE_TAG, WORKER_TAG]),
        ([PULSE_NAME], [PULSE_TAG, f"pi-camp-{SID}"]),
        ([PULSE_NAME], [PULSE_TAG, "prod"]),
        ([PULSE_NAME], ["pi-pulse-shifter"]),
        ([NAME], [PULSE_TAG]),
        ([PULSE_NAME], OURS),
        ([NAME], [*OURS, "pi-pulse-shifter"]),
    ):
        with pytest.raises(ScopeError):
            do_api.check_create(names, tags, ["9"])
    do_api.check_create([NAME, NAME], OURS, ["9"])
    with pytest.raises(ScopeError, match="one at a time"):
        do_api.check_create(["pi-camp-builder-20270101-cd34"] * 2, [BUILDER_TAG], ["9"])


def test_pulse_nodes_are_listed_only_by_their_exact_tag():
    do_api.check_allowed("GET", "/v2/droplets", {"tag_name": PULSE_TAG}, None)
    for tag in ("pi-pulse-shifter", "prod", "pi-pulse-node-x"):
        with pytest.raises(ScopeError, match="listed only"):
            do_api.check_allowed("GET", "/v2/droplets", {"tag_name": tag}, None)


async def test_campaign_deletes_over_the_whole_account_never_touch_a_pulse_node(tmp_path):
    fake, pulse, near, ours = world()
    ledger, audit = everything(fake), tmp_path / "audit.jsonl"
    early = Scope(CID, SID, CAMPAIGN_PROJECT, MANAGER_ID, datetime.fromisoformat(EARLY_TIME))
    deleted = set()
    async with running(fake, per_minute=10**6) as cloud:
        for role, role_scope in (("worker", SCOPE), ("manager", early), ("builder", SCOPE)):
            for droplet_id in sorted(ledger.ids["droplet"]):
                try:
                    result = await guarded_delete(
                        cloud, "droplet", droplet_id, role_scope, ledger, audit, role
                    )
                except OwnershipError:
                    continue
                if result == "deleted":
                    deleted.add(droplet_id)
    assert deleted == ours
    assert pulse | {near["worker_with_pulse_tag"]} <= set(fake.droplets)


async def test_retire_pulse_node_deletes_only_a_recorded_pulse_node_in_dev_resources(tmp_path):
    fake, pulse, near, ours = world()
    audit, target = tmp_path / "audit.jsonl", min(pulse)
    async with running(fake) as cloud:
        for key, *_, failed in PULSE_MISSES:
            with pytest.raises(OwnershipError) as refused:
                await retire_pulse_node(cloud, near[key], DEV_PROJECT, everything(fake), audit)
            assert refused.value.failed == failed, key
        with pytest.raises(OwnershipError) as refused:
            await retire_pulse_node(cloud, target, DEV_PROJECT, Ledger({"droplet": ours}), audit)
        assert refused.value.failed == ["recorded"]
        with pytest.raises(OwnershipError) as refused:
            await retire_pulse_node(cloud, target, CAMPAIGN_PROJECT, everything(fake), audit)
        assert refused.value.failed == ["project_id", "project"]
        assert await retire_pulse_node(cloud, target, DEV_PROJECT, everything(fake), audit) == (
            "deleted"
        )
    assert fake.deleted == [("droplet", target)] and max(pulse) in fake.droplets
    lines = [json.loads(line) for line in audit.read_text().splitlines()]
    assert {(line["role"], line["campaign"]) for line in lines} == {("pulse", "pulse")}
    assert [line["result"] for line in lines[-2:]] == ["attempt", "deleted"]


@pytest.mark.parametrize(
    "kind, role, use_pulse_scope",
    [("droplet", "pulse", False), ("droplet", "worker", True), ("ssh_key", "pulse", True)],
)
async def test_the_pulse_role_and_the_pulse_scope_go_only_together(
    tmp_path, kind, role, use_pulse_scope
):
    fake, pulse, _, _ = world()
    role_scope, audit = pulse_scope(DEV_PROJECT) if use_pulse_scope else SCOPE, tmp_path / "a"
    async with running(fake) as cloud:
        with pytest.raises(ValueError, match="retire_pulse_node"):
            await guarded_delete(cloud, kind, min(pulse), role_scope, everything(fake), audit, role)
    assert not fake.deleted and not audit.exists()


async def test_teardown_ignores_a_live_pulse_node(tmp_path, monkeypatch):
    fast_assign(monkeypatch)
    job = make_job(tmp_path)
    fake, _, _ = await owned_world(job)
    node = fake.add(PULSE_NAME, [PULSE_TAG])
    async with running(fake) as cloud:
        done = await deploy.teardown(
            cloud, job.inventory, [clean_report(tmp_path)], shell=Shell(), remove_agent=lambda: True
        )
    assert done["clean"] is True and PULSE_TAG not in done["tag_counts"]
    assert node in fake.droplets and ("droplet", node) not in fake.deleted


FAST = Retry(sleep=no_sleep)


async def created_pulse_node(cloud):
    """A pulse node from this client's own create (it lands in the default project)."""
    [droplet] = await cloud.create(
        [PULSE_NAME], region="lon1", size="s-1vcpu-1gb-amd", image="1", tags=[PULSE_TAG],
        ssh_keys=["9"],
    )  # fmt: skip
    return droplet.id, {droplet.id: DropletRecord("shifter", "lon1", 0, PULSE_NAME, droplet.id)}


async def test_a_pulse_node_is_assigned_into_dev_resources_with_the_pulse_scope(tmp_path):
    fake, *_ = world()
    async with running(fake) as cloud:
        rid, records = await created_pulse_node(cloud)
        confirmed, failed = await assign_and_verify_project(
            cloud, pulse_scope(DEV_PROJECT), [rid], records, tmp_path / "a", "pulse", FAST
        )
    assert (confirmed, failed) == ({rid}, set()) and records[rid].status == "assigned"
    assert rid in fake.projects[DEV_PROJECT] and not fake.deleted


@pytest.mark.parametrize("project", [CAMPAIGN_PROJECT, PROD_PROJECT])
async def test_a_pulse_assignment_into_a_project_not_named_dev_resources_is_refused(
    tmp_path, project
):
    fake, *_ = world()
    async with running(fake) as cloud:
        rid, records = await created_pulse_node(cloud)
        with pytest.raises(OwnershipError) as refused:
            await assign_and_verify_project(
                cloud, pulse_scope(project), [rid], records, tmp_path / "a", "pulse", FAST
            )
    assert refused.value.failed == ["project_id"]
    assert not [r for r in fake.requests if r[0] == "POST" and r[1].endswith("/resources")]
    assert rid in fake.projects[PROD_PROJECT] and not fake.deleted


async def test_an_unassigned_pulse_node_is_never_deleted_even_inside_dev_resources(tmp_path):
    fake, *_ = world()
    async with running(fake) as cloud:
        rid, records = await created_pulse_node(cloud)
        fake.move(rid, DEV_PROJECT)  # the membership shows only after the assign gave up
        await remove_unassigned(
            cloud, pulse_scope(DEV_PROJECT), records, tmp_path / "a", {rid: {}}, "pulse"
        )
    assert records[rid].status == "destroy_refused" and records[rid].assign_failed_at
    assert rid in fake.droplets and not fake.deleted
    assert not [r for r in fake.requests if r[0] == "DELETE"]


@pytest.mark.parametrize("role, use_pulse_scope", [("pulse", False), ("worker", True)])
async def test_assignment_and_its_fail_closed_path_refuse_a_role_scope_mismatch(
    tmp_path, role, use_pulse_scope
):
    fake, *_ = world()
    role_scope = pulse_scope(DEV_PROJECT) if use_pulse_scope else SCOPE
    async with running(fake) as cloud:
        rid, records = await created_pulse_node(cloud)
        with pytest.raises(ValueError, match="pulse scope"):
            await assign_and_verify_project(
                cloud, role_scope, [rid], records, tmp_path / "a", role, FAST
            )
        with pytest.raises(ValueError, match="pulse scope"):
            await remove_unassigned(cloud, role_scope, records, tmp_path / "a", {rid: {}}, role)
    assert records[rid].status == "requested" and records[rid].assign_failed_at is None
    assert not fake.deleted
    assert [r[1] for r in fake.requests if r[0] == "POST"] == ["/v2/droplets"]


SRC = Path(__file__).resolve().parents[1] / "src" / "proxy_index"
PULSE_HOMES = {"scope.py", "pulse_fleet.py"}  # in src/proxy_index/campaign/
PULSE_NAMES = {"PULSE_SID", "pulse_scope", "retire_pulse_node"}


def pulse_leaks(source: str) -> list[str]:
    """Uses of the pulse scope or of the pulse role: a reference (call, attribute or import) to
    `PULSE_SID` / `pulse_scope` / `retire_pulse_node`, a `Scope(...)` built with "pulse", or
    the role literal "pulse"."""
    found = []
    for node in ast.walk(ast.parse(source)):
        name = (
            node.id if isinstance(node, ast.Name)
            else node.attr if isinstance(node, ast.Attribute)
            else node.name if isinstance(node, ast.alias)
            else None
        )  # fmt: skip
        if name in PULSE_NAMES:
            found.append(name)
        if isinstance(node, ast.Constant) and node.value == "pulse":
            found.append('"pulse"')
        if isinstance(node, ast.Call) and _called(node.func) == "Scope":
            values = [*node.args, *(k.value for k in node.keywords)]
            if any(isinstance(v, ast.Constant) and v.value == "pulse" for v in values):
                found.append("Scope(pulse)")
    return found


def _called(func: ast.expr) -> str | None:
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


@pytest.mark.parametrize(
    "source",
    [
        'Scope("pulse", "pulse", p, "", t)',
        'scope.Scope(cid="pulse", sid=s, project=p, manager=m, spec=t)',
        "from .scope import PULSE_SID",
        'guarded_delete(c, "droplet", i, s, ledger, audit, "pulse")',
        "from .scope import pulse_scope as owned",
        "scope.retire_pulse_node(c, i, p, ledger, audit)",
        "action = scope.pulse_scope",
        'role = "pulse"',
    ],
)
def test_the_pulse_scan_catches_a_use_of_the_pulse_role(source):
    assert pulse_leaks(source)


def test_the_pulse_scan_ignores_other_words():
    assert pulse_leaks('TESTS = ("census", "pulse-fleet", "Pulse", "pulse node")') == []


def test_a_pulse_scope_built_by_hand_is_caught_outside_campaign_too():
    leaks = pulse_leaks('Scope("pulse", "pulse", p, "", t)')
    assert [leak for leak in leaks if leak != '"pulse"'] == ["Scope(pulse)"]


def test_only_scope_and_the_pulse_fleet_use_the_pulse_scope_and_role():
    """The pulse scope anywhere in src, and the role literal in the code that reaches the
    guard (campaign/ and daily/; elsewhere "pulse" names the pulse test), stay in scope.py and
    the pulse fleet module. contracts.py names the role once, in ROLES."""
    homes = {SRC / "campaign" / name for name in PULSE_HOMES}
    assert all(home.exists() for home in homes)
    for path in sorted(SRC.rglob("*.py")):
        if path in homes:
            continue
        leaks = pulse_leaks(path.read_text())
        if path.relative_to(SRC).parts[0] not in ("campaign", "daily"):
            leaks = [leak for leak in leaks if leak != '"pulse"']
        if path == SRC / "campaign" / "contracts.py":
            assert leaks.count('"pulse"') == 1
            leaks = [leak for leak in leaks if leak != '"pulse"']
        assert leaks == [], path.relative_to(SRC)
    for home in homes:
        assert pulse_leaks(home.read_text())
