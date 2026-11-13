"""SIM-PROFILE-1: profile.py builds the statistics and size profile from LC-ENRICH-1 and
BP-VERIFY-3 shaped fixtures (reserved addresses only) and adds the database reference; no
input IP, host name or path reaches the output."""

from __future__ import annotations

import gzip
import importlib
import importlib.util
import json
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from orchestrator_cases import prepare, query
from storage_cases import enrichment, measurement

from atlas.browser.engines import DEFAULT_ENGINE
from atlas.partitions import partition
from atlas.storage import connect, ingest

SCALE = Path(__file__).resolve().parents[1] / "scripts" / "scale"
sys.path.insert(0, str(SCALE))
profile_hours = importlib.import_module("profile_hours")
profile_reference = importlib.import_module("profile_reference")
# Not `import profile`: the standard library has a module of that name.
_spec = importlib.util.spec_from_file_location("scale_profile", SCALE / "profile.py")
profile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(profile)

pytest_plugins = ["test_storage"]

READ_ONLY, LEVEL = "transaction_read_only", "transaction_isolation"

HOUR_FOLDERS = ("2026-11-08T00", "2026-11-08T01")
HOSTS = ("mini-alpha.example.net", "browser-1", "browser-files/alpha/residential/x.tar.gz")
TOKYO = {"country_code": "JP", "region": "Tokyo", "city": "Tokyo", "asn": 64500}
TOKYO |= {"usage_type": "consumer", "latitude": 35.68, "longitude": 139.76}
TOKYO |= {"asn_organization": "Tokyo } Org"}  # a brace: parsed from the whole line
OSAKA = TOKYO | {"region": "Osaka", "city": "Osaka", "latitude": 34.69, "longitude": 135.5}
US = {"country_code": "US", "region": "New York", "city": "New York", "asn": 64501}
US |= {"usage_type": "hosting", "latitude": 40.71, "longitude": -74.0}
# alpha's hours: (slot, hour_start, records as (address, {country: rows}, enrichment)).
ALPHA = {
    "23": (
        "2026-01-01T23:00:00Z",
        [
            ("192.0.2.1", {"US": 1}, US),
            ("192.0.2.2", {"US": 2}, US),
            ("192.0.2.3", {"US": 3}, TOKYO),
            ("192.0.2.4", {"US": 1, "DE": 2}, US),
            ("192.0.2.10", {"DE": 4}, None),
            ("192.0.2.11", {"DE": 1}, OSAKA),
        ],
    ),
    "00": ("2026-01-02T00:00:00Z", [("192.0.2.1", {"US": 8}, US), ("192.0.2.5", {"US": 1}, US)]),
    "01": (
        "2026-01-02T01:00:00Z",
        [
            ("192.0.2.1", {"US": 1}, US),
            ("192.0.2.2", {"US": 1}, US),
            ("192.0.2.6", {"US": 1}, {**US, "matched": False}),
            ("192.0.2.10", {"DE": 1}, OSAKA),
            ("2001:db8::1", {"DE": 1}, TOKYO),
        ],
    ),
}
ADDRESSES = sorted({ip for _, records in ALPHA.values() for ip, *_ in records} | {"2001:db8::9"})


def record(ip, countries, place):
    seen = sum(countries.values())
    enriched = None if place is None else {"matched": True, "isp": "Fixture"} | place
    return {
        "ip": ip,
        "seen_count": seen,
        "regions": {"r1": seen},
        "countries": countries,
        "enrichment": enriched,
        "dbip_release": "dbip-test",
    }


def hour_slot(folder: Path, key: str, start: str, records: list) -> dict:
    stamp = start[:13].replace("-", "")
    name = f"hour-{stamp}Z.jsonl.gz"
    rows = [record(*item) for item in records]
    data = b"".join(json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in rows)
    (folder / name).write_bytes(gzip.compress(data, mtime=0))
    request_rows = sum(row["seen_count"] for row in rows)
    return {
        "status": "present",
        "file": name,
        "hour_start": start,
        "unique_ips": len(rows),
        "request_rows": request_rows,
        "requests": request_rows + 3,
        "successes": request_rows,
        "errors": {"timeout": 2, "http_status": 1},
        "gateway": HOSTS[0],
    }


def write_network(root: Path, name: str, hours: dict, other: dict | None = None) -> None:
    """A network folder: its hour files (slot -> (hour_start, records)), `other` slots."""
    folder = root / name
    folder.mkdir(parents=True)
    slots = dict(other or {})
    slots |= {key: hour_slot(folder, key, *hour) for key, hour in hours.items()}
    manifest = {"schema_version": 2, "network": name, "product": "residential", "slots": slots}
    (folder / "manifest.json").write_text(json.dumps(manifest))


def enrich_fixture(root: Path) -> None:
    write_network(root, "alpha", ALPHA)
    # Byteful-like: IPs in one hour, requests without IPs in the 23 others.
    no_ips = {"status": "requests_no_ips", "unique_ips": 0, "request_rows": 0, "requests": 50}
    no_ips |= {"errors": {"proxy": 50}}
    hours = {"16": ("2026-01-01T16:00:00Z", [("2001:db8::9", {"US": 2}, US)])}
    write_network(root, "beta", hours, {f"{hour:02d}": dict(no_ips) for hour in range(24)})
    write_network(root, "delta", {})  # no formula


def run(batch, provider, target, status, requests, **counts):
    stored = status == "done"
    return {
        "batch": batch,
        "target": target,
        "cc": "US",
        "provider": provider,
        "product": "residential",
        "run_status": status,
        "requests": requests,
        "successes": counts.get("ok", 0),
        "challenges": counts.get("challenge", 0),
        "blocks": 0,
        "timeouts": counts.get("timeout", 0),
        "proxy_errors": 0,
        "bytes_down": counts.get("bytes", 0),
        "run_key": batch if stored else None,
        "test_runs": 1 if stored else 0,
        "host": HOSTS[1],
    }


def browser_fixture(root: Path) -> None:
    first = [
        run(10, "alpha", "google_search", "running", 40),
        run(14, "beta", "google_search", "running", 3),
    ]
    google = {"ok": 40, "challenge": 50, "timeout": 5, "bytes": 150_000_000}
    last = [
        run(10, "alpha", "google_search", "done", 100, **google),
        run(11, "alpha", "amazon_pdp", "done", 100, ok=90, bytes=250_000_000),
        run(12, "alpha", "amazon_pdp", "done", 50, ok=50, bytes=100_000_000),
        run(13, "alpha", "ai_chatgpt", "failed", 8),
        run(14, "beta", "google_search", "running", 60),
    ]
    sample = {"present": True, "sha256": "0" * 64, "batch_meta": HOSTS[2]}
    samples = (
        [sample | {"batch": 10, "network": "alpha", "bytes": 1_500_000}],
        [sample | {"batch": 11, "network": "alpha/residential", "bytes": 2_000_000}],
    )
    for hour, runs, archive in zip(HOUR_FOLDERS, (first, last), samples, strict=True):
        folder = root / hour
        folder.mkdir(parents=True)
        mgr = {"hour": hour, "runs": runs, "archive_sample": archive}
        (folder / "mgr.json").write_text(json.dumps(mgr))
        (folder / "browser-1.json").write_text(json.dumps({"runs": [], "host": HOSTS[0]}))


def second_source(root: Path) -> None:
    """BP-VERIFY-4-like: a later batch in an hour folder of the same shape."""
    folder = root / "2026-11-15T00"
    folder.mkdir(parents=True)
    runs = [run(20, "alpha", "linkedin_public", "done", 50, ok=5, bytes=50_000_000)]
    (folder / "mgr.json").write_text(json.dumps({"hour": "x", "runs": runs}))


@pytest.fixture
def built(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_hours, "TOP_PLACES", 2)
    monkeypatch.setattr(profile_hours, "LONG_TAIL_PLACES", 2)
    formulas = tmp_path / "formulas"
    formulas.mkdir()
    for name in ("alpha", "beta", "gamma"):
        (formulas / f"{name}.json").write_text("{}")
    enrich_fixture(tmp_path / "enrich")
    browser_fixture(tmp_path / "hours")
    second_source(tmp_path / "hours4")
    out = tmp_path / "profile.json"
    argv = ["build", "--enrich", str(tmp_path / "enrich"), "--browser", str(tmp_path / "hours")]
    argv += ["--browser", str(tmp_path / "hours4")]
    assert profile.main([*argv, "--out", str(out), "--formulas", str(formulas)]) == 0
    return out


def test_hours_requests_per_ip_and_totals(built):
    data = json.loads(built.read_bytes())
    assert stat.S_IMODE(built.stat().st_mode) == 0o600
    assert data["profile_version"] == 1
    alpha = data["networks"]["alpha"]
    hours = {hour["hour"]: hour for hour in alpha["hours"]}
    assert len(hours) == 24 and hours["05"]["status"] == "missing"
    assert hours["23"] | {"countries": None} == {
        "hour": "23",
        "status": "present",
        "hour_start": "2026-01-01T23:00:00+00:00",
        "unique_ips": 6,
        "request_rows": 14,
        "attempted_requests": 17,
        "successes": 14,
        "errors": 3,
        "errors_by_kind": {"http_status": 1, "timeout": 2},
        "countries": None,
    }
    assert hours["23"]["countries"] == {
        "DE": {"unique_ips": 3, "request_rows": 7},
        "US": {"unique_ips": 4, "request_rows": 7},
    }
    assert hours["01"]["countries"]["DE"] == {"unique_ips": 2, "request_rows": 2}
    # seen_count 1 x8, 2 x1 (192.0.2.2), 3 x2 (.3 and .4), 4 x1, 8 x1
    assert alpha["seen_count_log2"] == {"1": 8, "2-3": 3, "4-7": 1, "8-15": 1}
    assert data["real_24h"]["alpha"] == {
        "unique_ips_hour_sum": 13,
        "unique_ips_distinct": 9,
        "request_rows": 28,
        "attempted_requests": 37,
        "browser_runs": 4,
        "browser_tries": 300,
        "browser_bytes_down": 550_000_000,
        "bundle_bytes_sampled": 3_500_000,
        "bundle_samples": 2,
    }
    beta = data["real_24h"]["beta"]
    assert beta == {
        "unique_ips_hour_sum": 1,
        "unique_ips_distinct": 1,
        "request_rows": 2,
        "attempted_requests": 23 * 50 + 5,
        "browser_runs": 0,  # its one run never finished
        "browser_tries": 0,
        "browser_bytes_down": 0,
        "bundle_bytes_sampled": 0,
        "bundle_samples": 0,
    }


def test_recurrence_uses_the_gap_closest_to_24_hours(built):
    recurrence = json.loads(built.read_bytes())["networks"]["alpha"]["recurrence"]
    # Hours 23 (day 1), 00 and 01 (day 2): gaps 1 and 2 h; 2 h is closest to 24 (23 -> 01).
    assert recurrence | {"other_country": None} == {
        "gap_hours": 2,
        "basis": "the measured gap closest to 24 h",
        "pairs": 1,
        "share": 0.6,  # US 2 of 3 (.1, .2), DE 1 of 2 (.10)
        "countries": {"DE": 0.5, "US": 0.666667},
        "other_country": None,
    }
    # Day sets: US .1-.6 (6), DE .4 .10 .11 db8::1 (4); .4 is in both.
    other = recurrence["other_country"]
    assert other["share"] == 0.2 and other["countries"] == {"DE": 0.25, "US": 0.166667}
    beta = json.loads(built.read_bytes())["networks"]["beta"]["recurrence"]
    assert beta["gap_hours"] is None and beta["share"] is None


def test_gap_pairs():
    def at(day, hour):
        return datetime(2026, 1, day, hour, tzinfo=UTC)

    exact = {"10": at(1, 10), "11": at(1, 11), "09": at(2, 10)}
    assert profile_hours.gap_pairs(exact) == ([("10", "09")], 24)
    tie = {"01": at(1, 1), "00": at(2, 0), "02": at(2, 2)}  # 23 and 25 h: the shorter
    assert profile_hours.gap_pairs(tie) == ([("01", "00")], 23)
    assert profile_hours.gap_pairs({"00": at(1, 0), "01": None}) == ([], None)


def tuples(counts: list[int]) -> dict:
    """The summary of one country whose i-th place has counts[i] records."""
    found = profile_hours._Places()
    for i, count in enumerate(counts):
        for _ in range(count):
            found.add(((f"C{i}", "R", f"City {i}", 64500 + i, "consumer"), 1.0, 2.0, "Org"))
    return found.summary()


def test_a_long_tail_keeps_up_to_the_long_tail_places(monkeypatch):
    monkeypatch.setattr(profile_hours, "TOP_PLACES", 2)
    monkeypatch.setattr(profile_hours, "LONG_TAIL_PLACES", 4)
    long = tuples([50, 30, 8, 7, 5])  # the top 2 leave 20%: 4 kept, 5% in the tail
    assert [place["city"] for place in long["top"]] == [f"City {i}" for i in range(4)]
    assert long["tail_share"] == 0.05
    short = tuples([60, 30, 5, 5])  # the top 2 leave 10%, not more: 2 kept
    assert len(short["top"]) == 2 and short["tail_share"] == 0.1


def test_places_top_tail_and_unmatched(built):
    places = json.loads(built.read_bytes())["networks"]["alpha"]["places"]
    us = places["US"]  # 9 (hour, IP) memberships: New York x7, Tokyo x1, one unmatched
    assert us["ips"] == 9 and us["unmatched_share"] == 0.111111 and us["tail_share"] == 0.0
    assert us["top"][0] == US | {"asn_name": None, "share": 0.777778}
    assert us["top"][1]["city"] == "Tokyo" and us["top"][1]["share"] == 0.111111
    assert us["top"][1]["asn_name"] == "Tokyo } Org"
    de = places["DE"]  # New York (.4), no enrichment (.10), Osaka (.11, .10), Tokyo (db8::1)
    assert de["ips"] == 5 and de["unmatched_share"] == 0.2
    # Osaka first; New York and Tokyo tie at one IP: the lower tuple (JP) is kept.
    assert [entry["city"] for entry in de["top"]] == ["Osaka", "Tokyo"]
    assert de["tail_share"] == 0.2


def test_browser_targets_from_the_last_state_of_each_run(built):
    browser = json.loads(built.read_bytes())["networks"]["alpha"]["browser"]
    assert set(browser) == {"ai_chatgpt", "amazon_pdp", "google_search", "linkedin_public"}
    assert browser["linkedin_public"][DEFAULT_ENGINE]["runs"] == 1  # from the second source
    chatgpt = browser["ai_chatgpt"][DEFAULT_ENGINE]
    assert chatgpt["runs"] == 0 and chatgpt["tries"]["count"] == 0
    failed = {"runs": 1, "stored": 0, "test_runs": 0, "statuses": {"failed": 1}}
    assert chatgpt["ingest"] == failed
    google = browser["google_search"][DEFAULT_ENGINE]
    assert google["runs"] == 1 and google["tries"]["p50"] == 100
    assert google["outcomes"] == {
        "ok": 40,
        "challenge": 50,
        "block": 0,
        "login_wall": None,
        "timeout": 5,
        "proxy_error": 0,
        "no_content": None,
        "no_answer": None,
        "other_error": None,
        "unsplit": 5,
    }
    assert google["page_load_ms"] == {"p50": None, "p90": None}
    assert google["ingest"] == {"runs": 1, "stored": 1, "test_runs": 1, "statuses": {"done": 1}}
    assert google["bytes_per_load"]["p50"] == 1_500_000
    assert google["bundle_bytes"]["count"] == 1 and google["bundle_bytes"]["p50"] == 1_500_000
    amazon = browser["amazon_pdp"][DEFAULT_ENGINE]
    assert amazon["tries"] == {"count": 2, "p10": 50, "p50": 50, "p90": 100, "mean": 75.0}
    loads = amazon["bytes_per_load"]
    assert (loads["p10"], loads["p50"], loads["p90"]) == (2_000_000, 2_000_000, 2_500_000)
    assert amazon["bundle_bytes"]["p50"] == 2_000_000 and amazon["ok_share"] == 0.933333


def test_gaps_name_what_the_sources_lack(built):
    gaps = json.loads(built.read_bytes())["gaps"]
    beta_hours = [g for g in gaps if g.get("network") == "beta" and "hour" in g]
    assert len(beta_hours) == 23 and beta_hours[0]["what"] == "no IPs (requests_no_ips)"
    alpha_hours = {g["hour"] for g in gaps if g.get("network") == "alpha" and "hour" in g}
    assert len(alpha_hours) == 21 and "05" in alpha_hours
    named = {(g["source"], g.get("network"), g.get("target"), g["what"]) for g in gaps}
    assert ("enrich", "gamma", None, "no network folder") in named
    assert ("enrich", "delta", None, "no formula: not profiled") in named
    assert ("browser", "alpha", "ai_chatgpt", "runs left out (not stored): 1") in named
    assert ("browser", "beta", "google_search", "runs left out (unfinished): 1") in named
    assert ("browser", "beta", None, "no stored browser run") in named
    assert ("browser", None, None, profile.profile_browser.UNSPLIT) in named
    assert "delta" not in json.loads(built.read_bytes())["networks"]


def test_no_input_address_host_or_path_in_the_output(built, tmp_path):
    output = built.read_bytes()
    for text in [*ADDRESSES, *HOSTS, str(tmp_path), "hour-20260101T23Z", "dbip-test"]:
        assert text.encode() not in output, text
    assert b"192.0.2." not in output and b"2001:db8" not in output


def test_a_source_that_does_not_match_its_manifest_fails(tmp_path, capsys):
    root = tmp_path / "enrich"
    enrich_fixture(root)
    manifest = json.loads((root / "alpha" / "manifest.json").read_text())
    manifest["slots"]["00"]["unique_ips"] = 3
    (root / "alpha" / "manifest.json").write_text(json.dumps(manifest))
    formulas = tmp_path / "formulas"
    formulas.mkdir()
    (formulas / "alpha.json").write_text("{}")
    argv = ["build", "--enrich", str(root), "--browser", str(tmp_path), "--out"]
    assert profile.main([*argv, str(tmp_path / "p.json"), "--formulas", str(formulas)]) == 1
    error = "alpha/hour-20260102T00Z.jsonl.gz: 2 IPs / 9 rows, manifest 3 / 9"
    assert error in capsys.readouterr().err
    assert not (tmp_path / "p.json").exists()
    manifest["schema_version"] = 1
    (root / "alpha" / "manifest.json").write_text(json.dumps(manifest))
    assert profile.main([*argv, str(tmp_path / "p.json"), "--formulas", str(formulas)]) == 1
    assert "alpha/manifest.json: manifest schema 1 is not 2" in capsys.readouterr().err


def test_an_enrich_folder_without_a_network_folder_fails(tmp_path, capsys):
    browser_fixture(tmp_path / "hours")
    formulas = tmp_path / "formulas"
    formulas.mkdir()
    (formulas / "alpha.json").write_text("{}")
    (tmp_path / "empty" / "alpha").mkdir(parents=True)  # a folder, but no manifest.json
    out = tmp_path / "p.json"
    for enrich in (tmp_path / "missing", tmp_path / "empty"):
        argv = ["build", "--enrich", str(enrich), "--browser", str(tmp_path / "hours")]
        assert profile.main([*argv, "--out", str(out), "--formulas", str(formulas)]) == 1
        error = capsys.readouterr().err
        assert error == f"profile: {enrich.name}: no network folder with a manifest.json\n"
        assert str(tmp_path) not in error and not out.exists()


def test_run_filters():
    assert profile_reference.run_filter("marker:scale_replay")[1] == {"marker": "scale_replay"}
    assert profile_reference.run_filter("batches:3-5,9")[1] == {"batches": [3, 4, 5, 9]}
    for text in ("batches:x", "marker:", "runs:1"):
        with pytest.raises(profile_hours.ProfileError):
            profile_reference.run_filter(text)


async def test_reference_rows_and_bytes_per_run_in_one_read_only_transaction(
    database, tmp_path, monkeypatch
):
    prepare(database, ("US",))
    keys = []
    for requests in (3, 2):
        directory = await measurement(tmp_path, requests=requests)
        enrichment(directory)
        assert ingest(directory, database)["imported"]
        keys.append(query(database, "SELECT max(run_key) FROM test_runs")[0][0])
    daily, browser = keys
    with connect(database) as conn:
        conn.execute(
            "UPDATE test_runs SET summary=summary||%s::jsonb WHERE run_key=%s",
            (json.dumps({"test_kind": "daily", "scale_replay": 1}), daily),
        )
        batch = conn.execute(
            "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
            "start_at,status) SELECT 'browser','US',region_id,'generic_http',now(),now(),'done' "
            "FROM countries WHERE country_code='US' RETURNING batch_id"
        ).fetchone()[0]
        conn.execute("UPDATE test_runs SET batch_id=%s WHERE run_key=%s", (batch, browser))
        product = conn.execute("SELECT min(product_id) FROM products").fetchone()[0]
        conn.execute(
            "INSERT INTO batch_runs(batch_id,product_id,status,run_key) VALUES (%s,%s,'done',%s)",
            (batch, product, browser),
        )
        partition(conn, "test_events", datetime.now(UTC))
        conn.execute(
            "INSERT INTO test_events(kind,batch_id,product_id) VALUES ('fixture',%s,%s)",
            (batch, product),
        )
        release = json.dumps({"file": "dbip-full-2026-10-05.mmdb"})
        conn.execute("UPDATE dbip_releases SET metadata=metadata||%s::jsonb", (release,))
    real, seen = profile_reference._read, []

    def read_once(conn, text):
        mode = conn.execute(f"SELECT current_setting('{READ_ONLY}'),current_setting('{LEVEL}')")
        mode = mode.fetchone()
        seen.append((mode, conn.info.transaction_status.name))
        return real(conn, text)

    monkeypatch.setattr(profile_reference, "_read", read_once)
    out = tmp_path / "profile.json"
    out.write_text(json.dumps({"profile_version": 1, "networks": {}}))
    base = ["reference", "--database", str(database), "--out", str(out)]
    assert profile.main([*base, "--runs", "marker:scale_replay"]) == 0
    assert profile.main([*base, "--runs", f"batches:{batch}", "--basis", "modelled"]) == 0
    assert seen == [(("on", "repeatable read"), "INTRANS")] * 2
    data = json.loads(out.read_bytes())
    assert stat.S_IMODE(out.stat().st_mode) == 0o600 and data["networks"] == {}
    reference = data["reference"]
    assert set(reference) == {"daily", "browser"}
    assert (reference["daily"]["basis"], reference["browser"]["basis"]) == ("real", "modelled")
    rows = {name: stats["p50"] for name, stats in reference["daily"]["rows_per_run"].items()}
    assert rows == {
        "test_runs": 1,
        "run_buckets": 1,
        "run_geo_buckets": 0,  # the fixture's ingest writes no geography bucket
        "ip_observations": 1,
        "run_enrichments": 1,
        "ip_enrichment": 1,
        "ip_addresses": 1,
        "product_ips": 1,
        "pool_ips": 1,
        "estimated_hours": 0,
        "batch_runs": 0,
        "test_events": 0,
    }
    daily = reference["daily"]
    one = {"per_run": {"1": 1}, "per_ip": {"1": {"1": 1}}}  # 1 bucket, its 1 IP seen in it
    assert daily["buckets"] == one == daily["networks"]["fixture"]["buckets"]
    assert daily["releases"] == ["2026-10-05"] and "buckets" not in reference["browser"]
    found = reference["browser"]["rows_per_run"]
    assert found["batch_runs"]["mean"] == 1 and found["test_events"]["mean"] == 1
    assert reference["browser"]["networks"]["fixture"]["runs"] == 1
    sizes = reference["daily"]["bytes"]
    table = sizes["tables"]["test_runs"]
    assert table["rows"] == 2 and table["table_bytes"] > 0 and table["index_bytes"] > 0
    assert sizes["per_run"]["test_runs"]["table_bytes"] == round(table["table_bytes"] / 2)
    outcomes = reference["browser"]["targets"]["fixture"]["generic_http"][DEFAULT_ENGINE]
    assert outcomes["runs"] == 1 and outcomes["tries"] == 2 and outcomes["ok_share"] == 1.0
    assert outcomes["outcomes"]["ok"] == 2 and outcomes["outcomes"]["other_error"] == 0
    assert outcomes["page_load_ms"] == {"p50": 11, "p90": 11}
    assert b"8.8.8.8" not in out.read_bytes()


def test_reference_without_a_matching_run_fails(database, tmp_path, capsys):
    out = tmp_path / "profile.json"
    argv = ["reference", "--database", str(database), "--runs", "marker:none", "--out", str(out)]
    assert profile.main(argv) == 1
    assert "no daily or browser run" in capsys.readouterr().err and not out.exists()


def test_a_browser_summary_without_a_run_field_fails(tmp_path, capsys):
    browser_fixture(tmp_path / "hours")
    mgr = tmp_path / "hours" / HOUR_FOLDERS[1] / "mgr.json"
    data = json.loads(mgr.read_text())
    del data["runs"][0]["bytes_down"]
    mgr.write_text(json.dumps(data))
    enrich_fixture(tmp_path / "enrich")
    formulas = tmp_path / "formulas"
    formulas.mkdir()
    (formulas / "alpha.json").write_text("{}")
    argv = ["build", "--enrich", str(tmp_path / "enrich"), "--browser", str(tmp_path / "hours")]
    argv.append("--out")
    assert profile.main([*argv, str(tmp_path / "p.json"), "--formulas", str(formulas)]) == 1
    assert f"{HOUR_FOLDERS[1]}/mgr.json: a run lacks bytes_down" in capsys.readouterr().err
    empty = tmp_path / "empty"
    empty.mkdir()
    argv[4] = str(empty)
    assert profile.main([*argv, str(tmp_path / "p.json"), "--formulas", str(formulas)]) == 1
    assert "no hour folder with a mgr.json" in capsys.readouterr().err
    assert profile.main([*argv, str(tmp_path / "p.json"), "--formulas", str(empty)]) == 1
    assert "no formula" in capsys.readouterr().err


def test_reference_refuses_another_profile_version(tmp_path, capsys):
    out = tmp_path / "profile.json"
    out.write_text(json.dumps({"profile_version": 2}))
    argv = ["reference", "--database", str(tmp_path / "db.toml"), "--runs", "marker:x"]
    assert profile.main([*argv, "--out", str(out)]) == 1
    assert "profile_version 2 is not 1" in capsys.readouterr().err
