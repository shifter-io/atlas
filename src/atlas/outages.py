"""Outages and the incident helpers detect() uses to open and close them.

Outages (the `outages` table) are the time a product was down in a country. A pulse 'down'
opens one (from its last healthy observation there: _outage_start) and the pulse 'recovered'
that ends the incident closes it (at the start of that healthy batch). A provider that is
down shows as a done pulse run without a success: that is data, not a gap. A run is only ever
'failed' by our side (no node, never started, went silent, node shut down or erred, upload
rejected), so failed runs are never judged.
Load batches (latency under load) never open or close an outage.
A census that stalled opens one in the orchestrator (orchestrator.batches.census_down); any
later healthy pulse in the country closes it in detect, with a 'recovered' event of source 'census'.
"""

from __future__ import annotations

from datetime import timedelta

from .daily import DAILY, MARKER

# Incidents that stay open until a later pulse of the same series is healthy again.
INCIDENTS = ("down", "success_drop")
# An incident older than this is no longer open (bounds the event index scan).
INCIDENT_TTL = timedelta(days=7)


def _exists(cur, query, params):
    return cur.execute("SELECT EXISTS (" + query + ") AS found", params).fetchone()["found"]


# No event of a batch is more than this before its slot: a 'down' is at its outage's start, at
# most INCIDENT_TTL before the batch (_open_outage), and every other event at or after its runs;
# an hour more covers the nodes' clocks.
EARLIEST = INCIDENT_TTL + timedelta(hours=1)


def earliest(batch):
    """No event of the batch is before this (EARLIEST)."""
    return batch["scheduled_for"] - EARLIEST


def run_end(run):
    """When a run finished: its own report's time, else when the orchestrator saw it end."""
    return run.get("run_finished") or run.get("finished_at")


def runs_end(batch, runs):
    """When the last of the runs finished (run_end), else when the batch did."""
    return max(filter(None, map(run_end, runs)), default=None) or batch.get("finished_at")


def _had(cur, batch, product_id, kinds):
    """Whether a batch already produced one of these events for the product."""
    return _exists(
        cur,
        "SELECT 1 FROM test_events WHERE product_id=%s AND at>=%s AND batch_id=%s AND kind=ANY(%s)",
        (product_id, earliest(batch), batch["batch_id"], list(kinds)),
    )


def _batch_start(batch):
    return batch["started_at"] or batch["start_at"]


# Another target of the product in the batch's slot that got through, or is still running.
OTHER_TARGET_SQL = (
    "SELECT 1 FROM batch_runs br JOIN test_batches b USING(batch_id) WHERE b.kind='pulse' "
    "AND b.country_code=%s AND b.scheduled_for=%s AND b.batch_id<>%s AND br.product_id=%s "
    "AND ((br.status='done' AND br.successes>0) OR br.status IN ('queued','running','uploading'))"
)


# The product's observations in the country before a pulse slot: each target's done pulse
# and each completed daily test, whether it got any success, its slot (a daily test's start),
# when it began and when it finished. Of these: when the last healthy one finished, and the
# slot and start of the first failing one after it.
OBSERVATIONS_SQL = (
    "WITH o AS (SELECT br.successes>0 AS healthy,b.scheduled_for AS slot,"
    "coalesce(b.started_at,b.start_at) AS began,"
    "coalesce(r.finished_at,br.finished_at,b.finished_at) AS finished "
    "FROM batch_runs br JOIN test_batches b USING(batch_id) "
    "LEFT JOIN test_runs r ON r.run_key=br.run_key "
    "WHERE b.kind='pulse' AND b.country_code=%(country)s AND br.product_id=%(product)s "
    "AND br.status='done' AND b.scheduled_for<%(slot)s AND b.scheduled_for>=%(floor)s "
    "UNION ALL SELECT (r.summary->>'successes')::bigint>0,r.started_at,r.started_at,r.finished_at "
    "FROM test_runs r JOIN scope_stats s ON s.scope_id=r.scope_id "
    "JOIN pool_stats p ON p.pool_id=s.pool_id WHERE s.product_id=%(product)s "
    "AND upper(p.requested_country)=%(country)s AND r.summary->>%(marker)s=%(daily)s "
    "AND r.status='completed' AND r.started_at<%(start)s AND r.finished_at>=%(floor)s), "
    "h AS (SELECT max(finished) AS at FROM o WHERE healthy) "
    "SELECT h.at AS healthy,f.slot,f.began FROM h LEFT JOIN LATERAL (SELECT slot,began FROM o "
    "WHERE NOT healthy AND slot>h.at ORDER BY slot,began LIMIT 1) f ON true"
)


def _outage_start(cur, batch, product):
    """When the product's outage in the country starts, for this batch's pulse 'down'.

    It starts when its last healthy observation there finished (a pulse of any target, or a
    daily test, with a success), or at this batch's start without one in the last
    INCIDENT_TTL. When nothing at all was observed for more than a pulse interval after that
    healthy one (the country was not pulsed for hours or days), the time without data is not
    the outage: it starts with the first failing observation (OUTAGE-START-1)."""
    start = _batch_start(batch)
    row = cur.execute(
        OBSERVATIONS_SQL,
        {
            "country": batch["country_code"],
            "product": product,
            "slot": batch["scheduled_for"],
            "start": start,
            "floor": start - INCIDENT_TTL,
            "marker": MARKER,
            "daily": DAILY,
        },
    ).fetchone()
    healthy = row["healthy"]
    if healthy is None:
        return start
    slot, began = (row["slot"], row["began"]) if row["slot"] else (batch["scheduled_for"], start)
    interval = cur.execute(
        "SELECT pulse_interval_minutes FROM targets WHERE target_id=%s", (batch["target_id"],)
    ).fetchone()["pulse_interval_minutes"]
    since = began if slot - healthy > timedelta(minutes=interval) else healthy
    return min(since, start)


def _open_outage(cur, batch, product):
    """A pulse 'down' opens an outage, unless the product is down in the country already (an
    open outage, or a closed one that covered this batch's start: judging it again) or
    another of its targets in the same slot got through or is still running (PULSE-OUTAGE-
    CLOSE-1: an outage is the product without any success in the country; one target failing
    alone is that target's 'down' on the tape only; a pending one opens it with its own 'down').

    It starts at _outage_start; returns that start either way.
    """
    start = _batch_start(batch)
    since = _outage_start(cur, batch, product)
    other = (batch["country_code"], batch["scheduled_for"], batch["batch_id"], product)
    if _exists(cur, OTHER_TARGET_SQL, other):
        return since
    cur.execute(
        "INSERT INTO outages(product_id,country_code,region_id,source,batch_id,started_at) "
        "SELECT %(product)s,%(country)s,%(region)s,'pulse',%(batch)s,%(since)s "
        "WHERE NOT EXISTS (SELECT 1 FROM outages WHERE product_id=%(product)s "
        "AND country_code=%(country)s AND (ended_at IS NULL OR batch_id=%(batch)s "
        "OR (started_at<=%(start)s AND ended_at>%(start)s)))",  # one already covers it
        {
            "product": product,
            "country": batch["country_code"],
            "region": batch["region_id"],
            "batch": batch["batch_id"],
            "since": since,
            "start": start,
        },
    )
    return since


def _close_outages(cur, batch, product, *, source=None):
    """End the product's open outages in the country that began before this batch, which got
    through (any target: PULSE-OUTAGE-CLOSE-1).

    A network-wide pulse outage (batch_id NULL) is left to the pulse evaluator."""
    start = _batch_start(batch)
    return cur.execute(
        "UPDATE outages SET ended_at=greatest(started_at,%s) WHERE product_id=%s "
        "AND country_code=%s AND ended_at IS NULL AND started_at<%s "
        "AND NOT (source='pulse' AND batch_id IS NULL) "
        + ("AND source=%s " if source else "")
        + "RETURNING outage_id,batch_id,started_at,ended_at",
        (start, product, batch["country_code"], start, *((source,) if source else ())),
    ).fetchall()


def _rate(numerator, requests):
    return numerator / requests if requests else None
