"""The live tape's fast replay (TAPE-FAST-1): detect() over stored batches, an hour at a time.

The step replay (daily.tape, fast=False) calls detect() every 5 minutes, and detect asks the
database 7 to 9 questions per pulse run. Here each hour's batches, their runs, the runs' medians
and the hosting audit are one query each, and every pulse is judged by detect's own judging
functions (_pulse, _judge_pulse and the rest) against Memory, a detect.Lookup that answers from
state seeded once at the start and kept as each pulse is judged:

- each series' (country, target, product) done pulses with requests, the last baseline_batches
  before the span and then every pulse up to the hour's batches, with their medians;
- the series' incident and recovery events within INCIDENT_TTL;
- the pulse events of the batches the span judges or compares with (judged already? repeated
  spike?);
- the open census outages (a healthy pulse closes them) and the (product, ASN) pairs reported.

The rare writes stay detect's: an outage is opened and closed with outages.py's statements
(opening reads the slot's other targets' runs). Each event is at its own
time, as detect writes it, never after the moment of the step its batch finished in plus
settle_seconds (the step replay's detection), so a span begun by one replay and finished by
the other matches a span done by either alone.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import timedelta
from itertools import count

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import detect, proxy_login, pulse_no_exit, verdicts
from .outages import EARLIEST, INCIDENT_TTL, INCIDENTS, _batch_start, earliest
from .partitions import month_bounds
from .storage import partition

TICK = timedelta(microseconds=1)  # the watermark is exclusive: batches finished after it
HOUR = timedelta(hours=1)
MARKED = frozenset(detect.PULSE_KINDS)
SERIES_KINDS = (*INCIDENTS, "recovered")
DONE = "b.status IN ('done','failed')"
# The span's first pulse slot (its batches' history is read from before it).
FIRST = (
    "SELECT least(%s,min(scheduled_for)) AS first FROM test_batches b WHERE b.kind='pulse' "
    "AND b.finished_at>%s AND b.finished_at<=%s AND " + DONE
)
RUN_COLUMNS = (
    "b.country_code,b.target_id,br.product_id,br.batch_id,b.finished_at,b.scheduled_for,"
    "br.requests,br.successes,br.challenges,br.blocks,br.run_key"
)
PULSE_RUNS = "b.kind='pulse' AND br.status='done' AND br.requests>0"
# A baseline pulse (with COUNTED's r) is not mostly refused at login nor answered mostly "no exit"
# (outages._pulse_history).
MEASURED = f" AND {proxy_login.MEASURED_SQL} AND NOT {pulse_no_exit.EMPTY_SQL}"
# Only the runs whose network's country counted when they started (detect.country_rule()).
COUNTED = (
    "LEFT JOIN test_runs r ON r.run_key=br.run_key "
    f"{verdicts.counts_at('br.product_id', 'trim(b.country_code)', verdicts.RUN_AT)}"
)
# The last baseline_batches pulses before the first slot of each series the span judges.
SEED = (
    "WITH s AS (SELECT DISTINCT b.country_code,b.target_id,br.product_id FROM test_batches b "
    "JOIN batch_runs br USING(batch_id) WHERE " + PULSE_RUNS + " AND b.finished_at>%(lo)s "
    "AND b.finished_at<=%(hi)s AND " + DONE + "),h AS (SELECT " + RUN_COLUMNS + ","
    "row_number() OVER (PARTITION BY b.country_code,b.target_id,br.product_id "
    "ORDER BY b.scheduled_for DESC) AS n FROM test_batches b JOIN batch_runs br USING(batch_id) "
    + COUNTED
    + "JOIN s ON s.country_code=b.country_code AND s.target_id=b.target_id "
    "AND s.product_id=br.product_id WHERE "
    + PULSE_RUNS
    + MEASURED
    + " AND b.scheduled_for<%(first)s) "
    "SELECT * FROM h WHERE n<=%(n)s ORDER BY scheduled_for"
)
# Every series' pulses of the slots [from, to), whatever their batch's state (as detect reads).
SLOTS = (
    "SELECT "
    + RUN_COLUMNS
    + " FROM test_batches b JOIN batch_runs br USING(batch_id) "
    + COUNTED
    + "WHERE "
    + PULSE_RUNS
    + MEASURED
    + " AND b.scheduled_for>=%(lo)s AND b.scheduled_for<%(hi)s ORDER BY b.scheduled_for"
)
INCIDENT_EVENTS = (
    "SELECT e.event_id,e.at,e.kind,e.data,e.product_id,e.country_code,e.target_id,"
    "b.scheduled_for FROM test_events e JOIN test_batches b USING(batch_id) "
    "WHERE e.at>=%s AND b.kind='pulse' AND e.kind=ANY(%s)"
)
MARKS = "SELECT batch_id,product_id,kind,at FROM test_events WHERE at>=%s AND kind=ANY(%s)"
CENSUS_OPEN = (
    "SELECT product_id,country_code,started_at FROM outages "
    "WHERE source='census' AND ended_at IS NULL"
)
HOSTED = (
    "SELECT DISTINCT product_id,data->>'asn' AS asn FROM test_events WHERE kind='new_hosting_asn'"
)
BATCHES = (
    "SELECT b.*,r.slug AS region FROM test_batches b JOIN runner_regions r USING(region_id) "
    "WHERE b.finished_at>%s AND b.finished_at<=%s AND " + DONE + " "
    "ORDER BY b.finished_at,b.batch_id"
)
RUNS = (
    "SELECT br.*,r.started_at AS run_started,r.finished_at AS run_finished,"
    f"{proxy_login.REFUSED_SQL},{pulse_no_exit.NO_EXIT_SQL} "
    "FROM batch_runs br JOIN test_batches b USING(batch_id) "
    + COUNTED
    + "WHERE br.batch_id=ANY(%(batches)s) AND br.status='done' ORDER BY br.batch_id,br.product_id"
)
# detect._hosting's query for all the hour's runs at once, each within its own bounds.
HOSTING = (
    "SELECT u.run_key,n.asn,min(n.asn_organization) AS name,min(n.usage_type) AS usage_type "
    "FROM unnest(%s::bigint[],%s::timestamptz[],%s::timestamptz[]) u(run_key,lo,hi) "
    "JOIN run_geo_buckets g ON g.run_key=u.run_key "
    "AND (u.lo IS NULL OR g.bucket_start BETWEEN u.lo AND u.hi) "
    "JOIN enrichment_network_fields n USING(network_id) "
    "WHERE g.successes>0 AND n.asn IS NOT NULL AND n.usage_type=ANY(%s) "
)
EVENT_COLUMNS = "at,kind,batch_id,product_id,country_code,target_id,region_id,signal,severity,data"
INSERT = (
    "INSERT INTO test_events(" + EVENT_COLUMNS + ") SELECT " + EVENT_COLUMNS + " FROM "
    "unnest(%s::timestamptz[],%s::text[],%s::bigint[],%s::integer[],%s::text[],%s::text[],"
    "%s::smallint[],%s::smallint[],%s::text[],%s::jsonb[]) WITH ORDINALITY "
    "AS u(" + EVENT_COLUMNS + ",n) ORDER BY n"
)


class Memory(detect.Lookup):
    """detect.Lookup's answers from memory (the module docstring); its cursor writes outages."""

    def __init__(self, cur, flush):
        super().__init__(cur)
        self.flush = flush
        self.series: dict = {}  # (country, target, product) -> pulses by slot
        self.slots: dict = {}  # the same keys -> their scheduled_for, for bisect
        self.medians: dict = {}
        self.measured: set = set()
        self.incidents: dict = {}  # (product, country, target) -> [(order, at, kind, data)]
        self.marks: dict = {}  # (batch, product) -> [(kind, at)]
        self.census: dict = {}  # (product, country) -> [started_at] of open census outages
        self.order = count()

    def add(self, row):
        key = (row["country_code"], row["target_id"], row["product_id"])
        self.series.setdefault(key, []).append(row)
        self.slots.setdefault(key, []).append(row["scheduled_for"])

    def measure(self, cur, run_keys):
        """Read the medians of the runs not measured yet (one RUN_P50_SQL)."""
        wanted = {key for key in run_keys if key} - self.measured
        if wanted:
            self.medians |= detect.run_p50(cur, wanted)
            self.measured |= wanted

    def record(self, kind, batch, product, at, data):
        """Remember an event the replay wrote, as detect's queries would read it back."""
        if kind in MARKED:
            self.marks.setdefault((batch["batch_id"], product), []).append((kind, at))
        if kind in SERIES_KINDS and batch["kind"] == "pulse":
            key = (product, batch["country_code"], batch["target_id"])
            order = (batch["scheduled_for"], 1, next(self.order))
            self.incidents.setdefault(key, []).append((order, at, kind, data))

    def _marked(self, batch, product, kinds):
        found, after = self.marks.get((batch["batch_id"], product), ()), earliest(batch)
        return any(k in kinds and at >= after for k, at in found)

    def judged(self, batch, product):
        return self._marked(batch, product, MARKED)

    def spiked(self, previous, product, kind):
        return self._marked(previous, product, (kind,))

    def p50(self, run_keys):
        return {key: self.medians[key] for key in run_keys if key in self.medians}

    def history(self, cfg, batch, product):
        key = (batch["country_code"], batch["target_id"], product)
        end = bisect_left(self.slots.get(key, []), batch["scheduled_for"])
        start = max(0, end - cfg["baseline_batches"])
        return self.series.get(key, [])[start:end][::-1]

    def incident(self, batch, product, now):
        floor, slot = now - INCIDENT_TTL, batch["scheduled_for"]
        events = self.incidents.get((product, batch["country_code"], batch["target_id"]), ())
        found = max((e for e in events if e[1] >= floor and e[0][0] < slot), default=None)
        return {"kind": found[2], "data": found[3]} if found else None

    def close_census(self, batch, product):
        key = (product, batch["country_code"])
        start = _batch_start(batch)
        if not any(started < start for started in self.census.get(key, ())):
            return []
        closed = super().close_census(batch, product)
        self.census[key] = [started for started in self.census[key] if started >= start]
        return closed


class _Buffer:
    """detect._Emitter's interface; keeps the events in memory and writes them in one INSERT."""

    def __init__(self, cur):
        self.cur, self.now, self.count, self.hosting = cur, None, 0, set()
        self.reported_asns: set = set()  # (product, ASN text) reported before the replay
        self.rows: list = []
        self.memory = Memory(cur, self.flush)

    def __call__(self, kind, batch, product_id, verdict, data, at):
        signal, severity = verdict
        at = min(at or self.now, self.now)
        self.rows.append(
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
            )
        )
        self.memory.record(kind, batch, product_id, at, data)
        self.count += 1

    def reported(self, product_id, asn):
        return (product_id, str(asn)) in self.reported_asns

    def flush(self):
        if self.rows:
            for month in sorted({month_bounds(row[0])[0] for row in self.rows}):
                partition(self.cur.connection, "test_events", month)
            self.cur.execute(INSERT, [list(column) for column in zip(*self.rows, strict=True)])
            self.rows = []


class Replay:
    """detect() over the batches that finished in [since, until), an hour at a time; daily.tape
    drives it (the watermark checks and the commits)."""

    def __init__(self, conn, cfg, since, until, rule=None):
        self.conn, self.cfg = conn, cfg
        self.rule = detect.country_rule() if rule is None else rule
        self.cur = conn.cursor(row_factory=dict_row)
        self.emit = _Buffer(self.cur)
        self.memory = self.emit.memory
        self.settle = timedelta(seconds=cfg["settle_seconds"])
        self._seed(since - TICK, until - TICK)

    def _seed(self, lo, hi):
        cur, memory = self.cur, self.memory
        first = cur.execute(FIRST, (lo + TICK, lo, hi)).fetchone()["first"]
        params = {"lo": lo, "hi": hi, "first": first, "n": self.cfg["baseline_batches"]}
        seeded = cur.execute(SEED, params | verdicts.params(self.rule)).fetchall()
        for row in seeded:
            memory.add(row)
        memory.measure(cur, [row["run_key"] for row in seeded])
        self.loaded = first  # every series' pulses of earlier slots are in memory
        latest = [rows[-1]["scheduled_for"] for rows in memory.series.values()]
        since = min([first, *latest]) - EARLIEST  # the marks _marked can find (earliest)
        for row in cur.execute(MARKS, (since, list(MARKED))):
            memory.marks.setdefault((row["batch_id"], row["product_id"]), []).append(
                (row["kind"], row["at"])
            )
        for row in cur.execute(INCIDENT_EVENTS, (lo - INCIDENT_TTL, list(SERIES_KINDS))):
            key = (row["product_id"], row["country_code"], row["target_id"])
            order = (row["scheduled_for"], 0, row["event_id"])
            memory.incidents.setdefault(key, []).append(
                (order, row["at"], row["kind"], row["data"])
            )
        for row in cur.execute(CENSUS_OPEN):
            key = (row["product_id"], row["country_code"])
            memory.census.setdefault(key, []).append(row["started_at"])
        self.emit.reported_asns = {(row["product_id"], row["asn"]) for row in cur.execute(HOSTED)}

    def hour(self, upto, end, step):
        """Judge the batches finished in (upto, end] (upto on the step grid), write their events
        and move the watermark to end, in the caller's transaction; returns the events."""
        cur, emit, before = self.cur, self.emit, self.emit.count
        batches = cur.execute(BATCHES, (upto, end)).fetchall()
        runs = self._runs(batches)
        stamps = [self._stamp(batch["finished_at"], upto, end, step) for batch in batches]
        hosting = self._hosting(runs)
        for batch, stamp in zip(batches, stamps, strict=True):
            emit.now, found = stamp, runs.get(batch["batch_id"], [])
            if batch["kind"] == "pulse":
                detect._pulse(self.memory, self.cfg, batch, found, emit)
            elif batch["kind"] == "census":
                detect._overlap(cur, batch, found, emit)
            for run in found:
                if run["run_key"]:
                    detect._new_hosting(batch, run, hosting.get(run["run_key"], []), emit)
        emit.flush()
        detect.set_watermark(cur, end, end + self.settle)
        return emit.count - before

    def _stamp(self, finished, upto, end, step):
        """The moment of the step the batch finished in, plus settle_seconds: the step replay's
        detection, which no event of the batch is after."""
        steps = -((upto - finished) // step)
        return min(upto + steps * step, end) + self.settle

    def _runs(self, batches):
        """The batches' done runs by batch; the pulse history up to their slots and the medians
        are loaded with them."""
        cur, memory, runs = self.cur, self.memory, {}
        found = {"batches": [batch["batch_id"] for batch in batches]} | verdicts.params(self.rule)
        for run in cur.execute(RUNS, found):
            runs.setdefault(run["batch_id"], []).append(run)
        pulses = [batch for batch in batches if batch["kind"] == "pulse"]
        last = max((batch["scheduled_for"] for batch in pulses), default=None)
        keys = [run["run_key"] for batch in pulses for run in runs.get(batch["batch_id"], [])]
        if last is not None:
            slots = {"lo": self.loaded, "hi": last} | verdicts.params(self.rule)
            for row in cur.execute(SLOTS, slots).fetchall():
                memory.add(row)
                keys.append(row["run_key"])
            self.loaded = max(self.loaded, last)
        memory.measure(cur, keys)
        return runs

    def _hosting(self, runs):
        """detect._hosting's networks of every run with a run key: run key -> rows."""
        found = [run for rows in runs.values() for run in rows if run["run_key"]]
        if not found:
            return {}
        lows = [r["run_started"] and r["run_started"] - timedelta(minutes=5) for r in found]
        highs = [r["run_finished"] for r in found]
        query, params = HOSTING, [[r["run_key"] for r in found], lows, highs]
        params.append(self.cfg["hosting_usage_types"])
        if all(lows):  # the partitions to read
            query += "AND g.bucket_start>=%s AND g.bucket_start<=%s "
            params += [min(lows), max(highs)]
        networks: dict = {}
        for row in self.cur.execute(query + "GROUP BY 1,2 ORDER BY 1,2", params):
            networks.setdefault(row["run_key"], []).append(row)
        return networks
