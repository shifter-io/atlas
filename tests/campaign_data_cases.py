"""Shared campaign data for the merge, completeness and collect tests: a built campaign of
2 networks × 2 regions × 3 collected rounds and its ledger helpers. RFC 5737 addresses only.

The test modules load it with `pytest_plugins` after importing its names, so pytest cannot
rewrite it; it holds no asserts (PYTEST_DONT_REWRITE).
"""

import asyncio
import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from test_onboarding import ROOT, START, Clock
from test_onboarding_round import census, measurement

from proxy_index.campaign.contracts import (
    CampaignSpec,
    DropletRecord,
    HourRecord,
    droplet_name,
    hour_path,
    spec_path,
)
from proxy_index.models import ProxyConfig
from proxy_index.onboarding import manifest as campaign
from proxy_index.onboarding.files import atomic_json
from proxy_index.onboarding.rounds import execute_round

CID = "20270101t220000z-0123456789ab"
NETWORKS = ("geonode", "shifter")
REGIONS = ("ams3", "tor1")
FAILED = ("geonode", "ams3", 2)  # a worker that never booted, and its replacement
STOPPED = ("shifter", "tor1", 1)  # an operator stop during GL


class Proxy:
    def __init__(self, provider, country):
        self.provider, self.country = provider, country.lower()

    def public(self):
        return {
            "provider": self.provider,
            "product": "residential",
            "requested_country": self.country,
        }

    def resolve(self, context):
        return ProxyConfig("http", "localhost", 8080)


def _spec(manifests: dict) -> CampaignSpec:
    return CampaignSpec(
        campaign_id=CID,
        created_at="2027-01-01T21:00:00+00:00",
        account="leftclick",
        project_id="project-1",
        starts_at=campaign.timestamp(START),
        grace_hours=48,
        regions=REGIONS,
        networks=NETWORKS,
        manifests=manifests,
        size_by_region=dict.fromkeys(REGIONS, "c-2"),
        image_by_region=dict.fromkeys(REGIONS, "image-1"),
        image_name="pi-camp-worker-test",
        wheel_sha256="0" * 64,
        framework_version="test",
        ssh_key_id="key-1",
        manager_droplet_id="manager-1",
    )


async def _round(path: Path, network: str, region: str, hour: int) -> None:
    scheduled = campaign.moment(campaign.load(path)["rounds"][hour]).timestamp()
    clock, stop, special = Clock(scheduled), asyncio.Event(), {}
    if (network, region, hour) == STOPPED:

        async def stopped(country_stop):
            stop.set()
            await country_stop.wait()
            return measurement(
                0, status="stopped", stop_reason="stopped", completed=0, total_errors=0
            )

        special["GL"] = stopped
    rows = 3 if network == "geonode" else 2
    await execute_round(
        path,
        region,
        hour,
        lambda c: Proxy(network, c),
        clock=clock,
        stop=stop,
        run_country=census(clock, [], rows=rows, special=special),
    )


def _droplet(network, region, hour, status, **extra) -> DropletRecord:
    scheduled = START + timedelta(hours=hour)
    values = {
        "droplet_id": f"{hour}{len(network)}{len(region)}",
        "size": "c-2",
        "status": status,
        "requested_at": campaign.timestamp(scheduled - timedelta(seconds=90)),
        "ssh_ready_at": campaign.timestamp(scheduled - timedelta(seconds=40)),
        "start_lateness_seconds": 0.25,
    } | extra
    if status.endswith("_failed"):  # a failed worker was never ready and never started
        values |= {"ssh_ready_at": None, "start_lateness_seconds": None}
    else:  # a worker that got this far armed and started its round
        values = {
            "armed_at": values["ssh_ready_at"],
            "started_at": campaign.timestamp(scheduled),
        } | values
    return DropletRecord(network, region, hour, droplet_name(CID, hour, region, network), **values)


def write_hour(root: Path, hour: int, statuses: dict, paused: tuple[str, ...] = ()) -> None:
    """One ledger record; `statuses` maps (network, region) to a status or a list of them.

    A status may be a `(status, {field: value})` pair to override record fields; `paused`
    names the networks paused when the hour launched.
    """
    droplets = []
    for (network, region), status in statuses.items():
        for item in status if isinstance(status, list) else [status]:
            item, extra = item if isinstance(item, tuple) else (item, {})
            overlap = 1 if (network, region, hour) == STOPPED else 0
            droplets.append(_droplet(network, region, hour, item, overlap=overlap, **extra))
    record = HourRecord(
        CID,
        hour,
        campaign.timestamp(START + timedelta(hours=hour)),
        state="done",
        droplets=droplets,
        paused_networks=list(paused),
    )
    atomic_json(hour_path(root, hour), record.to_dict())


def _cells(status):
    return {(n, r): status for n in NETWORKS for r in REGIONS}


# Session scope: both test modules share one build per worker, as before the split.
@pytest.fixture(scope="session")
def built(tmp_path_factory):
    """2 networks × 2 regions × 3 collected rounds, one stopped and one failed_ours cell."""
    root = tmp_path_factory.mktemp("campaign") / CID
    paths = {}
    for network in NETWORKS:
        config = ROOT / "configs" / f"{network}.toml"
        paths[network] = campaign.create(
            config,
            "residential",
            campaign.timestamp(START),
            root / "networks",
            execution_mode="ephemeral",
            now=START - timedelta(hours=1),
        )
    spec_path(root).write_text(_spec({n: p.parent.name for n, p in paths.items()}).to_json())
    for hour in range(3):
        for network in NETWORKS:
            for region in REGIONS:
                if (network, region, hour) != FAILED:
                    asyncio.run(_round(paths[network], network, region, hour))
        statuses = _cells("destroyed")
        if hour == 2:
            statuses[FAILED[:2]] = ["boot_failed", "create_failed"]
        write_hour(root, hour, statuses)
    return root


@pytest.fixture
def root(built, tmp_path):
    return Path(shutil.copytree(built, tmp_path / CID))


def finish_day(root: Path) -> None:
    """The rest of the day failed on our side, so every cell is finished."""
    for hour in range(3, 24):
        write_hour(root, hour, _cells("create_failed"))
