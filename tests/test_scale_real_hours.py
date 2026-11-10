"""REAL-2DAY-1: simulate.py --real-hours stores two days whose daily tests carry real hours'
records, and simulate_check.py real-overlap counts their overlap. The hour files are a fixture of
reserved addresses only (192.0.2.0/24, 2001:db8::/32): three networks sharing IPs in pairs, one
of them without day 1's hour, a country no formula schedules and a folder without a formula."""

import gzip
import hashlib
import importlib
import ipaddress
import json
import pickle
import re
import stat
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from orchestrator_cases import query

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "scale"))
simulate_real = importlib.import_module("simulate_real")
simulate_steps = importlib.import_module("simulate_steps")
enrichment = importlib.import_module("simulate_places").enrichment
cases = importlib.import_module("test_scale_simulate_model")
sim = importlib.import_module("test_scale_simulate")

pytest_plugins = ["test_storage"]

NETWORKS = ("alpha", "beta", "gamma")
SCHEDULE = {"US": 0, "DE": 1}  # formula hours: two hours a simulated day load every test
HOURS = (12, 0)  # the real hour of day 1, of day 2
RELEASE = "ab" * 32
STORED = (  # per daily test and IP: its network, country, start and request rows
    "SELECT p.provider,upper(o.requested_country),t.summary->>'started_at',host(a.ip),"
    "sum(i.count) FROM test_runs t JOIN scope_stats s USING(scope_id) "
    "JOIN products p USING(product_id) JOIN pool_stats o USING(pool_id) "
    "JOIN ip_observations i USING(run_key) JOIN ip_addresses a USING(ip_id) "
    "WHERE t.summary->>'test_kind'='daily' GROUP BY 1,2,3,4"
)
ENRICHED = (
    "SELECT count(DISTINCT e.ip_id) FROM ip_enrichment e JOIN dbip_releases d USING(release_id) "
    "WHERE d.sha256=%s"
)


def records() -> dict:
    """(network, real hour) -> its (IP, request rows per country): four own IPs per country,
    the shared ones, an IP of both countries and one of JP (no formula schedules it)."""
    found = {}
    for n, network in enumerate(NETWORKS):
        for h, hour in enumerate(HOURS):
            own = []
            for c, code in enumerate(SCHEDULE):
                own += [(f"192.0.2.{60 * n + 30 * h + 10 * c + i}", {code: 1 + i}) for i in (0, 1)]
                own += [(f"2001:db8:{n}:{h}:{c}::{i}", {code: 2 + i}) for i in (0, 1)]
            found[network, hour] = own
    del found["gamma", 12]  # gamma has no hour 12: no test on day 1
    found["alpha", 12] += [("192.0.2.200", {"US": 3}), ("2001:db8:ab::1", {"US": 1})]
    found["alpha", 12] += [("192.0.2.202", {"DE": 2})]
    found["beta", 12] += [("192.0.2.200", {"US": 1}), ("2001:db8:ab::1", {"US": 2})]
    found["beta", 12] += [("2001:db8:ff::7", {"JP": 4})]
    found["beta", 0] += [("192.0.2.201", {"DE": 5})]
    found["gamma", 0] += [("192.0.2.201", {"DE": 1}), ("192.0.2.202", {"US": 2})]
    found["alpha", 0] += [("192.0.2.210", {"US": 2, "DE": 3})]
    return found


def expected() -> dict:
    """(network, country, day) -> {IP: request rows} the daily tests must hold."""
    found = {}
    for (network, hour), rows in records().items():
        for ip, countries in rows:
            for code, count in countries.items():
                if code in SCHEDULE:
                    key = (network, code, HOURS.index(hour) + 1)
                    found.setdefault(key, {})[str(ipaddress.ip_address(ip))] = count
    return found


def hour_file(folder: Path, hour: int, rows: list) -> dict:
    """The hour file as LC-ENRICH-1 writes it, and its manifest slot."""
    lines = []
    for ip, countries in rows:
        place = {"country_code": "US", "city": "City 1", "asn": 64500}  # one IP, one place
        record = {"ip": ip, "seen_count": sum(countries.values()), "regions": {"r1": 1}}
        record |= {"countries": countries, "enrichment": enrichment(place | {"asn_name": "Org"})}
        lines.append(json.dumps(record | {"dbip_release": RELEASE}, separators=(",", ":")))
    data = gzip.compress(("\n".join(lines) + "\n").encode(), mtime=0)
    name = f"hour-20261025T{hour:02d}Z.jsonl.gz"
    (folder / name).write_bytes(data)
    slot = {"status": "present", "file": name, "sha256": hashlib.sha256(data).hexdigest()}
    rows_total = sum(sum(countries.values()) for _, countries in rows)
    return slot | {"unique_ips": len(rows), "request_rows": rows_total, "requests": rows_total}


@pytest.fixture
def source(tmp_path) -> Path:
    root = tmp_path / "lc-enrich"
    for network in (*NETWORKS, "delta"):  # delta: a folder without a formula
        folder = root / network
        folder.mkdir(parents=True)
        slots = {"12": {"status": "missing"}} if network == "gamma" else {}
        for hour in HOURS:
            if (network, hour) in records():
                slots[f"{hour:02d}"] = hour_file(folder, hour, records()[network, hour])
        manifest = {"schema_version": 2, "network": network, "product": "residential"}
        manifest["dbip_release"] = {"sha256": RELEASE, "file": "dbip-full-2026-10.mmdb"}
        (folder / "manifest.json").write_text(json.dumps(manifest | {"slots": slots}))
    return root


def formulas() -> dict:
    return {name: cases.formula(name) | {"schedule": dict(SCHEDULE)} for name in NETWORKS}


def profile() -> dict:
    given = cases.profile()
    given["networks"]["gamma"] = given["networks"]["alpha"]
    return given


def real_plan(tmp_path, source: Path):
    plan = cases.plan(tmp_path)
    real = simulate_real.Source.read(source, HOURS, formulas())
    return replace(plan, formulas=formulas(), days=3, real=real)


def daily_of(model) -> dict:
    """(network, country, day) -> {IP: request rows} of the model's daily tests."""
    found = {}
    for day in (1, 2, 3):
        for hour in range(24):
            for test in model.hour(day, hour).daily:
                assert test.started_at.hour == SCHEDULE[test.lane.country] == hour
                assert test.started_at.date() == cases.START + timedelta(days=day - 1)
                own = {str(ipaddress.ip_address(row["ip"])): n for _, _, n, row in test.ips()}
                found[test.lane.network, test.lane.country, day] = own
    return found


def test_hours_name_one_real_hour_a_day():
    assert simulate_real.hours("12,0") == (12, 0)
    for wrong in ("24", "12,-1", "noon"):
        with pytest.raises(ValueError):
            simulate_real.hours(wrong)


def test_a_days_tests_hold_the_real_records_of_its_hour(tmp_path, source):
    plan = real_plan(tmp_path, source)
    model = simulate_real.model(profile(), plan, 7)
    assert isinstance(model, simulate_real.Model) and not model.pools
    assert daily_of(model) == expected()  # gamma none on day 1, none on the extra day
    assert plan.real.notes == {
        "hours": [12, 0],
        "missing": {"1": ["gamma"], "2": []},
        "unscheduled": ["beta/JP"],
        "ignored": ["delta"],
    }
    hour = model.hour(2, SCHEDULE["DE"])
    assert {(cell.network, cell.countries) for cell in hour.cells} == {
        (name, ("DE",)) for name in NETWORKS
    }
    assert type(simulate_real.model(profile(), cases.plan(tmp_path), 7)) is cases.model.Model


def test_a_written_test_is_its_records_spread_over_buckets(tmp_path, source):
    model = simulate_real.model(profile(), real_plan(tmp_path, source), 7)
    test = next(t for t in model.hour(2, SCHEDULE["US"]).daily if t.lane.network == "alpha")
    (tmp_path / "run").mkdir()
    test.write(tmp_path / "run")
    with gzip.open(tmp_path / "run" / f"enriched-{RELEASE}.jsonl.gz", "rt") as lines:
        rows = [json.loads(line) for line in lines]
    found = {}
    for row in rows:
        ip = str(ipaddress.ip_address(row["ip"]))
        found[ip] = found.get(ip, 0) + row["count"]
        assert row["dbip_release"] == RELEASE and row["enrichment"]["asn"] == 64500
    assert found == expected()["alpha", "US", 2] and len(rows) >= len(found)
    report = json.loads((tmp_path / "run" / f"enrichment-{RELEASE}.json").read_text())
    assert report["release"] == {"sha256": RELEASE, "file": "dbip-full-2026-10.mmdb"}
    summary = json.loads((tmp_path / "run" / "summary.json").read_text())
    assert summary["run_id"] == test.run_id and summary["unique_ips"] == len(found) == 5


def test_a_test_with_two_releases_is_refused(tmp_path, source):
    model = simulate_real.model(profile(), real_plan(tmp_path, source), 7)
    test = next(t for t in model.hour(1, SCHEDULE["US"]).daily if t.lane.network == "beta")
    model.records(test.lane, 1)[0]["dbip_release"] = "cd" * 32
    with pytest.raises(ValueError, match="2 DB-IP releases"):
        test.write(tmp_path / "run")


def test_a_source_that_differs_from_its_manifest_is_refused(tmp_path, source):
    with pytest.raises(ValueError, match="no network folder of the formulas"):
        simulate_real.Source.read(tmp_path / "mistyped", HOURS, formulas())
    path = next((source / "alpha").glob("hour-*T12Z.jsonl.gz"))
    path.write_bytes(path.read_bytes() + b"\0")
    with pytest.raises(ValueError, match="sha256"):
        simulate_real.Source.read(source, HOURS, formulas())


def test_the_workers_write_the_real_tests(tmp_path, source):
    shared = simulate_steps.SettingsFile.of(sim.PRODUCTION)
    plan = replace(real_plan(tmp_path, source), browser=shared, regions=None)
    simulate_steps.start(profile(), pickle.loads(pickle.dumps(plan)), 7, "run")
    assert isinstance(simulate_steps.WORKER["model"], simulate_real.Model)
    found = simulate_steps.generate(1, SCHEDULE["US"], 0, 1, str(tmp_path / "hour"))
    assert len(found["figures"]) == 2  # alpha and beta: gamma has no hour 12
    ips = {figure["tables"]["ip_addresses"] for figure in found["figures"].values()}
    assert ips == {len(expected()[name, "US", 1]) for name in ("alpha", "beta")}


def direct() -> dict:
    """Per pair (7d and 30d, 24h): the distinct IPs both networks' tests had, counted plainly."""
    seen = {name: [set(), set()] for name in NETWORKS}
    for (network, _, day), ips in expected().items():
        seen[network][day - 1] |= set(ips)
    found = {}
    for one, other in (("alpha", "beta"), ("alpha", "gamma"), ("beta", "gamma")):
        both = (seen[one][0] | seen[one][1]) & (seen[other][0] | seen[other][1])
        found[one, other] = (len(both), len(both), len(seen[one][1] & seen[other][1]))
    return found


def union() -> dict:
    """Per range: the distinct IPs of every network's tests together, counted plainly."""
    days = [set(), set()]
    for (_, _, day), ips in expected().items():
        days[day - 1] |= set(ips)
    return {"24h": len(days[1]), "7d": len(days[0] | days[1]), "30d": len(days[0] | days[1])}


def test_the_overlap_reference_counts_the_shared_ips_and_names_none(tmp_path, source):
    folder = tmp_path / "formulas"
    folder.mkdir()
    for name, formula in formulas().items():
        (folder / f"{name}.json").write_text(json.dumps(formula))
    out = tmp_path / "overlap.json"
    argv = ["real-overlap", "--source", str(source), "--hours", "12,0", "--out", str(out)]
    assert simulate_real.main([*argv, "--formulas", str(folder)]) == 0
    found = json.loads(out.read_text())
    pairs = {tuple(p["networks"]): (p["7d"], p["30d"], p["24h"]) for p in found["pairs"]}
    assert pairs == direct()
    assert pairs == {("alpha", "beta"): (2, 2, 0), ("alpha", "gamma"): (1, 1, 0)} | {
        ("beta", "gamma"): (1, 1, 1)
    }
    assert found["networks"]["gamma"] == {"product": "residential", "24h": 10, "7d": 10, "30d": 10}
    assert found["union"] == {"residential": union()}
    assert found["missing"] == {"1": ["gamma"], "2": []} and found["ignored"] == ["delta"]
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert not re.search(r"192\.0\.2|2001:db8", out.read_text())
    (source / "beta" / "manifest.json").write_text("{")
    assert simulate_real.main([*argv, "--formulas", str(folder)]) == 2


@pytest.fixture
def inputs(tmp_path):
    folder = tmp_path / "formulas"
    folder.mkdir()
    for name, formula in formulas().items():
        (folder / f"{name}.json").write_text(json.dumps(formula))
    settings = tmp_path / "browser.toml"
    settings.write_text(cases.SETTINGS)
    given = profile()
    week = cases.week(given, cases.plan(tmp_path), days=(1,))
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(cases.with_reference(given, week)))
    return {"formulas": folder, "browser-settings": settings, "profile": path}


def argv(database, inputs, out, source, *extra):
    real = ("--hours-per-day", "2", "--real-hours", "12,0", "--source", str(source))
    return sim.argv(database, inputs, out, 2, *real, *extra)


def stored(database) -> dict:
    """(network, country, day) -> {IP: request rows} of the stored daily tests."""
    found = {}
    for network, code, at, ip, rows in query(database, STORED):
        day = (sim.day_of(at) - sim.FIRST).days + 1
        found.setdefault((network, code, day), {})[ip] = rows
    return found


def digests(root: Path) -> dict:
    return {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob("*.*")}


def test_two_real_days_store_the_real_records(staging, inputs, source, tmp_path, capsys):
    before, out = digests(source), tmp_path / "out"
    assert sim.simulate.main(argv(staging, inputs, out, source)) == 0
    assert json.loads(capsys.readouterr().out)["hours_done"] == 5
    assert stored(staging) == expected()
    distinct = {ip for ips in expected().values() for ip in ips}
    assert query(staging, ENRICHED, (RELEASE,))[0][0] == len(distinct)
    found = sim.state(out)
    mine = "SELECT count(*) FILTER (WHERE summary->>'scale_replay'=%s),count(*) FROM test_runs"
    assert len(set(query(staging, mine, (found["replay_id"],))[0])) == 1
    assert found["real_hours"] == [12, 0] and found["real"]["missing"]["1"] == ["gamma"]
    report = json.loads((out / "report.json").read_text())
    assert [day["runs"]["daily"] for day in report["days"]] == [4, 6]
    assert report["checks"]["stats"]["skipped"] == "real hours"
    text = (out / "report.md").read_text()
    assert "## Real hours" in text and "networks without it: gamma" in text
    assert not re.search(r"192\.0\.2|2001:db8", text + (out / "report.json").read_text())
    assert digests(source) == before  # the source is read where it is, never written
    recheck = ["check", "--out", str(out), "--database", str(staging)]
    recheck += [f"--{key}={value}" for key, value in inputs.items()]
    assert sim.simulate.main(recheck) == 2 and "--real-hours load" in capsys.readouterr().err


def test_a_stopped_real_load_resumes_to_the_same_records(
    staging, inputs, source, tmp_path, monkeypatch, capsys
):
    real, stops = simulate_steps.SimSteps.release, []

    def stop_once(self, day, start, step):
        if day == 1 and not stops:  # day 2's first hour, its daily tests stored
            stops.append(start)
            raise KeyboardInterrupt
        return real(self, day, start, step)

    monkeypatch.setattr(simulate_steps.SimSteps, "release", stop_once)
    out = tmp_path / "out"
    assert sim.simulate.main(argv(staging, inputs, out, source)) == 130
    assert [h["ingest"]["done"] for h in sim.state(out)["hours"][:3]] == [True, True, False]
    other = sim.argv(staging, inputs, out, 2, "--hours-per-day", "2")  # no real hours
    assert sim.simulate.main(other) == 2 and "is the load of" in capsys.readouterr().err
    assert sim.simulate.main(argv(staging, inputs, out, source)) == 0
    assert stored(staging) == expected()
    runs = "SELECT count(*) FROM test_runs WHERE summary->>'test_kind'='daily'"
    assert query(staging, runs)[0][0] == len(expected())


def test_the_plan_names_the_missing_hours_and_refusals_write_nothing(
    staging, inputs, source, tmp_path, monkeypatch, capsys
):
    out = tmp_path / "out"
    assert sim.simulate.main(argv(staging, inputs, out, source, "--plan-only")) == 0
    text = capsys.readouterr().out
    assert "day 1: daily 4, browser" in text and "day 2: daily 6, browser" in text
    assert re.search(r"ip_observations\s+\d+\s+\d+\.\d{3}\s+\d+\.\d{3}", text)
    assert "day 1: real hour 12, networks without it: gamma" in text
    assert "not in a formula's schedule: beta/JP" in text and "without a formula: delta" in text
    for wrong, message in (
        (["--real-hours", "12", "--source", str(source)], "one hour for each of the --days"),
        (["--source", str(source)], "go together"),
    ):
        with pytest.raises(SystemExit):
            sim.simulate.main([*sim.argv(staging, inputs, out, 2), *wrong])
        assert message in capsys.readouterr().err
    path = next((source / "beta").glob("hour-*T00Z.jsonl.gz"))
    path.write_bytes(path.read_bytes() + b"\0")
    assert sim.simulate.main(argv(staging, inputs, out, source)) == 2
    assert "--source: " in capsys.readouterr().err
    monkeypatch.setattr(sim.target, "PRODUCTION", sim.target.STAGING)
    assert sim.simulate.main(argv(staging, inputs, out, source)) == 2
    assert "production's" in capsys.readouterr().err
    assert not (out / "state.json").exists()
    assert query(staging, "SELECT count(*) FROM test_runs")[0][0] == 0


@pytest.fixture
def staging(database, monkeypatch):
    return sim.staged(database, monkeypatch)
