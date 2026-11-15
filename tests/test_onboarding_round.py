"""Ephemeral one-round execution: offline, with a fake census runner and clock."""

import asyncio
import json
import os
from copy import deepcopy
from datetime import timedelta

import pytest
from onboarding_round_cases import TOR1, census, make, measurement, run_round
from test_onboarding import ROOT, START, Clock, Proxy

from atlas.onboarding import manifest as campaign
from atlas.onboarding.files import read_json
from atlas.onboarding.rounds import execute_round, round_directory, verify_round
from atlas.onboarding.scheduler import COUNTRY_CAP_SECONDS, execute


@pytest.fixture
def ephemeral(tmp_path):
    return make(tmp_path)


def test_v3_manifest_window_grace_and_ephemeral_concurrency(ephemeral, tmp_path):
    data = campaign.load(ephemeral)
    assert (data["schema_version"], data["execution_mode"], data["grace_hours"]) == (
        3,
        "ephemeral",
        48,
    )
    assert campaign.moment(data["ends_at"]) - START == timedelta(hours=72)
    assert len(data["rounds"]) == 24 and data["rounds"][0] == data["starts_at"]
    assert data["settings"]["concurrency"] == campaign.EPHEMERAL_CONCURRENCY == 2000
    window = campaign.regional_window(data, "syd1")
    assert window == {
        "starts_at": data["starts_at"],
        "ends_at": data["ends_at"],
        "rounds": data["rounds"],
    }
    tuned = campaign.load(make(tmp_path / "b", grace_hours=1, run_settings={"concurrency": 2500}))
    assert tuned["settings"]["concurrency"] == 2500
    assert campaign.moment(tuned["ends_at"]) - START == timedelta(hours=25)
    assert campaign.load(make(tmp_path / "c", grace_hours=168))["grace_hours"] == 168
    for grace in (0, 169, True, 2.0):
        with pytest.raises(ValueError, match="grace_hours"):
            make(tmp_path / "d", grace_hours=grace)


@pytest.mark.parametrize(
    "change",
    [
        {"grace_hours": 0},
        {"grace_hours": None},
        {"execution_mode": "parallel"},
        {"region_order": ["lon1"]},
        {"ends_at": "2027-01-04T21:00:00.000Z"},
    ],
)
def test_v3_validation_rejects_tampering(ephemeral, change):
    data = deepcopy(campaign.load(ephemeral)) | change
    with pytest.raises(ValueError):
        campaign.validate(data)


def test_current_hour_only_for_ephemeral_and_never_past_hours(tmp_path):
    now = START + timedelta(minutes=10)
    with pytest.raises(ValueError, match="future"):
        make(tmp_path / "a", now=now)
    path = make(tmp_path / "b", now=now, allow_current_hour=True)
    assert campaign.load(path)["starts_at"] == campaign.timestamp(START)
    with pytest.raises(ValueError, match="future"):
        make(tmp_path / "c", start=START - timedelta(hours=1), now=now, allow_current_hour=True)
    with pytest.raises(ValueError, match="current hour"):
        make(tmp_path / "d", now=now, allow_current_hour=True, execution_mode="parallel")


def test_v1_and_v2_manifests_are_unchanged(tmp_path):
    parallel = campaign.load(make(tmp_path / "a", execution_mode="parallel"))
    assert parallel["schema_version"] == 1 and "grace_hours" not in parallel
    assert parallel["settings"]["concurrency"] == 1000
    assert campaign.moment(parallel["ends_at"]) - START == timedelta(hours=24)
    sequential = campaign.load(make(tmp_path / "b", execution_mode="sequential"))
    assert sequential["schema_version"] == 2 and "grace_hours" not in sequential
    with pytest.raises(ValueError, match="version 2"):
        campaign.validate(parallel | {"execution_mode": "ephemeral"})
    with pytest.raises(ValueError, match="Sequential"):
        campaign.validate(sequential | {"execution_mode": "ephemeral"})


async def test_round_runs_only_its_countries_in_order_with_round_layout(ephemeral):
    result, calls = await run_round(ephemeral, 3)
    data = campaign.load(ephemeral)
    assert [c.country for c in calls] == TOR1
    assert calls[0].at == campaign.moment(data["rounds"][3]).timestamp()
    assert all(c.deadline is None and c.requests is None and c.concurrency == 2000 for c in calls)
    # DAILY-OVERTIME-1 (#1152): no deadline, but each country's own cap.
    assert {c.cap_seconds for c in calls} == {COUNTRY_CAP_SECONDS} == {3600.0}
    folder = round_directory(ephemeral, "tor1", 3)
    saved = read_json(folder / "round.json")
    assert saved == result
    assert set(saved) == {
        "schema_version",
        "kind",
        "campaign_id",
        "region",
        "round",
        "scheduled_at",
        "campaign_sha256",
        "settings",
        "status",
        "active",
        "progress",
        "jobs",
        "pid",
        "updated_at",
        "peak_memory_mb",
    }
    assert (saved["kind"], saved["round"], saved["status"], saved["active"]) == (
        "round",
        3,
        "completed",
        None,
    )
    assert saved["scheduled_at"] == data["rounds"][3] and saved["pid"] == os.getpid()
    assert [j["status"] for j in saved["jobs"]] == ["completed"] * 3
    assert {p.name for p in folder.iterdir()} == {"round.json", "ca", "gl", "pm"}
    assert read_json(folder / "ca/summary.json")["status"] == "completed"
    assert not (ephemeral.parent / "regions/tor1/state.json").exists()


@pytest.mark.parametrize("concurrency", [2000, 2500])
async def test_round_runs_every_country_at_the_manifest_concurrency(tmp_path, concurrency):
    path = make(tmp_path, run_settings={"concurrency": concurrency})
    assert campaign.load(path)["settings"]["concurrency"] == concurrency
    result, calls = await run_round(path, 0)
    assert result["settings"]["concurrency"] == concurrency
    assert len(calls) == 3 and all(c.concurrency == concurrency for c in calls)


async def test_no_deadline_a_long_country_simply_finishes(ephemeral):
    result, calls = await run_round(ephemeral, 0, seconds=30 * 3600.0)
    assert result["status"] == "completed" and len(calls) == 3
    assert [j["stop_reason"] for j in result["jobs"]] == ["unique_ratio"] * 3
    assert result["jobs"][-1]["duration_seconds"] == 30 * 3600
    assert verify_round(ephemeral, "tor1", 0)["complete"]


async def test_stop_interrupts_the_active_country_and_skips_the_rest(ephemeral):
    stop = asyncio.Event()

    async def stopped(country_stop):
        stop.set()
        await country_stop.wait()
        return measurement(0, status="stopped", stop_reason="stopped", completed=0, total_errors=0)

    result, calls = await run_round(ephemeral, 1, stop=stop, special={"GL": stopped})
    assert result["status"] == "stopped"
    assert [j["status"] for j in result["jobs"]] == [
        "completed",
        "interrupted",
        "not_started_deadline",
    ]
    report = verify_round(ephemeral, "tor1", 1)
    assert not report["complete"]
    assert report["issues"] == ["GL: interrupted", "PM: not started"]


async def test_stop_while_waiting_starts_no_country(ephemeral):
    stop = asyncio.Event()
    stop.set()
    data = campaign.load(ephemeral)
    clock = Clock(campaign.moment(data["rounds"][2]).timestamp() - 100)
    result, calls = await run_round(ephemeral, 2, clock=clock, stop=stop)
    assert calls == [] and result["status"] == "stopped"
    assert {j["status"] for j in result["jobs"]} == {"not_started_deadline"}


async def test_failure_is_recorded_and_raised(ephemeral):
    async def broken(country_stop):
        raise RuntimeError("provider library failure")

    with pytest.raises(RuntimeError):
        await run_round(ephemeral, 4, special={"GL": broken})
    saved = read_json(round_directory(ephemeral, "tor1", 4) / "round.json")
    assert saved["status"] == "failed"
    assert [j["status"] for j in saved["jobs"]] == ["completed", "failed", "not_started_deadline"]


async def test_early_start_waits_but_more_than_300_seconds_is_refused(ephemeral):
    data = campaign.load(ephemeral)
    scheduled = campaign.moment(data["rounds"][5]).timestamp()
    result, calls = await run_round(ephemeral, 5, clock=Clock(scheduled - 90))
    assert calls[0].at == scheduled and result["jobs"][0]["delay_seconds"] == 0
    with pytest.raises(ValueError, match="300 seconds"):
        await run_round(ephemeral, 6, clock=Clock(scheduled + 3600 - 301))
    assert not round_directory(ephemeral, "tor1", 6).exists()


async def test_replay_and_non_ephemeral_manifests_are_refused(ephemeral, tmp_path):
    await run_round(ephemeral, 7)
    with pytest.raises(ValueError, match="replay"):
        await run_round(ephemeral, 7)
    with pytest.raises(ValueError, match="version 3"):
        await run_round(make(tmp_path / "v1", execution_mode="parallel"), 0)
    with pytest.raises(ValueError, match="hourly index"):
        await execute_round(ephemeral, "tor1", 24, Proxy)
    with pytest.raises(ValueError, match="one round per node"):
        await execute(ephemeral, "tor1", Proxy)
    assert not (ephemeral.parent / "regions/tor1/state.json").exists()


async def test_verify_round_passes_without_ips(ephemeral):
    await run_round(ephemeral, 8, rows=3)
    report = verify_round(ephemeral, "tor1", 8)
    assert report["complete"] and report["issues"] == [] and report["rows"] == 9
    first = report["countries"][0]
    assert first == {
        "country": "CA",
        "status": "completed",
        "stop_reason": "unique_ratio",
        "requests": 4,
        "successes": 3,
        "errors": 1,
        "rate_limited": 0,
        "no_inventory": 0,
        "other_country": 0,
        "observations": 3,
        "unique_ips": 3,
        "bytes_up": 300,
        "bytes_down": 900,
        "duration_seconds": 30.0,
    }
    assert "203.0.113" not in json.dumps(report)


async def test_a_country_without_a_provider_list_completes_as_no_list(ephemeral):
    scheduled = campaign.moment(campaign.load(ephemeral)["rounds"][2]).timestamp()
    clock, calls = Clock(scheduled), []
    result = await execute_round(
        ephemeral,
        "tor1",
        2,
        lambda country: None if country == "GL" else Proxy(country),
        clock=clock,
        run_country=census(clock, calls),
    )
    assert [c.country for c in calls] == ["CA", "PM"]
    assert result["status"] == "completed"
    assert [(j["status"], j["stop_reason"]) for j in result["jobs"]][1] == ("no_list", "no_list")
    assert not (round_directory(ephemeral, "tor1", 2) / "gl").exists()
    report = verify_round(ephemeral, "tor1", 2)
    assert report["complete"] and report["issues"] == [] and report["rows"] == 4
    assert (report["countries"][1]["status"], report["countries"][1]["requests"]) == ("no_list", 0)


@pytest.mark.parametrize(
    "damage,issue",
    [
        ("tamper", "CA: failed verification (ValueError)"),
        ("meta", "CA: chunk without verified metadata"),
        ("partial", "CA: unfinished chunk"),
        ("summary", "CA: failed verification (FileNotFoundError)"),
    ],
)
async def test_verify_round_reports_damaged_country_data(ephemeral, damage, issue):
    await run_round(ephemeral, 9)
    folder = round_directory(ephemeral, "tor1", 9) / "ca"
    chunk = next(folder.glob("observations-*.jsonl.gz"))
    if damage == "tamper":
        chunk.write_bytes(chunk.read_bytes() + b"\0")
    elif damage == "meta":
        chunk.with_suffix(".meta.json").unlink()
    elif damage == "partial":
        (folder / "observations-000009.jsonl.gz.partial").write_bytes(b"x")
    else:
        (folder / "summary.json").unlink()
    report = verify_round(ephemeral, "tor1", 9)
    assert not report["complete"] and report["issues"] == [issue]


async def test_verify_round_reconciles_counters(ephemeral):
    await run_round(ephemeral, 10)
    path = round_directory(ephemeral, "tor1", 10) / "gl/summary.json"
    saved = read_json(path)
    saved["measurement"]["unique_ips"] = 5
    path.write_text(json.dumps(saved))
    assert verify_round(ephemeral, "tor1", 10)["issues"] == ["GL: measurements do not reconcile"]


async def test_verify_round_rejects_a_foreign_checkpoint(ephemeral, tmp_path):
    await run_round(ephemeral, 11)
    other = make(tmp_path / "other")
    target = round_directory(other, "tor1", 11)
    target.mkdir(parents=True)
    (target / "round.json").write_bytes(
        (round_directory(ephemeral, "tor1", 11) / "round.json").read_bytes()
    )
    with pytest.raises(ValueError, match="does not match"):
        verify_round(other, "tor1", 11)


async def test_verify_round_flags_a_crashed_round_overlaps_and_foreign_queues(ephemeral):
    await run_round(ephemeral, 12)
    path = round_directory(ephemeral, "tor1", 12) / "round.json"
    saved = read_json(path)
    crashed = deepcopy(saved)
    crashed["status"] = "running"
    crashed["jobs"][2]["status"] = "running"
    crashed["jobs"][1]["started_at"] = crashed["jobs"][0]["started_at"]
    path.write_text(json.dumps(crashed))
    issues = verify_round(ephemeral, "tor1", 12)["issues"]
    assert issues[:3] == [
        "round is running",
        "GL: overlaps the previous country",
        "GL: failed verification (ValueError)",
    ]
    assert issues[-1] == "PM: running"
    saved["jobs"].reverse()
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="queue differs"):
        verify_round(ephemeral, "tor1", 12)
    with pytest.raises(ValueError, match="Unknown campaign region"):
        verify_round(ephemeral, "mars1", 12)


def test_onboarding_doc_examples_use_the_strict_configuration():
    text = (ROOT / "docs/onboarding.html").read_text()
    assert "configs/shifter.toml" not in text
    assert text.count("configs/shifter-onboarding.toml") == 7


async def test_verify_round_reconciles_other_country_successes(ephemeral):
    from atlas.providers.grammar import answer_classes

    classes, shares = [], {"other_country": 3}
    scheduled = campaign.moment(campaign.load(ephemeral)["rounds"][13]).timestamp()
    clock = Clock(scheduled)

    async def run_country(config, sink, options):
        classes.append(config.answer_classes)
        for index in range(2):  # two in-country exits; three more exits were in other countries
            await options.observation_consumer(f"203.0.113.{index + 1}", options.wall_clock())
        clock.value += 30.0
        return measurement(
            2,
            completed=9,
            successes=5,
            total_errors=4,
            no_inventory=1,
            rate_limited={"429": 2, "proxy_connect": 1},
            **shares,
        )

    await execute_round(ephemeral, "tor1", 13, Proxy, clock=clock, run_country=run_country)
    assert classes == [answer_classes("shifter")] * 3  # rule 4 from the network's grammar
    report = verify_round(ephemeral, "tor1", 13)
    assert report["complete"] and report["issues"] == [] and report["rows"] == 6
    first = report["countries"][0]
    assert (first["rate_limited"], first["no_inventory"], first["other_country"]) == (3, 1, 3)
    assert first["observations"] == first["successes"] - first["other_country"] == 2
    shares["other_country"] = 2
    clock.value = campaign.moment(campaign.load(ephemeral)["rounds"][14]).timestamp()
    await execute_round(ephemeral, "tor1", 14, Proxy, clock=clock, run_country=run_country)
    assert "CA: measurements do not reconcile" in verify_round(ephemeral, "tor1", 14)["issues"]
