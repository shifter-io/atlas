"""PLACE-LATENCY-TEST-1 (#870): simulate_place_buckets.py writes per-place buckets for the stored
daily tests (test data, model place-v1). Its splits are pure; the patch runs on a scratch
database loaded with the real-hours fixture of reserved addresses (test_scale_real_hours.py),
whose IPs here get DB-IP places in and out of their requested country, and unmatched ones."""

import gzip
import hashlib
import importlib
import json
import math
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from itertools import accumulate
from pathlib import Path

import pytest
from orchestrator_cases import query

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "scale"))
patch = importlib.import_module("simulate_place_buckets")
real = importlib.import_module("test_scale_real_hours")
scored = importlib.import_module("test_scale_simulate_scored")
sim = real.sim

from atlas import production_guard, storage  # noqa: E402
from atlas.calculated import calculator  # noqa: E402

pytest_plugins = ["test_storage"]

PLACES = (  # an IP's DB-IP place, by the md5 of its address; None: unmatched
    {"country_code": "US", "region": "California", "city": "San Jose", "asn": 64500},
    {"country_code": "US", "region": "New York", "city": "New York", "asn": 64501},
    {"country_code": "US", "region": "New York", "city": "New York", "asn": 64502},
    {"country_code": "DE", "region": "Berlin", "city": "Berlin", "asn": 64510},
    {"country_code": "DE", "region": "Hamburg", "city": "Hamburg", "asn": 64511},
    None,
)
UNKNOWN = (901, 902)


def bucket(at, completed, successes, bins, **outcomes) -> dict:
    """A run_buckets row as patch.bucket() reads it; bins {ms: n}."""
    ms = sorted(bins)
    found = {"run_key": 1, "at": at, "completed": completed, "successes": successes}
    found |= dict.fromkeys(("challenges", "blocks", "timeouts", "proxy_errors"), 0)
    found |= {"bytes_up": 7 * completed, "bytes_down": 1000 * successes + 3} | outcomes
    found["steps"] = [b - a for a, b in zip([0, *ms], ms, strict=False)]
    return found | {"counts": [bins[m] for m in ms]}


def bins_of(steps, counts) -> Counter:
    return Counter(dict(zip(accumulate(steps), counts, strict=True)))


def test_a_histogram_spreads_over_shares_exactly_and_across_its_ranks():
    steps, counts = [100, 50, 50, 300], [10, 40, 30, 20]
    shares = [37, 0, 1, 50, 12]
    found = patch.spread_bins(steps, counts, shares)
    assert [sum(own.values()) for own in found] == shares
    assert sum(found, Counter()) == bins_of(steps, counts)  # every bin's count, exactly
    assert set(found[3]) == {100, 150, 200, 500}  # a large share samples the whole histogram
    assert all(math.gcd(patch.stride(n), n) == 1 for n in range(1, 500))
    assert patch.spread_bins([5], [0], [0, 0]) == [Counter(), Counter()]


def test_a_bucket_splits_by_ip_count_and_proxy_errors_go_to_the_unknown_row():
    at = datetime(2026, 10, 25, 12, tzinfo=UTC)
    stored = bucket(at, 100, 80, {120: 50, 300: 30}, challenges=5, timeouts=6, proxy_errors=4)
    rows = patch.split(stored, [5, 3, 0, 2])
    for key in ("successes", "challenges", "blocks", "timeouts", "bytes_up", "bytes_down"):
        assert sum(row[key] for row in rows) == stored[key]
    assert sum(row["completed"] for row in rows) == 100 and rows[2]["completed"] == 0
    assert rows[-1]["completed"] >= 4 and [row["successes"] for row in rows] == [40, 24, 0, 16]
    assert sum((row["bins"] for row in rows), Counter()) == Counter({120: 50, 300: 30})
    for wrong in ({"completed": 50}, {"counts": [50, 29]}):
        with pytest.raises(patch.Refused, match="does not add up"):
            patch.split(stored | wrong, [1])


def test_a_runs_groups_are_its_places_in_the_requested_country_and_the_rest_unknown():
    run = patch.Run(1, "alpha", "us", False)
    found = [(11, 21, "US", 64500, 3), (12, 22, "DE", 64510, 2), (None, None, None, None, 4)]
    own = patch.groups(run, [*found, (13, 23, "US", None, 1)], UNKNOWN)
    assert [(g.location_id, g.network_id, g.ips) for g in own] == [
        (11, 21, 3),
        (13, 23, 1),
        (*UNKNOWN, 6),
    ]
    assert own[0].spread == patch.SPREAD * patch.signed_unit("alpha:us:11:64500")
    assert own[1].spread == patch.SPREAD * patch.signed_unit("alpha:us:13:")
    empty = patch.groups(run, [], UNKNOWN)
    assert [(g.location_id, g.ips, g.spread) for g in empty] == [(UNKNOWN[0], 1, 0.0)]


def test_a_runs_rows_add_up_and_centre_the_place_latency():
    run = patch.Run(1, "alpha", "us", False)
    found = [(10 + n, 20 + n, "US", 64500 + n, 1 + n) for n in range(6)]
    own = patch.groups(run, [*found, (None, None, None, None, 2)], UNKNOWN)
    at = datetime(2026, 10, 25, 12, tzinfo=UTC)
    buckets = [bucket(at, 60, 55, {200: 30, 400: 25}), bucket(at.replace(minute=5), 4, 0, {})]
    rows = patch.run_rows(run, own, buckets)
    assert len({row[:4] for row in rows}) == len(rows)
    assert sum(row[4] for row in rows) == 64 and sum(row[5] for row in rows) == 55
    spread = {g.location_id: g.spread for g in own}
    places = [row for row in rows if row[2] != UNKNOWN[0]]
    centre = sum(row[5] * spread[row[2]] for row in places) / sum(row[5] for row in places)
    for row in rows:  # a place's bins at ceil(ms x g), g centred; the unknown row's as stored
        ms = bins_of(row[11], row[12])
        assert sum(ms.values()) == row[5] and (row[5] > 0) == bool(ms)  # no successes: no bins
        g = 1 if row[2] == UNKNOWN[0] else math.exp(spread[row[2]] - centre)
        assert set(ms) <= {math.ceil(200 * g - 1e-9), math.ceil(400 * g - 1e-9)}
    assert len({row[2] for row in places if row[5]}) == 6
    one = patch.run_rows(run, own, patch.merged(buckets))
    assert {row[0] for row in one} == {at} and sum(row[4] for row in one) == 64


# The patch, on a scratch database.

COUNTERS = ("completed", "successes", "challenges", "blocks", "timeouts", "bytes_up", "bytes_down")
GEO_SUMS = (  # per daily run and bucket: the stored bucket and its geo rows' sums
    f"SELECT {','.join(f'b.{c}' for c in COUNTERS)},{','.join(f'g.{c}' for c in COUNTERS)},g.bins "
    "FROM run_buckets b JOIN test_runs t USING(run_key) LEFT JOIN (SELECT run_key,bucket_start,"
    f"{','.join(f'sum({c})::bigint AS {c}' for c in COUNTERS)},sum(bins)::bigint AS bins "
    "FROM (SELECT *,(SELECT coalesce(sum(c),0) FROM unnest(histogram_counts) c) AS bins "
    "FROM run_geo_buckets) x GROUP BY 1,2) g USING(run_key,bucket_start) "
    "WHERE t.summary->>'test_kind'='daily'"
)
MARKS = "SELECT summary->'artifacts'->'geo' FROM test_runs WHERE summary->>'test_kind'='daily'"
DAILY_GEO = (
    "SELECT count(*) FROM run_geo_buckets g JOIN test_runs t USING(run_key) "
    "WHERE t.batch_id IS NULL"
)
WINDOWS = (  # each window's figures but its ids and latency (compared within 5%)
    "SELECT p.provider,w.cohort,w.target,w.window_hours,to_jsonb(w)-'result_id'-'build_id'"
    "-'list_id' FROM calculated_windows w JOIN products p USING(product_id) WHERE w.build_id=%s"
)
LIST_ROWS = (
    "SELECT p.provider,w.cohort,w.window_hours,r.dimension,r.place_id,r.asn_label_id,"
    "r.completed,r.successes,r.p50 FROM calculated_windows w JOIN products p USING(product_id) "
    "JOIN calculated_list_rows r USING(list_id) WHERE w.build_id=%s"
)
LATENCY = ("p50", "p90", "p95", "p99")


def hour_file(folder: Path, hour: int, rows: list) -> dict:
    """real.hour_file with a DB-IP place per IP (PLACES)."""
    lines = []
    for ip, countries in rows:
        place = PLACES[int(hashlib.md5(ip.encode()).hexdigest(), 16) % len(PLACES)]
        enriched = {"matched": False}
        if place:
            named = place | {"asn_name": f"Org {place['asn']}", "usage_type": "isp"}
            enriched = real.enrichment(named)
        record = {"ip": ip, "seen_count": sum(countries.values()), "regions": {"r1": 1}}
        record |= {"countries": countries, "enrichment": enriched}
        lines.append(json.dumps(record | {"dbip_release": real.RELEASE}, separators=(",", ":")))
    data = gzip.compress(("\n".join(lines) + "\n").encode(), mtime=0)
    name = f"hour-20261025T{hour:02d}Z.jsonl.gz"
    (folder / name).write_bytes(data)
    slot = {"status": "present", "file": name, "sha256": hashlib.sha256(data).hexdigest()}
    total = sum(sum(countries.values()) for _, countries in rows)
    return slot | {"unique_ips": len(rows), "request_rows": total, "requests": total}


@pytest.fixture
def source(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(real, "hour_file", hour_file)
    root = tmp_path / "lc-enrich"
    for network in real.NETWORKS:
        folder = root / network
        folder.mkdir(parents=True)
        slots = {"12": {"status": "missing"}} if network == "gamma" else {}
        for hour in real.HOURS:
            if (network, hour) in real.records():
                slots[f"{hour:02d}"] = hour_file(folder, hour, real.records()[network, hour])
        manifest = {"schema_version": 2, "network": network, "product": "residential"}
        manifest["dbip_release"] = {"sha256": real.RELEASE, "file": "dbip-full-2026-10.mmdb"}
        (folder / "manifest.json").write_text(json.dumps(manifest | {"slots": slots}))
    return root


def builds(database) -> dict:
    """The day-one and day-two builds (drafts: the load published a later one), incremental as
    `calculate` runs."""
    found = {}
    for day in (1, 2):
        started = time.monotonic()
        at = scored.day_start(day + 1)
        payload = calculator.calculate(database, scored.METHODOLOGY, as_of=at, mode="incremental")
        found[day] = (payload, time.monotonic() - started)
    return found


def figures(database, payload) -> dict:
    """The build's windows (WINDOWS), its networks' scores and uptime, and its list rows."""
    build = int(payload["snapshot_id"])
    windows = {tuple(row[:4]): row[4] for row in query(database, WINDOWS, (build,))}
    networks = {
        (entry["provider"], label): (scores(window["scores"]), window["uptime"]["value"])
        for entry in payload["networks"]
        for label, window in entry["windows"].items()
    }
    rows = {tuple(row[:6]): row[6:] for row in query(database, LIST_ROWS, (build,))}
    return {"windows": windows, "networks": networks, "rows": rows}


def scores(found: dict) -> dict:
    """Each score's value, rank, state and components' values (not the previous build's)."""
    return {
        name: (s.get("score"), s.get("rank"), s.get("state"))
        + (sorted((k, c.get("state"), c.get("value")) for k, c in s["components"].items()),)
        for name, s in found.items()
        if isinstance(s, dict) and "components" in s
    }


def latency_change(before: dict, after: dict) -> float:
    """The largest relative change of a window's latency; every other figure is equal."""
    assert before.keys() == after.keys()
    largest = 0.0
    for key, old in before.items():
        new = after[key]
        assert {k: v for k, v in old.items() if k not in LATENCY} == {
            k: v for k, v in new.items() if k not in LATENCY
        }, key
        for k in LATENCY:
            assert (old[k] is None) == (new[k] is None), (key, k)
            if old[k]:
                largest = max(largest, abs(new[k] - old[k]) / old[k])
    return largest


def args(database, *extra) -> list:
    span = ["--from", scored.day_start(1).isoformat(), "--to", scored.day_start(3).isoformat()]
    return ["--database", str(database), *span, *extra]


def test_the_patch_fills_the_place_rows_and_changes_no_other_figure(
    staging, inputs, source, tmp_path, monkeypatch, capsys
):
    with storage.connect(staging) as conn:  # a pulse an hour: fewer runs
        conn.execute("UPDATE targets SET pulse_interval_minutes=60 WHERE pulse_enabled")
    load = real.argv(staging, inputs, tmp_path / "out", source)
    assert sim.simulate.main(load) == 0
    assert sim.simulate.main([*load, "--add-scored-inputs"]) == 0
    with storage.connect(staging) as conn:
        conn.execute(scored.EVIDENCE, tuple(scored.day_start(d) for d in (-29, 1, 5)))
    capsys.readouterr()
    before = builds(staging)
    ips = query(staging, scored.FIGURES)[0]
    assert query(staging, DAILY_GEO)[0][0] == 0
    # Production without #817's three is refused; a dry run writes nothing.
    name = query(staging, "SELECT current_database()")[0][0]
    monkeypatch.setattr(production_guard, "PRODUCTION", name)
    assert patch.main(args(staging)) == 2 and "production" in capsys.readouterr().err
    monkeypatch.setattr(production_guard, "PRODUCTION", "proxy_reviews")
    assert patch.main(args(staging, "--dry-run")) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] and dry["patched"] == dry["daily_runs"] == 10 and dry["rows"] > 0
    assert query(staging, DAILY_GEO)[0][0] == 0 and query(staging, MARKS) == [(None,)] * 10
    started = time.monotonic()
    assert patch.main(args(staging)) == 0
    done = json.loads(capsys.readouterr().out)
    minutes = (time.monotonic() - started) / 60
    assert done["rows"] == dry["rows"] == query(staging, DAILY_GEO)[0][0]
    assert done["rollup_hours_reset"] > 0 and not done["dry_run"]
    for row in query(staging, GEO_SUMS):  # every bucket's sums, exactly
        assert row[:7] == row[7:14] and row[14] == row[1]
    marks = [mark for (mark,) in query(staging, MARKS)]
    assert {m["model"] for m in marks} == {"place-v1"}
    assert sum(m["rows"] for m in marks) == done["rows"]
    assert query(staging, scored.FIGURES)[0] == ips  # no IP written anywhere new
    assert patch.main(args(staging)) == 0  # again: nothing
    again = json.loads(capsys.readouterr().out)
    assert again["patched"] == again["rows"] == 0 and again["skipped"] == 10
    after = builds(staging)
    report = {"rows": done["rows"], "runs": done["patched"], "patch_minutes": round(minutes, 3)}
    for day in (1, 2):
        old, new = figures(staging, before[day][0]), figures(staging, after[day][0])
        assert int(before[day][0]["snapshot_id"]) != int(after[day][0]["snapshot_id"])
        assert new["networks"] == old["networks"]  # every score, rank and uptime
        report[f"day{day}"] = check_rows(old["rows"], new["rows"])
        report[f"day{day}"]["window_latency"] = latency_change(old["windows"], new["windows"])
        report[f"day{day}"]["seconds"] = [round(before[day][1], 2), round(after[day][1], 2)]
    print("PLACE-BUCKETS", json.dumps(report))
    assert report["day2"]["window_latency"] <= 0.05


def check_rows(old: dict, new: dict) -> dict:
    """Country rows' requests and successes equal. A place row the patch changed (a daily
    window's) has requests and a p50 when it has successes, none without; returns the largest
    country p50 change and the changed place rows per dimension."""
    countries = {k: v for k, v in old.items() if k[3] == "c"}
    assert {k: v[:2] for k, v in new.items() if k[3] == "c"} == {
        k: v[:2] for k, v in countries.items()
    }
    changes = [abs(new[k][2] - v[2]) / v[2] for k, v in countries.items() if v[2]]
    places = Counter()
    for key, (completed, successes, p50) in new.items():
        if key[3] == "c" or old.get(key) == (completed, successes, p50):
            continue
        assert (p50 is not None) == bool(successes), key
        assert (completed or 0) >= (successes or 0), key
        places[key[3]] += bool(successes)
    found = {k for k, n in places.items() if n}
    assert found >= {"g", "r", "a", "y"}, places  # city, region, ASN, City x ASN; country x ASN
    return {"country_p50": round(max(changes, default=0.0), 4), "place_rows": dict(places)}


@pytest.fixture
def staging(database, monkeypatch):
    return sim.staged(database, monkeypatch)


inputs = real.inputs


def test_a_runs_geo_artifact_changes_its_fingerprint_and_list_signature():
    from atlas.calculated import incremental, lists

    run = {"run_key": 1, "summary": {"artifacts": {"ips": {"rows": 3}}}, "batch_id": None}
    patched = run | {"summary": {"artifacts": {"ips": {"rows": 3}, "geo": {"model": "place-v1"}}}}
    assert lists.geo_artifact(run) is None and lists.geo_artifact({"summary": None}) is None
    assert incremental._hashed(run) == {"run_key": 1, "batch_id": None}  # as before
    assert incremental._hashed(patched)["geo"] == {"model": "place-v1"}
