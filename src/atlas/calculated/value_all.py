"""The value component on the all-targets cost (methodology [value] basis = "rank_all_targets",
VALUE-ALL-RANK-1; the standard page and proportional score of VALUE-FAIL-1, the owner's choice of
2026-11-18).

A member's all-targets cost is its bill per 1,000 successful requests over the card targets
(card_targets(): the HTTP targets and every browser target or group that is not a group's
source, 8 today), equal weights. Each target's cost prices one standard page per finished try,
failed ones too: price $/GB x the target's standard page (the median wire bytes per try of the
peer group's members that measured it; GB = 10^9 bytes) x its tries / its successes x 1,000. So
every failure costs a full page, a retry, and a member whose failures move few bytes gains
nothing from it. A target without a run, successes or wire bytes has no cost of its own. When
every target has a cost the bill is their plain mean; when the only missing targets have tries but
no successes, each is costed at the highest cost any member of the peer group has on it in the
window and the bill, the mean over all the card targets with those costs, is a lower bound
("cost_is_lower_bound"). Otherwise (no price, a target without a run or wire bytes, or no member
with a cost on a missing target) the member is unpriced: null with its reason. Every priced
member is ranked, cheapest first, whatever its freshness; ties share the rank (1 + members
strictly cheaper). Its value is proportional to its cost: 50 + 50 x log(typical / its cost) /
log(4), from 0 to 100, typical being the median bill of the priced members: the typical bill
scores 50, each halving of the bill adds 25 points, 4 times cheaper or dearer than typical is 100
or 0; so a small change in a bill is a small change in its value, and one member joining moves the
others little. The component publishes the bill, the partial bill over the counted targets (some,
not all), the counted and missing targets, the lower-bound flag, the rank and N, and the standard
pages ("page_bytes_all_targets"), which the site API serves (costPer1kAllTargets and the rest) and
prices each target's detail with. This is the API's billOver (api/internal/build/value_all.go),
ported; a cross-check fixture (api/internal/build/testdata/value_all_crosscheck.json) holds both
to the same results.

With [value] browser_window (COVERAGE-ALERT-1, "7d"), a shorter window (24h) prices its browser
card targets over that window instead of its own, its HTTP targets staying its own: the browser
queue runs each (network, target) once a UTC day at a shifting hour, so a 24h window holds all of
them in only a third of the builds (reprice()). A browser target with no run in the shorter
window is carried over: the component lists it with the hours since its last observation in the
wider window ("carried_over_all_targets") and is "stale"; with no run in the wider window either,
it is missing as before. The component names the window ("browser_window", None when its
own window priced every target), so the site API costs each target the same way.
"""

from __future__ import annotations

import math
from statistics import median

from .policy import browser_difficulty, http_difficulty, value_browser_window

GB = 1e9
HOURS = {"24h": 24, "7d": 168, "30d": 720}  # a window label's length
# The proportional value: a bill SPAN times cheaper than the typical one scores 100, SPAN times
# dearer 0.
SPAN = 4
# Why a target has no cost of its own (the API's naReason codes).
NO_RUN, NO_SUCCESSES, NOT_MEASURED, CATALOG = "no_run", "no_successes", "not_measured", "catalog"
COST_MISSING = (
    "A card target without a run or wire bytes, or without a cost on any network, to price "
    "1,000 successful requests over the card targets"
)


def card_targets(config):
    """The card targets: the HTTP targets, then each browser target or group that is not a
    group's source (the targets of the all-targets success score)."""
    sources = {s for members in config.get("target_groups", {}).values() for s in members}
    browser = [t for t in browser_difficulty(config) if t not in sources]
    return [*http_difficulty(config), *browser]


def target_cost(price, metrics):
    """[tries per success, reason, wire bytes per try] of one target's window metrics at price
    $/GB: the reason (no run, no successes, no wire bytes, then no price) when it has no cost of
    its own, the bytes per try whenever the target measured some (they set its standard page)."""
    m = metrics or {}
    tries, successes = _count(m.get("completed")), _count(m.get("successes"))
    up, down = m.get("bytes_up"), m.get("bytes_down")
    measured = up is not None and down is not None and up + down > 0
    per_try = (up + down) / tries if tries and measured else None
    if not tries:
        return [None, NO_RUN, None]
    if not successes:
        return [None, NO_SUCCESSES, per_try]
    if not measured:
        return [None, NOT_MEASURED, None]
    if price is None:
        return [None, CATALOG, per_try]
    return [tries / successes, None, per_try]


def target_costs(price, targets, config):
    """Each card target's target_cost() from a window's targets ({target: {"metrics": ...}}), in
    card order: the raw value component's inputs["all_targets"], priced by priced()."""
    targets = targets or {}
    return {
        t: target_cost(price, (targets.get(t) or {}).get("metrics")) for t in card_targets(config)
    }


def standard_pages(all_figures):
    """Each target's standard page: the median wire bytes per try of the members (target_costs()
    each) that measured it."""
    found = {}
    for figures in all_figures:
        for t, (_, _, per_try) in figures.items():
            if per_try is not None:
                found.setdefault(t, []).append(per_try)
    return {t: median(sizes) for t, sizes in found.items()}


def priced(price, figures, pages):
    """Each target's [cost per 1,000 successes, reason] of one member's target_costs(): price x
    the target's standard page x its tries per success."""
    return {
        t: [None, why] if why else [price * pages[t] / GB * ratio * 1000, None]
        for t, (ratio, why, _) in figures.items()
    }


def proportional(cost, typical):
    """The value of a bill against the typical bill: 50 + 50 x log(typical / cost) / log(SPAN),
    from 0 to 100."""
    if cost <= 0:
        return 100.0
    return max(0.0, min(100.0, 50 + 50 * math.log(typical / cost) / math.log(SPAN)))


def wider(label, config):
    """The [value] browser_window when it is longer than window `label`, else None."""
    wide = value_browser_window(config)
    return wide if wide and HOURS[wide] > HOURS[label] else None


def reprice(value, own, wide, config, as_of):
    """Price value component `value`'s browser card targets from `wide` (the browser window's
    targets) instead of `own` (its window's), in place, and list the ones carried over: no run in
    its window, one in the wider, with the hours since its last observation there."""
    targets, carried = dict(own or {}), {}
    for t in [t for t in card_targets(config) if t not in http_difficulty(config)]:
        targets[t] = (wide or {}).get(t)
        before, after = (_metrics(own, t), _metrics(targets, t))
        if not _count(before.get("completed")) and _count(after.get("completed")):
            carried[t] = round((as_of - after["last_observed"]).total_seconds() / 3600, 1)
    value["inputs"]["all_targets"] = target_costs(value["value"], targets, config)
    value["inputs"]["carried_over_all_targets"] = carried
    value["inputs"]["browser_window"] = value_browser_window(config)


def reprice_windows(windows, values, config, as_of):
    """reprice() each window's value component (values: {label: component}) whose window is
    shorter than the browser window, from windows ({label: {"targets": ...}})."""
    for label, value in values.items():
        wide = wider(label, config)
        if wide in windows and value.get("inputs"):
            reprice(value, windows[label]["targets"], windows[wide]["targets"], config, as_of)


def _metrics(targets, target):
    return ((targets or {}).get(target) or {}).get("metrics") or {}


def bill(costs):
    """(bill, partial bill, counted, missing) of one member's priced() costs: the plain mean
    when every target has a cost, else the mean over the counted ones when some do; missing are
    the targets without a cost for a measured reason (a missing price is not theirs)."""
    counted = [cost for cost, _ in costs.values() if cost is not None]
    missing = [t for t, (cost, why) in costs.items() if cost is None and why != CATALOG]
    mean = sum(counted) / len(counted) if counted else None
    complete = len(counted) == len(costs)
    return (mean if complete else None), (None if complete else mean), len(counted), missing


def by_rank_all(raw, peers):
    """Value components on the all-targets cost: raw are the members' value components from
    product_inputs() (the price as value, target_costs() in inputs["all_targets"]); peers is the
    leader() count kept in the inputs. The value is proportional() to the median bill."""
    pages = standard_pages([(c.get("inputs") or {}).get("all_targets") or {} for c in raw])
    found = [_figures(c, pages) for c in raw]
    worst = _worst([f["costs"] for f in found])
    for c, f in zip(raw, found, strict=True):
        if c["value"] is not None:  # no lower bound without a price
            _lower_bound(f, worst)
    billed = [f["cost"] for f in found if f["cost"] is not None]
    n, typical = len(billed), median(billed) if billed else None
    out = []
    for c, f in zip(raw, found, strict=True):
        rank = None if f["cost"] is None else 1 + sum(x < f["cost"] for x in billed)
        out.append(
            {
                "value": None if rank is None else proportional(f["cost"], typical),
                "state": c["state"] if rank is not None else "unavailable",
                "reason": COST_MISSING if c["value"] is not None and rank is None else c["reason"],
                "inputs": {
                    "value": c["value"],
                    "cost_per_1k_all_targets": f["cost"],
                    "partial_cost_per_1k_all_targets": f["partial"],
                    "targets_counted_all_targets": f["counted"],
                    "missing_targets_all_targets": f["missing"],
                    "cost_is_lower_bound": f["lower"],
                    "browser_window": f["window"],
                    "carried_over_all_targets": f["carried"],
                    "stale": bool(f["carried"]),
                    "page_bytes_all_targets": pages,
                    "rank": rank,
                    "ranked": n,
                    "peers": peers,
                },
            }
        )
    return out


def _figures(c, pages):
    """A raw value component's target costs (priced() at the standard pages) and bill."""
    figures = (c.get("inputs") or {}).get("all_targets") or {}
    costs = priced(c["value"], figures, pages)
    cost, partial, counted, missing = bill(costs) if costs else (None, None, 0, [])
    carried = (c.get("inputs") or {}).get("carried_over_all_targets") or {}
    if c["value"] is None:  # nothing is carried over into a component without a price
        carried = {}
    return {
        "window": (c.get("inputs") or {}).get("browser_window"),
        "carried": {t: hours for t, hours in carried.items() if t not in missing},
        "costs": costs,
        "cost": cost,
        "partial": partial,
        "counted": counted,
        "missing": missing,
        "lower": False,
    }


def _worst(all_costs):
    """The highest cost per target among the members that have one."""
    worst = {}
    for costs in all_costs:
        for t, (cost, _) in costs.items():
            if cost is not None and (t not in worst or cost > worst[t]):
                worst[t] = cost
    return worst


def _lower_bound(f, worst):
    """A priced member whose only missing targets had tries but no successes: the mean over
    every card target, each missing one at the worst cost of the group on it."""
    costs, missing = f["costs"], f["missing"]
    if f["cost"] is not None or not missing:
        return
    if any(costs[t][1] != NO_SUCCESSES or t not in worst for t in missing):
        return
    total = sum(worst[t] if cost is None else cost for t, (cost, _) in costs.items())
    f["cost"], f["lower"] = total / len(costs), True


def _count(n):
    """A metric's count, 0 when missing or negative."""
    return int(n) if n is not None and n > 0 else 0
