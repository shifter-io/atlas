"""Shared helpers of the daily test (DAILY-1): formulas, the template campaign and its hours,
the runner's inputs and fake services, observation chunks and a collected daily test. Offline;
no droplet, request or database outside the test is touched.
"""

from __future__ import annotations

import gzip
import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from campaign_fakes import CID
from campaign_hour_cases import PRE_CREATE, SCHEDULED, FakeRemote, make_root, make_spec
from helpers import FakeTransport
from onboarding_round_cases import make
from test_onboarding import ROOT, START, Clock, Proxy

from atlas import RunConfig, run
from atlas.campaign import launch
from atlas.campaign.contracts import (
    WORKER_TAG,
    HourRecord,
    campaign_tag,
    droplet_name,
    hour_path,
    hour_tag,
    spec_path,
)
from atlas.campaign.tick import save_hour
from atlas.daily.runner import RunInputs
from atlas.onboarding import manifest as campaign
from atlas.onboarding.files import read_json
from atlas.onboarding.rounds import execute_round

SCHEDULES = {
    "shifter": {"PM": 5, "CA": 5, "DE": 5, "US": 6},
    "geonode": {"GB": 5, "FR": 5, "SG": 7},
}


def formulas(tmp_path, schedules=SCHEDULES):
    folder = tmp_path / "formulas"
    folder.mkdir(exist_ok=True)
    for network, schedule in schedules.items():
        body = {"network": network, "schedule": schedule}
        (folder / f"{network}.json").write_text(json.dumps(body))
    return folder


def template(tmp_path, regions=("lon1", "fra1")):
    """A planned template campaign on the fake manager, with a real ephemeral manifest."""
    spec = make_spec(networks=("shifter",), regions=regions)
    root = make_root(tmp_path, spec)
    path = root / spec.manifests["shifter"]
    shutil.copyfile(make(tmp_path / "planned", now=START - timedelta(hours=2)), path)
    shutil.copyfile(ROOT / "configs/shifter.toml", path.parent / "provider.toml")
    spec_path(root).parent.mkdir(parents=True, exist_ok=True)
    spec_path(root).write_text(spec.to_json())
    return root


def hours(root):
    return [p for p in root.parent.iterdir() if p != root]


def chunk(folder, name, rows):
    folder.mkdir(parents=True, exist_ok=True)
    with gzip.open(folder / name, "wt") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


def observation(ip, at):
    return {"ip": ip, "country_code": "CA", "observed_at": at}


async def daily_root(tmp_path):
    """A collected daily test: shifter due in fra1 (DE, never run) and tor1 (CA, PM, run)."""
    root = tmp_path / "daily"
    path = make(root, due_countries={"fra1": ["DE"], "tor1": ["CA", "PM"]})
    spec = make_spec(
        networks=("shifter",),
        regions=("fra1", "tor1"),
        manifests={"shifter": path.relative_to(root).as_posix()},
        starts_at=campaign.timestamp(START),
    )
    spec_path(root).parent.mkdir(parents=True, exist_ok=True)
    spec_path(root).write_text(spec.to_json())
    clock = Clock(START.timestamp())

    async def measure(config, sink, options):
        config = RunConfig("generic_http", config.proxy, requests=3, concurrency=1)
        options = replace(options, deadline=None, transport_factory=FakeTransport)
        result = await run(config, sink, options)
        clock.value += 30
        return result

    await execute_round(path, "tor1", 0, Proxy, clock=clock, run_country=measure)
    return root


def inputs(tmp_path, root, **schedule):
    return RunInputs(root, formulas(tmp_path, {"shifter": schedule}), start="next-hour")


NOW = datetime.fromtimestamp(PRE_CREATE, UTC)


class Borrowed:
    """The test owns the client; run_hour's close is a no-op here."""

    def __init__(self, cloud):
        self.cloud = cloud

    def __getattr__(self, name):
        return getattr(self.cloud, name)

    async def close(self):
        pass


def in_flight(root, fake, spec):
    """A recorded worker whose create answered after a crash: its name, but no id yet."""
    record = HourRecord(CID, 0, SCHEDULED, state="launching")
    record.traffic[spec.networks[0]] = dict.fromkeys(launch.TRAFFIC_KEYS, 0)
    name = droplet_name(CID, 0, "lon1", spec.networks[0])
    record.droplets.append(
        launch.DropletRecord(spec.networks[0], "lon1", 0, name, attempts=1, requested_at=SCHEDULED)
    )
    save_hour(root, record)
    return fake.add(name, [WORKER_TAG, campaign_tag(CID), hour_tag(CID, 0)])


def hanging_run(tmp_path, cloud, clock, after_launch):
    """Services whose launcher records the hour's workers, then calls `after_launch`."""
    remote = FakeRemote(clock)

    async def launcher(spec, record, hour_root):
        await launch.launch_hour(
            spec, record, hour_root, cloud=cloud, remote_factory=lambda h: remote, clock=clock
        )
        await after_launch()

    return lambda hour_root, spec: (Borrowed(cloud), None, launcher, None)


def removed(fake, hour_root):
    ids = [d["droplet_id"] for d in read_json(hour_path(hour_root, 0))["droplets"]]
    return len(ids) == 2 and all(("droplet", str(i)) in fake.deleted for i in ids)
