"""COV-3: the SSH fleet monitor's inventory, inspection, collection and command, offline.

`inspect` runs against a fake SSH subprocess; no server, key or network is touched.
"""

import asyncio
import json
import time

import pytest

from proxy_index.cli import main
from proxy_index.onboarding import fleet
from proxy_index.onboarding.files import atomic_json, read_json
from proxy_index.onboarding.fleet import classify, collect, inspect, load_inventory


def write_inventory(tmp_path, servers, **extra):
    path = tmp_path / "inventory.json"
    atomic_json(path, {"schema_version": 1, "servers": servers} | extra)
    return path


SERVER = {"host": "192.0.2.10", "root": "/srv/proxy-index"}


def test_inventory_expands_identity_files_and_keeps_valid_servers(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    servers = {
        "nyc3": SERVER | {"identity_file": "~/.ssh/fleet", "user": "runner"},
        "tor1": SERVER | {"host": "2001:db8::1"},
    }
    loaded = load_inventory(write_inventory(tmp_path, servers))
    assert loaded["nyc3"]["identity_file"] == str(tmp_path / ".ssh/fleet")
    assert loaded["tor1"] == SERVER | {"host": "2001:db8::1"}


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"schema_version": 2, "servers": {}}, "Invalid private server inventory"),
        ({"schema_version": 1, "servers": []}, "Invalid private server inventory"),
        ({"schema_version": 1, "servers": {"NYC3": SERVER}}, "Invalid region"),
        ({"schema_version": 1, "servers": {"nyc3": SERVER | {"user": "Root"}}}, "SSH user"),
        ({"schema_version": 1, "servers": {"nyc3": SERVER | {"root": "srv"}}}, "absolute"),
        ({"schema_version": 1, "servers": {"nyc3": SERVER | {"host": "nyc3.example"}}}, "IP"),
    ],
)
def test_inventory_refuses_invalid_entries(tmp_path, document, message):
    path = tmp_path / "inventory.json"
    atomic_json(path, document)
    with pytest.raises(ValueError, match=message):
        load_inventory(path)


@pytest.mark.parametrize(
    ("service", "health"),
    [({"ActiveState": "activating"}, "starting"), ({"ActiveState": "inactive"}, "failed")],
)
def test_an_assigned_server_without_state_is_starting_or_failed(service, health):
    data = {"region": "nyc3", "installed": True, "assignment": {"kind": "probe"}}
    assert classify(data | {"service": service}, time.time())["health"] == health
    assert classify(data, time.time())["health"] == "failed"  # no service reply at all


class FakeProcess:
    """An SSH child: `communicate` returns `stdout`, or never finishes when `hang`."""

    def __init__(self, stdout=b"", returncode=0, hang=False):
        self.stdout, self.hang, self.final = stdout, hang, returncode
        self.returncode = None
        self.killed = False

    async def communicate(self):
        if self.hang:
            await asyncio.Event().wait()
        self.returncode = self.final
        return self.stdout, b""

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


def fake_ssh(monkeypatch, process):
    commands = []

    async def spawn(*command, **kwargs):
        commands.append(command)
        assert kwargs == {"stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.PIPE}
        return process

    monkeypatch.setattr(fleet.asyncio, "create_subprocess_exec", spawn)
    return commands


async def test_inspect_returns_the_remote_json_with_its_region(monkeypatch):
    commands = fake_ssh(monkeypatch, FakeProcess(json.dumps({"installed": True}).encode()))
    server = SERVER | {"identity_file": "/keys/fleet", "user": "runner"}
    assert await inspect("nyc3", server) == {"installed": True, "region": "nyc3"}
    (command,) = commands
    assert command[0] == "ssh"
    assert command[-2] == "runner@192.0.2.10"
    assert "BatchMode=yes" in command and "IdentitiesOnly=yes" in command
    assert command[command.index("-i") + 1] == "/keys/fleet"
    assert command[-1].startswith("python3 -c ") and command[-1].endswith(" /srv/proxy-index")


async def test_inspect_without_identity_logs_in_as_root_and_reports_a_failed_exit(monkeypatch):
    commands = fake_ssh(monkeypatch, FakeProcess(returncode=255))
    result = await inspect("tor1", SERVER)
    assert result == {"region": "tor1", "error": "SSH inspection failed (exit 255)"}
    assert "-i" not in commands[0] and commands[0][-2] == "root@192.0.2.10"


async def test_inspect_rejects_invalid_json_without_killing_a_finished_child(monkeypatch):
    process = FakeProcess(b"not json")
    fake_ssh(monkeypatch, process)
    result = await inspect("tor1", SERVER)
    assert result["error"] == "SSH inspection timed out or returned invalid data"
    assert not process.killed


async def test_inspect_kills_a_child_past_its_time_limit(monkeypatch):
    process = FakeProcess(hang=True)
    fake_ssh(monkeypatch, process)

    async def expire(awaitable, seconds):
        assert seconds == 15
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(fleet.asyncio, "wait_for", expire)
    result = await inspect("tor1", SERVER)
    assert result == {
        "region": "tor1",
        "error": "SSH inspection timed out or returned invalid data",
    }
    assert process.killed


def remote_reply(now, busy_seconds):
    return {
        "server_time": now,
        "installed": True,
        "processes": [
            {
                "pid": 7,
                "role": "coordinator",
                "cpu_seconds": busy_seconds,
                "start_ticks": "1",
                "rss_mb": 10.0,
            }
        ],
        "core_ticks": {},
    }


def fake_inspect(monkeypatch, replies):
    async def reply(region, server):
        value = replies[region]
        return dict(value, processes=[dict(p) for p in value.get("processes", [])]) | {
            "region": region
        }

    monkeypatch.setattr(fleet, "inspect", reply)


async def test_collect_writes_the_snapshot_and_page_using_the_previous_interval(
    tmp_path, monkeypatch
):
    inventory = write_inventory(tmp_path, {"nyc3": SERVER, "tor1": SERVER})
    output = tmp_path / "monitor"
    now = time.time()
    previous = {"servers": [{"region": "nyc3"} | remote_reply(now - 10, 1.0)]}
    atomic_json(output / "snapshot.json", previous)
    fake_inspect(
        monkeypatch,
        {"nyc3": remote_reply(now, 6.0), "tor1": {"error": "SSH inspection failed (exit 255)"}},
    )
    result = await collect(inventory, output)
    nodes = {n["region"]: n for n in result["servers"]}
    assert nodes["nyc3"]["health"] == "idle"
    assert nodes["nyc3"]["processes"][0]["cpu_pct"] == pytest.approx(50.0, abs=0.5)
    assert nodes["tor1"]["health"] == "unreachable"
    assert read_json(output / "snapshot.json") == result
    page = (output / "index.html").read_text()
    assert page.startswith("<!doctype html>")
    assert "tor1: SSH inspection failed (exit 255)" in page


async def test_collect_without_a_previous_snapshot_has_no_cpu_interval(tmp_path, monkeypatch):
    inventory = write_inventory(tmp_path, {"nyc3": SERVER})
    fake_inspect(monkeypatch, {"nyc3": remote_reply(time.time(), 6.0)})
    result = await collect(inventory, tmp_path / "monitor")
    assert "cpu_pct" not in result["servers"][0]["processes"][0]


def test_fleet_status_command_prints_each_servers_health_once(tmp_path, monkeypatch, capsys):
    inventory = write_inventory(tmp_path, {"nyc3": SERVER, "tor1": SERVER})
    fake_inspect(monkeypatch, {"nyc3": {"installed": False}, "tor1": {"installed": True}})
    output = tmp_path / "monitor"
    command = ["onboarding", "fleet-status", "--inventory", str(inventory), "--output", str(output)]
    assert main(command) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["servers"] == {"nyc3": "not_deployed", "tor1": "idle"}
    assert printed["collected_at"] == read_json(output / "snapshot.json")["collected_at"]


def test_fleet_status_watch_collects_again_after_ten_seconds(tmp_path, monkeypatch, capsys):
    inventory = write_inventory(tmp_path, {"nyc3": SERVER})
    fake_inspect(monkeypatch, {"nyc3": {"installed": True}})
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            raise KeyboardInterrupt  # the operator's Ctrl-C ends the watch

    monkeypatch.setattr(fleet.asyncio, "sleep", sleep)
    command = ["onboarding", "fleet-status", "--inventory", str(inventory), "--watch"]
    assert main([*command, "--output", str(tmp_path / "monitor")]) == 130
    assert waits == [10, 10]
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["servers"] for line in lines] == [{"nyc3": "idle"}] * 2
