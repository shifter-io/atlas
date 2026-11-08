"""SIM-GEN-1: the browser runs of a generated hour (SIMULATE.md, "Browser runs"): each queued run's
tries, with the profile's outcome counts, bytes per load, page loads and bundle bytes, sealed
through browser_model.bundle."""

from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import NormalDist
from typing import Any

import browser_model
from synthetic import JITTER, apportion, key

BUNDLE_BASE = 16_000  # bytes of a sealed bundle of 100 tries without a screenshot
PAGE_LOAD_MS = (2500, 4500)  # p50 and p90 of a target the reference does not give
LOAD_JITTER = 0.03  # a run's median page load varies by up to this share from day to day
# Each outcome of calculated/browser.py as the error code a try records (ok: none).
CODES = {"ok": None, "challenge": "challenge", "block": "blocked", "login_wall": "login_wall"}
CODES |= {"timeout": "timeout", "proxy_error": "proxy_connect", "no_content": "no_content"}
CODES |= {"no_answer": "no_answer", "other_error": "browser_error"}
OUTCOME = {code: name for name, code in CODES.items()}


def dig(value, *keys) -> Any:
    """value[k1][k2]...; {} where a key is missing or a value is null."""
    for name in keys:
        value = (value or {}).get(name)
    return value if value is not None else {}


def outcomes(profile: dict, network: str, target: str, engine: str) -> dict[str, int]:
    """A target's tries per outcome: the reference's nine, else the profile's (its unsplit tries
    count as other_error), else BROWSER-MODEL-1's measured hour."""
    found = dict(dig(profile, "reference", "browser", "targets", network, target, engine))
    found = dict(found.get("outcomes") or {})
    if not any(found.values()):
        found = dict(dig(profile, "networks", network, "browser", target, engine, "outcomes"))
        found["other_error"] = (found.pop("unsplit", 0) or 0) + (found.get("other_error") or 0)
    if not any(found.values()):
        found = Counter()
        for code, count in browser_model.MEASURED[target].outcomes.items():
            found[OUTCOME.get(code, "other_error")] += count
    return {name: found.get(name) or 0 for name in CODES}


def page_load(profile: dict, network: str, target: str, engine: str) -> tuple[float, float]:
    """(p50, p90) of the target's successful page loads: the reference's, else PAGE_LOAD_MS."""
    found = dig(profile, "reference", "browser", "targets", network, target, engine)
    p50, p90 = (dig(found, "page_load_ms").get(name) for name in ("p50", "p90"))
    return (p50, max(p50, p90)) if p50 and p90 else PAGE_LOAD_MS


@dataclass(frozen=True)
class Browser:
    """One queued browser run: its tries as (error code, bytes, page load ms, screenshot bytes)."""

    host: str
    network: str
    product: str
    target: str
    engine: str
    country: str
    start: datetime
    day: int
    tries: tuple = field(repr=False)

    @property
    def bundle_bytes(self) -> int:
        return BUNDLE_BASE + sum(shot for *_, shot in self.tries)

    def outcomes(self) -> Counter:
        return Counter(OUTCOME[code] for code, *_ in self.tries)

    def tables(self) -> dict[str, int]:
        """The rows the run is sure to add: the ingest decides the others."""
        return {"test_runs": 1, "batch_runs": 1}

    def bundle(self, run_row: dict, seed, out: Path) -> Path:
        """The run's sealed bundle (browser_model.bundle) for its planned batch run row."""
        return browser_model.bundle(run_row, self.target, seed, out, tries=self.tries)


def run(model, entry, day: int) -> Browser:
    """A queue entry's run on `day` of the model (simulate_model.Model)."""
    network, product = entry.network.split("/", 1)
    settings, target, profile = model.plan.browser, entry.target, model.profile
    engine = settings.engine(target)
    count = next(item.tries for item in settings.targets if item.id == target)
    rng = random.Random(key(model.seed, "browser", entry.network, target, entry.country, day))
    weights = outcomes(profile, network, target, engine)
    counts = dict(zip(weights, apportion(count, list(weights.values())), strict=True))
    counts[max(counts, key=counts.get)] -= 1  # one try moves: a day differs by about a point
    counts[rng.choice([name for name, weight in weights.items() if weight])] += 1
    codes = [CODES[name] for name, n in counts.items() for _ in range(n)]
    rng.shuffle(codes)
    own = dig(profile, "networks", network, "browser", target, engine)
    mean = dig(own, "bytes_per_load").get("mean") or browser_model.MEASURED[target].mean_mb * 1e6
    mean *= model.jitter("bytes", entry.network, target, day)
    sizes = apportion(round(mean * count), [rng.randint(50, 150) for _ in codes])
    p50, p90 = page_load(profile, network, target, engine)
    median = p50 * model.jitter("load", entry.network, target, day, spread=LOAD_JITTER)
    loads = iter(_loads(rng, median, p90 / p50, codes.count(None)))
    bundle = dig(own, "bundle_bytes").get("mean") or 0
    bundle *= model.jitter("bundle", entry.network, target, day, spread=JITTER / 2)
    failed = len(codes) - codes.count(None)
    shots = iter(apportion(max(0, round(bundle) - BUNDLE_BASE), [1] * failed) if failed else [])
    tries = tuple(
        (code, size, next(loads), 0) if code is None else (code, size, round(median), next(shots))
        for code, size in zip(codes, sizes, strict=True)
    )
    where = (entry.host, network, product, target, engine, entry.country, entry.start, day)
    return Browser(*where, tries)


def _loads(rng: random.Random, median: float, ratio: float, ok: int) -> list[int]:
    """ok page loads, lognormal with this median and p90 / p50 ratio, in a shuffled order."""
    scale = math.log(ratio) / NormalDist().inv_cdf(0.9)
    found = [NormalDist().inv_cdf((j + 0.5) / ok) for j in range(ok)]
    found = [round(median * math.exp(scale * z)) for z in found]
    rng.shuffle(found)
    return found
