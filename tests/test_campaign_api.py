"""Campaign contracts and the DigitalOcean client against the in-process fake (no live calls)."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import replace
from pathlib import Path

import pytest
from campaign_fakes import CAMPAIGN_PROJECT, CID, SID, TOKEN, FakeDigitalOcean, Fault, running

from proxy_index import cli as main_cli
from proxy_index.campaign import cli as campaign_cli
from proxy_index.campaign import contracts, do_api
from proxy_index.campaign.contracts import (
    ACCOUNTS,
    MANAGER_TAG,
    WORKER_TAG,
    CampaignSpec,
    DropletRecord,
    HourRecord,
)
from proxy_index.campaign.do_api import ApiError, ScopeError, TokenError

OURS = [WORKER_TAG, f"pi-camp-{SID}", f"pi-camp-{SID}-h03"]
IP = re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")


def spec(**changes) -> CampaignSpec:
    base = CampaignSpec(
        campaign_id=CID, created_at="2027-01-01T21:00:00+00:00", account="leftclick",
        project_id=CAMPAIGN_PROJECT, starts_at="2027-01-01T22:00:00+00:00", grace_hours=48,
        regions=("lon1", "nyc3"), networks=("shifter", "geonode"),
        manifests={"shifter": "a.json", "geonode": "b.json"},
        size_by_region={"lon1": "c-2", "nyc3": "c-2"},
        image_by_region={"lon1": "11", "nyc3": "12"}, image_name="pi-camp-worker-0.9.5",
        wheel_sha256="a" * 64, framework_version="0.9.5", ssh_key_id="9",
        manager_droplet_id="500001",
    )
    return replace(base, **changes)


def test_names_tags_and_ids_follow_the_frozen_patterns():
    assert contracts.short_id(CID) == SID
    assert contracts.campaign_tag(CID) == f"pi-camp-{SID}"
    assert contracts.hour_tag(CID, 3) == f"pi-camp-{SID}-h03"
    name = contracts.droplet_name(CID, 3, "lon1", "proxy-cheap")
    assert name == f"pic-{SID}-h03-lon1-proxy-cheap"
    assert re.fullmatch(contracts.WORKER_NAME_RE, name)
    for bad in ("camp-1", "20270101T220000Z-0123456789ab", "20270101t220000z-xyz"):
        with pytest.raises(ValueError):
            contracts.short_id(bad)
    with pytest.raises(ValueError):
        contracts.droplet_name(CID, 3, "LON1", "shifter")
    assert set(ACCOUNTS) == {"leftclick", "proxy-index"}
    assert {a.project_name for a in ACCOUNTS.values()} == {"Proxy Index"}


def test_spec_round_trips_and_validates():
    good = spec().validate()
    assert CampaignSpec.from_json(good.to_json()) == good
    for changes in (
        {"account": "other"}, {"starts_at": "2027-01-01T22:30:00+00:00"},
        {"starts_at": "2027-01-01T22:00:00"}, {"created_at": "2027-01-01T21:00:00"},
        {"regions": ("nyc3", "lon1")}, {"regions": ("lon1", "xyz1")},
        {"networks": ("shifter", "shifter")}, {"networks": ("Shifter",)},
        {"manifests": {}}, {"size_by_region": {}}, {"image_by_region": {}},
        {"wheel_sha256": "x"}, {"grace_hours": 0}, {"hours": 23}, {"project_id": ""},
        {"create_lead_seconds": 0}, {"schema_version": 2},
    ):
        with pytest.raises(ValueError):
            spec(**changes).validate()
    with pytest.raises(ValueError):
        CampaignSpec.from_json(json.dumps({"campaign_id": CID, "surprise": 1}))


def test_records_and_paths():
    hour = HourRecord(CID, 3, "2027-01-02T01:00:00+00:00",
                      droplets=[DropletRecord("shifter", "lon1", 3, "pic-x", droplet_id="7")])
    assert HourRecord.from_dict(json.loads(json.dumps(hour.to_dict()))) == hour
    root = Path("/c")
    assert contracts.hour_path(root, 3) == root / "ledger" / "03" / "hour.json"
    assert contracts.audit_path(root) == root / "ledger" / "delete-audit.jsonl"
    assert contracts.network_root(root, "n", "m") == root / "networks" / "n" / "residential" / "m"
    assert [p(root).name for p in (contracts.spec_path, contracts.runtime_dir,
                                   contracts.reports_dir, contracts.status_path,
                                   contracts.alerts_path)] == [
        "campaign-spec.json", ".runtime", "reports", "status.json", "alerts.jsonl"]
    assert contracts.logs_dir(root, 4) == root / "logs" / "04"


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
        await cloud.assign_project(CAMPAIGN_PROJECT, [manager.id])
        assert await cloud.project_droplet_ids(CAMPAIGN_PROJECT) == {manager.id}
        assert await cloud.project_id("Proxy Index") == CAMPAIGN_PROJECT
        for bad in ([], ["do:droplet:1"]):
            with pytest.raises(ScopeError):
                await cloud.assign_project(CAMPAIGN_PROJECT, bad)
        with pytest.raises(ScopeError):
            await cloud.project_id("Missing")
        assert await cloud.get("424242") is None
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


async def test_limits_reports_counts_only(capsys):
    fake = FakeDigitalOcean()
    fake.seed_production(30)
    fake.add(f"pic-{SID}-h03-lon1-shifter", OURS, project=CAMPAIGN_PROJECT)
    async with running(fake) as cloud:
        report = await campaign_cli.limits(cloud, "Proxy Index", ["c-2", "s-1vcpu-1gb", "x"])
    assert report["droplet_limit"] == 10000 and report["account_status"] == "active"
    assert report["our_tag_counts"] == {WORKER_TAG: 1, MANAGER_TAG: 0, "pi-camp-builder": 0}
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
    monkeypatch.setattr(campaign_cli, "keychain_token", lambda account: lambda: TOKEN)
    assert main_cli.main(["campaign", "limits", "--account", "leftclick"]) == 0
    assert json.loads(capsys.readouterr().out) == {"droplet_limit": 1}
    assert seen["project"] == "Proxy Index" and seen["sizes"] == list(campaign_cli.DEFAULT_SIZES)
    assert isinstance(seen["cloud"], do_api.DigitalOcean)
