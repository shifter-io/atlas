"""Calculation inputs grouped per product for build()."""

from collections import defaultdict
from datetime import datetime


def group_by_product(rows, drop_id=False):
    """Rows by product_id, without that key when drop_id is set."""
    groups = defaultdict(list)
    for r in rows:
        groups[r["product_id"]].append(
            {k: v for k, v in r.items() if k != "product_id"} if drop_id else r
        )
    return groups


def tested_by_product(tested):
    """Per product: the latest test time per country, and the tested rows with trimmed countries."""
    tested_groups, monitored_groups = defaultdict(dict), defaultdict(list)
    for t in tested:
        country, group = t["country"].strip(), tested_groups[t["product_id"]]
        group[country] = max(group.get(country, t["last"]), t["last"])
        monitored_groups[t["product_id"]].append(t | {"country": country})
    return tested_groups, monitored_groups


def previous_products(previous, as_of):
    """The previous snapshot's products by id (none if it is newer than as_of).

    Their scores are stamped with the previous methodology id where they lack one.
    """
    if not previous or datetime.fromisoformat(previous["as_of"]) > as_of:
        return {}
    for old_product in previous["products"]:
        for old_window in old_product["windows"].values():
            old_scores = [*old_window["scores"].values()]
            old_scores += [t["score"] for t in old_window["targets"].values()]
            for old_score in old_scores:
                old_score.setdefault("methodology_id", previous["methodology"]["id"])
    return {p["id"]: p for p in previous["products"]}
