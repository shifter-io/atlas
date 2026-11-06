"""SSH-only local fleet monitor. Inventory, snapshots and HTML stay private."""

from __future__ import annotations

import argparse
import asyncio
import html
import ipaddress
import json
import os
import re
import shlex
import time
from pathlib import Path

from .files import atomic_bytes, atomic_json, read_json
from .manifest import moment, timestamp

# Runs with the remote system Python; requires no installed framework or credentials.
REMOTE_INSPECT = r"""
import json, os, pathlib, shutil, subprocess, sys, time
root=pathlib.Path(sys.argv[1])
result={"server_time":time.time(),"installed":root.exists(),"cpu_count":os.cpu_count(),
        "load":os.getloadavg(),
        "disk_free_gb":round(shutil.disk_usage(root if root.exists() else "/").free/1e9,2)}
mem={}
for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
    k,v=line.split(":",1);mem[k]=int(v.split()[0])
result["memory_used_pct"]=round(100*(1-mem["MemAvailable"]/mem["MemTotal"]),1)
result["core_ticks"]={}
for line in pathlib.Path("/proc/stat").read_text().splitlines():
    fields=line.split()
    if fields and fields[0].startswith("cpu") and fields[0][3:].isdigit():
        ticks=list(map(int,fields[1:9]))
        result["core_ticks"][fields[0]]={"total":sum(ticks),"idle":ticks[3]+ticks[4]}
marker=root/"active-run.json"
if marker.exists():
    active=json.loads(marker.read_text())
    state_path=(root/active["state_path"]).resolve()
    if not state_path.is_relative_to(root.resolve()): raise ValueError("State path escaped root")
    result["assignment"]={k:active.get(k) for k in ("kind","region","unit","state_path")}
    if state_path.exists(): result["state"]=json.loads(state_path.read_text())
    unit=active.get("unit")
    if unit:
        reply=subprocess.run(["systemctl","show",unit,"--property=ActiveState,SubState,Result,ExecMainStatus,MainPID"],capture_output=True,text=True,timeout=5)
        result["service"]=dict(line.split("=",1)
                               for line in reply.stdout.splitlines() if "=" in line)
        pid=int(result["service"].get("MainPID","0"))
        if pid and pathlib.Path(f"/proc/{pid}").exists():
            result["open_fds"]=len(list(pathlib.Path(f"/proc/{pid}/fd").iterdir()))
            for line in pathlib.Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"): result["rss_mb"]=round(int(line.split()[1])/1024,1)
            pids=[pid]+result.get("state",{}).get("progress",{}).get("worker_pids",[])
            result["processes"]=[]
            for process_id in pids:
                try:
                    proc=pathlib.Path(f"/proc/{int(process_id)}")
                    fields=proc.joinpath("stat").read_text().rsplit(")",1)[1].split()
                    result["processes"].append({"pid":process_id,
                        "role":"coordinator" if process_id==pid else "http_worker",
                        "cpu_seconds":(int(fields[11])+int(fields[12]))/os.sysconf("SC_CLK_TCK"),
                        "start_ticks":fields[19],
                        "rss_mb":round(int(fields[21])*os.sysconf("SC_PAGE_SIZE")/1024**2,1)})
                except (OSError,ValueError,IndexError): pass
            result["rss_mb"]=round(sum(p["rss_mb"] for p in result["processes"]),1)
print(json.dumps(result))
"""


def cpu_usage(data: dict, previous: dict) -> dict:
    """Interval CPU use: each process's 100% means one fully occupied core."""
    elapsed = data.get("server_time", 0) - previous.get("server_time", 0)
    old = {p["pid"]: p for p in previous.get("processes", [])}
    for process in data.get("processes", []):
        before = old.get(process["pid"], {})
        if elapsed > 0 and before.get("start_ticks") == process.get("start_ticks"):
            process["cpu_pct"] = round(
                max(0, process["cpu_seconds"] - before["cpu_seconds"]) / elapsed * 100, 1
            )
    cores = {}
    for name, ticks in data.get("core_ticks", {}).items():
        before = previous.get("core_ticks", {}).get(name)
        if before and ticks["total"] > before["total"]:
            cores[name] = round(
                100 * (1 - (ticks["idle"] - before["idle"]) / (ticks["total"] - before["total"])), 1
            )
    data["core_cpu_pct"] = cores
    return data


def load_inventory(path: Path) -> dict:
    data = read_json(path)
    if data.get("schema_version") != 1 or not isinstance(data.get("servers"), dict):
        raise ValueError("Invalid private server inventory")
    for region, server in data["servers"].items():
        if server.get("identity_file"):
            server["identity_file"] = os.path.expanduser(server["identity_file"])
        if not re.fullmatch(r"[a-z0-9]+", region):
            raise ValueError("Invalid region")
        ipaddress.ip_address(server["host"])
        if not re.fullmatch(r"[a-z_][a-z0-9_-]*", server.get("user", "root")):
            raise ValueError("Invalid SSH user")
        if not Path(server["root"]).is_absolute():
            raise ValueError("Remote root must be absolute")
    return data["servers"]


def _health(state: dict, service: dict, age: float, lagging: bool, elapsed: float) -> str:
    """A node's lifecycle, unless an unfinished one has a dead service, an old heartbeat, an
    unfinished earlier round or a probe running past an hour."""
    lifecycle = state["status"]
    if lifecycle in ("completed", "closed", "cancelled", "stopped", "failed"):
        return lifecycle
    if service and service.get("ActiveState") not in ("active", "activating"):
        return "failed"
    if age > 20:
        return "stale"
    if lagging:
        return "behind"
    if state.get("kind") == "probe" and elapsed > 3600:
        return "over_hour"
    return lifecycle


def _without_state(data: dict) -> dict:
    """A node with no scheduler state: starting, failed, idle or not deployed."""
    if data.get("assignment"):
        service = data.get("service", {})
        return data | {
            "health": "starting"
            if service.get("ActiveState") in ("active", "activating")
            else "failed"
        }
    return data | {"health": "idle" if data.get("installed") else "not_deployed"}


def _elapsed(state: dict, active, remote_now: float) -> float:
    """Seconds since the run started, up to its finish or the server's current time."""
    started = state.get("started_at") or (active or {}).get("started_at")
    return (
        (
            moment(state.get("finished_at") or timestamp(remote_now)) - moment(started)
        ).total_seconds()
        if started
        else 0
    )


def _lagging(jobs: list, remote_now: float) -> bool:
    """Whether a job of an earlier, already eligible hourly round is still unfinished."""
    first = moment(jobs[0]["scheduled_at"]).timestamp() if jobs else None
    eligible_round = min(23, int((remote_now - first) // 3600)) if first is not None else None
    return bool(
        jobs
        and any(j["status"] in ("pending", "running") and j["round"] < eligible_round for j in jobs)
    )


def _counters(measurement: dict, progress: dict, jobs: list, active) -> dict:
    """Request, IP, success and error counts: the measurement's, else the live progress."""
    return {
        "requests": measurement.get(
            "completed",
            sum(j.get("requests", 0) for j in jobs)
            + (progress.get("requests", 0) if active else 0),
        ),
        "unique_ips": measurement.get("unique_ips", progress.get("unique_ips", 0)),
        "successes": measurement.get("successes", progress.get("successes", 0)),
        "errors": measurement.get(
            "total_errors", progress.get("requests", 0) - progress.get("successes", 0)
        ),
        "requests_per_second": measurement.get(
            "requests_per_second", progress.get("requests_per_second")
        ),
    }


def classify(data: dict, now: float) -> dict:
    if data.get("error"):
        return data | {"health": "unreachable"}
    state = data.get("state")
    if not state:
        return _without_state(data)
    remote_now = data.get("server_time", now)
    age = max(0, remote_now - moment(state["updated_at"]).timestamp())
    lifecycle = state["status"]
    measurement = state.get("measurement") or {}
    progress = state.get("progress") or {}
    active = state.get("active")
    jobs = state.get("jobs", [])
    elapsed = _elapsed(state, active, remote_now)
    lagging = _lagging(jobs, remote_now)
    data.update(
        health=_health(state, data.get("service", {}), age, lagging, elapsed),
        heartbeat_age_seconds=round(age, 1),
        elapsed_seconds=round(elapsed, 2),
        behind=lagging,
        queue_depth=sum(
            j["status"] == "pending" and moment(j["scheduled_at"]).timestamp() <= remote_now
            for j in jobs
        ),
        **_counters(measurement, progress, jobs, active),
        country=state.get("country") or (active or {}).get("country"),
        lifecycle=lifecycle,
    )
    return data


async def inspect(region: str, server: dict) -> dict:
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=1",
    ]
    if server.get("identity_file"):
        command += [
            "-o",
            "IdentitiesOnly=yes",
            "-i",
            server["identity_file"],
        ]
    command += [
        f"{server.get('user', 'root')}@{server['host']}",
        shlex.join(["python3", "-c", REMOTE_INSPECT, server["root"]]),
    ]
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 15)
        if process.returncode:
            return {"region": region, "error": f"SSH inspection failed (exit {process.returncode})"}
        return json.loads(stdout) | {"region": region}
    except (TimeoutError, ValueError):
        if process.returncode is None:
            process.kill()
            await process.wait()
        return {"region": region, "error": "SSH inspection timed out or returned invalid data"}


def esc(value) -> str:
    return html.escape(str(value))


def _card(node: dict) -> str:
    """One server's status card on the fleet page."""
    state = node.get("state") or {}
    active = state.get("active") or {}
    config = state.get("settings") or {}
    health = node["health"]
    warning = health in ("unreachable", "failed", "stale", "behind", "over_hour")
    tone = (
        "warning"
        if warning
        else "healthy"
        if health in ("running", "armed", "completed")
        else "muted"
    )
    elapsed = node.get("elapsed_seconds")
    duration = f"{elapsed / 60:.1f} min" if elapsed is not None else "—"
    rate = node.get("requests_per_second")
    speed = f"{rate:,.1f}/s" if rate is not None else "—"

    def number(key):
        value = node.get(key)
        return f"{value:,}" if isinstance(value, (int, float)) else "—"

    cores = " · ".join(f"{esc(k)} {v}%" for k, v in node.get("core_cpu_pct", {}).items())
    processes = "<br>".join(
        f"{esc(p['role'])} PID {p['pid']}: {p.get('cpu_pct', '—')}% CPU, {p['rss_mb']} MiB"
        for p in node.get("processes", [])
    )

    return f'''<article class="node"><header><b>{esc(node["region"])}</b>
<span class="{tone}">{esc(health.replace("_", " "))}</span></header>
<p>{esc(node.get("country") or "No active country")} · {esc(duration)} · {esc(speed)}</p>
<div class="metrics"><div><b>{number("requests")}</b><small>Requests</small></div>
<div><b>{number("unique_ips")}</b><small>Unique IPs</small></div>
<div><b>{number("errors")}</b><small>Errors</small></div></div>
<p class="muted">Heartbeat: {esc(node.get("heartbeat_age_seconds", "—"))} s ·
Queued: {esc(node.get("queue_depth", "—"))} · Round: {esc(active.get("round", "—"))}</p>
<details><summary>Settings &amp; server resources</summary>
<p>Concurrency {esc(config.get("concurrency", "—"))}
 · Timeout {esc(config.get("timeout_seconds", "—"))} s<br>
Host RAM {esc(node.get("memory_used_pct", "—"))}% · Run total {esc(node.get("rss_mb", "—"))} MiB<br>
Free disk {esc(node.get("disk_free_gb", "—"))} GB · CPUs {esc(node.get("cpu_count", "—"))}<br>
{cores}<br>{processes}<br>
Scheduled {esc(state.get("scheduled_at") or "—")}<br>
Service {esc((node.get("service") or {}).get("SubState", "—"))}</p></details></article>'''


def render(snapshot: dict) -> bytes:
    cards = [_card(node) for node in snapshot["servers"]]
    warnings = "".join(
        f"<li>{esc(n['region'])}: {esc(n.get('error') or n['health'])}</li>"
        for n in snapshot["servers"]
        if n["health"] in ("unreachable", "failed", "stale", "behind", "over_hour")
    )
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="10"><title>Onboarding fleet status</title>
<style>body{{font:15px/1.6 system-ui;margin:32px;color:#1c2d40;background:#f4f7fa}}
h1{{font-size:32px}}
.card{{background:white;padding:20px;border:1px solid #d8e1eb;border-radius:10px}}
.nodes{{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:14px}}
.node{{border:1px solid #d8e1eb;border-radius:9px;padding:16px;min-width:0}}
.node header{{display:flex;justify-content:space-between;gap:12px}}header>b{{font-size:22px}}
.metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}
.metrics b{{font-size:18px}}small{{display:block;color:#56677b}}.muted{{color:#56677b}}
.healthy{{color:#176b4e}}summary{{cursor:pointer;font-size:13px}}
@media(max-width:600px){{body{{margin:16px}}.card{{padding:14px}}}}
.warning{{color:#a33d16}}#stale{{font-weight:bold}}</style>
<h1>Onboarding fleet status</h1><p>Private local monitor · refreshed from SSH every 10 seconds</p>
<div class="card"><b>Last collection: {esc(snapshot["collected_at"])}</b>
<span id="stale"></span><p>Running / armed: supervised process and fresh heartbeat.
Behind: an earlier
hourly round is still unfinished. Not deployed: reachable server without the onboarding
installation.</p>
<ul class="warning">{warnings}</ul><div class="nodes">{"".join(cards)}</div>
<p>Unique IPs and errors describe the active country (or the finished probe); requests include
completed campaign countries. Snapshots contain no observed IP addresses or credentials.</p></div>
<script>const collected={snapshot["collected_epoch"] * 1000};
function check(){{const age=(Date.now()-collected)/1000;
document.getElementById('stale').textContent=age>30?' — MONITOR STALE ('+Math.round(age)+'s)':'';}}
check();setInterval(check,1000);</script></html>""".encode()


async def collect(inventory: Path, output: Path) -> dict:
    servers = load_inventory(inventory)
    path = output / "snapshot.json"
    previous = read_json(path) if path.exists() else {}
    old = {n["region"]: n for n in previous.get("servers", [])}
    values = await asyncio.gather(*(inspect(region, value) for region, value in servers.items()))
    now = time.time()
    result = {
        "collected_at": timestamp(now),
        "collected_epoch": now,
        "servers": [
            classify(cpu_usage(value, old.get(value["region"], {})), now) for value in values
        ],
    }
    atomic_json(output / "snapshot.json", result)
    atomic_bytes(output / "index.html", render(result))
    return result


def register(actions):
    command = actions.add_parser("fleet-status", help="Collect private SSH fleet status and HTML")
    command.add_argument("--inventory", type=Path, required=True)
    command.add_argument("--output", type=Path, default=Path(".state/onboarding-monitor"))
    command.add_argument("--watch", action="store_true")


def dispatch(args: argparse.Namespace):
    async def work():
        while True:
            result = await collect(args.inventory, args.output)
            print(
                json.dumps(
                    {
                        "collected_at": result["collected_at"],
                        "servers": {n["region"]: n["health"] for n in result["servers"]},
                    }
                ),
                flush=True,
            )
            if not args.watch:
                return 0
            await asyncio.sleep(10)

    return asyncio.run(work())
