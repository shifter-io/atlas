"""SCORE-DATA-1: simulate.py --add-scored-inputs adds pulses, load waves and outages on the stored
days of a load (simulate_scored*.py), and `proxy-index daily tape` replays the live tape over them.
The model's cases are pure; the store's run on a scratch database loaded with the real-hours
fixture of reserved addresses (test_scale_real_hours.py)."""

import importlib
import json
import re
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from orchestrator_cases import query

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "scale"))
model = importlib.import_module("simulate_scored")
real = importlib.import_module("test_scale_real_hours")
sim = real.sim

from proxy_index import detect, storage  # noqa: E402
from proxy_index.calculated import calculator  # noqa: E402

pytest_plugins = ["test_storage"]

CODES = tuple(f"{a}{b}" for a in "ABCDE" for b in "QRSTU")  # 25 country codes
HTTP = model.Target("generic_http", 30, 100, 25, True, 1000)
HTTPS = model.Target("generic_https", 30, 100, 25, False, 1000)
NETWORKS = tuple(f"net{i:02d}" for i in range(18))
METHODOLOGY = ROOT / "configs" / "methodology.toml"
REFERENCES = {day: {"r1": "AQ", "r2": "BQ"} for day in (1, 2)}


def model_inputs(**changes) -> model.Inputs:
    """18 networks of 12 to 21 countries over two days; net00 (as byteful) has no day 2."""
    countries = {}
    for i, network in enumerate(NETWORKS):
        for day in (1, 2):
            countries[network, day] = CODES[: 12 + i % 10]
    del countries["net00", 2]
    regions = {code: "r1" if code < "B" else "r2" for code in CODES}
    profiles = {
        n: model.Profile(400 + 10 * i, 900 + 30 * i, 0.97 - 0.01 * i)
        for i, n in enumerate(NETWORKS)
    }
    given = (date(2026, 10, 25), 2, (HTTP, HTTPS), countries, regions, profiles, 8000, 30)
    found = model.Inputs(*given)
    return model.Inputs(**(vars(found) | changes))


def day_runs(given, plan, seed, day) -> list:
    found = (given, plan, seed, REFERENCES[day], day)
    return [run for hour in range(24) for run in model.hour_runs(*found, hour)]


def test_the_plan_and_the_runs_are_a_function_of_the_seed():
    given = model_inputs()
    plan = model.outages(given, "7")
    assert plan == model.outages(given, "7") and plan != model.outages(given, "8")
    runs = day_runs(given, plan, "7", 1)
    again = day_runs(given, model.outages(given, "7"), "7", 1)
    assert runs == again
    ids = [run.run_id for run in runs]
    assert len(set(ids)) == len(ids)
    assert {run.kind for run in runs} == {"pulse", "load"}


def test_each_network_completes_twice_the_minimum_a_target_and_day():
    solo = {("solo", day): ("AQ",) for day in (1, 2)}
    given = model_inputs(countries=solo, profiles={"solo": model.Profile(500, 900, 0.9)})
    counted = model.counts(given, model.outages(given, "7"), "7", REFERENCES)
    for day in (1, 2):
        own = counted[day]["networks"]["solo"]["requests"]
        assert own == {"generic_http": 48 * 167, "generic_https": 48 * 167}
        assert counted[day]["rows"]["test_runs"] == 2 * 48 + 48  # pulses, then the load waves


def test_load_runs_have_their_minimum_successes_and_real_ips_only():
    weak = {n: model.Profile(500, 900, 0.01) for n in NETWORKS}
    given = model_inputs(profiles=weak)
    loads = [r for r in day_runs(given, [], "7", 1) if r.kind == "load"]
    assert loads and min(r.successes for r in loads) == 30
    assert {r.country for r in loads} == {"AQ", "BQ"}
    ips = [f"192.0.2.{i}" for i in range(50)]
    for successes in (0, 7, 500):
        drawn = model.draw(ips, "run", successes)
        assert {ip for ip, _ in drawn} <= set(ips) and sum(n for _, n in drawn) == successes
        assert len(drawn) <= min(successes, len(ips)) and drawn == model.draw(ips, "run", successes)
    assert model.draw([], "run", 9) == []


def test_byteful_without_a_day_two_has_no_run_on_it():
    given = model_inputs()
    plan = model.outages(given, "7")
    assert all(r.network != "net00" for r in day_runs(given, plan, "7", 2))
    assert "net00" in {r.network for r in day_runs(given, plan, "7", 1)}
    assert all(o.day == 1 for o in plan if o.network == "net00")
    assert model.uptime(given, plan, "net00", 24) is None


def test_the_outage_plan_spreads_over_the_networks():
    given = model_inputs()
    plan = model.outages(given, "7")
    tiers = model.tiers(given, "7")
    assert 1 <= sum(t == "long" for t in tiers.values()) <= 2
    assert 3 <= sum(t == "major" for t in tiers.values()) <= 4
    worst = {1: 0, 2: 0}
    for network in NETWORKS:  # an outage on each pulse day, the tier's on the worst one
        own = sorted((o for o in plan if o.network == network), key=lambda o: o.day)
        assert [o.day for o in own] == given.pulse_days(network)
        worst[max(own, key=lambda o: len(o.countries) * (o.slots or 99)).day] += 1
        assert sum(o.tier != "calm" for o in own) == 1 and own[0].tier in ("calm", tiers[network])
    assert min(worst.values()) >= 2  # ranks move between the two days' builds
    factors = [[model.load_factor("7", n, day) for day in (1, 2)] for n in NETWORKS]
    assert all(1.1 <= f <= 1.8 for pair in factors for f in pair)
    assert all(abs(a - b) <= 0.4 for a, b in factors) and sum(a != b for a, b in factors) > 9
    for outage in plan:
        start, end = outage.span(given)
        minutes = (end - start).total_seconds() / 60 + 30  # from the last healthy pulse
        if outage.slots is None:
            assert outage.day == 2 and end == given.end and outage.tier != "short"
        elif outage.tier in ("short", "calm"):
            assert 30 <= minutes <= 150
        elif outage.tier == "long":
            assert minutes >= 360 or len(outage.countries) >= 2
    assert [o.slots for o in plan].count(None) == 1
    assert model.collision(given, plan) is None  # no two networks with the same uptime
    day = [model.uptime(given, plan, n, 24) for n in NETWORKS if n != "net00"]
    assert sum(99.0 <= u <= 99.95 for u in day) > len(day) / 2
    assert all(u < 100 for u in day)


def test_the_switches_go_together(tmp_path, capsys):
    base = ["--database", "db.toml", "--profile", "p.json", "--days", "2", "--first-day", "x"]
    for extra, message in (
        (["--plan-only", "--pulse-days", "byteful=1,2"], "goes with --add-scored-inputs"),
        (["--add-scored-inputs", "--plan-only"], "needs --out"),
        (["--add-scored-inputs", "--pulse-days", "byteful=0", "--out", "o"], "--pulse-days"),
    ):
        with pytest.raises(SystemExit):
            sim.simulate.arguments([*base, *extra])
        assert message in capsys.readouterr().err


# The store, on a scratch database.

OWN = "SELECT count(*) FROM test_runs WHERE summary ? 'score_inputs'"
TABLES = ("test_batches", "batch_runs", "test_runs", "run_buckets", "ip_observations")
FIGURES = (  # the daily figures the additive mode must not change
    "SELECT (SELECT count(DISTINCT ip_id) FROM ip_observations),(SELECT count(*) FROM product_ips),"
    "(SELECT count(*) FROM ip_addresses),(SELECT coalesce(sum(shared_ips),0) FROM provider_overlap)"
)
LOAD_IPS = (  # per load run's network, country and day, its IPs
    "SELECT p.provider,trim(b.country_code),b.scheduled_for::date,host(a.ip) FROM test_batches b "
    "JOIN test_runs t USING(batch_id) JOIN scope_stats s USING(scope_id) "
    "JOIN products p USING(product_id) JOIN ip_observations i ON i.run_key=t.run_key "
    "JOIN ip_addresses a ON a.ip_id=i.ip_id WHERE b.kind='load'"
)

BROWSER_BATCHES = "SELECT count(*) FROM test_batches WHERE kind='browser'"
BROWSER_RUNS = (
    "SELECT count(*) FROM batch_runs JOIN test_batches b USING(batch_id) WHERE b.kind='browser'"
)
GAMMA = (
    "SELECT count(*) FROM test_runs t JOIN test_batches b USING(batch_id) "
    "JOIN scope_stats s USING(scope_id) JOIN products p USING(product_id) "
    "WHERE p.provider='gamma' AND b.kind<>'browser' AND b.scheduled_for<%s"
)


def counts(database) -> dict:
    return {name: query(database, f"SELECT count(*) FROM {name}")[0][0] for name in TABLES}


def test_scored_inputs_are_added_once_and_make_every_score_input(
    staging, inputs, source, tmp_path, capsys
):
    with storage.connect(staging) as conn:  # a pulse an hour: fewer runs
        conn.execute("UPDATE targets SET pulse_interval_minutes=60 WHERE pulse_enabled")
    out = tmp_path / "out"
    load = real.argv(staging, inputs, out, source)
    assert sim.simulate.main(load) == 0
    capsys.readouterr()
    before = query(staging, FIGURES)[0]
    assert sim.simulate.main([*load, "--add-scored-inputs", "--plan-only"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["pulse_days"]["gamma"] == {"2": 2} and "gamma" not in plan["days"]["1"]["networks"]
    assert [o["slots"] for o in plan["outages"]].count(None) == 1
    assert not re.search(r"192\.0\.2|2001:db8", json.dumps(plan))
    planned = [*load, "--add-scored-inputs", "--plan-only", "--pulse-days"]
    assert sim.simulate.main([*planned, "gamma=1,2"]) == 0  # day 1 with day 2's countries
    assert json.loads(capsys.readouterr().out)["pulse_days"]["gamma"] == {"1": 2, "2": 2}
    for wrong, message in (("nobody=1", "no daily test of"), ("gamma=3", "past the load's")):
        assert sim.simulate.main([*planned, wrong]) == 2 and message in capsys.readouterr().err
    assert counts(staging)["test_batches"] == query(staging, BROWSER_BATCHES)[0][0]
    assert sim.simulate.main([*load, "--add-scored-inputs"]) == 0
    assert json.loads(capsys.readouterr().out)["hours_done"] == 48
    stored, mine = counts(staging), query(staging, OWN)[0][0]
    assert mine == stored["batch_runs"] - query(staging, BROWSER_RUNS)[0][0]
    assert query(staging, FIGURES)[0] == before  # the daily figures stay
    expected = real.expected()
    for network, country, day, ip in query(staging, LOAD_IPS):
        index = (day - sim.FIRST).days + 1
        assert ip in expected[network, country, index]
    statuses = "SELECT DISTINCT status FROM test_batches WHERE kind IN ('pulse','load')"
    assert query(staging, statuses) == [("done",)]
    assert query(staging, GAMMA, (day_start(2),))[0][0] == 0  # no daily test of day 1
    # Again: nothing new; an hour written again whole stores nothing new either.
    assert sim.simulate.main([*load, "--add-scored-inputs"]) == 0
    state = sim.state(out)
    state["score_inputs"]["hours"] = state["score_inputs"]["hours"][:-3]
    (out / "state.json").write_text(json.dumps(state))
    assert sim.simulate.main([*load, "--add-scored-inputs"]) == 0
    assert counts(staging) == stored and query(staging, OWN)[0][0] == mine
    capsys.readouterr()
    other = [*load, "--add-scored-inputs", "--pulse-days", "gamma=1,2"]
    assert sim.simulate.main(other) == 2 and "--pulse-days {}" in capsys.readouterr().err
    tape(staging)
    scores(staging)


def tape(database) -> None:
    """detect() over the two days in time order (SCORE-DATA-1b makes it a command): down,
    recovered and pulse events, and one outage still open."""
    with storage.connect(database) as conn:
        conn.autocommit = True
        moment = day_start(1)
        while moment < day_start(3) + timedelta(minutes=5):
            moment += timedelta(minutes=15)
            with conn.transaction():
                detect.detect(conn, moment)
    kinds = {row[0] for row in query(database, "SELECT DISTINCT kind FROM test_events")}
    assert {"down", "recovered"} <= kinds
    assert query(database, "SELECT count(*) FROM outages WHERE ended_at IS NULL")[0][0] >= 1


EVIDENCE = (  # the price issue's row (#839): one per network, cohort "*"
    "INSERT INTO metric_evidence(product_id,cohort,metric,value,period_start,period_end,"
    "valid_until,source,content_hash) SELECT product_id,'*','price_per_gb_lowest',"
    "3+product_id,%s,%s,%s,'fixture',md5(product_id::text) FROM products "
    "WHERE provider IN ('alpha','beta','gamma')"
)


def scores(database) -> None:
    """Builds at the end of day 1 and of day 2: every component ready, a balanced score with a
    rank in every window of day 2, and a rank that moves between the two builds."""
    with storage.connect(database) as conn:
        conn.execute(EVIDENCE, (day_start(-29), day_start(1), day_start(5)))
    ranks = {}
    for day in (1, 2):
        payload = calculator.calculate(database, METHODOLOGY, as_of=day_start(day + 1))
        for entry in payload["networks"]:
            for label, window in entry["windows"].items():
                balanced = window["scores"]["balanced"]
                ranks[day, entry["provider"], label] = balanced["rank"]
                parts = balanced["components"]
                own = " ".join(f"{k}={v['state']}:{v['value']}" for k, v in parts.items())
                head = f"COMPONENTS day {day} {entry['provider']} {label} {balanced['state']}"
                tail = f"rank={balanced['rank']} uptime={window['uptime']['value']}"
                print(head, f"score={balanced['score']}", own, tail)
                if day == 2:
                    assert balanced["state"] == "ready" and balanced["rank"] is not None
                    assert all(v["state"] == "ready" for v in parts.values())
                    assert window["uptime"]["value"] < 1  # every network has an outage
    before = {key[1:]: rank for key, rank in ranks.items() if key[0] == 1}
    moved = [key for key in ranks if key[0] == 2 and before.get(key[1:]) not in (None, ranks[key])]
    assert moved, ranks


def day_start(day: int) -> datetime:
    return datetime.combine(sim.FIRST + timedelta(days=day - 1), datetime.min.time(), UTC)


@pytest.fixture
def staging(database, monkeypatch):
    """Staging, with a DB-IP usage type on the fixture's IPs (dcAsns needs one)."""
    given = real.enrichment
    monkeypatch.setattr(real, "enrichment", lambda place: given(place | {"usage_type": "isp"}))
    return sim.staged(database, monkeypatch)


inputs, source = real.inputs, real.source
