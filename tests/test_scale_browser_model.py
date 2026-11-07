"""BROWSER-MODEL-1: modelled browser pulse bundles from BP-VERIFY-2's measured numbers (#147)."""

import importlib
import json
import sys
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from orchestrator_cases import prepare, query

from proxy_index.browser import contracts as bc
from proxy_index.orchestrator import browser_ingest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scale"))
model = importlib.import_module("browser_model")

pytest_plugins = ["test_storage"]

SLOT = datetime(2026, 10, 1, 12, tzinfo=UTC)
# #147's hour: target -> (loads, mean MB, ok, challenge, block, timeout).
TABLE = {
    "amazon_pdp": (100, 4.51, 93, 0, 0, 7),
    "google_search": (100, 1.52, 39, 56, 0, 5),
    "linkedin_public": (100, 1.35, 12, 0, 0, 0),
    "instagram_web": (100, 3.69, 48, 0, 0, 1),
    "cloudflare_challenge": (100, 3.01, 29, 3, 20, 1),
    "ai_overview": (34, 1.66, 0, 23, 0, 0),
    "ai_chatgpt": (33, 2.16, 0, 0, 0, 2),
    "ai_gemini": (33, 4.16, 31, 0, 0, 1),
}
VERDICTS = ("ok", "challenge", "block", "timeout")


def planned_run(batch_id=1, provider="alpha"):
    return {
        "batch_id": batch_id,
        "product_id": 1,
        "country": "US",
        "provider": provider,
        "product": "residential",
        "scheduled_for": SLOT,
        "start_at": SLOT + timedelta(minutes=1),
    }


def opened(path, work):
    """The bundle as browser ingest opens and validates it: (summary, manifest, tries)."""
    work.mkdir()
    path.replace(work / "bundle.tar.gz")
    summary, manifest = browser_ingest._open_bundle(work, 1 << 30)
    lines = (work / "browser" / "tries.jsonl").read_text().splitlines()
    return summary, manifest, [json.loads(line) for line in lines]


def test_settings_are_the_measured_targets_in_order(tmp_path):
    settings = model.load(model.settings(), tmp_path)
    assert settings.enabled and settings.schedule.countries == ("US",)
    assert settings.targets_for_planner() == tuple((t, TABLE[t][0], 60) for t in TABLE)
    assert {t.every_minutes for t in model.load(model.settings(120), tmp_path).targets} == {120}
    assert sum(row[0] for row in TABLE.values()) == 600


def test_a_bundle_passes_the_ingest_checks(tmp_path):
    path = model.bundle(planned_run(7), "amazon_pdp", 3, tmp_path / "outbox")
    summary, manifest, _ = opened(path, tmp_path / "work")
    state = {"target_id": "amazon_pdp", "batch_id": 7, "country_code": "US"}
    node = {"provider": "alpha", "product": "residential"}
    browser_ingest._check(summary, manifest, summary["run_id"], state, node)
    assert manifest["host"] == model.HOST and manifest["slot"] == SLOT.isoformat()
    assert summary[model.MARKER] == "BP-VERIFY-2"
    assert summary["started_at"].startswith("2026-10-01T12:01:")


@pytest.mark.parametrize("target", TABLE)
def test_bytes_and_outcomes_per_target_match_the_measured_hour(tmp_path, target):
    loads, mean, *counts = TABLE[target]
    path = model.bundle(planned_run(), target, 11, tmp_path / "outbox")
    summary, manifest, tries = opened(path, tmp_path / "work")
    assert manifest["tries"] == len(tries) == summary["completed"] == loads
    verdicts = manifest["outcomes"]["verdicts"]
    assert tuple(verdicts[name] for name in VERDICTS) == tuple(counts)
    total = sum(entry["bytes_down_est"] for entry in tries)
    assert summary["bytes_down"] == total
    assert abs(total - mean * 1e6 * loads) <= 0.01 * mean * 1e6 * loads
    sizes = {entry["bytes_down_est"] for entry in tries}
    assert len(sizes) > 1 and max(sizes) < 2 * mean * 1e6  # drawn around the mean


def test_the_same_seed_draws_the_same_tries():
    assert model.plan("google_search", "1:2:3") == model.plan("google_search", "1:2:3")
    assert model.plan("google_search", "1:2:3") != model.plan("google_search", "1:2:4")


def test_one_hour_writes_the_rows_of_two_networks(database, tmp_path):
    prepare(database, ("US",))
    settings_rows = "SELECT key,value FROM settings ORDER BY key"
    before = query(database, settings_rows)
    networks = Namespace(networks=("alpha", "beta"), seed=5)
    formulas = [{"product": "residential"}, {}]
    steps = Namespace(args=Namespace(database=database), model=Namespace(settings=networks))
    steps.model.formulas = formulas
    state = {}
    part, done = model.browser_step(steps, 1, SLOT, state)
    assert done and part["failed"] == 0, part["failures"]
    assert part["planned"] == 8 and part["runs"] == 16 and part["loads"] == 1200
    assert part["rows"] == {
        "test_batches": 8,
        "batch_runs": 16,
        "test_runs": 16,
        "run_buckets": 16,
        "ip_observations": 0,
        "run_geo_buckets": 0,
    }
    statuses = query(database, "SELECT DISTINCT status FROM batch_runs")
    assert statuses == [("done",)]
    [(completed, down, challenges, blocks, timeouts)] = query(
        database,
        "SELECT sum(completed),sum(bytes_down),sum(challenges),sum(blocks),sum(timeouts) "
        "FROM run_buckets",
    )
    assert (completed, down) == (1200, part["bytes"])
    assert abs(part["bytes"] - 2 * 1.674e9) <= 0.01 * 2 * 1.674e9
    assert (challenges, blocks, timeouts) == (2 * 82, 2 * 20, 2 * 17)
    hosts = query(
        database, "SELECT DISTINCT host FROM nodes WHERE region_id=%s", (bc.BROWSER_REGION_ID,)
    )
    assert hosts == [(model.HOST,)]
    assert query(database, settings_rows) == before  # no settings row, browser.enabled as it was
    assert len(state["uploaded"]) == 16
    again, done = model.browser_step(steps, 1, SLOT, state)
    assert done and again["runs"] == 0 and again["rows"] == part["rows"]
