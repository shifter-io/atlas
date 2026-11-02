"""DAILY-1: the hourly daily test, offline.

The planner, the manifest's `due_countries`, the launcher's skip of regions with nothing due,
the runner's clean-up on failure (against the in-process fake DigitalOcean) and the ingest path
(a real round through the production runner with a fake transport, a fake DB-IP database and an
isolated PostgreSQL). No droplet, request or database outside the test is touched.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import shutil
import signal
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from campaign_fakes import CAMPAIGN_PROJECT, CID, FakeDigitalOcean, running
from helpers import FakeTransport
from test_campaign_hour import (
    PRE_CREATE,
    SCHEDULED,
    FakeClock,
    FakeRemote,
    launched,
    make_root,
    make_spec,
)
from test_onboarding import ROOT, START, Clock, Proxy
from test_onboarding_round import TOR1, make

from proxy_index import RunConfig, run
from proxy_index.campaign import launch
from proxy_index.campaign.collector import Pacing
from proxy_index.campaign.contracts import (
    WORKER_TAG,
    HourRecord,
    campaign_tag,
    droplet_name,
    hour_path,
    hour_tag,
    spec_path,
)
from proxy_index.campaign.do_api import ApiError, ScopeError
from proxy_index.campaign.tick import load_hour, save_hour
from proxy_index.cli import main
from proxy_index.daily import ingest as daily_ingest
from proxy_index.daily.planner import Cell, load_schedules, plan_hour
from proxy_index.daily.runner import (
    RunError,
    RunInputs,
    clean_up,
    run_hour,
    start_hour,
    time_limit,
)
from proxy_index.onboarding import manifest as campaign
from proxy_index.onboarding.files import lock, read_json
from proxy_index.onboarding.rounds import execute_round, new_round
from proxy_index.orchestrator.db import Database
from proxy_index.storage import connect, seed

pytest_plugins = ["test_storage", "test_enrichment_cli"]

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


# --- planner -------------------------------------------------------------------------------


def test_the_hour_groups_due_countries_into_one_droplet_per_network_and_region(tmp_path):
    plan = plan_hour(load_schedules(formulas(tmp_path)), 5)
    assert plan.cells == (
        Cell("geonode", "lon1", ("GB", "FR")),
        Cell("shifter", "fra1", ("DE",)),
        Cell("shifter", "tor1", ("CA", "PM")),
    )
    assert [c.region_id for c in plan.cells] == [1, 2, 5]
    assert plan.networks() == ("geonode", "shifter")
    assert plan.regions() == ("lon1", "fra1", "tor1")
    assert plan.due("shifter") == {"fra1": ["DE"], "tor1": ["CA", "PM"]}
    assert plan.to_dict()["droplets"] == 3 and plan.to_dict()["skipped"] == []
    assert [c.region for c in plan_hour(load_schedules(formulas(tmp_path)), 6).cells] == ["nyc3"]


def test_networks_the_runner_cannot_test_are_skipped_and_an_empty_hour_has_no_droplet(tmp_path):
    schedules = load_schedules(formulas(tmp_path))
    plan = plan_hour(schedules, 5, networks=("shifter",))
    assert plan.networks() == ("shifter",) and plan.skipped == ("geonode",)
    empty = plan_hour(schedules, 12)
    assert empty.cells == () and empty.to_dict()["droplets"] == 0


@pytest.mark.parametrize(
    "body, message",
    [
        ({"network": "x"}, "needs a network and a schedule"),
        ({"network": "x", "schedule": {}}, "needs a network and a schedule"),
        ({"network": "x", "schedule": {"de": 1}}, "invalid country code"),
        ({"network": "x", "schedule": {"DE": 24}}, "test hour 0..23"),
        ({"network": "x", "schedule": {"DE": True}}, "test hour 0..23"),
    ],
)
def test_bad_formulas_are_refused(tmp_path, body, message):
    (tmp_path / "x.json").write_text(json.dumps(body))
    with pytest.raises(ValueError, match=message):
        load_schedules(tmp_path)


def test_a_network_with_two_formulas_or_an_unknown_country_is_refused(tmp_path):
    for name in ("a", "b"):
        body = {"network": "x", "schedule": {"DE": 1}}
        (tmp_path / f"{name}.json").write_text(json.dumps(body))
    with pytest.raises(ValueError, match="two formulas"):
        load_schedules(tmp_path)
    with pytest.raises(ValueError, match="countries in no region: ZZ"):
        plan_hour({"x": {"ZZ": 1}}, 1)
    with pytest.raises(ValueError, match="hour must be 0..23"):
        plan_hour({}, 24)


def test_start_hour():
    now = datetime(2027, 1, 1, 21, 58, 30, tzinfo=UTC)
    hour = datetime(2027, 1, 1, 21, tzinfo=UTC)
    assert start_hour("now-hour", now) == (hour, True)
    assert start_hour("next-hour", now) == (hour + timedelta(hours=1), False)
    assert start_hour("2027-01-02T01:00:00+04:00", now) == (hour, True)
    assert start_hour("2027-01-01T22:00:00Z", now) == (hour + timedelta(hours=1), False)
    with pytest.raises(RunError, match="explicit UTC offset"):
        start_hour("2027-01-01T22:00:00", now)


def test_plan_cli_prints_the_droplet_plan_and_writes_nothing(tmp_path, capsys):
    folder = formulas(tmp_path)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    code = main(["daily", "plan", "--formulas", str(folder), "--start", "2027-01-01T05:00:00Z"])
    plan = json.loads(capsys.readouterr().out)
    assert code == 0 and plan["droplets"] == 3 and plan["starts_at"].startswith("2027-01-01T05")
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


# --- the manifest's due_countries ----------------------------------------------------------


def test_due_countries_limit_the_round_to_the_countries_due(tmp_path):
    path = make(tmp_path, due_countries={"tor1": ["CA", "PM"]})
    data = campaign.load(path)
    assert campaign.region_countries(data, "tor1") == ["CA", "PM"]
    assert campaign.region_countries(data, "fra1") == []
    assert [job["country"] for job in new_round(data, "tor1", 0)["jobs"]] == ["CA", "PM"]
    whole = campaign.load(make(tmp_path / "whole"))
    assert campaign.region_countries(whole, "tor1") == TOR1


@pytest.mark.parametrize(
    "bad",
    [{}, {"tor1": []}, {"xyz1": ["CA"]}, {"tor1": ["PM", "CA"]}, {"tor1": ["DE"]}, {"tor1": "CA"}],
)
def test_bad_due_countries_are_refused(tmp_path, bad):
    data = campaign.load(make(tmp_path))
    with pytest.raises(ValueError, match="due_countries"):
        campaign.validate(deepcopy(data) | {"due_countries": bad})


def test_due_countries_are_ephemeral_only(tmp_path):
    with pytest.raises(ValueError, match="due_countries"):
        make(tmp_path, execution_mode="parallel", due_countries={"tor1": ["CA"]})


async def test_the_launch_creates_no_worker_for_a_region_with_nothing_due(tmp_path):
    spec = make_spec()  # net00, net01 in lon1, fra1
    root = make_root(tmp_path, spec)
    path = root / spec.manifests["net00"]
    path.write_text(json.dumps(json.loads(path.read_text()) | {"due_countries": {"lon1": ["GB"]}}))
    fake = FakeDigitalOcean()
    record, _, remote, _ = await launched(tmp_path, fake, spec)
    workers = sorted((d.network, d.region) for d in record.droplets)
    assert workers == [("net00", "lon1"), ("net01", "fra1"), ("net01", "lon1")]
    assert record.traffic["net00"]["countries_total"] == 1
    assert record.traffic["net01"]["countries_total"] == 4
    assert len(remote.pushes) == 3


# --- the runner ----------------------------------------------------------------------------


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


def inputs(tmp_path, root, **schedule):
    return RunInputs(root, formulas(tmp_path, {"shifter": schedule}), start="next-hour")


def hours(root):
    return [p for p in root.parent.iterdir() if p != root]


NOW = datetime.fromtimestamp(PRE_CREATE, UTC)


class Borrowed:
    """The test owns the client; run_hour's close is a no-op here."""

    def __init__(self, cloud):
        self.cloud = cloud

    def __getattr__(self, name):
        return getattr(self.cloud, name)

    async def close(self):
        pass


async def test_a_failure_after_the_launch_still_removes_every_worker(tmp_path):
    root = template(tmp_path)
    fake, clock = FakeDigitalOcean(), FakeClock(PRE_CREATE)
    async with running(fake) as cloud:
        remote = FakeRemote(clock)

        async def broken(spec, record, hour_root):
            await launch.launch_hour(
                spec, record, hour_root, cloud=cloud, remote_factory=lambda h: remote, clock=clock
            )
            raise RuntimeError("the manager lost its connection")

        def services(hour_root, spec):
            return Borrowed(cloud), None, broken, None

        due = inputs(tmp_path, root, GB=22, DE=22, US=3)
        with pytest.raises(RuntimeError):
            await run_hour(due, services=services, now=NOW, clock=clock)
    (hour_root,) = hours(root)
    outcome = read_json(hour_root / "daily.json")
    assert outcome["error"] == "RuntimeError" and outcome["leftover"] == []
    assert outcome["plan"]["droplets"] == 2
    record = read_json(hour_path(hour_root, 0))
    ids = [d["droplet_id"] for d in record["droplets"]]
    assert len(ids) == 2 and len(remote.pushes) == 2
    assert all(("droplet", str(i)) in fake.deleted and str(i) not in fake.droplets for i in ids)
    spec = json.loads(spec_path(hour_root).read_text())
    assert spec["regions"] == ["lon1", "fra1"] and spec["hours"] == 1
    hour = campaign.load(hour_root / spec["manifests"]["shifter"])
    planned = campaign.load(root / make_spec(networks=("shifter",)).manifests["shifter"])
    assert hour["due_countries"] == {"lon1": ["GB"], "fra1": ["DE"]}
    assert hour["settings"] == planned["settings"]
    assert hour["target_url"] == planned["target_url"]


async def test_a_worker_still_tagged_for_the_hour_is_reported_left_over(tmp_path):
    spec = make_spec()
    root = make_root(tmp_path, spec)
    fake = FakeDigitalOcean()
    stray = fake.add("pic-stray", tags=[hour_tag(CID, 0)])
    async with running(fake) as cloud:
        assert await clean_up(root, spec, cloud, FakeClock(PRE_CREATE)) == ([str(stray)], None)
        assert await clean_up(root, make_spec(synthetic=True), cloud) == ([], None)


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


async def test_a_worker_whose_create_was_in_flight_is_taken_by_name_and_removed(tmp_path):
    spec = make_spec()
    root = make_root(tmp_path, spec)
    fake = FakeDigitalOcean()
    worker = in_flight(root, fake, spec)
    fake.add(droplet_name(CID, 0, "lon1", spec.networks[0]), ["pi-camp-other"])  # not ours
    async with running(fake) as cloud:
        assert await clean_up(root, spec, cloud, FakeClock(PRE_CREATE)) == ([], None)
    assert ("droplet", str(worker)) in fake.deleted and str(worker) not in fake.droplets
    assert len(fake.droplets) == 1  # the same name without our tags stays
    assert read_json(hour_path(root, 0))["droplets"][0]["status"] == "destroyed"


async def test_a_held_hour_lock_says_why_nothing_was_removed(tmp_path):
    spec = make_spec()
    root = make_root(tmp_path, spec)
    fake = FakeDigitalOcean()
    worker = in_flight(root, fake, spec)
    async with running(fake) as cloud:
        with lock(hour_path(root, 0).parent / ".lock"):
            left, skipped = await clean_up(root, spec, cloud, FakeClock(PRE_CREATE))
    assert left == [str(worker)] and "locked by a launcher still running" in skipped


@pytest.mark.parametrize("fault", [ApiError("GET", "/v2/droplets", 500), ScopeError("refused")])
async def test_a_failed_take_of_in_flight_workers_still_retires_the_recorded_ones(tmp_path, fault):
    """N1: an API or scope error while taking an in-flight worker stops no recorded removal."""
    spec = make_spec()
    root = make_root(tmp_path, spec)
    fake = FakeDigitalOcean()
    in_flight(root, fake, spec)
    record = load_hour(root, spec, 0)
    name = droplet_name(CID, 0, "lon1", spec.networks[1])
    tags = [WORKER_TAG, campaign_tag(CID), hour_tag(CID, 0)]
    recorded = fake.add(name, tags, project=CAMPAIGN_PROJECT)
    record.traffic[spec.networks[1]] = dict.fromkeys(launch.TRAFFIC_KEYS, 0)
    record.droplets.append(
        launch.DropletRecord(spec.networks[1], "lon1", 0, name, droplet_id=recorded, status="armed")
    )
    save_hour(root, record)

    async def failing(names, tag):
        raise fault

    async with running(fake) as cloud:
        cloud.adopt_uncertain = failing
        left, skipped = await clean_up(root, spec, cloud, FakeClock(PRE_CREATE))
    assert ("droplet", str(recorded)) in fake.deleted and str(recorded) not in left
    assert type(fault).__name__ in skipped and "locked" not in skipped


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


async def test_the_time_limit_stops_a_run_that_never_settles_and_removes_its_workers(tmp_path):
    root = template(tmp_path)
    fake, clock = FakeDigitalOcean(), FakeClock(PRE_CREATE)
    async with running(fake) as cloud:
        services = hanging_run(tmp_path, cloud, clock, lambda: asyncio.sleep(3600))
        due = inputs(tmp_path, root, GB=22, DE=22)
        outcome = await run_hour(due, services=services, now=NOW, clock=clock, limit=1.0)
    (hour_root,) = hours(root)
    assert outcome["error"] == "stopped at the time limit (1 s)" and outcome["leftover"] == []
    assert read_json(hour_root / "daily.json") == outcome and removed(fake, hour_root)


def test_the_time_limit_is_the_slot_plus_the_margin():
    begins = datetime(2027, 1, 1, 22, tzinfo=UTC)
    assert time_limit(begins, begins - timedelta(minutes=2), 60) == 92 * 60
    assert time_limit(begins, begins + timedelta(hours=5), 60) == 0.0


async def test_a_timeout_inside_the_run_is_a_failure_not_the_time_limit(tmp_path):
    """N2: a TimeoutError raised by the run itself is re-raised; the workers still go."""
    root = template(tmp_path)
    fake, clock = FakeDigitalOcean(), FakeClock(PRE_CREATE)

    async def times_out():
        raise TimeoutError("a socket timed out")

    async with running(fake) as cloud:
        services = hanging_run(tmp_path, cloud, clock, times_out)
        due = inputs(tmp_path, root, GB=22, DE=22)
        with pytest.raises(TimeoutError, match="socket"):
            await run_hour(due, services=services, now=NOW, clock=clock, limit=3600.0)
    (hour_root,) = hours(root)
    outcome = read_json(hour_root / "daily.json")
    assert outcome["error"] == "TimeoutError" and outcome["leftover"] == []
    assert removed(fake, hour_root)


def test_a_signal_during_the_run_still_removes_every_worker(tmp_path, monkeypatch, capsys):
    """SIGTERM through `daily run`: the run is cancelled and the clean-up still runs."""
    import proxy_index.daily.runner as runner

    root = template(tmp_path)
    fake, clock = FakeDigitalOcean(), FakeClock(PRE_CREATE)
    real = runner.run_hour

    async def signalled():
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(2)
        raise AssertionError("the signal did not stop the run")

    async def run_with_fakes(due):
        async with running(fake) as cloud:
            services = hanging_run(tmp_path, cloud, clock, signalled)
            return await real(due, services=services, now=NOW, clock=clock)

    monkeypatch.setattr(runner, "run_hour", run_with_fakes)
    folder = formulas(tmp_path, {"shifter": {"GB": 22, "DE": 22}})
    args = ["daily", "run", "--formulas", str(folder), "--template", str(root)]
    previous = signal.signal(signal.SIGTERM, lambda *_: None)  # never kill the test runner
    try:
        assert main(args + ["--start", "next-hour"]) == 3
    finally:
        signal.signal(signal.SIGTERM, previous)
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["error"] == "cancelled by a signal" and outcome["leftover"] == []
    (hour_root,) = hours(root)
    assert removed(fake, hour_root)


async def test_an_hour_with_nothing_due_writes_nothing(tmp_path):
    root = template(tmp_path)
    outcome = await run_hour(inputs(tmp_path, root, GB=3), now=NOW)
    assert outcome["state"] == "nothing_due" and outcome["plan"]["droplets"] == 0
    assert hours(root) == []


async def test_a_region_missing_from_the_template_is_refused(tmp_path):
    root = template(tmp_path)
    with pytest.raises(RunError, match="no region tor1"):
        await run_hour(inputs(tmp_path, root, CA=22), now=NOW)
    assert hours(root) == []


async def test_a_template_that_is_not_ephemeral_leaves_no_hour_behind(tmp_path):
    root = template(tmp_path)
    path = root / make_spec(networks=("shifter",)).manifests["shifter"]
    shutil.copyfile(make(tmp_path / "parallel", execution_mode="parallel"), path)
    with pytest.raises(RunError, match="not an ephemeral campaign"):
        await run_hour(inputs(tmp_path, root, GB=22), now=NOW)
    assert hours(root) == []


async def test_a_finished_hour_is_done_with_nothing_left(tmp_path):
    root = template(tmp_path)

    async def finished(spec, record, hour_root):
        record.state, record.finished_at = "done", NOW.isoformat()
        save_hour(hour_root, record)

    async def harvester(*args, **kwargs):
        raise AssertionError("nothing is left to harvest")

    def services(hour_root, spec):
        return None, None, finished, harvester

    pacing = Pacing(interval=0, clock_synced=lambda: True)
    outcome = await run_hour(
        inputs(tmp_path, root, GB=22), services=services, now=NOW, pacing=pacing
    )
    assert outcome["state"] == "done" and outcome["leftover"] == []


# --- ingest --------------------------------------------------------------------------------


def chunk(folder, name, rows):
    folder.mkdir(parents=True, exist_ok=True)
    with gzip.open(folder / name, "wt") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


def observation(ip, at):
    return {"ip": ip, "country_code": "CA", "observed_at": at}


def test_observations_become_ip_rows_per_bucket_and_are_converted_once(tmp_path):
    folder = tmp_path / "ca"
    chunk(
        folder,
        "observations-000001.jsonl.gz",
        [
            observation("192.0.2.1", "2027-01-01T22:00:01Z"),
            observation("192.0.2.1", "2027-01-01T22:01:00Z"),
        ],
    )
    chunk(
        folder,
        "observations-000002.jsonl.gz",
        [
            observation("192.0.2.1", "2027-01-01T22:05:00Z"),
            observation("192.0.2.2", "2027-01-01T22:00:00Z"),
        ],
    )
    summary = {"run_id": "r1", "schema_version": "1"}
    (folder / "summary.json").write_text(json.dumps({"measurement": summary}))
    directory = daily_ingest.convert_country(folder, tmp_path / "runs")
    with gzip.open(directory / "ips.jsonl.gz", "rt") as stream:
        rows = [json.loads(line) for line in stream]
    assert [(r["ip"], r["bucket_start"], r["count"]) for r in rows] == [
        ("192.0.2.1", "2027-01-01T22:00:00.000000+00:00", 2),
        ("192.0.2.2", "2027-01-01T22:00:00.000000+00:00", 1),
        ("192.0.2.1", "2027-01-01T22:05:00.000000+00:00", 1),
    ]
    assert rows[0]["first_seen"] == "2027-01-01T22:00:01.000000+00:00" and rows[0]["run_id"] == "r1"
    written = read_json(directory / "summary.json")
    assert written["artifacts"]["ips"]["rows"] == 3 and written["run_id"] == "r1"
    (directory / "ips.jsonl.gz").write_bytes(b"kept")
    assert daily_ingest.convert_country(folder, tmp_path / "runs") == directory
    assert (directory / "ips.jsonl.gz").read_bytes() == b"kept"
    (folder / "summary.json").write_text(json.dumps({"measurement": None}))
    with pytest.raises(ValueError, match="no measurement"):
        daily_ingest.convert_country(folder, tmp_path / "other")


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

    async def measure(config, sink, **kwargs):
        kwargs.pop("deadline")
        config = RunConfig("generic_http", config.proxy, requests=3, concurrency=1)
        result = await run(config, sink, transport_factory=FakeTransport, **kwargs)
        clock.value += 30
        return result

    await execute_round(path, "tor1", 0, Proxy, clock=clock, run_country=measure)
    return root


async def test_a_collected_test_is_imported_and_its_events_emitted_once(
    tmp_path, monkeypatch, database, fake_database
):
    import proxy_index.runner as runner

    monkeypatch.setattr(runner.time, "time", lambda: START.timestamp() + 1)
    seed(database)  # db-seed: the runner regions the events reference
    root = await daily_root(tmp_path)
    mmdb, _ = fake_database
    report = await daily_ingest.ingest_campaign(root, database, mmdb)
    tor1, fra1 = report["cells"]["shifter/tor1"], report["cells"]["shifter/fra1"]
    assert (tor1["status"], tor1["ingested"], tor1["events"]) == ("done", ["CA", "PM"], True)
    assert tor1["requests"] == 6 and tor1["unique_ips"] == 2 and tor1["issues"] == []
    assert fra1["status"] == "failed" and fra1["issues"][0].startswith("round unreadable")
    # fra1 comes first, before shifter's product exists: its events wait for the rerun.
    assert fra1["events"] is False and report["complete"] is False
    assert kinds(database) == ["daily_test_finished", "daily_test_started"]
    again = await daily_ingest.ingest_campaign(root, database, mmdb)
    assert again["cells"]["shifter/tor1"]["ingested"] == ["CA", "PM"]
    assert again["cells"]["shifter/fra1"]["events"] is True and again["complete"] is False
    assert kinds(database) == ["daily_test_finished"] * 2 + ["daily_test_started"] * 2
    assert len(list((root / "runs").iterdir())) == 2
    text = (root / daily_ingest.REPORT).read_text()
    seen = set()
    for ips in (root / "runs").glob("*/ips.jsonl.gz"):
        with gzip.open(ips, "rt") as stream:
            seen |= {json.loads(line)["ip"] for line in stream}
    assert seen and not any(ip in text for ip in seen) and str(tmp_path) not in text


def kinds(database):
    with connect(database) as conn:
        return sorted(row[0] for row in conn.execute("SELECT kind FROM test_events"))


async def test_a_network_never_imported_gets_no_events_until_it_is(tmp_path, database):
    root = tmp_path / "daily"
    path = make(root, due_countries={"tor1": ["CA"]})
    spec = make_spec(
        networks=("shifter",),
        regions=("tor1",),
        manifests={"shifter": path.relative_to(root).as_posix()},
    )
    spec_path(root).parent.mkdir(parents=True, exist_ok=True)
    spec_path(root).write_text(spec.to_json())
    db = Database(str(database), size=1)
    try:
        report = await daily_ingest.ingest_campaign(root, database, database=db)
    finally:
        await db.close()
    cell = report["cells"]["shifter/tor1"]
    assert cell["status"] == "failed" and cell["events"] is False


def test_ingest_cli_refuses_a_download_that_did_not_verify(tmp_path, capsys):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "collect-report.json").write_text(json.dumps({"clean": False}))
    args = ["daily", "ingest", "--inventory", str(tmp_path / "inv"), "--campaign-id", CID]
    args += ["--dest", str(dest), "--database-file", str(tmp_path / "db.toml")]
    assert main(args) == 3
    assert "did not verify" in capsys.readouterr().err


def test_ingest_cli_downloads_once_then_imports(tmp_path, monkeypatch, capsys):
    import proxy_index.campaign.collect as collect

    downloads = []

    def fake_collect(inventory, campaign_id, dest):
        downloads.append(campaign_id)
        (dest / collect.REPORT).write_text(json.dumps({"clean": True}))

    async def fake_ingest(root, credentials, mmdb):
        return {"campaign_id": CID, "complete": len(downloads) == 1}

    monkeypatch.setattr(collect, "collect", fake_collect)
    monkeypatch.setattr(daily_ingest, "ingest_campaign", fake_ingest)
    dest = tmp_path / "dest"
    dest.mkdir()
    args = ["daily", "ingest", "--inventory", str(tmp_path / "inv"), "--campaign-id", CID]
    args += ["--dest", str(dest), "--database-file", str(tmp_path / "db.toml")]
    assert main(args) == 0 and json.loads(capsys.readouterr().out)["complete"] is True
    downloads.append("not again")
    assert main(args) == 3 and downloads == [CID, "not again"]  # the rerun reuses the download


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        ({"state": "nothing_due"}, 0),
        ({"state": "done", "leftover": []}, 0),
        ({"state": "done", "leftover": ["1"]}, 3),
        ({"state": "failed", "leftover": []}, 3),
    ],
)
def test_run_cli_exits_3_unless_the_hour_is_done_and_nothing_is_left(
    tmp_path, monkeypatch, capsys, outcome, code
):
    import proxy_index.daily.runner as runner

    seen = []

    async def fake_run(inputs):
        seen.append(inputs)
        return outcome

    monkeypatch.setattr(runner, "run_hour", fake_run)
    args = ["daily", "run", "--formulas", str(tmp_path), "--template", str(tmp_path / "t")]
    assert main(args + ["--start", "next-hour"]) == code
    assert json.loads(capsys.readouterr().out) == outcome
    assert seen == [RunInputs(tmp_path / "t", tmp_path, "next-hour")]


def test_plan_cli_with_a_template_names_the_networks_it_cannot_test(tmp_path, capsys):
    root = template(tmp_path)
    folder = formulas(tmp_path, {"shifter": {"GB": 5}, "geonode": {"DE": 5}})
    args = ["daily", "plan", "--formulas", str(folder), "--template", str(root)]
    assert main(args + ["--start", "2027-01-01T05:00:00+00:00"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["droplets"] == 1 and plan["skipped"] == ["geonode"]
    assert plan["cells"] == [{"network": "shifter", "region": "lon1", "countries": ["GB"]}]
    assert hours(root) == []  # nothing written beside the template
