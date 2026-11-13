"""Synthetic run directories, a counting PostgreSQL proxy and a canonical dump, for the tests of
storage.ingest_many (test_ingest_many.py, test_ingest_round_trips.py) and its measurement.

daily_test() writes a daily test's directory (IP rows over 5-minute buckets and a DB-IP
enrichment), pulse() a pulse's (one bucket, geo rows, an IP per successful geo row). Proxy
forwards a client to PostgreSQL, delaying each direction (a 78 ms round trip is 0.039 each way),
and counts the client's protocol messages. dump() reads every row ingest writes, with each
surrogate id replaced by its natural key, so two databases compare equal row for row.

Measurement (INGEST-RTT-1), on a disposable server with PG_TEST_ADMIN_FILE set:
    python tests/ingest_cases.py --delay 0.039 [--daily 20] [--pulses 1000] [--one-by-one 100]
prints one JSON line per case: statements, round trips and wall time, one by one (ingest) and,
when the build has it, together (ingest_many).
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import ipaddress
import json
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

START = datetime(2026, 11, 2, 10, tzinfo=UTC)
TARGET = {"id": "generic_http", "url": "http://example.invalid/", "engine": "http", "version": "1"}
BASE_IP = int(ipaddress.ip_address("10.0.0.0"))
RELEASE = hashlib.sha256(b"ingest-cases release").hexdigest()
CITIES = [
    {"country_code": "US", "country": "United States", "region_code": f"R{n}", "city": f"C{n}"}
    | {"region": f"Region {n}", "latitude": 30.5 + n, "longitude": -100.25 + n}
    for n in range(10)
]
NETWORKS = [
    {"isp": f"ISP {n}", "organization": f"Org {n}", "asn": 64500 + n, "connection_type": "Cable"}
    | {"asn_organization": f"AS {n}", "usage_type": "residential" if n % 3 else "hosting"}
    for n in range(7)
]
SCOPE = {"concurrency": 25, "timeout_seconds": 15, "requests_per_second": None}


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds")


def gzip_rows(path: Path, rows: list[dict]) -> dict:
    """Write JSON lines, reproducibly (no gzip time); the artifact's manifest entry."""
    with path.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as out:
        out.writelines((json.dumps(row) + "\n").encode() for row in rows)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"file": path.name, "rows": len(rows), "sha256": digest}


def counters(moment: datetime, completed: int, errors: int, bins: Counter) -> dict:
    """A bucket's or geo row's counters; timeouts are the only errors."""
    successes = completed - errors
    return {
        "bucket_start": iso(moment),
        "completed": completed,
        "successes": successes,
        "challenges": 0,
        "blocks": 0,
        "timeouts": errors,
        "bytes_up": completed * 300,
        "bytes_down": successes * 2000,
        "latency_histogram": {
            "format": "ceil_ms_v1",
            "bins": [[m, n] for m, n in sorted(bins.items()) if n],
        },
    }


def bucket(moment: datetime, completed: int, errors: int, bins: Counter) -> dict:
    successes = completed - errors
    extra = {"response_body_bytes": successes * 1000, "proxy_errors": 0}
    extra["errors"] = {"timeout": errors} if errors else {}
    extra["status_codes"] = {"200": successes} if successes else {}
    return counters(moment, completed, errors, bins) | extra


def summary(run_id: str, started: datetime, buckets: list[dict], config: dict, **keys) -> dict:
    """A run's summary.json from its buckets; keys adds requested, unique_ips and artifacts."""
    totals, bins = Counter(), Counter()
    for item in buckets:
        totals.update({k: item[k] for k in ("completed", "timeouts", "bytes_up", "bytes_down")})
        totals.update({"response_body_bytes": item["response_body_bytes"]})
        bins.update(dict(item["latency_histogram"]["bins"]))
    completed, errors = totals["completed"], totals["timeouts"]
    duration = len(buckets) * 300 - 1
    return {
        "schema_version": "1",
        "run_id": run_id,
        "status": "completed",
        "started_at": iso(started),
        "finished_at": iso(started + timedelta(seconds=duration)),
        "duration_seconds": float(duration),
        "started": completed,
        "completed": completed,
        "successes": completed - errors,
        "total_errors": errors,
        "cancelled": 0,
        "not_started": 0,
        "errors": {"timeout": errors} if errors else {},
        "status_codes": {"200": completed - errors} if completed > errors else {},
        "response_body_bytes": totals["response_body_bytes"],
        "challenges": 0,
        "blocks": 0,
        "timeouts": errors,
        "proxy_errors": 0,
        "bytes_up": totals["bytes_up"],
        "bytes_down": totals["bytes_down"],
        "pool_eligible": True,
        "latency_histogram": {
            "format": "ceil_ms_v1",
            "bins": [[m, n] for m, n in sorted(bins.items()) if n],
        },
        "buckets": buckets,
        "target": TARGET,
        "config": config,
    } | keys


def config(provider: str, product: str, country: str, **scope) -> dict:
    found = {"target": TARGET["id"], "provider": provider, "product": product}
    found |= {"requested_country": country, "runner_location": "fra1", "proxy_protocol": "http"}
    return found | SCOPE | scope


def ip_row(run_id: str, n: int, moment: datetime, count: int) -> dict:
    first = moment + timedelta(seconds=n % 250)
    return {
        "schema_version": "1",
        "run_id": run_id,
        "ip": str(ipaddress.ip_address(BASE_IP + n)),
        "bucket_start": iso(moment),
        "first_seen": iso(first),
        "last_seen": iso(first + timedelta(seconds=n % 40)),
        "count": count,
    }


def attributes(n: int) -> dict:
    """An IP's DB-IP attributes: the same for the same IP in every run."""
    return {"matched": True} | CITIES[n % len(CITIES)] | NETWORKS[n % len(NETWORKS)]


def index_of(row: dict) -> int:
    """The IP number n of an ip_row()."""
    return int(ipaddress.ip_address(row["ip"])) - BASE_IP


def run_id(kind: str, index: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ingest-cases:{kind}:{index}"))


def daily_test(directory: Path, index: int, *, ips=4300, repeats=2000, provider=None) -> Path:
    """A daily test of `ips` IPs over 12 buckets, `repeats` of them seen in a second bucket;
    test `index` starts `index` hours after START and shares 30% of its IPs with the next."""
    directory.mkdir(parents=True, exist_ok=True)
    rid, started = run_id("daily", index), START + timedelta(hours=index)
    moments = [started + timedelta(minutes=5 * b) for b in range(12)]
    rows = []
    for i in range(ips):
        n = index * (ips * 7 // 10) + i
        rows.append(ip_row(rid, n, moments[i % 12], 1 + i % 3))
        if i < repeats:
            rows.append(ip_row(rid, n, moments[(i + 5) % 12], 1))
    successes = Counter()
    for row in rows:
        successes[row["bucket_start"]] += row["count"]
    buckets = [
        bucket(m, successes[iso(m)] + 2, 2, Counter({100 + 7 * b: successes[iso(m)]}))
        for b, m in enumerate(moments)
    ]
    ips_file = gzip_rows(directory / "ips.jsonl.gz", rows)
    enriched = [
        row | {"dbip_release": RELEASE, "enrichment": attributes(index_of(row))} for row in rows
    ]
    report = gzip_rows(directory / f"enriched-{RELEASE}.jsonl.gz", enriched)
    manifest = {"run_id": rid, "release": {"sha256": RELEASE, "synthetic": True}}
    (directory / f"enrichment-{RELEASE}.json").write_text(json.dumps(report | manifest))
    run_length = {"requests": None, "stop_rule": "unique_ratio", "min_sample": 1000}
    found = config(provider or f"daily-{index % 3}", "residential", "US", concurrency=1000)
    body = summary(rid, started, buckets, found | run_length, requested=None)
    body |= {"unique_ips": ips, "artifacts": {"ips": ips_file}, "stop_reason": "unique_ratio"}
    (directory / "summary.json").write_text(json.dumps(body))
    return directory


def pulse(directory: Path, index: int, *, geo=70, provider=None) -> Path:
    """A pulse of 100 requests in one bucket with `geo` geo rows (distinct location and network)
    and an IP row for each geo row with a success; pulse `index` starts 5 minutes after the last."""
    directory.mkdir(parents=True, exist_ok=True)
    rid, moment = run_id("pulse", index), START + timedelta(minutes=5 * index)
    sizes = [1] * geo
    for g in range(100 - geo):
        sizes[g % geo] += 1
    geo_rows, ips, bins = [], [], Counter()
    for g, completed in enumerate(sizes):
        errors, ms = (1 if g % 14 == 0 else 0), 50 + g % 9 * 10
        found = Counter({ms: completed - errors})
        row = counters(moment, completed, errors, found)
        geo_rows.append(row | {"location": CITIES[g % 10], "network": NETWORKS[g // 10 % 7]})
        bins.update(found)
        if completed > errors:
            ips.append(ip_row(rid, 500_000 + index * 20 + g, moment, completed - errors))
    errors = sum(row["timeouts"] for row in geo_rows)
    artifacts = {"ips": gzip_rows(directory / "ips.jsonl.gz", ips)}
    artifacts["geo"] = gzip_rows(directory / "geo.jsonl.gz", geo_rows)
    found = config(provider or f"pulse-{index % 2}", "residential", "US")
    buckets = [bucket(moment, 100, errors, bins)]
    body = summary(rid, moment, buckets, found | {"requests": 100}, requested=100)
    body |= {"unique_ips": len(ips), "artifacts": artifacts, "stop_reason": "requests"}
    (directory / "summary.json").write_text(json.dumps(body))
    return directory


# Every row ingest writes, its surrogate ids replaced by natural keys (runs by run_id, products
# by provider and name, pools and scopes by their hashes, IPs by address, releases by sha256).
RUN = "(SELECT run_id FROM test_runs WHERE run_key=t.run_key)"
PRODUCT = "(SELECT provider||'/'||name FROM products WHERE product_id=t.product_id)"
POOL = "(SELECT pool_hash FROM pool_stats WHERE pool_id=t.pool_id)"
IP = "(SELECT host(ip) FROM ip_addresses WHERE ip_id=t.ip_id)"
RELEASE_OF = "(SELECT sha256 FROM dbip_releases WHERE release_id=t.release_id)"
LOCATION = (
    "(SELECT to_jsonb(l)-'location_id' FROM enrichment_location_fields l "
    "WHERE l.location_id=t.location_id)"
)
NETWORK = (
    "(SELECT to_jsonb(n)-'network_id' FROM enrichment_network_fields n "
    "WHERE n.network_id=t.network_id)"
)
DUMP = {
    "providers": "to_jsonb(t)",
    "products": "to_jsonb(t)-'product_id'",
    "targets": "to_jsonb(t)",
    "pool_stats": f"to_jsonb(t)-'pool_id'-'product_id'||jsonb_build_object('product',{PRODUCT})",
    "scope_stats": f"to_jsonb(t)-'scope_id'-'pool_id'-'product_id'-'updated_at'"
    f"||jsonb_build_object('product',{PRODUCT},'pool',{POOL})",
    "test_runs": "to_jsonb(t)-'run_key'-'scope_id'-'imported_at'||jsonb_build_object('scope',"
    "(SELECT scope_hash FROM scope_stats WHERE scope_id=t.scope_id))",
    "run_buckets": f"to_jsonb(t)-'run_key'||jsonb_build_object('run',{RUN})",
    "run_geo_buckets": f"to_jsonb(t)-'run_key'-'location_id'-'network_id'"
    f"||jsonb_build_object('run',{RUN},'location',{LOCATION},'network',{NETWORK})",
    "ip_addresses": "to_jsonb(host(t.ip))",
    "ip_observations": f"to_jsonb(t)-'run_key'-'ip_id'||jsonb_build_object('run',{RUN},'ip',{IP})",
    "product_ips": f"to_jsonb(t)-'product_id'-'ip_id'"
    f"||jsonb_build_object('product',{PRODUCT},'ip',{IP})",
    "pool_ips": f"jsonb_build_object('pool',{POOL},'ip',{IP})",
    "scope_ips": "jsonb_build_object('scope',(SELECT scope_hash FROM scope_stats "
    f"WHERE scope_id=t.scope_id),'ip',{IP})",
    "provider_overlap": "to_jsonb(t)-'updated_at'",
    "dbip_releases": "to_jsonb(t)-'release_id'",
    "run_enrichments": f"to_jsonb(t)-'run_key'-'release_id'"
    f"||jsonb_build_object('run',{RUN},'release',{RELEASE_OF})",
    "ip_enrichment_fields": f"to_jsonb(t)-'ip_id'-'release_id'"
    f"||jsonb_build_object('ip',{IP},'release',{RELEASE_OF})",
    "enrichment_location_fields": "to_jsonb(t)-'location_id'",
    "enrichment_network_fields": "to_jsonb(t)-'network_id'",
}


def dump(conn) -> dict[str, list[str]]:
    """Every table's rows as sorted canonical JSON, and the raw tables' partitions."""
    found = {}
    for table, value in DUMP.items():
        rows = conn.execute(f"SELECT {value} FROM {table} t")
        found[table] = sorted(json.dumps(row, sort_keys=True) for (row,) in rows)
    found["partitions"] = sorted(
        name
        for (name,) in conn.execute(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid "
            "WHERE i.inhparent::regclass::text "
            "IN ('run_buckets','ip_observations','run_geo_buckets')"
        )
    )
    return found


class Frontend:
    """The client's side of the PostgreSQL protocol, counted by message type after the startup
    message (untyped, like an SSL request before it)."""

    def __init__(self):
        self.buffer, self.started, self.counts = bytearray(), False, Counter()

    def feed(self, data: bytes) -> None:
        self.buffer += data
        at = 0
        while True:
            head = 0 if not self.started else 1
            if len(self.buffer) - at < head + 4:
                break
            length = int.from_bytes(self.buffer[at + head : at + head + 4], "big")
            if len(self.buffer) - at < head + length:
                break
            if self.started:
                self.counts[chr(self.buffer[at])] += 1
            elif int.from_bytes(self.buffer[at + 4 : at + 8], "big") not in (80877103, 80877104):
                self.started = True
            at += head + length
        del self.buffer[:at]

    def round_trips(self) -> int:
        """Each Sync, simple Query, Flush or CopyDone waits for the server once; the startup and
        authentication exchange is not counted (password messages)."""
        return sum(self.counts[kind] for kind in "SQHc")

    def statements(self) -> int:
        return self.counts["E"] + self.counts["Q"]


class Proxy:
    """A TCP proxy to PostgreSQL at (host, port), delaying each direction by `delay` seconds
    (a delay line: data in flight does not wait for earlier data to be answered). frontends
    holds each connection's counts."""

    def __init__(self, host: str, port: int, delay: float = 0.0):
        self.target, self.delay, self.frontends = (host, port), delay, []
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)

    def __enter__(self) -> Proxy:
        self.thread.start()
        start = asyncio.start_server(self.serve, "127.0.0.1", 0)
        self.server = asyncio.run_coroutine_threadsafe(start, self.loop).result()
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    def __exit__(self, *exc) -> None:
        asyncio.run_coroutine_threadsafe(self.close(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()

    async def close(self) -> None:
        self.server.close()
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def reset(self) -> None:
        self.frontends.clear()

    def round_trips(self) -> int:
        return sum(f.round_trips() for f in self.frontends)

    def statements(self) -> int:
        return sum(f.statements() for f in self.frontends)

    async def serve(self, reader, writer) -> None:
        upstream, downstream = await asyncio.open_connection(*self.target)
        frontend = Frontend()
        self.frontends.append(frontend)
        await asyncio.gather(
            self.pipe(reader, downstream, frontend.feed), self.pipe(upstream, writer, None)
        )

    async def pipe(self, reader, writer, observe) -> None:
        queue: asyncio.Queue = asyncio.Queue()

        async def receive():
            while data := await reader.read(1 << 16):
                if observe:
                    observe(data)
                queue.put_nowait((self.loop.time() + self.delay, data))
            queue.put_nowait((self.loop.time() + self.delay, b""))

        async def send():
            while True:
                due, data = await queue.get()
                if due > self.loop.time():
                    await asyncio.sleep(due - self.loop.time())
                if not data:
                    writer.close()
                    return
                writer.write(data)
                await writer.drain()

        await asyncio.gather(receive(), send())


def credentials_file(values: dict, path: Path) -> Path:
    """Write a [postgres] credentials file of `values`."""
    path.write_text("[postgres]\n" + "\n".join(f"{k} = {json.dumps(v)}" for k, v in values.items()))
    path.chmod(0o600)
    return path


def proxied(credentials: dict, proxy: Proxy, directory: Path) -> Path:
    """A credentials file for the database `credentials` names, through `proxy` (no TLS, so
    the proxy can read the protocol)."""
    values = credentials | {"host": "127.0.0.1", "port": str(proxy.port), "sslmode": "disable"}
    return credentials_file(values, directory / f"proxied-{proxy.port}.toml")


def _measure(args) -> None:
    """Print statements, round trips and wall time per case (the module docstring's command)."""
    import helpers

    from atlas import storage
    from atlas.providers.credentials import read_credentials

    admin = read_credentials(args.admin, "postgres")
    work = Path(tempfile.mkdtemp(prefix="ingest-rtt-"))
    daily = [daily_test(work / f"d{i}", i) for i in range(args.daily)]
    pulses = [pulse(work / f"p{i}", i) for i in range(args.pulses)]
    together = getattr(storage, "ingest_many", None)
    cases = [("1 pulse", pulses[:1], False), ("1 daily test", daily[:1], True)]
    cases += [(f"{len(daily)} daily tests", daily, True), (f"{len(pulses)} pulses", pulses, False)]
    for name, directories, ip_rows in cases:
        modes = [("one by one", directories[: args.one_by_one])] if args.one_by_one else []
        modes += [("together", directories)] if together else []
        for mode, chosen in modes:
            database = helpers.scratch_database(admin)
            values = admin | {"dbname": database}
            direct = credentials_file(values, work / f"{database}.toml")
            storage.initialize(direct)
            with Proxy(values["host"], values["port"], args.delay) as proxy:
                credentials = proxied(values, proxy, work)
                began = time.monotonic()
                if mode == "together":
                    together(chosen, credentials, ip_rows=ip_rows)
                else:
                    for directory in chosen:
                        storage.ingest(directory, credentials, ip_rows=ip_rows)
                wall = time.monotonic() - began
            if args.dump:
                with storage.connect(direct) as conn:
                    path = Path(args.dump, f"{name}-{mode}.json".replace(" ", "_"))
                    path.write_text(json.dumps(dump(conn)))
            record = {"case": name, "mode": mode, "runs": len(chosen)}
            record |= {"wall_seconds": round(wall, 2)}
            record |= {"statements": proxy.statements(), "round_trips": proxy.round_trips()}
            record |= {"connections": len(proxy.frontends)}
            record |= {"messages": dict(sum((f.counts for f in proxy.frontends), Counter()))}
            if mode == "together":  # the reads and checks before the transaction, alone
                began = time.monotonic()
                storage._read_all(chosen, None, ip_rows)
                record["read_seconds"] = round(time.monotonic() - began, 2)
            print(json.dumps(record), flush=True)
            helpers.drop_databases(admin, [database])
    helpers.drop_queued()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--admin", default=None, help="PostgreSQL admin TOML (PG_TEST_ADMIN_FILE)")
    parser.add_argument("--delay", type=float, default=0.039, help="seconds each way")
    parser.add_argument("--daily", type=int, default=20)
    parser.add_argument("--pulses", type=int, default=1000)
    parser.add_argument("--one-by-one", type=int, default=100, help="runs per one-by-one case")
    parser.add_argument("--dump", default=None, help="directory for each case's dump()")
    parsed = parser.parse_args()
    if parsed.admin is None:
        import os

        parsed.admin = os.environ["PG_TEST_ADMIN_FILE"]
    sys.exit(_measure(parsed))
