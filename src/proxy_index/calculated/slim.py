"""The published snapshot: the calculated payload with each long list cut to its top rows.

calculate() persists the full payload into the typed tables (calculated_breakdowns keeps every
breakdown row) and stores and serves this capped copy. Every total stays exact: pool counts
(unique_ips, countries, regions, cities, asns), shares and window metrics are computed from the
full data before capping and are not touched here, and each capped list records how many rows it
had, so a reader can say "showing 50 of 61,497". Window targets are kept whole, including each
browser target's browser object (calculated/browser.py).
"""

from __future__ import annotations

from .policy import payload_limits

# Pool lists the payload carries (aggregate.pool_lists()), each ordered by IPs, most first.
POOL_LISTS = ("geo", "countries_breakdown", "asns_breakdown")


def slim(payload, config):
    """A copy of payload whose breakdown and pool lists keep their first N rows.

    Breakdowns are ordered by completed requests, then IPs (aggregate.breakdowns()); pool lists
    by IPs (aggregate.pool_lists()), so the first N rows are the top N. N per list comes from
    the methodology's [payload] settings (policy.payload_limits()). Each window gains
    breakdown_entries and each pool list_entries: the full row count of every list. The input
    payload is not changed.
    """
    limits = payload_limits(config)
    products = []
    for product in payload["products"]:
        windows = {label: _window(window, limits) for label, window in product["windows"].items()}
        products.append(product | {"windows": windows})
    return payload | {"products": products}


def _window(window, limits):
    breakdowns = window.get("breakdowns")
    if breakdowns is not None:
        window = window | {
            "breakdowns": {name: rows[: limits[name]] for name, rows in breakdowns.items()},
            "breakdown_entries": {name: len(rows) for name, rows in breakdowns.items()},
        }
    pool = window["pool"]
    lists = {name: pool[name] for name in POOL_LISTS if name in pool}
    return window | {
        "pool": pool
        | {name: rows[: limits[name]] for name, rows in lists.items()}
        | {"list_entries": {name: len(rows) for name, rows in lists.items()}}
    }
