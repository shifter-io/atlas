"""Every browser fixture through its own judge (BP-TGT-DOCS-1): each JSON file anywhere under
tests/fixtures/browser/, hand-written or captured, must give exactly the verdict, error and
reason it records. The test uses only the public judges, so it runs unchanged on any commit
and proves a refactor of the shared judge path left every fixture's outcome as it was. Also:
docs/browser.html names every reason a target can record."""

import ast
import re

import pytest
from browser_targets_cases import FIXTURES, ROOT, load, outcome
from test_browser_targets_ai import run

from atlas.browser import contracts as c
from atlas.browser import judge as j
from atlas.targets import registry

ALL = sorted(FIXTURES.rglob("*.json"))


def folder(path):
    """The fixture's target id (or `judge`): the first folder under tests/fixtures/browser/."""
    return path.relative_to(FIXTURES).parts[0]


def judged(path):
    """(verdict, error, reason) the fixture records, and the one its judge gives now."""
    if folder(path) in c.AI_SOURCES:
        expect, _, result, *_ = run(path)
        return (expect["verdict"], expect["error"], expect["reason"]), outcome(result)
    view, expected = load(path)
    if folder(path) == "judge":
        # The shared steps decide these before any target rule runs.
        return expected, outcome(j.judge(view, lambda _: pytest.fail("rules ran")))
    return expected, outcome(registry.get(folder(path)).judge(view))


def test_every_fixture_folder_is_a_target():
    known = {*c.PAGE_TARGETS, *c.AI_SOURCES, "judge"}
    assert {folder(path) for path in ALL} == known


@pytest.mark.parametrize("path", ALL, ids=lambda p: str(p.relative_to(FIXTURES)))
def test_fixture_outcome_unchanged(path):
    expected, actual = judged(path)
    assert actual == expected


def target_reasons():
    """The reasons the target modules name: failed(code, reason) and Mark(code, reason)."""
    reasons = set()
    for module in (ROOT / "src" / "atlas" / "browser" / "targets").glob("*.py"):
        for node in ast.walk(ast.parse(module.read_text())):
            name = getattr(node, "func", None)
            if isinstance(node, ast.Call) and getattr(name, "id", None) in {"failed", "Mark"}:
                assert len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                reasons.add(node.args[1].value)
    return reasons


def test_docs_list_every_reason():
    html = (ROOT / "docs" / "browser.html").read_text()
    (paragraph,) = re.findall(r"<p>Each failure also records a short .*?</p>", html)
    listed = set(re.findall(r"<code>([a-z_0-9]+)</code>", paragraph))
    reasons = target_reasons()
    assert {"checkpoint_page", "authwall_page", "cf_error_page", "too_many_requests"} <= reasons
    assert reasons - listed == set()
    shared = {j.PROXY_AUTH, j.CHALLENGE_MARKER, j.BLOCK_MARKER, j.BLOCK_STATUS}
    assert listed - reasons - {"ok", "reason"} == shared
