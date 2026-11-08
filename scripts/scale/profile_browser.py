"""SIM-PROFILE-1: the browser figures of BP-VERIFY-3's (and BP-VERIFY-4's) hour folders: each
folder's mgr.json lists the runs the manager knew that hour and archive samples; a run's state
in the last folder is its final one (SIMULATE.md, "Inputs")."""

from __future__ import annotations

import json
from pathlib import Path

from profile_hours import ProfileError, share

from proxy_index.browser.engines import DEFAULT_ENGINE
from proxy_index.calculated.browser import OUTCOMES

FINAL = ("done", "failed")
# The outcomes the manager's summary counts; the others are in the database (reference).
COUNTED = {
    "ok": "successes",
    "challenge": "challenges",
    "block": "blocks",
    "timeout": "timeouts",
    "proxy_error": "proxy_errors",
}
RUN_FIELDS = ("batch", "target", "provider", "product", "run_status", "requests", "bytes_down")
UNSPLIT = (
    "page_load_ms, and login_wall, no_content, no_answer and other_error, are not in the "
    "manager's hour summaries: `unsplit` holds those tries; profile.py reference reads them "
    "from the database (reference.browser.targets)"
)
SAMPLED = "bundle bytes: the manager's archive samples only (up to three per hour)"


def spread(values) -> dict:
    """Nearest-rank p10, p50 and p90, the mean and the count of a list of numbers."""
    values = sorted(values)
    if not values:
        return {"count": 0, "p10": None, "p50": None, "p90": None, "mean": None}

    def rank(percent: int):
        return values[max(0, -(-len(values) * percent // 100) - 1)]

    mean = round(sum(values) / len(values), 3)
    return {"count": len(values), "p10": rank(10), "p50": rank(50), "p90": rank(90), "mean": mean}


def read(sources: list[Path]) -> tuple[dict[tuple, dict], dict[tuple, int]]:
    """(each run's last state by (batch, provider, product), the archive sample bytes by
    (batch, provider)) of the hour folders of every source (BP-VERIFY-3, then BP-VERIFY-4),
    each source's folders in name order."""
    runs, samples = {}, {}
    for hours in sources:
        paths = sorted(hours.glob("*/mgr.json"))
        if not paths:
            raise ProfileError(f"{hours.name}: no hour folder with a mgr.json")
        for path in paths:
            try:
                _read_hour(json.loads(path.read_bytes()), runs, samples)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                raise ProfileError(f"{path.parent.name}/{path.name}: {error}") from None
    return runs, samples


def _read_hour(data: dict, runs: dict, samples: dict) -> None:
    for run in data["runs"]:
        missing = [field for field in RUN_FIELDS if field not in run]
        if missing:
            raise ProfileError(f"a run lacks {', '.join(missing)}")
        runs[(run["batch"], run["provider"], run["product"])] = run
    for sample in data.get("archive_sample", []):
        samples[(sample["batch"], sample["network"].split("/")[0])] = int(sample["bytes"])


def stored(run: dict) -> bool:
    return run["run_status"] in FINAL and bool(run.get("test_runs") or run.get("run_key"))


def target_entry(runs: list[dict], samples: list[int]) -> dict:
    """One target and engine of one network: its stored runs' tries, outcomes and bytes."""
    tries = sum(run["requests"] for run in runs)
    outcomes = dict.fromkeys(OUTCOMES)
    for name, field in COUNTED.items():
        outcomes[name] = sum(int(run.get(field, 0)) for run in runs)
    outcomes["unsplit"] = tries - sum(outcomes[name] for name in COUNTED)
    loads = [run["bytes_down"] / run["requests"] for run in runs if run["requests"]]
    return {
        "runs": len(runs),
        "tries": spread([run["requests"] for run in runs]),
        "outcomes": outcomes,
        "ok_share": share(outcomes["ok"], tries),
        "page_load_ms": {"p50": None, "p90": None},
        "bytes_down": sum(run["bytes_down"] for run in runs),
        "bytes_per_load": spread([round(value) for value in loads]),
        "bundle_bytes": spread(samples),
    }


def ingest(runs: list[dict]) -> dict:
    """What the manager recorded of the runs' ingest: stored runs, test_runs rows, statuses."""
    statuses: dict[str, int] = {}
    for run in runs:
        statuses[run["run_status"]] = statuses.get(run["run_status"], 0) + 1
    test_runs = sum(int(run.get("test_runs") or 0) for run in runs)
    kept = sum(map(stored, runs))
    return {"runs": len(runs), "stored": kept, "test_runs": test_runs, "statuses": statuses}


def network(runs: list[dict], samples: dict[tuple, int]) -> tuple[dict, dict]:
    """(target -> engine -> target_entry() of the stored runs with the ingest() of all, the
    network's real_24h browser totals) of one network's runs."""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for run in runs:
        grouped.setdefault((run["target"], run.get("engine") or DEFAULT_ENGINE), []).append(run)
    targets: dict[str, dict] = {}
    for (target, engine), own in sorted(grouped.items()):
        kept = [run for run in own if stored(run)]
        entry = target_entry(kept, sampled(kept, samples)) | {"ingest": ingest(own)}
        targets.setdefault(target, {})[engine] = entry
    kept = [run for run in runs if stored(run)]
    bundles = sampled(kept, samples)
    totals = {
        "browser_runs": len(kept),
        "browser_tries": sum(run["requests"] for run in kept),
        "browser_bytes_down": sum(run["bytes_down"] for run in kept),
        "bundle_bytes_sampled": sum(bundles),
        "bundle_samples": len(bundles),
    }
    return targets, totals


def sampled(runs: list[dict], samples: dict[tuple, int]) -> list[int]:
    keys = sorted({(run["batch"], run["provider"]) for run in runs})
    return [samples[key] for key in keys if key in samples]


def profile(sources: list[Path], wanted: list[str]) -> tuple[dict, dict, list[dict]]:
    """(network -> browser targets, network -> real_24h browser totals, gaps) of the wanted
    networks."""
    runs, samples = read(sources)
    by_network: dict[str, list[dict]] = {}
    left_out: dict[tuple[str, str, str], int] = {}
    for run in runs.values():
        by_network.setdefault(run["provider"], []).append(run)
        if not stored(run):
            state = "not stored" if run["run_status"] in FINAL else "unfinished"
            key = (run["provider"], run["target"], state)
            left_out[key] = left_out.get(key, 0) + 1
    gaps = [{"source": "browser", "what": UNSPLIT}, {"source": "browser", "what": SAMPLED}]
    for (name, target, state), count in sorted(left_out.items()):
        what = f"runs left out ({state}): {count}"
        gaps.append({"source": "browser", "network": name, "target": target, "what": what})
    targets, totals = {}, {}
    for name in (name for name in wanted if name in by_network):
        targets[name], totals[name] = network(by_network[name], samples)
    for name in wanted:
        if not totals.get(name, {}).get("browser_runs"):
            gaps.append({"source": "browser", "network": name, "what": "no stored browser run"})
    return targets, totals, gaps
