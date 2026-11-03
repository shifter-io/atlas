"""SCALE-GEN-1a: the rows of the synthetic data set, written by COPY (generate.py drives it).

Daily tests are stored the way `daily ingest` stores one (storage.ingest, then its estimated
hours) and pulses the way the orchestrator stores a pulse batch (no IP rows, geo rows from the
node). Every id is explicit, so the same settings and seed give the same rows; generate.py moves
the identity sequences past them afterwards.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from datetime import UTC, datetime, time, timedelta

import synthetic
from psycopg.types.json import Jsonb
from synthetic import Model, address, apportion, mix

from proxy_index import __version__
from proxy_index.partitions import DAILY, partition
from proxy_index.stats import Histogram
from proxy_index.targets import registry

MARKER = "scale_generator"  # summary key of every run this generator writes (holds the seed)
TARGET = "generic_http"
DAILY_CONCURRENCY, PULSE_CONCURRENCY, TIMEOUT = 1000, 25, 15
BODY, UP, DOWN = 14, 180, 420  # bytes per request
BATCH = 20000  # COPY lines per write


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S+00")


def copy_lines(conn, table: str, columns: str, lines) -> None:
    """COPY tab-separated text lines (values never hold tabs, newlines or backslashes)."""
    with conn.cursor().copy(f"COPY {table}({columns}) FROM STDIN") as copy:
        batch = []
        for line in lines:
            batch.append(line)
            if len(batch) == BATCH:
                copy.write("\n".join(batch) + "\n")
                batch.clear()
        if batch:
            copy.write("\n".join(batch) + "\n")


def copy_rows(conn, table: str, columns: str, rows) -> None:
    with conn.cursor().copy(f"COPY {table}({columns}) FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def steps(bins) -> tuple[list[int], list[int]]:
    """Histogram bins as stored: millisecond gaps (the first absolute) and counts."""
    ms = [m for m, _ in bins]
    return [b - a for a, b in zip([0, *ms], ms, strict=False)], [n for _, n in bins]


def target() -> dict:
    known = registry.get(TARGET)
    url, version = known.build_request().url, known.version
    return {"id": TARGET, "version": version, "url": url, "engine": known.engine}


class Writer:
    """Writes the model's rows; `regions` maps a country code to (region_id, region slug)."""

    def __init__(self, conn, model: Model, regions: dict[str, tuple[int, str]]):
        self.conn, self.model, self.regions = conn, model, regions
        self.settings = model.settings
        self.target = target()
        self.created = self.settings.day_start(1)

    # One-time setup: networks, places, pools and scopes.

    def setup(self) -> None:
        conn, settings = self.conn, self.settings
        for n, network in enumerate(settings.networks):
            product = self.model.formulas[n].get("product", "residential")
            conn.execute("INSERT INTO providers VALUES (%s)", (network,))
            conn.execute(
                "INSERT INTO products(product_id,provider,name) OVERRIDING SYSTEM VALUE "
                "VALUES (%s,%s,%s)",
                (n + 1, network, product),
            )
        self.places()
        copy_rows(conn, "pool_stats", POOL_COLUMNS, (self.pool(lane) for lane in self.model.lanes))
        scopes = [self.scope(lane, daily) for lane in self.model.lanes for daily in (True, False)]
        copy_rows(conn, "scope_stats", SCOPE_COLUMNS, scopes)

    def places(self) -> None:
        """Synthetic city and ASN lists per country; id 1 is the unknown member of each."""
        place, location = [(1, None, None, None, None, None)], [(1, 1, None, None)]
        asn, network = [(1, None, None)], [(1, 1, None, None, None, None)]
        for c, code in enumerate(self.settings.countries):
            item, name = self.model.places[c], self.model.names[code]
            lat = -50 + 110 * synthetic.unit(self.settings.seed, "lat", c)
            lon = -170 + 340 * synthetic.unit(self.settings.seed, "lon", c)
            for i in range(item.cities):
                ident, region = item.location + i, i * item.regions // item.cities + 1
                city = f"{name} City {i + 1:04d}"
                place.append((ident, code, name, f"R{region:02d}", f"{name} Region {region}", city))
                spot = (round(lat + i % 97 / 100, 4), round(lon + i % 89 / 100, 4))
                location.append((ident, ident, *spot))
            for j in range(item.asns):
                ident, label = item.network + j, f"Synthetic Network {code}-{j + 1:03d}"
                usage = "hosting" if (j + 1) % synthetic.HOSTING_EVERY == 0 else "residential"
                asn.append((ident, 4200000000 + ident, label))
                network.append((ident, ident, label, label, "cable", usage))
        copy_rows(self.conn, "place_names", PLACE_COLUMNS, place)
        copy_rows(self.conn, "enrichment_locations", LOCATION_COLUMNS, location)
        copy_rows(self.conn, "asn_names", "asn_name_id,asn,name", asn)
        copy_rows(self.conn, "enrichment_networks", NETWORK_COLUMNS, network)

    def config(self, lane) -> dict:
        """The pool definition's config, as storage.ingest stores a daily test's or pulse's."""
        country = self.settings.countries[lane.country]
        return {
            "target": TARGET,
            "provider": self.settings.networks[lane.network],
            "product": self.model.formulas[lane.network].get("product", "residential"),
            "requested_country": country,
            "runner_location": self.regions[country][1],
            "proxy_protocol": "http",
        }

    def pool(self, lane) -> tuple:
        config, t = self.config(lane), self.target
        ids = (lane.position + 1, lane.network + 1, digest({"target": t, "config": config}), 0)
        where = (config["requested_country"], config["runner_location"])
        return (*ids, TARGET, t["url"], t["engine"], t["version"], "http", *where)

    def scope(self, lane, daily: bool) -> tuple:
        concurrency = DAILY_CONCURRENCY if daily else PULSE_CONCURRENCY
        performance = {"concurrency": concurrency, "timeout_seconds": TIMEOUT}
        config = self.config(lane) | performance | {"requests_per_second": None}
        scope_hash = digest({"target": self.target, "config": config})
        ids = (2 * lane.position + (1 if daily else 2), lane.position + 1, lane.network + 1)
        return (*ids, TARGET, scope_hash, Jsonb({}), self.created, concurrency, TIMEOUT)

    # One day.

    def day(self, day: int) -> None:
        start = self.settings.day_start(day)
        end = start + timedelta(days=1)
        conn = self.conn
        for table in DAILY:
            partition(conn, table, start)
        sha = hashlib.sha256(f"scale-gen:{self.settings.seed}:{day}".encode()).hexdigest()
        conn.execute(
            "INSERT INTO dbip_releases(release_id,sha256,metadata) OVERRIDING SYSTEM VALUE "
            "VALUES (%s,%s,%s)",
            (day, sha, Jsonb({"synthetic": True, "date": start.date().isoformat()})),
        )
        copy_lines(conn, "ip_addresses", "ip_id,ip", self.addresses(day))
        columns = "ip_id,release_id,location_id,network_id,matched"
        copy_lines(conn, "ip_enrichment", columns, self.enrichment(day))
        estimated = []
        for lane in self.model.lanes:
            estimated += self.daily_test(lane, day, sha)
        conn.execute(RELEASE, (end,))
        copy_rows(conn, "estimated_hours", ESTIMATE_COLUMNS, estimated)
        for hour in range(24):
            self.pulses(day, hour, sha)
        conn.execute(PRODUCT_IPS, (start, end))
        conn.execute(POOL_IPS, (start, end))

    def addresses(self, day: int):
        for lane in self.model.lanes:
            first, after = self.model.new(lane, day)
            for n in range(lane.offset + first, lane.offset + after):
                yield f"{n + 1}\t{address(n)}"

    def enrichment(self, day: int):
        for lane in self.model.lanes:
            places = self.model.places[lane.country]
            start, size = self.model.window(lane, day)
            for n in range(lane.offset + start, lane.offset + start + size):
                location, network, matched = self.model.enrichment(n, places)
                yield f"{n + 1}\t{day}\t{location}\t{network}\t{'t' if matched else 'f'}"

    def daily_test(self, lane, day: int, release: str) -> list[tuple]:
        """Write one daily test with its buckets, IP rows and enrichment; return its estimates."""
        test = self.model.test(lane, day)
        lines, successes, countries = self.observations(test)
        bins = self.model.latency(lane.network)
        errors = apportion(test.errors, [1] * test.buckets)
        histogram, buckets = Counter(), []
        for b, (good, bad) in enumerate(zip(successes, errors, strict=True)):
            counts = apportion(good, synthetic.WEIGHTS)
            found = [(ms, n) for ms, n in zip(bins, counts, strict=True) if n]
            histogram.update(dict(found))
            moment = test.started_at + timedelta(minutes=5 * b)
            buckets.append(bucket_row(moment, test.run_key, good, bad, found))
        duration = test.buckets * 300 - 1
        artifact = {"file": "ips.jsonl.gz", "rows": test.rows}
        artifact["sha256"] = digest(["ips", test.run_key])
        overrides = {"requests": None, "stop_rule": "unique_ratio", "min_sample": 1000}
        overrides |= {"stall_seconds": 600, "target_url": self.target["url"]}
        extra = {"requested": None, "unique_ips": test.ips, "stop_reason": "unique_ratio"}
        extra |= {"artifacts": {"ips": artifact}, "test_kind": "daily"}
        summary = self.summary(test.successes, test.errors, histogram, duration, extra, overrides)
        finished = test.started_at + timedelta(seconds=duration)
        self.run(test.run_key, 2 * lane.position + 1, test.started_at, finished, summary, None)
        self.conn.cursor().executemany(BUCKET_INSERT, buckets)
        artifact_sha = digest(["enriched", release, test.run_key])
        self.conn.execute(
            "INSERT INTO run_enrichments VALUES (%s,%s,%s,%s)",
            (test.run_key, test.day, artifact_sha, Jsonb(countries)),
        )
        copy_lines(self.conn, "ip_observations", OBSERVATION_COLUMNS, lines)
        return self.estimates(test)

    def observations(self, test):
        """ip_observations lines, successes per bucket and run_enrichments country counts."""
        model, codes = self.model, self.settings.countries
        moments = [test.started_at + timedelta(minutes=5 * b) for b in range(test.buckets)]
        seconds = [[stamp(m + timedelta(seconds=s)) for s in range(300)] for m in moments]
        base, extra = divmod(test.successes, test.rows)
        other = model.neighbour(test.lane)
        lines, successes, row = [], [0] * test.buckets, 0
        countries = {}
        for i, n in enumerate(test.indexes()):
            owner = test.lane if i < test.size else other
            matched = model.enrichment(n, model.places[owner.country])[2]
            label = codes[owner.country] if matched else "Unknown"
            entry = countries.setdefault(label, {"requests": 0, "unique_ips": 0})
            entry["unique_ips"] += 1
            first = i % test.buckets
            for b in (first, (first + 1) % test.buckets) if i % 3 == 0 else (first,):
                count = base + (row < extra)
                row += 1
                value = mix(n * 8 + b)
                begin = value % 240
                lines.append(
                    f"{seconds[b][0]}\t{test.run_key}\t{n + 1}\t{count}\t{seconds[b][begin]}\t"
                    f"{seconds[b][begin + (value >> 8) % 60]}"
                )
                successes[b] += count
                entry["requests"] += count
        return lines, successes, dict(sorted(countries.items()))

    def estimates(self, test) -> list[tuple]:
        """The test's estimated_hours rows; an hour is released once the day it ends in is."""
        lane = test.lane
        version = self.model.formulas[lane.network]["formula_version"]
        last = datetime.combine(test.started_at.date() + timedelta(days=1), time(), UTC)
        fixed = (lane.network + 1, self.settings.countries[lane.country])
        measured = (test.run_key, test.started_at, test.ips, test.successes)
        computed = test.started_at + timedelta(minutes=30)
        rows = []
        for row in self.model.estimates(test):
            ended = datetime.combine(row["day"], time(), UTC) + timedelta(hours=row["hour"] + 1)
            figures = tuple(row[k] for k in ("hour_factor", "unique_ips", "requests"))
            figures += (row["day_unique_ips"], row["curve"], version, computed)
            released = ended if ended <= last else None
            rows.append((*fixed, row["day"], row["hour"], *measured, *figures, released))
        return rows

    def pulses(self, day: int, hour: int, release: str) -> None:
        """One hour of pulses: a batch per country, a run per network, one bucket per run."""
        model, settings = self.model, self.settings
        slot = settings.day_start(day) + timedelta(hours=hour)
        ended = (slot + timedelta(seconds=1), slot + timedelta(seconds=45), "done")
        batches, runs, members, buckets, geo = [], [], [], [], []
        for c, code in enumerate(settings.countries):
            batch = ((day - 1) * 24 + hour) * len(settings.countries) + c + 1
            batches.append(
                (batch, "pulse", code, self.regions[code][0], TARGET, slot, slot, *ended)
            )
            for n in range(len(settings.networks)):
                lane = model.lane(n, c)
                run_key = model.run_key(day, len(model.lanes) * (hour + 1) + lane.position)
                errors, rows = model.pulse(lane, day, hour)
                run, member, bucket = self.pulse_run(lane, slot, (run_key, batch), errors, rows)
                runs.append(run)
                members.append(member)
                buckets.append(bucket)
                geo += [geo_line(slot, run_key, row) for row in rows]
        copy_rows(self.conn, "test_batches", BATCH_COLUMNS, batches)
        copy_rows(self.conn, "test_runs", RUN_COLUMNS, runs)
        copy_rows(self.conn, "batch_runs", MEMBER_COLUMNS, members)
        copy_rows(self.conn, "run_buckets", BUCKET_COLUMNS, buckets)
        copy_lines(self.conn, "run_geo_buckets", GEO_COLUMNS, geo)

    def pulse_run(self, lane, slot, keys, errors, rows):
        """The test_runs, batch_runs and run_buckets rows of one pulse; keys: (run, batch)."""
        (run_key, batch), requests = keys, synthetic.PULSE_REQUESTS
        histogram = Counter()
        for _, _, _, successes, ms in rows:
            if successes:
                histogram[ms] += successes
        successes = sum(row[3] for row in rows)
        started, duration = slot + timedelta(seconds=2), 30 + run_key % 10
        artifact = {"file": "geo.jsonl.gz", "rows": len(rows)}
        extra = {"requested": requests, "unique_ips": successes, "stop_reason": "requests"}
        extra["artifacts"] = {"geo": artifact}
        overrides = {"requests": requests}
        summary = self.summary(successes, errors, histogram, duration, extra, overrides)
        finished = started + timedelta(seconds=duration)
        run = self.run_row(run_key, 2 * lane.position + 2, started, finished, summary, batch)
        counters = (requests, successes, successes, 0, 0, errors, 0, requests * UP)
        member = (batch, lane.network + 1, "done", started, finished, finished, *counters)
        member += (successes * DOWN, "requests", run_key)
        found = sorted(histogram.items())
        return run, member, bucket_row(slot, run_key, successes, errors, found)

    def summary(self, successes, errors, histogram, duration, extra, overrides) -> dict:
        """A stored run summary: the keys storage.ingest keeps in test_runs.summary."""
        completed = successes + errors
        merged = Histogram(Counter(histogram))
        body = {
            "schema_version": "1",
            "framework_version": __version__,
            "duration_seconds": float(duration),
            "started": completed,
            "completed": completed,
            "successes": successes,
            "total_errors": errors,
            "errors": {"timeout": errors} if errors else {},
            "status_codes": {"200": successes} if successes else {},
            "success_rate": successes / completed if completed else None,
            "requests_per_second": completed / duration,
            "pool_eligible": True,
            "latency_ms": merged.percentiles(),
            "latency_mean_ms": merged.mean(),
            "response_body_bytes": successes * BODY,
            "timeouts": errors,
            "bytes_up": completed * UP,
            "bytes_down": successes * DOWN,
            "config_overrides": overrides,
            MARKER: self.settings.seed,
        }
        zero = dict.fromkeys(
            ("cancelled", "not_started", "challenges", "blocks", "proxy_errors"), 0
        )
        return body | zero | extra

    def run_row(self, run_key, scope_id, started, finished, summary, batch) -> tuple:
        run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"scale-gen:{self.settings.seed}:{run_key}")
        stored = (digest(summary), Jsonb(summary), finished + timedelta(minutes=1), batch)
        return (run_key, run_id, scope_id, started, finished, "completed", *stored)

    def run(self, *args) -> None:
        values = self.run_row(*args)
        marks = ",".join(["%s"] * len(values))
        sql = f"INSERT INTO test_runs({RUN_COLUMNS}) OVERRIDING SYSTEM VALUE VALUES ({marks})"
        self.conn.execute(sql, values)


def bucket_row(moment, run_key, successes, errors, bins) -> tuple:
    """A run_buckets row with typed outcome counters (timeouts are the only errors)."""
    completed = successes + errors
    counters = (
        Jsonb({"timeout": errors} if errors else {}),
        Jsonb({"200": successes} if successes else {}),
    )
    outcome = (None, True, 0, 0, errors, 0, completed * UP, successes * DOWN)
    return (
        moment,
        run_key,
        completed,
        successes,
        successes * BODY,
        *counters,
        *steps(bins),
        *outcome,
    )


def geo_line(slot, run_key, row) -> str:
    location, network, completed, successes, ms = row
    timeouts = completed - successes
    histogram = f"{{{ms}}}\t{{{successes}}}" if successes else "{}\t{}"
    return (
        f"{stamp(slot)}\t{run_key}\t{location}\t{network}\t{completed}\t{successes}\t0\t0\t"
        f"{timeouts}\t{completed * UP}\t{successes * DOWN}\t{histogram}"
    )


PLACE_COLUMNS = "place_id,country_code,country,region_code,region,city"
LOCATION_COLUMNS = "location_id,place_id,latitude,longitude"
NETWORK_COLUMNS = "network_id,asn_name_id,isp,organization,connection_type,usage_type"
POOL_COLUMNS = (
    "pool_id,product_id,pool_hash,unique_ips,target_id,target_url,target_engine,target_version,"
    "proxy_protocol,requested_country,runner_location"
)
SCOPE_COLUMNS = (
    "scope_id,pool_id,product_id,target,scope_hash,metrics,updated_at,concurrency,timeout_seconds"
)
RUN_COLUMNS = (
    "run_key,run_id,scope_id,started_at,finished_at,status,summary_sha256,summary,imported_at,"
    "batch_id"
)
BUCKET_COLUMNS = (
    "bucket_start,run_key,completed,successes,response_body_bytes,errors,status_codes,"
    "histogram_steps,histogram_counts,extra,outcome_counters,challenges,blocks,timeouts,"
    "proxy_errors,bytes_up,bytes_down"
)
BUCKET_INSERT = f"INSERT INTO run_buckets({BUCKET_COLUMNS}) VALUES ({','.join(['%s'] * 17)})"
OBSERVATION_COLUMNS = "bucket_start,run_key,ip_id,count,first_seen,last_seen"
GEO_COLUMNS = (
    "bucket_start,run_key,location_id,network_id,completed,successes,challenges,blocks,timeouts,"
    "bytes_up,bytes_down,histogram_steps,histogram_counts"
)
BATCH_COLUMNS = (
    "batch_id,kind,country_code,region_id,target_id,scheduled_for,start_at,started_at,"
    "finished_at,status"
)
MEMBER_COLUMNS = (
    "batch_id,product_id,status,started_at,finished_at,last_progress_at,requests,successes,"
    "unique_ips,challenges,blocks,timeouts,proxy_errors,bytes_up,bytes_down,stop_reason,run_key"
)
ESTIMATE_COLUMNS = (
    "product_id,country_code,day,hour,run_key,measured_at,measured_ips,measured_requests,"
    "hour_factor,unique_ips,requests,day_unique_ips,curve,formula_version,computed_at,released_at"
)
# Hours a test left unreleased are released once the day that holds their end is generated.
RELEASE = (
    "UPDATE estimated_hours SET released_at=day+make_interval(hours=>hour+1) "
    "WHERE released_at IS NULL AND day+make_interval(hours=>hour+1)<=%s"
)
# The per-IP caches storage.ingest keeps, from the day's IP rows.
PRODUCT_IPS = (
    "INSERT INTO product_ips SELECT s.product_id,o.ip_id,min(o.first_seen),max(o.last_seen),"
    "sum(o.count),count(DISTINCT o.run_key) FROM ip_observations o JOIN test_runs r "
    "USING(run_key) JOIN scope_stats s USING(scope_id) "
    "WHERE o.bucket_start>=%s AND o.bucket_start<%s GROUP BY 1,2 "
    "ON CONFLICT(product_id,ip_id) DO UPDATE SET "
    "first_seen=least(product_ips.first_seen,excluded.first_seen),"
    "last_seen=greatest(product_ips.last_seen,excluded.last_seen),"
    "observation_count=product_ips.observation_count+excluded.observation_count,"
    "run_count=product_ips.run_count+excluded.run_count"
)
POOL_IPS = (
    "WITH added AS (INSERT INTO pool_ips SELECT DISTINCT s.pool_id,o.ip_id "
    "FROM ip_observations o JOIN test_runs r USING(run_key) JOIN scope_stats s USING(scope_id) "
    "WHERE o.bucket_start>=%s AND o.bucket_start<%s ON CONFLICT DO NOTHING RETURNING pool_id) "
    "UPDATE pool_stats p SET unique_ips=p.unique_ips+a.n "
    "FROM (SELECT pool_id,count(*) AS n FROM added GROUP BY 1) a WHERE p.pool_id=a.pool_id"
)
