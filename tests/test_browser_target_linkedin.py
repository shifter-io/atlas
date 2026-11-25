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

from atlas.browser import contracts as c
from atlas.targets import registry

CASES = cases(c.LINKEDIN_PUBLIC)


def test_linkedin_public_has_its_cases():
    assert c.LINKEDIN_PUBLIC in c.PAGE_TARGETS
    assert {path.stem for path in CASES} == {
        "ok",
        "ok_login_like_slug",
        "ok_marker_words",
        "ok_sign_in_modal",
        "authwall",
        "signup_in_place",
        "checkpoint",
        "status_999",
        "unavailable",
        "empty_page",
        "no_name",
        "real_authwall_999",
        "real_ok_sign_in_modal",
        "real_no_headline",
    }


@pytest.mark.parametrize("path", CASES, ids=case_id)
def test_fixture_outcome(path):
    check_fixture_outcome(path)


@pytest.mark.parametrize("path", CASES, ids=case_id)
def test_fixture_counts_exactly_the_selectors_the_target_waits_for(path):
    check_fixture_counts(path)


def test_page_url_has_no_trailing_slash():
    """LinkedIn redirects /in/<name>/ to /in/<name> and walls most redirected visits."""
    assert url(c.LINKEDIN_PUBLIC, "example-person") == "https://www.linkedin.com/in/example-person"
    assert url(c.LINKEDIN_PUBLIC, "a/b c") == "https://www.linkedin.com/in/a%2Fb%20c"


@pytest.mark.parametrize("slug", ["loginov-example", "signup-example", "uas", "authwall-a"])
def test_a_profile_slug_that_looks_like_a_login_path_is_the_profile(slug):
    view, _ = load(FIXTURES / c.LINKEDIN_PUBLIC / "ok.json")
    profile = replace(view, final_url=f"https://www.linkedin.com/in/{slug}/")
    assert outcome(registry.get(c.LINKEDIN_PUBLIC).judge(profile)) == ("ok", None, None)


def judged(name, **changes):
    view, _ = load(FIXTURES / c.LINKEDIN_PUBLIC / f"{name}.json")
    return outcome(registry.get(c.LINKEDIN_PUBLIC).judge(replace(view, **changes)))


def test_the_security_check_is_a_challenge_by_its_path_or_its_text():
    profile = "https://www.linkedin.com/in/example-person/"
    assert judged("checkpoint", title="") == ("challenge", "challenge", "checkpoint_page")
    assert judged("checkpoint", final_url=profile) == ("challenge", "challenge", "checkpoint_page")
    assert judged("checkpoint", final_url=profile, text_head="")[2] == "checkpoint_page"


@pytest.mark.parametrize(
    "title", ["Sign Up | LinkedIn", "LinkedIn Login, Sign in | LinkedIn", " sign in | linkedin "]
)
def test_a_login_or_sign_up_page_at_the_profile_url_is_a_login_wall(title):
    assert judged("signup_in_place", title=title) == ("error", "login_wall", "authwall_page")


def test_a_login_title_beside_the_profile_is_the_profile():
    assert judged("ok", title="Sign Up | LinkedIn") == ("ok", None, None)


def test_a_shown_profile_still_counts_its_status():
    assert judged("ok_marker_words", status=999) == ("block", "blocked", "block_status")
    assert judged("ok_marker_words", status=429) == ("block", "blocked", "block_status")
    assert judged("ok_marker_words", status=407)[1:] == ("proxy_auth", "proxy_auth_status")


def test_a_999_on_the_login_page_is_the_login_wall_and_a_bare_999_a_block():
    """LinkedIn also serves its authwall and sign-up page with 999: the page decides."""
    assert judged("authwall", status=999) == ("error", "login_wall", "authwall_redirect")
    assert judged("signup_in_place", status=999) == ("error", "login_wall", "authwall_page")
    assert judged("status_999") == ("block", "blocked", "block_status")
    assert judged("checkpoint", status=999) == ("challenge", "challenge", "checkpoint_page")
    assert judged("ok", status=999, title="Sign Up | LinkedIn")[2] == "block_status"


def test_marker_words_without_the_whole_profile_still_count():
    no_headline = {".top-card-layout__headline": 0, "h1.top-card-layout__title": 1}
    assert judged("ok_marker_words", found=no_headline)[2] == "challenge_marker"


def test_a_page_with_some_text_but_no_profile_is_no_name():
    assert judged("empty_page", text_head="Example") == ("error", "no_content", "no_name")
    other = "https://www.linkedin.com/in/unavailable-example/"
    assert judged("unavailable", final_url=other) == ("error", "no_content", "no_name")
