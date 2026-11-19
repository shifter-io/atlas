"""PULSE-STATE-2: a node's heartbeat carries its host's I/O pressure, memory and swap
(node_health), and the orchestrator keeps them on the node row. The case is the real one: the
stalled 14:00 droplet of 2026-11-15 showed I/O pressure 92 to 97% with 961 MB of RAM."""

from __future__ import annotations

from orchestrator_cases import bearer, prepare, query

from atlas.node_health import HostHealth
from atlas.orchestrator.nodes import issue_token

pytest_plugins = ["test_storage", "orchestrator_cases"]


def proc(tmp_path, io="some avg10=96.60 avg60=96.30 avg300=92.70 total=1", swapped=(10, 20)):
    root = tmp_path / "proc"
    (root / "pressure").mkdir(parents=True)
    (root / "pressure" / "io").write_text(f"{io}\nfull avg10=80.00 avg60=0 avg300=0 total=1\n")
    (root / "meminfo").write_text("MemTotal:  984064 kB\nMemAvailable:  611328 kB\n")
    (root / "vmstat").write_text(f"pgpgin 5\npswpin {swapped[0]}\npswpout {swapped[1]}\n")
    return root


def test_a_heartbeat_samples_io_pressure_memory_and_swap_since_the_last_one(tmp_path):
    root = proc(tmp_path)
    health = HostHealth(root)
    first = health.sample()
    assert first == {"io_pressure": 96.6, "mem_available_mb": 597, "swap_pages": None}
    (root / "vmstat").write_text("pswpin 25\npswpout 45\n")
    assert health.sample()["swap_pages"] == 40  # 15 in and 25 out since the previous beat


def test_a_host_without_pressure_files_reports_none(tmp_path):
    health = HostHealth(tmp_path / "nothing")
    assert health.sample() == {"io_pressure": None, "mem_available_mb": None, "swap_pages": None}
    root = proc(tmp_path, io="some total=1")  # a PSI line without avg10
    assert HostHealth(root).sample()["io_pressure"] is None


async def test_the_orchestrator_keeps_the_hosts_pressure_on_the_node_row(database, client):
    prepare(database)
    token = issue_token(database, provider="nodemaven", product="res", region="fra1")["token"]
    body = {"io_pressure": 96.6, "mem_available_mb": 597, "swap_pages": 40}
    assert (await client.post("/v1/heartbeat", json=body, headers=bearer(token))).status == 200
    columns = "io_pressure,mem_available_mb,swap_pages"
    assert query(database, f"SELECT {columns} FROM nodes") == [(96.6, 597, 40)]
    # An older node sends none: the columns go back to unknown, never stale.
    assert (await client.post("/v1/heartbeat", json={}, headers=bearer(token))).status == 200
    assert query(database, f"SELECT {columns} FROM nodes") == [(None, None, None)]
    bad = {"io_pressure": -1}
    assert (await client.post("/v1/heartbeat", json=bad, headers=bearer(token))).status == 400
