"""ATLAS-SCORE-SPARK-1: the Atlas Score history behind each network window's atlas_score_spark.

Every publication (calculator.calculate(publish=True), store.publish()) records the published
build's balanced score (the composite that ranks the leaderboard) of every network (provider and
kind) and window, keyed by the build's as_of, and forgets the records before KEEP_DAYS whole UTC
days back from it (record()). A build reads the records of earlier cutoffs only (spark(), from
network.attach() once the networks are scored), so each window's spark is one point per slot,
oldest first, and the last point is the build's own score:

- 24h: the 24 hours ending at as_of; an hour's point is the newest record in (end - 1h, end];
- 7d and 30d: the 7 or 30 UTC days ending with as_of's day; a day's point is its newest record,
  the day's last published build.

A point is {"at", "score", "stored"}: at is the slot's end (a day's end; as_of for the last), score
the record's (None when that build published none) and stored whether a build's score was found
(False: no published build in the slot, such as before the history began). Records are kept as
published: a methodology change does not recompute the earlier points. migrate_v18_v19.sql fills
the store from the retained published builds.

SCORE-HISTORY-BACKFILL-1: a record's source is "build" (as published) or "backfill" (recomputed
later at that as_of under the methodology then current, score_backfill.py). A spark reads only the
records of its own build's methodology, so a point of another methodology is never mixed in (it is
"stored": False unless a backfill recomputed it); at the same as_of a published record wins.

SCORE-HISTORY-1: "its own build's methodology" is every methodology whose stored definition
(methodologies.definition) equals the build's apart from DISPLAY_ONLY, the settings no Atlas Score
reads (same_scores()). A change to them alone (SUCCESS-ALL-8-1's [success_score]) gives a new
methodology id but the same Atlas Scores, so the spark keeps its points.

SCORE-HISTORY-2: after any other change (a weight, LATENCY-BASKET-1's Speed rule) a slot without a
record of the build's Atlas Scores takes the newest record of an earlier methodology, marked
"earlier_rule": True, so the spark stays continuous and says which points the old rule scored. A
record of the build's Atlas Scores always wins its slot over an earlier one, and the back-fill can
still restate an earlier slot under the new methodology (it counts it as missing).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from psycopg.rows import tuple_row

from .latency_store import utc_day
from .policy import digest

PROFILE = "balanced"  # rankings.go RankingProfile: the score that ranks the leaderboard
SLOTS = {"24h": 24, "7d": 7, "30d": 30}
KEEP_DAYS = 31  # the 30d spark's 29 days before as_of's day, and two more
HOUR, DAY = timedelta(hours=1), timedelta(days=1)
# The methodology settings no Atlas Score reads (display only): the Success x/10's.
DISPLAY_ONLY = ("success_score",)
# The current build's network window scores (each v4 head network's windows), as published.
RECORD = (
    "INSERT INTO calculated_score_history "
    "SELECT n->>'provider',n->>'kind',w.key,(s.payload->>'as_of')::timestamptz,"
    "(w.value->'scores'->'balanced'->>'score')::double precision,s.payload->'methodology'->>'id' "
    "FROM calculated_snapshots s JOIN current_calculation USING(build_id),"
    "jsonb_array_elements(CASE jsonb_typeof(s.payload->'networks') WHEN 'array' "
    "THEN s.payload->'networks' END) n,jsonb_each(n->'windows') w "
    "ON CONFLICT(provider,kind,period,as_of,source) DO UPDATE SET score=excluded.score,"
    "methodology_id=excluded.methodology_id"
)
FORGET = (
    "DELETE FROM calculated_score_history WHERE as_of<(SELECT date_trunc('day',b.as_of,'UTC')"
    f"-interval '{KEEP_DAYS} days' FROM current_calculation JOIN calculation_builds b "
    "USING(build_id))"
)
# Every record before a cutoff, whether it is of the same Atlas Scores' methodologies; of one
# as_of, a same one after an earlier one, a published one after a recomputed one (each replaces).
READ = (
    "SELECT provider,kind,period,as_of,score,methodology_id=ANY(%s) FROM calculated_score_history "
    "WHERE as_of>=%s AND as_of<%s ORDER BY as_of,methodology_id=ANY(%s),source='build'"
)
# The definitions of a methodology and of those with records in [since, until).
DEFINITIONS = (
    "SELECT methodology_id,definition FROM methodologies WHERE methodology_id=%s OR methodology_id "
    "IN (SELECT DISTINCT methodology_id FROM calculated_score_history WHERE as_of>=%s AND as_of<%s)"
)


def record(conn):
    """Record the current build's network window scores and forget the records before KEEP_DAYS
    whole UTC days back from its as_of (each publication, once current_calculation names it)."""
    conn.execute(RECORD)
    conn.execute(FORGET)


def same_scores(conn, method_id, since, until):
    """The methodology ids with records in [since, until) whose definition equals method_id's
    apart from DISPLAY_ONLY, method_id among them (alone when it has no stored definition)."""
    cur = conn.cursor(row_factory=tuple_row)
    found = dict(cur.execute(DEFINITIONS, (method_id, since, until)))
    if method_id not in found:
        return [method_id]
    own = score_key(found[method_id])
    return sorted(m for m, definition in found.items() if score_key(definition) == own)


def score_key(definition):
    """The digest of a methodology definition without its DISPLAY_ONLY settings."""
    return digest({k: v for k, v in definition.items() if k not in DISPLAY_ONLY})


def spark(conn, networks, as_of, method_id):
    """Set each network window's atlas_score_spark from the records before as_of, those of the
    methodologies with the build's Atlas Scores (same_scores(), method_id the build's) first,
    and the window's own balanced score."""
    since = utc_day(as_of) - (max(SLOTS.values()) - 1) * DAY
    found, earlier = defaultdict(dict), defaultdict(set)
    cur = conn.cursor(row_factory=tuple_row)
    ids = same_scores(conn, method_id, since, as_of)
    for provider, kind, period, at, score, same in cur.execute(READ, (ids, since, as_of, ids)):
        found[provider, kind, period][at] = score
        if same:
            earlier[provider, kind, period].discard(at)
        else:
            earlier[provider, kind, period].add(at)
    for entry in networks:
        for label, window in entry["windows"].items():
            own = window["scores"].get(PROFILE, {}).get("score")
            key = entry["provider"], entry["kind"], label
            window["atlas_score_spark"] = points(found[key], own, as_of, label, earlier[key])


def points(records, own, as_of, label, earlier=frozenset()):
    """The window's spark from records ({as_of: score} of earlier builds) and its own score: each
    slot its newest record, one in earlier (an earlier methodology's) only without another."""
    newest = {}
    for at in sorted(records, key=lambda at: (at not in earlier, at)):  # a same one replaces
        back = slots_back(as_of, at, label)
        if 0 < back < SLOTS[label]:
            newest[back] = at
    out = []
    for back in range(SLOTS[label] - 1, 0, -1):
        at = newest.get(back)
        score = records[at] if at is not None else None
        point = {"at": _end(as_of, back, label), "score": score, "stored": at is not None}
        out.append(point | ({"earlier_rule": True} if at in earlier else {}))
    return [*out, {"at": as_of, "score": own, "stored": True}]


def slots_back(as_of, at, label):
    """How many slots before as_of's own slot at is: whole hours for 24h, UTC days otherwise."""
    if label == "24h":
        return (as_of - at) // HOUR
    return (utc_day(as_of) - utc_day(at)) // DAY


def _end(as_of, back, label):
    """The end of the slot back slots before as_of's own (back > 0)."""
    if label == "24h":
        return as_of - back * HOUR
    return utc_day(as_of) - (back - 1) * DAY
