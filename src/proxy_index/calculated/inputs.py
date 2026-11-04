"""Calculation inputs grouped per product for build()."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta


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

    previous is parts.previous(): as_of, the methodology id and each group's id and scores only.
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


def cutoff(conn, requested, latest):
    """The calculation's cutoff: `requested` on the 5-minute bucket grid of the stored facts.

    Exact half-open windows need whole buckets, so a time inside a bucket moves to one of its
    edges. It moves to the bucket's end when the bucket holds data and every run with data in it
    had finished by `requested` (always so for `latest`, a calculation of everything stored now:
    a run is stored only once it has finished), so a run that ended just before the calculation
    is in it. Otherwise it moves to the bucket's start, which leaves out any part of a run that
    ended after `requested`. Each bucket is in a snapshot once or not at all.
    """
    start = datetime.fromtimestamp(int(requested.timestamp() // 300) * 300, UTC)
    if start == requested:
        return start
    row = conn.execute(
        "SELECT count(*) AS runs,count(*) FILTER (WHERE r.finished_at>%s) AS running "
        "FROM (SELECT DISTINCT run_key FROM run_buckets WHERE bucket_start=%s) b "
        "JOIN test_runs r USING(run_key)",
        (requested, start),
    ).fetchone()
    closed = row["runs"] and (latest or not row["running"])
    return start + timedelta(minutes=5) if closed else start
