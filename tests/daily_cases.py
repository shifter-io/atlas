"""Shared helpers of the daily test (DAILY-1): formulas, the template campaign and its hours,
observation chunks and a collected daily test. Offline; no droplet, request or database outside
the test is touched.
"""

from __future__ import annotations

import gzip
import json
import shutil
from dataclasses import replace
from datetime import timedelta

from campaign_hour_cases import make_root, make_spec
from helpers import FakeTransport
from onboarding_round_cases import make
from test_onboarding import ROOT, START, Clock, Proxy

from proxy_index import RunConfig, run
from proxy_index.campaign.contracts import spec_path
from proxy_index.onboarding import manifest as campaign
from proxy_index.onboarding.rounds import execute_round

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
