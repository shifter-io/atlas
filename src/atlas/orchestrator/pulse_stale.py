"""PULSE-OUTAGE-CLOSE-1: per-country pulse outages that the data says are over.

detect.py opens a per-country pulse outage (source 'pulse', batch_id set) when a network gets no
success at all in a country (every target of the slot), and ends it with the network's next
pulse there that gets through, on any target. An outage can still stay open after its end:
one opened by an older rule (a single target's failure), or past its closing pulse under one
(a comeback below the baseline used to keep it open). The pulse evaluator therefore closes, at
each tick, every open per-country pulse outage whose network got through in that country after
its last slot (hour) without any success: it ends at the start of the first pulse through
(data-driven, never now()), with a 'recovered' tape event at that time for each target that got
through and whose series is still 'down' (data `closed_by`: "pulse_evaluate"). A pulse counts
with at least detect's min_requests (configs/methodology.toml), and the first one through must
have started SETTLED before now, so detect judged it first. An outage whose later pulses all
fail, or that has none (an unmeasured network), stays open. A network-wide outage (batch_id
NULL) is the evaluator's own windows' (pulse.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import psycopg

from .. import verdicts
from ..detect import thresholds

SETTLED = timedelta(hours=1)  # detect judges a batch within minutes; this leaves it the first say
RECENT = timedelta(days=7)  # outages.INCIDENT_TTL: only an end this recent is written

# Each open per-country pulse outage with the first slot after its network's last failing one
# in the country (a slot fails when none of its pulses got a success) and that slot's first
# pulse that got through (ties: the lowest batch id): its end.
ENDS_SQL = """
WITH open AS (
  SELECT o.outage_id,o.product_id,o.started_at,b.country_code,b.scheduled_for AS slot
  FROM outages o JOIN test_batches b ON b.batch_id=o.batch_id
  WHERE o.source='pulse' AND o.batch_id IS NOT NULL AND o.ended_at IS NULL
), pulses AS (
  SELECT o.outage_id,b.scheduled_for AS slot,b.batch_id,coalesce(b.started_at,b.start_at) AS at,
  br.successes>0 AS ok FROM open o JOIN test_batches b ON b.kind='pulse'
  AND b.country_code=o.country_code AND b.scheduled_for>=o.slot JOIN batch_runs br
  ON br.batch_id=b.batch_id AND br.product_id=o.product_id AND br.status='done'
  AND br.requests>=%(min_requests)s
), slots AS (
  SELECT outage_id,slot,bool_or(ok) AS ok FROM pulses GROUP BY 1,2
), ends AS (
  SELECT outage_id,min(slot) FILTER (WHERE ok AND slot>coalesce(last_fail,'-infinity')) AS slot
  FROM (SELECT *,max(slot) FILTER (WHERE NOT ok) OVER (PARTITION BY outage_id) AS last_fail
        FROM slots) s GROUP BY 1
)
SELECT DISTINCT ON (p.outage_id) p.outage_id,p.batch_id,p.at,p.slot,o.product_id,o.country_code,
o.started_at FROM pulses p JOIN ends e ON e.outage_id=p.outage_id AND e.slot=p.slot AND p.ok
JOIN open o ON o.outage_id=p.outage_id ORDER BY p.outage_id,p.at,p.batch_id
"""
CLOSE_SQL = (
    "UPDATE outages SET ended_at=greatest(started_at,%s) WHERE outage_id=%s AND ended_at IS NULL "
    "RETURNING ended_at"
)
# A 'recovered' on each batch of the end slot that got through and whose series (product,
# country, target) is still 'down' on the tape, as detect writes one per target: never a second
# one, and detect judges the series afresh after it; a target still failing keeps its 'down'.
EVENT_SQL = (
    "INSERT INTO test_events(at,kind,batch_id,product_id,country_code,target_id,region_id,"
    "signal,severity,data) SELECT %(at)s,'recovered',b.batch_id,%(product)s,b.country_code,"
    "b.target_id,b.region_id,1,'info',jsonb_build_object('incident','down','source','pulse',"
    "'down_since',%(since)s::text,'closed_by','pulse_evaluate') FROM test_batches b "
    "WHERE b.kind='pulse' AND b.country_code=%(country)s AND b.scheduled_for=%(slot)s "
    "AND EXISTS (SELECT 1 FROM batch_runs br WHERE br.batch_id=b.batch_id "
    "AND br.product_id=%(product)s AND br.status='done' AND br.successes>0) "
    "AND (SELECT e.kind FROM test_events e JOIN test_batches eb ON eb.batch_id=e.batch_id "
    "WHERE e.product_id=%(product)s AND e.country_code=b.country_code "
    "AND e.target_id=b.target_id AND e.at>=%(started)s-interval '1 day' AND eb.kind='pulse' "
    "AND e.kind IN ('down','success_drop','recovered') AND eb.scheduled_for<=b.scheduled_for "
    "ORDER BY eb.scheduled_for DESC,e.event_id DESC LIMIT 1)='down' ORDER BY b.batch_id"
)


async def stale_ends(conn: psycopg.AsyncConnection, now: datetime) -> list[dict]:
    """The open per-country pulse outages that are over, each with its end (`at`) and the
    batch of its first pulse through; only ends between RECENT and SETTLED before now."""
    minimum = int(thresholds()["min_requests"])
    rows = await (await conn.execute(ENDS_SQL, {"min_requests": minimum})).fetchall()
    return [row for row in rows if now - RECENT <= row["at"] <= now - SETTLED]


async def close_stale(conn: psycopg.AsyncConnection, now: datetime) -> int:
    """Close the outages stale_ends finds, each with its 'recovered' event; returns how many.
    Idempotent: a closed outage is never found again. Call inside the evaluator's transaction."""
    closed = 0
    for row in await stale_ends(conn, now):
        ended = await (await conn.execute(CLOSE_SQL, (row["at"], row["outage_id"]))).fetchone()
        if ended is None:
            continue
        params = {"at": ended["ended_at"], "product": row["product_id"], "slot": row["slot"]}
        params |= {"country": row["country_code"], "started": row["started_at"]}
        await conn.execute(EVENT_SQL, params | {"since": row["started_at"].isoformat()})
        closed += 1
    return closed


async def close_ignored(conn: psycopg.AsyncConnection, now: datetime, rule: int) -> int:
    """Close every open outage (per-country, network-wide or census) of a network's country that
    does not count at `now` (COUNTRY-MIN-IPS-1, under the methodology's country rule `rule`,
    detect.country_rule(): its latest daily test found fewer unique IPs, or it has had none): at that verdict's start,
    never before the outage's own (without a verdict, at its start: it never counted), with
    end_reason 'too_few_ips' and no tape event; returns how many. Idempotent. Call inside the
    evaluator's transaction."""
    return len(await (await conn.execute(verdicts.CLOSE, {"now": now, "min_ips": rule})).fetchall())
