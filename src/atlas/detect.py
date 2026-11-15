"""Live tape events from finished census and pulse batches, and their JSON form.

The orchestrator calls detect(conn, now) every minute inside its own transaction. Each call looks
at the batches that finished since the previous call (a watermark in `settings`), compares every
product's pulse with its trailing baseline for the same country and target, and writes
test_events. Thresholds are the [events] section of configs/methodology.toml (policy.EVENTS).

Each event is at the time it happened, never the detection time (TAPE-TIME-1): a pulse's
events at its run's own finish (the node's report; the batch's finish without one), a 'down' at
its outage's start (data.since), a 'recovered' at the outage's end (the healthy batch's start),
the audits at their runs' finish; never after the detection. A row can so be older than rows
written before it: a client polls by event id (the site API's `after`), not by `at`. The batch's
own finish time is in data.batch_finished_at.

Outages (the `outages` table) are opened and closed with the helpers in outages.py.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .calculated.policy import events as event_settings
from .outages import (
    INCIDENT_TTL,
    INCIDENTS,
    _batch_start,
    _close_outages,
    _exists,
    _had,
    _open_outage,
    _rate,
    run_end,
    runs_end,
)
from .partitions import month_bounds
from .regions import CONFIGS
from .storage import partition

METHODOLOGY_FILE = CONFIGS / "methodology.toml"
LOCK = 817452903
WATERMARK = "detect_watermark"
# Kinds a pulse batch run can produce; at most one evaluation per batch and product.
PULSE_KINDS = (
    "down",
    "success_drop",
    "recovered",
    "latency_spike",
    "block_spike",
    "pulse_finished",
)
CATEGORIES = {
    "success_drop": "incident",
    "latency_spike": "incident",
    "block_spike": "incident",
    "down": "incident",
    "recovered": "incident",
    "node_offline": "incident",
    "no_node": "incident",
    "latency_rise": "incident",
    "new_hosting_asn": "audit",
    "ip_overlap": "audit",
    # The daily peak-hour test and its hour estimates replace the census (orchestrator/events.py).
    "daily_test_started": "census",
    "daily_test_finished": "census",
    "estimate_hour_released": "census",
}
CATEGORY_SQL = (
    "CASE WHEN left(e.kind,7)='census_' OR e.kind IN "
    "('daily_test_started','daily_test_finished','estimate_hour_released') THEN 'census' "
    "WHEN left(e.kind,6)='pulse_' THEN 'pulse' "
    "WHEN e.kind IN ('new_hosting_asn','ip_overlap') THEN 'audit' ELSE 'incident' END"
)


def category(kind):
    """Live tape tab of an event kind: census_* and the daily test → census, pulse_* (pulse_down
    and pulse_recovered too) → pulse, audits, incidents."""
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


# Median successful latency of each run (%s: run keys), exactly as stats.Histogram computes it.
RUN_P50_SQL = (
    "WITH h AS (SELECT b.run_key,x.ms,sum(x.n) AS n FROM run_buckets b CROSS JOIN LATERAL "
    "histogram(b.histogram_steps,b.histogram_counts::bigint[]) x "
    "WHERE b.run_key=ANY(%s) GROUP BY 1,2), c AS (SELECT run_key,ms,"
    "sum(n) OVER (PARTITION BY run_key ORDER BY ms) AS seen,"
    "sum(n) OVER (PARTITION BY run_key) AS total FROM h) "
    "SELECT run_key,min(ms)::integer AS p50 FROM c WHERE seen>=ceil(total*0.5) "
    "GROUP BY run_key"
)


def run_p50(cur, run_keys):
    """Median successful latency of each run, exactly as stats.Histogram computes it."""
    if not run_keys:
        return {}
    rows = cur.execute(RUN_P50_SQL, (sorted(set(run_keys)),))
    return dict((row["run_key"], row["p50"]) for row in rows)


class _Emitter:
    """Writes events, each at its own time but never after now (the detection); counts them and
    keeps the (product, ASN) pairs it reported as new_hosting_asn in this call."""

    def __init__(self, cur, now):
        self.cur, self.now, self.count, self.hosting = cur, now, 0, set()
        self.months = {month_bounds(now)[0]}  # whose partitions the caller made

    def __call__(self, kind, batch, product_id, verdict, data, at):
        """Write one event of batch at `at` (None: now); verdict is its (signal, severity)."""
        signal, severity = verdict
        at = min(at or self.now, self.now)
        if month_bounds(at)[0] not in self.months:
            partition(self.cur.connection, "test_events", at)
            self.months.add(month_bounds(at)[0])
        self.cur.execute(
            "INSERT INTO test_events(at,kind,batch_id,product_id,country_code,target_id,"
            "region_id,signal,severity,data) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                at,
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

    def reported(self, product_id, asn):
        """Whether an earlier call reported the product's ASN as new_hosting_asn."""
        return _exists(
            self.cur,
            "SELECT 1 FROM test_events WHERE product_id=%s AND kind='new_hosting_asn' "
            "AND data->>'asn'=%s",
            (product_id, str(asn)),
        )


@dataclass
class _PulseRun:
    """One product's pulse in a batch, with its series' trailing baseline and open incident."""

    run: dict
    latency: int | None
    data: dict
    history: list
    past: dict
    incident: dict | None
    finished: object = None  # when the run finished (run_end), its events' time
    product: object = field(init=False)
    requests: int = field(init=False)
    successes: int = field(init=False)
    blocked: int = field(init=False)
    rate: float = field(init=False)
    block_share: float = field(init=False)
    base_rate: float | None = field(init=False)
    base_blocked: float | None = field(init=False)
    base_p50: int | None = field(init=False)
    open_kind: str | None = field(init=False)
    previous: dict | None = field(init=False)

    def __post_init__(self):
        run, history, past = self.run, self.history, self.past
        self.product, self.requests = run["product_id"], run["requests"]
        self.successes, self.blocked = run["successes"], run["challenges"] + run["blocks"]
        self.rate, self.block_share = self.successes / self.requests, self.blocked / self.requests
        base_requests = sum(h["requests"] for h in history)
        self.base_rate = _rate(sum(h["successes"] for h in history), base_requests)
        self.base_blocked = _rate(
            sum(h["challenges"] + h["blocks"] for h in history), base_requests
        )
        latencies = [past[h["run_key"]] for h in history if past.get(h["run_key"]) is not None]
        self.base_p50 = statistics.median_low(latencies) if latencies else None
        incident = self.incident
        self.open_kind = incident["kind"] if incident and incident["kind"] in INCIDENTS else None
        self.previous = history[0] if history else None

    def enough(self, cfg):
        """Whether the baseline and the pulse are large enough to judge success and blocks."""
        history, requests = len(self.history), self.requests
        return history >= cfg["min_baseline_batches"] and requests >= cfg["min_requests"]


class Lookup:
    """The facts the judging reads, from the database: detect()'s own queries. The tape's fast
    replay (detect_replay.Memory) answers the same questions from memory."""

    def __init__(self, cur):
        self.cur = cur

    def judged(self, batch, product):
        """Whether the batch already has one of the product's pulse events."""
        return _had(self.cur, batch, product, PULSE_KINDS)

    def spiked(self, previous, product, kind):
        """Whether the previous pulse reported this spike."""
        return _had(self.cur, previous, product, (kind,))

    def p50(self, run_keys):
        return run_p50(self.cur, run_keys)

    def close_census(self, batch, product):
        """End the product's census outages in the country (the closed rows)."""
        return _close_outages(self.cur, batch, product, source="census")

    def history(self, cfg, batch, product):
        return _pulse_history(self.cur, cfg, batch, product)

    def incident(self, batch, product, now):
        return _pulse_incident(self.cur, batch, product, now)


def _pulse(lookup, cfg, batch, runs, emit):
    p50 = lookup.p50([r["run_key"] for r in runs if r["run_key"]])
    common = {"region": batch["region"], "batch_finished_at": timestamp(batch["finished_at"])}
    for run in runs:
        product, requests = run["product_id"], run["requests"]
        if not requests or lookup.judged(batch, product):
            continue
        history = lookup.history(cfg, batch, product)
        past = lookup.p50([h["run_key"] for h in history if h["run_key"]])
        latency = p50.get(run["run_key"])
        data = common | {
            "requests": requests,
            "successes": run["successes"],
            "success_rate": run["successes"] / requests,
            "p50": latency,
            "challenges": run["challenges"],
            "blocks": run["blocks"],
        }
        incident = lookup.incident(batch, product, emit.now)
        pulse = _PulseRun(run, latency, data, history, past, incident, run_end(run))
        _judge_pulse(lookup, cfg, batch, emit, pulse)


def _pulse_history(cur, cfg, batch, product):
    """The series' trailing baseline: its last done pulses with requests before this batch."""
    return cur.execute(
        "SELECT br.batch_id,b.finished_at,b.scheduled_for,br.requests,br.successes,br.challenges,"
        "br.blocks,br.run_key FROM batch_runs br JOIN test_batches b USING(batch_id) "
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


def _judge_pulse(lookup, cfg, batch, emit, p):
    """Write the events of one product's pulse, in a fixed order."""
    before = emit.count
    handled = p.enough(cfg) and _judge_success(lookup.cur, cfg, batch, emit, p)
    through = p.requests >= cfg["min_requests"] and p.successes > 0
    # A 'down' ends with the first pulse that gets through, even below the baseline (a success
    # drop is judged afresh next time; PULSE-OUTAGE-CLOSE-1); a success_drop needs it back.
    recovered = through and (
        p.open_kind == "down"
        or (
            not handled
            and p.open_kind
            and p.rate >= p.incident["data"].get("baseline", 0) - cfg["success_drop_pp"]
        )
    )
    if recovered:  # the product got through in the country: its outage there is over
        _close_outages(lookup.cur, batch, p.product)
    if p.successes > 0:
        _census_recovered(lookup, batch, emit, p)
    if recovered:
        emit(
            "recovered",
            batch,
            p.product,
            (1, "info"),
            p.data
            | {
                "baseline": p.incident["data"].get("baseline"),
                "value": p.rate,
                "incident": p.open_kind,
            },
            _batch_start(batch),  # the outage's end (_close_outages)
        )
    _judge_spikes(lookup, cfg, batch, emit, p)
    if handled or emit.count > before or p.previous is None:
        return
    _pulse_changed(cfg, batch, emit, p)


def _judge_success(cur, cfg, batch, emit, p):
    """A 'down' or 'success_drop' against the baseline; whether the pulse was one of them."""
    if p.base_rate is None:
        return False
    if p.successes == 0 and p.base_rate > cfg["down_baseline"]:
        if p.open_kind != "down":
            since = _open_outage(cur, batch, p.product)
            emit(
                "down",
                batch,
                p.product,
                (-1, "critical"),
                p.data
                | {
                    "baseline": p.base_rate,
                    "value": p.rate,
                    "since": timestamp(since),
                    "source": "pulse",
                },
                since,
            )
        return True
    if p.rate < p.base_rate - cfg["success_drop_pp"]:
        if p.open_kind is None:
            severe = p.rate < p.base_rate - 2 * cfg["success_drop_pp"]
            emit(
                "success_drop",
                batch,
                p.product,
                (-1, "major" if severe else "minor"),
                p.data | {"baseline": p.base_rate, "value": p.rate},
                p.finished,
            )
        return True
    return False


def _census_recovered(lookup, batch, emit, p):
    """A census outage ends with any later pulse that got through."""
    for outage in lookup.close_census(batch, p.product):
        emit(
            "recovered",
            batch,
            p.product,
            (1, "info"),
            p.data
            | {
                "source": "census",
                "incident": "down",
                "since": timestamp(outage["started_at"]),
                "duration_s": round((outage["ended_at"] - outage["started_at"]).total_seconds(), 3),
                "outage_batch_id": outage["batch_id"],
            },
            outage["ended_at"],
        )


def _repeated(lookup, p, kind):
    """A spike that the previous pulse already reported stays silent while it lasts."""
    return p.previous is not None and lookup.spiked(p.previous, p.product, kind)


def _judge_spikes(lookup, cfg, batch, emit, p):
    """A 'block_spike' or 'latency_spike' against the baseline, once per spike."""
    if (
        p.enough(cfg)
        and p.base_blocked is not None
        and p.block_share > p.base_blocked + cfg["block_spike_pp"]
        and not _repeated(lookup, p, "block_spike")
    ):
        severe = p.block_share > p.base_blocked + 2 * cfg["block_spike_pp"]
        emit(
            "block_spike",
            batch,
            p.product,
            (-1, "major" if severe else "minor"),
            p.data | {"baseline": p.base_blocked, "value": p.block_share},
            p.finished,
        )
    if (
        len(p.history) >= cfg["min_baseline_batches"]
        and p.latency
        and p.base_p50
        and p.successes >= cfg["min_latency_samples"]
        and p.latency > cfg["latency_spike_ratio"] * p.base_p50
        and not _repeated(lookup, p, "latency_spike")
    ):
        emit(
            "latency_spike",
            batch,
            p.product,
            (-1, "minor"),
            p.data | {"baseline": p.base_p50, "value": p.latency},
            p.finished,
        )


def _pulse_changed(cfg, batch, emit, p):
    """Otherwise the pulse is silent unless it differs from the previous one."""
    previous, rate, latency = p.previous, p.rate, p.latency
    prior_rate = previous["successes"] / previous["requests"]
    prior_p50 = p.past.get(previous["run_key"])
    prior_blocked = previous["challenges"] + previous["blocks"]
    rate_moved = abs(rate - prior_rate) >= cfg["pulse_change_pp"]
    latency_moved = bool(latency and prior_p50) and (
        max(latency / prior_p50, prior_p50 / latency) >= cfg["pulse_latency_ratio"]
    )
    blocks_moved = (p.blocked > 0) != (prior_blocked > 0)
    if not (rate_moved or latency_moved or blocks_moved):
        return
    if rate_moved:
        signal = 1 if rate > prior_rate else -1
    elif latency_moved:
        signal = 1 if latency < prior_p50 else -1
    else:
        signal = -1 if p.blocked else 1
    emit(
        "pulse_finished",
        batch,
        p.product,
        (signal, "info"),
        p.data
        | {
            "value": rate,
            "previous": {
                "success_rate": prior_rate,
                "successes": previous["successes"],
                "requests": previous["requests"],
                "p50": prior_p50,
                "challenges": previous["challenges"],
                "blocks": previous["blocks"],
            },
        },
        p.finished,
    )


def _hosting(cur, cfg, batch, runs, emit):
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
            "FROM run_geo_buckets g JOIN enrichment_network_fields n USING(network_id) "
            "WHERE g.run_key=%s " + bounds + "AND g.successes>0 AND n.asn IS NOT NULL "
            "AND n.usage_type=ANY(%s) GROUP BY n.asn ORDER BY n.asn",
            (*params, cfg["hosting_usage_types"]),
        ).fetchall()
        _new_hosting(batch, run, networks, emit)


def _new_hosting(batch, run, networks, emit):
    """A new_hosting_asn for each of the run's hosting networks (asn order) not reported yet."""
    for network in networks:
        key = (run["product_id"], network["asn"])
        if key in emit.hosting or emit.reported(*key):
            continue
        emit.hosting.add(key)
        emit(
            "new_hosting_asn",
            batch,
            run["product_id"],
            (-1, "minor"),
            {
                "region": batch["region"],
                "asn": network["asn"],
                "name": network["name"],
                "usage_type": network["usage_type"],
            },
            run_end(run),
        )


# The IPs of a batch's done runs, as (product_id, provider, ip_id) rows for overlaps().
BATCH_IPS = (
    "SELECT DISTINCT br.product_id,p.provider,io.ip_id "
    "FROM batch_runs br JOIN products p USING(product_id) "
    "JOIN ip_observations io ON io.run_key=br.run_key "
    "WHERE br.batch_id=%s AND br.status='done'"
)


# The shared addresses as sets, each step a hash aggregate or join, so the cost grows with the
# rows (AUDIT-DAILY-2): d holds each address's providers once, s the addresses of two or more.
SHARED = (
    "d AS (SELECT DISTINCT ip_id,provider FROM o),"
    "s AS (SELECT ip_id FROM d GROUP BY ip_id HAVING count(*)>1) "
)


def overlaps(cur, source, params):
    """(product_id, data) of each product, in order, that shares exit addresses with another
    provider among the rows `source` selects (distinct (product_id, provider, ip_id), with
    params; a product has one provider): its share of shared addresses, their count, its unique
    addresses and the count per provider.

    A row's address is shared when two or more providers have it (s). The peers join only the
    shared addresses' rows to their other providers, one row per (product, address, provider)."""
    query = "WITH o AS MATERIALIZED (" + source + ")," + SHARED
    totals = {
        row["product_id"]: row
        for row in cur.execute(
            query + "SELECT o.product_id,count(*) AS unique_ips,count(s.ip_id) AS shared "
            "FROM o LEFT JOIN s USING(ip_id) GROUP BY 1",
            params,
        )
    }
    peers = {}
    for row in cur.execute(
        query + "SELECT a.product_id,b.provider,count(*) AS shared FROM (SELECT o.* FROM o "
        "JOIN s USING(ip_id)) a JOIN d b ON b.ip_id=a.ip_id AND b.provider<>a.provider "
        "GROUP BY 1,2 ORDER BY 1,2",
        params,
    ):
        peers.setdefault(row["product_id"], {})[row["provider"]] = row["shared"]
    return [
        (
            product,
            {
                "value": row["shared"] / row["unique_ips"],
                "shared_ips": row["shared"],
                "unique_ips": row["unique_ips"],
                "peers": peers.get(product, {}),
            },
        )
        for product, row in sorted(totals.items())
        if row["shared"]
    ]


def _overlap(cur, batch, runs, emit):
    """Exit addresses a product's census shared with other providers in the same census slot."""
    if sum(1 for r in runs if r["run_key"]) < 2:
        return
    for product, data in overlaps(cur, BATCH_IPS, (batch["batch_id"],)):
        if _had(cur, batch, product, ("ip_overlap",)):
            continue
        at = runs_end(batch, runs)
        emit("ip_overlap", batch, product, (0, "info"), {"region": batch["region"]} | data, at)


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
    for batch in batches:
        runs = cur.execute(
            "SELECT br.*,r.started_at AS run_started,r.finished_at AS run_finished "
            "FROM batch_runs br LEFT JOIN test_runs r ON r.run_key=br.run_key "
            "WHERE br.batch_id=%s AND br.status='done' ORDER BY br.product_id",
            (batch["batch_id"],),
        ).fetchall()
        if batch["kind"] == "pulse":
            _pulse(Lookup(cur), cfg, batch, runs, emit)
        elif batch["kind"] == "census":
            _overlap(cur, batch, runs, emit)
        # Load runs (latency under load) only feed the hosting-ASN audit.
        _hosting(cur, cfg, batch, runs, emit)
    set_watermark(cur, upto, now)
    return emit.count


def set_watermark(cur, upto, now):
    """detect() judges the batches that finish after `upto` next (the tape's replay sets it)."""
    cur.execute(
        "INSERT INTO settings(key,value,updated_at) VALUES (%s,%s,%s) ON CONFLICT(key) "
        "DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (WATERMARK, Jsonb({"finished_at": upto.isoformat()}), now),
    )
