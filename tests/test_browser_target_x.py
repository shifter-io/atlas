"""The X profile page target (x_web; X-TARGET-1, docs/browser.html)."""

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
from atlas.browser.targets.x import XWeb
from atlas.targets import registry

CASES = cases(c.X_WEB)


def judge(view):
    return outcome(registry.get(c.X_WEB).judge(view))


def test_x_web_has_its_cases():
    assert c.X_WEB in c.PAGE_TARGETS
    assert {path.stem for path in CASES} == {
        "ok",
        "older_app_ok",
        "login_flow",
        "login_sheet",
        "no_posts",
        "no_profile",
        "post_text_with_block_words",
        "rate_limit_without_posts",
    }


@pytest.mark.parametrize("path", CASES, ids=case_id)
def test_fixture_outcome(path):
    check_fixture_outcome(path)


@pytest.mark.parametrize("path", CASES, ids=case_id)
def test_fixture_counts_exactly_the_selectors_the_target_waits_for(path):
    check_fixture_counts(path)


def test_page_urls():
    assert url(c.X_WEB, "example") == "https://x.com/example"
    assert url(c.X_WEB, "Example_1") == "https://x.com/Example_1"


def test_a_profile_path_that_looks_like_a_login_path_is_the_profile():
    view, _ = load(FIXTURES / c.X_WEB / "ok.json")
    for path in ("/loginov", "/i/flow/logins", "/challenge"):
        assert judge(replace(view, final_url=f"https://x.com{path}")) == ("ok", None, None)


def test_the_login_page_is_a_login_wall():
    view, _ = load(FIXTURES / c.X_WEB / "no_profile.json")
    for path in ("/login", "/i/flow/login", "/i/jf/onboarding/web"):
        moved = replace(view, final_url=f"https://x.com{path}")
        assert judge(moved) == ("error", "login_wall", "login_redirect")


def test_post_text_reads_as_a_block_page_while_the_profile_is_missing():
    view, _ = load(FIXTURES / c.X_WEB / "post_text_with_block_words.json")
    no_header = replace(view, found={**view.found, **dict.fromkeys(XWeb.HEADERS, 0)})
    assert judge(no_header) == ("block", "blocked", "block_marker")
