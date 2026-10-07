"""Verify local chunks and stream one private network export and HTML report."""

from __future__ import annotations

import gzip
import html
import ipaddress
import json
import os
import shutil
from contextlib import ExitStack
from pathlib import Path

from .files import atomic_bytes, atomic_json, digest, fingerprint, lock, private_dir, read_json
from .manifest import load, moment, regional_window
from .scheduler import job_directory, status, validate_state

FIELDS = {"ip", "country_code", "round_scheduled_at", "country_started_at", "observed_at"}


def verify_chunk(path: Path, metadata: dict, job: dict, end: str) -> int:
    if path.is_symlink() or metadata.get("file") != path.name:
        raise ValueError("Invalid observation chunk path")
    if metadata.get("schema_version") != 1 or digest(path) != metadata["sha256"]:
        raise ValueError("Observation chunk checksum mismatch")
    count = 0
    with gzip.open(path, "rb") as stream:
        while line := stream.readline(2049):
            if len(line) > 2048 or not line.endswith(b"\n"):
                raise ValueError("Malformed observation line")
            row = json.loads(line)
            if set(row) != FIELDS or not all(isinstance(v, str) for v in row.values()):
                raise ValueError("Invalid observation schema")
            ipaddress.ip_address(row["ip"])
            if (
                row["country_code"] != job["country"].lower()
                or row["round_scheduled_at"] != job["scheduled_at"]
                or row["country_started_at"] != job["started_at"]
                or not moment(row["round_scheduled_at"])
                <= moment(row["country_started_at"])
                <= moment(row["observed_at"])
                < moment(end)
            ):
                raise ValueError("Observation does not belong to this country run or time window")
            count += 1
    if type(metadata["rows"]) is not int or count != metadata["rows"]:
        raise ValueError("Observation chunk row count mismatch")
    return count


def render_report(manifest: dict, result: dict) -> bytes:
    esc = lambda value: html.escape(str(value))  # noqa: E731
    rows = []
    for region in result["regions"]:
        for round_info in region["rounds"]:
            duration = round_info["duration_seconds"]
            minutes = f"{duration / 60:.1f}" if duration is not None else "Incomplete"
            delay = round_info["delay_seconds"]
            rows.append(
                "<tr>"
                + "".join(
                    f"<td>{esc(v)}</td>"
                    for v in (
                        region["region"],
                        round_info["round"],
                        round_info["scheduled_at"],
                        minutes,
                        f"{delay:.1f}" if delay is not None else "Not started",
                        "Over 40 min"
                        if duration is not None and duration > 2400
                        else "Complete"
                        if round_info["completed"]
                        else "Incomplete",
                    )
                )
                + "</tr>"
            )
    issues = "".join(f"<li>{esc(issue)}</li>" for issue in result["issues"])
    details = "".join(
        f"<li><b>{esc(r['region'])}</b>: {r['requests']:,} requests; {r['errors']:,} errors; "
        f"peak memory {esc(r['peak_memory_mb'])} MiB; concurrency "
        f"{r['settings']['concurrency']}; timeout {r['settings']['timeout_seconds']} s</li>"
        for r in result["regions"]
    )
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Network onboarding report</title><style>
body{{font:16px/1.6 system-ui,sans-serif;max-width:1200px;margin:40px auto;padding:0 24px;
color:#182434;background:#f5f7fa}}h1,h2{{line-height:1.2}}.summary{{background:white;padding:24px;
border:1px solid #dae1e9;border-radius:12px}}table{{width:100%;border-collapse:collapse;
background:white;font-size:14px}}
td,th{{padding:10px;text-align:left;border-bottom:1px solid #dae1e9}}
.scroll{{overflow:auto}}code{{overflow-wrap:anywhere}}.issues{{color:#973814}}
</style><h1>Network onboarding</h1><p>{esc(manifest["provider"])} / {esc(manifest["product"])}</p>
<div class="summary"><b>{result["rows"]:,} verified observations</b><br>
Campaign: <code>{esc(manifest["campaign_id"])}</code><br>
{esc(manifest["starts_at"])} → {esc(manifest["ends_at"])}<br>
Coverage: {"Complete" if result["complete"] else "Incomplete"} · duplicates retained
 · requested countries
</div><h2>Coverage and integrity</h2><ul class="issues">
{issues or "<li>No missing work or partial chunks.</li>"}</ul>
<p>The export contains no observations at or after the campaign deadline. Round duration includes
sequential country testing and file finalization. The initial target is 30–40 minutes per round.</p>
<h2>Regional settings and resources</h2><ul>{details}</ul>
<h2>Hourly rounds</h2><div class="scroll"><table><thead><tr><th>Region</th><th>Round (0–23)</th>
<th>Scheduled UTC</th><th>Duration, min</th><th>Start delay, s</th><th>Status</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div></html>""".encode()


def export_campaign(manifest_path: Path) -> dict:
    manifest = load(manifest_path)
    root = manifest_path.parent
    output = root / "export"
    private_dir(output)
    result = {
        "schema_version": 1,
        "campaign_id": manifest["campaign_id"],
        "campaign_sha256": fingerprint(manifest),
        "rows": 0,
        "chunks": 0,
        "regions": [],
        "issues": [],
    }
    with ExitStack() as locks:
        locks.enter_context(lock(output / ".lock"))
        for region in manifest["regions"]:
            directory = root / "regions" / region
            if directory.exists():
                locks.enter_context(lock(directory / ".lock"))
        partial = output / "observations.jsonl.gz.partial"
        # A previous failed export is not a published export; it can be rebuilt from chunks.
        fd = os.open(partial, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as destination:
                for region in manifest["regions"]:
                    directory = root / "regions" / region
                    if not (directory / "state.json").exists():
                        result["issues"].append(f"{region}: missing regional checkpoint")
                        continue
                    state = read_json(directory / "state.json")
                    validate_state(state, manifest, region)
                    regional = status(state)
                    result["regions"].append(regional)
                    if regional["unfinished_countries"]:
                        result["issues"].append(
                            f"{region}: {regional['unfinished_countries']} unfinished country runs"
                        )
                    for job in state["jobs"]:
                        folder = job_directory(directory, job)
                        count = 0
                        verified = set()
                        for meta_path in sorted(folder.glob("observations-*.jsonl.meta.json")):
                            metadata = read_json(meta_path)
                            filename = metadata["file"]
                            if not isinstance(filename, str) or Path(filename).name != filename:
                                raise ValueError("Invalid chunk filename")
                            chunk = folder / filename
                            if chunk.with_suffix(".meta.json") != meta_path:
                                raise ValueError("Chunk filename does not match its sidecar")
                            count += verify_chunk(
                                chunk, metadata, job, regional_window(manifest, region)["ends_at"]
                            )
                            with chunk.open("rb") as source:
                                shutil.copyfileobj(source, destination, length=1024 * 1024)
                            verified.add(filename)
                            result["chunks"] += 1
                        result["rows"] += count
                        if job["status"] == "completed" and count != job.get("observations"):
                            raise ValueError("Country observation count does not reconcile")
                        leftovers = [
                            p.name
                            for p in folder.glob("observations-*")
                            if p.name.endswith(".partial")
                            or (p.name.endswith(".gz") and p.name not in verified)
                        ]
                        if leftovers:
                            result["issues"].append(
                                f"{region} round {job['round']} {job['country']}: "
                                f"{len(leftovers)} uncommitted chunk files excluded"
                            )
                    if state["status"] not in ("closed", "completed"):
                        result["issues"].append(f"{region}: regional state is {state['status']}")
                if result["chunks"] == 0:
                    destination.write(gzip.compress(b"", mtime=0))
                destination.flush()
                os.fsync(destination.fileno())
            result["complete"] = not result["issues"]
            result["file"] = "observations.jsonl.gz"
            result["sha256"] = digest(partial)
            # The manifest is the commit marker. Invalidate it before replacing the data.
            (output / "manifest.json").unlink(missing_ok=True)
            partial.replace(output / result["file"])
            atomic_bytes(output / "report.html", render_report(manifest, result))
            atomic_json(output / "manifest.json", result)
        except BaseException:
            # Leave an explicitly partial export for diagnosis, never publish it.
            raise
    return result
