"""TIMEOUT-LATENCY-1: a request that timed out counts in every latency at its run's timeout limit,
for every network; a time slot still votes only with min_latency_successes successes, and every
other failure stays out of the latency (atlas/calculated/timeout_latency.py)."""

import gc
import os
import random
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from scoring_cases import row

from atlas.calculated import incremental, latency_basket, parallel_measure
from atlas.calculated.bins import Bins
from atlas.calculated.latency import percentiles
from atlas.calculated.policy import load, validate
from atlas.calculated.timeout_latency import counted, limits, rule, with_timeouts
from atlas.calculated.underload import latency_detail, light_votes, time_slots, under_load

T0 = datetime(2026, 11, 22, tzinfo=UTC)
SLOT = timedelta(minutes=30)
RULE = {"timeouts_in_latency": {"targets": ["generic_http", "generic_https"]}}
MIN = 30  # [slots] min_latency_successes


def run(key, target="generic_http", seconds=10.0):
    scope = {"target": {"id": target}, "config": {"timeout_seconds": seconds}}
    return {"run_key": key, "scope": scope}


def answered(slot, ms, ok, timeouts, *, other=0, **kw):
    """A bucket row of ok successes at ms, timeouts timed out and other instant failures."""
    r = row(slot, ms, n=ok + timeouts + other, successes=ok, **kw)
    errors = {"timeout": timeouts, "proxy_connect": other}
    r["counters"]["errors"] = {k: v for k, v in errors.items() if v}
    return r


def histogram(r):
    return dict(r["counters"]["latency_histogram"]["bins"])


def test_the_shipped_methodology_counts_the_echo_targets_timeouts_at_each_runs_limit():
    config = load(Path("configs/methodology.toml"))[0]
    assert rule(config) == (frozenset({"generic_http", "generic_https"}), None)


@pytest.mark.parametrize(
    "setting",
    [
        [],
        {"targets": "generic_http"},
        {"targets": ["generic_http", "generic_http"]},
        {"targets": [""]},
        {"targets": [], "limit_ms": 0},
        {"targets": [], "limit_ms": 1.5},
        {"targets": [], "scope": "every"},
    ],
)
def test_an_invalid_setting_is_refused(setting):
    with pytest.raises(ValueError, match="timeouts_in_latency"):
        rule({"timeouts_in_latency": setting})
    config = load(Path("configs/methodology.toml"))[0]
    with pytest.raises(ValueError, match="timeouts_in_latency"):
        validate(config | {"timeouts_in_latency": setting})


def test_each_run_counts_at_its_own_limit_or_the_settings_fixed_one():
    runs = [run(1), run(2, "generic_https", 15), run(3, "amazon_pdp", 45)]
    assert limits(RULE, runs) == {1: 10000, 2: 15000}  # a browser target is not in the rule
    fixed = {"timeouts_in_latency": RULE["timeouts_in_latency"] | {"limit_ms": 15000}}
    assert limits(fixed, runs) == {1: 15000, 2: 15000}
    assert limits({}, runs) == {}  # without the setting nothing counts


def test_a_timeout_is_one_bin_at_the_limit_and_the_successes_stay():
    r = answered(T0, 300, 90, 10, other=5)
    found = with_timeouts(r["counters"], 10000)
    assert dict(found["latency_histogram"]["bins"]) == {300: 90, 10000: 10}
    assert isinstance(found["latency_histogram"]["bins"], Bins)
    assert (found["successes"], found["completed"]) == (90, 105)  # 5 instant failures stay out
    assert histogram(r) == {300: 90}  # the stored row is not edited
    same = answered(T0, 300, 90, 0, other=5)["counters"]
    assert with_timeouts(same, 10000) is same
    late = answered(T0, 10000, 3, 2)["counters"]
    late["latency_histogram"] = {"bins": [(9, 1), (10001, 2)]}  # a success over the limit
    assert histogram({"counters": with_timeouts(late, 10000)}) == {9: 1, 10000: 2, 10001: 2}
    late["latency_histogram"]["bins"] = [(10000, 3)]  # a success right at the limit: one bin
    assert list(with_timeouts(late, 10000)["latency_histogram"]["bins"]) == [(10000, 5)]


def test_the_calculation_reads_the_rows_with_their_timeouts():
    """Fails before TIMEOUT-LATENCY-1: incremental._measured() gave the rows as stored."""
    config = load(Path("configs/methodology.toml"))[0]
    runs = [run(1), run(2, "amazon_pdp", 45)]
    rows = [answered(T0, 300, 90, 10, run_key=1), answered(T0, 900, 90, 10, run_key=2)]
    rows[1]["target"] = "amazon_pdp"
    kept = incremental._measured(config, runs, rows, {"pulses": []})
    assert histogram(kept[0]) == {300: 90, 10000: 10}
    assert kept[1] is rows[1]  # not a target of the rule
    plain = answered(T0, 300, 100, 0, run_key=1)
    assert counted(config, runs, [plain])[0] is plain  # a row without timeouts is kept as it is


def test_a_slot_votes_by_its_successes_and_weighs_its_timeouts():
    runs = [run(1)]
    voting = counted(RULE, runs, [answered(T0, 300, 30, 70, run_key=1)])
    short = counted(RULE, runs, [answered(T0 + SLOT, 300, 29, 71, run_key=1)])
    assert len(light_votes(time_slots(voting), MIN)) == 1  # 30 successes: it votes
    assert light_votes(time_slots(short), MIN) == []  # 29 successes, 100 answers: it does not
    ((_, _, h),) = light_votes(time_slots(voting), MIN)
    assert h == Counter({300: 30, 10000: 70})


def test_the_latency_and_its_mean_count_each_timeout_at_the_limit():
    """90 successes at 300 ms and 10 timeouts of a 10 s run: p90 stays (the 90th answer is a
    success), p95, p99 and the mean take the timeouts; without the rule only the successes."""
    rows = [answered(T0, 300, 90, 10, run_key=1, country="DE")]
    before = latency_basket.countries(rows, "generic_http", MIN, {"DE"})["DE"]
    after = latency_basket.countries(counted(RULE, [run(1)], rows), "generic_http", MIN, {"DE"})
    assert before == {"p50": 300, "p90": 300, "p95": 300, "p99": 300, "mean": 300.0}
    assert after["DE"] == {"p50": 300, "p90": 300, "p95": 10000, "p99": 10000, "mean": 1270.0}


def test_the_latency_under_load_and_per_country_count_them_too():
    daily = counted(RULE, [run(1)], [answered(T0, 500, 50, 50, run_key=1, test=None)])
    assert under_load(daily, MIN)["ms"]["p90"] == 10000
    pulse = counted(RULE, [run(2)], [answered(T0, 200, 80, 20, run_key=2)])
    (country,) = latency_detail(pulse + daily, MIN)["countries"]
    assert country["latency_ms"]["p90"] == 10000 and country["latency_ms"]["p50"] == 200
    assert country["latency_under_load_ms"]["p50"] == 500


def reference(entries, limit):
    """The investigation's what-if (LATENCY-INVESTIGATION-1, section 4), written apart from the
    calculation: per slot its successes and each timeout as one answer at the limit, every slot
    with MIN successes weighing 1, the nearest rank of the merged weights."""
    weights, used = Counter(), 0
    for ok, timeouts in entries:
        if sum(ok.values()) < MIN:
            continue
        values = [ms for ms, n in ok.items() for _ in range(n)] + [limit] * timeouts
        used += 1
        for ms in values:
            weights[ms] += 1 / len(values)
    return percentiles(weights, used)


def test_the_basket_country_latency_is_the_investigations_what_if():
    rng = random.Random(1123)
    rows, slots = [], defaultdict(list)
    for i in range(48):
        times = (180, 260, 340, 420, 900, 2400)
        ok = Counter(rng.choice(times) for _ in range(rng.randint(20, 100)))
        timeouts = rng.choice((0, 0, 1, 3, 12))
        slot = T0 + i * SLOT
        r = answered(slot, 0, sum(ok.values()), timeouts, run_key=i, country="JP")
        r["counters"]["latency_histogram"]["bins"] = sorted(ok.items())
        rows.append(r)
        slots["JP"].append((ok, timeouts))
    runs = [run(i) for i in range(48)]
    found = latency_basket.countries(counted(RULE, runs, rows), "generic_http", MIN, {"JP"})
    expected = reference(slots["JP"], 10000)
    assert {k: found["JP"][k] for k in expected} == expected
    assert found["JP"]["p99"] == 10000  # some slots time out often enough to reach the p99


def copied(groups):
    """The private memory (MB) a forked worker gains reading every row of groups[0] (as
    test_calc_parallel_measure.copied())."""
    gc.freeze()
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:  # the worker
        before = parallel_measure.private_mb()
        touched = sum(row["counters"]["completed"] for row in groups[0])
        os.write(write, f"{parallel_measure.private_mb() - before} {touched}".encode())
        os._exit(0)
    os.close(write)
    found = int(os.read(read, 100).split()[0])
    os.waitpid(pid, 0)
    gc.unfreeze()
    return found


@pytest.mark.skipif(not Path("/proc/self/smaps_rollup").exists(), reason="Linux /proc only")
def test_the_rows_with_timeouts_are_made_network_by_network():
    """Rows in time order, four networks interleaved: the rows made for their timeouts are made
    network by network, so a worker reading one network copies about a quarter of them."""
    runs = [run(k) | {"product_id": k} for k in range(4)]

    def bucket(n):
        counters = {"completed": n, "successes": 1, "errors": {"timeout": 1}}
        counters["latency_histogram"] = {"bins": Bins.of([(300, 1)])}
        return {"run_key": n % 4, "bucket_start": n, "counters": counters}

    rows = counted(RULE, runs, [bucket(n) for n in range(200_000)])
    assert [r["bucket_start"] for r in rows[:5]] == [0, 1, 2, 3, 4]  # in their order
    own = copied([[r for r in rows if r["run_key"] == g] for g in range(4)])
    mixed = [r | {"counters": dict(r["counters"])} for r in rows]  # made in time order
    interleaved = copied([[r for r in mixed if r["run_key"] == g] for g in range(4)])
    assert interleaved > 10 and own < interleaved / 2, (own, interleaved)
