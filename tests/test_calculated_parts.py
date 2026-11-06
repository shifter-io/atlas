"""SNAP-SIZE-3a: a snapshot stored as a head and parts, sparse daily 24h series, no IP samples.

Every new path is compared with the old one on the same PostgreSQL: the head and parts read back
as the single jsonb payload row would, a build stored before calculated v8 still reads, exports
and serves, and the dense series the API serves equals the full series of the same inputs.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from aiohttp.test_utils import TestClient, TestServer
from census_pulse_cases import outage, scenario
from migration_cases import catalog
from psycopg import sql
from psycopg.types.json import Jsonb
from test_storage import measurement

from proxy_index.calculated import calculator, parts, store
from proxy_index.calculated.aggregate import series
from proxy_index.calculated.api import CACHE, RESOURCES, app
from proxy_index.calculated.calculator import calculate
from proxy_index.calculated.policy import digest
from proxy_index.storage import connect, ingest, initialize, migrate

pytest_plugins = ["test_storage"]

POLICY = Path("configs/methodology.toml")
# Keys that differ between two builds of the same inputs (ids, clock, implementation).
VOLATILE = {"snapshot_id", "generated_at", "implementation_id"}


async def two_providers(database, tmp_path):
    """Two unbatched measurements (daily groups); returns the cutoff after both."""
    store.initialize(database)
    first = await measurement(tmp_path, requests=10)
    ingest(first, database)
    ingest(await measurement(tmp_path, requests=10, provider="second"), database)
    raw = json.loads((first / "summary.json").read_text())
    return datetime.fromisoformat(raw["finished_at"]) + timedelta(minutes=5)


def stored(database, build_id):
    with connect(database) as conn:
        head, hashed = conn.execute(
            "SELECT payload,content_hash FROM calculated_snapshots WHERE build_id=%s", (build_id,)
        ).fetchone()
        rows = conn.execute(
            "SELECT tableoid::regclass::text,kind,position,part_id,product_id,cohort,content_hash,"
            "part FROM calculated_snapshot_parts WHERE build_id=%s ORDER BY kind,position",
            (build_id,),
        ).fetchall()
        whole = conn.execute(
            "SELECT %s::jsonb", (Jsonb(store.read(database, str(build_id))),)
        ).fetchone()[0]
    return head, hashed, rows, whole


@pytest.mark.asyncio
async def test_a_build_is_a_head_and_parts_that_read_back_as_one_payload(
    database, tmp_path, monkeypatch
):
    monkeypatch.setattr(parts, "RUNS_PER_PART", 1)
    end = await two_providers(database, tmp_path)
    payload = calculate(database, POLICY, as_of=end, publish=True)
    build_id = int(payload["snapshot_id"])
    head, hashed, rows, whole = stored(database, build_id)
    assert set(head) == set(payload) - {"products", "runs"} | {"parts"}
    groups = [r for r in rows if r[1] == "product"]
    runs = [r for r in rows if r[1] == "runs"]
    assert (
        head["parts"] == {"products": len(groups), "runs": len(runs)} == {"products": 2, "runs": 2}
    )
    assert {r[0] for r in rows} == {f"calculated_snapshot_parts_b{build_id}"}
    # Each part is its payload v3 group: its windows name their lists (SNAP-LISTREF-1).
    with connect(database) as conn:
        filled = parts.fill(conn, [r[7] for r in groups])
    for position, (_, _, at, part_id, product_id, cohort, part_hash, part) in enumerate(groups):
        group = payload["products"][position]
        assert (at, part_id, product_id, cohort, filled[position]) == (
            position,
            group["id"],
            group["product_id"],
            group["cohort"],
            group,
        )
        assert all("lists" in w for w in part["windows"].values())
        assert part_hash == digest(part)
    assert [r[7] for r in runs] == [[run] for run in payload["runs"]]
    assert [r[3] for r in runs] == ["runs:0", "runs:1"] and {r[4] for r in runs} == {None}
    # The snapshot hash covers the head and every part, in order.
    expected = hashlib.sha256((digest(head) + "".join(r[6] for r in rows)).encode()).hexdigest()
    assert hashed == expected
    # Reassembled, the payload is the single jsonb payload of the same content, byte for byte
    # (key order included), and equals what calculate() returned.
    read = store.read(database, str(build_id))
    assert json.dumps(read) == json.dumps(whole) and read == payload
    assert store.read(database, assemble=False) == head
    assert store.read_head(database) == head and "products" not in head
    # No window carries IP samples any more.
    assert all("ip_samples" not in w for p in read["products"] for w in p["windows"].values())
    one = payload["products"][1]
    assert store.read_parts(database, str(build_id), product=str(one["product_id"])) == [one]
    assert store.read_parts(database, "current", cohort=one["cohort"]) == [
        p for p in payload["products"] if p["cohort"] == one["cohort"]
    ]
    assert store.read_parts(database, "current", "runs") == payload["runs"]
    index = store.read_parts(database, "current", keys=("id", "kind", "missing"))
    assert index == [{"id": p["id"], "kind": p["kind"]} for p in payload["products"]]
    # Recalculating the same inputs returns the stored build, read from its parts.
    assert calculate(database, POLICY, as_of=end) == payload


@pytest.mark.asyncio
async def test_export_streams_the_payload_byte_for_byte(database, tmp_path, monkeypatch):
    monkeypatch.setattr(parts, "RUNS_PER_PART", 1)
    end = await two_providers(database, tmp_path)
    payload = calculate(database, POLICY, as_of=end, publish=True)
    path = tmp_path / "out" / "snapshot.json"
    assert store.export(database, path) == {
        "snapshot_id": payload["snapshot_id"],
        "path": str(path),
    }
    expected = json.dumps(store.read(database), indent=2, allow_nan=False) + "\n"
    assert path.read_text() == expected
    legacy = whole_payload_build(database, payload)
    store.export(database, path, str(legacy))
    assert path.read_text() == json.dumps(store.read(database, str(legacy)), indent=2) + "\n"
    with pytest.raises(ValueError, match="No published snapshot"):
        store.export(database, tmp_path / "missing.json", "999999")
    assert not (tmp_path / "missing.json").exists()


def whole_payload_build(database, payload):
    """A build stored the way calculated v7 stored it: its whole payload in one row."""
    with connect(database) as conn:
        build_id = conn.execute(
            "INSERT INTO calculation_builds(methodology_id,as_of,input_hash) "
            "VALUES (%s,%s,%s) RETURNING build_id",
            (payload["methodology"]["id"], payload["as_of"], uuid.uuid4().hex),
        ).fetchone()[0]
        whole = payload | {"snapshot_id": str(build_id)}
        conn.execute(
            "INSERT INTO calculated_snapshots VALUES (%s,%s,%s)",
            (build_id, digest(whole), Jsonb(whole)),
        )
    return build_id


@pytest.mark.asyncio
async def test_a_build_stored_whole_before_v8_still_reads_publishes_and_serves(
    database, tmp_path, monkeypatch
):
    # Both builds stay after the second publication (retention has its own tests).
    monkeypatch.setattr(store, "RETAIN_PER_DAY", 100)
    end = await two_providers(database, tmp_path)
    payload = calculate(database, POLICY, as_of=end, publish=True)
    with connect(database) as conn:
        split = parts.previous(conn)
    legacy = whole_payload_build(database, payload)
    expected = payload | {"snapshot_id": str(legacy)}
    assert store.read(database, str(legacy)) == expected
    assert store.read(database, str(legacy), assemble=False) == expected
    assert store.read_head(database, str(legacy)) == {
        k: v for k, v in expected.items() if k not in ("products", "runs")
    }
    assert store.read_parts(database, str(legacy)) == payload["products"]
    assert store.read_parts(database, str(legacy), "runs") == payload["runs"]
    one = payload["products"][0]
    assert store.read_parts(database, str(legacy), product=str(one["product_id"])) == [one]
    assert store.read_parts(database, str(legacy), keys=("id",)) == [
        {"id": p["id"]} for p in payload["products"]
    ]
    store.publish(database, str(legacy))
    with connect(database) as conn:
        whole = parts.previous(conn)
    # The next calculation reads the same projection from either layout: ids and scores only.
    assert whole == split
    window = whole["products"][0]["windows"]["24h"]
    assert set(window) == {"scores", "targets"}
    assert all(set(t) == {"score"} for t in window["targets"].values())
    async with TestClient(TestServer(app(database))) as client:
        for path in ["", *(f"/{r}?window=7d" for r in sorted(RESOURCES))]:
            new = await client.get(f"/v1/snapshots/{payload['snapshot_id']}{path}")
            old = await client.get(f"/v1/snapshots/{legacy}{path}")
            assert (new.status, old.status) == (200, 200)
            assert without(await new.json()) == without(await old.json()), path


def without(answer, keys=VOLATILE):
    return {k: v for k, v in answer.items() if k not in keys}


@pytest.mark.asyncio
async def test_sparse_daily_series_serve_the_full_series_of_the_same_inputs(
    database, tmp_path, monkeypatch
):
    as_of, ids = await scenario(database, tmp_path)
    as_of = datetime.fromtimestamp(as_of.timestamp() // 300 * 300, UTC)
    with connect(database) as conn:
        # The scheduled group has availability in buckets without requests.
        outage(conn, ids["alpha"], as_of - timedelta(hours=6), as_of - timedelta(hours=3))
    sparse = calculate(database, POLICY, as_of=as_of, publish=True)
    # The same inputs with the old full series (another implementation id, so another build).
    monkeypatch.setattr(calculator, "IMPLEMENTATION_ID", "full-series")
    monkeypatch.setattr(calculator, "sparse", lambda points, every=False: points)
    full = calculate(database, POLICY, as_of=as_of)
    assert full["snapshot_id"] != sparse["snapshot_id"]
    sparse = store.read(database, sparse["snapshot_id"])
    full = store.read(database, full["snapshot_id"])
    dropped = 0
    for s, f in zip(sparse["products"], full["products"], strict=True):
        scheduled = s["criteria"].get("schedule") == "census_pulse"
        for label, window in s["windows"].items():
            points, every = window["series"], f["windows"][label]["series"]
            if label != "24h" or scheduled:
                assert points == every
                continue
            dropped += len(every) - len(points)
            kept = [p for p in every if p["unique_ips"] is not None or p["uptime"] is not None]
            assert points == kept and all(p["completed"] for p in kept)
            # The dense series is the full one, key order included.
            assert json.dumps(parts.dense(window)) == json.dumps(f["windows"][label])
        assert without(s, {"windows"}) == without(f, {"windows"})
        assert {k: without(w, {"series"}) for k, w in s["windows"].items()} == {
            k: without(w, {"series"}) for k, w in f["windows"].items()
        }
    assert dropped > 0
    # networks (NET-SUM-1) name each build's own network lists.
    head = VOLATILE | {"products", "networks"}
    assert without(sparse, head) == without(full, head)
    # A scheduled group's 24h series keeps every bucket (and here availability without requests).
    scheduled = next(p for p in sparse["products"] if p["criteria"].get("schedule"))
    assert len(scheduled["windows"]["24h"]["series"]) == 288
    # Every API answer is the same from both builds.
    builds = (sparse["snapshot_id"], full["snapshot_id"])
    async with TestClient(TestServer(app(database))) as client:
        for path in ["", *(f"/{r}?window={w}" for r in RESOURCES for w in ("24h", "7d", "30d"))]:
            a, b = [await client.get(f"/v1/snapshots/{n}{path}") for n in builds]
            assert (a.status, b.status) == (200, 200)
            assert without(await a.json()) == without(await b.json()), path


def test_sparse_keeps_buckets_with_requests_or_availability():
    start = datetime(2026, 1, 1, tzinfo=UTC)

    def m(minutes):
        return start + timedelta(minutes=minutes)

    request = {"bucket_start": m(10), "target": "generic_http", "last_observed": m(11)}
    request["counters"] = {"completed": 2, "successes": 1, "errors": {"timeout": 1}}
    request["counters"] |= {"response_body_bytes": 0, "latency_histogram": {"bins": [[10, 1]]}}
    history = {
        "tested": [{"country": "DE", "started_at": start, "last": m(5)}],
        "outages": [{"country": "DE", "started_at": m(5), "ended_at": m(10)}],
    }
    full = series([request], [], start, m(20), 24, **history)
    points = parts.sparse(full)
    # Availability alone (0:00, 0:05) or requests alone (0:10, no IP seen: unique_ips 0) keep a
    # bucket; 0:15 has neither.
    assert [p["uptime"] for p in full] == [1, 0, None, None]
    assert [p["unique_ips"] for p in full] == [None, None, 0, None]
    assert points == full[:3] and full[3]["completed"] == 0
    assert parts.sparse(full, every=True) is full


def test_dense_rebuilds_dropped_points_and_keeps_other_windows():
    start = datetime(2026, 11, 1, tzinfo=UTC)
    end = start + timedelta(hours=24)
    period = {"start": start.isoformat(), "end": end.isoformat(), "hours": 24}
    kept = {"at": (start + timedelta(minutes=5)).isoformat(), "completed": 3}
    window = {"period": period, "series": [kept], "latency": {"target": "generic_http"}}
    points = parts.dense(window)["series"]
    assert len(points) == 288 and points[1] is kept
    first = points[0]
    # An empty point as series() writes it, its keys in jsonb's order (as stored points read).
    assert list(first) == sorted(first, key=lambda k: (len(k), k))
    assert (first["at"], first["completed"], first["successes"]) == (start.isoformat(), 0, 0)
    assert {first[k] for k in ("uptime", "unique_ips", "success_rate")} == {None}
    for name in ("latency_ms", "latency_under_load_ms"):
        assert list(first[name]) == sorted(first[name], key=lambda k: (len(k), k))
        assert set(first[name].values()) == {None}
    untimed = parts.dense(window | {"latency": {"target": None}})["series"][0]
    assert "latency_under_load_ms" not in untimed
    week = {"period": period | {"hours": 168}, "series": [{"at": "x"}]}
    assert parts.dense(week) is week


@pytest.mark.asyncio
async def test_prune_and_discard_drop_the_parts_partition(database, tmp_path, monkeypatch):
    end = await two_providers(database, tmp_path)
    drafts = [calculate(database, POLICY, as_of=end + timedelta(hours=h)) for h in range(4)]
    ids = [int(d["snapshot_id"]) for d in drafts]

    def partitions(conn):
        return {
            n
            for n in ids
            if conn.execute(
                "SELECT to_regclass(%s)", (f"calculated_snapshot_parts_b{n}",)
            ).fetchone()[0]
        }

    with connect(database) as conn:
        # Each calculation keeps the newest KEEP_DRAFTS drafts' payloads, heads and parts.
        assert partitions(conn) == set(ids[-store.KEEP_DRAFTS :])
        assert conn.execute(
            "SELECT count(*) FROM calculated_snapshot_parts WHERE build_id=ANY(%s)",
            (ids[: -store.KEEP_DRAFTS],),
        ).fetchone() == (0,)
        store.discard(conn, ids[-1])
        assert partitions(conn) == set(ids[-store.KEEP_DRAFTS : -1])
        assert conn.execute(
            "SELECT count(*) FROM calculated_snapshots WHERE build_id=%s", (ids[-1],)
        ).fetchone() == (0,)
        report = store.estimate(conn, [ids[-2]])
        assert report["rows"]["calculated_snapshot_parts"] > 0
        assert report["bytes"]["calculated_snapshot_parts"] > 0
    assert store.read(database, str(ids[-1])) is None
    assert store.read_head(database, str(ids[-1])) is None
    assert store.read_parts(database, "current") is None
    assert store.read_parts(database, str(ids[-1])) is None
    with connect(database) as conn, pytest.raises(ValueError, match="Runs are read whole"):
        parts.rows(conn, ids[-2], "runs", product="1")


@pytest.mark.asyncio
async def test_a_head_over_the_bound_is_refused_and_nothing_is_stored(
    database, tmp_path, monkeypatch
):
    end = await two_providers(database, tmp_path)
    monkeypatch.setattr(parts, "HEAD_BYTES", 1000)
    with pytest.raises(ValueError, match="Snapshot head is"):
        calculate(database, POLICY, as_of=end, publish=True)
    assert store.read(database) is None
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM calculation_builds").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM calculated_snapshot_parts").fetchone() == (0,)


@pytest.mark.asyncio
async def test_parts_are_written_and_read_through_the_parent_by_non_owner_roles(database, tmp_path):
    """The run-time role writes parts and the API role reads them with privileges on the
    partitioned table only (a build's partition is created by the table's owner)."""
    end = await two_providers(database, tmp_path)
    payload = calculate(database, POLICY, as_of=end, publish=True)
    roles = {name: f"{name}_{uuid.uuid4().hex}" for name in ("writer", "reader")}
    tables = "calculated_snapshots,calculated_snapshot_parts,current_calculation"
    with connect(database) as conn:
        for name, privileges in (("writer", "SELECT,INSERT"), ("reader", "SELECT")):
            role = sql.Identifier(roles[name])
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(role))
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
            conn.execute(sql.SQL(f"GRANT {privileges} ON {tables} TO {{}}").format(role))
        legacy = whole_payload_build(database, payload)
        conn.execute("DELETE FROM calculated_snapshots WHERE build_id=%s", (legacy,))
        store.partition(conn, legacy, "calculated_snapshot_parts")
    try:
        with connect(database) as conn:
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(roles["writer"])))
            columns = "build_id,kind,position,part_id,product_id,cohort,content_hash,part"
            group = payload["products"][0]
            with conn.cursor().copy(f"COPY calculated_snapshot_parts({columns}) FROM STDIN") as c:
                c.write_row(
                    (legacy, "product", 0, group["id"], group["product_id"], group["cohort"])
                    + (digest(group), json.dumps(group))
                )
            head = {k: v for k, v in payload.items() if k not in ("products", "runs")}
            head |= {"snapshot_id": str(legacy), "parts": {"products": 1, "runs": 0}}
            conn.execute(
                "INSERT INTO calculated_snapshots VALUES (%s,%s,%s)", (legacy, "x", Jsonb(head))
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege), connect(database) as conn:
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(roles["reader"])))
            conn.execute("SELECT count(*) FROM ip_observations")
        with connect(database) as conn:
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(roles["reader"])))
            found = parts.read(conn, legacy)
        assert found["products"] == [group] and found["runs"] == []
        assert found["snapshot_id"] == str(legacy) and "parts" not in found
    finally:
        with connect(database) as conn:
            for role in roles.values():
                conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


CALCULATED_V7 = Path(__file__).parent / "fixtures/calculated_v7.sql"


def v7_layer(database, payloads):
    """A current database whose calculated layer is the v7 release's, holding payloads as v7
    stored them (one row each); the last one is current. Returns their build ids."""
    assert hashlib.sha256(CALCULATED_V7.read_bytes()).hexdigest() == store.V7_CHECKSUM
    initialize(database)
    with connect(database) as conn:
        conn.execute(CALCULATED_V7.read_text())
        conn.execute("INSERT INTO calculation_schema VALUES (7,%s)", (store.V7_CHECKSUM,))
        for payload in payloads:
            method = payload["methodology"]
            conn.execute(
                "INSERT INTO methodologies VALUES (%s,%s,now()) ON CONFLICT DO NOTHING",
                (method["id"], Jsonb(method["settings"])),
            )
    builds = [whole_payload_build(database, payload) for payload in payloads]
    with connect(database) as conn:
        if builds:
            conn.execute("INSERT INTO current_calculation VALUES (true,%s)", (builds[-1],))
        conn.execute("UPDATE calculation_builds SET published=true")
    return builds


def snapshots(database):
    with connect(database) as conn:
        return conn.execute(
            "SELECT build_id,content_hash,payload::text FROM calculated_snapshots ORDER BY 1"
        ).fetchall()


@pytest.mark.asyncio
async def test_calculated_v7_builds_migrate_unchanged_and_still_serve(databases, tmp_path):
    source = databases()
    end = await two_providers(source, tmp_path)
    payloads = [calculate(source, POLICY, as_of=end + timedelta(hours=h)) for h in (0, 1)]
    database = databases()
    builds = v7_layer(database, payloads)
    before = snapshots(database)
    result = migrate(database)
    assert (result["from_calculated_schema_version"], result["calculated_schema_version"]) == (
        7,
        11,
    )
    assert result["verified"] == [
        *store.V7_CHECKS,
        *store.V8_CHECKS,
        *store.V9_CHECKS,
        *store.V10_CHECKS,
    ]
    assert snapshots(database) == before
    for build_id, payload in zip(builds, payloads, strict=True):
        assert store.read(database, str(build_id)) == payload | {"snapshot_id": str(build_id)}
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM calculated_snapshot_parts").fetchone() == (0,)
    async with TestClient(TestServer(app(database))) as client:
        for path in ["", *(f"/{r}?window=24h" for r in sorted(RESOURCES))]:
            assert (await client.get(f"/v1/snapshots/{builds[0]}{path}")).status == 200
    assert migrate(database)["migrated"] is False
    store.initialize(database)
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)


def test_a_failed_v7_check_rolls_back(databases, monkeypatch):
    database = databases()
    v7_layer(database, [])
    monkeypatch.setattr(store, "V7_CHECKS", {"always": "SELECT 1"})
    with pytest.raises(ValueError, match="always"):
        migrate(database)
    with connect(database) as conn:
        assert conn.execute("SELECT version FROM calculation_schema").fetchall() == [(7,)]
        assert conn.execute("SELECT to_regclass('calculated_snapshot_parts')").fetchone() == (None,)


@pytest.mark.asyncio
async def test_a_build_gone_after_its_head_was_cached_answers_404_and_caches_nothing(
    database, tmp_path
):
    """Its id and head are cached, then the build is expired (a same-day publication does this
    to the previous current build): its group answers are 404, never a cached empty 200."""
    end = await two_providers(database, tmp_path)
    build = calculate(database, POLICY, as_of=end, publish=True)["snapshot_id"]
    application = app(database)
    async with TestClient(TestServer(application)) as client:
        assert (await client.get(f"/v1/snapshots/{build}")).status == 200
        with connect(database) as conn:
            conn.execute("DELETE FROM current_calculation")
            store.discard(conn, int(build))
        for _ in range(2):  # the id and head stay cached for HEAD_SECONDS
            for path in (f"/products?window=24h&product={10**6}", "/runs?window=24h"):
                response = await client.get(f"/v1/snapshots/{build}{path}")
                assert (response.status, await response.text()) == (404, "Snapshot unavailable")
        cache = application[CACHE]
        assert [key[1] for key in cache.answers] == [None]
        assert all(key[1][0] == "products" and key[1][3] for key in cache.parts)
