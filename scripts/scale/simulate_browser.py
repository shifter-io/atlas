"""SIM-GEN-1: the browser runs of a generated hour (SIMULATE.md, "Browser runs"): each queued run's
tries, with the network's outcome mix (mix(): the profile's outcome counts moved per network,
BROWSER-SPREAD-1), bytes per load, page loads and bundle bytes, sealed through
browser_model.bundle."""

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
from synthetic import JITTER, apportion, key, signed_unit

BUNDLE_BASE = 16_000  # bytes of a sealed bundle of 100 tries without a screenshot
PAGE_LOAD_MS = (2500, 4500)  # p50 and p90 of a target the reference does not give
WEEK = 7  # a run's bytes per page load: the day's seventh of the spread (run_bytes)
LOAD_JITTER = 0.03  # a run's median page load varies by up to this share from day to day
# Each outcome of calculated/browser.py as the error code a try records (ok: none).
CODES = {"ok": None, "challenge": "challenge", "block": "blocked", "login_wall": "login_wall"}
CODES |= {"timeout": "timeout", "proxy_error": "proxy_connect", "no_content": "no_content"}
CODES |= {"no_answer": "no_answer", "other_error": "browser_error"}
OUTCOME = {code: name for name, code in CODES.items()}
# BROWSER-SPREAD-1 (#875): a network's success share is its source's moved by up to SPREAD,
# within BOUNDS, and each failure outcome by up to FAILURE_SPREAD of itself (mix()).
MODEL = "per-network-v1"
SPREAD = 0.18
FAILURE_SPREAD = 0.10
BOUNDS = (0.02, 0.98)
PARTS = 1_000_000  # mix()'s weights are parts per million of the tries


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


def real_rates(profile: dict, target: str, engine: str) -> dict[str, float]:
    """The success share of each network with real tries of the target: the reference's, else
    the profile's, of the engine when there are, else of the target's first other engine."""
    reference, found = dig(profile, "reference", "browser", "targets"), {}
    for network in sorted(set(dig(profile, "networks")) | set(reference)):
        engines = dig(reference, network, target) or dig(
            profile, "networks", network, "browser", target
        )
        tried = [dig(engines, engine, "outcomes")]
        tried += [dig(own, "outcomes") for _, own in sorted(engines.items())]
        counts = next((own for own in tried if sum(n or 0 for n in own.values())), {})
        if counts:
            found[network] = (counts.get("ok") or 0) / sum(n or 0 for n in counts.values())
    return found


def offset(profile: dict, network: str, target: str, engine: str) -> float:
    """2u - 1 of the network, u from md5(network:target:engine). Where the profile has real
    success shares of the target, the networks' values are handed out in their order instead,
    the lowest to the lowest share (ties by the value), so the generated order is the real one."""
    unit = signed_unit(f"{network}:{target}:{engine}")
    real = real_rates(profile, target, engine)
    if network not in real:
        return unit
    units = {name: signed_unit(f"{name}:{target}:{engine}") for name in real}
    order = sorted(real, key=lambda name: (real[name], units[name]))
    return sorted(units.values())[order.index(network)]


def base(profile: dict, target: str, engine: str) -> float | None:
    """The owner's real success share of the target and engine the profile's `browser_bases`
    gives (BROWSER-AI-SPREAD-1, #887: a private file merged in, never committed), else None."""
    found = dig(profile, "browser_bases", target).get(engine)
    return None if found is None else float(found)


def mix(profile: dict, network: str, target: str, engine: str) -> dict[str, int]:
    """The network's tries per outcome, in parts per million (SIMULATE.md, "Browser runs"): the
    source's success share (outcomes(); base() where it gives one) plus SPREAD x offset(),
    within BOUNDS; its failures keep their mix, each moved by FAILURE_SPREAD x u of itself
    (md5), rescaled to the rest. A source without a success (none measured) and no base()
    keeps its mix and no success."""
    found = outcomes(profile, network, target, engine)
    total, real = sum(found.values()), base(profile, target, engine)
    if not found["ok"] and real is None:
        return {name: round(PARTS * n / total) for name, n in found.items()}
    low, high = BOUNDS
    real = found["ok"] / total if real is None else real
    ok = real + SPREAD * offset(profile, network, target, engine)
    ok = min(high, max(low, ok))
    failed = {
        name: n * (1 + FAILURE_SPREAD * signed_unit(f"{network}:{target}:{engine}:{name}"))
        for name, n in found.items()
        if name != "ok" and n
    } or {"other_error": 1.0}
    rest = sum(failed.values())
    shares = {"ok": ok} | {name: (1 - ok) * n / rest for name, n in failed.items()}
    return {name: round(PARTS * shares.get(name, 0)) for name in CODES}


def bytes_spread(profile: dict, network: str, target: str, engine: str) -> tuple[dict, str]:
    """(the `bytes_per_load` spread a run's bytes per page load follow, its basis): the
    network's; without a p50 (a network whose runs recorded no bytes has 0), the other network's
    of the target and engine with the median p50; else the measured mean, a p50 alone (basis
    BP-VERIFY-2, or X-TARGET-1 for x_web)."""
    own = dig(profile, "networks", network, "browser", target, engine, "bytes_per_load")
    if own.get("p50"):
        return own, "network"
    found = [
        spread
        for entry in dig(profile, "networks").values()
        if (spread := dig(entry, "browser", target, engine, "bytes_per_load")).get("p50")
    ]
    if found:
        return sorted(found, key=lambda s: s["p50"])[(len(found) - 1) // 2], "target"
    basis = "BP-VERIFY-2" if target in browser_model.HOUR_147 else "X-TARGET-1"
    return {"p50": browser_model.MEASURED[target].mean_mb * 1e6}, basis


def bytes_per_load(profile: dict, network: str, target: str, engine: str) -> tuple[float, str]:
    """(bytes per page load, its basis): bytes_spread's p50."""
    found, basis = bytes_spread(profile, network, target, engine)
    return found["p50"], basis


def run_bytes(model, network: str, target: str, engine: str, day: int) -> float:
    """A run's bytes per page load: a log-normal through bytes_spread's p50, as wide as its p10
    and p90 (either alone without the other). Each day of a week takes another seventh of it,
    in an order fixed by the seed, so a week's runs have the spread's median and band."""
    found = bytes_spread(model.profile, network.split("/")[0], target, engine)[0]
    p50, z90 = found["p50"], NormalDist().inv_cdf(0.9)
    sides = [math.log(found[k] / p50) / z for k, z in (("p90", z90), ("p10", -z90)) if found.get(k)]
    sigma = max(0.0, sum(sides) / len(sides)) if sides else 0.0
    order = sorted(range(WEEK), key=lambda d: key(model.seed, "bytes", network, target, d))
    u = (order.index((day - 1) % WEEK) + 0.5) / WEEK
    return p50 * math.exp(sigma * NormalDist().inv_cdf(u))


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
    def loads(self) -> int:
        return len(self.tries)

    @property
    def bytes_per_load(self) -> float:
        return sum(size for _, size, *_ in self.tries) / len(self.tries)

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
    weights = mix(profile, network, target, engine)
    counts = dict(zip(weights, apportion(count, list(weights.values())), strict=True))
    counts[max(counts, key=counts.get)] -= 1  # one try moves: a day differs by about a point
    counts[rng.choice([name for name, weight in weights.items() if weight])] += 1
    codes = [CODES[name] for name, n in counts.items() for _ in range(n)]
    rng.shuffle(codes)
    own = dig(profile, "networks", network, "browser", target, engine)
    mean = run_bytes(model, entry.network, target, engine, day)
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
