"""The value component by place (methodology [value] basis = "rank", VALUE-BY-RANK-1).

A member's cost is its HTTP cost per 1,000 successful requests: the lowest list $/GB
(price_per_gb_lowest) x the window's wire GB per 1,000 requests ((bytes_up + bytes_down) /
completed x 1,000 / 10^9) / its request success rate, the site API's wireCostPer1k. Every
member of the peer group with a cost is ranked, cheapest first, whatever its freshness (so the
rank is the API's valueRank); ties share the rank (1 + members strictly cheaper). With N ranked
members, rank r scores 100 x (N - r + 1) / N. A member without a cost is null with its reason
and neither ranked nor counted in N.
"""

from __future__ import annotations

GB = 1e9
COST_MISSING = "No wire bytes or successful requests in this window to price 1,000 requests"


def cost_per_1k(price, metrics):
    """Dollars per 1,000 successful requests at price $/GB from a window's metrics (request
    success rate, success_rate in older metrics); None without a price, wire bytes or
    successes."""
    m = metrics or {}
    rate = m.get("request_success_rate")
    rate = m.get("success_rate") if rate is None else rate
    up, down, completed = m.get("bytes_up"), m.get("bytes_down"), m.get("completed")
    if price is None or up is None or down is None or not completed or not rate or rate <= 0:
        return None
    return (up + down) / completed * 1000 / GB * price / rate


def by_rank(raw, peers):
    """Value components scored by place: raw are the members' value components from
    product_inputs() (the lowest price as value, the cost in inputs["cost_per_1k"]); peers is
    the leader() count kept in the inputs (Score peers). Each scored component's inputs hold the
    price, the cost, its rank and N ("ranked")."""
    costs = [_cost(c) for c in raw]
    priced = [cost for cost in costs if cost is not None]
    n = len(priced)
    out = []
    for c, cost in zip(raw, costs, strict=True):
        rank = None if cost is None else 1 + sum(x < cost for x in priced)
        missing = c["value"] is not None and cost is None
        out.append(
            {
                "value": None if rank is None else 100 * (n - rank + 1) / n,
                "state": c["state"] if rank is not None else "unavailable",
                "reason": COST_MISSING if missing else c["reason"],
                "inputs": {
                    "value": c["value"],
                    "cost_per_1k": cost,
                    "rank": rank,
                    "ranked": n,
                    "peers": peers,
                },
            }
        )
    return out


def _cost(c):
    """A raw value component's cost (None without a price)."""
    return (c.get("inputs") or {}).get("cost_per_1k") if c["value"] is not None else None
