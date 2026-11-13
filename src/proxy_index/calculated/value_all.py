"""The value component by place on the all-targets cost (methodology [value] basis =
"rank_all_targets", VALUE-ALL-RANK-1).

A member's all-targets cost is its bill per 1,000 successful requests over the card targets
(card_targets(): the HTTP targets and every browser target or group that is not a group's
source, 8 today), equal weights: each target's cost is every wire byte of its finished tries,
failed ones too, at the lowest list $/GB (GB = 10^9 bytes) / its successes x 1,000. A target
without a run, successes or wire bytes has no cost of its own. When every target has a cost the
bill is their plain mean; when the only missing targets have tries but no successes, each is
costed at the highest cost any member of the peer group has on it in the window and the bill,
the mean over all the card targets with those costs, is a lower bound ("cost_is_lower_bound").
Otherwise (no price, a target without a run or wire bytes, or no member with a cost on a missing
target) the member is unpriced: null with its reason and not counted in N. Every priced member
is ranked, cheapest first, whatever its freshness; ties share the rank (1 + members strictly
cheaper). With N ranked, rank r scores 100 x (N - r + 1) / N. The component publishes the bill,
the partial bill over the counted targets (some, not all), the counted and missing targets, the
lower-bound flag, the rank and N, which the site API serves (costPer1kAllTargets and the rest).
This is the API's billOver (api/internal/build/value_all.go), ported; a cross-check fixture
(api/internal/build/testdata/value_all_crosscheck.json) holds both to the same results.
"""

from __future__ import annotations

from .policy import browser_difficulty, http_difficulty

GB = 1e9
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
    """(cost per 1,000 successes, None) of one target's window metrics at price $/GB, else
    (None, the reason): no run, no successes, no wire bytes, then no price."""
    m = metrics or {}
    tries, successes = _count(m.get("completed")), _count(m.get("successes"))
    up, down = m.get("bytes_up"), m.get("bytes_down")
    measured = up is not None and down is not None and up + down > 0
    if not tries:
        return None, NO_RUN
    if not successes:
        return None, NO_SUCCESSES
    if not measured:
        return None, NOT_MEASURED
    if price is None:
        return None, CATALOG
    return price * (up + down) / GB / successes * 1000, None


def target_costs(price, targets, config):
    """Each card target's [cost, reason] from a window's targets ({target: {"metrics": ...}}),
    in card order: the raw value component's inputs["all_targets"]."""
    targets = targets or {}
    return {
        t: list(target_cost(price, (targets.get(t) or {}).get("metrics")))
        for t in card_targets(config)
    }


def bill(costs):
    """(bill, partial bill, counted, missing) of one member's target_costs(): the plain mean
    when every target has a cost, else the mean over the counted ones when some do; missing are
    the targets without a cost for a measured reason (a missing price is not theirs)."""
    counted = [cost for cost, _ in costs.values() if cost is not None]
    missing = [t for t, (cost, why) in costs.items() if cost is None and why != CATALOG]
    mean = sum(counted) / len(counted) if counted else None
    complete = len(counted) == len(costs)
    return (mean if complete else None), (None if complete else mean), len(counted), missing


def by_rank_all(raw, peers):
    """Value components scored by place on the all-targets cost: raw are the members'
    value components from product_inputs() (the lowest price as value, target_costs() in
    inputs["all_targets"]); peers is the leader() count kept in the inputs."""
    found = [_figures(c) for c in raw]
    worst = _worst([f["costs"] for f in found])
    for c, f in zip(raw, found, strict=True):
        if c["value"] is not None:  # no lower bound without a price
            _lower_bound(f, worst)
    priced = [f["cost"] for f in found if f["cost"] is not None]
    n = len(priced)
    out = []
    for c, f in zip(raw, found, strict=True):
        rank = None if f["cost"] is None else 1 + sum(x < f["cost"] for x in priced)
        out.append(
            {
                "value": None if rank is None else 100 * (n - rank + 1) / n,
                "state": c["state"] if rank is not None else "unavailable",
                "reason": COST_MISSING if c["value"] is not None and rank is None else c["reason"],
                "inputs": {
                    "value": c["value"],
                    "cost_per_1k_all_targets": f["cost"],
                    "partial_cost_per_1k_all_targets": f["partial"],
                    "targets_counted_all_targets": f["counted"],
                    "missing_targets_all_targets": f["missing"],
                    "cost_is_lower_bound": f["lower"],
                    "rank": rank,
                    "ranked": n,
                    "peers": peers,
                },
            }
        )
    return out


def _figures(c):
    """A raw value component's target costs (target_costs()) and bill."""
    costs = (c.get("inputs") or {}).get("all_targets") or {}
    cost, partial, counted, missing = bill(costs) if costs else (None, None, 0, [])
    return {
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
