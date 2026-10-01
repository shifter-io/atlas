"""Live tape events from finished census and pulse batches, and their JSON form.

The orchestrator calls detect(conn, now) every minute inside its own transaction. Each call looks
at the batches that finished since the previous call (a watermark in `settings`), compares every
product's pulse with its trailing baseline for the same country and target, and writes
test_events. Thresholds are the [events] section of configs/methodology.toml (policy.EVENTS).

Events are stamped with the detection time, not the batch time, so a client polling with
`since=<newest at it has seen>` never misses one. The batch's own finish time is in
data.batch_finished_at.

Outages (the `outages` table) are the time a product was down in a country. A pulse 'down'
opens one (from its last healthy pulse there) and the pulse 'recovered' that ends the incident
closes it (at the start of that healthy batch). A census that stalled opens one in the
orchestrator (orchestrator.batches.census_down); any later healthy pulse in the country closes
it here, with a 'recovered' event of source 'census'.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .calculated.policy import events as event_settings
from .regions import CONFIGS
from .storage import partition

METHODOLOGY_FILE = CONFIGS / "methodology.toml"
LOCK = 817452903
WATERMARK = "detect_watermark"
# Incidents that stay open until a later pulse of the same series is healthy again.
INCIDENTS = ("down", "success_drop")
# Kinds a pulse batch run can produce; at most one evaluation per batch and product.
PULSE_KINDS = (
    "down",
    "success_drop",
    "recovered",
    "latency_spike",
    "block_spike",
    "pulse_finished",
)
# An incident older than this is no longer open (bounds the event index scan).
INCIDENT_TTL = timedelta(days=7)
CATEGORIES = {
    "success_drop": "incident",
    "latency_spike": "incident",
    "block_spike": "incident",
    "down": "incident",
    "recovered": "incident",
    "node_offline": "incident",
    "no_node": "incident",
    "new_hosting_asn": "audit",
    "ip_overlap": "audit",
}
CATEGORY_SQL = (
    "CASE WHEN left(e.kind,7)='census_' THEN 'census' WHEN left(e.kind,6)='pulse_' THEN 'pulse' "
    "WHEN e.kind IN ('new_hosting_asn','ip_overlap') THEN 'audit' ELSE 'incident' END"
)


def category(kind):
    """Live tape tab of an event kind: census_* → census, pulse_* → pulse, audits, incidents."""
    if kind.startswith("census_"):
        return "census"
    if kind.startswith("pulse_"):
        return "pulse"
    return CATEGORIES.get(kind, "incident")


def timestamp(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def event_json(row):
    """The contract shape shared by /v1/events and the snapshot's `events`."""
    provider = row["provider"]
    return {
        "id": row["event_id"],
        "at": timestamp(row["at"]),
        "kind": row["kind"],
        "category": category(row["kind"]),
        # Events that concern no single product (a skipped census, an offline node) keep empty
        # strings, so consumers can always split "provider/product".
        "product": f"{provider}/{row['name']}" if provider else "",
        "provider": provider or "",
        "country": row["country_code"],
        "region": row["region"],
        "target": row["target_id"],
        "signal": row["signal"],
        "severity": row["severity"],
        "data": row["data"],
    }


def list_events(
    conn, *, product=None, kinds=None, category=None, since=None, before=None, limit=100
):
    """Newest first. product is "provider" or "provider/product"; since and before are exclusive."""
    clauses, params = [], []
    if product:
        provider, _, name = product.partition("/")
        clauses.append("p.provider=%s")
        params.append(provider)
        if name:
            clauses.append("p.name=%s")
            params.append(name)
    if kinds:
        clauses.append("e.kind=ANY(%s)")
        params.append(list(kinds))
    if category:
        clauses.append(CATEGORY_SQL + "=%s")
        params.append(category)
    if since is not None:
        clauses.append("e.at>%s")
        params.append(since)
    if before is not None:
        clauses.append("e.at<%s")
        params.append(before)
    rows = (
        conn.cursor(row_factory=dict_row)
        .execute(
            "SELECT e.event_id,e.at,e.kind,p.provider,p.name,e.country_code,"
            "coalesce(r.slug,e.data->>'region') AS region,e.target_id,e.signal,e.severity,e.data "
            "FROM test_events e LEFT JOIN products p ON p.product_id=e.product_id "
            "LEFT JOIN runner_regions r ON r.region_id=e.region_id "
            + ("WHERE " + " AND ".join(clauses) + " " if clauses else "")
            + "ORDER BY e.at DESC,e.event_id DESC LIMIT %s",
            (*params, limit),
        )
        .fetchall()
    )
    return [event_json(row) for row in rows]


def thresholds(path=None):
    return event_settings(path or METHODOLOGY_FILE)


def run_p50(cur, run_keys):
    """Median successful latency of each run, exactly as stats.Histogram computes it."""
    if not run_keys:
        return {}
    return dict(
        (row["run_key"], row["p50"])
        for row in cur.execute(
            "WITH h AS (SELECT b.run_key,x.ms,sum(x.n) AS n FROM run_buckets b CROSS JOIN LATERAL "
            "histogram(b.histogram_steps,b.histogram_counts::bigint[]) x "
            "WHERE b.run_key=ANY(%s) GROUP BY 1,2), c AS (SELECT run_key,ms,"
            "sum(n) OVER (PARTITION BY run_key ORDER BY ms) AS seen,"
            "sum(n) OVER (PARTITION BY run_key) AS total FROM h) "
            "SELECT run_key,min(ms)::integer AS p50 FROM c WHERE seen>=ceil(total*0.5) "
            "GROUP BY run_key",
            (sorted(set(run_keys)),),
        )
    )


class _Emitter:
    def __init__(self, cur, now):
        self.cur, self.now, self.count = cur, now, 0

    def __call__(self, kind, batch, product_id, signal, severity, data):
        self.cur.execute(
            "INSERT INTO test_events(at,kind,batch_id,product_id,country_code,target_id,"
            "region_id,signal,severity,data) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                self.now,
                kind,
                batch["batch_id"],
                product_id,
                batch["country_code"],
                batch["target_id"],
                batch["region_id"],
                signal,
                severity,
                Jsonb(data),
            ),
        )
        self.count += 1


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
    """End the product's open outages in the country that began before this healthy batch."""
    start = _batch_start(batch)
    return cur.execute(
        "UPDATE outages SET ended_at=greatest(started_at,%s) WHERE product_id=%s "
        "AND country_code=%s AND ended_at IS NULL AND started_at<%s "
        + ("AND source=%s " if source else "")
        + "RETURNING outage_id,batch_id,started_at,ended_at",
        (start, product, batch["country_code"], start, *((source,) if source else ())),
    ).fetchall()


def _rate(numerator, requests):
    return numerator / requests if requests else None


def _pulse(cur, cfg, batch, runs, emit):
    p50 = run_p50(cur, [r["run_key"] for r in runs if r["run_key"]])
    common = {"region": batch["region"], "batch_finished_at": timestamp(batch["finished_at"])}
    for run in runs:
        product, requests = run["product_id"], run["requests"]
        if not requests or _had(cur, batch["batch_id"], product, PULSE_KINDS, batch["finished_at"]):
            continue
        successes, blocked = run["successes"], run["challenges"] + run["blocks"]
        rate, block_share = successes / requests, blocked / requests
        history = cur.execute(
            "SELECT br.batch_id,b.finished_at,br.requests,br.successes,br.challenges,br.blocks,"
            "br.run_key FROM batch_runs br JOIN test_batches b USING(batch_id) "
            "WHERE b.kind='pulse' AND b.country_code=%s AND b.target_id=%s AND br.product_id=%s "
            "AND br.status='done' AND br.requests>0 AND b.scheduled_for<%s "
            "ORDER BY b.scheduled_for DESC LIMIT %s",
            (
                batch["country_code"],
                batch["target_id"],
                product,
                batch["scheduled_for"],
                cfg["baseline_batches"],
            ),
        ).fetchall()
        past = run_p50(cur, [h["run_key"] for h in history if h["run_key"]])
        base_requests = sum(h["requests"] for h in history)
        base_rate = _rate(sum(h["successes"] for h in history), base_requests)
        base_blocked = _rate(sum(h["challenges"] + h["blocks"] for h in history), base_requests)
        latencies = [past[h["run_key"]] for h in history if past.get(h["run_key"]) is not None]
        base_p50 = statistics.median_low(latencies) if latencies else None
        enough = len(history) >= cfg["min_baseline_batches"] and requests >= cfg["min_requests"]
        latency = p50.get(run["run_key"])
        data = common | {
            "requests": requests,
            "successes": successes,
            "success_rate": rate,
            "p50": latency,
            "challenges": run["challenges"],
            "blocks": run["blocks"],
        }
        # The series state as of this batch: events of earlier batches only, so judging a batch
        # again (after a watermark reset) reaches the same verdict.
        incident = cur.execute(
            "SELECT e.kind,e.data FROM test_events e JOIN test_batches b USING(batch_id) "
            "WHERE e.product_id=%s AND e.at>=%s AND e.country_code=%s AND e.target_id=%s "
            "AND b.kind='pulse' AND e.kind=ANY(%s) AND b.scheduled_for<%s "
            "ORDER BY b.scheduled_for DESC,e.event_id DESC LIMIT 1",
            (
                product,
                emit.now - INCIDENT_TTL,
                batch["country_code"],
                batch["target_id"],
                [*INCIDENTS, "recovered"],
                batch["scheduled_for"],
            ),
        ).fetchone()
        open_kind = incident["kind"] if incident and incident["kind"] in INCIDENTS else None
        previous = history[0] if history else None

        def repeated(kind, previous=previous, product=product):
            # A spike that the previous pulse already reported stays silent while it lasts.
            return previous is not None and _had(
                cur, previous["batch_id"], product, (kind,), previous["finished_at"]
            )

        before = emit.count
        handled = False
        if enough and base_rate is not None:
            if successes == 0 and base_rate > cfg["down_baseline"]:
                handled = True
                if open_kind != "down":
                    since = _open_outage(cur, batch, product)
                    emit(
                        "down",
                        batch,
                        product,
                        -1,
                        "critical",
                        data
                        | {
                            "baseline": base_rate,
                            "value": rate,
                            "since": timestamp(since),
                            "source": "pulse",
                        },
                    )
            elif rate < base_rate - cfg["success_drop_pp"]:
                handled = True
                if open_kind is None:
                    severe = rate < base_rate - 2 * cfg["success_drop_pp"]
                    emit(
                        "success_drop",
                        batch,
                        product,
                        -1,
                        "major" if severe else "minor",
                        data | {"baseline": base_rate, "value": rate},
                    )
        recovered = (
            not handled
            and open_kind
            and requests >= cfg["min_requests"]
            and successes > 0
            and rate >= incident["data"].get("baseline", 0) - cfg["success_drop_pp"]
        )
        if recovered and not _still_down(cur, batch, product, emit.now):
            _close_outages(cur, batch, product)
        if successes > 0:
            # A census outage ends with any later pulse that got through.
            for outage in _close_outages(cur, batch, product, source="census"):
                emit(
                    "recovered",
                    batch,
                    product,
                    1,
                    "info",
                    data
                    | {
                        "source": "census",
                        "incident": "down",
                        "since": timestamp(outage["started_at"]),
                        "duration_s": round(
                            (outage["ended_at"] - outage["started_at"]).total_seconds(), 3
                        ),
                        "outage_batch_id": outage["batch_id"],
                    },
                )
        if recovered:
            emit(
                "recovered",
                batch,
                product,
                1,
                "info",
                data
                | {
                    "baseline": incident["data"].get("baseline"),
                    "value": rate,
                    "incident": open_kind,
                },
            )
        if (
            enough
            and base_blocked is not None
            and block_share > base_blocked + cfg["block_spike_pp"]
            and not repeated("block_spike")
        ):
            severe = block_share > base_blocked + 2 * cfg["block_spike_pp"]
            emit(
                "block_spike",
                batch,
                product,
                -1,
                "major" if severe else "minor",
                data | {"baseline": base_blocked, "value": block_share},
            )
        if (
            len(history) >= cfg["min_baseline_batches"]
            and latency
            and base_p50
            and successes >= cfg["min_latency_samples"]
            and latency > cfg["latency_spike_ratio"] * base_p50
            and not repeated("latency_spike")
        ):
            emit(
                "latency_spike",
                batch,
                product,
                -1,
                "minor",
                data | {"baseline": base_p50, "value": latency},
            )
        if handled or emit.count > before or previous is None:
            continue
        # Otherwise the pulse is silent unless it differs from the previous one.
        prior_rate = previous["successes"] / previous["requests"]
        prior_p50 = past.get(previous["run_key"])
        prior_blocked = previous["challenges"] + previous["blocks"]
        rate_moved = abs(rate - prior_rate) >= cfg["pulse_change_pp"]
        latency_moved = bool(latency and prior_p50) and (
            max(latency / prior_p50, prior_p50 / latency) >= cfg["pulse_latency_ratio"]
        )
        blocks_moved = (blocked > 0) != (prior_blocked > 0)
        if not (rate_moved or latency_moved or blocks_moved):
            continue
        if rate_moved:
            signal = 1 if rate > prior_rate else -1
        elif latency_moved:
            signal = 1 if latency < prior_p50 else -1
        else:
            signal = -1 if blocked else 1
        emit(
            "pulse_finished",
            batch,
            product,
            signal,
            "info",
            data
            | {
                "value": rate,
                "previous": {
                    "success_rate": prior_rate,
                    "p50": prior_p50,
                    "challenges": previous["challenges"],
                    "blocks": previous["blocks"],
                },
            },
        )


def _hosting(cur, cfg, batch, runs, emit, seen):
    """First datacenter or hosting ASN a product's exit addresses land in (events as memory)."""
    for run in runs:
        if not run["run_key"]:
            continue
        bounds = ""
        params = [run["run_key"]]
        if run["run_started"] is not None:
            bounds = "AND g.bucket_start>=%s AND g.bucket_start<=%s "
            params += [run["run_started"] - timedelta(minutes=5), run["run_finished"]]
        networks = cur.execute(
            "SELECT n.asn,min(n.asn_organization) AS name,min(n.usage_type) AS usage_type "
            "FROM run_geo_buckets g JOIN enrichment_networks n USING(network_id) "
            "WHERE g.run_key=%s " + bounds + "AND g.successes>0 AND n.asn IS NOT NULL "
            "AND n.usage_type=ANY(%s) GROUP BY n.asn ORDER BY n.asn",
            (*params, cfg["hosting_usage_types"]),
        ).fetchall()
        for network in networks:
            key = (run["product_id"], network["asn"])
            if key in seen or _exists(
                cur,
                "SELECT 1 FROM test_events WHERE product_id=%s AND kind='new_hosting_asn' "
                "AND data->>'asn'=%s",
                (run["product_id"], str(network["asn"])),
            ):
                continue
            seen.add(key)
            emit(
                "new_hosting_asn",
                batch,
                run["product_id"],
                -1,
                "minor",
                {
                    "region": batch["region"],
                    "asn": network["asn"],
                    "name": network["name"],
                    "usage_type": network["usage_type"],
                },
            )


def _overlap(cur, batch, runs, emit):
    """Exit addresses a product's census shared with other providers in the same census slot."""
    if sum(1 for r in runs if r["run_key"]) < 2:
        return
    query = (
        "WITH o AS MATERIALIZED (SELECT DISTINCT br.product_id,p.provider,io.ip_id "
        "FROM batch_runs br JOIN products p USING(product_id) "
        "JOIN ip_observations io ON io.run_key=br.run_key "
        "WHERE br.batch_id=%s AND br.status='done') "
    )
    totals = {
        row["product_id"]: row
        for row in cur.execute(
            query + "SELECT a.product_id,count(*) AS unique_ips,count(*) FILTER (WHERE EXISTS "
            "(SELECT 1 FROM o b WHERE b.ip_id=a.ip_id AND b.provider<>a.provider)) AS shared "
            "FROM o a GROUP BY 1",
            (batch["batch_id"],),
        )
    }
    peers = {}
    for row in cur.execute(
        query + "SELECT a.product_id,b.provider,count(DISTINCT a.ip_id) AS shared FROM o a "
        "JOIN o b ON b.ip_id=a.ip_id AND b.provider<>a.provider GROUP BY 1,2 ORDER BY 1,2",
        (batch["batch_id"],),
    ):
        peers.setdefault(row["product_id"], {})[row["provider"]] = row["shared"]
    for product, row in sorted(totals.items()):
        if not row["shared"] or _had(
            cur, batch["batch_id"], product, ("ip_overlap",), batch["finished_at"]
        ):
            continue
        emit(
            "ip_overlap",
            batch,
            product,
            0,
            "info",
            {
                "region": batch["region"],
                "value": row["shared"] / row["unique_ips"],
                "shared_ips": row["shared"],
                "unique_ips": row["unique_ips"],
                "peers": peers.get(product, {}),
            },
        )


def detect(conn, now, *, config=None):
    """Write the events of every batch that finished since the last call; returns how many.

    Only batches finished at least settle_seconds before `now` are taken, so a barrier that
    commits a moment late is not skipped; the watermark then advances to that settle point.
    Running it again for the same `now` (or an earlier one) writes nothing.
    """
    cfg = config or thresholds()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK,))
    upto = now - timedelta(seconds=cfg["settle_seconds"])
    mark = cur.execute(
        "SELECT value FROM settings WHERE key=%s FOR UPDATE", (WATERMARK,)
    ).fetchone()
    since = (
        datetime.fromisoformat(mark["value"]["finished_at"])
        if mark
        else upto - timedelta(minutes=cfg["initial_lookback_minutes"])
    )
    if upto <= since:
        return 0
    batches = cur.execute(
        "SELECT b.*,r.slug AS region FROM test_batches b JOIN runner_regions r USING(region_id) "
        "WHERE b.finished_at>%s AND b.finished_at<=%s AND b.status IN ('done','failed') "
        "ORDER BY b.finished_at,b.batch_id",
        (since, upto),
    ).fetchall()
    partition(conn, "test_events", now)
    emit = _Emitter(cur, now)
    seen = set()
    for batch in batches:
        runs = cur.execute(
            "SELECT br.*,r.started_at AS run_started,r.finished_at AS run_finished "
            "FROM batch_runs br LEFT JOIN test_runs r ON r.run_key=br.run_key "
            "WHERE br.batch_id=%s AND br.status='done' ORDER BY br.product_id",
            (batch["batch_id"],),
        ).fetchall()
        if batch["kind"] == "pulse":
            _pulse(cur, cfg, batch, runs, emit)
        else:
            _overlap(cur, batch, runs, emit)
        _hosting(cur, cfg, batch, runs, emit, seen)
    cur.execute(
        "INSERT INTO settings(key,value,updated_at) VALUES (%s,%s,%s) ON CONFLICT(key) "
        "DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (WATERMARK, Jsonb({"finished_at": upto.isoformat()}), now),
    )
    return emit.count
