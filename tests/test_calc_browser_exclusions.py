"""CHROME-STALE-EXCLUDE-1 end to end: a run the methodology's [[browser_exclusions]] name is in
no figure of a build, in the full calculation and the incremental one alike, and every other run
counts as before (unit cases: test_browser_exclusions.py)."""

import re
from datetime import timedelta

import pytest
from calculated_browser_cases import generic
from calculated_cases import lenient_policy
from test_calc_network_target_scores import PATTERNS, REPEAT, TARGETS, add_runs

from atlas.calculated.calculator import calculate
from atlas.storage import connect

pytest_plugins = ["test_storage", "calculated_browser_cases"]

# The start of one network's amazon_pdp run (Camoufox, as the fixture's runs).
STARTED = (
    "SELECT r.started_at FROM test_runs r JOIN scope_stats s USING(scope_id) JOIN products p "
    "ON p.product_id=s.product_id WHERE p.provider=%s AND s.target=%s"
)
ENTRY = re.compile(r"^  \{ target = .*\},$", re.M)


def excluding(tmp_path, started):
    """The lenient methodology whose one exclusion holds the run that started at `started`."""
    policy = lenient_policy(tmp_path)
    second = timedelta(seconds=1)
    entry = (
        f'  {{ target = "amazon_pdp", engine = "camoufox", started_from = '
        f'"{(started - second).isoformat()}", started_before = "{(started + second).isoformat()}"'
        ', reason = "test" },'
    )
    text, found = ENTRY.subn(entry, policy.read_text())
    assert found == 1
    out = tmp_path / "excluding.toml"
    out.write_text(text)
    return out


def figures(payload):
    """Each network's browser target figures per window: {(provider, window, target): (completed,
    successes)} of its main engine."""
    found = {}
    for entry in payload["networks"]:
        for label, window in entry["windows"].items():
            for target in TARGETS:
                if own := window["targets"].get(target):
                    engine = own["browser"]["engines"][own["browser"]["engine"]]
                    found[entry["provider"], label, target] = (
                        engine["completed"],
                        engine["successes"],
                    )
    return found


@pytest.mark.asyncio
async def test_both_modes_leave_out_the_excluded_run_and_only_it(database, tmp_path):
    as_of = await generic(database, tmp_path)
    await add_runs(database, tmp_path)
    with connect(database) as conn:
        (started,) = conn.execute(STARTED, ("alpha", "amazon_pdp")).fetchone()
    policy = lenient_policy(tmp_path)
    before = figures(calculate(database, policy, as_of=as_of))
    assert len(before) == len(PATTERNS) * 3 * len(TARGETS)
    assert set(before.values()) == {(10 * REPEAT, 9 * REPEAT)} | {
        (10 * REPEAT, 6 * REPEAT),
        (10 * REPEAT, 3 * REPEAT),
    }
    out = excluding(tmp_path, started)
    expected = {k: v for k, v in before.items() if k[0] != "alpha" or k[2] != "amazon_pdp"}
    for mode in ("full", "incremental"):
        payload = calculate(database, out, as_of=as_of, mode=mode)
        assert figures(payload) == expected, mode
        alpha = next(n for n in payload["networks"] if n["provider"] == "alpha")
        for label, window in alpha["windows"].items():
            assert "amazon_pdp" in window["success_overall"]["missing_targets"], (mode, label)
