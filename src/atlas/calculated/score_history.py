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
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from psycopg.rows import tuple_row

from .latency_store import utc_day

PROFILE = "balanced"  # rankings.go RankingProfile: the score that ranks the leaderboard
SLOTS = {"24h": 24, "7d": 7, "30d": 30}
KEEP_DAYS = 31  # the 30d spark's 29 days before as_of's day, and two more
HOUR, DAY = timedelta(hours=1), timedelta(days=1)
# The current build's network window scores (each v4 head network's windows), as published.
RECORD = (
    "INSERT INTO calculated_score_history "
    "SELECT n->>'provider',n->>'kind',w.key,(s.payload->>'as_of')::timestamptz,"
    "(w.value->'scores'->'balanced'->>'score')::double precision,s.payload->'methodology'->>'id' "
    "FROM calculated_snapshots s JOIN current_calculation USING(build_id),"
    "jsonb_array_elements(CASE jsonb_typeof(s.payload->'networks') WHEN 'array' "
    "THEN s.payload->'networks' END) n,jsonb_each(n->'windows') w "
    "ON CONFLICT(provider,kind,period,as_of) DO UPDATE SET score=excluded.score,"
    "methodology_id=excluded.methodology_id"
)
FORGET = (
    "DELETE FROM calculated_score_history WHERE as_of<(SELECT date_trunc('day',b.as_of,'UTC')"
    f"-interval '{KEEP_DAYS} days' FROM current_calculation JOIN calculation_builds b "
    "USING(build_id))"
)
READ = (
    "SELECT provider,kind,period,as_of,score FROM calculated_score_history "
    "WHERE as_of>=%s AND as_of<%s"
)


def record(conn):
    """Record the current build's network window scores and forget the records before KEEP_DAYS
    whole UTC days back from its as_of (each publication, once current_calculation names it)."""
    conn.execute(RECORD)
    conn.execute(FORGET)


def spark(conn, networks, as_of):
    """Set each network window's atlas_score_spark from the records before as_of and the
    window's own balanced score."""
    since = utc_day(as_of) - (max(SLOTS.values()) - 1) * DAY
    found = defaultdict(dict)
    cur = conn.cursor(row_factory=tuple_row)
    for provider, kind, period, at, score in cur.execute(READ, (since, as_of)):
        found[provider, kind, period][at] = score
    for entry in networks:
        for label, window in entry["windows"].items():
            own = window["scores"].get(PROFILE, {}).get("score")
            records = found[entry["provider"], entry["kind"], label]
            window["atlas_score_spark"] = points(records, own, as_of, label)


def points(records, own, as_of, label):
    """The window's spark from records ({as_of: score} of earlier builds) and its own score."""
    newest = {}
    for at in sorted(records):  # oldest first: a slot keeps its newest record
        back = _back(as_of, at, label)
        if 0 < back < SLOTS[label]:
            newest[back] = at
    out = []
    for back in range(SLOTS[label] - 1, 0, -1):
        at = newest.get(back)
        score = records[at] if at is not None else None
        out.append({"at": _end(as_of, back, label), "score": score, "stored": at is not None})
    return [*out, {"at": as_of, "score": own, "stored": True}]


def _back(as_of, at, label):
    """How many slots before as_of's own slot at is: whole hours for 24h, UTC days otherwise."""
    if label == "24h":
        return (as_of - at) // HOUR
    return (utc_day(as_of) - utc_day(at)) // DAY


def _end(as_of, back, label):
    """The end of the slot back slots before as_of's own (back > 0)."""
    if label == "24h":
        return as_of - back * HOUR
    return utc_day(as_of) - (back - 1) * DAY
