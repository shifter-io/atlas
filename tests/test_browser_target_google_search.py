"""The Google search results page target (google_search; BP-TARGETS, docs/browser.html)."""

import pytest
from browser_targets_cases import case_id, cases, check_fixture_counts, check_fixture_outcome, url

from atlas.browser import contracts as c

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
