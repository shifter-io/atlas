"""Shared helpers of the tick, collector and watch tests: specs, a fake clock, fake workers.

Offline only: fake launchers and harvesters, a fake clock, fixed disk readings, a fake SSH
remote and the CAMP-1c host map, as the CAMP-1d suites use them.
"""

from __future__ import annotations

import json
import re
import sys
import types
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from campaign_fakes import CAMPAIGN_PROJECT, CID, MANAGER_ID, SID, SPEC_TIME

from proxy_index.campaign.collector import Pacing
from proxy_index.campaign.contracts import (
    WORKER_TAG,
    CampaignSpec,
    DropletRecord,
    HourRecord,
    spec_path,
)
from proxy_index.campaign.tick import save_hour

START = datetime(2027, 1, 1, 22, tzinfo=UTC)
IPV4 = re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")
OURS = [WORKER_TAG, f"pi-camp-{SID}", f"pi-camp-{SID}-h03"]


def make_spec(**changes) -> CampaignSpec:
    base = CampaignSpec(
        campaign_id=CID,
        created_at=SPEC_TIME,
        account="leftclick",
        project_id=CAMPAIGN_PROJECT,
        starts_at=START.isoformat(),
        grace_hours=48,
        regions=("lon1", "nyc3"),
        networks=("shifter", "geonode"),
        manifests={"shifter": "a.json", "geonode": "b.json"},
        size_by_region={"lon1": "c-2", "nyc3": "c-2"},
        image_by_region={"lon1": "11", "nyc3": "12"},
        image_name="pi-camp-worker-0.9.5",
        wheel_sha256="a" * 64,
        framework_version="0.9.5",
        ssh_key_id="9",
        manager_droplet_id=MANAGER_ID,
    )
    return replace(base, **changes)


def make_root(tmp_path: Path, **changes) -> Path:
    root = tmp_path / "campaign"
    root.mkdir()
    spec_path(root).write_text(make_spec(**changes).validate().to_json())
    return root


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def worker(network, region, hour, **values) -> DropletRecord:
    return DropletRecord(
        network, region, hour, f"pic-{SID}-h{hour:02d}-{region}-{network}", **values
    )


def fake_launcher(clock, calls):
    async def launch(spec, record, root):
        calls.append((record.hour, clock(), spec.networks))
        record.droplets = [
            worker(
                net,
                region,
                record.hour,
                droplet_id=f"9{record.hour:02d}{i}",
                status="armed",
                requested_at=clock().isoformat(),
            )
            for i, (net, region) in enumerate((n, r) for n in spec.networks for r in spec.regions)
        ]
        record.state = "running"
        return record

    return launch


def finishing_harvester(clock, finish_on: dict[int, int]):
    """Hour h becomes done on its finish_on[h]-th harvest pass."""
    passes: dict[int, int] = {}

    async def harvest(spec, record, root):
        passes[record.hour] = passes.get(record.hour, 0) + 1
        if passes[record.hour] < finish_on[record.hour]:
            record.state = "harvesting"
            return record
        stamp = clock().isoformat()
        for drop in record.droplets:
            drop.status, drop.finished_at, drop.collected_at, drop.destroyed_at = (
                "destroyed",
                stamp,
                stamp,
                stamp,
            )
            drop.bytes_up, drop.bytes_down, drop.requests, drop.successes = 10**8, 4 * 10**8, 50, 45
            drop.errors = 5
        record.state = "done"
        record.cells = {f"{d.network}/{d.region}": "complete" for d in record.droplets}
        return record

    return harvest


def running_hour(root, hour, clock):
    record = HourRecord(CID, hour, (START + timedelta(hours=hour)).isoformat(), state="running")
    record.droplets = [
        worker(
            net,
            region,
            hour,
            droplet_id=f"8{hour}{i}",
            status="running",
            requested_at=clock().isoformat(),
            started_at=clock().isoformat(),
        )
        for i, (net, region) in enumerate(
            [("shifter", "lon1"), ("shifter", "nyc3"), ("geonode", "lon1")]
        )
    ]
    save_hour(root, record)


def disk(free_pct):
    """A fixed disk reading: the collector's alerts never depend on the test host's disk."""
    return lambda _path: types.SimpleNamespace(total=100, free=free_pct)


def pacing(clock, step=timedelta(seconds=30)):
    async def sleep(_seconds):
        clock.now += step

    return Pacing(sleep=sleep, clock_synced=lambda: True, disk_usage=disk(50))


def install_host_map(monkeypatch):
    """CAMP-1c's host-map helpers (`launch.hosts_path`, `launch.read_hosts`), same format."""

    def path(root, hour):
        return root / ".runtime" / f"hosts-{hour:02d}.json"

    hosts = types.SimpleNamespace(
        hosts_path=path,
        read_hosts=lambda root, hour: (
            json.loads(path(root, hour).read_text()) if path(root, hour).exists() else {}
        ),
    )
    monkeypatch.setitem(sys.modules, "proxy_index.campaign.launch", hosts)
    return hosts


class FakeRemote:
    def __init__(self, answers):
        self.answers = answers

    async def inspect(self, droplet):
        answer = self.answers[droplet.name]
        if isinstance(answer, Exception):
            raise answer
        return answer


def watch_data(now, *, reports=None, alerts=(), finished=False, offset=0):
    status = {
        "now": now.isoformat(),
        "live_workers": 16,
        "finished": finished,
        "hours": [{"hour": 0, "state": "done"}, {"hour": 1, "state": "running"}],
    }
    tsv = "hour\tnetwork\tbytes_up\tbytes_down\n00\tshifter\t1000000000\t2000000000\n"
    return {
        "status": status,
        "alerts": [json.dumps(a) for a in alerts],
        "offset": offset,
        "reports": reports or {},
        "tsv": tsv,
    }
