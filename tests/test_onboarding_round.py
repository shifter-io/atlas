"""Ephemeral one-round execution: offline, with a fake census runner and clock."""

import asyncio
import functools
import json
import os
import signal
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_onboarding import ROOT, START, Clock, Proxy

from proxy_index.cli import main
from proxy_index.onboarding import cli as onboarding_cli
from proxy_index.onboarding import manifest as campaign
from proxy_index.onboarding.files import read_json
from proxy_index.onboarding.rounds import execute_round, round_directory, verify_round
from proxy_index.onboarding.scheduler import execute
from proxy_index.providers import NoCountryList
from proxy_index.runner import RunOptions

TOR1 = ["CA", "GL", "PM"]


def make(tmp_path, **kwargs):
    kwargs.setdefault("now", START - timedelta(hours=1))
    return campaign.create(
        ROOT / "configs/shifter.toml",
        "residential",
        campaign.timestamp(kwargs.pop("start", START)),
        tmp_path / "networks",
        execution_mode=kwargs.pop("execution_mode", "ephemeral"),
        **kwargs,
    )


@pytest.fixture
def ephemeral(tmp_path):
    return make(tmp_path)


def measurement(rows, **extra):
    values = (
        dict(
            status="completed",
            stop_reason="unique_ratio",
            completed=rows + 1,
            successes=rows,
            total_errors=1,
            unique_ips=rows,
            cancelled=0,
            bytes_up=100 * rows,
            bytes_down=300 * rows,
        )
        | extra
    )
    return SimpleNamespace(**values, to_dict=lambda: dict(values))


def census(clock, calls, *, rows=2, seconds=30.0, special=None):
    """A fake runner that writes ``rows`` observations and takes ``seconds`` per country."""

    async def run_country(config, sink, options):
        country = config.proxy.public()["requested_country"].upper()
        calls.append(
            SimpleNamespace(
                country=country,
                at=clock.now(),
                deadline=options.deadline,
                concurrency=config.concurrency,
                requests=config.requests,
            )
        )
        if special and country in special:
            return await special[country](options.stop)
        for index in range(rows):
            await options.observation_consumer(f"203.0.113.{index + 1}", options.wall_clock())
        clock.value += seconds
        return measurement(rows)

    return run_country


async def run_round(manifest, index=3, *, clock=None, calls=None, **kwargs):
    scheduled = campaign.moment(campaign.load(manifest)["rounds"][index]).timestamp()
    clock = clock or Clock(scheduled)
    calls = [] if calls is None else calls
    stop = kwargs.pop("stop", None)
    result = await execute_round(
        manifest,
        "tor1",
        index,
        Proxy,
        clock=clock,
        stop=stop,
        run_country=census(clock, calls, **kwargs),
    )
    return result, calls


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


def test_plan_cli_writes_an_ephemeral_manifest(tmp_path, capsys):
    start = "2099-01-01T00:00:00Z"
    base = [
        "onboarding",
        "plan",
        "--provider-config",
        str(ROOT / "configs/shifter.toml"),
        "--product",
        "residential",
        "--start",
        start,
    ]
    assert (
        main(
            [
                *base,
                "--output",
                str(tmp_path / "a"),
                "--execution-mode",
                "ephemeral",
                "--grace-hours",
                "2",
            ]
        )
        == 0
    )
    data = campaign.load(campaign.Path(capsys.readouterr().out.strip()))
    assert (data["schema_version"], data["grace_hours"]) == (3, 2)
    assert data["settings"]["concurrency"] == 2000
    assert main([*base, "--output", str(tmp_path / "b")]) == 0
    legacy = campaign.load(campaign.Path(capsys.readouterr().out.strip()))
    assert legacy["schema_version"] == 1 and legacy["settings"]["concurrency"] == 1000


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A current-hour campaign plus fake credentials; the census itself stays fake."""
    monkeypatch.setenv("SHIFTER_ACCOUNT", "customer-example")
    monkeypatch.setenv("SHIFTER_PASSWORD", "example-password")
    now = datetime.now(UTC)
    hour = now.replace(minute=0, second=0, microsecond=0)
    path = make(tmp_path, start=hour, now=now, allow_current_hour=True)
    calls = []

    class RealClock(Clock):
        def now(self):
            return datetime.now(UTC).timestamp()

    def patch(**kwargs):
        clock = RealClock(0)
        monkeypatch.setattr(
            onboarding_cli,
            "execute_round",
            functools.partial(
                execute_round, clock=clock, run_country=census(clock, calls, **kwargs)
            ),
        )

    args = ["--manifest", str(path), "--region", "tor1", "--round", "0"]
    return SimpleNamespace(
        path=path,
        calls=calls,
        patch=patch,
        args=args,
        config=["--provider-config", str(ROOT / "configs/shifter.toml")],
    )


def test_round_cli_exit_codes_and_verify(live, capsys):
    live.patch(seconds=0.0)
    assert main(["onboarding", "round", *live.args, *live.config]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert [c.country for c in live.calls] == TOR1
    assert main(["onboarding", "verify-round", *live.args]) == 0
    assert json.loads(capsys.readouterr().out)["complete"]
    assert main(["onboarding", "round", *live.args, *live.config]) == 1  # replay
    assert capsys.readouterr().err == (
        "Round refused: This round already has a checkpoint; a replay is refused\n"
    )
    (round_directory(live.path, "tor1", 0) / "pm/summary.json").unlink()
    assert main(["onboarding", "verify-round", *live.args]) == 3
    assert main(["onboarding", "verify-round", *live.args[:3], "lon1", "--round", "0"]) == 1


def test_round_cli_signal_stops_with_130(live, capsys):
    async def terminate(country_stop):
        os.kill(os.getpid(), signal.SIGTERM)
        await country_stop.wait()
        return measurement(0, status="stopped", stop_reason="stopped")

    live.patch(seconds=0.0, special={"CA": terminate})
    assert main(["onboarding", "round", *live.args, *live.config]) == 130
    saved = read_json(round_directory(live.path, "tor1", 0) / "round.json")
    assert [j["status"] for j in saved["jobs"]] == [
        "interrupted",
        "not_started_deadline",
        "not_started_deadline",
    ]


def test_round_cli_refuses_a_changed_provider_config_before_traffic(live, tmp_path):
    changed = tmp_path / "changed.toml"
    changed.write_text((ROOT / "configs/shifter.toml").read_text() + "\n# edited\n")
    live.patch()
    assert main(["onboarding", "round", *live.args, "--provider-config", str(changed)]) == 1
    assert live.calls == []
    assert not round_directory(live.path, "tor1", 0).exists()


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


def test_round_cli_prints_each_refusal_and_only_the_type_of_other_errors(live, tmp_path, capsys):
    live.patch(seconds=0.0)
    hour = campaign.moment(campaign.load(live.path)["starts_at"])
    later = make(tmp_path / "later", start=hour + timedelta(hours=1), now=datetime.now(UTC))
    v1 = make(tmp_path / "v1", execution_mode="parallel")
    folder = round_directory(live.path, "tor1", 0)

    def refused(command, manifest, region="tor1", index=0):
        args = ["--manifest", str(manifest), "--region", region, "--round", str(index)]
        extra = live.config if command == "round" else []
        assert main(["onboarding", command, *args, *extra]) == 1
        return capsys.readouterr().err

    assert refused("round", later, index=1) == (
        "Round refused: A round may be armed at most 300 seconds before it is scheduled\n"
    )
    assert refused("verify-round", v1) == (
        "Round refused: One-round execution requires an ephemeral (version 3) manifest\n"
    )
    assert refused("verify-round", live.path, "xx1") == "Round refused: Unknown campaign region\n"
    assert refused("verify-round", live.path, index=24) == (
        "Round refused: Round must be an hourly index from 0 to 23\n"
    )
    # Other errors can quote paths or values, so only their type is printed.
    assert refused("verify-round", live.path) == "Round command failed (FileNotFoundError).\n"
    assert main(["onboarding", "round", *live.args, *live.config]) == 0
    saved = read_json(folder / "round.json")
    (folder / "round.json").write_text(json.dumps(saved | {"campaign_id": "other"}))
    assert refused("verify-round", live.path) == (
        "Round refused: Round checkpoint does not match this campaign, region and round\n"
    )
    (folder / "round.json").write_text(json.dumps(saved | {"jobs": saved["jobs"][::-1]}))
    assert refused("verify-round", live.path) == (
        "Round refused: Round checkpoint country queue differs from the campaign\n"
    )
    changed = tmp_path / "changed.toml"
    changed.write_text((ROOT / "configs/shifter.toml").read_text() + "\n# edited\n")
    assert main(["onboarding", "round", *live.args, "--provider-config", str(changed)]) == 1
    assert capsys.readouterr().err == "Round command failed (ValueError).\n"


def test_round_cli_skips_a_country_without_a_provider_list(live, monkeypatch, capsys):
    real = onboarding_cli.load_provider

    def load_provider(path, *, parameters, **kwargs):
        if parameters["country"] == "gl":
            raise NoCountryList("no shifter list for GL")
        return real(path, parameters=parameters, **kwargs)

    monkeypatch.setattr(onboarding_cli, "load_provider", load_provider)
    args = SimpleNamespace(
        region="tor1", provider_config=ROOT / "configs/shifter.toml", credentials_file=None
    )
    with pytest.raises(NoCountryList):  # every other command still refuses the country
        onboarding_cli._proxies(args, campaign.load(live.path))
    live.patch(seconds=0.0)
    assert main(["onboarding", "round", *live.args, *live.config]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert [c.country for c in live.calls] == ["CA", "PM"]
    assert main(["onboarding", "verify-round", *live.args]) == 0
    assert json.loads(capsys.readouterr().out)["countries"][1]["status"] == "no_list"


def test_onboarding_doc_examples_use_the_strict_configuration():
    text = (ROOT / "docs/onboarding.html").read_text()
    assert "configs/shifter.toml" not in text
    assert text.count("configs/shifter-onboarding.toml") == 7


async def test_verify_round_reconciles_other_country_successes(ephemeral):
    from proxy_index.providers.grammar import answer_classes

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


async def test_round_enricher_joins_the_scheduler_options(monkeypatch):
    calls = []

    async def fake_run(config, sink, options):
        calls.append((config, sink, options))
        return "summary"

    monkeypatch.setattr(onboarding_cli, "run", fake_run)
    options = RunOptions(deadline=5.0, aggregate_ips=False, progress_interval=5)
    run_country = functools.partial(onboarding_cli._enriched, enricher="geo")
    assert await run_country("config", "sink", options) == "summary"
    assert calls == [("config", "sink", replace(options, enricher="geo"))]


def test_round_cli_mmdb_gives_the_census_an_enricher(live, monkeypatch):
    opened, given = [], []

    class Enricher:
        def __init__(self, path):
            opened.append(str(path))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            opened.append("closed")

    async def fake_round(*args, stop, run_country):
        given.append(run_country.keywords["enricher"])
        return {"campaign_id": "c", "region": "tor1", "round": 0, "status": "completed"}

    monkeypatch.setattr("proxy_index.enrichment.LocalEnricher", Enricher)
    monkeypatch.setattr(onboarding_cli, "execute_round", fake_round)
    assert main(["onboarding", "round", *live.args, *live.config, "--mmdb", "db.mmdb"]) == 0
    assert opened == ["db.mmdb", "closed"] and isinstance(given[0], Enricher)
