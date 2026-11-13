"""CAMP-1g: `campaign plan`, its inputs, census configs, slots and CLI (fakes only)."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tomllib
from pathlib import Path

import pytest
from campaign_deploy_fakes import CID, IMAGE, ROOT, other_team, parse
from campaign_fakes import CAMPAIGN_PROJECT
from campaign_plan_cases import run_plan, with_geonode

from atlas.campaign import cli as campaign_cli
from atlas.campaign import deploy, planspec
from atlas.campaign.contracts import CampaignSpec, spec_path
from atlas.campaign.planspec import PlanError
from atlas.models import RequestContext
from atlas.onboarding import manifest
from atlas.onboarding.files import digest
from atlas.providers import load_provider
from atlas.regions import REGION_IDS

pytest_plugins = ["campaign_plan_cases"]


def test_plan_writes_v3_manifests_and_a_valid_spec(plan_world):
    fake, inputs = plan_world
    result = asyncio.run(run_plan(fake, inputs))
    root = Path(result["root"])
    spec = CampaignSpec.from_json(spec_path(root).read_text())
    assert (spec.project_id, spec.ssh_key_id, spec.manager_droplet_id) == (
        CAMPAIGN_PROJECT,
        "77",
        "500001",
    )
    assert spec.regions == tuple(REGION_IDS) and set(spec.image_by_region.values()) == {"9001"}
    assert set(spec.size_by_region.values()) == {"c-2"} and spec.concurrency == 2000
    assert spec.starts_at == "2027-01-01T23:00:00+00:00" and not spec.synthetic
    assert re.fullmatch(r"20270101t230000z-[0-9a-f]{12}", spec.campaign_id)
    path = root / spec.manifests["shifter"]
    loaded = manifest.load(path)
    assert loaded["schema_version"] == 3 and loaded["settings"]["concurrency"] == 2000
    assert spec.manifests["shifter"].startswith("networks/shifter/residential/")
    assert (path.parent / "provider.toml").read_bytes() == (
        ROOT / "configs/shifter-onboarding.toml"
    ).read_bytes()
    assert "acct_9" not in "".join(p.read_text() for p in root.rglob("*") if p.is_file())
    assert not [m for m, *_ in fake.requests if m != "GET"]


def test_plan_binds_the_non_strict_census_config(plan_world):
    """CTRY-1 (owner decision 2026-10-25, reversing CAMP-1i): Shifter and Geonode census
    configs turn strict targeting off and still send the country; an other-country exit or a
    no-IP answer counts like a duplicate in the runner's answer classes."""
    fake, inputs = plan_world
    inputs = with_geonode(inputs)
    root = Path(asyncio.run(run_plan(fake, inputs))["root"])
    spec = CampaignSpec.from_json(spec_path(root).read_text())
    names = {}
    for network, country in (("shifter", "as"), ("geonode", "us")):
        bound = (root / spec.manifests[network]).parent / "provider.toml"
        assert manifest.load(bound.parent / "campaign.json")["provider_config_sha256"] == (
            digest(bound)
        )
        assert tomllib.loads(bound.read_text())["parameters"] == {"strict": False}
        proxy = load_provider(
            bound, parameters={"country": country}, credentials_file=inputs.credentials
        )
        names[network] = proxy.resolve(RequestContext("run", 0, 0)).username
    assert names["shifter"] == "customer-acct_9-country-as"
    assert names["geonode"].endswith("-country-us-strict-off")


def test_plan_refuses_a_census_config_with_strict_targeting(plan_world, tmp_path):
    from dataclasses import replace

    fake, inputs = plan_world
    configs = tmp_path / "configs"
    configs.mkdir()
    shutil.copy(ROOT / "configs/shifter.toml", configs / "shifter.toml")
    original = (ROOT / "configs/shifter-onboarding.toml").read_text()
    for line in ("strict = true", "# strict = false"):
        (configs / "shifter-onboarding.toml").write_text(
            original.replace("strict = false", line, 1)
        )
        with pytest.raises(PlanError, match="network shifter: .*must set strict = false"):
            asyncio.run(run_plan(fake, replace(inputs, configs=configs)))
    assert not inputs.output.exists()


def test_plan_includes_geonode_at_500(plan_world):
    fake, inputs = plan_world
    result = asyncio.run(run_plan(fake, with_geonode(inputs)))
    assert result["networks"] == ["shifter", "geonode"]
    assert result["concurrency_by_network"] == {"shifter": 2000, "geonode": 500}


def test_the_census_config_of_a_network_without_a_strict_flag_is_its_run_config():
    configs = ROOT / "configs"
    assert planspec.census_config(configs, "shifter") == configs / "shifter-onboarding.toml"
    assert planspec.census_config(configs, "soax") == configs / "soax.toml"


async def test_plan_now_hour_and_options(plan_world):
    fake, inputs = plan_world
    from dataclasses import replace

    result = await run_plan(
        fake,
        replace(
            inputs, start="now-hour", regions=("syd1", "lon1"), concurrency=2500, synthetic=True
        ),
    )
    spec = CampaignSpec.from_json(spec_path(Path(result["root"])).read_text())
    assert spec.starts_at == "2027-01-01T21:00:00+00:00" and spec.regions == ("lon1", "syd1")
    assert spec.concurrency == 2500 and spec.synthetic


async def test_a_campaign_plan_refuses_an_image_without_the_country_database(plan_world):
    """CTRY-2: only a synthetic plan may use an image built without `--mmdb`."""
    from dataclasses import replace

    fake, inputs = plan_world
    inputs.images.write_text(
        json.dumps({"builds": [{"snapshot_id": "9001", "snapshot_name": IMAGE}]})
    )
    with pytest.raises(PlanError, match="no country database"):
        await run_plan(fake, inputs)
    assert not inputs.output.exists()
    result = await run_plan(fake, replace(inputs, synthetic=True))
    assert result["synthetic"] is True


async def test_plan_records_the_worker_size_it_checked(plan_world):
    """CAMP-1j: `--size` (default c-2) must be offered in every region and is the spec's size."""
    from dataclasses import replace

    fake, inputs = plan_world
    fake.sizes.append({"slug": "c-4", "available": True, "regions": list(REGION_IDS)})
    fake.sizes.append({"slug": "s-4vcpu-8gb", "available": True, "regions": ["lon1"]})
    result = await run_plan(fake, replace(inputs, size="c-4", concurrency=2500))
    spec = CampaignSpec.from_json(spec_path(Path(result["root"])).read_text())
    assert spec.size_by_region == dict.fromkeys(REGION_IDS, "c-4") and spec.concurrency == 2500
    with pytest.raises(PlanError, match="size s-4vcpu-8gb in fra1"):
        await run_plan(fake, replace(inputs, size="s-4vcpu-8gb"))
    with pytest.raises(PlanError, match="size c-32 in lon1"):
        await run_plan(fake, replace(inputs, size="c-32"))
    for bad in ("g-2vcpu-8gb", "gpu-h100x1-80gb", "m-2vcpu-16gb", "c-2;x", ""):
        with pytest.raises(PlanError, match="--size"):
            await run_plan(fake, replace(inputs, size=bad))
    assert len(list(inputs.output.iterdir())) == 1


@pytest.mark.parametrize(
    "change, message",
    [
        ("table", "network shifter"),
        ("size", "size c-2 in syd1"),
        ("image", "image in syd1"),
        ("key", "ssh key"),
        ("recorded image", "not one recorded build"),
        ("manager", "deploy the manager"),
        ("region", "--regions"),
        ("offset", "UTC offset"),
        ("config", "network shifter"),
        ("concurrency", "concurrency"),
        ("past", "future"),
        ("manager credentials", "no credentials for shifter"),
    ],
)
async def test_plan_rejects_a_missing_input(plan_world, change, message):
    from dataclasses import replace

    fake, inputs = plan_world
    if change == "table":
        inputs.credentials.write_text(deploy.toml_table("geonode", {"a": "b"}).decode())
    elif change == "size":
        fake.sizes[0]["regions"].remove("syd1")
    elif change == "image":
        fake.images["9001"]["regions"].remove("syd1")
    elif change == "key":
        fake.resources["ssh_key"].clear()
    elif change == "recorded image":
        inputs = replace(inputs, image="pi-camp-worker-other")
    elif change == "manager":
        inputs.inventory.write_text("{}")
    elif change == "region":
        inputs = replace(inputs, regions=("mars1",))
    elif change == "offset":
        inputs = replace(inputs, start="2027-01-01T23:00:00")
    elif change == "config":
        inputs = replace(inputs, configs=inputs.output)
    elif change == "manager credentials":
        recorded = json.loads(inputs.inventory.read_text())
        inputs.inventory.write_text(json.dumps(recorded | {"credentials": planspec.ALL16}))
    elif change == "concurrency":
        inputs = replace(inputs, concurrency=0)
    else:
        inputs = replace(inputs, start="2027-01-01T20:00:00+00:00")
    with pytest.raises((PlanError, ValueError), match=message):
        await run_plan(fake, inputs)
    assert not inputs.output.exists() or not any(inputs.output.iterdir())


async def test_plan_refuses_another_account_than_the_managers(plan_world):
    """B1: the spec's account and project must be the deployed manager's."""
    from dataclasses import replace

    fake, inputs = plan_world
    with pytest.raises(PlanError, match="account"):
        await run_plan(fake, replace(inputs, account="proxy-index"))
    other_team(fake)
    with pytest.raises(PlanError, match="account"):
        await run_plan(fake, inputs)
    assert not inputs.output.exists()


def test_parse_networks():
    assert planspec.parse_networks("all16") == planspec.ALL16 and len(planspec.ALL16) == 16
    assert planspec.parse_networks("shifter,soax") == ("shifter", "soax")
    assert planspec.parse_networks("all16,shifter") == (*planspec.ALL16, "shifter")
    for bad in ("", "a,a", "Bad", "all16,soax"):
        with pytest.raises(PlanError):
            planspec.parse_networks(bad)


def test_the_plan_cli_reports_bad_input(capsys):
    args = parse(
        [
            "campaign",
            "plan",
            "--account",
            "leftclick",
            "--start",
            "now-hour",
            "--networks",
            "a,a",
            "--image",
            IMAGE,
            "--output",
            "out",
        ]
    )
    assert campaign_cli.dispatch(args) == 3
    assert "Not planned" in capsys.readouterr().err


def test_plan_refuses_a_config_for_another_provider_or_an_existing_folder(
    plan_world, tmp_path, monkeypatch
):
    from dataclasses import replace

    fake, inputs = plan_world
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "shifter.toml").write_text('schema_version = 1\nprovider = "geonode"\n')
    with pytest.raises(PlanError, match="another provider"):
        asyncio.run(run_plan(fake, replace(inputs, configs=configs)))
    monkeypatch.setattr(planspec.secrets, "token_hex", lambda n: "0" * 2 * n)
    (inputs.output / "20270101t230000z-000000000000").mkdir(parents=True)
    with pytest.raises(PlanError, match="already exists"):
        asyncio.run(run_plan(fake, inputs))


def test_the_plan_cli_prints_the_summary(monkeypatch, capsys):
    class Cloud:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    async def fake_plan(cloud, inputs):
        return {
            "campaign_id": CID,
            "networks": list(inputs.networks),
            "regions": list(inputs.regions),
        }

    monkeypatch.setattr(campaign_cli, "_cloud", lambda account: Cloud())
    monkeypatch.setattr(planspec, "plan", fake_plan)
    args_list = [
        "campaign",
        "plan",
        "--account",
        "leftclick",
        "--start",
        "now-hour",
        "--networks",
        "shifter",
        "--regions",
        "lon1",
        "--image",
        IMAGE,
        "--output",
        "out",
    ]
    args = parse(args_list)
    assert campaign_cli.dispatch(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "campaign_id": CID,
        "networks": ["shifter"],
        "regions": ["lon1"],
    }
    seen = []
    monkeypatch.setattr(
        planspec, "plan", lambda cloud, inputs: seen.append(inputs.size) or fake_plan(cloud, inputs)
    )
    assert campaign_cli.dispatch(args) == 0
    assert campaign_cli.dispatch(parse([*args_list, "--size", "c-4"])) == 0
    assert seen == ["c-2", "c-4"]


async def test_plan_with_quarter_slots_spaces_the_spec_and_the_manifests(plan_world):
    from dataclasses import replace

    fake, inputs = plan_world
    result = await run_plan(fake, replace(inputs, slot_minutes=15, slots=4))
    root = Path(result["root"])
    spec = CampaignSpec.from_json(spec_path(root).read_text())
    assert (spec.slot_minutes, spec.hours, spec.replace_until_minutes) == (15, 4, 15)
    assert (result["slot_minutes"], result["slots"], spec.create_lead_seconds) == (15, 4, 90)
    rounds = manifest.load(root / spec.manifests["shifter"])["rounds"]
    assert rounds[:4] == [
        "2027-01-01T23:00:00.000Z",
        "2027-01-01T23:15:00.000Z",
        "2027-01-01T23:30:00.000Z",
        "2027-01-01T23:45:00.000Z",
    ]
    hourly = CampaignSpec.from_json(
        spec_path(Path((await run_plan(fake, inputs))["root"])).read_text()
    )
    assert (hourly.slot_minutes, hourly.hours, hourly.replace_until_minutes) == (60, 24, 15)
    ten = await run_plan(fake, replace(inputs, slot_minutes=10, slots=6))
    assert (
        CampaignSpec.from_json(spec_path(Path(ten["root"])).read_text()).replace_until_minutes == 10
    )
    for bad in ({"slot_minutes": 45}, {"slot_minutes": 7}, {"slots": 0}, {"slots": 25}):
        with pytest.raises(PlanError, match="--slot-minutes"):
            await run_plan(fake, replace(inputs, **bad))
    args = parse(
        [
            "campaign",
            "plan",
            "--account",
            "leftclick",
            "--start",
            "now-hour",
            "--networks",
            "shifter",
            "--image",
            IMAGE,
            "--output",
            "o",
            "--slot-minutes",
            "15",
            "--slots",
            "4",
        ]
    )
    assert (args.slot_minutes, args.slots) == (15, 4)


async def test_plan_refuses_manifests_with_another_slot_interval(plan_world, monkeypatch):
    """CAMP-1n m3: the spec and its manifests must agree on the slot interval."""
    from dataclasses import replace

    fake, inputs = plan_world
    monkeypatch.setattr(planspec.manifest, "slot_minutes", lambda data: 60)
    with pytest.raises(PlanError, match="slot_minutes differs"):
        await run_plan(fake, replace(inputs, slot_minutes=15, slots=4))
    assert not inputs.output.exists() or not list(inputs.output.iterdir())


async def test_a_plan_for_a_staging_manager_records_its_environment(plan_world):
    """CAMP-STAGING-1: the spec takes the manager's environment; production's has no field."""
    fake, inputs = plan_world
    root = Path((await run_plan(fake, inputs))["root"])
    assert "environment" not in json.loads(spec_path(root).read_text())
    data = json.loads(inputs.inventory.read_text())
    inputs.inventory.write_text(json.dumps(data | {"environment": "staging"}))
    root = Path((await run_plan(fake, inputs))["root"])
    assert CampaignSpec.from_json(spec_path(root).read_text()).environment == "staging"
