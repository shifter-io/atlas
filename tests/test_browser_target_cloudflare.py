"""The Cloudflare managed challenge target (cloudflare_challenge; BP-TARGETS, docs/browser.html)."""

import asyncio
from dataclasses import replace

import pytest
from browser_targets_cases import (
    EXPECT,
    FakeDriver,
    case_id,
    cases,
    check_fixture_counts,
    check_fixture_outcome,
    context,
    outcome,
    url,
)

from proxy_index.browser import contracts as c
from proxy_index.browser import judge as j
from proxy_index.browser.targets.cloudflare import CloudflareChallenge
from proxy_index.targets import PageView, registry

CASES = cases(c.CLOUDFLARE_CHALLENGE)


def test_cloudflare_challenge_has_its_cases():
    assert c.CLOUDFLARE_CHALLENGE in c.PAGE_TARGETS
    assert {path.stem for path in CASES} == {
        "cleared",
        "still_challenged",
        "block_1020",
        "cleared_without_expect",
        "cleared_after_403",
        "cleared_page_says_captcha",
        "challenge_orchestrated",
        "block_1020_page",
        "block_1015_rate_limit",
        "origin_522",
        "empty_page",
    }


@pytest.mark.parametrize("path", CASES, ids=case_id)
def test_fixture_outcome(path):
    check_fixture_outcome(path)


@pytest.mark.parametrize("path", CASES, ids=case_id)
def test_fixture_counts_exactly_the_selectors_the_target_waits_for(path):
    check_fixture_counts(path)


def test_page_url():
    page = {"url": "https://www.example.com/page?a=1", "expect": EXPECT}
    assert url(c.CLOUDFLARE_CHALLENGE, page) == "https://www.example.com/page?a=1"
    spec = registry.get(c.CLOUDFLARE_CHALLENGE).page(context(0), (page,))
    assert j.selectors(spec.wait_for)[0] == 'body:has-text("Example Domain")'


def test_cloudflare_waits_for_the_challenge_to_clear():
    target = registry.get(c.CLOUDFLARE_CHALLENGE)
    page = {"url": "https://www.example.com/", "expect": EXPECT}
    spec = target.page(context(0), (page,))
    expect = 'body:has-text("Example Domain")'
    shown = FakeDriver(1)
    assert asyncio.run(target.interact(shown, spec)) is None
    assert shown.calls == [("count", expect)]
    hidden = FakeDriver(0)
    asyncio.run(target.interact(hidden, spec))
    assert hidden.calls[1] == ("wait_for", f"{expect}, #cf-error-details", 30_000)
    bound = replace(target, challenge_wait_s=5)
    assert isinstance(bound, CloudflareChallenge) and bound.id == target.id
    hidden = FakeDriver(0)
    asyncio.run(bound.interact(hidden, spec))
    assert hidden.calls[1][2] == 5_000



PAGE = {"url": "https://www.example.com/", "expect": EXPECT}
SHOWN = {'body:has-text("Example Domain")': 1}


def judged(status, title, **found):
    """The outcome of a page with this status and title (also its text) and these counts."""
    target = registry.get(c.CLOUDFLARE_CHALLENGE)
    counts = dict.fromkeys(j.selectors(target.page(context(0), (PAGE,)).wait_for), 0)
    view = PageView("https://www.example.com/", status, title, title, counts | found)
    return outcome(target.judge(view))


def test_a_cloudflare_sign_or_a_407_wins_over_the_expected_text():
    stage = judged(403, "Example Domain", **SHOWN, **{"#challenge-stage": 1})
    assert stage == ("challenge", "challenge", "challenge_page")
    blocked = judged(403, "Sorry, you have been blocked. Example Domain", **SHOWN)
    assert blocked == ("block", "blocked", "cf_block")
    proxy = judged(407, "Example Domain", **SHOWN)
    assert proxy == ("proxy_error", "proxy_auth", "proxy_auth_status")


def test_the_challenge_status_counts_only_until_the_expected_text_shows():
    assert judged(403, "Example Domain") == ("block", "blocked", "block_status")
    assert judged(403, "Example Domain", **SHOWN) == ("ok", None, None)
