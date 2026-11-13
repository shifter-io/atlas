"""The rank rule's shared cases (SUCCESS-SCORE-API-1): metric_scores of
calculated/target_scores.py gives each case of api/fixtures/scores/success-score-cases.json
the scores it lists, the cases the API's place scores are checked against too."""

import json
from pathlib import Path

import pytest

from atlas.calculated.target_scores import metric_scores

CASES = Path(__file__).parents[1] / "api/fixtures/scores/success-score-cases.json"
FIELDS = ("score", "rank", "ranked_of", "status")


@pytest.mark.parametrize("case", json.loads(CASES.read_text())["cases"], ids=lambda c: c["name"])
def test_metric_scores_match_the_shared_cases(case):
    rated = [(rate, requests) for rate, requests in case["rated"]]
    got = [{k: s[k] for k in FIELDS} for s in metric_scores(rated, True)]
    assert got == case["want"]
