#!/usr/bin/env python3
"""Verify every observation in one complete regional round and combine its chunks."""

import argparse
import gzip
import html
import json
import os
import shutil
from pathlib import Path

from proxy_index.onboarding.export import verify_chunk
from proxy_index.onboarding.files import (
    atomic_bytes,
    atomic_json,
    digest,
    private_dir,
    read_json,
)
from proxy_index.onboarding.manifest import load, moment
from proxy_index.onboarding.scheduler import job_directory, validate_state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--round", type=int, default=0)
    args = parser.parse_args()
    manifest = load(args.manifest)
    region = args.manifest.parent / "regions" / args.region
    state = read_json(region / "state.json")
    validate_state(state, manifest, args.region)
    jobs = [j for j in state["jobs"] if j["round"] == args.round]
    if (
        not jobs
        or [j["country"] for j in jobs] != manifest["regions"][args.region]
        or any(j["status"] != "completed" for j in jobs)
    ):
        raise ValueError("Round is incomplete")
    output = args.manifest.parent / "export" / args.region / f"round-{args.round:02d}"
    private_dir(output)
    partial = output / "observations.jsonl.gz.partial"
    results = []
    previous_end = None
    with partial.open("wb") as combined:
        os.chmod(partial, 0o600)
        for job in jobs:
            folder = job_directory(region, job)
            saved = read_json(folder / "summary.json")
            measured = saved["measurement"]
            if previous_end and moment(job["started_at"]) < previous_end:
                raise ValueError("Country runs overlap")
            previous_end = moment(job["finished_at"])
            if list(folder.glob("*.partial")):
                raise ValueError("Unfinished country chunk")
            count = chunks = 0
            unique = set()
            verified = set()
            for meta_path in sorted(folder.glob("observations-*.jsonl.meta.json")):
                meta = read_json(meta_path)
                if Path(meta["file"]).name != meta["file"]:
                    raise ValueError("Invalid chunk filename")
                chunk = folder / meta["file"]
                count += verify_chunk(chunk, meta, job, job["finished_at"])
                with gzip.open(chunk, "rt") as stream:
                    for line in stream:
                        unique.add(json.loads(line)["ip"])
                with chunk.open("rb") as source:
                    shutil.copyfileobj(source, combined)
                verified.add(chunk.name)
                chunks += 1
            if verified != {p.name for p in folder.glob("observations-*.jsonl.gz")}:
                raise ValueError("Unverified country chunk")
            if (
                count != measured["successes"]
                or count != job["observations"]
                or len(unique) != measured["unique_ips"]
                or measured["completed"] != measured["successes"] + measured["total_errors"]
                or measured["cancelled"] != 0
            ):
                raise ValueError("Country measurements do not reconcile")
            results.append(
                {
                    "country": job["country"],
                    "observations": count,
                    "chunks": chunks,
                    "unique_ips": len(unique),
                    "requests": measured["completed"],
                    "errors": measured["total_errors"],
                    "stop_reason": measured["stop_reason"],
                    "duration_seconds": job["duration_seconds"],
                }
            )
        if combined.tell() == 0:
            combined.write(gzip.compress(b"", mtime=0))
        combined.flush()
        os.fsync(combined.fileno())
    partial.replace(output / "observations.jsonl.gz")
    # Re-read the combined stream, including every gzip member, to validate its row total.
    with gzip.open(output / "observations.jsonl.gz", "rt") as stream:
        rows = sum(1 for _ in stream)
    assert rows == sum(r["observations"] for r in results)
    result = {
        "verified": True,
        "schema_version": 1,
        "region": args.region,
        "round": args.round,
        "campaign_id": manifest["campaign_id"],
        "countries": results,
        "rows": rows,
        "duration_seconds": (
            moment(jobs[-1]["finished_at"]) - moment(jobs[0]["started_at"])
        ).total_seconds(),
        "file": "observations.jsonl.gz",
        "sha256": digest(output / "observations.jsonl.gz"),
    }
    atomic_json(output / "manifest.json", result)
    table = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(str(r[k]))}</td>"
            for k in (
                "country",
                "duration_seconds",
                "requests",
                "observations",
                "unique_ips",
                "errors",
            )
        )
        + "</tr>"
        for r in results
    )
    page = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Regional round validation</title>
<style>body{{font:16px/1.6 system-ui;max-width:1100px;margin:40px auto;padding:0 24px;
background:#f4f7fa;color:#203040}}table{{border-collapse:collapse;background:white;width:100%}}
th,td{{padding:12px;text-align:left;border-bottom:1px solid #ccd8e0}}
.scroll{{overflow:auto}}</style>
<h1>{html.escape(args.region)} · regional round verified</h1>
<p>Duration: {result["duration_seconds"]:.2f} seconds · {rows:,} successful observations.</p>
<div class="scroll"><table><tr><th>Country requested</th><th>Seconds</th><th>Requests</th>
<th>Observations</th><th>Unique IPs</th><th>Errors</th></tr>{table}</table></div>
<p>All chunks passed gzip, JSON schema, IP syntax, country/timestamp, checksum and count checks.
Exact unique-IP totals reconcile with each country summary. No partial chunks or cancelled requests.
Countries ran sequentially in the frozen configuration order.
Duplicate observations remain intact.</p>
<p>Country codes describe requested targeting; this format does not assert geolocated country.</p>
<p><a href="manifest.json">Verification manifest and checksum</a> ·
<a href="observations.jsonl.gz">Combined round observations</a></p></html>"""
    atomic_bytes(output / "report.html", page.encode())
    print(json.dumps(result))


if __name__ == "__main__":
    main()
