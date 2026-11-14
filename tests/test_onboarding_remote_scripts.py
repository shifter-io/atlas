"""The remote campaign and probe launchers send the same commands in the same order."""

import importlib.util
import json
import shlex
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from atlas.onboarding import manifest as campaign

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2027, 1, 1, 22, tzinfo=UTC)
NOW = START - timedelta(hours=1)
SERVER_ROOT = "/srv/atlas"
OPTIONS = ["-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-i", "/keys/tor1"]


class Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def script(name, monkeypatch):
    """Load a launcher with a frozen clock and a recorder in place of ssh and scp."""
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    monkeypatch.setattr(module, "datetime", Frozen)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    return module, calls


@pytest.fixture
def inventory(tmp_path):
    path = tmp_path / "inventory.json"
    server = {"root": SERVER_ROOT, "host": "tor1.example.test", "identity_file": "/keys/tor1"}
    path.write_text(json.dumps({"servers": {"tor1": server}}))
    return path


@pytest.mark.parametrize("calibration", [False, True])
def test_campaign_distributes_checks_starts_and_records_in_order(
    tmp_path, monkeypatch, capsys, inventory, calibration
):
    path = campaign.create(
        ROOT / "configs/shifter.toml",
        "residential",
        campaign.timestamp(START),
        tmp_path / "manifests",
        now=NOW,
    )
    data = campaign.load(path)
    module, calls = script("onboarding-remote-campaign.py", monkeypatch)
    argv = ["x", "--inventory", str(inventory), "--manifest", str(path), "--region", "tor1"]
    monkeypatch.setattr(sys, "argv", argv + ["--calibration"] * calibration)
    module.main()

    relative = f"onboarding-networks/shifter/residential/{data['campaign_id']}"
    directory = f"{SERVER_ROOT}/{relative}"
    unit = f"atlas-onboarding-{data['campaign_id']}-tor1"
    options = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", *OPTIONS[2:]]
    target = "root@tor1.example.test"
    commands = [args[0] for args, _ in calls]
    assert all(kw == {"check": True} for _, kw in calls)
    assert commands[0] == ["ssh", *options, target, f"umask 077\nmkdir -p {directory}"]
    assert commands[1] == ["scp", *options, str(path), f"{target}:{directory}/campaign.json"]
    tool = f"{SERVER_ROOT}/tools/onboarding-calibration.py"
    if calibration:
        driver = str(ROOT / "scripts/onboarding-calibration.py")
        assert commands[2] == ["scp", *options, driver, f"{target}:{tool}"]
    check, start, save = (shlex.split(c[-1]) for c in commands[2 + calibration :])
    assert [c[:-1] for c in commands[2 + calibration :]] == [["ssh", *options, target]] * 3
    assert check[:2] == [f"{SERVER_ROOT}/venv/bin/python", "-c"]
    assert "A regional run is already active" in check[2]
    assert check[3:] == [SERVER_ROOT, f"{directory}/campaign.json"]
    base = [f"{SERVER_ROOT}/venv/bin/atlas", "onboarding", "run"]
    if calibration:
        base = [f"{SERVER_ROOT}/venv/bin/python", tool]
    assert start == [
        "systemd-run",
        f"--unit={unit}",
        "--property=LimitNOFILE=65536",
        "--property=UMask=0077",
        "--property=Restart=no",
        f"--property=WorkingDirectory={SERVER_ROOT}",
        "--property=RemainAfterExit=yes",
        "--property=RuntimeMaxSec=90600",
        *base,
        "--manifest",
        f"{directory}/campaign.json",
        "--region",
        "tor1",
        "--provider-config",
        f"{SERVER_ROOT}/configs/provider.toml",
        "--credentials-file",
        f"{SERVER_ROOT}/.secrets/credentials.toml",
    ]
    assert save[:2] == [f"{SERVER_ROOT}/venv/bin/python", "-c"]
    assert save[3] == f"{SERVER_ROOT}/active-run.json"
    assert json.loads(save[4]) == {
        "kind": "calibration" if calibration else "campaign",
        "region": "tor1",
        "unit": unit + ".service",
        "state_path": f"{relative}/regions/tor1/state.json",
    }
    assert json.loads(capsys.readouterr().out) == {
        "region": "tor1",
        "unit": unit,
        "remote_directory": directory,
        "manifest": str(path),
        "calibration": calibration,
    }


def test_campaign_refuses_an_absent_region_or_a_start_already_due(tmp_path, monkeypatch, inventory):
    path = campaign.create(
        ROOT / "configs/shifter.toml",
        "residential",
        campaign.timestamp(START),
        tmp_path / "manifests",
        now=NOW,
    )
    module, calls = script("onboarding-remote-campaign.py", monkeypatch)
    argv = ["x", "--inventory", str(inventory), "--manifest", str(path), "--region"]
    monkeypatch.setattr(sys, "argv", argv + ["nowhere"])
    with pytest.raises(ValueError, match="Region absent from manifest"):
        module.main()
    monkeypatch.setattr(sys, "argv", argv + ["tor1"])
    monkeypatch.setattr(module, "datetime", type("Late", (Frozen,), {"now": lambda *a: START}))
    with pytest.raises(ValueError, match="after its scheduled start"):
        module.main()
    assert calls == []


@pytest.mark.parametrize("start", [None, "2027-01-01T22:00:00+00:00"])
def test_probe_starts_the_service_and_writes_the_pointer_in_one_session(
    monkeypatch, capsys, inventory, start
):
    module, calls = script("onboarding-remote-probe.py", monkeypatch)
    timing = ["--start", start] if start else ["--now"]
    argv = ["x", "--inventory", str(inventory), "--region", "tor1", "--country", "ca"]
    monkeypatch.setattr(sys, "argv", argv + ["--network", "shifter/residential", *timing])
    module.main()

    name = "ca-20270101t220000z-c1000" if start else "ca-20270101t210000z-c1000"
    unit = f"atlas-onboarding-{name}"
    output = f"{SERVER_ROOT}/onboarding-networks/shifter/residential/probes/{name}"
    [(args, kw)] = calls
    assert args[0] == ["ssh", *OPTIONS, "root@tor1.example.test", "sh -s"]
    assert kw.keys() == {"input", "text", "check"} and kw["text"] and kw["check"]
    prefix = "set -eu\numask 077\n"
    assert kw["input"].startswith(prefix) and kw["input"].endswith("\n")
    launch, pointer = kw["input"].removeprefix(prefix).split("\n", 1)
    assert shlex.split(launch) == [
        "systemd-run",
        f"--unit={unit}",
        "--property=LimitNOFILE=65536",
        "--property=UMask=0077",
        "--property=Restart=no",
        "--property=RuntimeMaxSec=90000",
        f"--property=WorkingDirectory={SERVER_ROOT}",
        "--property=RemainAfterExit=yes",
        f"{SERVER_ROOT}/venv/bin/atlas",
        "onboarding",
        "probe",
        "--provider-config",
        f"{SERVER_ROOT}/configs/provider.toml",
        "--credentials-file",
        f"{SERVER_ROOT}/.secrets/credentials.toml",
        "--region",
        "tor1",
        "--country",
        "ca",
        "--output",
        output,
        "--concurrency",
        "1000",
        "--processes",
        "0",
        "--timeout",
        "10",
        *(["--start", start] if start else []),
    ]
    marker = shlex.split(pointer)
    assert marker[:2] == ["python3", "-c"] and marker[3] == SERVER_ROOT
    assert json.loads(marker[4]) == {
        "kind": "probe",
        "region": "tor1",
        "unit": unit + ".service",
        "state_path": f"onboarding-networks/shifter/residential/probes/{name}/state.json",
    }
    assert json.loads(capsys.readouterr().out) == {
        "region": "tor1",
        "unit": unit,
        "remote_output": output,
        "scheduled_at": start,
        "concurrency": 1000,
        "processes": 0,
        "timeout_seconds": 10.0,
    }


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [("--network", "Shifter", "provider/product"), ("--country", "CA", "ISO alpha-2")],
)
def test_probe_refuses_a_malformed_network_or_country(
    monkeypatch, inventory, option, value, message
):
    module, calls = script("onboarding-remote-probe.py", monkeypatch)
    values = {"--country": "ca", "--network": "shifter/residential", option: value}
    argv = ["x", "--inventory", str(inventory), "--region", "tor1", "--now"]
    monkeypatch.setattr(sys, "argv", argv + [a for kv in values.items() for a in kv])
    with pytest.raises(ValueError, match=message):
        module.main()
    assert calls == []
