"""The Google search results page target (google_search; BP-TARGETS, docs/browser.html)."""

import asyncio

import pytest
from browser_targets_cases import (
    FakeDriver,
    case_id,
    cases,
    check_fixture_counts,
    check_fixture_outcome,
    context,
    url,
)

from atlas.browser import contracts as c
from atlas.browser.targets.pages import GoogleSearch

CASES = cases(c.GOOGLE_SEARCH)


def test_google_search_has_its_cases():
    assert c.GOOGLE_SEARCH in c.PAGE_TARGETS
    assert {path.stem for path in CASES} == {
        "ok",
        "sorry",
        "few_results",
        "consent",
        "consent_form",
        "consent_banner_with_results",
        "login_redirect",
        "empty_page",
        "no_results",
        "real_ok",
        "real_sorry_page",
    }


@pytest.mark.parametrize("path", CASES, ids=case_id)
def test_fixture_outcome(path):
    check_fixture_outcome(path)


@pytest.mark.parametrize("path", CASES, ids=case_id)
def test_fixture_counts_exactly_the_selectors_the_target_waits_for(path):
    check_fixture_counts(path)


def test_page_url():
    assert url(c.GOOGLE_SEARCH, "best coffee & tea") == (
        "https://www.google.com/search?q=best+coffee+%26+tea&hl=en&gl=us"
    )


@pytest.mark.parametrize(("present", "waits"), [(0, False), (1, True), (2, True), (3, False)])
def test_a_page_read_early_waits_for_its_third_organic_link(present, waits):
    target = GoogleSearch()
    driver = FakeDriver(present)
    assert asyncio.run(target.interact(driver, target.page(context(0), ("query",)))) is None
    expected = [("wait_for", ":nth-match(#search a h3, 3)", 5000)] if waits else []
    assert [call for call in driver.calls if call[0] == "wait_for"] == expected
