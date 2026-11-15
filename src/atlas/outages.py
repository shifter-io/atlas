"""Outages and the incident helpers detect() uses to open and close them, and the series' baseline
and incident lookups it judges a pulse against.

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

from . import verdicts

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


def _open_outage(cur, batch, product):
    """A pulse 'down' opens an outage, unless the product is down in the country already (an
    open outage, or a closed one that covered this batch's start: judging it again) or
    another of its targets in the same slot got through or is still running (PULSE-OUTAGE-
    CLOSE-1: an outage is the product without any success in the country; one target failing
    alone is that target's 'down' on the tape only; a pending one opens it with its own 'down').

    It starts when the product's last healthy pulse in the country (any target) finished, or
    at this batch's start without one in the last INCIDENT_TTL; returns that start either way.
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


def _pulse_history(cur, cfg, batch, product, rule=False):
    """The series' trailing baseline: its last done pulses with requests before this batch (with
    the rule, of the time its country counted)."""
    return cur.execute(
        "SELECT br.batch_id,b.finished_at,b.scheduled_for,br.requests,br.successes,br.challenges,"
        "br.blocks,br.run_key FROM batch_runs br JOIN test_batches b USING(batch_id) "
        "LEFT JOIN test_runs r ON r.run_key=br.run_key "
        f"{verdicts.counts_at('br.product_id', 'trim(b.country_code)', verdicts.RUN_AT)}"
        "WHERE b.kind='pulse' AND b.country_code=%(country)s AND b.target_id=%(target)s "
        "AND br.product_id=%(product)s AND br.status='done' AND br.requests>0 "
        "AND b.scheduled_for<%(slot)s ORDER BY b.scheduled_for DESC LIMIT %(n)s",
        {
            "country": batch["country_code"],
            "target": batch["target_id"],
            "product": product,
            "slot": batch["scheduled_for"],
            "n": cfg["baseline_batches"],
            "verdicts": rule,
        },
    ).fetchall()


def _pulse_incident(cur, batch, product, now):
    """The series' latest incident or recovery event, if any."""
    # The series state as of this batch: events of earlier batches only, so judging a batch
    # again (after a watermark reset) reaches the same verdict.
    return cur.execute(
        "SELECT e.kind,e.data FROM test_events e JOIN test_batches b USING(batch_id) "
        "WHERE e.product_id=%s AND e.at>=%s AND e.country_code=%s AND e.target_id=%s "
        "AND b.kind='pulse' AND e.kind=ANY(%s) AND b.scheduled_for<%s "
        "ORDER BY b.scheduled_for DESC,e.event_id DESC LIMIT 1",
        (
            product,
            now - INCIDENT_TTL,
            batch["country_code"],
            batch["target_id"],
            [*INCIDENTS, "recovered"],
            batch["scheduled_for"],
        ),
    ).fetchone()


def _rate(numerator, requests):
    return numerator / requests if requests else None
