"""The Amazon product page target (amazon_pdp; BP-TARGETS, docs/browser.html)."""

import pytest
from browser_targets_cases import (
    case_id,
    cases,
    check_fixture_counts,
    check_fixture_outcome,
    context,
    url,
)

from atlas.browser import contracts as c
from atlas.browser import judge as j
from atlas.targets import registry

CASES = cases(c.AMAZON_PDP)


def test_amazon_pdp_has_its_cases():
    assert c.AMAZON_PDP in c.PAGE_TARGETS
    assert {path.stem for path in CASES} == {
        "ok",
        "ok_error_text",
        "captcha",
        "robot_check",
        "dogs_page",
        "status_503",
        "not_found",
        "no_price",
        "no_title",
        "signin_redirect",
        "empty_page",
        "real_ok",
        "real_robot_check",
        "real_used_only_buy_box",
    }


@pytest.mark.parametrize("path", CASES, ids=case_id)
def test_fixture_outcome(path):
    check_fixture_outcome(path)


@pytest.mark.parametrize("path", CASES, ids=case_id)
def test_fixture_counts_exactly_the_selectors_the_target_waits_for(path):
    check_fixture_counts(path)


def test_page_url():
    assert url(c.AMAZON_PDP, "0735211299") == "https://www.amazon.com/dp/0735211299"


DOGS_LINK = 'a[href*="/dogsofamazon"]'


def test_the_dogs_error_pages_end_the_wait():
    """Amazon's 503 and 404 pages link to the dogs gallery; waiting for that link ends the
    wait on them instead of holding the browser for the whole page timeout. The link alone
    marks no block: the 404 page of an unknown ASIN has it too (fixture not_found)."""
    spec = registry.get(c.AMAZON_PDP).page(context(0), ("0000000000",))
    assert DOGS_LINK in j.selectors(spec.wait_for)
