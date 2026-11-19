"""The owner's fixed target difficulties (TARGET-DIFFICULTY-FIXED-1, set 2026-11-12): never derived
from results, changed only on the owner's word. This test holds the 13 values and fails when any
place that carries them drifts: the methodology's [browser_difficulty] and [http_difficulty]
(configs/methodology.toml), the site catalog (api/scripts/catalog-targets.sql) and the API head
the API serves /v1/meta's scoreWeight from (api/fixtures/measured/head-v4-browser-scores.json,
TestTheCalculatorsBrowserScoresAreServed)."""

import json
import tomllib
from pathlib import Path

from test_catalog_targets import catalog

from atlas.calculated.policy import browser_difficulty, http_difficulty, load

OWNER = {
    "generic_http": 1,
    "generic_https": 1,
    "amazon_pdp": 2,
    "social_web": 3,
    "instagram_web": 3,
    "x_web": 3,
    "google_search": 4,
    "cloudflare_challenge": 4,
    "ai_answers": 5,
    "ai_overview": 5,
    "ai_chatgpt": 5,
    "ai_gemini": 5,
    "linkedin_public": 5,
}
HTTP = ("generic_http", "generic_https")
BROWSER = {t: d for t, d in OWNER.items() if t not in HTTP}
POLICY = Path("configs/methodology.toml")
HEAD = Path("api/fixtures/measured/head-v4-browser-scores.json")


def test_the_methodology_holds_the_owners_values():
    raw = tomllib.loads(POLICY.read_text())
    # Stated explicitly, the group included: nothing is filled in from another rule.
    assert raw["browser_difficulty"] == BROWSER
    assert raw["http_difficulty"] == {t: OWNER[t] for t in HTTP}
    config, _ = load(POLICY)
    assert browser_difficulty(config) == BROWSER
    assert http_difficulty(config) == {t: OWNER[t] for t in HTTP}


def test_the_catalog_holds_the_owners_values():
    assert {t: d for t, _, d, _ in catalog()} == OWNER


def test_the_served_head_holds_the_owners_values():
    head = json.loads(HEAD.read_text())
    settings = head["methodology"]["settings"]
    assert settings["browser_difficulty"] == BROWSER
    assert settings["http_difficulty"] == {t: OWNER[t] for t in HTTP}
    for network in head["networks"]:
        for window in network["windows"].values():
            for engine in window.get("browser_overall", {}).values():
                assert engine["weights"] == {t: OWNER[t] for t in engine["weights"]}
