"""PULSE-1b: `fleet.json` validation and what an armed pulse node is given and runs (fake
cloud and fake SSH; nothing leaves the machine)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import self_signed
from pulse_fleet_cases import NODE, Remote, bundle, make_root, reconcile, world

from atlas.campaign import pulse_fleet
from atlas.campaign.pulse_fleet import FleetError


@pytest.mark.parametrize(
    "change, what",
    [
        ({"account": "someone"}, "account"),
        ({"size": "s-2vcpu-2gb"}, "size"),
        ({"regions": []}, "none"),
        ({"networks": {}}, "none"),
        ({"regions": ["LON1"], "image_by_region": {"LON1": "1"}}, "region"),
        ({"image_by_region": {}}, "image_by_region"),
        (
            {"image_by_region": {"lon1": "atlas-camp-worker-1.0-0123456789ab-20261114"}},
            r"image_by_region lon1: a snapshot id \(digits\), not a name",
        ),
        ({"image_by_region": {"lon1": "1", "fra1": "12a"}}, "image_by_region fra1: a snapshot id"),
        ({"orchestrator_url": "ftp://example"}, "orchestrator_url"),
        ({"orchestrator_url": "http://x/ y"}, "orchestrator_url"),
        ({"orchestrator_url": "http://203.0.113.9:8080"}, "plain http:// is refused"),
        ({"ca_file": "missing.crt"}, "needs an https://"),
        ({"orchestrator_url": "https://203.0.113.9:8443", "ca_file": "x.crt"}, "cannot be read"),
        ({"wheel": "atlas.tar.gz"}, "wheel"),
        ({"networks": {"Bad Name": {}}}, "network name"),
        (
            {
                "networks": {
                    "shifter": {
                        "product": "res idential",
                        "provider_config": "p",
                        "credentials": "c",
                    }
                }
            },
            "shifter",
        ),
    ],
)
def test_fleet_json_is_validated(tmp_path, change, what):
    root = make_root(tmp_path)
    raw = json.loads((root / "fleet.json").read_text())
    (root / "fleet.json").write_text(json.dumps({**raw, **change}))
    with pytest.raises(FleetError, match=what):
        pulse_fleet.load_config(root)


def test_a_snapshot_id_may_be_a_json_number(tmp_path):
    root = make_root(tmp_path, image_by_region={"lon1": 210987})
    assert pulse_fleet.load_config(root).image_by_region == {"lon1": "210987"}


def test_the_config_names_one_node_per_network_and_region(tmp_path):
    root = make_root(tmp_path, networks=("shifter",), regions=("lon1",))
    config = pulse_fleet.load_config(root)
    assert config.wanted() == {NODE: ("shifter", "lon1")} and config.size == "s-1vcpu-1gb-amd"
    assert config.networks["shifter"].provider == "shifter"


async def test_a_dbip_base_url_left_in_fleet_json_is_ignored(tmp_path):
    """PULSE-NO-DBIP-1: a pulse droplet never runs the DB-IP updater, whatever fleet.json says."""
    root = make_root(tmp_path, dbip_base_url="https://cdn.invalid/dbip/")
    remote = Remote()
    await reconcile(world(), root, remote)
    ((_, payload, _),) = remote.pushed
    node = json.loads(bundle(payload)["pulse/node.json"][1])
    assert "dbip_base_url" not in node


def arm_script(tmp_path, node, databases=(), units=None):
    """Runs NODE_SCRIPT after its wheel install on a fake worker root; returns the root, the
    systemd units left in place and the systemctl calls. `units` are already installed."""
    root = tmp_path / "worker"
    (root / "pulse").mkdir(parents=True)
    (root / "db-ip").mkdir()
    for name in databases:
        (root / "db-ip" / name).write_bytes(name.encode())
    (root / "pulse" / "node.json").write_text(json.dumps(node))
    systemd, steps = tmp_path / "systemd", []
    systemd.mkdir()
    for name in units or ():
        (systemd / name).write_text("old")
    shim = SimpleNamespace(
        Path=lambda p: systemd / Path(p).name if p.startswith("/etc/systemd/") else Path(p)
    )
    run = SimpleNamespace(run=lambda command, check: steps.append((command[1:], check)))
    tail = pulse_fleet.NODE_SCRIPT.split(pulse_fleet.INSTALL_WHEEL)[1]
    exec(tail, {"json": json, "pathlib": shim, "subprocess": run, "root": root, "secrets": root})
    return root, {p.name: p.read_text() for p in systemd.iterdir()}, steps


async def test_with_a_ca_file_the_node_gets_the_managers_certificate(tmp_path):
    root = make_root(tmp_path, orchestrator_url="https://203.0.113.9:8443", ca_file="m.crt")
    cert, _ = self_signed(root, "m", "203.0.113.9")
    remote = Remote()
    await reconcile(world(), root, remote)
    ((_, payload, _),) = remote.pushed
    files = bundle(payload)
    assert files["pulse/orchestrator-ca.crt"] == (0o600, cert.read_bytes())
    assert json.loads(files["pulse/node.json"][1])["ca_file"] is True


def test_arming_points_the_node_at_its_ca_file(tmp_path):
    root, units, _ = arm_script(tmp_path, NODE_JSON | {"ca_file": True})
    assert f"--ca-file {root}/pulse/orchestrator-ca.crt" in units["atlas-pulse-node.service"]
    _, units, _ = arm_script(tmp_path / "plain", NODE_JSON)
    assert "--ca-file" not in units["atlas-pulse-node.service"]


NODE_JSON = {"orchestrator": "http://o", "provider": "p", "product": "q", "region": "lon1"}
UPDATER = ("atlas-dbip-update.service", "atlas-dbip-update.timer")


def test_arming_names_the_image_database_and_never_installs_the_updater(tmp_path):
    """PULSE-NO-DBIP-1: the updater rewrote the 1.34 GB file within the hour on 1 GB droplets
    that read it for every answer; a node told a base URL by an older manager gets none."""
    base = "https://cdn.invalid/dbip/"
    root, units, steps = arm_script(
        tmp_path, NODE_JSON | {"dbip_base_url": base}, ["dbip-full-2026-11-02.mmdb"]
    )
    db = root / "db-ip"
    assert sorted(p.name for p in db.iterdir()) == ["dbip-full.mmdb"]
    assert (db / "dbip-full.mmdb").read_bytes() == b"dbip-full-2026-11-02.mmdb"  # renamed
    assert f"--mmdb {db}/dbip-full.mmdb" in units["atlas-pulse-node.service"]
    assert set(units) == {"atlas-pulse-node.service"}  # no updater service or timer
    assert steps[:2] == [
        (["disable", "--now", "atlas-dbip-update.timer"], False),
        (["stop", "atlas-dbip-update.service"], False),  # nor an update already running
    ]
    assert not any("enable" in s and "atlas-dbip-update.timer" in s for s, _ in steps)


def test_arming_keeps_an_updated_database_and_removes_a_timer_no_longer_wanted(tmp_path):
    names = ["dbip-full-2026-09-14.mmdb", "dbip-full.mmdb"]  # the update's file and an old one
    root, units, steps = arm_script(tmp_path, NODE_JSON, names, units=UPDATER)
    assert (root / "db-ip" / "dbip-full.mmdb").read_bytes() == b"dbip-full.mmdb"
    assert set(units) == {"atlas-pulse-node.service"}  # the earlier arming's timer is gone
    assert steps[0] == (["disable", "--now", "atlas-dbip-update.timer"], False)
    assert [s for s, _ in steps[2:]] == [
        ["daemon-reload"],
        ["enable", "atlas-pulse-node"],
        ["restart", "atlas-pulse-node"],
    ]
    _, units, _ = arm_script(tmp_path / "bare", NODE_JSON)
    assert "--mmdb" not in units["atlas-pulse-node.service"]
