"""Outages and the incident helpers detect() uses to open and close them.

Outages (the `outages` table) are the time a product was down in a country. A pulse 'down'
opens one (from its last healthy pulse there) and the pulse 'recovered' that ends the incident
closes it (at the start of that healthy batch). A provider that is down shows as a done pulse
run without a success: that is data, not a gap. A run is only ever 'failed' by our side (no
node, never started, went silent, node shut down or erred, upload rejected), so failed runs are
never judged. Load batches (latency under load) never open or close an outage.
A census that stalled opens one in the orchestrator (orchestrator.batches.census_down); any
later healthy pulse in the country closes it in detect, with a 'recovered' event of source 'census'.
"""

from __future__ import annotations

from datetime import timedelta

# Incidents that stay open until a later pulse of the same series is healthy again.
INCIDENTS = ("down", "success_drop")
# An incident older than this is no longer open (bounds the event index scan).
INCIDENT_TTL = timedelta(days=7)


def _exists(cur, query, params):
    return cur.execute("SELECT EXISTS (" + query + ") AS found", params).fetchone()["found"]


def _had(cur, batch_id, product_id, kinds, after):
    """Whether a batch already produced one of these events for the product."""
    return _exists(
        cur,
        "SELECT 1 FROM test_events WHERE product_id=%s AND at>=%s AND batch_id=%s AND kind=ANY(%s)",
        (product_id, after, batch_id, list(kinds)),
    )


def _batch_start(batch):
    return batch["started_at"] or batch["start_at"]


def _open_outage(cur, batch, product):
    """A pulse 'down' opens an outage, unless the product is already down in the country.

    It starts when the product's last healthy pulse in the country (any target) finished, or
    at this batch's start without one in the last INCIDENT_TTL.
    """
    start = _batch_start(batch)
    healthy = cur.execute(
        "SELECT max(coalesce(r.finished_at,br.finished_at,b.finished_at)) AS at "
        "FROM batch_runs br JOIN test_batches b USING(batch_id) "
        "LEFT JOIN test_runs r ON r.run_key=br.run_key "
        "WHERE b.kind='pulse' AND b.country_code=%s AND br.product_id=%s AND br.status='done' "
        "AND br.successes>0 AND b.scheduled_for<%s AND b.scheduled_for>=%s",
        (batch["country_code"], product, batch["scheduled_for"], start - INCIDENT_TTL),
    ).fetchone()["at"]
    since = min(healthy, start) if healthy else start
    cur.execute(
        "INSERT INTO outages(product_id,country_code,region_id,source,batch_id,started_at) "
        "SELECT %(product)s,%(country)s,%(region)s,'pulse',%(batch)s,%(since)s "
        "WHERE NOT EXISTS (SELECT 1 FROM outages WHERE product_id=%(product)s "
        "AND country_code=%(country)s AND (ended_at IS NULL OR batch_id=%(batch)s))",
        {
            "product": product,
            "country": batch["country_code"],
            "region": batch["region_id"],
            "batch": batch["batch_id"],
            "since": since,
        },
    )
    return since


def _still_down(cur, batch, product, now):
    """Whether another pulse target of the product is still 'down' in the batch's country."""
    rows = cur.execute(
        "SELECT DISTINCT ON (e.target_id) e.kind FROM test_events e JOIN test_batches b "
        "USING(batch_id) WHERE e.product_id=%s AND e.at>=%s AND e.country_code=%s "
        "AND e.target_id<>%s AND b.kind='pulse' AND e.kind=ANY(%s) AND b.scheduled_for<=%s "
        "ORDER BY e.target_id,b.scheduled_for DESC,e.event_id DESC",
        (
            product,
            now - INCIDENT_TTL,
            batch["country_code"],
            batch["target_id"],
            [*INCIDENTS, "recovered"],
            batch["scheduled_for"],
        ),
    ).fetchall()
    return any(row["kind"] == "down" for row in rows)


def _close_outages(cur, batch, product, *, source=None):
    """End the product's open outages in the country that began before this healthy batch.

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
