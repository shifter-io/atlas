"""The census/pulse inputs of a calculation: one function per input, read in the caller's
snapshot (its one transaction), in the order calculator.scheduled_inputs() always read them.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import timedelta

from .. import detect, traffic
from . import browser
from .aggregate import PLACE, census_overlaps

EVENTS_IN_SNAPSHOT = 200
# A geo row's group: product group, smallest window (hours), location, network and fallback.
GEO_KEY = ("scope_id", "product_id", "hours", "location_id", "network_id", "fallback")
GEO_KEYS = (
    "CASE WHEN r.batch_id IS NULL THEN r.scope_id END AS scope_id,s.product_id,"
    "CASE WHEN g.bucket_start>=%(d1)s THEN 24 WHEN g.bucket_start>=%(d7)s THEN 168 "
    "ELSE 720 END AS hours,g.location_id,g.network_id,CASE WHEN l.country_code IS NULL "
    "THEN coalesce(b.country_code,upper(ps.requested_country)) END AS fallback"
)
GEO_SOURCE = (
    "FROM run_geo_buckets g JOIN test_runs r USING(run_key) "
    "JOIN scope_stats s ON s.scope_id=r.scope_id JOIN pool_stats ps ON ps.pool_id=s.pool_id "
    "JOIN enrichment_location_fields l ON l.location_id=g.location_id "
    "LEFT JOIN test_batches b ON b.batch_id=r.batch_id "
)
GEO_WINDOW = f"WHERE g.bucket_start>=%(start)s AND g.bucket_start<%(end)s AND {browser.NOT_BROWSER}"
# Breakdown latency is light-load latency of the latency target: its pulses and runs outside
# a batch, never census or load runs (census concurrency), nor pulses that ran during a
# census. Counters keep every request of every target.
LIGHT = (
    "AND (b.kind IS NULL OR b.kind='pulse') AND NOT g.run_key=ANY(%(flagged)s) "
    "AND s.target=%(latency_target)s "
)
# Census runs of every product (a census still running at the cutoff ends there), and the
# pulse and load runs they may overlap; a day of margin covers runs that began earlier.
RUN_TIMES = (
    "coalesce(r.started_at,br.started_at) AS started_at,"
    "least(coalesce(r.finished_at,br.finished_at,%(end)s),%(end)s) AS finished_at "
    "FROM batch_runs br JOIN test_batches b USING(batch_id) "
    "LEFT JOIN test_runs r ON r.run_key=br.run_key WHERE br.started_at IS NOT NULL "
    "AND br.started_at<%(end)s AND coalesce(br.finished_at,%(end)s)>=%(margin)s "
)


def scheduled_inputs(conn, as_of, timed_target="generic_http"):
    """The census/pulse history a v2 payload adds, read in the calculation's snapshot.

    geo: run_geo_buckets of the 30-day window summed per product group (scope_id for runs
    outside a batch, NULL for census/pulse runs), smallest window (hours), location, network and,
    for requests without an exit location, the country the run requested. pulses: finished
    pulse batch runs (uptime). census: the latest finished census per product and country.
    outages: the outages overlapping the 30-day window, as known at the cutoff. tested: per
    product and country, the spans of started census or pulse runs (uptime's denominator
    and per-bucket monitoring evidence). events: the newest test_events before the cutoff.
    traffic: wire bytes per test kind. pulse_minutes: the cohort's pulse interval, the shortest
    pulse_interval_minutes of pulse-enabled targets (else the shortest gap between pulse slots, else
    None); slot_length() turns it into the time slot. overlaps: the pulse and load runs that ran
    while a census of the same product was running (census_overlaps()), from stored run start
    and finish times (the ingested run's, else the orchestrator's); their histograms stay out of
    the breakdowns' (light-load) latency, which is the latency target's (timed_target) only.
    """
    start = as_of - timedelta(hours=720)
    bounds = {
        "start": start,
        "end": as_of,
        "d1": as_of - timedelta(hours=24),
        "d7": as_of - timedelta(hours=168),
        "margin": start - timedelta(days=1),
    }
    overlaps = overlap_runs(conn, bounds)
    geo = geo_rows(conn, bounds, overlaps, timed_target)
    pulses = pulse_runs(conn, start, as_of)
    census = latest_census(conn, as_of)
    outages = window_outages(conn, start, as_of)
    tested = tested_spans(conn, start, as_of)
    minutes = pulse_minutes(conn, start, as_of)
    by_test = traffic_by_test(conn, start, as_of)
    return {
        "pulse_minutes": minutes,
        "overlaps": overlaps,
        # The daily reference country of every runner region (the load waves' choice basis).
        "references": load_references(conn, start, as_of),
        "geo": geo,
        "pulses": pulses,
        "census": census,
        "outages": outages,
        "tested": tested,
        "events": recent_events(conn, as_of),
        "traffic": by_test,
    }


def overlap_runs(conn, bounds):
    """The pulse and load runs that ran during a census of the same product (census_overlaps())."""
    census_runs = conn.execute(
        f"SELECT br.product_id,br.batch_id,{RUN_TIMES}AND b.kind='census' ORDER BY 1,2", bounds
    ).fetchall()
    tested_runs = conn.execute(
        f"SELECT br.product_id,br.batch_id,br.run_key,{RUN_TIMES}AND b.kind IN ('pulse','load') "
        "ORDER BY 1,2",
        bounds,
    ).fetchall()
    return census_overlaps(census_runs, tested_runs)


def geo_rows(conn, bounds, overlaps, timed_target):
    """The window's run_geo_buckets per GEO_KEY, labelled, with their light-load histograms."""
    bounds = bounds | {
        "flagged": sorted(o["run_key"] for o in overlaps if o["run_key"] is not None),
        "latency_target": timed_target,
    }
    totals = conn.execute(
        f"SELECT {GEO_KEYS},sum(g.completed)::bigint AS completed,"
        "sum(g.successes)::bigint AS successes,sum(g.challenges)::bigint AS challenges,"
        f"sum(g.blocks)::bigint AS blocks {GEO_SOURCE}{GEO_WINDOW}"
        "GROUP BY 1,2,3,4,5,6 ORDER BY 1,2,3,4,5,6",
        bounds,
    ).fetchall()
    histograms = geo_histograms(conn, bounds)
    locations, networks, names = geo_labels(conn, totals)
    geo = []
    for t in totals:
        code = t["fallback"]
        geo.append(
            {
                "scope_id": t["scope_id"],
                "product_id": t["product_id"],
                "hours": t["hours"],
                "location": locations[t["location_id"]],
                "network": networks[t["network_id"]],
                "fallback": (code, names.get(code))
                if code and re.fullmatch("[A-Z]{2}", code)
                else None,
                **{k: t[k] for k in ("completed", "successes", "challenges", "blocks")},
                "histogram": histograms[tuple(t[k] for k in GEO_KEY)],
            }
        )
    return geo


def geo_histograms(conn, bounds):
    """The light-load latency histogram ([ms, n] pairs) of every GEO_KEY group."""
    histograms = defaultdict(list)
    for row in conn.execute(
        f"SELECT {GEO_KEYS},h.ms,sum(h.n)::bigint AS n {GEO_SOURCE}CROSS JOIN LATERAL "
        f"histogram(g.histogram_steps,g.histogram_counts::bigint[]) h {GEO_WINDOW}{LIGHT}"
        "GROUP BY 1,2,3,4,5,6,7 ORDER BY 1,2,3,4,5,6,7",
        bounds,
    ):
        histograms[tuple(row[k] for k in GEO_KEY)].append([row["ms"], row["n"]])
    return histograms


def geo_labels(conn, totals):
    """The places and networks the geo rows name, and every country's name."""
    locations = {
        r["location_id"]: tuple(r[f] for f in PLACE)
        for r in conn.execute(
            "SELECT * FROM enrichment_location_fields WHERE location_id=ANY(%s)",
            (sorted({t["location_id"] for t in totals}),),
        )
    }
    networks = {
        r["network_id"]: (r["asn"], r["asn_organization"], r["usage_type"])
        for r in conn.execute(
            "SELECT * FROM enrichment_network_fields WHERE network_id=ANY(%s)",
            (sorted({t["network_id"] for t in totals}),),
        )
    }
    names = {
        r["country_code"]: r["name"]
        for r in conn.execute("SELECT country_code,name FROM countries")
    }
    return locations, networks, names


def pulse_runs(conn, start, as_of):
    """Pulse slots a product was due in and that ended before the cutoff: its done runs. A run
    is only ever 'failed' by our side (no node, never started, went silent, the node shut
    down or erred, the upload was rejected), so those never count against the provider; a
    provider that is down shows as a done run whose requests failed. Operator-skipped or
    killed batches are not slots either, and load batches never are."""
    return conn.execute(
        "SELECT br.product_id,br.batch_id,b.target_id,b.scheduled_for,"
        "trim(b.country_code) AS country,br.status,br.requests,br.successes,br.run_key "
        "FROM batch_runs br JOIN test_batches b USING(batch_id) "
        "WHERE b.kind='pulse' AND b.status NOT IN ('skipped','killed') "
        "AND b.scheduled_for>=%s AND b.scheduled_for<%s AND br.finished_at<%s "
        "AND br.status='done' ORDER BY 3,1,2",
        (start, as_of, as_of),
    ).fetchall()


def latest_census(conn, as_of):
    """The latest finished census per product and country, with its cities, ASNs and duration."""
    census = []
    for r in conn.execute(
        "SELECT DISTINCT ON (br.product_id,b.country_code) br.product_id,"
        "b.country_code AS country,rr.slug AS region,b.target_id AS target,br.started_at,"
        "br.finished_at,br.requests,br.successes,br.unique_ips,br.stop_reason,"
        "CASE WHEN br.run_key IS NOT NULL THEN (SELECT count(DISTINCT (l.country_code,"
        "l.region_code,l.region,l.city)) FROM run_geo_buckets g JOIN enrichment_location_fields l "
        "USING(location_id) WHERE g.run_key=br.run_key AND g.successes>0 AND l.city IS NOT NULL) "
        "END AS cities,CASE WHEN br.run_key IS NOT NULL THEN (SELECT count(DISTINCT n.asn) "
        "FROM run_geo_buckets g JOIN enrichment_network_fields n USING(network_id) "
        "WHERE g.run_key=br.run_key AND g.successes>0) END AS asns "
        "FROM batch_runs br JOIN test_batches b USING(batch_id) "
        "JOIN runner_regions rr ON rr.region_id=b.region_id "
        "WHERE b.kind='census' AND br.status='done' AND br.finished_at<%s "
        "ORDER BY br.product_id,b.country_code,br.finished_at DESC,b.batch_id DESC",
        (as_of,),
    ):
        started = r.pop("started_at")
        r["duration_s"] = (
            (r["finished_at"] - started).total_seconds() if started and r["finished_at"] else None
        )
        census.append(r)
    return census


def window_outages(conn, start, as_of):
    """The outages overlapping the window. An outage closed after the cutoff was still open as
    of it."""
    return conn.execute(
        "SELECT o.outage_id,o.product_id,o.country_code AS country,r.slug AS region,o.source,"
        "o.started_at,CASE WHEN o.ended_at<=%(end)s THEN o.ended_at END AS ended_at "
        "FROM outages o LEFT JOIN runner_regions r ON r.region_id=o.region_id "
        "WHERE o.started_at<%(end)s AND (o.ended_at IS NULL OR o.ended_at>%(start)s) "
        "ORDER BY o.product_id,o.started_at,o.outage_id",
        {"start": start, "end": as_of},
    ).fetchall()


def tested_spans(conn, start, as_of):
    """Per product and country, the spans of the started runs other than load and browser."""
    return conn.execute(
        "SELECT br.product_id,b.country_code AS country,"
        "br.started_at,least(coalesce(br.finished_at,%(end)s),%(end)s) AS last "
        "FROM batch_runs br JOIN test_batches b USING(batch_id) "
        "WHERE b.kind NOT IN ('load','browser') AND br.started_at IS NOT NULL "
        "AND br.started_at<%(end)s AND coalesce(br.finished_at,%(end)s)>=%(start)s "
        "ORDER BY 1,2,br.started_at",
        {"start": start, "end": as_of},
    ).fetchall()


def pulse_minutes(conn, start, as_of):
    """The cohort's pulse interval in minutes (see scheduled_inputs()), or None."""
    minutes = conn.execute(
        "SELECT coalesce((SELECT min(pulse_interval_minutes) FROM targets WHERE pulse_enabled),"
        "(SELECT min(extract(epoch FROM d))/60 FROM (SELECT scheduled_for-lag(scheduled_for) "
        "OVER (ORDER BY scheduled_for) AS d FROM (SELECT DISTINCT scheduled_for FROM "
        "test_batches WHERE kind='pulse' AND scheduled_for>=%s AND scheduled_for<%s) s) g "
        "WHERE d>interval '0')) AS minutes",
        (start, as_of),
    ).fetchone()["minutes"]
    return float(minutes) if minutes is not None else None


def traffic_by_test(conn, start, as_of):
    """The window's wire bytes per test kind."""
    report = traffic.summary(conn, start=start, end=as_of, finished_before=as_of)
    return {
        "period": {"start": start, "end": as_of},
        "by_test": traffic.snapshot(report["by_test"]),
    }


def load_references(conn, start, as_of):
    """The daily reference country of every runner region in the window."""
    return conn.execute(
        "SELECT l.day::text AS day,r.slug AS region,trim(l.country_code) AS country,l.basis,"
        "l.p50_ms,"
        "l.successes,l.candidates FROM load_references l JOIN runner_regions r "
        "USING(region_id) WHERE l.day>=%s AND l.day<=%s ORDER BY 1,2",
        (start.date(), as_of.date()),
    ).fetchall()


def recent_events(conn, as_of):
    """The newest test_events before the cutoff."""
    return detect.list_events(conn, before=as_of, limit=EVENTS_IN_SNAPSHOT)
