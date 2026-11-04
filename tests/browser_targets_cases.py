"""Shared helpers of the browser page target tests: test_browser_targets.py for the shared
judge and base, and one test_browser_target_<target>.py per target (BP-TARGETS)."""

import json
from pathlib import Path

from proxy_index.browser import contracts as c
from proxy_index.browser import judge as j
from proxy_index.models import RequestContext
from proxy_index.targets import PageView, registry

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "browser"
RUN_ID = "00000000-0000-4000-8000-000000000000"
EXPECT = "Example Domain"
# The input each target's fixtures were written for.
INPUTS = {
    c.AMAZON_PDP: "0000000000",
    c.GOOGLE_SEARCH: "example",
    c.LINKEDIN_PUBLIC: "example-person",
    c.CLOUDFLARE_CHALLENGE: {"url": "https://www.example.com/", "expect": EXPECT},
}


def load(path):
    document = json.loads(path.read_text())
    expect = document["expect"]
    return PageView(**document["view"]), (expect["verdict"], expect["error"], expect["reason"])


def outcome(assessment):
    return assessment.verdict, assessment.error, assessment.reason


def cases(folder):
    return sorted((FIXTURES / folder).glob("*.json"))


def case_id(path):
    return f"{path.parent.name}/{path.stem}"


TARGET_CASES = [path for target in c.PAGE_TARGETS for path in cases(target)]


def fixture_input(target, view):
    if target != c.INSTAGRAM_WEB:
        return INPUTS[target]
    site = "x" if j.host_of(view) == "x.com" else "instagram"
    return {"site": site, "handle": "example_person"}


def context(i):
    return RequestContext(RUN_ID, i, 0)


def check_fixture_outcome(path):
    view, expected = load(path)
    assert outcome(registry.get(path.parent.name).judge(view)) == expected


def check_fixture_counts(path):
    """The fixture counts exactly the selectors the target waits for."""
    target_id, (view, _) = path.parent.name, load(path)
    spec = registry.get(target_id).page(context(0), (fixture_input(target_id, view),))
    assert tuple(view.found) == j.selectors(spec.wait_for)


def url(target_id, value, i=0):
    return registry.get(target_id).page(context(i), (value,)).url


class FakeDriver:
    def __init__(self, present):
        self.present, self.calls = present, []

    async def count(self, selector):
        self.calls.append(("count", selector))
        return self.present

    async def wait_for(self, selector, timeout_ms):
        self.calls.append(("wait_for", selector, timeout_ms))
        return False
