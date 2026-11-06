"""The probe's report side: one step's shares (`step_result`), the step-down rule (`reasons`),
the setup tar (`payload`) with the script that unpacks it (`SETUP_SCRIPT`) and the owner's
table (`markdown`). `probe` imports these back; this module never imports `probe`."""

from __future__ import annotations

import io
import tarfile
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

from .contracts import CampaignSpec
from .fanout import INSTALL_WHEEL
from .launch import WorkerInputs, manifest_path

# The step-down rule (docs/campaign.html#probe). A step fails for concurrency when:
RATE_LIMIT_PCT = 2.0  # more than 2% of completed requests are HTTP 429 (or Geonode's 469);
CONNECT_PCT = 2.0  # more than 2% are refused by or cannot reach the gateway (incl. 407, DNS);
SLOWDOWN_FACTOR, SLOWDOWN_POINTS = 2.0, 1.0  # timeouts + 502/503/504, or every other failure
# (TLS, protocol, other statuses, challenges, blocks), exceed twice the baseline rate and the
# baseline plus 1 point;
SUCCESS_DROP_POINTS = 3.0  # success falls more than 3 points below the baseline's;
COMPLETE_SHARE = 0.95  # fewer than 95% of the requests completed, or the run failed.
RATE_STATUSES, GATEWAY_STATUSES = ("429", "469"), ("502", "503", "504")
# The gateway refused or could not be reached: CONNECT failures, resets, a 407 (some gateways
# answer a connection or thread limit with it) and an unresolvable gateway.
CONNECT_ERRORS = ("proxy_connect", "connection", "proxy_auth", "dns")

# Runs with the worker's system Python: argv = worker root, secrets dir. Credentials go to a
# 0600 file under the secrets dir; the wheel is installed as for a round (`INSTALL_WHEEL`).
SETUP_SCRIPT = (
    r"""
import hashlib, io, os, pathlib, subprocess, sys, tarfile
root, secrets = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]) / "probe"
archive = tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()), mode="r:")
secrets.mkdir(mode=0o700, parents=True, exist_ok=True)
for member in archive.getmembers():
    if member.name.startswith("secrets/"):
        path = secrets / pathlib.PurePosixPath(member.name).name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, archive.extractfile(member).read()); os.close(fd)
members = [m for m in archive.getmembers() if m.name.startswith(("probe/", "wheel/"))]
archive.extractall(root, members=members, filter="data")
wheel = next(root / m.name for m in members if m.name.endswith(".whl"))
"""
    + INSTALL_WHEEL
    + r"""print("ready")
"""
)


def step_result(concurrency: int, requested: int, summary: dict[str, Any]) -> dict[str, Any]:
    """One run's counts as shares of its completed requests (a CONNECT answered 429 or 5xx is
    counted under its status and under connect errors), throughput and success latency.

    With the runner's answer classes (`--answer-classes`) the rate limits are the summary's
    `rate_limited` signals (CONNECT refusals among them leave `proxy_connect`) and its no-IP
    answers leave `other`; `answered` excludes the rate limits, and the real-error share is of
    answered requests. A summary without those fields gives the shares it always gave."""
    done, statuses, errors = summary["completed"], summary["status_codes"], summary["errors"]
    limited = summary.get("rate_limited")
    classes = {
        "rate_limited": sum(limited.values())
        if limited is not None
        else sum(statuses.get(s, 0) for s in RATE_STATUSES),
        "gateway_5xx": sum(statuses.get(s, 0) for s in GATEWAY_STATUSES),
        "proxy_connect": sum(errors.get(e, 0) for e in CONNECT_ERRORS)
        - (limited or {}).get("proxy_connect", 0),
        "timeout": errors.get("timeout", 0),
        "no_inventory": summary.get("no_inventory") or 0,
    }
    classes["other"] = max(0, summary["total_errors"] - sum(classes.values()))
    share = (lambda n: round(100.0 * n / done, 3)) if done else (lambda n: 0.0)
    answered = done - classes["rate_limited"] if limited is not None else done
    latency = summary.get("latency_ms") or {}
    return {
        "concurrency": concurrency,
        "requested": requested,
        "completed": done,
        "success_pct": share(summary["successes"]),
        **{f"{name}_pct": share(count) for name, count in classes.items()},
        "rps": round(summary["requests_per_second"], 1),
        "latency_p50_ms": latency.get("p50"),
        "latency_p95_ms": latency.get("p95"),
        "unique_ips": summary.get("unique_ips"),
        "stop_reason": summary.get("stop_reason"),
        **answer_shares(summary, classes, answered, share),
    }


def answer_shares(
    summary: dict[str, Any], classes: dict[str, int], answered: int, share: Callable[[int], float]
) -> dict[str, Any]:
    """The answer-class keys of `step_result`; None where the summary lacks their counts."""
    other, unique = summary.get("other_country"), summary.get("unique_ips")
    real = summary["total_errors"] - classes["rate_limited"] - classes["no_inventory"]
    return {
        "answered": answered,
        "other_country_pct": None if other is None else share(other),
        "duplicates_pct": None
        if unique is None
        else share(max(0, summary["successes"] - (other or 0) - unique)),
        "real_error_pct": round(100.0 * max(0, real) / answered, 3) if answered else 0.0,
    }


def reasons(step: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """Why a step fails for concurrency (empty: it passes). See the constants above."""
    if step.get("run_failed"):
        return ["run_failed"]
    found = []
    if step.get("answered", step["completed"]) < COMPLETE_SHARE * step["requested"]:
        found.append("incomplete")
    if step["rate_limited_pct"] > RATE_LIMIT_PCT:
        found.append("rate_limited")
    if step["proxy_connect_pct"] > CONNECT_PCT:
        found.append("proxy_connect")
    for reason, keys in (
        ("timeouts_5xx", ("timeout_pct", "gateway_5xx_pct")),
        ("other_errors", ("other_pct",)),
    ):
        rate, base = (sum(s[k] for k in keys) for s in (step, baseline))
        if rate > max(SLOWDOWN_FACTOR * base, base + SLOWDOWN_POINTS):
            found.append(reason)
    if step["success_pct"] < baseline["success_pct"] - SUCCESS_DROP_POINTS:
        found.append("success_drop")
    return found


def payload(
    root: Path, spec: CampaignSpec, files: WorkerInputs, networks: Collection[str]
) -> bytes:
    """One tar for `SETUP_SCRIPT`: the wheel, each network's census config and credentials."""
    entries = [(f"wheel/{files.wheel.name}", files.wheel.read_bytes())]
    for network in networks:
        config = manifest_path(root, spec, network).parent / "provider.toml"
        entries += [
            (f"probe/{network}.toml", config.read_bytes()),
            (f"secrets/{network}.toml", (files.credentials_dir / f"{network}.toml").read_bytes()),
        ]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o600
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


# The answer-class columns of the probe and shot tables (from `step_result`).
CLASS_COLUMNS = {
    "No-IP %": "no_inventory_pct",
    "Other-country %": "other_country_pct",
    "Duplicates %": "duplicates_pct",
    "Real errors % (answered)": "real_error_pct",
}
CLASS_HEADER = " | ".join(CLASS_COLUMNS)


def class_cells(step: dict[str, Any]) -> list[Any]:
    """A step's answer-class cells; "-" where it lacks one (a result written before them)."""
    return ["-" if step.get(key) is None else step[key] for key in CLASS_COLUMNS.values()]


STEP_COLUMNS = (
    "concurrency",
    "completed",
    "success_pct",
    "rate_limited_pct",
    "gateway_5xx_pct",
    "proxy_connect_pct",
    "timeout_pct",
    "other_pct",
    "rps",
    "latency_p50_ms",
    "latency_p95_ms",
)


def step_row(network: str, s: dict[str, Any]) -> str:
    """One step's row of the steps table; "-" where the step lacks a value."""
    cells = [s.get(k, "-") for k in STEP_COLUMNS]
    row = [network, *cells, *class_cells(s), ", ".join(s.get("step_down", [])) or "-"]
    return "| " + " | ".join(map(str, row)) + " |"


def markdown(result: dict[str, Any]) -> str:
    """The owner's table: one row per step, the maximum and the grammar line per network."""
    lines = [
        f"# Concurrency probe {result['probe_id']}",
        "",
        f"{result['requests']} requests per step, country {result['country']}, one "
        f"{result['size']} worker in {result['region']}.",
        "",
        "| Network | Max | Grammar |",
        "|---|---|---|",
    ]
    for network, found in result["networks"].items():
        best, verdict = found.get("max_concurrency"), found.get("verdict")
        # A verdict (the floor also fails, or no maximum) replaces the grammar line.
        line = verdict or f"`[testing] max_concurrency = {best}`"
        lines.append(f"| {network} | {best or '-'} | {line} |")
    lines += [
        "",
        "| Network | Concurrency | Completed | Success % | Rate-limited % | 5xx % "
        f"| Connect % | Timeout % | Other % | RPS | p50 ms | p95 ms | {CLASS_HEADER} "
        "| Step down |",
        "|" + "---|" * 17,
    ]
    for network, found in result["networks"].items():
        lines += [step_row(network, s) for s in found["steps"]]
    return "\n".join(lines) + "\n"
