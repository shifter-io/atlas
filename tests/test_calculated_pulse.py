"""Calculated v3 over census and pulse batches: event detection. The events and traffic API,
the traffic report and scheduled coverage are in `test_calculated_pulse_reports.py`."""

from datetime import timedelta

import pytest
from calculated_cases import OtherIP
from calculated_pulse_cases import SETTINGS, T0, after, pulses
from census_pulse_cases import HOSTING, batch, batch_run, product
from helpers import FakeTransport
from psycopg.rows import dict_row
from test_storage import measurement
from test_storage_v6 import census

from atlas.detect import WATERMARK, detect
from atlas.storage import connect, ingest, seed

pytest_plugins = ["test_storage"]


def run_key(database, directory):
    return ingest(directory, database)["run_key"]


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
        assert detect(conn, after(10), config=SETTINGS) == 3
        # The first pulse without success waits for the next one (PULSE-NODE-DNS-2).
        down = [("pulse_finished", -1), ("down", -1), ("recovered", 1)]
        assert kinds(conn, drop["event_id"]) == down
        recovered = conn.execute("SELECT data FROM test_events WHERE kind='recovered'").fetchone()[
            0
        ]
        # Healthy again against the baseline the incident was judged by (which held the 70 and
        # the first pulse without success).
        assert recovered["incident"] == "down"
        assert recovered["baseline"] == pytest.approx((6 * 95 + 70 + 0) / 800)
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
        # The previous pulse's counts, not only its rate (TAPE-VALUES-2): 75 of its 100 requests.
        assert (change[0]["previous"]["successes"], change[0]["previous"]["requests"]) == (75, 100)
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
        # minute after it started) to the start of its first pulse through on any target (8:
        # http; PULSE-OUTAGE-CLOSE-1), https staying down one more pulse on its own series.
        last_healthy = T0 + timedelta(minutes=30 * 5 + 1)
        assert outages(conn) == [("pulse", last_healthy, T0 + timedelta(minutes=30 * 8))]
        assert events[0][2]["since"] == last_healthy.isoformat(timespec="microseconds")
        assert events[0][2]["source"] == "pulse"
        # Judging the same batches again opens and closes nothing.
        total = conn.execute("SELECT count(*) FROM test_events").fetchone()[0]
        conn.execute("DELETE FROM settings WHERE key=%s", (WATERMARK,))
        assert detect(conn, after(10), config=SETTINGS) == 0
        assert conn.execute("SELECT count(*) FROM test_events").fetchone()[0] == total
        assert len(outages(conn)) == 1


def test_healthy_pulse_closes_a_census_outage_once(database):
    from atlas.storage import partition

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
