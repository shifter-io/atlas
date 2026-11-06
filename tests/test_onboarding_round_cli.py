"""The onboarding plan, round and verify-round commands: offline, with a fake census runner."""

import functools
import json
import os
import signal
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from onboarding_round_cases import TOR1, census, make, measurement
from test_onboarding import ROOT, Clock

from proxy_index.cli import main
from proxy_index.onboarding import cli as onboarding_cli
from proxy_index.onboarding import manifest as campaign
from proxy_index.onboarding.files import read_json
from proxy_index.onboarding.rounds import execute_round, round_directory
from proxy_index.providers import NoCountryList
from proxy_index.runner import RunOptions


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
