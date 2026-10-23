"""Campaign contracts and the DigitalOcean client against the in-process fake (no live calls)."""

from __future__ import annotations

import argparse
import json
import logging
import re
import time

import pytest
from aiohttp import web
from campaign_fakes import (
    CAMPAIGN_PROJECT,
    PROD_PROJECT,
    SID,
    TOKEN,
    FakeDigitalOcean,
    Fault,
    running,
)

from proxy_index import cli as main_cli
from proxy_index.campaign import cli as campaign_cli
from proxy_index.campaign import do_api, scope
from proxy_index.campaign.contracts import (
    ACCOUNTS,
    BUILDER_TAG,
    MANAGER_TAG,
    WORKER_TAG,
)
from proxy_index.campaign.do_api import ApiError, ScopeError, TokenError

OURS = [WORKER_TAG, f"pi-camp-{SID}", f"pi-camp-{SID}-h03"]
IP = re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")
BUILDER = "pi-camp-builder-20270101-ab12"
SNAPSHOT = "pi-camp-worker-0.9.5-0123456789ab-20270101"
FIREWALL = "33333333-3333-4333-8333-000000000001"


async def test_list_tag_paginates_200_per_page_and_parses_droplets():
    fake = FakeDigitalOcean()
    for index in range(450):
        fake.add(f"pic-{SID}-h03-lon1-n{index}", OURS, ip=None if index else "203.0.113.9")
    fake.add("prod-1", ["prod"])
    async with running(fake) as cloud:
        found = await cloud.list_tag(OURS[1])
    assert len(found) == 450 and found[0].public_ipv4 == "203.0.113.9"
    assert "203.0.113.9" not in repr(found[0])
    pages = [q for m, p, q, _ in fake.requests if p == "/v2/droplets"]
    assert [q["page"] for q in pages] == ["1", "2", "3"]
    assert {q["per_page"] for q in pages} == {"200"} and {q["tag_name"] for q in pages} == {OURS[1]}


async def test_create_splits_into_batches_of_ten_and_sends_every_option():
    fake = FakeDigitalOcean()
    names = [f"pic-{SID}-h03-lon1-net{i}" for i in range(16)]
    bodies = []
    original = fake._create

    async def capture(request):
        bodies.append(await request.json())
        return await original(request)

    fake._create = capture
    async with running(fake) as cloud:
        created = await cloud.create(names, region="lon1", size="c-2", image="11", tags=OURS,
                                     ssh_keys=["9"], monitoring=True)
    assert [len(b["names"]) for b in bodies] == [10, 6] and len(created) == 16
    assert bodies[0]["ssh_keys"] == ["9"] and bodies[0]["monitoring"] is True
    assert bodies[0]["with_droplet_agent"] is False and bodies[0]["ipv6"] is False


async def test_create_refuses_unknown_flags():
    async with running(FakeDigitalOcean()) as cloud:
        with pytest.raises(ScopeError):
            await cloud.create([f"pic-{SID}-h03-lon1-x"], region="lon1", size="c-2", image="1",
                               tags=OURS, ssh_keys=["9"], backups=True)


@pytest.mark.parametrize(
    ("names", "tags", "keys"),
    [
        (["web-01"], OURS, ["9"]),
        ([f"pic-{SID}-h03-lon1-x"], [WORKER_TAG], ["9"]),
        ([f"pic-{SID}-h03-lon1-x"], [*OURS, "prod"], ["9"]),
        ([f"pic-{SID}-h03-lon1-x"], [*OURS, MANAGER_TAG], ["9"]),
        ([f"pic-{SID}-h03-lon1-x"], OURS, []),
        ([], OURS, ["9"]),
        ([f"pic-{SID}-h03-lon1-x", "pic-89abcdef-h03-lon1-x"], OURS, ["9"]),
        (["pi-camp-manager-20270101-ab12"] * 2, [MANAGER_TAG], ["9"]),
        (["pi-camp-builder-20270101-ab12"], [MANAGER_TAG], ["9"]),
    ],
)
async def test_create_refuses_anything_without_all_markers(names, tags, keys):
    fake = FakeDigitalOcean()
    async with running(fake) as cloud:
        with pytest.raises(ScopeError):
            await cloud.create(names, region="lon1", size="c-2", image="1", tags=tags,
                               ssh_keys=keys)
    assert not fake.requests


async def test_manager_and_builder_create_and_project_assignment_body():
    fake = FakeDigitalOcean()
    async with running(fake) as cloud:
        [manager] = await cloud.create(["pi-camp-manager-20270101-ab12"], region="lon1",
                                       size="s-8vcpu-16gb", image="1", tags=[MANAGER_TAG],
                                       ssh_keys=["9"])
        await cloud.assign_project(CAMPAIGN_PROJECT, [manager.id], guard=scope._GUARD)
        assert await cloud.project_droplet_ids(CAMPAIGN_PROJECT) == {manager.id}
        assert await cloud.project_id("Proxy Index") == CAMPAIGN_PROJECT
        assert await cloud.project_name(CAMPAIGN_PROJECT) == "Proxy Index"
        assert await cloud.project_name("44444444-4444-4444-8444-444444444444") is None
        for bad in ([], ["do:droplet:1"]):
            with pytest.raises(ScopeError):
                await cloud.assign_project(CAMPAIGN_PROJECT, bad, guard=scope._GUARD)
        with pytest.raises(ScopeError):
            await cloud.project_id("Missing")
        assert await cloud.get("424242") is None
        assert cloud.created_ids == {manager.id}
    assert manager.id in fake.projects[CAMPAIGN_PROJECT]


async def test_list_tag_refuses_tags_outside_our_prefix():
    fake = FakeDigitalOcean()
    async with running(fake) as cloud:
        for tag in ("prod", "", "camp-x"):
            with pytest.raises(ScopeError):
                await cloud.list_tag(tag)
    assert not fake.requests


@pytest.mark.parametrize(
    ("fault", "sleeps"),
    [
        (Fault("GET", "/v2/account", 429, {"Retry-After": "5"}), [5.0]),
        (Fault("GET", "/v2/account", 429, {"Retry-After": "900"}), [60.0]),
        (Fault("GET", "/v2/account", 429), [1]),
        (Fault("GET", "/v2/account", 503, times=2), [1, 2]),
    ],
)
async def test_retries_honour_rate_limits_and_back_off(fault, sleeps):
    fake, slept = FakeDigitalOcean(), []
    fake.faults.append(fault)

    async def sleep(seconds):
        slept.append(seconds)

    async with running(fake, sleep=sleep) as cloud:
        assert await cloud.account() == {"droplet_limit": 10000, "status": "active"}
        assert cloud.ratelimit_remaining == 4321
    assert slept == sleeps


async def test_rate_limit_reset_time_is_honoured():
    fake, slept = FakeDigitalOcean(), []
    reset = str(int(time.time()) + 30)
    fake.faults.append(Fault("GET", "/v2/account", 429, {"ratelimit-reset": reset}))

    async def sleep(seconds):
        slept.append(seconds)

    async with running(fake, sleep=sleep) as cloud:
        await cloud.account()
    assert len(slept) == 1 and 1.0 <= slept[0] <= 31


async def test_a_partial_create_keeps_the_droplets_already_created():
    """S1: batch 2 fails; the 10 droplets of batch 1 are returned on the error, not lost."""
    fake = FakeDigitalOcean()
    names = [f"pic-{SID}-h03-lon1-net{i}" for i in range(16)]
    original, calls = fake._create, []

    async def second_fails(request):
        calls.append(1)
        if len(calls) == 2:
            return web.json_response({"id": "unprocessable"}, status=422)
        return await original(request)

    fake._create = second_fails
    async with running(fake) as cloud:
        with pytest.raises(do_api.CreateError) as stopped:
            await cloud.create(names, region="lon1", size="c-2", image="1", tags=OURS,
                               ssh_keys=["9"])
    error = stopped.value
    assert [d.name for d in error.created] == names[:10] and error.pending == names[10:]
    assert not error.uncertain and error.status == 422
    assert {d.id for d in error.created} == set(fake.droplets) == cloud.created_ids


async def test_an_uncertain_create_is_never_retried_and_can_be_adopted():
    """S1: a create answered 5xx may exist; one POST only, then adopt by exact name."""
    fake = FakeDigitalOcean()
    fake.create_then_fail = 1
    fake.add(f"pic-{SID}-h03-lon1-other", OURS)  # same tag, another name: not adopted
    name = f"pic-{SID}-h03-lon1-shifter"
    async with running(fake) as cloud:
        with pytest.raises(do_api.CreateError) as stopped:
            await cloud.create([name], region="lon1", size="c-2", image="1", tags=OURS,
                               ssh_keys=["9"])
        assert stopped.value.uncertain and stopped.value.pending == [name]
        assert not stopped.value.created and not cloud.created_ids
        [adopted] = await cloud.adopt_uncertain([name], OURS[2])
    posts = [r for r in fake.requests if r[:2] == ("POST", "/v2/droplets")]
    assert len(posts) == 1 and len(fake.droplets) == 2
    assert adopted.name == name and cloud.created_ids == {adopted.id}


async def test_pagination_without_a_total_is_an_error():
    """m5: a page with no meta.total never silently ends a listing."""
    fake = FakeDigitalOcean()
    fake.add(f"pic-{SID}-h03-lon1-x", OURS)
    original = fake._list

    async def no_meta(request):
        response = await original(request)
        body = json.loads(response.body)
        body.pop("meta")
        return web.json_response(body)

    fake._list = no_meta
    async with running(fake) as cloud:
        with pytest.raises(ApiError):
            await cloud.list_tag(OURS[1])


async def test_errors_after_retries_or_on_4xx_carry_no_token(caplog):
    caplog.set_level(logging.DEBUG)
    fake, slept = FakeDigitalOcean(), []
    fake.faults += [Fault("GET", "/v2/account", 500, times=5),
                    Fault("POST", "/v2/droplets", 422)]

    async def sleep(seconds):
        slept.append(seconds)

    async with running(fake, sleep=sleep) as cloud:
        with pytest.raises(ApiError) as exhausted:
            await cloud.account()
        with pytest.raises(ApiError) as rejected:
            await cloud.create([f"pic-{SID}-h03-lon1-x"], region="lon1", size="c-2",
                               image="1", tags=OURS, ssh_keys=["9"])
        text = " ".join([repr(cloud), str(exhausted.value), str(rejected.value),
                         repr(exhausted.value.__context__), caplog.text])
    assert slept == [1, 2, 4, 8] and rejected.value.status == 422
    assert TOKEN not in text and "Bearer" not in text
    assert exhausted.value.__cause__ is None and rejected.value.__cause__ is None


async def test_connection_errors_are_retried_then_raised():
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    cloud = do_api.DigitalOcean(lambda: TOKEN, base_url="http://127.0.0.1:9", sleep=sleep)
    async with cloud:
        with pytest.raises(ApiError):
            await cloud.account()
    assert slept == [1, 2, 4, 8]


async def test_the_limiter_allows_at_most_the_configured_requests_per_minute():
    now, slept = [0.0], []

    async def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    async with running(FakeDigitalOcean(), sleep=sleep, clock=lambda: now[0],
                       per_minute=2) as cloud:
        for _ in range(3):
            await cloud.account()
            now[0] += 1
        now[0] += 120
        await cloud.account()
    assert slept == [58.0]


def test_token_sources_read_memory_only_and_refuse_loose_files(tmp_path, monkeypatch):
    token = tmp_path / "do-token"
    token.write_text(TOKEN + "\n")
    token.chmod(0o600)
    assert do_api.file_token(token)() == TOKEN
    link = tmp_path / "link"
    link.symlink_to(token)
    empty = tmp_path / "empty"
    empty.write_text("")
    empty.chmod(0o600)
    token.chmod(0o644)
    for path in (token, link, empty):
        with pytest.raises(TokenError):
            do_api.file_token(path)()
    calls = []

    def run(command, **_):
        calls.append(command)
        found = type("Done", (), {"returncode": 0 if len(calls) == 1 else 44,
                                  "stdout": TOKEN + "\n" if len(calls) == 1 else ""})
        return found

    monkeypatch.setattr(do_api.subprocess, "run", run)
    read = do_api.keychain_token(ACCOUNTS["leftclick"])
    assert read() == TOKEN
    with pytest.raises(TokenError) as missing:
        read()
    assert calls[0] == ["security", "find-generic-password", "-s", "Leftclick DigitalOcean",
                        "-a", "team: Leftclick", "-w"]
    assert TOKEN not in str(missing.value)


def test_campaign_command_group_is_registered(monkeypatch):
    monkeypatch.setitem(campaign_cli.HANDLERS, "probe", lambda args: 7)
    assert campaign_cli.dispatch(argparse.Namespace(campaign_action="probe")) == 7
    with pytest.raises(SystemExit):
        main_cli.main(["campaign"])


async def test_limits_reports_counts_only():
    fake = FakeDigitalOcean()
    fake.seed_production(30)
    fake.add(f"pic-{SID}-h03-lon1-shifter", OURS, project=CAMPAIGN_PROJECT)
    async with running(fake) as cloud:
        report = await campaign_cli.limits(cloud, "Proxy Index", ["c-2", "s-1vcpu-1gb", "x"])
    assert report["droplet_limit"] == 10000 and report["account_status"] == "active"
    assert report["our_tag_counts"] == {WORKER_TAG: 1, MANAGER_TAG: 0, BUILDER_TAG: 0}
    assert report["project"] == {"name": "Proxy Index", "droplets": 1}
    assert report["sizes"] == {"c-2": ["lon1", "fra1"], "s-1vcpu-1gb": [], "x": []}
    text = json.dumps(report)
    assert not IP.search(text) and TOKEN not in text and "owner-at-example" not in text
    listed = [q for m, p, q, _ in fake.requests if p == "/v2/droplets"]
    assert listed and all(q["tag_name"].startswith("pi-camp-") for q in listed)


def test_campaign_limits_command_is_wired(monkeypatch, capsys):
    seen = {}

    async def limits(cloud, project, sizes):
        seen.update(cloud=cloud, project=project, sizes=sizes)
        return {"droplet_limit": 1}

    monkeypatch.setattr(campaign_cli, "limits", limits)
    monkeypatch.setattr(do_api, "keychain_token", lambda account: lambda: TOKEN)
    assert main_cli.main(["campaign", "limits", "--account", "leftclick"]) == 0
    assert json.loads(capsys.readouterr().out) == {"droplet_limit": 1}
    assert seen["project"] == "Proxy Index" and seen["sizes"] == list(campaign_cli.DEFAULT_SIZES)
    assert isinstance(seen["cloud"], do_api.DigitalOcean)


RULES = {"name": "pi-camp-worker-fw", "inbound_rules": [], "outbound_rules": [],
         "tags": [WORKER_TAG], "droplet_ids": []}


@pytest.mark.parametrize(("method", "path", "body"), [
    ("POST", "/v2/account/keys", {"name": "owner-laptop", "public_key": "ssh-ed25519 AAAA"}),
    ("POST", "/v2/account/keys", {"name": "pi-camp-mac-1", "public_key": "k", "extra": 1}),
    ("POST", "/v2/droplets/700001/actions", {"type": "rebuild", "image": "x"}),
    ("POST", "/v2/droplets/700001/actions", {"type": "restore"}),
    ("POST", "/v2/droplets/700001/actions", {"type": "snapshot", "name": "prod-backup"}),
    ("POST", "/v2/droplets/700001/actions", {"type": "power_off", "force": True}),
    ("POST", "/v2/droplets/700001/actions", None),
    ("POST", "/v2/images/700001/actions", {"type": "transfer", "region": "mars1"}),
    ("POST", "/v2/images/700001/actions", {"type": "convert", "region": "lon1"}),
    ("POST", "/v2/firewalls", {**RULES, "name": "prod-fw"}),
    ("POST", "/v2/firewalls", {**RULES, "tags": ["prod"]}),
    ("POST", "/v2/firewalls", {**RULES, "tags": ["pi-camp-other"]}),
    ("POST", "/v2/firewalls", {**RULES, "droplet_ids": ["all"]}),
    ("POST", "/v2/firewalls", {**RULES, "load_balancer_uids": ["x"]}),
    ("PUT", f"/v2/firewalls/{FIREWALL}", {**RULES, "name": "default"}),
])
async def test_operation_bodies_outside_our_scope_are_refused_before_the_network(
    method, path, body
):
    fake = FakeDigitalOcean()
    async with running(fake) as cloud:
        with pytest.raises(ScopeError):
            await cloud.request(method, path, body=body)
    assert not fake.requests


async def test_ssh_keys_lists_only_ours_and_adds_one():
    fake = FakeDigitalOcean()
    fake.add_resource("ssh_key", 1, name="owner-laptop", public_key="ssh-ed25519 AAAA owner")
    fake.add_resource("ssh_key", 2, name="pi-camp-mac-20270101", public_key="ssh-ed25519 BBBB")
    async with running(fake) as cloud:
        assert [k["id"] for k in await cloud.ssh_keys()] == [2]
        added = await cloud.create_ssh_key("pi-camp-worker-20270101", "ssh-ed25519 CCCC")
    assert added["name"] == "pi-camp-worker-20270101" and str(added["id"]) in fake.resources[
        "ssh_key"]


async def test_droplet_actions_run_only_on_a_builder_this_client_created():
    fake = FakeDigitalOcean()
    production = fake.add("prod-node-0001", ["prod"])
    foreign = fake.add("pi-camp-builder-20270101-ee99", [BUILDER_TAG])
    async with running(fake) as cloud:
        [worker] = await cloud.create([f"pic-{SID}-h03-lon1-shifter"], region="lon1",
                                      size="c-2", image="1", tags=OURS, ssh_keys=["9"])
        [builder, renamed, gone] = [(await cloud.create(
            [f"pi-camp-builder-20270101-ab1{i}"], region="lon1", size="s-1vcpu-1gb",
            image="ubuntu-24-04-x64", tags=[BUILDER_TAG], ssh_keys=["9"]))[0] for i in range(3)]
        fake.droplets[renamed.id]["name"] = "prod-node-0002"
        fake.droplets.pop(gone.id)
        for target in (production, foreign, worker.id, renamed.id, gone.id, builder.id):
            with pytest.raises(ScopeError):  # the builder is not in Proxy Index yet
                await cloud.droplet_action(target, "power_off", CAMPAIGN_PROJECT)
        for droplet_id in (production, foreign, worker.id, renamed.id, builder.id):
            fake.move(droplet_id)
        for target in (production, foreign, worker.id, renamed.id):
            with pytest.raises(ScopeError):
                await cloud.droplet_action(target, "power_off", CAMPAIGN_PROJECT)
        fake.move(builder.id, PROD_PROJECT)
        fake.project_names[PROD_PROJECT] = "Proxy Index (copy)"
        with pytest.raises(ScopeError):  # a project that is not the one named Proxy Index
            await cloud.droplet_action(builder.id, "power_off", PROD_PROJECT)
        fake.move(builder.id)
        assert not [b for b in fake.bodies if "/actions" in b[1]]
        off = await cloud.droplet_action(builder.id, "shutdown", CAMPAIGN_PROJECT)
        shot = await cloud.droplet_action(builder.id, "snapshot", CAMPAIGN_PROJECT, SNAPSHOT)
        assert (await cloud.action(off["id"]))["status"] == "completed"
        [image] = await cloud.droplet_snapshots(builder.id)
        assert image["name"] == SNAPSHOT and shot["type"] == "snapshot"
        assert await cloud.image(image["id"]) == image and await cloud.image("1") is None
    assert fake.droplets[builder.id]["status"] == "off"
    assert fake.droplets[production]["status"] == "active"


async def test_only_campaign_worker_snapshots_are_transferred():
    fake = FakeDigitalOcean()
    fake.images["11"] = {"id": 11, "name": "prod-golden", "regions": ["lon1"], "droplet": ""}
    fake.images["12"] = {"id": 12, "name": SNAPSHOT, "regions": ["lon1"], "droplet": ""}
    async with running(fake) as cloud:
        for image_id in ("11", "13"):
            with pytest.raises(ScopeError):
                await cloud.transfer_image(image_id, "fra1")
        action = await cloud.transfer_image("12", "fra1")
    assert action["type"] == "transfer" and fake.images["12"]["regions"] == ["lon1", "fra1"]
    assert fake.images["11"]["regions"] == ["lon1"]


async def test_firewalls_attach_only_to_campaign_droplets():
    fake = FakeDigitalOcean()
    production = fake.add("prod-node-0001", ["prod"])
    manager = fake.add("pi-camp-manager-20270101-ab12", [MANAGER_TAG])
    fake.add_resource("firewall", FIREWALL, name="prod-fw", tags=["prod"], droplet_ids=[])
    async with running(fake) as cloud:
        fake.move(production)
        for target in (production, "999", manager):  # the manager is not in Proxy Index yet
            with pytest.raises(ScopeError):
                await cloud.create_firewall({**RULES, "tags": [], "droplet_ids": [int(target)]},
                                            CAMPAIGN_PROJECT)
        for firewall_id in (FIREWALL, "33333333-3333-4333-8333-999999999999"):
            with pytest.raises(ScopeError):
                await cloud.update_firewall(firewall_id, RULES, CAMPAIGN_PROJECT)
        with pytest.raises(ScopeError):
            await cloud.create_firewall({**RULES, "name": "default"}, CAMPAIGN_PROJECT)
        fake.move(manager)
        with pytest.raises(ScopeError):  # not the project named Proxy Index
            await cloud.create_firewall({**RULES, "droplet_ids": [int(manager)]}, PROD_PROJECT)
        created = await cloud.create_firewall({**RULES, "droplet_ids": [int(manager)]},
                                              CAMPAIGN_PROJECT)
        updated = await cloud.update_firewall(created["id"], {**RULES, "droplet_ids": []},
                                              CAMPAIGN_PROJECT)
    assert [m for m, _, _ in fake.bodies] == ["POST", "PUT"]
    assert updated["droplet_ids"] == [] and fake.resources["firewall"][FIREWALL]["name"] == (
        "prod-fw")
