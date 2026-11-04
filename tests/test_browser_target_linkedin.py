"""The LinkedIn public profile page target (linkedin_public; BP-TARGETS, docs/browser.html)."""

from dataclasses import replace

import pytest
from browser_targets_cases import (
    FIXTURES,
    case_id,
    cases,
    check_fixture_counts,
    check_fixture_outcome,
    load,
    outcome,
    url,
)

from proxy_index.browser import contracts as c
from proxy_index.targets import registry

CASES = cases(c.LINKEDIN_PUBLIC)


def test_linkedin_public_has_its_cases():
    assert c.LINKEDIN_PUBLIC in c.PAGE_TARGETS
    assert {path.stem for path in CASES} == {
        "ok",
        "ok_login_like_slug",
        "authwall",
        "status_999",
        "no_name",
    }


@pytest.mark.parametrize("path", CASES, ids=case_id)
def test_fixture_outcome(path):
    check_fixture_outcome(path)


@pytest.mark.parametrize("path", CASES, ids=case_id)
def test_fixture_counts_exactly_the_selectors_the_target_waits_for(path):
    check_fixture_counts(path)


def test_page_url():
    assert url(c.LINKEDIN_PUBLIC, "example-person") == "https://www.linkedin.com/in/example-person/"


@pytest.mark.parametrize("slug", ["loginov-example", "signup-example", "uas", "authwall-a"])
def test_a_profile_slug_that_looks_like_a_login_path_is_the_profile(slug):
    view, _ = load(FIXTURES / c.LINKEDIN_PUBLIC / "ok.json")
    profile = replace(view, final_url=f"https://www.linkedin.com/in/{slug}/")
    assert outcome(registry.get(c.LINKEDIN_PUBLIC).judge(profile)) == ("ok", None, None)
