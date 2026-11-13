"""CAMP-1d: the traffic reports and the Mac watch that copies them and notifies.

Offline only: saved hour records, a fake clock, faked SSH replies and notifications; the shared
helpers are in `campaign_tick_cases.py`.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta

from campaign_fakes import CID
from campaign_tick_cases import IPV4, START, Clock, make_root, watch_data, worker

from atlas.campaign.contracts import HourRecord
from atlas.campaign.reports import write_traffic_report
from atlas.campaign.tick import save_hour
from atlas.campaign.watch import Watch, WatchIO, acknowledge, report_message, run_watch

# 6: traffic report totals equal the sum of the records (golden markdown and TSV).
GOLDEN = """# Campaign hour 03 — traffic per network

Campaign `20270101t220000z-0123456789ab`, hour scheduled 2027-01-02T01:00:00+00:00, state done. \
Start lateness: 0.2–1.4 s. Concurrency per worker: 2000. Launch delay: 0.4 s.

| Network | GB up | GB down | GB total | Requests | Successes | Error % | Countries | Regions \
| Slowest region | Worker-hours | Cumulative GB |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|
| geonode | 0.200 | 1.000 | 1.200 | 300 | 270 | 10.0 | 20/25 | 1/2 | lon1 (2h 00m) \
| 2.50 | 1.200 |
| shifter | 0.300 | 2.700 | 3.000 | 700 | 693 | 1.0 | 50/50 | 2/2 | nyc3 (1h 30m) \
| 3.00 | 4.000 |
| **Total** | 0.500 | 3.700 | 4.200 | 1000 | 963 | 3.7 | 70/75 | 3/4 |  | 5.50 | 5.200 |

Cumulative GB: every worker collected so far, in all hours, when this report was written.

## CPU per region (worst worker)

| Region | Workers | CPU p50 % | CPU p95 % | CPU max % | Load1 p95 | Memory max % |
|---|---:|---:|---:|---:|---:|---:|
| lon1 | 2 | 41.0 | 71.0 | 93.0 | 1.9 | 38.0 |
| nyc3 | 1 | 30.0 | 60.0 | 80.0 | 1.5 | 30.0 |

Bytes are the framework's client-side wire counts (`bytes_up` and `bytes_down` of each country \
summary); a provider's own billing may differ slightly.
"""


def report_fixture(root):
    base = "2027-01-02T00:58:30+00:00"

    def at(minutes):
        return (datetime.fromisoformat(base) + timedelta(minutes=minutes)).isoformat()

    cpu = {
        "cpu_p50_pct": 30.0,
        "cpu_p95_pct": 60.0,
        "cpu_max_pct": 80.0,
        "load1_p95": 1.5,
        "mem_used_max_pct": 30.0,
        "concurrency": 2000,
    }
    hot = cpu | {
        "cpu_p50_pct": 41.0,
        "cpu_p95_pct": 71.0,
        "cpu_max_pct": 93.0,
        "load1_p95": 1.9,
        "mem_used_max_pct": 38.0,
    }
    done = {"status": "destroyed", "requested_at": base, "started_at": at(1.5)}
    drops = [
        worker(
            "shifter",
            "lon1",
            3,
            droplet_id="1",
            bytes_up=10**8,
            bytes_down=9 * 10**8,
            requests=300,
            successes=297,
            errors=3,
            finished_at=at(61.5),
            collected_at=at(62),
            destroyed_at=at(90),
            start_lateness_seconds=0.2,
            **done,
            **cpu | hot,
        ),
        worker(
            "shifter",
            "nyc3",
            3,
            droplet_id="2",
            bytes_up=2 * 10**8,
            bytes_down=18 * 10**8,
            requests=400,
            successes=396,
            errors=4,
            finished_at=at(91.5),
            collected_at=at(92),
            destroyed_at=at(90),
            start_lateness_seconds=1.4,
            **done,
            **cpu,
        ),
        worker(
            "geonode",
            "lon1",
            3,
            droplet_id="3",
            bytes_up=2 * 10**8,
            bytes_down=10**9,
            requests=300,
            successes=270,
            errors=30,
            finished_at=at(121.5),
            collected_at=at(122),
            destroyed_at=at(150),
            **done,
            **cpu | hot,
        ),
    ]
    record = HourRecord(
        CID,
        3,
        "2027-01-02T01:00:00+00:00",
        state="done",
        droplets=drops,
        launch_delay_seconds=0.4,
        traffic={
            "shifter": {"countries_done": 50, "countries_total": 50, "worker_hours": 3.0},
            "geonode": {"countries_done": 20, "countries_total": 25, "worker_hours": 2.5},
        },
    )
    save_hour(root, record)
    earlier = HourRecord(
        CID,
        2,
        "2027-01-02T00:00:00+00:00",
        state="done",
        droplets=[
            worker(
                "shifter",
                "lon1",
                2,
                droplet_id="4",
                bytes_up=5 * 10**8,
                bytes_down=5 * 10**8,
                collected_at=base,
            )
        ],
    )
    save_hour(root, earlier)
    return record


def test_the_traffic_report_totals_equal_the_sum_of_the_records(tmp_path):
    root = make_root(tmp_path)
    record = report_fixture(root)
    path = write_traffic_report(root, 3)
    assert path == root / "reports" / "traffic" / "hour-03.md"
    assert path.read_text() == GOLDEN
    total = sum(d.bytes_up + d.bytes_down for d in record.droplets)
    assert "| **Total** | 0.500 | 3.700 | 4.200 |" in GOLDEN and total == 42 * 10**8
    assert (root / "reports" / "traffic.tsv").read_text().splitlines() == [
        "hour\tnetwork\tbytes_up\tbytes_down\trequests\tsuccesses\terrors\tcountries_done\t"
        "countries_total\tregions_done\tworker_hours",
        "02\tshifter\t500000000\t500000000\t0\t0\t0\t0\t0\t0\t0.000",
        "03\tgeonode\t200000000\t1000000000\t300\t270\t30\t20\t25\t1\t2.500",
        "03\tshifter\t300000000\t2700000000\t700\t693\t7\t50\t50\t2\t3.000",
    ]


def test_an_hour_without_samples_or_traffic_still_reports(tmp_path):
    root = make_root(tmp_path)
    save_hour(root, HourRecord(CID, 0, START.isoformat(), state="failed"))
    text = write_traffic_report(root, 0).read_text()
    assert "No CPU samples recorded." in text and "Start lateness: not recorded" in text


# 10: the watch writes the states, copies reports and notifies (faked SSH and notifications).
def test_the_watch_writes_states_copies_reports_and_notifies(tmp_path):
    clock, notes, replies = Clock(START), [], []
    status_file, traffic = tmp_path / "campaign.status.md", tmp_path / "traffic"
    status_file.write_text("State: active\n\nOperator notes stay.\n")
    io = WatchIO(
        fetch=lambda inv, offset, seen: replies.pop(0),
        notify=notes.append,
        clock=clock,
        sleep=lambda _s: None,
    )
    replies.append(watch_data(START, reports={"hour-00.md": "# hour 0\n"}, offset=10))
    watch = Watch({"host": "manager"}, status_file, traffic, io)
    assert watch.once() == "active"
    assert (traffic / "hour-00.md").read_text() == "# hour 0\n"
    text = status_file.read_text()
    assert text.startswith("State: active\nUpdated UTC: 2027-01-01T22:00:00Z\n")
    assert "Hours: 1 done, 1 running (of 2)\nLive workers: 16\n" in text
    assert "Traffic-Latest: hour 00 traffic/hour-00.md\nAlerts: 0 high (0 new)\n" in text  # w3
    assert text.endswith("\nOperator notes stay.\n")
    assert notes == ["CAMP-1 hour 00: 3.00 GB; top network shifter 3.00 GB"]
    high = {"code": "destroy_refused", "level": "high", "key": "123"}
    replies.append(watch_data(START, alerts=[high, {"code": "x", "level": "info"}], offset=20))
    assert watch.once() == "blocked" and notes[-1] == "CAMP-1 alert destroy_refused (123)"
    replies.append(OSError("ssh"))
    io.fetch = lambda *a: (_ for _ in ()).throw(replies.pop(0))
    clock.now = START + timedelta(minutes=4)
    assert Watch({}, status_file, traffic, io).once() == "blocked"  # state persists
    assert notes[-1].startswith("CAMP-1 alert manager_stale") and len(notes) == 3
    assert not IPV4.search(status_file.read_text())


def test_run_watch_stops_when_finished(tmp_path):
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"host": "manager", "campaign_id": CID}))
    replies = [watch_data(START), watch_data(START, finished=True)]
    io = WatchIO(
        fetch=lambda *a: replies.pop(0),
        notify=lambda _t: None,
        clock=Clock(START),
        sleep=lambda _s: None,
    )
    assert run_watch(inventory, tmp_path / "s.md", tmp_path / "t", io=io) == "finished"
    assert report_message("hour-05.md", {}) == "CAMP-1 hour 05: 0.00 GB"


def test_ssh_fetch_and_notify_build_safe_commands(monkeypatch):
    from atlas.campaign import watch

    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return types.SimpleNamespace(stdout=json.dumps({"ok": True}))

    monkeypatch.setattr(watch.subprocess, "run", run)
    inventory = {
        "host": "manager.example",
        "identity": "id_mac",
        "known_hosts": "kh",
        "campaign_id": CID,
    }
    assert watch.ssh_fetch(inventory, 5, {}) == {"ok": True}
    assert calls[0][:3] == ["ssh", "-i", "id_mac"] and "StrictHostKeyChecking=yes" in calls[0]
    assert calls[0][-1].endswith(f"/opt/proxy-index-campaign/campaigns/{CID} 5 -")
    watch.ssh_fetch(inventory, 5, {"hour-01.md": "ab", "hour-00.md": "cd"})
    assert calls[1][-1].endswith(" 5 hour-00.md:cd,hour-01.md:ab")
    watch.notify('hour "1"; do shell script "x"')
    assert calls[2][-1] == 'hour "1"; do shell script "x"'  # argv only, never in the script


def test_the_remote_reader_returns_new_and_rewritten_reports(tmp_path):
    """w2: reports are tracked by name and content hash, so a rewritten report comes again."""
    import hashlib
    import subprocess

    from atlas.campaign import watch

    reports = tmp_path / "reports" / "traffic"
    reports.mkdir(parents=True)
    (reports / "hour-00.md").write_text("old\n")
    (tmp_path / "alerts.jsonl").write_text('{"code": "a"}\n{"code": "b"}\n')

    def read(offset, seen):
        out = subprocess.run(
            [sys.executable, "-c", watch.REMOTE_READ, str(tmp_path), str(offset), seen],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(out.stdout)

    first = read(0, "-")
    assert first["reports"] == {"hour-00.md": "old\n"} and len(first["alerts"]) == 2
    seen = f"hour-00.md:{hashlib.sha256(b'old' + bytes([10])).hexdigest()}"
    again = read(first["offset"], seen)
    assert again["reports"] == {} and again["alerts"] == [] and again["status"] is None
    (reports / "hour-00.md").write_text("rewritten\n")
    assert read(first["offset"], seen)["reports"] == {"hour-00.md": "rewritten\n"}


def test_the_watch_acknowledges_alerts_and_survives_bad_lines(tmp_path):
    """w1: only unacknowledged high alerts block; w2: a bad line never repeats notifications."""
    clock, notes, replies = Clock(START), [], []
    status_file, traffic = tmp_path / "campaign.status.md", tmp_path / "traffic"
    io = WatchIO(
        fetch=lambda *a: replies.pop(0), notify=notes.append, clock=clock, sleep=lambda _s: None
    )
    high = json.dumps({"code": "missed_launch", "level": "high", "key": "h01-armed"})
    replies.append(
        watch_data(START, reports={"hour-00.md": "# hour 0\n"}, offset=10)
        | {"alerts": ["not json", "[1]", high]}
    )
    assert Watch({}, status_file, traffic, io).once() == "blocked"
    assert notes[1:] == [
        "CAMP-1 alert watch_parse_error: skipped a malformed alert line",
        "CAMP-1 alert missed_launch (h01-armed)",
    ]
    assert "Alerts: 1 high (1 new)" in status_file.read_text()
    assert acknowledge(status_file) == 1
    replies.append(
        watch_data(START, reports={"hour-00.md": "# hour 0, rewritten\n"}, offset=20)
        | {"alerts": ["also bad"]}
    )
    watch = Watch({}, status_file, traffic, io)
    assert watch.once() == "active" and "Alerts: 1 high (0 new)" in status_file.read_text()
    assert (traffic / "hour-00.md").read_text() == "# hour 0, rewritten\n"
    assert len(notes) == 4  # the rewritten report once; the parse error only once
    replies.append({"reports": {"hour-01.md": "# hour 1\n"}, "tsv": "", "alerts": None})
    assert watch.once() == "active"  # malformed reply: the report is kept, state saved
    assert notes[-1] == "CAMP-1 hour 01: 0.00 GB" and "hour-01.md" in watch.memory["seen"]
    assert "hour-01.md" in Watch({}, status_file, traffic, io).memory["seen"]


# Review finding S4: failed at 34bdc8b.
def test_s4_cumulative_gb_counts_every_hour_collected_so_far(tmp_path):
    root = make_root(tmp_path)
    late = START + timedelta(hours=2)
    save_hour(
        root,
        HourRecord(
            CID,
            0,
            START.isoformat(),
            state="done",
            droplets=[
                worker(
                    "shifter",
                    "lon1",
                    0,
                    droplet_id="1",
                    bytes_up=10**9,
                    bytes_down=10**9,
                    collected_at=late.isoformat(),
                )
            ],
        ),
    )
    save_hour(
        root,
        HourRecord(
            CID,
            1,
            (START + timedelta(hours=1)).isoformat(),
            state="done",
            droplets=[
                worker(
                    "shifter",
                    "lon1",
                    1,
                    droplet_id="2",
                    bytes_up=10**9,
                    bytes_down=2 * 10**9,
                    collected_at=late.isoformat(),
                )
            ],
        ),
    )
    text = write_traffic_report(root, 0).read_text()
    assert "| shifter | 1.000 | 1.000 | 2.000 |" in text
    assert text.split("| shifter |")[1].splitlines()[0].endswith("| 5.000 |")


def test_w1b_an_ack_reaches_the_long_running_watch(tmp_path):
    """The launchd watch keeps one Watch for the whole campaign; its saves never undo --ack."""
    clock, notes = Clock(START), []
    status_file = tmp_path / "campaign.status.md"
    high = json.dumps({"code": "missed_launch", "level": "high", "key": "h01-armed"})
    replies = [
        watch_data(START, offset=10) | {"alerts": [high]},
        watch_data(START, offset=10),
        watch_data(START, offset=10),
        watch_data(START, offset=20) | {"alerts": [high.replace("h01", "h02")]},
    ]
    watch = Watch(
        {},
        status_file,
        tmp_path / "traffic",
        WatchIO(
            fetch=lambda *a: replies.pop(0), notify=notes.append, clock=clock, sleep=lambda _s: None
        ),
    )
    assert watch.once() == "blocked"
    assert acknowledge(status_file) == 1
    assert watch.once() == "active" and watch.once() == "active"  # the same, long-lived watch
    assert "Alerts: 1 high (0 new)" in status_file.read_text()
    assert watch.once() == "blocked"  # a later high alert blocks again
    assert "Alerts: 2 high (1 new)" in status_file.read_text()
