"""COV-3: the onboarding commands `run`, `status`, `export` and `probe`, offline.

The region scheduler and the probe are faked where they would send traffic; the provider
configuration, the manifest checks and the printed results are real.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from onboarding_round_cases import TOR1
from test_onboarding import ROOT, Proxy
from test_onboarding_sequential import ORDER, make

from proxy_index.cli import main
from proxy_index.onboarding import cli as onboarding_cli
from proxy_index.onboarding import manifest as campaign
from proxy_index.onboarding import probe
from proxy_index.onboarding.files import atomic_json, read_json
from proxy_index.onboarding.probe import ProbeOptions
from proxy_index.onboarding.scheduler import RegionOptions, new_state, status

CONFIG = ROOT / "configs/shifter.toml"
FAILED = "Command failed (ValueError). Check configuration and file paths.\n"


@pytest.fixture
def planned(tmp_path, monkeypatch):
    """A planned 24-hour campaign and fake provider credentials."""
    monkeypatch.setenv("SHIFTER_ACCOUNT", "customer-example")
    monkeypatch.setenv("SHIFTER_PASSWORD", "example-password")
    return make(tmp_path / "networks")


def fake_execute(monkeypatch, *, stop_it=False):
    calls = []

    async def execute(manifest, region, proxy_for, options, *, stop):
        calls.append(
            SimpleNamespace(
                manifest=manifest,
                region=region,
                countries={c: proxy_for(c).public()["requested_country"] for c in TOR1},
                options=options,
            )
        )
        if stop_it:
            stop.set()  # what SIGINT or SIGTERM does
        return {"region": region, "status": "closed"}

    monkeypatch.setattr(onboarding_cli, "execute", execute)
    return calls


def run_command(planned, *extra):
    base = ["onboarding", "run", "--manifest", str(planned), "--region", "tor1"]
    return main([*base, "--provider-config", str(CONFIG), *extra])


def test_run_arms_the_region_with_the_operator_settings(planned, monkeypatch, capsys):
    calls = fake_execute(monkeypatch)
    assert run_command(planned, "--concurrency", "50", "--timeout", "4", "--resume") == 0
    assert json.loads(capsys.readouterr().out) == {"region": "tor1", "status": "closed"}
    (call,) = calls
    assert (call.manifest, call.region) == (planned, "tor1")
    assert call.countries == {country: country.lower() for country in TOR1}
    assert call.options == RegionOptions(
        concurrency=50, processes=None, request_timeout=4.0, resume=True
    )


def test_run_exits_130_when_a_signal_stopped_the_region(planned, monkeypatch, capsys):
    fake_execute(monkeypatch, stop_it=True)
    assert run_command(planned) == 130
    assert json.loads(capsys.readouterr().out)["status"] == "closed"


def test_run_refuses_an_unknown_region_before_any_work(planned, monkeypatch, capsys):
    calls = fake_execute(monkeypatch)
    base = ["onboarding", "run", "--manifest", str(planned), "--region", "mars1"]
    assert main([*base, "--provider-config", str(CONFIG)]) == 2
    assert capsys.readouterr().err == FAILED
    assert calls == []


def test_proxies_refuse_a_provider_targeting_another_country(planned, monkeypatch):
    def load_provider(config, *, parameters, credentials_file):
        return Proxy("us")  # every login would exit in the US, not the campaign's country

    monkeypatch.setattr(onboarding_cli, "load_provider", load_provider)
    args = SimpleNamespace(region="tor1", provider_config=CONFIG, credentials_file=None)
    with pytest.raises(ValueError, match="targeting does not match the campaign"):
        onboarding_cli._proxies(args, campaign.load(planned))
    args.region = "mars1"
    with pytest.raises(ValueError, match="Unknown campaign region"):
        onboarding_cli._proxies(args, campaign.load(planned))


async def test_signals_work_on_a_loop_without_signal_handlers(monkeypatch):
    loop = type(asyncio.get_running_loop())
    removed = []

    def unsupported(self, sig, callback):
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_signal_handler", unsupported)
    monkeypatch.setattr(loop, "remove_signal_handler", lambda self, sig: removed.append(sig))
    stop = asyncio.Event()
    with onboarding_cli._signals(stop):
        entered = True
    assert entered and removed == [] and not stop.is_set()


def test_status_prints_the_regions_progress(planned, capsys):
    manifest = campaign.load(planned)
    state = new_state(manifest, "tor1", manifest["settings"])
    atomic_json(planned.parent / "regions/tor1/state.json", state)
    assert main(["onboarding", "status", "--manifest", str(planned), "--region", "tor1"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == json.loads(json.dumps(status(state)))
    assert len(printed["rounds"]) == 24


def test_status_refuses_an_unknown_region(planned, capsys):
    assert main(["onboarding", "status", "--manifest", str(planned), "--region", "mars1"]) == 2
    assert capsys.readouterr().err == FAILED


def test_export_prints_an_incomplete_export_of_a_campaign_without_regions(planned, capsys):
    assert main(["onboarding", "export", "--manifest", str(planned)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (printed["complete"], printed["rows"], printed["chunks"]) == (False, 0, 0)
    assert sorted(printed["issues"]) == sorted(f"{r}: missing regional checkpoint" for r in ORDER)
    assert read_json(planned.parent / "export/manifest.json") == printed


def fake_probe(monkeypatch, *, stop_it=False):
    calls = []

    async def execute_probe(directory, proxy, region, country, options, *, stop):
        calls.append((directory, proxy.public()["requested_country"], region, country, options))
        if stop_it:
            stop.set()
        return {"kind": "probe", "status": "completed"}

    monkeypatch.setattr(probe, "execute_probe", execute_probe)
    return calls


def probe_command(tmp_path, *extra):
    base = ["onboarding", "probe", "--provider-config", str(CONFIG), "--country", "CA"]
    return main([*base, "--region", "tor1", "--output", str(tmp_path / "probe"), *extra])


def test_probe_passes_its_options_and_prints_the_result(planned, tmp_path, monkeypatch, capsys):
    calls = fake_probe(monkeypatch)
    extra = ["--concurrency", "20", "--processes", "2", "--timeout", "3"]
    start = "2099-01-01T00:00:00Z"
    assert probe_command(tmp_path, *extra, "--start", start, "--max-seconds", "60") == 0
    assert json.loads(capsys.readouterr().out) == {"kind": "probe", "status": "completed"}
    options = ProbeOptions(
        concurrency=20, processes=2, request_timeout=3.0, start=start, max_seconds=60.0
    )
    assert calls == [(tmp_path / "probe", "ca", "tor1", "CA", options)]


def test_probe_exits_130_when_a_signal_stopped_it(planned, tmp_path, monkeypatch, capsys):
    calls = fake_probe(monkeypatch, stop_it=True)
    assert probe_command(tmp_path) == 130
    assert calls[0][4] == ProbeOptions(concurrency=1000, processes=0, request_timeout=10.0)
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
