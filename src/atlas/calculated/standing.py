"""A network's published standing while it is not measured (STANDING-FREEZE-1, docs/index.html).

The rule is the same for every network, whatever the reason it is not measured (a pause, a refused
account, a scheduler or collection failure): only the stored data decide. A network is stopped at a
build when its newest stored network request (any outcome: a failed request is a measurement) is
[standing] freeze_after_hours (policy.standing_settings(); 0 turns the freeze off) or more before
the build's cutoff, or when it has none. From that build each of its windows is held:

- frozen: the window keeps its last published values from before the stop, carried unchanged from
  build to build: scores, ranks, components, the success, challenge and browser overalls, each
  target's metrics and scores, metrics, uptime, series (with their point scores), the Atlas Score
  trend and period, and with the 30d window the network's days. Its IP pool, lists, overlap and
  estimates stay the current window's (CURRENT): they count what was seen in the window, so they
  only fall;
- stopped: no earlier values to keep (no published build in which the network was still
  measured); the window has no score and no rank.

The values come from the previous published build's held window, else from the newest published
build at or before the cutoff in which the network was still measured (sources()): in steady
operation the previous build, the last values published before the freeze began.

A held window is in no measured peer set: no target rank, Success x/10 (its best rate), overall,
Index Score peer, latency basket penalty, day or point score of a measured network counts its rows.
Its own ranks are held (rerank()): each headline ranking (the Index Score's and the success,
challenge and browser overalls') ranks the measured networks with every frozen network's last score;
a measured network that beats it passes it, and it never ranks above its rank at the stop; the
browser and challenge overalls rank on the values the API serves, and a frozen window keeps the
API's group bests of the build its values are from (standing_shift.py). A held window lifts once the
network is measured again and lift_coverage of the window's hours since the earlier of its first
request there and the stop hold one of its network requests (hours(), _covered()), so a network
measured again for one hour does not rank on that hour, even after a stop longer than the window,
and a young network is not held for a window it never filled. Every window carries `standing`: its
status (STATUSES), the network's last measurement, when the freeze began and the cutoff its values
are from.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from psycopg.rows import tuple_row

from . import standing_shift as shift
from .policy import standing_settings

MEASURED, FROZEN, STOPPED = "measured", "frozen", "stopped"
STATUSES = (MEASURED, FROZEN, STOPPED)
HELD = (FROZEN, STOPPED)
# What a frozen window takes from the current build rather than from its last values.
CURRENT = ("pool", "lists", "breakdown_entries", "overlap", "estimated")
# The window keys of the overall scores ranked across networks (browser_overall: per engine).
OVERALLS = ("success_overall", "challenge_overall")

# The current build's head networks that may be held now: no request in the 30d window by
# %(stop)s, or a held window.
CANDIDATES = (
    "SELECT n FROM calculated_snapshots s JOIN current_calculation USING(build_id),"
    " jsonb_array_elements(coalesce(s.payload->'networks','[]')) n"
    " WHERE coalesce((n->'windows'->'30d'->'metrics'->>'last_observed')::timestamptz<=%(stop)s,"
    " true) OR EXISTS (SELECT 1 FROM jsonb_each(n->'windows') w"
    " WHERE w.value->'standing'->>'status'=ANY(%(held)s))"
)
# The published builds at or before a cutoff, newest first, and one network of one of them.
PUBLISHED = (
    "SELECT build_id,as_of FROM calculation_builds WHERE published AND as_of<=%s "
    "ORDER BY as_of DESC,build_id DESC"
)
ENTRY = (
    "SELECT n FROM calculated_snapshots s, jsonb_array_elements(coalesce(s.payload->'networks',"
    "'[]')) n WHERE s.build_id=%s AND n->>'id'=%s"
)


def _time(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def last_measured(entry):
    """The newest stored network request of a network entry: its windows' newest last_observed
    or, of a stored held window, last measurement."""
    found = []
    for w in entry["windows"].values():
        found += [w["metrics"].get("last_observed"), (w.get("standing") or {}).get("last_measured")]
    return max((_time(t) for t in found if t is not None), default=None)


def held(entry):
    """Whether a stored network entry has a held window."""
    return any((w.get("standing") or {}).get("status") in HELD for w in entry["windows"].values())


def sources(conn, as_of, config):
    """{network id: {"held": the current head's entry when it has a held window, "measured":
    {"as_of", "entry"} of the newest published build at or before as_of in which the network was
    still measured, or None}} of the networks that may be held at as_of (CANDIDATES). Earlier
    builds are read only for a network whose stop began before the current head was built."""
    limit = timedelta(hours=standing_settings(config)["freeze_after_hours"])
    if not limit:
        return {}
    cursor = conn.cursor(row_factory=tuple_row)
    params = {"stop": as_of - limit, "held": list(HELD)}
    found = {row[0]["id"]: row[0] for row in cursor.execute(CANDIDATES, params)}
    if not found:
        return {}
    builds = cursor.execute(PUBLISHED, (as_of,)).fetchall()
    out = {}
    for network, entry in found.items():
        source = {"held": entry if held(entry) else None, "measured": None}
        if not source["held"] or any(
            (w.get("standing") or {}).get("status", MEASURED) == MEASURED
            for w in entry["windows"].values()
        ):
            source["measured"] = _measured(cursor, builds, network, limit)
        if source["held"]:
            source["bests"] = _refill(cursor, builds, entry)
        out[network] = source
    return out


def _measured(cursor, builds, network, limit):
    """{"as_of", "entry", "peers"} of the newest of builds in which network's newest request was
    less than limit before its cutoff (peers: that build's networks of its kind), or None."""
    for build_id, built in builds:
        row = cursor.execute(ENTRY, (build_id, network)).fetchone()
        last = row and last_measured(row[0])
        if last is not None and built - last < limit:
            peers = shift.peers(cursor, build_id, row[0]["kind"])
            return {"as_of": built, "entry": row[0], "peers": peers}
    return None


def _refill(cursor, builds, entry):
    """{label: its group bests (shift.bests())} of a held entry's frozen windows stored without
    them (frozen before they were kept), from the newest of builds at or before the cutoff their
    values are from (normally that build; {} when none is kept), so they are read once."""
    out = {}
    for label, w in entry["windows"].items():
        own = w.get("standing") or {}
        if own.get("status") != FROZEN or "bests" in own:
            continue
        since = own.get("values_from") and _time(own["values_from"])
        build = next((b for b, built in builds if since and built <= since), None)
        found = build is not None and shift.peers(cursor, build, entry["kind"])
        out[label] = bests(found, label) if found else {}
    return out


def bests(peers, label):
    """shift.bests() of the measured windows label of peers (one build's networks of one kind):
    the group bests a window frozen from that build keeps."""
    windows = [p["windows"].get(label) for p in peers]
    return shift.bests(
        [w for w in windows if w and (w.get("standing") or {}).get("status") not in HELD]
    )


def hours(rows, end):
    """(the hours holding one of rows, the hours from the first one's to end) of a window's
    network rows: what mark() compares with lift_coverage."""
    found = {int(r["bucket_start"].timestamp()) // 3600 for r in rows}
    return len(found), (math.ceil(end.timestamp() / 3600) - min(found)) if found else 0


def mark(networks, found, as_of, config):
    """Give every window of build()'s network summaries its `standing` (see the module) from
    found (sources()); a held window gains "_held", the stored window it keeps (None: stopped),
    and a network whose 30d window is held its kept days. Returns the held (network id, label)."""
    settings = standing_settings(config)
    limit = timedelta(hours=settings["freeze_after_hours"])
    out = set()
    for entry in networks:
        last = last_measured(entry)
        stopped = bool(limit) and (last is None or as_of - last >= limit)
        source = found.get(entry["id"]) or {"held": None, "measured": None}
        for label, w in entry["windows"].items():
            cover = (*w.pop("_hours"), w["period"]["hours"], settings["lift_coverage"])
            kept = _kept(source, label, stopped, cover if limit else None, as_of)
            if kept is None:
                w["standing"] = _label(MEASURED, last)
                continue
            w["_held"], w["standing"] = kept[0], kept[1] | {"last_measured": last}
            out.add((entry["id"], label))
            if label == "30d":
                entry["days"] = kept[2]
    return out


def _kept(source, label, stopped, cover, as_of):
    """(the stored window kept or None, its standing, the days kept) of one window, or None
    when it is measured: a held window stays held while the network is stopped or the window
    is not covered (_covered(); cover None: the freeze is off); a measured one is held once the
    network is stopped."""
    before = (source["held"] or {}).get("windows", {}).get(label)
    status = ((before or {}).get("standing") or {}).get("status")
    if status in HELD and (stopped or not _covered(cover, before, as_of)):
        window = before if status == FROZEN else None
        own = dict(before["standing"])
        if status == FROZEN and "bests" not in own:
            own["bests"] = source.get("bests", {}).get(label, {})
        return window, own, source["held"].get("days", [])
    if not stopped:
        return None
    measured = source["measured"]
    window = measured and measured["entry"]["windows"].get(label)
    if not window:
        return None, _label(STOPPED, None, as_of), []
    values_from = _time(measured["as_of"])
    found = bests(measured.get("peers", ()), label)
    own = _label(FROZEN, None, as_of, values_from) | {"bests": found}
    return window, own, measured["entry"].get("days", [])


def _covered(cover, held, as_of):
    """Whether a held window (held: the stored one) is covered again: its hours with a network
    request (cover: hours(), the window's hours and lift_coverage) reach lift_coverage of its
    hours since the earlier of its first request and the stop (the kept values' newest request,
    else the freeze's start), at most the window's. The stopped hours count as uncovered, so a
    window whose rows all came after a stop longer than it does not lift on its first hour back."""
    if cover is None:
        return True
    measured, span, hours, share = cover
    found = [held["standing"].get("frozen_since"), held["metrics"].get("last_observed")]
    found = [_time(t) for t in found if t is not None]
    if found:
        back = math.ceil((as_of - min(found)).total_seconds() / 3600)
        span = max(span, min(hours, back))
    return measured >= share * span


def _label(status, last, since=None, values_from=None):
    return {
        "status": status,
        "last_measured": last,
        "frozen_since": since,
        "values_from": values_from,
    }


def live(networks):
    """Each network with only its measured windows (shared, not copied): the peers of every
    measured score."""
    return [
        entry | {"windows": {k: w for k, w in entry["windows"].items() if "_held" not in w}}
        for entry in networks
    ]


def hold(networks, previous):
    """Put each held window's kept values in place (see the module), once network.attach() has
    scored the measured windows, and rank the frozen windows' last scores with the measured ones
    (rerank()). previous is parts.previous(): a frozen Index Score's previous rank is the
    previous build's."""
    old = {n["id"]: n["windows"] for n in (previous or {}).get("networks", ())}
    for entry in networks:
        for label, w in list(entry["windows"].items()):
            if "_held" not in w:
                continue
            kept = w.pop("_held")
            if kept is None:  # stopped: its current figures without what scoring would consume
                _internal(w)
                continue
            frozen = {k: v for k, v in kept.items() if k not in CURRENT}
            frozen |= {k: w[k] for k in CURRENT if k in w} | {"standing": w["standing"]}
            balanced = (frozen.get("scores") or {}).get("balanced")
            if balanced:
                before = old.get(entry["id"], {}).get(label, {}).get("scores", {})
                balanced["previous_rank"] = (before.get("balanced") or {}).get("rank")
            entry["windows"][label] = frozen
    rerank(networks)


def rerank(networks):
    """Rank every headline score of each kind and window over the measured and frozen windows
    (place()): the Index Score by its rule (window_scoring.rank(): ties share the best rank), the
    overalls by theirs (ties share the average rank, with ranked_of). The browser and challenge
    overalls rank on the values the API serves (served()), so a rank and its served score always
    agree (standing_shift.py)."""
    peers = {}
    for entry in networks:
        for label, w in entry["windows"].items():
            own = w.get("standing") or {}
            frozen = own.get("status") == FROZEN
            for key, score in _headline(w):
                member = (score, frozen, own.get("bests") or {})
                peers.setdefault((entry["kind"], label, key), []).append(member)
    for (_, _, key), members in peers.items():
        if not any(frozen for _, frozen, _ in members):
            continue
        balanced = key == "balanced"
        live = [s for s, frozen, _ in members if not frozen and _eligible(s, balanced)]
        held = [(s, b) for s, frozen, b in members if frozen and s.get("rank") is not None]
        value = shift.served(key, live, held)
        place(live, [(s, s["rank"]) for s, _ in held], not balanced, value)


def _headline(window):
    """(key, score object) of a window's ranked scores: the balanced Index Score, each overall's
    and each browser engine's overall success score."""
    balanced = (window.get("scores") or {}).get("balanced")
    if balanced:
        yield "balanced", balanced
    for key in OVERALLS:
        if window.get(key):
            yield key, window[key]
    for engine, found in (window.get("browser_overall") or {}).items():
        if found.get("success"):
            yield f"browser:{engine}", found["success"]


def _eligible(score, balanced):
    if score.get("score") is None:
        return False
    return score.get("state") in ("ready", "retained") if balanced else True


def place(live, kept, average, value=None):
    """Rank live (score objects with a score) and kept ((score object, its rank at the stop)) in
    place by value ({id(score object): value}, served(); default: their scores): live highest
    first; each kept one inserted after every value above its own and never above its rank at
    the stop. Adjacent equal values share the best (average: the mean) of their places; a kept
    one never ranks better than its rank at the stop. average also sets ranked_of. Fewer than two
    (Index Score) or none (overall) change no rank."""
    value = value or {}
    of = {id(s): value.get(id(s), s["score"]) for s in [*live, *(s for s, _ in kept)]}
    order = sorted(live, key=lambda s: -of[id(s)])
    floors = {}
    for score, at in sorted(kept, key=lambda k: k[1]):
        above = sum(of[id(other)] > of[id(score)] for other in order)
        order.insert(min(max(int(at) - 1, above), len(order)), score)
        floors[id(score)] = at
    if len(order) < (1 if average else 2):
        return
    ranks = _adjacent([of[id(s)] for s in order], average)
    for s, rank in zip(order, ranks, strict=True):
        s["rank"] = max(rank, floors.get(id(s), rank))
        if average:
            s["ranked_of"] = len(order)


def _adjacent(values, average):
    """Each position's rank (1 the first) where runs of adjacent equal values share their best
    (average: mean) place."""
    ranks, start = [], 0
    while start < len(values):
        end = start
        while end + 1 < len(values) and values[end + 1] == values[start]:
            end += 1
        rank = (start + end) / 2 + 1 if average else start + 1
        ranks += [rank] * (end - start + 1)
        start = end + 1
    return ranks


def _internal(window):
    """Remove the window's and its series points' internal keys ("_..."), which only the
    measured windows' scoring consumes."""
    for key in [k for k in window if k.startswith("_")]:
        del window[key]
    for point in window.get("series", ()):
        for key in [k for k in point if k.startswith("_")]:
            del point[key]
