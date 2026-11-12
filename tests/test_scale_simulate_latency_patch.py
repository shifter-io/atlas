"""LATENCY-TRUTH-1 (#873): the generator takes each network's real median (--latency-medians),
the load factor is one value for every network, and simulate_latency_patch.py rescales a stored
load's latency to the real medians. Its scaling is pure; the patch runs on a scratch database
loaded with the real-hours fixture of reserved addresses, its score inputs and #870's geo rows."""

import importlib
import json
import math
import sys
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime
from itertools import accumulate
from pathlib import Path

import pytest
from orchestrator_cases import query

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "scale"))
patch = importlib.import_module("simulate_latency_patch")
scored_model = importlib.import_module("simulate_scored")
scored_store = importlib.import_module("simulate_scored_store")
NEW_FACTOR = scored_model.load_factor  # the test below generates its inputs with the old one
simulate_real = importlib.import_module("simulate_real")
synthetic = importlib.import_module("synthetic")
models = importlib.import_module("test_scale_simulate_model")
places = importlib.import_module("test_scale_simulate_place_buckets")
real, scored, sim = places.real, places.scored, places.sim

from proxy_index import partitions, production_guard, storage  # noqa: E402
from proxy_index.calculated import incremental, lists  # noqa: E402
from proxy_index.stats import Histogram  # noqa: E402

pytest_plugins = ["test_storage"]

source = places.source
inputs = real.inputs


def test_the_load_factor_is_one_value_moved_a_little_per_network_and_day():
    found = [scored_model.load_factor("7", f"n{i}", day) for i in range(40) for day in (1, 2)]
    assert all(1.35 <= f <= 1.45 for f in found) and len(set(found)) == len(found)
    old = [patch.previous_load_factor("7", f"n{i}", day) for i in range(40) for day in (1, 2)]
    assert all(1.1 <= f <= 1.8 for f in old) and max(old) - min(old) > 0.4


def test_each_run_scales_by_s_and_a_load_run_also_by_its_new_load_factor():
    span = (datetime(2026, 11, 8, tzinfo=UTC), datetime(2026, 11, 10, tzinfo=UTC))
    load, at = patch.Load("r", "7", date(2026, 11, 8), *span), datetime(2026, 11, 9, 6, tzinfo=UTC)
    kinds = ("daily", "pulse", "load")
    runs = [patch.Run(k, "alpha", kind, "generic_http", at, False) for k, kind in enumerate(kinds)]
    runs[2] = replace(runs[2], version="score-inputs-1")  # the old rule's load run
    runs.append(patch.Run(3, "alpha", "pulse", "generic_https", at, False))
    runs.append(patch.Run(4, "alpha", "load", "generic_http", at, False, "score-inputs-2"))
    found = patch.scales(load, runs, 1.5)
    new = scored_model.load_factor("7", "alpha", 2)
    old = patch.previous_load_factor("7", "alpha", 2)
    h = patch.https_overhead("alpha")
    assert found == {
        0: (1.5, None),
        1: (1.5, None),
        2: (1.5 * new / old, None),
        3: (1.5 * h, h),
        4: (1.5, None),  # made with the new factor already
    }
    overheads = [patch.https_overhead(f"n{i}") for i in range(40)]  # #874: 10 to 40 %
    assert all(1.1 <= h <= 1.4 for h in overheads) and max(overheads) - min(overheads) > 0.2
    u = (synthetic.signed_unit("alpha:https") + 1) / 2
    assert h == 1.1 + 0.3 * u
    assert load.day(at) == 2 and load.bounds()["end"] == datetime(2026, 11, 11, tzinfo=UTC)


def test_a_pulse_events_latency_figures_scale_and_its_rates_stay():
    spike = {"p50": 2379, "value": 2379, "baseline": 954, "success_rate": 0.89, "requests": 100}
    assert patch.scaled_event("latency_spike", spike, 0.5) == spike | {
        "p50": 1190,
        "value": 1190,
        "baseline": 477,
    }
    down = {"p50": None, "baseline": 0.97, "value": 0.0}  # down: success rates, no latency
    assert patch.scaled_event("down", down, 2.0) == down
    done = {"p50": 400, "value": 0.9, "previous": {"p50": 401, "blocks": 0}}
    assert patch.scaled_event("pulse_finished", done, 1.25) == done | {
        "p50": 500,
        "previous": {"p50": 502, "blocks": 0},
    }
    assert patch.scaled_ms(True, 2.0) is True and patch.scaled_ms(1000, 0.3) == 300


def test_the_medians_file_names_known_networks_with_positive_medians(tmp_path):
    path = tmp_path / "medians.json"
    path.write_text(json.dumps({"alpha": 653, "beta": 430.5}))
    assert simulate_real.read_medians(path, ("alpha", "beta", "gamma")) == {
        "alpha": 653.0,
        "beta": 430.5,
    }
    for given, wrong in (({"delta": 1}, "delta"), ({"alpha": 0}, "alpha"), ([1], "object")):
        path.write_text(json.dumps(given))
        with pytest.raises(ValueError, match=wrong):
            simulate_real.read_medians(path, ("alpha", "beta"))


def test_a_real_median_replaces_the_hashed_one_every_day(tmp_path):
    built, given = models.plan(tmp_path), models.profile()
    hashed = models.model.Model(given, built, 7)
    found = models.model.Model(given, replace(built, medians={"alpha": 800.0}), 7)
    assert found.bins("alpha", None, 1) == found.bins("alpha", None, 2)  # no day jitter
    assert found.bins("alpha", None, 2) == [math.ceil(800 * f) for f in synthetic.LATENCY]
    for country in models.SCHEDULE:  # the country factor stays on top (#868)
        factor = found.factors["alpha", country]
        assert found.bins("alpha", country, 1) == [
            math.ceil(800 * factor * f) for f in synthetic.LATENCY
        ]
    assert found.bins("beta", "US", 2) == hashed.bins("beta", "US", 2)  # not named: hashed
    assert hashed.bins("alpha", None, 1) != hashed.bins("alpha", None, 2)


def test_a_runs_latency_model_changes_its_fingerprint_and_list_signature():
    run = {"run_key": 1, "summary": {"artifacts": {"geo": {"model": "place-v1"}}}, "batch_id": None}
    assert lists.patches(run) == [{"model": "place-v1"}]  # as before #873
    assert lists.patches({"summary": None}) == [] and lists.latency_model(run) is None
    marked = run | {"summary": run["summary"] | {"latency_model": "real-median-v1"}}
    assert lists.patches(marked) == [{"model": "place-v1"}, {"latency_model": "real-median-v1"}]
    assert incremental._hashed(run) == {"run_key": 1, "batch_id": None} | {
        "geo": {"model": "place-v1"}
    }
    assert incremental._hashed(marked)["latency_model"] == "real-median-v1"


# The patch, on a scratch database.

BUCKETS = (
    "SELECT b.run_key,b.bucket_start,b.completed,b.successes,b.histogram_steps,"
    "b.histogram_counts FROM run_buckets b ORDER BY 1,2"
)
GEO = (
    "SELECT g.run_key,g.bucket_start,g.location_id,g.network_id,g.completed,g.successes,"
    "g.histogram_steps,g.histogram_counts FROM run_geo_buckets g ORDER BY 1,2,3,4"
)
RUNS = (
    "SELECT r.run_key,p.provider,CASE WHEN r.batch_id IS NULL AND r.summary->>'test_kind'="
    "'daily' THEN 'daily' ELSE coalesce(b.kind,'other') END,s.target,"
    "coalesce(b.scheduled_for,r.started_at),r.summary->>'score_inputs',r.summary "
    "FROM test_runs r JOIN scope_stats s "
    "USING(scope_id) JOIN products p ON p.product_id=s.product_id "
    "LEFT JOIN test_batches b ON b.batch_id=r.batch_id ORDER BY 1"
)
EVENTS = "SELECT event_id,kind,data FROM test_events ORDER BY 1"
PULSE = (  # alpha's first pulse batch of generic_http and its product
    "SELECT b.batch_id,b.scheduled_for,s.product_id FROM test_batches b JOIN test_runs r "
    "USING(batch_id) JOIN scope_stats s USING(scope_id) JOIN products p "
    "ON p.product_id=s.product_id WHERE b.kind='pulse' AND b.target_id='generic_http' "
    "AND p.provider='alpha' ORDER BY b.scheduled_for LIMIT 1"
)
ADD_EVENT = "INSERT INTO test_events(at,kind,batch_id,product_id,data) VALUES (%s,%s,%s,%s,%s)"
TAPE = (  # alpha's pulse events, as the tape writes them
    ("latency_spike", {"p50": 2379, "value": 2379, "baseline": 954, "success_rate": 0.89}),
    ("pulse_finished", {"p50": 400, "value": 0.9, "previous": {"p50": 401, "blocks": 0}}),
    ("down", {"p50": None, "baseline": 0.97, "value": 0.0}),
)
MEDIANS = {"alpha": 2400, "beta": 300}  # gamma: none
P50S = (  # per network, kind and day: the generic_http pulses' and load runs' merged bins
    "SELECT p.provider,b.kind,b.scheduled_for::date,x.ms,sum(x.n)::bigint FROM run_buckets g "
    "JOIN test_runs r USING(run_key) JOIN test_batches b ON b.batch_id=r.batch_id "
    "JOIN scope_stats s ON s.scope_id=r.scope_id JOIN products p ON p.product_id=s.product_id "
    "CROSS JOIN LATERAL histogram(g.histogram_steps,g.histogram_counts::bigint[]) x "
    "WHERE b.kind IN ('pulse','load') AND s.target='generic_http' GROUP BY 1,2,3,4"
)


def stored(database) -> dict:
    return {name: query(database, sql) for name, sql in (("buckets", BUCKETS), ("geo", GEO))} | {
        "runs": query(database, RUNS),
        "events": query(database, EVENTS),
    }


def add_events(database) -> None:
    batch, at, product = query(database, PULSE)[0]
    with storage.connect(database) as conn:
        partitions.partition(conn, "test_events", at)
        for kind, data in TAPE:
            conn.execute(ADD_EVENT, (at, kind, batch, product, json.dumps(data)))


def bins(steps, counts) -> Counter:
    return Counter(dict(zip(accumulate(steps), counts, strict=True)))


def expected(steps, counts, scale) -> Counter:
    found = Counter()
    for ms, n in bins(steps, counts).items():
        found[math.ceil(ms * scale - 1e-9)] += n
    return found


def run_scales(load, runs, report) -> dict:
    """Each run's scale from the report's medians, as the patch computes it."""
    found = {}
    for run_key, network, kind, target, at, version, _ in runs:
        own = report["networks"][network]
        s = own["real_median"] / own["current_median"] if own["real_median"] else 1.0
        run = patch.Run(run_key, network, kind, target, at, False, version)
        found |= patch.scales(load, [run], s) if kind in ("daily", "pulse", "load") else {}
    return found


def check_rows(old: dict, new: dict, scale: dict) -> None:
    """Every bucket and geo row keeps its counts; its bins are the old ones at ceil(ms x s)."""
    for name, fixed in (("buckets", 4), ("geo", 6)):
        assert len(old[name]) == len(new[name])
        for was, now in zip(old[name], new[name], strict=True):
            assert was[:fixed] == now[:fixed]
            assert sum(now[fixed + 1]) == now[fixed - 1]  # the successes, every one of them
            own = scale.get(was[0], (1.0, None))[0]
            assert bins(*now[fixed:]) == expected(*was[fixed:], own), (name, was[:fixed])


def check_summaries(old: list, new: list, scale: dict) -> None:
    for was, now in zip(old, new, strict=True):
        summary, before = now[6], was[6]
        if was[0] not in scale:  # browser runs stay
            assert summary == before
            continue
        own, h = scale[was[0]]
        assert summary["latency_model"] == "real-median-v1"
        assert summary.get("https_overhead") == h and (h is not None) == (was[3] == "generic_https")
        if own != 1 and before.get("latency_ms", {}).get("p50"):
            p50 = before["latency_ms"]["p50"]
            assert summary["latency_ms"]["p50"] == math.ceil(p50 * own - 1e-9)


def load_factors(database, load) -> dict:
    """Per network and day: its load runs' p50 / its pulses' p50, against load_factor()."""
    merged = {}
    for network, kind, day, ms, n in query(database, P50S):
        at = load.day(datetime.combine(day, datetime.min.time(), UTC))
        merged.setdefault((network, at), {}).setdefault(kind, Counter())[ms] += n
    found = {}
    for (network, day), own in merged.items():
        if "load" in own and "pulse" in own:
            p50 = {kind: Histogram(own[kind]).percentiles()["p50"] for kind in ("load", "pulse")}
            want = NEW_FACTOR(load.seed, network, day)
            found[network, day] = (p50["load"] / p50["pulse"], want)
    return found


def window_figures(payload) -> dict:
    """Per network and window: the latency target's light-load and under-load p50, its
    counts, rate and uptime, and the speed component."""
    found = {}
    for entry in payload["networks"]:
        for label, window in entry["windows"].items():
            m = window["targets"].get("generic_http", {}).get("metrics", {})
            parts = window.get("scores", {}).get("balanced", {}).get("components", {})
            found[entry["provider"], label] = {
                "light": (m.get("latency_ms") or {}).get("p50"),
                "loaded": (m.get("latency_under_load_ms") or {}).get("p50"),
                "same": [m.get(k) for k in ("completed", "successes", "success_rate", "uptime")]
                + [window["uptime"]["value"]],
                "speed": parts.get("speed", {}).get("value"),
            }
    return found


def args(database, out, medians, *extra) -> list:
    given = ["--database", str(database), "--state", str(out / "state.json")]
    return [*given, "--medians", str(medians), *extra]


@pytest.fixture
def staging(database, monkeypatch):
    return sim.staged(database, monkeypatch)


def test_the_patch_moves_latency_to_the_real_medians_and_nothing_else(
    staging, inputs, source, tmp_path, monkeypatch, capsys
):
    with storage.connect(staging) as conn:  # a pulse an hour: fewer runs
        conn.execute("UPDATE targets SET pulse_interval_minutes=60 WHERE pulse_enabled")
    out = tmp_path / "out"
    load = real.argv(staging, inputs, out, source)
    assert sim.simulate.main(load) == 0
    # Score inputs as #840 stored them: score-inputs-1, a load factor per network.
    monkeypatch.setattr(scored_model, "load_factor", patch.previous_load_factor)
    monkeypatch.setattr(scored_store, "MARK", {"synthetic": True, "score_inputs": patch.OLD})
    assert sim.simulate.main([*load, "--add-scored-inputs"]) == 0
    with storage.connect(staging) as conn:
        conn.execute(scored.EVIDENCE, tuple(scored.day_start(d) for d in (-29, 1, 5)))
    assert places.patch.main(places.args(staging)) == 0  # #870's geo rows, patched too
    add_events(staging)
    capsys.readouterr()
    before, old = places.builds(staging), stored(staging)
    medians = tmp_path / "medians.json"
    medians.write_text(json.dumps(MEDIANS))
    given = args(staging, out, medians)
    # Production without #817's three is refused, and so is a network the load lacks.
    name = query(staging, "SELECT current_database()")[0][0]
    monkeypatch.setattr(production_guard, "PRODUCTION", name)
    assert patch.main(given) == 2 and "production" in capsys.readouterr().err
    monkeypatch.setattr(production_guard, "PRODUCTION", "proxy_reviews")
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"delta": 500}))
    assert patch.main(args(staging, out, wrong)) == 2 and "delta" in capsys.readouterr().err
    # The dry run writes nothing; the patch the rows it counted.
    assert patch.main([*given, "--dry-run"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert stored(staging) == old and dry["without_real_median"] == ["gamma"]
    assert patch.main(given) == 0
    done = json.loads(capsys.readouterr().out)
    counted = ("runs", "buckets", "geo_rows", "events")
    for network, own in done["networks"].items():
        assert not own["skipped"] and own["real_median"] == MEDIANS.get(network)
        assert {k: own[k] for k in counted} == {k: dry["networks"][network][k] for k in counted}
    assert done["networks"]["alpha"]["events"] == 3 and done["networks"]["alpha"]["geo_rows"] > 0
    new = stored(staging)
    scale = run_scales(patch.Load.read(out / "state.json"), old["runs"], done)
    check_rows(old, new, scale)
    check_summaries(old["runs"], new["runs"], scale)
    pulses = [r for r in old["runs"] if r[1] == "alpha" and r[2] == "pulse"]
    s = scale[next(r[0] for r in pulses if r[3] == "generic_http")][0]
    https = [scale[r[0]] for r in pulses if r[3] == "generic_https"]
    assert https and all(own == s * h and 1.1 <= h <= 1.4 for own, h in https)
    assert [row[2] for row in new["events"]] == [
        patch.scaled_event(kind, data, s) for _, kind, data in old["events"]
    ]
    # Again: every network skipped, nothing written.
    assert patch.main(given) == 0
    again = json.loads(capsys.readouterr().out)
    assert all(own["skipped"] for own in again["networks"].values()) and stored(staging) == new
    factors = load_factors(staging, patch.Load.read(out / "state.json"))
    assert factors and all(abs(got / want - 1) < 0.06 for got, want in factors.values()), factors
    after = places.builds(staging)
    report = {"load_factors": {f"{n} {d}": v for (n, d), v in factors.items()}}
    for day in (1, 2):
        assert int(before[day][0]["snapshot_id"]) != int(after[day][0]["snapshot_id"])
        report[day] = check_figures(before[day][0], after[day][0])
    print("LATENCY-PATCH", json.dumps({"done": done, "figures": report}, default=str))


def check_figures(before: dict, after: dict) -> dict:
    """Counts, rates and uptime unchanged; the light-load p50 near each real median (gamma's as
    it was), under load about 1.4 times it, and the speed order the real one."""
    old, new = window_figures(before), window_figures(after)
    assert old.keys() == new.keys()
    found = {}
    for key, now in new.items():
        network, _ = key
        assert now["same"] == old[key]["same"], key
        if now["light"] is None:
            continue
        real_median = MEDIANS.get(network)
        if real_median is None:
            assert now["light"] == old[key]["light"], key
        else:
            assert abs(now["light"] / real_median - 1) < 0.1, (key, now)
        if now["loaded"] is not None:
            assert now["loaded"] > now["light"], (key, now)
        found[f"{network} {key[1]}"] = {k: now[k] for k in ("light", "loaded", "speed")}
    for label in {key[1] for key in new}:
        alpha, beta = new.get(("alpha", label)), new.get(("beta", label))
        if alpha and beta and alpha["speed"] is not None and beta["speed"] is not None:
            assert beta["speed"] > alpha["speed"], label  # beta's real median is the lower
    return found
