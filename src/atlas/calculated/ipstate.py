"""Counts across product groups in the hourly build from per-scope IP state (CALC-SCALE-1).

A fleet cohort's member pairs (fleetsql: its IPs per pair, those shared with another provider,
and its distinct IPs) and a product kind's distinct IPs (incremental.kinds()) were counted over
every observation of their window whenever a member's runs changed, which an hourly build's new
runs do: their time grew with the window. Each is a sum over IPs of what the IP's groups in the
window give, so the latest build (fleetsql: no folded bucket at or after the cutoff) keeps the
IPs counted by their groups instead, in two tables:

- calculated_scope_ip_last_seen: each IP's newest bucket per scope, a run's scope_id * 2 plus 1
  for a run in a batch, which fix the run's product group (with the policy and the code, which
  this table does not depend on). A 5-minute bucket is folded in (greatest()) when its runs
  differ from its scopes_digest in calculated_bucket_ips; a stored run's observations never
  change, so the table holds the newest sighting of every folded observation. A scope saw an IP
  in a window ending at the cutoff iff its newest sighting is in the window, since no folded
  bucket is at or after the cutoff.
- calculated_ip_vectors: each window's IPs counted by the scopes that saw them in it (a
  "vector"), as at the cutoff of calculated_ip_vector_state. A build moves them to its cutoff
  through the IPs whose vector may change, those of the folded buckets and those with a sighting
  that left a window since that cutoff: in one statement, each such IP's vector before the fold
  (in the old cutoff's windows) is taken out and its vector after put in. Without a usable state
  (none, a later cutoff, other windows, or a count gone negative) every window's vectors are
  counted again from the table.
- A run outside a batch can be linked to one later (storage_batch.link()), which moves it to
  another scope though its buckets keep their runs. The state keeps the window's runs outside a
  batch; when one of them is in a batch now, the sightings of its scopes (in a batch and not) are
  counted again from their runs' observations (rescope()), and the vectors from the table.

A build whose cutoff passes UTC midnights (midnights()) can move the state to each of them
first (unique_ips.py closes the day there). Besides the 24h, 7d and 30d windows the state keeps the
month to date (MONTH, since the 1st of the month of the cutoff's last instant: month_start()), so
the close reads the month's IPs from it; the window moves on at the first cutoff after a month's
last midnight, once a month. Sightings older than KEEP are deleted by each build (no window
reaches them). A cohort's counts
are a sum over the vectors holding one of its members' scopes, in Python (pair_counts()); they
equal fleetsql's COUNTS over the window's observations.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import UTC, timedelta

from psycopg.rows import tuple_row

from .. import timing

KEEP = timedelta(days=31)
# The month-to-date window's key among the window hours (CALC-SCALE-3, #1307): its IPs are those
# seen since the 1st of the month of the cutoff's last instant (month_start()), which
# unique_ips.close() counts at each midnight instead of every sighting since the 1st.
MONTH = 0
STATE = "SELECT as_of,windows,unbatched FROM calculated_ip_vector_state"
FOLDED = """SELECT bucket_start,scopes_digest FROM calculated_bucket_ips
WHERE bucket_start>=%s AND bucket_start<%s"""
MARK = """UPDATE calculated_bucket_ips m SET scopes_digest=k.d
FROM unnest(%s::timestamptz[],%s::text[]) AS k(t,d) WHERE m.bucket_start=k.t"""
# The changed buckets' observations folded: every run's, or with the country verdicts only the
# counted runs' (bucket, run) pairs (fleet_verdicts.Counted.pairs()).
ALL = "unnest(%(changed)s::timestamptz[]) AS b(t) JOIN ip_observations o ON o.bucket_start=b.t"
PAIRS = (
    "unnest(%(at)s::timestamptz[],%(keys)s::bigint[]) AS k(t,run_key) "
    "JOIN ip_observations o ON o.run_key=k.run_key AND o.bucket_start=k.t"
)
FOLD = """SELECT o.ip_id,r.scope_id::bigint*2+(r.batch_id IS NOT NULL)::int AS scope,
 max(o.bucket_start) AS last_seen FROM {source} JOIN test_runs r ON r.run_key=o.run_key
 GROUP BY 1,2"""
SAVE = """INSERT INTO calculated_scope_ip_last_seen {rows} ON CONFLICT(ip_id,scope)
DO UPDATE SET last_seen=greatest(calculated_scope_ip_last_seen.last_seen,excluded.last_seen)"""
# One statement (module docstring): the fold; ips, the IPs folded or with a sighting in a
# window's [prev, since); their rows before the fold, each IP's read on its own through the
# primary key (OFFSET 0 keeps the lookup: a join could scan the whole table), and after it; the
# fold saved; and per window, the vectors of those IPs from prev taken out and from since put in.
ADVANCE = (
    """WITH w AS (
 SELECT * FROM unnest(%(hours)s::int[],%(prev)s::timestamptz[],%(since)s::timestamptz[])
  AS w(hours,prev,since)
), fold AS ("""
    + FOLD
    + """), ips AS (
 SELECT ip_id FROM fold UNION SELECT g.ip_id FROM w JOIN calculated_scope_ip_last_seen g
  ON g.last_seen>=w.prev AND g.last_seen<w.since
), before AS (
 SELECT g.* FROM ips CROSS JOIN LATERAL (SELECT g.ip_id,g.scope,g.last_seen
  FROM calculated_scope_ip_last_seen g WHERE g.ip_id=ips.ip_id OFFSET 0) g
), after AS (
 SELECT ip_id,scope,max(last_seen) AS last_seen
 FROM (SELECT * FROM before UNION ALL SELECT * FROM fold) x GROUP BY 1,2
), saved AS ("""
    + SAVE.format(rows="SELECT * FROM fold")
    + """), moved AS (
 SELECT w.hours,b.ip_id,array_agg(b.scope ORDER BY b.scope) AS scopes,-1 AS n
 FROM before b JOIN w ON b.last_seen>=w.prev GROUP BY 1,2
 UNION ALL
 SELECT w.hours,a.ip_id,array_agg(a.scope ORDER BY a.scope),1
 FROM after a JOIN w ON a.last_seen>=w.since GROUP BY 1,2
)
INSERT INTO calculated_ip_vectors(window_hours,scopes,ips)
SELECT hours,scopes,sum(n) FROM moved GROUP BY 1,2 HAVING sum(n)<>0
ON CONFLICT(window_hours,scopes) DO UPDATE SET ips=calculated_ip_vectors.ips+excluded.ips"""
)
# Every window's vectors counted again from the table, in one pass over its newest sightings.
COUNT_ALL = """INSERT INTO calculated_ip_vectors(window_hours,scopes,ips)
SELECT hours,scopes,count(*) FROM (SELECT w.hours,g.ip_id,array_agg(g.scope ORDER BY g.scope)
 AS scopes FROM unnest(%(hours)s::int[],%(since)s::timestamptz[]) AS w(hours,since)
 JOIN calculated_scope_ip_last_seen g ON g.last_seen>=w.since GROUP BY 1,2) x GROUP BY 1,2"""
NEGATIVE = "SELECT count(*) FROM calculated_ip_vectors WHERE ips<0"
# The sightings of the scopes of %(ids)s (scope_ids), in a batch and not, counted again.
RESCOPE = (
    "CREATE TEMP TABLE ipstate_fresh ON COMMIT DROP AS SELECT o.ip_id,"
    "r.scope_id::bigint*2+(r.batch_id IS NOT NULL)::int AS scope,max(o.bucket_start) AS last_seen "
    "FROM test_runs r JOIN ip_observations o USING(run_key) WHERE r.scope_id=ANY(%(ids)s) "
    "AND (%(counted)s::bigint[] IS NULL OR r.run_key=ANY(%(counted)s)) "
    "AND o.bucket_start>=%(old)s AND o.bucket_start<%(as_of)s GROUP BY 1,2",
    "DELETE FROM calculated_scope_ip_last_seen g USING (SELECT DISTINCT ip_id FROM ipstate_fresh) "
    "f WHERE g.ip_id=f.ip_id AND g.scope/2=ANY(%(ids)s)",
    "INSERT INTO calculated_scope_ip_last_seen SELECT * FROM ipstate_fresh",
    "DROP TABLE ipstate_fresh",
)
TIDY = (
    "DELETE FROM calculated_ip_vectors WHERE ips=0",
    "DELETE FROM calculated_scope_ip_last_seen WHERE last_seen<%(old)s",
    "DELETE FROM calculated_ip_vector_state",
    "INSERT INTO calculated_ip_vector_state VALUES (%(as_of)s,%(hours)s,%(unbatched)s)",
)
VECTORS = "SELECT window_hours,scopes,ips FROM calculated_ip_vectors"


def month_start(as_of):
    """The start of the UTC month holding the last instant before as_of: at a midnight that
    begins a month, the month that ends there."""
    last = (as_of - timedelta(microseconds=1)).astimezone(UTC)
    return last.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def starts(as_of, windows):
    """Each window's start at as_of: its hours before it, the month's start for MONTH."""
    return [month_start(as_of) if h == MONTH else as_of - timedelta(hours=h) for h in windows]


def scopes(runs):
    """{run_key: its scope} (the module docstring's) of runs (incremental's, with their scope_id
    and batch_id)."""
    return {r["run_key"]: r["scope_id"] * 2 + (r["batch_id"] is not None) for r in runs}


def midnights(conn, as_of):
    """The UTC midnights after the state's cutoff up to as_of, the oldest first: none without a
    state, none older than KEEP."""
    state = conn.cursor(row_factory=tuple_row).execute(STATE).fetchall()
    day = as_of.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    found = []
    while state and day > state[0][0] and day > as_of - KEEP:
        found.append(day)
        day -= timedelta(days=1)
    return found[::-1]


def advance(conn, as_of, hours, runs, scope_of, ruled=None, detail=None):
    """{window hours: Vectors} at as_of of the windows of hours, once the buckets of runs
    (fleetsql.run_sets() of the longest window) whose runs changed are folded; the latest
    build's only (module docstring). scope_of: each run of the window's scope (scopes()).
    ruled (fleet_verdicts.Counted, with the country verdicts): only the counted runs'
    observations are folded, and a relinked scope's sightings counted again from them. detail:
    more fields of its timing line."""
    cursor = conn.cursor(row_factory=tuple_row)
    hours = sorted({*hours, MONTH})
    folded = dict(cursor.execute(FOLDED, (as_of - timedelta(hours=hours[-1]), as_of)))
    changed = sorted(t for t, d in runs.items() if folded.get(t) != d)
    state, began = cursor.execute(STATE).fetchall(), time.monotonic()
    usable = len(state) == 1 and state[0][0] <= as_of and state[0][1] == hours
    linked = {scope_of[k] // 2 for k in (state[0][2] if state else ()) if scope_of.get(k, 0) % 2}
    if linked:  # a run outside a batch then is in one now: its vectors counted again
        rescope(cursor, as_of, sorted(linked), ruled)
        usable = False
    _move(cursor, as_of, hours, _fold(changed, ruled), state[0][0] if usable else None)
    if usable and cursor.execute(NEGATIVE).fetchone()[0]:
        usable = False  # an inconsistent state: counted again
        _move(cursor, as_of, hours, None, None)
    seconds = time.monotonic() - began
    detail = {"buckets": len(changed), "moved": int(usable)} | (detail or {})
    timing.line("calculate", "ip_state", seconds, **detail)
    cursor.execute(MARK, (changed, [runs[t] for t in changed]))
    unbatched = sorted(k for k, scope in scope_of.items() if scope % 2 == 0)
    for statement in TIDY:
        bounds = {"old": as_of - KEEP, "as_of": as_of, "hours": hours, "unbatched": unbatched}
        cursor.execute(statement, bounds)
    found = defaultdict(list)
    for window, held, ips in cursor.execute(VECTORS):
        found[window].append((tuple(held), ips))
    return {window: Vectors(found[window]) for window in hours}


def rescope(cursor, as_of, ids, ruled=None):
    """Count the sightings of the scopes of ids (scope_ids) again from their runs' kept rows
    (the counted runs' with ruled)."""
    counted = None if ruled is None else ruled.keys(as_of - KEEP, as_of)
    params = {"ids": ids, "old": as_of - KEEP, "as_of": as_of, "counted": counted}
    for statement in RESCOPE:
        cursor.execute(statement, params)


def _fold(changed, ruled):
    """(the fold's source, its parameters) for the changed buckets: every run's observations,
    or the counted runs' only (ruled)."""
    if ruled is None:
        return ALL, {"changed": changed}
    at, keys = ruled.pairs(changed)
    return PAIRS, {"at": at, "keys": keys}


def _move(cursor, as_of, hours, fold, before):
    """Fold (source, parameters; None: nothing) and move the vectors from the cutoff before to
    as_of; with before None, count every window's vectors again from the table."""
    since = starts(as_of, hours)
    source, params = fold or (ALL, {"changed": []})
    if before is None:
        cursor.execute(SAVE.format(rows=FOLD.format(source=source)), params)
        cursor.execute("DELETE FROM calculated_ip_vectors")
        cursor.execute(COUNT_ALL, {"hours": hours, "since": since})
        return
    prev = starts(before, hours)
    statement = ADVANCE.format(source=source)
    cursor.execute(statement, {"hours": hours, "prev": prev, "since": since} | params)


class Vectors:
    """A window's vectors [(scopes, IPs)], found by scope."""

    def __init__(self, rows):
        self.rows, self.by_scope = rows, defaultdict(list)
        for n, (held, _) in enumerate(rows):
            for scope in held:
                self.by_scope[scope].append(n)

    def holding(self, wanted):
        """The vectors holding one of the scopes wanted, each once."""
        found = {n for scope in wanted for n in self.by_scope.get(scope, ())}
        return [self.rows[n] for n in sorted(found)]

    def distinct(self, wanted):
        """The IPs some scope of wanted saw in the window."""
        return sum(ips for _, ips in self.holding(wanted))


def network_counts(vectors, members, scope_of):
    """network_scores._counted()'s counts of one window from its Vectors (CALC-SCALE-3, #1307):
    {slot: (IPs, shared)} and {slot: {peer slot: IPs both saw}} of members [(network entry, its
    window with "_runs")], each IP counted for the networks of its kind whose scopes saw it, and
    shared when more than one did; scope_of maps a run key to its scope."""
    slot_of = {}
    for slot, (entry, window) in enumerate(members):
        for key in window["_runs"]:
            if key in scope_of:
                slot_of[scope_of[key]] = (entry["kind"], slot)
    own, pairs = {}, defaultdict(dict)
    for held, ips in vectors.holding(slot_of):
        kinds = defaultdict(set)
        for scope in held:
            if scope in slot_of:
                kinds[slot_of[scope][0]].add(slot_of[scope][1])
        for slots in kinds.values():
            for a in slots:
                n, shared = own.get(a, (0, 0))
                own[a] = (n + ips, shared + (ips if len(slots) > 1 else 0))
                for b in slots - {a}:
                    pairs[a][b] = pairs[a].get(b, 0) + ips
    return own, pairs


def pair_counts(vectors, members, scope_of):
    """fleetsql._count_sets()'s counts of one cohort window from its Vectors: {(a, b): (IPs,
    shared)} for members a <= b that both saw IPs, and (None, None): the distinct IPs. members:
    [(group, its run keys)]; scope_of maps a run key to its scope."""
    member = {scope_of[k]: m for m, (_, keys) in enumerate(members) for k in keys}
    providers = [group["provider"] for group, _ in members]
    found, distinct = {}, 0
    for held, ips in vectors.holding(member):
        seen = sorted({member[s] for s in held if s in member})
        shared = ips if len({providers[m] for m in seen}) > 1 else 0
        for i, a in enumerate(seen):
            for b in seen[i:]:
                n, s = found.get((a, b), (0, 0))
                found[a, b] = (n + ips, s + shared)
        distinct += ips
    if distinct:
        found[None, None] = (distinct, 0)
    return found
