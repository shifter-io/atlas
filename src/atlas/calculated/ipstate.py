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

Sightings older than KEEP are deleted by each build (no window reaches them). A cohort's counts
are a sum over the vectors holding one of its members' scopes, in Python (pair_counts()); they
equal fleetsql's COUNTS over the window's observations.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import timedelta

from psycopg.rows import tuple_row

from .. import timing

KEEP = timedelta(days=31)
STATE = "SELECT as_of,windows,unbatched FROM calculated_ip_vector_state"
FOLDED = """SELECT bucket_start,scopes_digest FROM calculated_bucket_ips
WHERE bucket_start>=%s AND bucket_start<%s"""
MARK = """UPDATE calculated_bucket_ips m SET scopes_digest=k.d
FROM unnest(%s::timestamptz[],%s::text[]) AS k(t,d) WHERE m.bucket_start=k.t"""
# One statement (module docstring): fold, the changed buckets' newest bucket per IP and scope;
# ips, the IPs folded or with a sighting in a window's [lo, hi) (hi NULL: no bound); their rows
# before the fold and after it; the fold saved; and per window, the vectors of those IPs from
# prev (NULL: none) taken out and from since put in.
ADVANCE = """WITH w AS (
 SELECT * FROM unnest(%(hours)s::int[],%(prev)s::timestamptz[],%(since)s::timestamptz[],
  %(lo)s::timestamptz[],%(hi)s::timestamptz[]) AS w(hours,prev,since,lo,hi)
), fold AS (
 SELECT o.ip_id,r.scope_id::bigint*2+(r.batch_id IS NOT NULL)::int AS scope,
  max(o.bucket_start) AS last_seen
 FROM unnest(%(changed)s::timestamptz[]) AS b(t) JOIN ip_observations o ON o.bucket_start=b.t
 JOIN test_runs r USING(run_key) GROUP BY 1,2
), ips AS (
 SELECT ip_id FROM fold UNION SELECT g.ip_id FROM w JOIN calculated_scope_ip_last_seen g
  ON g.last_seen>=w.lo AND (w.hi IS NULL OR g.last_seen<w.hi)
), before AS (
 SELECT g.ip_id,g.scope,g.last_seen FROM ips JOIN calculated_scope_ip_last_seen g USING(ip_id)
), after AS (
 SELECT ip_id,scope,max(last_seen) AS last_seen
 FROM (SELECT * FROM before UNION ALL SELECT * FROM fold) x GROUP BY 1,2
), saved AS (
 INSERT INTO calculated_scope_ip_last_seen SELECT * FROM fold ON CONFLICT(ip_id,scope)
 DO UPDATE SET last_seen=greatest(calculated_scope_ip_last_seen.last_seen,excluded.last_seen)
), moved AS (
 SELECT w.hours,b.ip_id,array_agg(b.scope ORDER BY b.scope) AS scopes,-1 AS n
 FROM before b JOIN w ON b.last_seen>=w.prev GROUP BY 1,2
 UNION ALL
 SELECT w.hours,a.ip_id,array_agg(a.scope ORDER BY a.scope),1
 FROM after a JOIN w ON a.last_seen>=w.since GROUP BY 1,2
)
INSERT INTO calculated_ip_vectors(window_hours,scopes,ips)
SELECT hours,scopes,sum(n) FROM moved GROUP BY 1,2 HAVING sum(n)<>0
ON CONFLICT(window_hours,scopes) DO UPDATE SET ips=calculated_ip_vectors.ips+excluded.ips"""
NEGATIVE = "SELECT count(*) FROM calculated_ip_vectors WHERE ips<0"
# The sightings of the scopes of %(ids)s (scope_ids), in a batch and not, counted again.
RESCOPE = (
    "CREATE TEMP TABLE ipstate_fresh ON COMMIT DROP AS SELECT o.ip_id,"
    "r.scope_id::bigint*2+(r.batch_id IS NOT NULL)::int AS scope,max(o.bucket_start) AS last_seen "
    "FROM test_runs r JOIN ip_observations o USING(run_key) WHERE r.scope_id=ANY(%(ids)s) "
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


def scopes(runs):
    """{run_key: its scope} (the module docstring's) of runs (incremental's, with their scope_id
    and batch_id)."""
    return {r["run_key"]: r["scope_id"] * 2 + (r["batch_id"] is not None) for r in runs}


def advance(conn, as_of, hours, runs, scope_of):
    """{window hours: Vectors} at as_of of the windows of hours, once the buckets of runs
    (fleetsql.run_sets() of the longest window) whose runs changed are folded; the latest
    build's only (module docstring). scope_of: each run of the window's scope (scopes())."""
    cursor = conn.cursor(row_factory=tuple_row)
    hours = sorted(hours)
    folded = dict(cursor.execute(FOLDED, (as_of - timedelta(hours=hours[-1]), as_of)))
    changed = sorted(t for t, d in runs.items() if folded.get(t) != d)
    state, began = cursor.execute(STATE).fetchall(), time.monotonic()
    usable = len(state) == 1 and state[0][0] <= as_of and state[0][1] == hours
    linked = {scope_of[k] // 2 for k in (state[0][2] if state else ()) if scope_of.get(k, 0) % 2}
    if linked:  # a run outside a batch then is in one now: its vectors counted again
        rescope(cursor, as_of, sorted(linked))
        usable = False
    _move(cursor, as_of, hours, changed, state[0][0] if usable else None)
    if usable and cursor.execute(NEGATIVE).fetchone()[0]:
        usable = False  # an inconsistent state: counted again
        _move(cursor, as_of, hours, [], None)
    seconds = time.monotonic() - began
    timing.line("calculate", "ip_state", seconds, buckets=len(changed), moved=int(usable))
    cursor.execute(MARK, (changed, [runs[t] for t in changed]))
    unbatched = sorted(k for k, scope in scope_of.items() if scope % 2 == 0)
    for statement in TIDY:
        bounds = {"old": as_of - KEEP, "as_of": as_of, "hours": hours, "unbatched": unbatched}
        cursor.execute(statement, bounds)
    found = defaultdict(list)
    for window, held, ips in cursor.execute(VECTORS):
        found[window].append((tuple(held), ips))
    return {window: Vectors(found[window]) for window in hours}


def rescope(cursor, as_of, ids):
    """Count the sightings of the scopes of ids (scope_ids) again from their runs."""
    for statement in RESCOPE:
        cursor.execute(statement, {"ids": ids, "old": as_of - KEEP, "as_of": as_of})


def _move(cursor, as_of, hours, changed, before):
    """Fold the changed buckets and move the vectors from the cutoff before to as_of; with
    before None, count every window's vectors again."""
    since = [as_of - timedelta(hours=h) for h in hours]
    if before is None:
        cursor.execute("DELETE FROM calculated_ip_vectors")
        prev, lo, hi = [None] * len(hours), since, [None] * len(hours)
    else:
        prev = [before - timedelta(hours=h) for h in hours]
        lo, hi = prev, since
    bounds = {"hours": hours, "prev": prev, "since": since, "lo": lo, "hi": hi}
    cursor.execute(ADVANCE, bounds | {"changed": changed})


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
