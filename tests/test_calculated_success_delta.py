"""The previous build's success scores (SUCCESS-SCORE-DELTA-1, target_scores.previous_scores()):
two builds in a row give previous_score = the first build's score, null without a previous
build, one newer than the build or a network it lacks; nothing else changes. The PostgreSQL case
reads them through parts.previous()'s projection (NETWORK_SCORES)."""

import copy
from datetime import datetime, timedelta

import pytest
from psycopg.types.json import Jsonb
from test_calculated_parts import POLICY, two_providers
from test_calculated_success_scores import BROWSER, network, scored
from test_calculated_target_scores import GROUPS, WEIGHTS, products

from atlas.calculated import parts, store
from atlas.calculated import target_scores as ts
from atlas.calculated.calculator import calculate
from atlas.storage import connect

pytest_plugins = ["test_storage", "calculated_template"]

AS_OF = "2026-11-10T00:00:00+00:00"
LATER = datetime.fromisoformat(AS_OF) + timedelta(hours=1)


@pytest.fixture
def database(calculated_database):
    return calculated_database


def build(*rates):
    """Network summaries scored as score_networks() scores them, one per (http, https, browser)."""
    found = [network(*r) for r in rates]
    ts.score(products(*found), WEIGHTS, GROUPS)
    return [{"id": f"n{i}", "windows": {"24h": w}} for i, w in enumerate(found)]


def projection(networks, as_of=AS_OF):
    """parts.previous() of a published build of networks: its windows' success scores."""
    windows = {}
    for n in networks:
        for label, w in n["windows"].items():
            targets = {}
            for t, v in w["targets"].items():
                own = v["browser"]["scores"] if "browser" in v else v.get("scores")
                targets[t] = own and own["success"]["score"]
            overall = w.get("success_overall", {}).get("score")
            windows[n["id"], label] = {"success": {"overall": overall, "targets": targets}}
    networks = [
        {"id": n["id"], "windows": {lb: windows[n["id"], lb] for lb in n["windows"]}}
        for n in networks
    ]
    return {"as_of": as_of, "products": [], "networks": networks}


def target_score(w, target):
    entry = w["targets"][target]
    return (entry["browser"]["scores"] if "browser" in entry else entry["scores"])["success"]


def test_two_builds_in_a_row_carry_the_first_builds_scores():
    first = build((0.9, 0.3, 0.9), (0.6, 0.6, 0.6), (0.3, 0.9, 0.3))
    second = build((0.3, 0.9, 0.3), (0.6, 0.6, 0.6), (0.9, 0.3, 0.9))
    ts.previous_scores(second, projection(first), LATER)
    for old, new in zip(first, second, strict=True):
        w, was = new["windows"]["24h"], old["windows"]["24h"]
        assert w["success_overall"]["previous_score"] == was["success_overall"]["score"]
        for target in (*ts.HTTP_TARGETS, *BROWSER):
            assert target_score(w, target)["previous_score"] == target_score(was, target)["score"]
    best = second[2]["windows"]["24h"]
    assert best["success_overall"]["previous_score"] == pytest.approx(2.125)
    assert scored(best, "generic_http")["previous_score"] == 1


def test_no_previous_build_or_a_newer_one_or_a_new_network_is_null():
    first = build((0.9, 0.3, 0.9), (0.6, 0.6, 0.6), (0.3, 0.9, 0.3))
    for previous, as_of in (
        (None, AS_OF),
        (projection(first, "2026-11-10T01:00:00+00:00"), AS_OF),
        (projection(first[:1]) | {"networks": []}, AS_OF),
    ):
        second = build((0.9, 0.3, 0.9), (0.6, 0.6, 0.6), (0.3, 0.9, 0.3))
        ts.previous_scores(second, previous, datetime.fromisoformat(as_of))
        for n in second:
            w = n["windows"]["24h"]
            assert w["success_overall"]["previous_score"] is None
            assert {target_score(w, t)["previous_score"] for t in ts.HTTP_TARGETS} == {None}
    # A previous score that was not ranked stays null.
    unranked = build((0.9, 0.3), (0.6, 0.6))
    later = build((0.9, 0.3), (0.6, 0.6))
    ts.previous_scores(later, projection(unranked), datetime.fromisoformat(AS_OF))
    assert later[0]["windows"]["24h"]["success_overall"]["previous_score"] is None


def test_only_previous_score_is_added():
    """No Index Score, rank, Speed or other success field changes: removing previous_score
    gives the build back exactly."""
    first = build((0.9, 0.3, 0.9), (0.6, 0.6, 0.6), (0.3, 0.9, 0.3))
    second = build((0.3, 0.9, 0.3), (0.6, 0.6, 0.6), (0.9, 0.3, 0.9))
    for n in second:
        n["windows"]["24h"]["scores"] = {"balanced": {"score": 61.5, "rank": 2, "speed": 40}}
    before = copy.deepcopy(second)
    ts.previous_scores(second, projection(first), datetime.fromisoformat(AS_OF))
    for n in second:
        w = n["windows"]["24h"]
        del w["success_overall"]["previous_score"]
        for target in w["targets"]:
            entry = w["targets"][target]
            own = entry["browser"].get("scores") if "browser" in entry else entry.get("scores")
            if own:
                del own["success"]["previous_score"]
    assert second == before


@pytest.mark.asyncio
async def test_the_next_build_reads_the_published_scores(database, tmp_path):
    end = await two_providers(database, tmp_path)
    first = calculate(database, POLICY, as_of=end, publish=True)
    head = store.read_head(database, first["snapshot_id"])
    with connect(database) as conn:
        found = parts.previous(conn, groups=False)
    # The projection of every network window: success_overall's score and each target's.
    for n, old in zip(found["networks"], head["networks"], strict=True):
        for label, w in old["windows"].items():
            success = n["windows"][label]["success"]
            assert success["overall"] == (w.get("success_overall") or {}).get("score")
            assert set(success["targets"]) == set(w["targets"])
    # Give the published build ranked scores (two networks never rank): the next build keeps them.
    path = next(
        (i, label)
        for i, n in enumerate(head["networks"])
        for label, w in n["windows"].items()
        if "scores" in w["targets"].get("generic_http", {}) and "success_overall" in w
    )
    i, label = path
    with connect(database) as conn:
        payload = conn.execute(
            "SELECT payload FROM calculated_snapshots WHERE build_id=%s",
            (int(first["snapshot_id"]),),
        ).fetchone()[0]
        w = payload["networks"][i]["windows"][label]
        w["success_overall"]["score"] = 7.25
        w["targets"]["generic_http"]["scores"]["success"]["score"] = 4.5
        conn.execute(
            "UPDATE calculated_snapshots SET payload=%s WHERE build_id=%s",
            (Jsonb(payload), int(first["snapshot_id"])),
        )
    second = calculate(database, POLICY, as_of=end + timedelta(hours=1))
    later = store.read_head(database, second["snapshot_id"])["networks"]
    w = {n["id"]: n for n in later}[head["networks"][i]["id"]]["windows"][label]
    assert w["success_overall"]["previous_score"] == 7.25
    assert w["targets"]["generic_http"]["scores"]["success"]["previous_score"] == 4.5
