"""Calculated v3 over census and pulse batches: event detection, the events and traffic API,
the traffic report and scheduled coverage."""

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from census_pulse_cases import HOSTING, batch, batch_run, product, scenario
from helpers import FakeTransport
from psycopg.rows import dict_row
from test_calculated import OtherIP
from test_storage import measurement
from test_storage_v6 import census

from proxy_index import traffic
from proxy_index.calculated import store
from proxy_index.calculated.api import app
from proxy_index.calculated.calculator import calculate
from proxy_index.cli import main
from proxy_index.detect import WATERMARK, category, detect, list_events, thresholds
from proxy_index.storage import connect, ingest, seed

pytest_plugins = ["test_storage"]

POLICY = Path("configs/methodology.toml")


def run_key(database, directory):
    return ingest(directory, database)["run_key"]


# --- detection ---------------------------------------------------------------------------------

T0 = datetime(2026, 10, 1, tzinfo=UTC)
SETTINGS = thresholds() | {"initial_lookback_minutes": 60 * 24 * 30}


def pulses(conn, product_id, results, *, start=0, run_keys=None, **options):
    """One finished pulse batch per entry (successes or a dict of counters), 30 minutes apart."""
    for i, result in enumerate(results, start):
        at = T0 + timedelta(minutes=30 * i)
        counters = result if isinstance(result, dict) else {"successes": result}
        b = batch(conn, "pulse", at, **options)
        batch_run(
            conn,
            b,
            product_id,
            at + timedelta(minutes=1),
            run_key=(run_keys or {}).get(i),
            **counters,
        )


def after(i):
    """A detection time that covers pulse i (settled)."""
    return T0 + timedelta(minutes=30 * i + 10)


def kinds(conn, since_id=0):
    return [
        (r[0], r[1])
        for r in conn.execute(
            "SELECT kind,signal FROM test_events WHERE event_id>%s ORDER BY event_id", (since_id,)
        )
    ]


def test_detect_drop_down_recovered_block_spike_and_pulse_changes(database):
    seed(database)
    with connect(database) as conn:
        alpha = product(conn)
        pulses(conn, alpha, [95] * 6)
        # No events while the baseline forms and nothing changes.
        assert detect(conn, after(5), config=SETTINGS) == 0
        assert conn.execute("SELECT value FROM settings WHERE key=%s", (WATERMARK,)).fetchone()
        pulses(conn, alpha, [70], start=6)
        assert detect(conn, after(6), config=SETTINGS) == 1
        drop = (
            conn.cursor(row_factory=dict_row)
            .execute("SELECT * FROM test_events ORDER BY event_id DESC LIMIT 1")
            .fetchone()
        )
        assert (drop["kind"], drop["signal"], drop["severity"]) == ("success_drop", -1, "minor")
        assert drop["data"]["baseline"] == 0.95 and drop["data"]["value"] == 0.7
        assert drop["data"]["region"] == "fra1" and drop["country_code"] == "DE"
        assert drop["target_id"] == "generic_http" and drop["region_id"] is not None
        pulses(conn, alpha, [0, 0, 96, 95], start=7)
        assert detect(conn, after(10), config=SETTINGS) == 2
        assert kinds(conn, drop["event_id"]) == [("down", -1), ("recovered", 1)]
        recovered = conn.execute("SELECT data FROM test_events WHERE kind='recovered'").fetchone()[
            0
        ]
        # Healthy again against the baseline the incident was judged by (which held the 70).
        assert recovered["incident"] == "down"
        assert recovered["baseline"] == pytest.approx((6 * 95 + 70) / 700)
        last = conn.execute("SELECT max(event_id) FROM test_events").fetchone()[0]
        pulses(
            conn,
            alpha,
            [{"successes": 75, "challenges": 15, "blocks": 5}] * 2 + [85],
            start=11,
        )
        assert detect(conn, after(13), config=SETTINGS) == 2
        # The spike is reported once while it lasts; the next clean pulse is a change.
        assert kinds(conn, last) == [("block_spike", -1), ("pulse_finished", 1)]
        spike, change = conn.execute(
            "SELECT data FROM test_events WHERE event_id>%s ORDER BY event_id", (last,)
        ).fetchall()
        assert (spike[0]["challenges"], spike[0]["blocks"], spike[0]["value"]) == (15, 5, 0.2)
        assert change[0]["previous"]["challenges"] == 15 and change[0]["success_rate"] == 0.85
        total = conn.execute("SELECT count(*) FROM test_events").fetchone()[0]
        # Idempotent: the same moment, an earlier one, and a reset watermark add nothing.
        assert detect(conn, after(13), config=SETTINGS) == 0
        assert detect(conn, after(3), config=SETTINGS) == 0
        conn.execute("DELETE FROM settings WHERE key=%s", (WATERMARK,))
        assert detect(conn, after(14), config=SETTINGS) == 0
        assert conn.execute("SELECT count(*) FROM test_events").fetchone()[0] == total


def outages(conn):
    return conn.execute(
        "SELECT source,started_at,ended_at FROM outages ORDER BY outage_id"
    ).fetchall()


def test_pulse_down_opens_an_outage_and_recovered_closes_it(database):
    seed(database)
    with connect(database) as conn:
        alpha = product(conn)
        # Both targets go down at pulse 6; http recovers at 8, https only at 9.
        pulses(conn, alpha, [95] * 6 + [0, 0, 96, 95])
        pulses(conn, alpha, [95] * 6 + [0, 0, 0, 96], target="generic_https")
        detect(conn, after(9), config=SETTINGS)
        events = conn.execute(
            "SELECT e.kind,e.target_id,e.data FROM test_events e "
            "WHERE e.kind IN ('down','recovered') ORDER BY e.event_id"
        ).fetchall()
        assert [(e[0], e[1]) for e in events] == [
            ("down", "generic_http"),
            ("down", "generic_https"),
            ("recovered", "generic_http"),
            ("recovered", "generic_https"),
        ]
        # One outage for the product and country: from the last healthy pulse (5, finished a
        # minute after it started) to the start of the pulse after which no target was down.
        last_healthy = T0 + timedelta(minutes=30 * 5 + 1)
        assert outages(conn) == [("pulse", last_healthy, T0 + timedelta(minutes=30 * 9))]
        assert events[0][2]["since"] == last_healthy.isoformat(timespec="microseconds")
        assert events[0][2]["source"] == "pulse"
        # Judging the same batches again opens and closes nothing.
        total = conn.execute("SELECT count(*) FROM test_events").fetchone()[0]
        conn.execute("DELETE FROM settings WHERE key=%s", (WATERMARK,))
        assert detect(conn, after(10), config=SETTINGS) == 0
        assert conn.execute("SELECT count(*) FROM test_events").fetchone()[0] == total
        assert len(outages(conn)) == 1


def test_healthy_pulse_closes_a_census_outage_once(database):
    from proxy_index.storage import partition

    seed(database)
    with connect(database) as conn:
        alpha = product(conn)
        # A census on the pulse's own target stalled at T0+100 min: an open census outage and
        # its 'down' event, which the pulse incident state must not take for its own.
        census_batch = batch(conn, "census", T0 + timedelta(minutes=60))
        partition(conn, "test_events", T0)
        conn.execute(
            "INSERT INTO test_events(at,kind,batch_id,product_id,country_code,target_id,signal,"
            "data) VALUES (%s,'down',%s,%s,'DE','generic_http',-1,'{\"source\":\"census\"}')",
            (T0 + timedelta(minutes=115), census_batch, alpha),
        )
        conn.execute(
            "INSERT INTO outages(product_id,country_code,source,batch_id,started_at) "
            "VALUES (%s,'DE','census',%s,%s)",
            (alpha, census_batch, T0 + timedelta(minutes=100)),
        )
        last = conn.execute("SELECT max(event_id) FROM test_events").fetchone()[0]
        # Pulse 3 (T0+90) started before the outage; pulse 4 (T0+120) is the first after it.
        pulses(conn, alpha, [95] * 8)
        detect(conn, after(7), config=SETTINGS)
        assert outages(conn) == [
            ("census", T0 + timedelta(minutes=100), T0 + timedelta(minutes=120))
        ]
        assert kinds(conn, last) == [("recovered", 1)]
        data = conn.execute("SELECT data FROM test_events WHERE event_id>%s", (last,)).fetchone()[0]
        assert data["source"] == "census" and data["incident"] == "down"
        assert data["duration_s"] == 20 * 60
        conn.execute("DELETE FROM settings WHERE key=%s", (WATERMARK,))
        assert detect(conn, after(8), config=SETTINGS) == 0


def test_detect_waits_for_settled_batches_and_filters_small_pulses(database):
    seed(database)
    with connect(database) as conn:
        alpha = product(conn)
        pulses(conn, alpha, [95] * 6 + [{"requests": 10, "successes": 0}])
        settle = timedelta(seconds=SETTINGS["settle_seconds"])
        finished = T0 + timedelta(minutes=30 * 6 + 1)
        # Finished less than settle_seconds ago: not yet.
        assert detect(conn, finished + settle - timedelta(seconds=1), config=SETTINGS) == 0
        # Ten requests are below min_requests: not judged as down, only reported as a change.
        assert detect(conn, finished + settle, config=SETTINGS) == 1
        assert kinds(conn) == [("pulse_finished", -1)]


async def test_detect_latency_spike_from_run_histograms(database, tmp_path):
    seed(database)
    keys = {}
    for i in range(7):
        transport = OtherIP if i == 6 else FakeTransport
        keys[i] = run_key(database, await measurement(tmp_path, transport_factory=transport))
    with connect(database) as conn:
        alpha = product(conn, "fixture")
        pulses(conn, alpha, [100] * 7, run_keys=keys)
        assert detect(conn, after(6), config=SETTINGS | {"min_latency_samples": 3}) == 1
        event = conn.execute("SELECT kind,data FROM test_events").fetchone()
    assert event[0] == "latency_spike"
    assert (event[1]["p50"], event[1]["baseline"]) == (31, 11)


async def test_detect_new_hosting_asn_and_census_overlap(database, tmp_path):
    seed(database)
    a, _ = await census(tmp_path, provider="alpha", network=HOSTING)
    b, _ = await census(tmp_path, provider="beta")
    again, _ = await census(tmp_path, provider="alpha", network=HOSTING)
    with connect(database) as conn:
        first, second = batch(conn, "census", T0), batch(conn, "pulse", T0 + timedelta(hours=1))
    keys = [
        ingest(a, database, batch_id=first)["run_key"],
        ingest(b, database, batch_id=first)["run_key"],
        ingest(again, database, batch_id=second)["run_key"],
    ]
    with connect(database) as conn:
        alpha, beta = product(conn, "alpha"), product(conn, "beta")
        batch_run(conn, first, alpha, T0 + timedelta(minutes=1), run_key=keys[0])
        batch_run(conn, first, beta, T0 + timedelta(minutes=1), run_key=keys[1])
        batch_run(conn, second, alpha, T0 + timedelta(hours=1, minutes=1), run_key=keys[2])
        assert detect(conn, T0 + timedelta(hours=2), config=SETTINGS) == 3
        events = (
            conn.cursor(row_factory=dict_row)
            .execute(
                "SELECT e.kind,p.provider,e.batch_id,e.signal,e.data FROM test_events e "
                "JOIN products p USING(product_id) ORDER BY e.event_id"
            )
            .fetchall()
        )
    overlap = {e["provider"]: e for e in events if e["kind"] == "ip_overlap"}
    assert set(overlap) == {"alpha", "beta"}
    assert overlap["alpha"]["data"] | {"region": None} == {
        "region": None,
        "value": 1.0,
        "shared_ips": 1,
        "unique_ips": 1,
        "peers": {"beta": 1},
    }
    hosting = [e for e in events if e["kind"] == "new_hosting_asn"]
    # Only once per product and ASN, although the pulse landed in it again.
    assert len(hosting) == 1 and hosting[0]["batch_id"] == first
    assert (
        hosting[0]["data"]["asn"] == 24940 and hosting[0]["data"]["name"] == "Hetzner Online GmbH"
    )


# --- events and traffic API, traffic report ----------------------------------------------------


def test_event_categories():
    assert [category(k) for k in ("census_started", "census_finished", "census_skipped")] == [
        "census"
    ] * 3
    assert category("pulse_finished") == "pulse"
    assert {category(k) for k in ("down", "recovered", "node_offline", "no_node")} == {"incident"}
    assert category("new_hosting_asn") == category("ip_overlap") == "audit"


def events_fixture(database):
    seed(database)
    with connect(database) as conn:
        alpha, beta = product(conn, "alpha"), product(conn, "beta", "datacenter")
        pulses(conn, alpha, [95] * 6 + [0])
        pulses(conn, beta, [95] * 6 + [60], target="generic_https")
        detect(conn, after(6), config=SETTINGS)
        conn.execute(
            "INSERT INTO test_events(at,kind,country_code,region_id,signal,data) SELECT %s,"
            "'census_started','DE',region_id,0,'{\"requests\":0}' FROM countries "
            "WHERE country_code='DE'",
            (after(7),),
        )
        # As the orchestrator writes them: per product, with the batch's country and target.
        conn.execute(
            "INSERT INTO test_events(at,kind,product_id,country_code,target_id,region_id,"
            "signal,data) SELECT %s,'census_finished',%s,'DE','generic_http',region_id,1,"
            "'{\"requests\":10,\"unique_ips\":4}' FROM countries WHERE country_code='DE'",
            (after(8), alpha),
        )
        return [r[0] for r in conn.execute("SELECT at FROM test_events ORDER BY at,event_id")]


async def test_events_api_filters_and_shape(database):
    times = events_fixture(database)
    async with TestClient(TestServer(app(database))) as client:

        async def get(query=""):
            response = await client.get("/v1/events" + query)
            assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
            return (await response.json())["events"]

        everything = await get()
        assert [e["kind"] for e in everything] == [
            "census_finished",
            "census_started",
            "success_drop",
            "down",
        ]
        down = everything[3]
        assert set(down) == {
            "id",
            "at",
            "kind",
            "category",
            "product",
            "provider",
            "country",
            "region",
            "target",
            "signal",
            "severity",
            "data",
        }
        assert (down["product"], down["provider"], down["category"]) == (
            "alpha/residential",
            "alpha",
            "incident",
        )
        assert (down["country"], down["region"], down["target"]) == ("DE", "fra1", "generic_http")
        assert everything[1]["product"] == "" and everything[1]["category"] == "census"
        census = everything[0]
        assert (census["product"], census["provider"], census["category"]) == (
            "alpha/residential",
            "alpha",
            "census",
        )
        assert (census["country"], census["region"], census["data"]["unique_ips"]) == (
            "DE",
            "fra1",
            4,
        )
        assert [e["kind"] for e in await get("?product=alpha")] == ["census_finished", "down"]
        assert await get("?product=alpha/residential&category=census") == [census]
        assert [e["kind"] for e in await get("?product=beta/datacenter")] == ["success_drop"]
        assert await get("?product=beta/residential") == []
        assert [e["kind"] for e in await get("?kind=down,census_started")] == [
            "census_started",
            "down",
        ]
        assert [e["kind"] for e in await get("?category=census")] == [
            "census_finished",
            "census_started",
        ]
        assert len(await get("?category=incident&limit=1")) == 1
        # since is exclusive and empty means none (the site's first poll).
        newest = everything[0]["at"]
        assert await get("?since=" + newest.replace("+", "%2B")) == []
        assert len(await get("?since=")) == 4
        assert len(await get("?since=" + times[0].isoformat().replace("+", "%2B"))) == 2
        for bad in ("?category=other", "?limit=0", "?limit=501", "?since=yesterday", "?x=1"):
            assert (await client.get("/v1/events" + bad)).status == 400
    with connect(database) as conn:
        assert list_events(conn, before=times[-1]) == everything[1:]


async def test_traffic_api_cli_and_sums(database, tmp_path, capsys):
    as_of, _ = await scenario(database, tmp_path)
    with connect(database) as conn:
        inputs = traffic.TrafficInputs
        report = traffic.summary(conn, inputs())
        pulse_only = traffic.summary(conn, inputs(test="pulse", provider="alpha/residential"))
        none = traffic.summary(conn, inputs(country="FR"))
        windowed = traffic.summary(conn, inputs(end=as_of - timedelta(minutes=60)))
    assert report["total"]["requests"] == 312 and report["total"]["bytes_total"] == 18600
    assert {r["provider"]: r["requests"] for r in report["by_provider"]} == {
        "alpha": 308,
        "beta": 4,
    }
    alpha_census = next(
        r for r in report["rows"] if (r["test"], r["provider"]) == ("census", "alpha")
    )
    assert alpha_census["artifact_runs"] == 1 and alpha_census["bytes_up"] == 300
    assert alpha_census["gb_total"] == 1200 / 10**9
    assert alpha_census["avg_bytes_per_request"] == 300
    assert pulse_only["total"]["runs"] == 4 and pulse_only["by_test"]["census"]["runs"] == 0
    assert none["total"] == traffic.totals([]) and none["total"]["avg_bytes_per_request"] is None
    assert windowed["total"]["runs"] == 2  # only the two pulses scheduled an hour earlier
    async with TestClient(TestServer(app(database))) as client:
        response = await client.get("/v1/traffic?test=census&provider=beta")
        body = await response.json()
        assert body["total"]["requests"] == 4 and body["filters"]["provider"] == "beta"
        assert (await client.get("/v1/traffic?test=other")).status == 400
        assert (await client.get("/v1/traffic?from=never")).status == 400
    args = ["report", "traffic", "--database-file", str(database), "--test", "census"]
    assert main(args) == 0
    table = capsys.readouterr().out.splitlines()
    assert table[0].split() == [
        "TEST",
        "PROVIDER",
        "PRODUCT",
        "RUNS",
        "REQUESTS",
        "GB",
        "UP",
        "GB",
        "DOWN",
        "GB",
        "TOTAL",
        "AVG",
        "B/REQ",
    ]
    assert table[1].split()[:5] == ["census", "alpha", "residential", "1", "4"]
    assert table[-1].split()[:5] == ["all", "all", "2", "8", "0.000"]
    assert main([*args, "--json", "--region", "fra1", "--from", "2000-01-01"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["total"]["bytes_up"] == 600 and printed["period"]["from"].startswith("2000")
    assert Counter(r["test"] for r in printed["rows"]) == {"census": 2}


def coverage_policy(tmp_path):
    """The shipped methodology (minimum_coverage 0.90) with a one-request minimum."""
    policy = tmp_path / "methodology.toml"
    policy.write_text(POLICY.read_text().replace("minimum_requests = 4000", "minimum_requests = 1"))
    return policy


async def test_scheduled_coverage_is_the_share_of_pulse_slots(database, tmp_path):
    """Operator skips/kills and failed runs (a run is only ever failed by our side) are not
    slots and never count against the provider. A provider that is down (a done run whose
    requests all failed) fills its slot: it lowers success and uptime, never coverage. 40 of 48
    slots filled (8 done without a request) is below minimum_coverage 0.90."""
    store.initialize(database)
    seed(database)
    directory, summary = await census(tmp_path, provider="alpha")
    as_of = datetime.fromisoformat(summary["finished_at"]) + timedelta(minutes=10)
    slot = [as_of - timedelta(minutes=30 * (i + 1)) for i in range(48)]
    with connect(database) as conn:
        first = batch(conn, "pulse", slot[0])
    linked = ingest(directory, database, batch_id=first)["run_key"]
    with connect(database) as conn:
        alpha = product(conn)
        batch_run(conn, first, alpha, slot[0] + timedelta(minutes=1), run_key=linked, requests=4)
        for moment in slot[1:47]:
            batch_run(conn, batch(conn, "pulse", moment), alpha, moment + timedelta(minutes=1))
        # The provider is down: every request of the slot's run failed.
        down = batch(conn, "pulse", slot[47])
        batch_run(conn, down, alpha, slot[47] + timedelta(minutes=1), requests=100, successes=0)
        # Not slots: runs our side failed (whatever they counted) and an operator-killed batch.
        for minute, error in (
            (3, "node error (OSError)"),
            (4, "node_shutdown"),
            (5, "no_node"),
            (6, "not_started"),
            (7, "stale"),
        ):
            b = batch(conn, "pulse", slot[10] + timedelta(minutes=minute))
            batch_run(conn, b, alpha, slot[10] + timedelta(minutes=minute + 1))
            conn.execute(
                "UPDATE batch_runs SET status='failed',error=%s WHERE batch_id=%s", (error, b)
            )
        killed = batch(conn, "pulse", slot[10] + timedelta(minutes=8), status="killed")
        batch_run(conn, killed, alpha, slot[10] + timedelta(minutes=9), requests=0)
    policy = coverage_policy(tmp_path)

    def window(as_of):
        payload = calculate(database, policy, as_of=as_of)
        scheduled = next(
            p for p in payload["products"] if p["criteria"].get("schedule") == "census_pulse"
        )
        return scheduled["windows"]["24h"]

    w = window(as_of)
    target = w["targets"]["generic_http"]
    assert target["metrics"]["coverage_slots"] == {"due": 48, "filled": 48, "down": 1}
    assert target["metrics"]["coverage"] == 1
    assert w["metrics"]["coverage_basis"] == "pulse slots"
    assert w["metrics"]["coverage"] == 1
    assert target["metrics"]["uptime"] == pytest.approx(47 / 48)
    # Every slot weighs the same: the ingested run (3 of 4), 46 healthy pulses and the down
    # slot, which votes 0. Failed runs add nothing, even the requests they counted.
    assert target["metrics"]["success_rate"] == pytest.approx((0.75 + 46) / 48)
    assert target["metrics"]["time_slots"]["down"] == 1
    assert target["metrics"]["time_slots"]["success"] == 48
    assert "Insufficient time coverage" not in target["score"]["reasons"]
    # Seven more slots with the provider down: still covered, but down.
    with connect(database) as conn:
        conn.execute(
            "UPDATE batch_runs SET successes=0 WHERE batch_id IN "
            "(SELECT batch_id FROM test_batches WHERE scheduled_for=ANY(%s))",
            (slot[1:8],),
        )
    target = window(as_of)["targets"]["generic_http"]  # new inputs, new build
    assert target["metrics"]["coverage_slots"] == {"due": 48, "filled": 48, "down": 8}
    assert "Insufficient time coverage" not in target["score"]["reasons"]
    assert target["metrics"]["uptime"] == pytest.approx(40 / 48)
    # Eight slots done without a single request are gaps.
    with connect(database) as conn:
        conn.execute(
            "UPDATE batch_runs SET status='done',error=NULL,requests=0 WHERE batch_id IN "
            "(SELECT batch_id FROM test_batches WHERE scheduled_for=ANY(%s))",
            (slot[1:9],),
        )
    target = window(as_of)["targets"]["generic_http"]
    assert target["metrics"]["coverage_slots"] == {"due": 48, "filled": 40, "down": 1}
    assert "Insufficient time coverage" in target["score"]["reasons"]
    assert target["score"]["state"] == "incomplete"
