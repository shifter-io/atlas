"""Schema v6: outcome counters, geography buckets, batch links, partitions and the seed."""

import gzip
import hashlib
import json
from datetime import UTC, datetime, time

import pytest
from test_storage import measurement

from milf_framework.cli import main
from milf_framework.regions import REGIONS_FILE
from milf_framework.storage import (
    OUTCOME_KEYS,
    connect,
    ingest,
    partition,
    read_summary,
    seed,
    status,
)

pytest_plugins = ["test_storage"]

BERLIN = {
    "country_code": "DE",
    "country": "Germany",
    "region_code": "BE",
    "region": "Berlin",
    "city": "Berlin",
    "latitude": 52.52,
    "longitude": 13.4,
}
NETWORK = {
    "isp": "Deutsche Telekom",
    "organization": "Telekom",
    "asn": 3320,
    "asn_organization": "Deutsche Telekom AG",
    "connection_type": "Cable/DSL",
    "usage_type": "residential",
}


def load(directory):
    return json.loads((directory / "summary.json").read_text())


def save(directory, summary):
    (directory / "summary.json").write_text(json.dumps(summary))
    return summary


def legacy(directory):
    """Strip every v6 key, whatever the runner emits, leaving a v5-era artifact."""
    summary = load(directory)
    for counters in (summary, *summary["buckets"]):
        for key in OUTCOME_KEYS:
            counters.pop(key, None)
    summary.pop("stop_reason", None)
    summary["artifacts"].pop("geo", None)
    (directory / "geo.jsonl.gz").unlink(missing_ok=True)
    return save(directory, summary)


def write_geo(directory, summary, rows):
    path = directory / "geo.jsonl.gz"
    with gzip.open(path, "wt") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")
    summary["artifacts"]["geo"] = {
        "file": path.name,
        "rows": len(rows),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    return save(directory, summary)


async def census(tmp_path, *, location=BERLIN, network=NETWORK, **options):
    """A v6 artifact: one more request that timed out without an exit address, typed outcome
    counters, requested=null and a geo artifact splitting the bucket in two."""
    directory = await measurement(tmp_path, **options)
    summary = legacy(directory)
    bucket = summary["buckets"][0]
    for counters in (summary, bucket):
        counters["completed"] += 1
        counters["errors"]["timeout"] = counters["errors"].get("timeout", 0) + 1
    for name in ("started", "total_errors"):
        summary[name] += 1
    summary["requested"] = summary["config"]["requests"] = None
    summary["config"]["stop_rule"] = "unique_ratio"
    # The runner records min_sample and stall_seconds for every non-count run (RunConfig.public).
    summary["config"]["min_sample"] = 1000
    summary["config"]["stall_seconds"] = 600
    summary["stop_reason"] = "unique_ratio"
    outcomes = {
        "challenges": 0,
        "blocks": 0,
        "timeouts": 1,
        "proxy_errors": 0,
        "bytes_up": 300,
        "bytes_down": 900,
    }
    summary |= outcomes
    bucket |= outcomes
    rows = [
        {
            "bucket_start": bucket["bucket_start"],
            "location": location,
            "network": network,
            "completed": bucket["successes"],
            "successes": bucket["successes"],
            "challenges": 0,
            "blocks": 0,
            "timeouts": 0,
            "bytes_up": 200,
            "bytes_down": 800,
            "latency_histogram": bucket["latency_histogram"],
        },
        {
            "bucket_start": bucket["bucket_start"],
            "location": None,
            "network": None,
            "completed": 1,
            "successes": 0,
            "challenges": 0,
            "blocks": 0,
            "timeouts": 1,
            "bytes_up": 100,
            "bytes_down": 100,
            "latency_histogram": {"format": "ceil_ms_v1", "bins": []},
        },
    ]
    return directory, write_geo(directory, summary, rows)


GEO_ROWS = (
    "SELECT l.country_code,l.city,l.latitude,n.asn,n.usage_type,g.completed,g.successes,"
    "g.timeouts,g.bytes_up,g.bytes_down,histogram_bins(g.histogram_steps,g.histogram_counts) "
    "FROM run_geo_buckets g JOIN enrichment_locations l USING(location_id) "
    "JOIN enrichment_networks n USING(network_id) ORDER BY g.completed DESC,g.run_key"
)


async def test_geo_buckets_and_outcomes_round_trip(database, tmp_path):
    directory, summary = await census(tmp_path)
    result = ingest(directory, database)
    assert result["imported"] and isinstance(result["run_key"], int)
    assert read_summary(summary["run_id"], database) == summary
    assert not ingest(directory, database)["imported"]
    bins = summary["buckets"][0]["latency_histogram"]["bins"]
    with connect(database) as conn:
        assert conn.execute(GEO_ROWS).fetchall() == [
            ("DE", "Berlin", 52.52, 3320, "residential", 3, 3, 0, 200, 800, bins),
            (None, None, None, None, None, 1, 0, 1, 100, 100, []),
        ]
        bucket = conn.execute(
            "SELECT outcome_counters,timeouts,bytes_up,bytes_down,extra FROM run_buckets"
        ).fetchone()
        assert bucket == (True, 1, 300, 900, None)
        metrics = conn.execute("SELECT metrics FROM scope_stats").fetchone()[0]
        assert metrics["requested"] == metrics["started"] == 4
    # A second run shares the location, network and the single unknown member.
    other, _ = await census(tmp_path)
    ingest(other, database)
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM enrichment_locations").fetchone() == (2,)
        assert conn.execute("SELECT count(*) FROM enrichment_networks").fetchone() == (2,)
        assert conn.execute("SELECT count(*) FROM run_geo_buckets").fetchone() == (4,)
    assert status(database)["counts"]["run_geo_buckets"] == 4
    # A count-rule run of the same product, target and country shares the census pool.
    ingest(await measurement(tmp_path), database)
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM pool_stats").fetchone() == (1,)
        overrides = conn.execute(
            "SELECT summary->'config_overrides' FROM test_runs ORDER BY run_key"
        ).fetchall()
    assert overrides[0][0] == {
        "requests": None, "stop_rule": "unique_ratio", "min_sample": 1000, "stall_seconds": 600,
        "target_url": None,
    }
    assert overrides[2][0] == {"requests": 3, "target_url": None}


async def test_geo_ids_are_the_enrichment_ids(database, tmp_path):
    from test_storage import enrichment

    directory, _ = await census(tmp_path, location={"country_code": "US"}, network={"asn": 15169})
    # Same attributes through the IP enrichment path: missing and null fields are both NULL.
    enrichment(directory, attributes={"country_code": "US", "asn": 15169})
    ingest(directory, database)
    with connect(database) as conn:
        enriched = conn.execute("SELECT location_id,network_id FROM ip_enrichment").fetchone()
        geo = conn.execute(
            "SELECT location_id,network_id FROM run_geo_buckets WHERE successes>0"
        ).fetchone()
    assert enriched == geo


async def test_v5_artifacts_rebuild_without_v6_keys(database, tmp_path):
    directory = await measurement(tmp_path)
    summary = legacy(directory)
    ingest(directory, database)
    stored = read_summary(summary["run_id"], database)
    assert stored == summary
    assert not set(OUTCOME_KEYS) & (set(stored) | set(stored["buckets"][0]))
    # Some but not all outcome keys in a bucket stay verbatim in extra.
    partial = await measurement(tmp_path)
    summary = legacy(partial)
    summary["buckets"][0] |= {"bytes_up": 5, "timeouts": "n/a"}
    save(partial, summary)
    ingest(partial, database)
    assert read_summary(summary["run_id"], database) == summary
    with connect(database) as conn:
        rows = conn.execute(
            "SELECT outcome_counters,bytes_up,extra FROM run_buckets ORDER BY run_key"
        ).fetchall()
    assert rows == [(False, 0, None), (False, 0, {"bytes_up": 5, "timeouts": "n/a"})]


def corrupt_geo(directory, summary, change):
    with gzip.open(directory / "geo.jsonl.gz", "rt") as stream:
        rows = [json.loads(line) for line in stream]
    change(rows)
    write_geo(directory, summary, rows)


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda s, d: (d / "geo.jsonl.gz").write_bytes(b""), "checksum"),
        (lambda s, d: s["artifacts"]["geo"].update(rows=3) or save(d, s), "row count"),
        (lambda s, d: s["artifacts"]["geo"].update(file="../x") or save(d, s), "Invalid geo"),
        (lambda s, d: corrupt_geo(d, s, lambda r: r[1].update(timeouts=0)), "do not reconcile"),
        (lambda s, d: corrupt_geo(d, s, lambda r: r[0].update(bytes_up=1)), "do not reconcile"),
        (lambda s, d: corrupt_geo(d, s, lambda r: r.append(r[1] | {"location": {}})), "Duplicate"),
        (
            lambda s, d: corrupt_geo(d, s, lambda r: r[0]["location"].update(matched=True)),
            "geo dimension",
        ),
        (
            lambda s, d: corrupt_geo(d, s, lambda r: r[0]["network"].update(asn="3320")),
            "geo dimension",
        ),
        (lambda s, d: corrupt_geo(d, s, lambda r: r[0].update(proxy_errors=0)), "Invalid geo"),
        (lambda s, d: corrupt_geo(d, s, lambda r: r[1].update(completed=-1)), "Invalid geo"),
        (lambda s, d: save(d, s | {"bytes_up": 1}), "Outcome counters"),
        (lambda s, d: save(d, {k: v for k, v in s.items() if k != "blocks"}), "Outcome"),
    ],
)
async def test_invalid_v6_artifacts_are_rejected(database, tmp_path, change, message):
    directory, summary = await census(tmp_path)
    change(summary, directory)
    with pytest.raises(ValueError, match=message):
        ingest(directory, database)
    assert all(value == 0 for value in status(database)["counts"].values())


async def test_batch_link(database, tmp_path):
    seed(database)
    with connect(database) as conn:
        first, second = (
            conn.execute(
                "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
                "start_at) SELECT 'census','DE',region_id,'generic_http',%s,%s FROM countries "
                "WHERE country_code='DE' RETURNING batch_id",
                (moment, moment),
            ).fetchone()[0]
            for moment in (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC))
        )
    directory, summary = await census(tmp_path)
    ingest(directory, database, batch_id=first)
    assert not ingest(directory, database, batch_id=first)["imported"]
    with pytest.raises(ValueError, match="another batch"):
        ingest(directory, database, batch_id=second)
    unlinked = await measurement(tmp_path)
    ingest(unlinked, database)
    ingest(unlinked, database, batch_id=second)
    with connect(database) as conn:
        assert conn.execute(
            "SELECT batch_id FROM test_runs ORDER BY run_key"
        ).fetchall() == [(first,), (second,)]


async def test_seed_is_idempotent_and_keeps_operator_edits(database, tmp_path):
    # Ingest first: the target joins unscheduled, then the seed schedules it once.
    ingest(await measurement(tmp_path), database)
    with connect(database) as conn:
        assert conn.execute(
            "SELECT label,census,pulse_enabled FROM targets WHERE target_id='generic_http'"
        ).fetchone() == (None, False, False)
    result = seed(database)
    assert result["regions"] == 8 and result["countries"] == 249
    with connect(database) as conn:
        assert conn.execute(
            "SELECT count(*),count(DISTINCT region_id) FROM countries WHERE enabled"
        ).fetchone() == (249, 8)
        assert conn.execute(
            "SELECT target_id,census,pulse_enabled,pulse_interval_minutes FROM targets "
            "ORDER BY sort_order"
        ).fetchall() == [("generic_http", True, True, 30), ("generic_https", False, True, 30)]
        original = conn.execute("SELECT census_time FROM countries WHERE country_code='NO'")
        original = original.fetchone()[0]
        conn.execute(
            "UPDATE countries SET census_time='12:34',enabled=false WHERE country_code='NO'"
        )
        conn.execute(
            "UPDATE targets SET pulse_interval_minutes=15,pulse_enabled=false,url='http://old/' "
            "WHERE target_id='generic_https'"
        )
    moved = tmp_path / "regions.yaml"
    text = REGIONS_FILE.read_text()
    moved.write_text(text.replace(", NO,", ",").replace("[GB,", "[NO, GB,"))
    seed(database, moved)
    with connect(database) as conn:
        assert conn.execute(
            "SELECT r.slug,c.census_time,c.enabled FROM countries c JOIN runner_regions r "
            "USING(region_id) WHERE country_code='NO'"
        ).fetchone() == ("lon1", time(12, 34), False)
        assert conn.execute(
            "SELECT pulse_interval_minutes,pulse_enabled,url FROM targets "
            "WHERE target_id='generic_https'"
        ).fetchone() == (15, False, "https://ip-info.com/myip")
    assert main(["db-seed", "--database-file", str(database), "--reset-times"]) == 0
    with connect(database) as conn:
        assert conn.execute(
            "SELECT r.slug,c.census_time,c.enabled FROM countries c JOIN runner_regions r "
            "USING(region_id) WHERE country_code='NO'"
        ).fetchone() == ("ams3", original, False)
        assert conn.execute("SELECT count(*) FROM runner_regions").fetchone() == (8,)


def test_partition_helper_serves_test_events(database):
    seed(database)
    moment = datetime(2026, 10, 31, 23, 59, tzinfo=UTC)
    with connect(database) as conn:
        partition(conn, "test_events", moment)
        partition(conn, "test_events", moment)
        conn.execute(
            "INSERT INTO test_events(at,kind,country_code,target_id,signal,data) "
            "VALUES (%s,'census_started','DE','generic_http',1,'{\"requests\":0}')",
            (moment,),
        )
        assert conn.execute(
            "SELECT tableoid::regclass::text,kind FROM test_events"
        ).fetchone() == ("test_events_2026_10", "census_started")


class FakeEnricher:
    release = {"sha256": "b" * 64}

    def lookup(self, address):
        return {"matched": True, **BERLIN, **NETWORK}


@pytest.mark.parametrize("stop_rule", ["count", "unique_ratio"])
async def test_runner_geo_artifact_ingests(database, tmp_path, stop_rule):
    """Whatever the runner writes today (pulse and census) must ingest and rebuild exactly."""
    from helpers import FakeTransport

    from milf_framework import JsonSink, ProxyConfig, RunConfig, run

    sink = JsonSink(tmp_path / "results")
    census = stop_rule == "unique_ratio"
    await run(
        RunConfig(
            "generic_http",
            ProxyConfig("http", "unused.invalid", 8080),
            requests=None if census else 5,
            concurrency=1 if census else 2,
            provider="fixture",
            product="residential",
            stop_rule=stop_rule,
        ),
        sink,
        transport_factory=FakeTransport,
        enricher=FakeEnricher(),
    )
    summary = load(sink.directory)
    assert "geo" in summary["artifacts"] and "release" in summary["artifacts"]["geo"]
    assert (summary["requested"] is None) is census
    ingest(sink.directory, database)
    assert read_summary(summary["run_id"], database) == summary
    with connect(database) as conn:
        assert conn.execute(
            "SELECT sum(g.completed),min(l.city) FROM run_geo_buckets g "
            "JOIN enrichment_locations l USING(location_id)"
        ).fetchone() == (summary["completed"], "Berlin")
        assert conn.execute("SELECT bool_and(outcome_counters) FROM run_buckets").fetchone() == (
            True,
        )
