"""BROWSER-SPREAD-1 (#875): each network's own browser outcome mix (simulate_browser.mix()) and
simulate_browser_patch.py, which rewrites the stored browser runs of the test days to it. The
mix and the rewrite are pure; the patch runs on the test database loaded by simulate.py."""

import importlib
import json
import random
import sys
from datetime import UTC, datetime, time, timedelta
from itertools import accumulate
from pathlib import Path

import pytest
from orchestrator_cases import query

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "scale"))
browser = importlib.import_module("simulate_browser")
patch = importlib.import_module("simulate_browser_patch")
sim = importlib.import_module("test_scale_simulate")

from proxy_index import production_guard, storage  # noqa: E402
from proxy_index.calculated import calculator, target_scores  # noqa: E402

pytest_plugins = ["test_storage"]

NETWORKS = tuple(f"net{n:02d}" for n in range(18))
METHODOLOGY = ROOT / "configs" / "methodology.toml"
STORED = (
    "SELECT g.run_key,p.provider,s.target,g.completed,g.successes,g.challenges,g.blocks,"
    "g.timeouts,g.proxy_errors,g.errors,g.histogram_counts,r.summary,br.successes "
    "FROM run_buckets g JOIN test_runs r USING(run_key) JOIN test_batches b USING(batch_id) "
    "JOIN batch_runs br ON br.run_key=r.run_key JOIN scope_stats s ON s.scope_id=r.scope_id "
    "JOIN products p ON p.product_id=s.product_id WHERE b.kind='browser' ORDER BY 1"
)
# The copies' marks of the hours with a browser bucket (rollups.refresh_buckets()).
COPIED = (
    "SELECT count(*) FROM calculated_pulse_rollups m WHERE m.hour IN (SELECT "
    "date_trunc('hour',g.bucket_start) FROM run_buckets g JOIN test_runs r USING(run_key) "
    "JOIN test_batches b USING(batch_id) WHERE b.kind='browser')"
)


def engines(**outcomes) -> dict:
    return {"camoufox": {"outcomes": outcomes}}


def profile(target: str, rates: dict | None = None, **outcomes) -> dict:
    """18 networks with camoufox outcomes of the target: `outcomes` each, or rates' ok shares;
    none without either."""
    networks = {}
    for name in NETWORKS:
        own = outcomes
        if rates:
            own = {"ok": round(100 * rates[name]), "challenge": 100 - round(100 * rates[name])}
        networks[name] = {"browser": {target: engines(**own)} if own else {}}
    return {"networks": networks}


def shares(given: dict, target: str, engine: str) -> dict:
    return {name: browser.mix(given, name, target, engine) for name in NETWORKS}


def similar(rates: list[float], tries: int) -> bool:
    return target_scores.similar([(rate, tries) for rate in rates])


def test_a_shared_hour_becomes_a_spread_wide_enough_to_rank_at_200_tries():
    given = profile("amazon_pdp")  # no network has it: every one takes the measured hour
    found = shares(given, "amazon_pdp", "chrome")
    base = 93 / 100
    for mix in found.values():
        assert sum(mix.values()) == pytest.approx(browser.PARTS, abs=10)
        ok = mix["ok"] / browser.PARTS
        assert browser.BOUNDS[0] <= ok <= browser.BOUNDS[1]
        assert abs(ok - base) <= browser.SPREAD + 1e-9
        assert set(k for k, n in mix.items() if n) <= {"ok", "timeout"}  # its failures' mix
    rates = [mix["ok"] / browser.PARTS for mix in found.values()]
    assert similar([0.92, 0.93], 200) and not similar(rates, 200)
    assert found == shares(given, "amazon_pdp", "chrome")  # a function of its inputs only
    assert shares(given, "amazon_pdp", "camoufox") != found  # the engine is in the md5


def test_the_networks_keep_their_real_order():
    real = {name: 0.2 + 0.03 * n for n, name in enumerate(reversed(NETWORKS))}
    given = profile("instagram_web", rates=real)
    found = shares(given, "instagram_web", "chrome")  # the real ones are camoufox's
    order = sorted(NETWORKS, key=lambda name: found[name]["ok"])
    assert order == sorted(NETWORKS, key=real.get)
    assert browser.real_rates(given, "instagram_web", "chrome") == pytest.approx(real, abs=0.006)
    assert browser.real_rates(given, "google_search", "chrome") == {}


def test_a_reference_comes_before_the_profile_for_the_order():
    given = profile("google_search", rates=dict.fromkeys(NETWORKS, 0.5))
    targets = {NETWORKS[0]: {"google_search": engines(ok=1, challenge=99)}}
    given["reference"] = {"browser": {"targets": targets}}
    assert browser.real_rates(given, "google_search", "camoufox")[NETWORKS[0]] == 0.01
    low = min(NETWORKS, key=lambda n: browser.offset(given, n, "google_search", "camoufox"))
    assert low == NETWORKS[0]


def test_a_source_without_a_success_keeps_none():
    given = profile("ai_chatgpt", unsplit=9, timeout=1)
    for mix in shares(given, "ai_chatgpt", "camoufox").values():
        assert mix["ok"] == 0
        own = {"other_error": 900_000, "timeout": 100_000}
        assert mix == dict.fromkeys(browser.CODES, 0) | own


def test_a_base_centres_a_source_without_a_success_and_nothing_else():
    given = profile("ai_overview", challenge=60, unsplit=40)
    given["browser_bases"] = {"ai_overview": {"camoufox": 0.3}}
    found = shares(given, "ai_overview", "camoufox")
    for mix in found.values():
        ok = mix["ok"] / browser.PARTS
        assert abs(ok - 0.3) <= browser.SPREAD + 1e-9 and ok >= browser.BOUNDS[0]
        assert set(k for k, n in mix.items() if n) == {"ok", "challenge", "other_error"}
        ratio = (mix["challenge"] / mix["other_error"]) / 1.5  # the failures keep their mix
        assert 0.9 / 1.1 - 1e-6 <= ratio <= 1.1 / 0.9 + 1e-6
    rates = [mix["ok"] / browser.PARTS for mix in found.values()]
    assert not similar(rates, 200)
    order = sorted(NETWORKS, key=lambda n: found[n]["ok"])  # md5's order: the real shares tie
    assert order == sorted(NETWORKS, key=lambda n: browser.signed_unit(f"{n}:ai_overview:camoufox"))
    plain = profile("ai_overview", challenge=60, unsplit=40)
    assert shares(given, "ai_overview", "chrome") == shares(plain, "ai_overview", "chrome")
    assert all(mix["ok"] == 0 for mix in shares(given, "ai_overview", "chrome").values())
    plain = profile("google_search", ok=30, challenge=70)
    other = plain | {"browser_bases": given["browser_bases"]}
    assert shares(other, "google_search", "camoufox") == shares(plain, "google_search", "camoufox")
    assert browser.base(given, "ai_chatgpt", "camoufox") is None


def test_the_bases_file_is_a_target_engine_share_object(tmp_path):
    path = tmp_path / "bases.json"
    path.write_text('{"ai_overview": {"camoufox": 0.4}, "ai_chatgpt": {"camoufox": 1}}')
    found, digest = patch.read_bases(path)
    assert found["ai_chatgpt"] == {"camoufox": 1} and len(digest) == 12
    for wrong in ("[0.4]", "{", '{"ai_overview": 0.4}', '{"ai_overview": {}}'):
        path.write_text(wrong)
        with pytest.raises(patch.Refused, match="--bases"):
            patch.read_bases(path)
    for share in ("1.2", "-0.1", "true", '"0.4"'):
        path.write_text(f'{{"ai_overview": {{"camoufox": {share}}}}}')
        with pytest.raises(patch.Refused, match="--bases"):
            patch.read_bases(path)


def test_the_failures_keep_their_mix_within_a_tenth_and_fill_the_rest():
    given = profile("cloudflare_challenge", ok=30, challenge=40, block=20, no_content=10)
    for mix in shares(given, "cloudflare_challenge", "camoufox").values():
        failed = browser.PARTS - mix["ok"]
        assert sum(n for k, n in mix.items() if k != "ok") == pytest.approx(failed, abs=10)
        ratio = (mix["challenge"] / mix["block"]) / 2
        assert 0.9 / 1.1 - 1e-6 <= ratio <= 1.1 / 0.9 + 1e-6
    full = profile("ai_gemini", ok=100)  # no failure measured: the rest is other_error
    mix = browser.mix(full, NETWORKS[0], "ai_gemini", "camoufox")
    assert mix["ok"] + mix["other_error"] == pytest.approx(browser.PARTS, abs=1)
    assert mix["ok"] <= 980_000


def bucket(completed=100, successes=40, steps=(1000, 500), counts=(30, 10), typed=True) -> dict:
    found = {"run_key": 7, "at": datetime(2026, 11, 8, 3, 10, tzinfo=UTC)}
    found |= {"completed": completed, "successes": successes, "outcome_counters": typed}
    return found | {"steps": list(steps), "counts": list(counts)}


def test_a_bucket_is_rewritten_to_the_mix_and_its_summary_follows():
    weights = {name: 0 for name in browser.CODES} | {"ok": 600, "challenge": 300, "no_content": 100}
    new = patch.rewritten(bucket(), weights, (2500, 4500))
    assert new["successes"] == 60 and new["challenges"] == 30 and new["blocks"] == 0
    assert new["errors"] == {"challenge": 30, "no_content": 10}
    assert sum(new["counts"]) == 60 and len(new["steps"]) == 2  # the stored bins, scaled
    assert new["counts"] == [45, 15]
    found = patch.summary(new, 100)
    assert found["total_errors"] == 40 and found["success_rate"] == 0.6
    assert found["latency_ms"]["p50"] == 1000 and found["browser_model"] == browser.MODEL
    empty = patch.rewritten(bucket(successes=0, counts=(), steps=()), weights, (2500, 4500))
    assert sum(empty["counts"]) == 60  # page loads drawn as the generator does
    none = patch.rewritten(bucket(), dict(weights, ok=0), (2500, 4500))
    assert none["steps"] == none["counts"] == []
    assert patch.summary(none, 100)["latency_ms"]["p50"] is None
    with pytest.raises(patch.Refused, match="typed outcome counters"):
        patch.rewritten(bucket(typed=False), weights, (2500, 4500))


def test_scaled_draws_page_loads_around_the_p50():
    steps, counts = patch.scaled([], [], 50, random.Random(1), (3000, 6000))
    ms = list(accumulate(steps))
    assert sum(counts) == 50 and min(ms) < 3000 < max(ms)


def outcomes_of(row) -> int:
    """A stored bucket's tries over the K-BP6 outcomes (calculated/browser.py)."""
    _, _, _, completed, ok, *typed, errors, _, _, _ = row
    codes = sum(errors.get(code, 0) for code in ("login_wall", "no_content", "no_answer"))
    other = errors.get("browser_error", 0)
    assert sum(errors.values()) == completed - ok
    return ok + sum(typed) + codes + other


def network_figures(payload) -> dict:
    """Each network's windows: every score's value and rank, its uptime."""
    found = {}
    for entry in payload["networks"]:
        for label, window in entry["windows"].items():
            own = {k: s for k, s in window["scores"].items() if isinstance(s, dict)}
            scores = {k: (s.get("score"), s.get("rank")) for k, s in own.items()}
            found[entry["provider"], label] = (scores, window["uptime"]["value"])
    return found


@pytest.fixture
def staging(database, monkeypatch):
    return sim.staged(database, monkeypatch)


inputs = sim.inputs


def test_the_patch_rewrites_the_browser_runs_once_and_no_network_figure(
    staging, inputs, tmp_path, capsys, monkeypatch
):
    sim.loaded(staging, inputs, tmp_path)
    as_of = datetime.combine(sim.FIRST + timedelta(days=1), time(), UTC)
    before = calculator.calculate(staging, METHODOLOGY, as_of=as_of, mode="incremental")
    assert query(staging, COPIED)[0][0] > 0
    with storage.connect(staging) as conn:  # the stored runs of the shared hour: all ok
        conn.execute(
            "UPDATE run_buckets SET successes=completed,challenges=0,blocks=0,timeouts=0,"
            "proxy_errors=0,errors='{}' WHERE run_key IN (SELECT run_key FROM test_runs "
            "JOIN test_batches USING(batch_id) WHERE kind='browser')"
        )
    stored = query(staging, STORED)
    # The load's day and the extra hour after it (the queue's entries that start there).
    day = timedelta(days=1)
    span = ["--from", (as_of - day).isoformat(), "--to", (as_of + day).isoformat()]
    argv = ["--database", str(staging), "--profile", str(inputs["profile"]), *span]
    argv += ["--browser-settings", str(inputs["browser-settings"])]
    capsys.readouterr()
    # Production without #817's three is refused; a dry run writes nothing.
    name = query(staging, "SELECT current_database()")[0][0]
    monkeypatch.setattr(production_guard, "PRODUCTION", name)
    assert patch.main(argv) == 2 and "production" in capsys.readouterr().err
    monkeypatch.setattr(production_guard, "PRODUCTION", "proxy_reviews")
    assert patch.main([*argv, "--dry-run"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] and dry["patched"] == dry["browser_runs"] == len(stored) > 0
    assert query(staging, STORED) == stored
    assert patch.main(argv) == 0
    done = json.loads(capsys.readouterr().out)
    assert done["patched"] == len(stored) and done["hours_reset"] > 0
    assert done["success_share"] == dry["success_share"]
    given = json.loads(inputs["profile"].read_text())
    settings = patch.load_settings(inputs["browser-settings"])
    for row in query(staging, STORED):
        run_key, network, target, completed, ok, *_, errors, counts, summary, batch_ok = row
        mix = browser.mix(given, network, target, settings.engine(target))
        assert ok == patch.counts(mix, completed)["ok"] == sum(counts) == batch_ok
        assert outcomes_of(row) == completed  # every try has one outcome
        assert summary["successes"] == ok and summary["errors"] == errors
        assert summary["browser_model"] == browser.MODEL
        assert summary["total_errors"] == completed - ok
    assert query(staging, COPIED)[0][0] == 0  # the next calculation copies them again
    assert patch.main(argv) == 0  # again: nothing
    again = json.loads(capsys.readouterr().out)
    assert again["patched"] == 0 and again["skipped"] == len(stored)
    # BROWSER-AI-SPREAD-1: a base for one target and its engine rewrites its runs once more.
    target = settings.targets[0].id
    path = tmp_path / "bases.json"
    path.write_text(json.dumps({target: {settings.engine(target): 0.5}}))
    assert patch.main([*argv, "--bases", str(path)]) == 0
    centred = json.loads(capsys.readouterr().out)
    own = [row for row in query(staging, STORED) if row[2] == target]
    assert centred["patched"] == len(own) > 0 and list(centred["success_share"]) == [target]
    given["browser_bases"], digest = patch.read_bases(path)
    for row in own:
        mix = browser.mix(given, row[1], target, settings.engine(target))
        assert row[4] == patch.counts(mix, row[3])["ok"] == row[11]["successes"]
        assert row[11]["browser_bases"] == digest and outcomes_of(row) == row[3]
    assert patch.main([*argv, "--bases", str(path)]) == 0  # again: nothing
    assert json.loads(capsys.readouterr().out)["patched"] == 0
    after = calculator.calculate(staging, METHODOLOGY, as_of=as_of, mode="incremental")
    assert int(after["snapshot_id"]) != int(before["snapshot_id"])
    assert network_figures(after) == network_figures(before)  # browser rows never count there
