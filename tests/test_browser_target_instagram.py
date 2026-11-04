"""The Instagram or X profile page target (instagram_web; BP-TARGETS, docs/browser.html)."""

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

CASES = cases(c.INSTAGRAM_WEB)


def test_instagram_web_has_its_cases():
    assert c.INSTAGRAM_WEB in c.PAGE_TARGETS
    assert {path.stem for path in CASES} == {
        "instagram_ok",
        "instagram_login_dialog",
        "x_ok",
        "x_login_flow",
        "no_posts",
        "instagram_reels_only",
        "instagram_post_text_with_challenge_words",
        "x_post_text_with_block_words",
        "x_rate_limit_without_posts",
        "instagram_wait_page",
        "instagram_checkpoint",
    }


@pytest.mark.parametrize("path", CASES, ids=case_id)
def test_fixture_outcome(path):
    check_fixture_outcome(path)


@pytest.mark.parametrize("path", CASES, ids=case_id)
def test_fixture_counts_exactly_the_selectors_the_target_waits_for(path):
    check_fixture_counts(path)


def test_page_urls():
    profile = {"site": "instagram", "handle": "example.person"}
    assert url(c.INSTAGRAM_WEB, profile) == "https://www.instagram.com/example.person/"
    assert url(c.INSTAGRAM_WEB, {"site": "x", "handle": "example"}) == "https://x.com/example"


def test_a_profile_path_that_looks_like_a_login_path_is_the_profile():
    view, _ = load(FIXTURES / c.INSTAGRAM_WEB / "instagram_ok.json")
    for path in ("/loginov-example/", "/accounts/loginov/", "/i/flow/logins"):
        moved = replace(view, final_url=f"https://www.instagram.com{path}")
        assert outcome(registry.get(c.INSTAGRAM_WEB).judge(moved)) == ("ok", None, None)


def test_a_profile_named_like_the_checkpoint_is_the_profile():
    view, _ = load(FIXTURES / c.INSTAGRAM_WEB / "x_ok.json")
    for path in ("/challenge", "/challenges"):
        moved = replace(view, final_url=f"https://x.com{path}")
        assert outcome(registry.get(c.INSTAGRAM_WEB).judge(moved)) == ("ok", None, None)


def test_post_text_reads_as_a_block_page_while_the_profile_is_missing():
    view, _ = load(FIXTURES / c.INSTAGRAM_WEB / "x_post_text_with_block_words.json")
    no_header = replace(view, found={**view.found, '[data-testid="UserName"]': 0})
    assert outcome(registry.get(c.INSTAGRAM_WEB).judge(no_header)) == (
        "block",
        "blocked",
        "block_marker",
    )
