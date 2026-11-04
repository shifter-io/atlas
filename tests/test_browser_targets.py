"""The five browser page targets and their shared judge (BP-TARGETS; docs/browser.html)."""

import asyncio
import json
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from proxy_index.browser import contracts as c
from proxy_index.browser import judge as j
from proxy_index.browser.targets import PageTarget, choose
from proxy_index.browser.targets.pages import PAGE_TARGET_TYPES, CloudflareChallenge
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


TARGET_CASES = [path for target in c.PAGE_TARGETS for path in cases(target)]


def fixture_input(target, view):
    if target != c.INSTAGRAM_WEB:
        return INPUTS[target]
    site = "x" if j.host_of(view) == "x.com" else "instagram"
    return {"site": site, "handle": "example_person"}


def context(i):
    return RequestContext(RUN_ID, i, 0)


def test_every_target_has_its_cases():
    names = {target: {path.stem for path in cases(target)} for target in c.PAGE_TARGETS}
    assert names == {
        c.AMAZON_PDP: {"ok", "captcha", "dogs_page", "no_price", "status_503"},
        c.GOOGLE_SEARCH: {
            "ok",
            "sorry",
            "few_results",
            "consent",
            "consent_form",
            "consent_banner_with_results",
        },
        c.LINKEDIN_PUBLIC: {"ok", "ok_login_like_slug", "authwall", "status_999", "no_name"},
        c.INSTAGRAM_WEB: {
            "instagram_ok",
            "instagram_login_dialog",
            "x_ok",
            "x_login_flow",
            "no_posts",
        },
        c.CLOUDFLARE_CHALLENGE: {
            "cleared",
            "still_challenged",
            "block_1020",
            "cleared_without_expect",
        },
    }


@pytest.mark.parametrize("path", TARGET_CASES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_fixture_outcome(path):
    view, expected = load(path)
    assert outcome(registry.get(path.parent.name).judge(view)) == expected


@pytest.mark.parametrize("path", TARGET_CASES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_fixture_counts_exactly_the_selectors_the_target_waits_for(path):
    target_id, (view, _) = path.parent.name, load(path)
    spec = registry.get(target_id).page(context(0), (fixture_input(target_id, view),))
    assert tuple(view.found) == j.selectors(spec.wait_for)


@pytest.mark.parametrize("path", cases("judge"), ids=lambda p: p.stem)
def test_shared_judge_cases(path):
    view, expected = load(path)
    # The shared steps decide these before any target rule runs.
    assert outcome(j.judge(view, lambda _: pytest.fail("rules ran"))) == expected


def test_judge_order():
    view = PageView("https://www.example.com/", 200, "Example", "Example text")
    assert j.judge(view, lambda _: j.OK) is j.OK
    rules = lambda _: j.failed("no_content", "no_thing")  # noqa: E731
    assert outcome(j.judge(view, rules)) == ("error", "no_content", "no_thing")
    for status in (403, 429, 451):
        blocked = replace(view, status=status)
        assert outcome(j.judge(blocked, rules)) == ("block", "blocked", "block_status")
    assert j.judge(replace(view, status=999), rules).reason == "no_thing"
    assert j.judge(replace(view, status=999), rules, block_statuses={999}).reason == "block_status"
    # Challenge markers win over block markers, the 407 over both; site marks come first.
    both = replace(view, title="Access denied", text_head="please complete the captcha")
    assert j.judge(both, rules).reason == "challenge_marker"
    assert j.judge(replace(both, status=407), rules).reason == "proxy_auth_status"
    mark = j.Mark("blocked", "site_block", text=("access denied",))
    assert outcome(j.judge(both, rules, (mark,))) == ("block", "blocked", "site_block")
    block = replace(view, text_head="You have been blocked")
    assert j.judge(block, rules).reason == "block_marker"
    # Markers are read in the title and in the whole visible text head.
    late = replace(view, title="x" * 5000, text_head="y" * 4000 + " rate limit")
    assert j.judge(late, rules).reason == "block_marker"


def test_marks():
    view = PageView("https://www.example.com/Errors/Check?x=1", 200, "Title", "Body", {"#a": 2})
    assert j.Mark("challenge", "r", paths=("/errors/",)).seen(view)
    assert j.Mark("challenge", "r", found=("#a",)).seen(view)
    assert j.Mark("challenge", "r", text=("body",)).seen(view)
    assert not j.Mark("challenge", "r", text=("other",), paths=("/x",), found=("#b",)).seen(view)
    # Paths match as prefixes of the final path, never inside it (a slug may contain "login").
    assert not j.Mark("challenge", "r", paths=("/check",)).seen(view)
    assert not j.path_has(
        replace(view, final_url="https://www.example.com/in/loginov-a/"), ["/login"]
    )
    for path, fragment, hit in [
        ("/login?x=/in/", "/login", True),
        ("/login/", "/login", True),
        ("/signup/cold-join", "/signup", True),
        ("/uas/login", "/uas/", True),
        ("/sorry/index", "/sorry/", True),
        ("/Errors/ValidateCaptcha", "/errors/validatecaptcha", True),
        ("/loginov", "/login", False),
        ("/in/uas/", "/uas/", False),
        ("/sorryx/", "/sorry/", False),
    ]:
        moved = replace(view, final_url=f"https://www.example.com{path}")
        assert j.path_has(moved, [fragment]) is hit, path
    assert j.count(view, "#a", "#b") == 2 and j.host_of(view) == "www.example.com"
    assert j.host_of(replace(view, final_url="about:blank")) == ""


def test_judge_never_returns_ok_with_an_error():
    with pytest.raises(ValueError, match="verdict"):
        j.PageAssessment(verdict="ok", error="no_content")
    with pytest.raises(ValueError, match="verdict"):
        j.PageAssessment(verdict="block", error="challenge")
    with pytest.raises(ValueError, match="verdict"):
        j.PageAssessment(verdict="error")
    with pytest.raises(ValueError, match="reason"):
        j.PageAssessment(verdict="ok", reason="Not An Enum")
    with pytest.raises(ValueError, match="error code"):
        j.failed("http_status", "x")
    for code, verdict in c.CODE_VERDICTS.items():
        assert outcome(j.failed(code, "why")) == (verdict, code, "why")
    for path in [*TARGET_CASES, *cases("judge")]:
        verdict, error, reason = load(path)[1]
        assert (verdict == "ok") == (error is None) == (reason is None)


def test_every_target_judges_any_view_consistently():
    texts = ("", "Example", "just a moment", "you have been blocked", "type the characters you see")
    urls = ("https://www.example.com/", "https://www.example.com/authwall", "about:blank")
    for target in (registry.get(target_id) for target_id in c.PAGE_TARGETS):
        keys = {key for path in cases(target.id) for key in load(path)[0].found}
        for status in (None, 200, 404, 503, 999):
            for text in texts:
                for url in urls:
                    for n in (0, 1, 5):
                        view = PageView(url, status, text, text, dict.fromkeys(keys, n))
                        result = target.judge(view)
                        assert (result.verdict == "ok") == (result.error is None)
                        assert result.error is None or result.error in c.CODE_VERDICTS


def test_selectors():
    assert j.selectors(None) == j.selectors("") == j.selectors(" , ") == ()
    assert j.selectors("#a") == ("#a",)
    assert j.selectors(" #a , div[title=\"x, y\"] , :is(a, b) , 'c,d' ") == (
        "#a",
        'div[title="x, y"]',
        ":is(a, b)",
        "'c,d'",
    )
    assert j.selectors('a:has-text("say \\"hi, you\\"")') == ('a:has-text("say \\"hi, you\\"")',)
    parts = ("#a", j.text_selector('Example, "quoted"\n text'), "#b")
    assert j.selectors(j.selector_list(*parts)) == parts
    assert j.text_selector(" Example \t Domain ") == 'body:has-text("Example Domain")'
    assert j.text_selector("Grüße").endswith('"Grüße")')


def test_page_rotation_is_deterministic():
    inputs = ("A000000001", "A000000002", "A000000003")
    target = registry.get(c.AMAZON_PDP)
    for i in range(10):
        spec = target.page(context(i), inputs)
        assert spec.url == f"https://www.amazon.com/dp/{inputs[i % 3]}"
        assert target.try_fields(context(i), inputs) == {"input": inputs[i % 3], "source": None}
        assert choose(context(i), inputs) == inputs[i % 3]
    with pytest.raises(ValueError, match="inputs"):
        target.page(context(0), ())


def test_page_urls():
    def url(target_id, value, i=0):
        return registry.get(target_id).page(context(i), (value,)).url

    assert url(c.AMAZON_PDP, "0735211299") == "https://www.amazon.com/dp/0735211299"
    assert url(c.GOOGLE_SEARCH, "best coffee & tea") == (
        "https://www.google.com/search?q=best+coffee+%26+tea&hl=en&gl=us"
    )
    assert url(c.LINKEDIN_PUBLIC, "example-person") == "https://www.linkedin.com/in/example-person/"
    profile = {"site": "instagram", "handle": "example.person"}
    assert url(c.INSTAGRAM_WEB, profile) == "https://www.instagram.com/example.person/"
    assert url(c.INSTAGRAM_WEB, {"site": "x", "handle": "example"}) == "https://x.com/example"
    page = {"url": "https://www.example.com/page?a=1", "expect": EXPECT}
    assert url(c.CLOUDFLARE_CHALLENGE, page) == "https://www.example.com/page?a=1"
    spec = registry.get(c.CLOUDFLARE_CHALLENGE).page(context(0), (page,))
    assert j.selectors(spec.wait_for)[0] == 'body:has-text("Example Domain")'
    for target_id, value in INPUTS.items():
        assert registry.get(target_id).page(context(0), (value,)).settle_ms == 0


def test_registered_targets_match_the_targets_file():
    rows = tomllib.loads((ROOT / "configs" / "targets.toml").read_text())["target"]
    urls = {row["id"]: row["url"] for row in rows}
    assert tuple(type(t) for t in map(registry.get, c.PAGE_TARGETS)) == PAGE_TARGET_TYPES
    for target_id in c.PAGE_TARGETS:
        target = registry.get(target_id)
        assert (target.id, target.engine, target.pool_eligible) == (target_id, "browser", False)
        assert target.version == "1" and isinstance(target, PageTarget)
        assert target.build_request().url == urls[target_id]
    assert {t.id for t in registry.all() if t.engine == c.BROWSER_ENGINE} == set(c.PAGE_TARGETS)


class FakeDriver:
    def __init__(self, present):
        self.present, self.calls = present, []

    async def count(self, selector):
        self.calls.append(("count", selector))
        return self.present

    async def wait_for(self, selector, timeout_ms):
        self.calls.append(("wait_for", selector, timeout_ms))
        return False


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


def test_other_targets_only_read_the_page():
    driver = FakeDriver(0)
    for target_id in (c.AMAZON_PDP, c.GOOGLE_SEARCH, c.LINKEDIN_PUBLIC, c.INSTAGRAM_WEB):
        target = registry.get(target_id)
        spec = target.page(context(0), (INPUTS.get(target_id, {"site": "x", "handle": "a"}),))
        assert asyncio.run(target.interact(driver, spec)) is None
    assert driver.calls == []


@pytest.mark.parametrize("slug", ["loginov-example", "signup-example", "uas", "authwall-a"])
def test_a_profile_slug_that_looks_like_a_login_path_is_the_profile(slug):
    view, _ = load(FIXTURES / c.LINKEDIN_PUBLIC / "ok.json")
    profile = replace(view, final_url=f"https://www.linkedin.com/in/{slug}/")
    assert outcome(registry.get(c.LINKEDIN_PUBLIC).judge(profile)) == ("ok", None, None)
    view, _ = load(FIXTURES / c.INSTAGRAM_WEB / "instagram_ok.json")
    for path in ("/loginov-example/", "/accounts/loginov/", "/i/flow/logins"):
        moved = replace(view, final_url=f"https://www.instagram.com{path}")
        assert outcome(registry.get(c.INSTAGRAM_WEB).judge(moved)) == ("ok", None, None)


def test_page_target_base_needs_its_parts():
    base = PageTarget("x", "https://www.example.com/")
    view = PageView("https://www.example.com/", 200, "", "")
    for call in (lambda: base.address("a"), lambda: base.wanted("a"), lambda: base.rules(view)):
        with pytest.raises(NotImplementedError):
            call()
    with pytest.raises(NotImplementedError):
        base.page(context(0), ("a",))
