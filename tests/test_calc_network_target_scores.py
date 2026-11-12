"""SCORES-V4-1: the browser target scores and browser_overall on the network summaries of a
payload v4 build, the windows the API reads (calculated_snapshots.api_head).

The end-to-end test calculates and publishes a build of three networks with Camoufox runs of
two browser targets, 120 tries each, and checks the API head carries every network's scores.
With SCORES_V4_FIXTURE_WRITE=1 it writes that head as api/fixtures/measured/
head-v4-browser-scores.json, which api/internal/server publishes and serves
(TestTheCalculatorsBrowserScoresAreServed): the calculator and the API meet on a real head.
"""

import copy
import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from calculated_browser_cases import TRIES, browser_run, generic
from calculated_cases import lenient_policy
from census_pulse_cases import batch, batch_run, product

from proxy_index.calculated import target_scores as ts
from proxy_index.calculated.calculator import calculate
from proxy_index.calculated.policy import load
from proxy_index.storage import connect, ingest

pytest_plugins = ["test_storage", "calculated_browser_cases"]

FIXTURE = Path(__file__).parents[1] / "api/fixtures/measured/head-v4-browser-scores.json"
TARGETS = ("amazon_pdp", "google_search")
OK, CHALLENGE, BLOCK = TRIES[0], TRIES[2], TRIES[3]
# Ten tries of each provider, repeated: success, challenge and block rates far apart.
PATTERNS = {
    "alpha": [OK] * 9 + [CHALLENGE],
    "beta": [OK] * 6 + [CHALLENGE] * 2 + [BLOCK] * 2,
    "gamma": [OK] * 3 + [CHALLENGE] * 3 + [BLOCK] * 4,
}
REPEAT = 12  # 120 tries per run, over MIN_REQUESTS
SUCCESS = {"alpha": 10, "beta": 5.5, "gamma": 1}
API_HEAD = "SELECT api_head FROM calculated_snapshots s JOIN current_calculation c USING(build_id)"


def kinds_network(provider, kind, ok):
    """A network entry of one 24h window with Camoufox figures of amazon_pdp."""
    own = {"completed": 500, "successes": round(ok * 500)}
    own |= {"outcome_rates": {"ok": ok, "challenge": 0.0, "block": 1 - ok}}
    browser = {"engine": "camoufox", "outcome_rates": own["outcome_rates"]}
    target = {"metrics": {}, "browser": browser | {"engines": {"camoufox": own}}}
    window = {"targets": {"amazon_pdp": target}}
    return {"provider": provider, "kind": kind, "windows": {"24h": window}}


def test_networks_are_ranked_within_their_kind():
    config, _ = load(Path("configs/methodology.toml"))
    residential = [kinds_network(p, "residential", ok) for p, ok in (("a", 0.9), ("b", 0.6))]
    residential.append(kinds_network("c", "residential", 0.3))
    mobile = [kinds_network(p, "mobile", ok) for p, ok in (("a", 0.2), ("b", 0.1))]
    ts.score_networks(residential + mobile, config)
    scores = [
        n["windows"]["24h"]["targets"]["amazon_pdp"]["browser"]["scores"]["success"]
        for n in residential + mobile
    ]
    assert [s["score"] for s in scores[:3]] == [10, 5.5, 1]
    # Two mobile networks are too few: the residential ones never count for them.
    assert {s["status"] for s in scores[3:]} == {ts.TOO_FEW_NETWORKS}
    overall = residential[0]["windows"]["24h"]["browser_overall"]["camoufox"]["success"]
    assert overall == {"score": 10, "rank": 1, "ranked_of": 3, "status": ts.RANKED}


async def add_runs(database, tmp_path):
    """Each provider's Camoufox run of each of TARGETS, an hour back, with its batch run."""
    for target in TARGETS:
        for provider, pattern in PATTERNS.items():
            directory, summary = await browser_run(tmp_path, provider, target, 1, pattern * REPEAT)
            started = datetime.fromisoformat(summary["started_at"])
            with connect(database) as conn:
                batch_id = batch(conn, "browser", started, country="US", target=target)
            key = ingest(directory, database, batch_id=batch_id)["run_key"]
            with connect(database) as conn:
                batch_run(
                    conn,
                    batch_id,
                    product(conn, provider),
                    datetime.fromisoformat(summary["finished_at"]),
                    run_key=key,
                    requests=summary["completed"],
                    successes=summary["successes"],
                    bytes_down=summary["bytes_down"],
                )


def check_head(head):
    """Every network of head has, in every window, the success, challenge and block scores of
    each browser target with tries, on the browser object and per engine, and a browser_overall:
    the networks ranked against each other."""
    assert head["schema_version"] == 4 and len(head["networks"]) == len(PATTERNS)
    for entry in head["networks"]:
        for label, window in entry["windows"].items():
            for target in TARGETS:
                b = window["targets"][target]["browser"]
                assert b["engines"]["camoufox"]["completed"] == 10 * REPEAT, (label, target)
                assert set(b["scores"]) == set(ts.METRICS)
                # The main engine's, with the previous build's score (SUCCESS-SCORE-DELTA-1).
                main = copy.deepcopy(b["scores"])
                assert main["success"].pop("previous_score", None) is None  # the first build
                assert main == b["engines"]["camoufox"]["scores"]
                success = b["scores"]["success"]
                assert success["status"] == ts.RANKED and success["ranked_of"] == 3
                assert success["score"] == SUCCESS[entry["provider"]], (label, target)
            overall = window["browser_overall"]["camoufox"]
            assert overall["success"]["score"] == SUCCESS[entry["provider"]], label
            assert overall["targets"] == list(TARGETS)


@pytest.mark.asyncio
async def test_the_published_api_head_carries_every_networks_scores(database, tmp_path):
    as_of = await generic(database, tmp_path)
    await add_runs(database, tmp_path)
    calculate(database, lenient_policy(tmp_path), as_of=as_of, publish=True)
    with connect(database) as conn:
        (text,) = conn.execute(API_HEAD).fetchone()
    head = json.loads(text)
    check_head(head)
    if os.environ.get("SCORES_V4_FIXTURE_WRITE") == "1":
        FIXTURE.write_text(json.dumps(head, indent=1) + "\n")
    # The committed fixture is such a head, as the API test publishes it.
    check_head(json.loads(FIXTURE.read_text()))
