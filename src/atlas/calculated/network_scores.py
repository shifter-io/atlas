"""Network scores, ranks and ownership overlap (contract K9, "Scores and overlap").

network.attach() completes each network window in store.write(), once its distinct IPs and
K10 place counts are known; then it calls the two functions here.

score() scores the networks of one kind and window against each other, by the rules one
group's window is scored with (calculator.score_window()):

- inputs: product_inputs() on the network pool (coverage from pool.daily's unique IPs,
  diversity from the window's network counts in pool.daily), the headline product's pricing,
  the window's measured uptime and the headline group's evidence of that window;
- targets: target_score() on the network target metrics, each with the freshness of its own
  last observation, and the headline product's effective methodology (its required targets,
  weights and validity hours);
- rank() over the networks of the kind; previous_rank, scored-at and valid-until by retain(),
  from the previous build's head networks (parts.previous()).

A network window publishes the balanced score only: its targets carry no score, and the other
profiles are dropped (K9, "Not published"). Its readiness is the group rule: ready when the
balanced score is, else partial with completed requests, else unavailable.

overlap() counts, in one statement per window, each network's distinct IPs against the other
networks of its kind. The IPs are the window's observations of the network's runs in the window
(the set its pool.unique_ips counts, hourly.bucket_ips() with the window as one point), so a
freshly counted ips_ours equals pool.unique_ips. No IP set is held in Python. Each overlap's
"as_of" is the cutoff it was counted at. The 24h overlap is counted every build; the 7d and 30d
overlaps once a UTC day (owner decision, NET-SUM-2 question 1): a build whose previous head has
them for the same networks, counted the same UTC day by the same implementation and with at least
one pair, carries them (carried()), so they can be up to a day old and their ips_ours is the pool
of their own "as_of". A previous head of another implementation (a deploy or a code change), or
one whose window has no pair, is counted again (OVERLAP-7D-1): a count with no pair, such as one
made before the window's IPs were stored, is never carried for the rest of the day.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from psycopg.rows import dict_row

from . import standing
from .aggregate import component, product_inputs, target_score
from .inputs import previous_products
from .policy import DIMENSIONS, effective
from .value_all import reprice_windows

# The windows whose overlap is counted once a UTC day and carried in between.
DAILY = ("7d", "30d")
UNPLACED = "No network place counts in this window"

# Each network's distinct IPs in its window (slot: its position; kind: its kind's position),
# then per IP of a kind the networks that have it, and per pair (a, b) of those networks the
# IPs they share (a = b: the network's own IPs, and those another network of its kind also has).
# Distinct values are counted in two levels of GROUP BY, without a DISTINCT or a self-join
# (CALC-FLEET-DISTINCT-1).
OVERLAP = """WITH k AS (SELECT * FROM unnest(%s::int[],%s::int[],%s::bigint[],%s::timestamptz[],
 %s::timestamptz[]) AS k(slot,kind,run_key,since,until)),
s AS (SELECT k.slot,k.kind,o.ip_id FROM k JOIN ip_observations o
 ON o.run_key=k.run_key AND o.bucket_start>=k.since AND o.bucket_start<k.until GROUP BY 1,2,3),
ip AS (SELECT array_agg(slot) AS slots FROM s GROUP BY kind,ip_id)
SELECT a,b,count(*) AS ips,count(*) FILTER (WHERE cardinality(slots)>1) AS shared
FROM ip CROSS JOIN LATERAL unnest(slots) a CROSS JOIN LATERAL unnest(slots) b GROUP BY 1,2"""


def overlap(conn, payload, previous, counted=None):
    """{(network id, window label): overlap} of payload's networks still holding their "_runs"
    (network.window()): exclusive and shared share of the network's IPs against the union of
    the other networks of its kind with IPs (compared_products), one peer row per such network
    sharing at least one IP, and "as_of" (when counted). A network without IPs has null shares
    and no peers. previous is parts.previous(): a daily window it carries is not counted, and
    none is carried from a head of another implementation than payload's. counted(members): the
    counts from the IP state (incremental.Incremental.overlaps()), None to count them here."""
    networks, as_of = payload["networks"], payload["as_of"]
    if previous and previous.get("implementation_id") != payload["implementation_id"]:
        previous = None
    labels = defaultdict(list)
    for entry in networks:
        for label, w in entry["windows"].items():
            labels[label].append((entry, w))
    found = {}
    for label, members in labels.items():
        kept = carried([e for e, _ in members], previous, as_of, label)
        if kept is not None:
            found |= {(e["id"], label): kept[e["id"]] for e, _ in members}
            continue
        own, pairs = (counted and counted(members)) or _counted(conn, members)
        for slot, (entry, _) in enumerate(members):
            found[entry["id"], label] = shares(slot, members, own, pairs[slot]) | {"as_of": as_of}
    return found


def _counted(conn, members):
    """OVERLAP's counts of one window's members ((network, window) pairs, by slot): {slot: (IPs,
    shared)} and {slot: {peer slot: shared IPs}}."""
    kinds = {kind: n for n, kind in enumerate(sorted({e["kind"] for e, _ in members}))}
    rows = [
        (slot, kinds[entry["kind"]], run_key, w["period"]["start"], w["period"]["end"])
        for slot, (entry, w) in enumerate(members)
        for run_key in w["_runs"]
    ]
    own, pairs = {}, defaultdict(dict)
    if not rows:
        return own, pairs
    columns = [[row[n] for row in rows] for n in range(5)]
    for row in conn.cursor(row_factory=dict_row).execute(OVERLAP, columns):
        if row["a"] == row["b"]:
            own[row["a"]] = (row["ips"], row["shared"])
        else:
            pairs[row["a"]][row["b"]] = row["ips"]
    return own, pairs


def carried(networks, previous, as_of, label):
    """{network id: overlap} of a daily window from the previous head (parts.previous()) when
    it has one for exactly these networks, counted the same UTC day as as_of and not after it,
    with at least one pair; else None (the window is counted)."""
    if label not in DAILY or not previous:
        return None
    old = {n["id"]: n["windows"].get(label, {}).get("overlap") for n in previous["networks"]}
    if set(old) != {n["id"] for n in networks}:
        return None
    if not any(found and found.get("peers") for found in old.values()):
        return None
    day = as_of.astimezone(UTC).date()
    for found in old.values():
        counted = found and found.get("as_of") and datetime.fromisoformat(found["as_of"])
        if not counted or counted > as_of or counted.astimezone(UTC).date() != day:
            return None
    return old


def shares(slot, members, own, pairs):
    """One network's overlap (overlap()) from the window's counts: own maps a slot to (its IPs,
    those shared with any other network of its kind), pairs this slot's peers to shared IPs."""
    entry = members[slot][0]
    ips, shared = own.get(slot, (0, 0))
    peers = [
        n
        for n, (other, _) in enumerate(members)
        if n != slot and other["kind"] == entry["kind"] and own.get(n, (0, 0))[0]
    ]
    if not ips:
        return {"exclusive_share": None, "shared_share": None, "compared_products": 0, "peers": []}
    rows = [
        {
            "provider": members[n][0]["provider"],
            "product_id": members[n][0]["product_id"],
            "shared_ips": pairs[n],
            "ips_ours": ips,
            "ips_theirs": own[n][0],
        }
        for n in peers
        if pairs.get(n)
    ]
    return {
        "exclusive_share": 1 - shared / ips,
        "shared_share": shared / ips,
        "compared_products": len(peers),
        "peers": rows,
    }


def score(networks, payload, previous):
    """Score, rank and retain the balanced score of every window of networks (completed by
    network.attach(), still holding "_lead"), in place, per kind and window. payload is the
    build's (its as_of and methodology); previous is parts.previous() with the previous
    build's head networks."""
    # The group helpers: calculator imports network (and so this module) at import time.
    from .calculator import rank, score_window

    as_of, method = payload["as_of"], payload["methodology"]
    old = previous_products(previous and previous | {"products": previous["networks"]}, as_of)
    peers = defaultdict(list)
    for entry in standing.live(networks):  # a held window is no peer (standing.py)
        values = {}
        for label, w in entry["windows"].items():
            prior = old.get(entry["id"], {}).get("windows", {}).get(label, {})
            # Its own target dicts: the scoring writes a score into each (removed below).
            w["targets"] = {t: dict(v) for t, v in w["targets"].items()}
            found = member(entry, w, prior, method["settings"], as_of)
            values[label] = found["inputs"]["value"]
            peers[entry["kind"], label].append(({"windows": {label: w}}, found))
        reprice_windows(entry["windows"], values, method["settings"], as_of)
    for (_, label), members in peers.items():
        score_window(members, label, method["settings"], as_of, method["id"])
        rank([p["windows"][label]["scores"]["balanced"] for p, _ in members])
        for p, _ in members:
            w = p["windows"][label]
            w["scores"] = {"balanced": w["scores"]["balanced"]}
            for target in w["targets"].values():
                del target["score"]


def member(entry, window, old, config, as_of):
    """A network window's raw score inputs, as build() makes a group's member for
    calculator.score_window(): the headline product's effective methodology, the window's
    freshness, its targets' target_score(), product_inputs() and the headline group's evidence
    rows of that window (their validity caps the score's)."""
    # calculator.fresh(), imported here for the same reason as in score().
    from .calculator import fresh

    m, lead = window["metrics"], window.pop("_lead")
    cfg = effective(config, entry["provider"], entry["product"])
    targets = {}
    for target, t in window["targets"].items():
        tc = effective(config, entry["provider"], entry["product"], target)
        observed = t["metrics"]["last_observed"]
        targets[target] = target_score(t["metrics"], tc, target) | {
            "fresh": fresh(observed, as_of, tc["score_valid_hours"]),
            "observed": observed,
            "valid_hours": tc["score_valid_hours"],
        }
    values = lead.get("evidence", {})
    inputs = product_inputs(
        window["pool"], entry["pricing"], cfg, entry["kind"], (window["uptime"], values), window
    )
    counts = inputs["diversity"]["inputs"]
    if any(counts[k] is None for k in DIMENSIONS):  # no K10 list: diversity unavailable
        inputs["diversity"] = component(None, reason=UNPLACED, inputs=counts)
    return {
        "config": cfg,
        "fresh": fresh(m["last_observed"], as_of, cfg["score_valid_hours"]),
        "targets": targets,
        "old": old,
        "evidence": lead.get("evidence_sources", []),
        "inputs": inputs,
        "observed": m["last_observed"],
    }
