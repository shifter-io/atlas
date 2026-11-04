"""The Amazon product page target (amazon_pdp; BP-TARGETS, docs/browser.html)."""

import pytest
from browser_targets_cases import case_id, cases, check_fixture_counts, check_fixture_outcome, url

from proxy_index.browser import contracts as c

CASES = cases(c.AMAZON_PDP)


def test_amazon_pdp_has_its_cases():
    assert c.AMAZON_PDP in c.PAGE_TARGETS
    assert {path.stem for path in CASES} == {"ok", "captcha", "dogs_page", "no_price", "status_503"}


@pytest.mark.parametrize("path", CASES, ids=case_id)
def test_fixture_outcome(path):
    check_fixture_outcome(path)


@pytest.mark.parametrize("path", CASES, ids=case_id)
def test_fixture_counts_exactly_the_selectors_the_target_waits_for(path):
    check_fixture_counts(path)


def test_page_url():
    assert url(c.AMAZON_PDP, "0735211299") == "https://www.amazon.com/dp/0735211299"
