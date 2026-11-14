import gzip
import json
import shutil
import subprocess
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from test_onboarding import START
from test_onboarding_sequential import ORDER, make

from atlas.onboarding import backup
from atlas.onboarding import manifest as campaign
from atlas.onboarding.backup import collect_region, tree_inventory, verify_region
from atlas.onboarding.files import atomic_json, digest, read_json
from atlas.onboarding.health import audit
from atlas.onboarding.scheduler import job_directory, new_state


def snapshot(m, now):
    servers = []
    for region in ORDER:
        s = new_state(m, region, m["settings"])
        s["updated_at"] = campaign.timestamp(now)
        servers.append(
            {"region": region, "state": s, "service": {"MainPID": "123"}, "server_time": now}
        )
    return {"servers": servers}


def test_audit_armed_days_and_overlap(tmp_path):
    m = campaign.load(make(tmp_path, execution_mode="sequential", region_order=ORDER))
    now = START.timestamp() - 60
    snap = snapshot(m, now)
    assert not audit(m, snap, now)["alerts"]
    snap["servers"][1]["state"]["active"] = {"country": "ca"}
    snap["servers"][0]["state"]["active"] = {"country": "us"}
    issues = {a["issue"] for a in audit(m, snap, now)["alerts"]}
    assert {"overlapping_regions", "traffic_outside_regional_window"} <= issues


def test_audit_missed_jobs_and_dead_heartbeat(tmp_path):
    m = campaign.load(make(tmp_path, execution_mode="sequential", region_order=ORDER))
    now = (START + timedelta(days=1, minutes=1)).timestamp()
    snap = snapshot(m, now)
    snap["servers"][0]["state"]["status"] = "closed"
    snap["servers"][1]["state"]["updated_at"] = campaign.timestamp(now - 60)
    snap["servers"][1]["service"]["MainPID"] = "0"
    issues = {a["issue"] for a in audit(m, snap, now)["alerts"]}
    assert {"region_not_complete_at_deadline", "stale_heartbeat", "scheduler_not_running"} <= issues
    assert not audit(m, snap, now)["measurement_complete"]


def test_audit_reports_each_regional_alert_in_order(tmp_path):
    m = campaign.load(make(tmp_path, execution_mode="sequential", region_order=ORDER))
    now = (START + timedelta(hours=3)).timestamp()
    snap = snapshot(m, now)
    server = snap["servers"][0]
    state = server["state"]
    state["status"] = "failed"
    state["active"] = {"country": "us"}
    state["progress"] = {"requests": 2000, "http_469_responses": 100, "successes": 1000}
    state["jobs"][0]["status"] = "interrupted"
    server.update(server_time=now + 60, disk_free_gb=1, memory_used_pct=95)
    snap["servers"][1] = {"region": "tor1", "error": "timeout"}
    snap["servers"][2]["state"]["campaign_id"] = "another"
    alerts = audit(m, snap, now)["alerts"]

    def issues(region):
        return [a for a in alerts if a.get("region") == region]

    assert [a["issue"] for a in issues("nyc3")] == [
        "scheduler_failed",
        "clock_offset",
        "low_disk",
        "high_memory",
        "incomplete_jobs",
        "round_backlog",
        "http_469_observed",
        "high_other_error_rate",
    ]
    assert issues("nyc3")[6:] == [
        {"region": "nyc3", "issue": "http_469_observed", "count": 100, "completed_attempts": 2000},
        {
            "region": "nyc3",
            "issue": "high_other_error_rate",
            "other_errors": 900,
            "non_469_attempts": 1900,
        },
    ]
    assert [a["issue"] for a in issues("tor1")] == ["unreachable_or_missing_state"]
    assert [a["issue"] for a in issues(ORDER[2])] == ["campaign_checkpoint_mismatch"]


def closed_empty_region(tmp_path):
    path = make(tmp_path, execution_mode="sequential", region_order=ORDER)
    m = campaign.load(path)
    s = new_state(m, "tor1", m["settings"])
    directory = path.parent / "regions" / "tor1"
    s["status"] = "closed"
    for job in s["jobs"]:
        job.update(
            status="completed",
            started_at=job["scheduled_at"],
            finished_at=job["scheduled_at"],
            observations=0,
            requests=1000,
            errors=1000,
        )
        atomic_json(
            job_directory(directory, job) / "summary.json",
            job | {"measurement": {"successes": 0, "completed": 1000}},
        )
    atomic_json(directory / "state.json", s)
    return m, s, directory


def test_backup_accepts_completed_no_success_but_detects_summary_and_tree_damage(tmp_path):
    m, state, directory = closed_empty_region(tmp_path)
    expected = tree_inventory(directory)
    receipt = verify_region(m, "tor1", directory, expected)
    assert receipt["verified_backup"] and receipt["complete_measurement"]
    assert receipt["completed_jobs"] == 72 and receipt["observations"] == 0
    summary = job_directory(directory, state["jobs"][0]) / "summary.json"
    summary.write_text("{}")
    with pytest.raises(ValueError, match="remote original"):
        verify_region(m, "tor1", directory, expected)
    with pytest.raises(ValueError, match="summary"):
        verify_region(m, "tor1", directory, tree_inventory(directory))


def test_backup_never_treats_terminal_failure_as_complete(tmp_path):
    m, state, directory = closed_empty_region(tmp_path)
    state = deepcopy(state)
    state["jobs"][0]["status"] = "not_started_deadline"
    atomic_json(directory / "state.json", state)
    receipt = verify_region(m, "tor1", directory, tree_inventory(directory))
    assert receipt["verified_backup"] and not receipt["complete_measurement"]
    assert receipt["completed_jobs"] == 71


def region_with_a_chunk(tmp_path):
    """A closed tor1 region whose first job committed one observation chunk of one row."""
    m, state, directory = closed_empty_region(tmp_path)
    job = state["jobs"][0]
    job["observations"] = 1
    folder = job_directory(directory, job)
    row = {
        "ip": "203.0.113.1",
        "country_code": job["country"].lower(),
        "round_scheduled_at": job["scheduled_at"],
        "country_started_at": job["started_at"],
        "observed_at": job["started_at"],
    }
    chunk = folder / "observations-000001.jsonl.gz"
    chunk.write_bytes(gzip.compress((json.dumps(row) + "\n").encode()))
    metadata = {"schema_version": 1, "file": chunk.name, "rows": 1, "sha256": digest(chunk)}
    atomic_json(chunk.with_suffix(".meta.json"), metadata)
    measurement = {"successes": 1, "completed": job["requests"]}
    atomic_json(folder / "summary.json", job | {"measurement": measurement})
    atomic_json(directory / "state.json", state)
    return m, directory, folder


def test_backup_counts_committed_chunks(tmp_path):
    m, directory, _ = region_with_a_chunk(tmp_path)
    receipt = verify_region(m, "tor1", directory, tree_inventory(directory))
    assert (receipt["observations"], receipt["chunks"]) == (1, 1)


def _rename_chunk(sidecar, name):
    atomic_json(sidecar, read_json(sidecar) | {"file": name})


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda folder, sidecar: _rename_chunk(sidecar, "../x.jsonl.gz"), "observation filename"),
        (
            lambda folder, sidecar: _rename_chunk(sidecar, "observations-000002.jsonl.gz"),
            "observation sidecar",
        ),
        (lambda folder, sidecar: sidecar.unlink(), "Observation rows differ"),
        (
            lambda folder, sidecar: (folder / "observations-000002.jsonl.gz.partial").touch(),
            "Uncommitted observations",
        ),
        (
            lambda folder, sidecar: atomic_json(
                folder / "summary.json",
                read_json(folder / "summary.json") | {"measurement": {"successes": 2}},
            ),
            "Measurement counters differ",
        ),
    ],
)
def test_backup_refuses_damaged_observations(tmp_path, damage, message):
    m, directory, folder = region_with_a_chunk(tmp_path)
    damage(folder, folder / "observations-000001.jsonl.meta.json")
    with pytest.raises(ValueError, match=message):
        verify_region(m, "tor1", directory, tree_inventory(directory))


def test_remote_tree_checks_only_the_current_unit_name():
    from atlas.onboarding.backup import REMOTE_TREE

    compile(REMOTE_TREE, "remote_tree", "exec")
    assert (
        "unit='atlas-onboarding-'+state['campaign_id']+'-'+state['region']+'.service'"
        in REMOTE_TREE
    )
    assert REMOTE_TREE.count(".service") == 1


INVENTORY = {"servers": {"tor1": {"host": "tor1.test", "root": "/srv", "identity_file": "key"}}}


class Transfer:
    """A fake rsync that copies the fake remote tree into staging, or hangs."""

    calls: list = []
    source: Path
    hang = False

    def __init__(self, command, **kwargs):
        Transfer.calls.append(command)
        self.staging = command[-1]
        self.pid, self.returncode = 4242, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def wait(self, timeout=None):
        if Transfer.hang and timeout is not None:
            raise subprocess.TimeoutExpired("rsync", timeout)
        shutil.copytree(Transfer.source, self.staging, dirs_exist_ok=True)
        self.returncode = 0
        return 0


def collecting(tmp_path, monkeypatch, returncode=0):
    """A closed tor1 region kept on a fake remote server, with ssh and rsync replaced."""
    m, _, directory = closed_empty_region(tmp_path)
    manifest_path = directory.parent.parent / "campaign.json"
    remote = Path("/srv/onboarding-networks", m["provider"], m["product"], m["campaign_id"])
    source = tmp_path / "remote"
    directory.rename(source)
    calls = []

    def ssh(command, **kwargs):
        calls.append(command)
        stdout = json.dumps(tree_inventory(source))
        return subprocess.CompletedProcess(command, returncode, stdout, "remote stderr")

    Transfer.calls, Transfer.source, Transfer.hang = [], source, False
    monkeypatch.setattr(backup.subprocess, "run", ssh)
    monkeypatch.setattr(backup.subprocess, "Popen", Transfer)
    atomic_json(tmp_path / "inventory.json", INVENTORY)
    return manifest_path, directory, remote / "regions" / "tor1", calls


def test_collect_region_transfers_verifies_and_publishes(tmp_path, monkeypatch):
    manifest_path, directory, remote, calls = collecting(tmp_path, monkeypatch)
    receipt = collect_region(manifest_path, tmp_path / "inventory.json", "tor1")
    assert receipt["verified_backup"] and receipt["completed_jobs"] == 72
    assert receipt["directory"] == str(directory) and receipt["remote_originals_preserved"]
    assert directory.is_dir() and not (manifest_path.parent / "backup/tor1.staging").exists()
    assert read_json(manifest_path.parent / "backup/tor1.json") == receipt
    ssh = calls[0]
    assert ssh[:2] == ["ssh", "-o"] and ssh[-2] == "root@tor1.test"
    assert ssh[-1].endswith(str(remote))
    rsync = Transfer.calls[0]
    assert rsync[-2] == "root@tor1.test:" + str(remote) + "/"
    assert rsync[-1] == str(manifest_path.parent / "backup/tor1.staging") + "/"
    log = manifest_path.parent / "backup/tor1-remote.log"
    assert log.read_text() == "remote stderr"
    # A second run verifies the published directory and transfers nothing.
    assert collect_region(manifest_path, tmp_path / "inventory.json", "tor1") == receipt
    assert len(Transfer.calls) == 1


def test_collect_region_refuses_unknown_region_and_failed_remote_check(tmp_path, monkeypatch):
    manifest_path, *_ = collecting(tmp_path, monkeypatch, returncode=1)
    with pytest.raises(ValueError, match="Unknown campaign region"):
        collect_region(manifest_path, tmp_path / "inventory.json", "mars")
    with pytest.raises(RuntimeError, match="Remote tree check failed"):
        collect_region(manifest_path, tmp_path / "inventory.json", "tor1")
    assert not Transfer.calls


def test_collect_region_kills_a_hung_transfer_and_keeps_staging(tmp_path, monkeypatch):
    manifest_path, directory, *_ = collecting(tmp_path, monkeypatch)
    Transfer.hang = True
    killed = []
    monkeypatch.setattr(backup.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    with pytest.raises(RuntimeError, match="Transfer timed out"):
        collect_region(manifest_path, tmp_path / "inventory.json", "tor1")
    assert killed == [(4242, backup.signal.SIGTERM), (4242, backup.signal.SIGKILL)]
    assert (manifest_path.parent / "backup/tor1.staging").is_dir() and not directory.exists()


def test_collect_region_keeps_staging_after_an_incomplete_transfer(tmp_path, monkeypatch):
    manifest_path, directory, *_ = collecting(tmp_path, monkeypatch)

    def wait(self, timeout=None):
        self.returncode = 23
        return 23

    monkeypatch.setattr(Transfer, "wait", wait)
    with pytest.raises(RuntimeError, match="Transfer incomplete"):
        collect_region(manifest_path, tmp_path / "inventory.json", "tor1")
    assert (manifest_path.parent / "backup/tor1.staging").is_dir() and not directory.exists()
