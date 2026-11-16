"""The hour engine, harvest: pull, verify, record and remove each finished worker.

Everything runs against the in-process fake DigitalOcean and a fake `Remote`; no request or
SSH connection leaves the machine. Addresses are RFC 5737 documentation addresses.
"""

from __future__ import annotations

import json
import shutil

import pytest
from campaign_fakes import CID, FakeDigitalOcean, Fault
from campaign_hour_cases import (
    AT_HH,
    HOUR,
    NETWORKS,
    PRE_CREATE,
    REGIONS,
    RUNNING,
    SCHEDULED,
    FakeClock,
    ListFailsAfterCreate,
    ended,
    harvest_setup,
    harvested,
    launched,
    make_spec,
)

from atlas.campaign import fanout, harvest, launch
from atlas.campaign.contracts import HourRecord, budget_stop_path, hour_path


async def test_a_finished_round_is_pulled_verified_recorded_and_removed(tmp_path):
    """The happy path; verify_round ignores the sampler and start files; idempotent (14)."""
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    remote.inspected[record.droplets[0].name] = RUNNING
    await harvested(spec, record, root, fake, remote, clock)
    worker = record.droplets[0]
    assert worker.status == "running" and worker.start_lateness_seconds == 1.5
    assert record.state == "running" and fake.deleted == []
    remote.inspected[worker.name] = ended()
    await harvested(spec, record, root, fake, remote, clock)
    assert worker.status == "destroyed" and fake.deleted == [("droplet", worker.droplet_id)]
    assert (worker.exit_code, worker.error_class) == (0, None)
    assert (worker.requests, worker.successes, worker.errors) == (9, 6, 3)
    assert (worker.bytes_up, worker.bytes_down) == (600, 1800)
    assert (worker.cpu_p50_pct, worker.cpu_max_pct, worker.cpu_core_max_pct) == (60.0, 90.0, 95.0)
    assert worker.rss_peak_mb == 200.0 and worker.mem_used_max_pct == 30.0
    assert record.cells == {"net00/tor1": "complete"} and record.state == "done"
    traffic = record.traffic["net00"]
    assert (traffic["countries_done"], traffic["countries_total"], traffic["bytes_down"]) == (
        3,
        3,
        1800,
    )
    assert traffic["worker_hours"] > 0
    local = root / "networks/net00/residential/m-net00/regions/tor1/rounds/00"
    assert (local / "host-samples.jsonl").exists() and (local / "worker-start.json").exists()
    assert (root / "logs/00/net00-tor1.journal.txt").read_bytes() == b"journal"
    assert "203.0.113" not in hour_path(root, 0).read_text()
    before = len(fake.requests)
    again = launch.load_hour(root, 0)
    await harvested(spec, again, root, fake, remote, clock)
    assert len(fake.requests) == before and again.to_dict() == record.to_dict()


async def test_a_long_round_is_never_stopped_only_alerted(tmp_path):
    """5: info at 3 h, high at 12 h; no stop and no removal."""
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    remote.inspected[record.droplets[0].name] = RUNNING
    for hours in (1, 3.1, 12.1):
        clock.t = AT_HH + hours * HOUR
        await harvested(spec, record, root, fake, remote, clock)
    assert [(a["level"], a["kind"]) for a in record.alerts] == [
        ("info", "round_long_3h"),
        ("high", "round_long_12h"),
    ]
    assert remote.stops == [] and fake.deleted == [] and record.droplets[0].status == "running"


@pytest.mark.parametrize("samples", [True, False])
async def test_a_failed_round_unit_is_collected_partially_and_removed(tmp_path, samples):
    """6: exit not 0 or 130 -> pull, journal, failed_ours, removal; no samples is fine."""
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path, samples=samples)
    remote.inspected[record.droplets[0].name] = ended(1, "failed")
    await harvested(spec, record, root, fake, remote, clock)
    worker = record.droplets[0]
    assert (worker.status, worker.exit_code, worker.error_class) == ("destroyed", 1, "round_failed")
    assert record.cells == {"net00/tor1": "failed_ours"} and len(remote.pulls) == 1
    assert (worker.cpu_max_pct is None) is (not samples)


async def test_a_unit_that_failed_before_its_checkpoint_is_still_removed(tmp_path):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    (remote.source / "round.json").unlink()
    remote.inspected[record.droplets[0].name] = {
        "service": {"ActiveState": "failed", "SubState": "failed", "ExecMainStatus": "1"}
    }
    await harvested(spec, record, root, fake, remote, clock)
    assert record.droplets[0].status == "destroyed" and record.droplets[0].requests == 0
    assert record.cells == {"net00/tor1": "failed_ours"}


async def test_an_operator_stop_is_partial(tmp_path):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    remote.inspected[record.droplets[0].name] = ended(130, "stopped")
    await harvested(spec, record, root, fake, remote, clock)
    assert record.cells == {"net00/tor1": "partial"} and record.droplets[0].error_class is None


async def test_a_budget_stop_is_stopped_budget_not_partial(tmp_path):
    """DAILY-BUDGET-STOP-1: the cells the hour's budget stopped (daily.overtime's record)."""
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    budget_stop_path(root, 0).write_text(json.dumps({"cells": ["net00/tor1"]}))
    remote.inspected[record.droplets[0].name] = ended(130, "stopped")
    await harvested(spec, record, root, fake, remote, clock)
    assert record.cells == {"net00/tor1": "stopped_budget"} and record.state == "done"
    assert record.droplets[0].error_class is None


async def test_a_verify_failure_is_repulled_and_kept_with_an_alert(tmp_path):
    """7: re-pull every 5 min; high alert after 30 min; never removed meanwhile."""
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    worker = record.droplets[0]
    remote.inspected[worker.name] = ended()
    chunk = next((remote.source / "ca").glob("observations-*.jsonl.gz"))
    good = chunk.read_bytes()
    chunk.write_bytes(good + b"\0")
    await harvested(spec, record, root, fake, remote, clock)
    assert worker.status == "collect_failed" and len(remote.pulls) == 1
    clock.t += 60
    await harvested(spec, record, root, fake, remote, clock)
    assert len(remote.pulls) == 1  # not yet 5 minutes
    for _ in range(6):
        clock.t += harvest.PULL_RETRY_SECONDS
        await harvested(spec, record, root, fake, remote, clock)
    assert len(remote.pulls) == 7 and fake.deleted == [] and record.state == "harvesting"
    assert [(a["level"], a["kind"]) for a in record.alerts] == [("high", "collect_failed")]
    chunk.write_bytes(good)
    clock.t += harvest.PULL_RETRY_SECONDS
    await harvested(spec, record, root, fake, remote, clock)
    assert worker.status == "destroyed" and record.cells == {"net00/tor1": "complete"}


async def test_a_pull_error_keeps_the_worker(tmp_path):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    remote.inspected[record.droplets[0].name] = ended()
    remote.pull_failures = 1
    await harvested(spec, record, root, fake, remote, clock)
    assert record.droplets[0].status == "collect_failed" and fake.deleted == []
    clock.t += harvest.PULL_RETRY_SECONDS
    await harvested(spec, record, root, fake, remote, clock)
    assert record.droplets[0].status == "destroyed"


async def test_an_unreachable_worker_is_kept_and_alerted_after_30_minutes(tmp_path):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    for minutes in (0, 20, 31):
        clock.t = AT_HH + 60 + minutes * 60
        await harvested(spec, record, root, fake, remote, clock)
        assert [a["kind"] for a in record.alerts] == (
            ["worker_unreachable"] if minutes > 30 else []
        )
    assert record.droplets[0].status == "armed" and fake.deleted == []
    memo = json.loads(harvest.harvest_state_path(root, 0).read_text())
    assert memo[record.droplets[0].droplet_id]["unreachable_since"] == AT_HH + 60
    remote.inspected[record.droplets[0].name] = RUNNING
    await harvested(spec, record, root, fake, remote, clock)
    assert (
        "unreachable_since"
        not in json.loads(harvest.harvest_state_path(root, 0).read_text())[
            record.droplets[0].droplet_id
        ]
    )


async def test_a_guard_refusal_in_harvest_is_final_and_fails_the_hour(tmp_path):
    """8: destroy_refused, a high alert, no other removal path."""
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    worker = record.droplets[0]
    remote.inspected[worker.name] = ended()
    fake.droplets[worker.droplet_id]["tags"] = ["prod"]
    await harvested(spec, record, root, fake, remote, clock)
    assert worker.status == "destroy_refused" and fake.deleted == []
    assert record.state == "failed" and record.finished_at
    assert ("high", "destroy_refused") in [(a["level"], a["kind"]) for a in record.alerts]


async def test_a_failed_removal_is_retried_on_the_next_pass(tmp_path):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    worker = record.droplets[0]
    remote.inspected[worker.name] = ended()
    fake.faults.append(Fault("DELETE", f"/v2/droplets/{worker.droplet_id}", 422))
    await harvested(spec, record, root, fake, remote, clock)
    assert worker.status == "destroy_failed" and record.state == "harvesting"
    await harvested(spec, record, root, fake, remote, clock)
    assert worker.status == "destroyed" and record.state == "done"
    assert len(remote.pulls) == 1


async def test_an_unexpected_error_stays_with_its_worker(tmp_path):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    remote.inspected[record.droplets[0].name] = {
        "service": {"ActiveState": "inactive", "ExecMainStatus": "x"}
    }
    await harvested(spec, record, root, fake, remote, clock)
    assert record.droplets[0].error_class == "ValueError" and fake.deleted == []
    assert [a["kind"] for a in record.alerts] == ["harvest_error"]


async def test_harvest_leaves_planned_and_finished_hours_alone(tmp_path):
    record = HourRecord(CID, 0, SCHEDULED)
    spec = make_spec(synthetic=True)
    await harvest.harvest_hour(
        spec, record, tmp_path, cloud=None, remote_factory=None, clock=FakeClock(AT_HH)
    )
    assert record.state == "planned" and not hour_path(tmp_path, 0).exists()


async def test_synthetic_workers_finish_after_synthetic_minutes_without_calls(tmp_path):
    """15: synthetic harvest makes no cloud or SSH calls either."""
    spec = make_spec(synthetic=True)
    root = tmp_path / "campaign"
    for network in spec.networks:
        manifest = root / spec.manifests[network]
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            json.dumps({"regions": {r: ["US"] for r in REGIONS}, "settings": {"concurrency": 2000}})
        )
    record = HourRecord(CID, 0, SCHEDULED)
    clock = FakeClock(PRE_CREATE)
    await launch.launch_hour(spec, record, root, cloud=None, remote_factory=None, clock=clock)
    clock.t = AT_HH + 60
    await harvest.harvest_hour(spec, record, root, cloud=None, remote_factory=None, clock=clock)
    assert record.state == "running"
    clock.t = AT_HH + launch.SYNTHETIC_MINUTES * 60
    await harvest.harvest_hour(spec, record, root, cloud=None, remote_factory=None, clock=clock)
    assert {d.status for d in record.droplets} == {"destroyed"} and record.state == "done"
    assert set(record.cells.values()) == {"complete"}


@pytest.mark.parametrize(
    "info,expected",
    [
        (RUNNING, (False, None)),
        ({"service": {"ActiveState": "activating"}}, (False, None)),
        (ended(0), (True, 0)),
        (ended(130, "stopped"), (True, 130)),
        (
            {
                "service": {"ActiveState": "inactive", "ExecMainStatus": "0"},
                "state": {"status": "running"},
            },
            (True, 1),
        ),
        ({}, (True, 1)),
    ],
)
def test_round_ended_reads_the_unit_and_the_checkpoint(info, expected):
    assert harvest.round_ended(info) == expected


async def test_a_missing_checkpoint_after_a_clean_exit_is_repulled_and_a_journal_error_is_info(
    tmp_path,
):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    remote.inspected[record.droplets[0].name] = ended()
    checkpoint = (remote.source / "round.json").read_bytes()
    (remote.source / "round.json").unlink()
    await harvested(spec, record, root, fake, remote, clock)
    assert record.droplets[0].status == "collect_failed" and fake.deleted == []

    async def broken(droplet, local):
        raise fanout.RemoteError("journal failed (exit 1)")

    remote.journal = broken
    (remote.source / "round.json").write_bytes(checkpoint)
    clock.t += harvest.PULL_RETRY_SECONDS
    await harvested(spec, record, root, fake, remote, clock)
    assert record.droplets[0].status == "destroyed"
    assert [(a["level"], a["kind"]) for a in record.alerts] == [("info", "journal_failed")]


async def test_b2_a_killed_round_is_collected_partially_and_removed(tmp_path):
    """B2: SIGKILL left the checkpoint `running`; the ended unit still lets it be pulled."""
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    checkpoint = json.loads((remote.source / "round.json").read_text())
    (remote.source / "round.json").write_text(json.dumps(checkpoint | {"status": "running"}))
    worker = record.droplets[0]
    remote.inspected[worker.name] = {
        "service": {"ActiveState": "failed", "SubState": "failed", "ExecMainStatus": "9"},
        "state": {"status": "running", "jobs": [{"started_at": "2027-01-01T22:00:01.500Z"}]},
    }
    await harvested(spec, record, root, fake, remote, clock)
    assert (worker.status, worker.exit_code, worker.error_class) == ("destroyed", 9, "round_failed")
    assert record.cells == {"net00/tor1": "failed_ours"} and record.state == "done"
    local = root / "networks/net00/residential/m-net00/regions/tor1/rounds/00"
    assert json.loads((local / "round.json").read_text())["status"] == "running"
    assert fake.deleted == [("droplet", worker.droplet_id)]


async def test_b2_a_running_round_is_never_pulled_even_if_inspect_misreads_it(tmp_path):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    checkpoint = json.loads((remote.source / "round.json").read_text())
    (remote.source / "round.json").write_text(json.dumps(checkpoint | {"status": "running"}))
    worker = record.droplets[0]
    remote.inspected[worker.name] = {
        "service": {"ActiveState": "active", "SubState": "running", "ExecMainStatus": "0"}
    }
    worker.status, worker.exit_code = "finished", 0
    await harvested(spec, record, root, fake, remote, clock)
    assert worker.status == "collect_failed" and fake.deleted == []


async def test_b1_after_a_readiness_api_error_the_hour_reaches_done(tmp_path):
    """B1 (a): every created droplet was retired at launch, so one pass finishes the hour."""
    fake = ListFailsAfterCreate()
    spec = make_spec(NETWORKS[:2], REGIONS[:1])
    record, root, remote, clock = await launched(tmp_path, fake, spec)
    await harvested(spec, record, root, fake, remote, clock)
    assert record.state == "done" and {d.status for d in record.droplets} == {
        "destroyed",
        "create_failed",
    }


async def test_b1_a_failed_region_does_not_keep_the_hour_open(tmp_path):
    """B1 (c): a region whose create raised ends create_failed; harvest finishes the hour."""
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:1], ("fra1",))
    original = launch._Launch._create_call

    async def broken(self, workers):
        raise RuntimeError("boom")

    launch._Launch._create_call = broken
    try:
        record, root, remote, clock = await launched(tmp_path, fake, spec)
    finally:
        launch._Launch._create_call = original
    await harvested(spec, record, root, fake, remote, clock)
    assert [d.status for d in record.droplets] == ["create_failed"] * (
        1 + launch.REPLACEMENTS_PER_CELL
    ) and record.state == "done"


async def test_m7_a_collect_failed_worker_without_a_memo_is_repulled(tmp_path):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    worker = record.droplets[0]
    worker.status, worker.exit_code, worker.finished_at = "collect_failed", 0, SCHEDULED
    assert not harvest.harvest_state_path(root, 0).exists()
    await harvested(spec, record, root, fake, remote, clock)
    assert worker.status == "destroyed" and len(remote.pulls) == 1
    assert [a["kind"] for a in record.alerts] == []


async def test_1e_r1_an_unconfirmed_start_that_never_ran_ends_failed_ours(tmp_path):
    """A worker armed as start_unconfirmed whose round never started: removed, never
    start_failed; armed_at stays, so 1e never reads it as a round that provably did not run."""
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    shutil.rmtree(remote.source)
    remote.source.mkdir()
    worker = record.droplets[0]
    worker.error_class = "start_unconfirmed"
    remote.inspected[worker.name] = {"server_time": 0}  # reachable, no fleet marker
    await harvested(spec, record, root, fake, remote, clock)
    assert (worker.status, worker.exit_code, worker.error_class) == ("destroyed", 1, "round_failed")
    assert worker.armed_at and record.cells == {"net00/tor1": "failed_ours"}
    assert record.state == "done"
