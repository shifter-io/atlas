"""Per-hour traffic reports per network (D5) and the cumulative `reports/traffic.tsv`.

Bytes, requests and outcomes are summed from the hour's worker records (the framework's
client-side wire counts); countries come from the harvest's per-network `traffic`. No IPs.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from ..onboarding.files import atomic_bytes
from .contracts import CampaignSpec, DropletRecord, HourRecord, reports_dir
from .tick import FINAL, load_hours, load_spec

GB = 1e9
SUMS = ("bytes_up", "bytes_down", "requests", "successes", "errors")
TSV_COLUMNS = ("hour", "network", *SUMS, "countries_done", "countries_total", "regions_done",
               "worker_hours")
NOTE = ("Bytes are the framework's client-side wire counts (`bytes_up` and `bytes_down` of each "
        "country summary); a provider's own billing may differ slightly.")


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _span(start: str | None, end: str | None) -> float:
    first, last = _time(start), _time(end)
    return (last - first).total_seconds() if first and last else 0.0


def worker_hours(record: DropletRecord) -> float:
    """Droplet lifetime, from the create request to its destruction (or last known step)."""
    end = record.destroyed_at or record.collected_at or record.finished_at
    return _span(record.requested_at, end) / 3600


def network_rows(record: HourRecord, regions: int) -> dict[str, dict]:
    """One row per network of the hour, from its worker records and harvest totals."""
    rows: dict[str, dict] = {}
    for drop in record.droplets:
        row = rows.setdefault(drop.network, {key: 0 for key in SUMS} | {
            "worker_hours": 0.0, "regions": set(), "slowest": ("", 0.0)})
        values = asdict(drop)
        for key in SUMS:
            row[key] += values[key]
        row["worker_hours"] += worker_hours(drop)
        if drop.finished_at and drop.collected_at:
            row["regions"].add(drop.region)
        duration = _span(drop.started_at, drop.finished_at)
        if duration > row["slowest"][1]:
            row["slowest"] = (drop.region, duration)
    for network, row in rows.items():
        harvested = record.traffic.get(network, {})
        row["countries_done"] = int(harvested.get("countries_done", 0))
        row["countries_total"] = int(harvested.get("countries_total", 0))
        row["regions_done"] = f"{len(row.pop('regions'))}/{regions}"
    return dict(sorted(rows.items()))


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _gb(value: float) -> str:
    return f"{value / GB:.3f}"


def _traffic_table(rows: dict[str, dict], cumulative: dict[str, float]) -> list[str]:
    lines = ["| Network | GB up | GB down | GB total | Requests | Successes | Error % | "
             "Countries | Regions | Slowest region | Worker-hours | Cumulative GB |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|"]
    total = {key: sum(row[key] for row in rows.values()) for key in (*SUMS, "worker_hours")}
    for name, row in [*rows.items(), ("**Total**", total)]:
        done = row.get("errors", 0) + row.get("successes", 0)
        error = f"{100 * row['errors'] / done:.1f}" if done else "-"
        countries = (f"{row['countries_done']}/{row['countries_total']}"
                     if "countries_done" in row else "")
        region, seconds = row.get("slowest", ("", 0.0))
        slowest = f"{region} ({_duration(seconds)})" if region else ""
        cumulated = (cumulative.get(name, 0.0) if name in rows else sum(cumulative.values()))
        lines.append(
            f"| {name} | {_gb(row['bytes_up'])} | {_gb(row['bytes_down'])} | "
            f"{_gb(row['bytes_up'] + row['bytes_down'])} | {row['requests']} | "
            f"{row['successes']} | {error} | {countries} | {row.get('regions_done', '')} | "
            f"{slowest} | {row['worker_hours']:.2f} | {_gb(cumulated)} |")
    return lines


def _cpu_table(record: HourRecord) -> list[str]:
    """(v2.1) Per region, the worst worker's CPU, load and memory samples."""
    by_region: dict[str, list[DropletRecord]] = defaultdict(list)
    for drop in record.droplets:
        if drop.cpu_p50_pct is not None:
            by_region[drop.region].append(drop)
    lines = ["| Region | Workers | CPU p50 % | CPU p95 % | CPU max % | Load1 p95 | Memory max % |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for region, drops in by_region.items():
        samples = [asdict(drop) for drop in drops]
        worst = [max((sample[key] or 0.0) for sample in samples) for key in (
            "cpu_p50_pct", "cpu_p95_pct", "cpu_max_pct", "load1_p95", "mem_used_max_pct")]
        lines.append(f"| {region} | {len(drops)} | " + " | ".join(f"{v:.1f}" for v in worst) + " |")
    return lines if by_region else ["No CPU samples recorded."]


def _cumulative(hours: dict[int, HourRecord], upto: int) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for slot, record in hours.items():
        for drop in record.droplets if slot <= upto else ():
            totals[drop.network] += drop.bytes_up + drop.bytes_down
    return totals


def render(spec: CampaignSpec, record: HourRecord, hours: dict[int, HourRecord]) -> str:
    rows = network_rows(record, len(spec.regions))
    late = [d.start_lateness_seconds for d in record.droplets
            if d.start_lateness_seconds is not None]
    lateness = f"{min(late):.1f}–{max(late):.1f} s" if late else "not recorded"
    used = sorted({d.concurrency for d in record.droplets if d.concurrency} or {spec.concurrency})
    return "\n".join([
        f"# Campaign hour {record.hour:02d} — traffic per network", "",
        f"Campaign `{spec.campaign_id}`, hour scheduled {record.scheduled_at}, state "
        f"{record.state}. Start lateness: {lateness}. Concurrency per worker: "
        f"{', '.join(map(str, used))}. Launch delay: {record.launch_delay_seconds or 0:.1f} s.",
        "", *_traffic_table(rows, _cumulative(hours, record.hour)), "",
        "## CPU per region (worst worker)", "", *_cpu_table(record), "", NOTE, ""])


def tsv(hours: dict[int, HourRecord], regions: int) -> str:
    lines = ["\t".join(TSV_COLUMNS)]
    for slot, record in sorted(hours.items()):
        for network, row in network_rows(record, regions).items() if record.state in FINAL else ():
            values = [f"{slot:02d}", network, *(str(row[key]) for key in SUMS),
                      str(row["countries_done"]), str(row["countries_total"]),
                      row["regions_done"].split("/")[0], f"{row['worker_hours']:.3f}"]
            lines.append("\t".join(values))
    return "\n".join(lines) + "\n"


def write_traffic_report(root: Path, hour: int) -> Path:
    """Write `reports/traffic/hour-NN.md` and rebuild `reports/traffic.tsv` (every final hour)."""
    spec = load_spec(root)
    hours = load_hours(root, spec)
    path = reports_dir(root) / "traffic" / f"hour-{hour:02d}.md"
    atomic_bytes(path, render(spec, hours[hour], hours).encode())
    atomic_bytes(reports_dir(root) / "traffic.tsv", tsv(hours, len(spec.regions)).encode())
    return path


def latest_summary(hours: dict[int, HourRecord]) -> dict | None:
    """The newest reported hour: total bytes and the top network, for status.json."""
    reported = [record for record in hours.values() if record.report_path]
    if not reported:
        return None
    record = max(reported, key=lambda item: item.hour)
    totals = {net: row["bytes_up"] + row["bytes_down"]
              for net, row in network_rows(record, 0).items()}
    top = max(totals, key=totals.__getitem__) if totals else None
    return {"hour": record.hour, "path": record.report_path, "total_bytes": sum(totals.values()),
            "top_network": top, "top_bytes": totals.get(top, 0)}

