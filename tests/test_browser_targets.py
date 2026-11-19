"""The browser page targets' shared judge and base (BP-TARGETS; docs/browser.html). Each
target's own fixtures, URLs and rules are tested in its test_browser_target_<target>.py."""

import asyncio
import tomllib
from dataclasses import dataclass, replace

import pytest
from browser_targets_cases import (
    INPUTS,
    ROOT,
    TARGET_CASES,
    FakeDriver,
    cases,
    context,
    load,
    outcome,
)

from atlas.browser import contracts as c
from atlas.browser import judge as j
from atlas.browser.targets import PageTarget, choose
from atlas.browser.targets.pages import PAGE_TARGET_TYPES
from atlas.targets import PageView, registry

# Page targets past their first version (a changed selector or wait bumps it).
VERSIONS = {c.LINKEDIN_PUBLIC: "2"}


def test_every_page_target_has_its_cases():
    assert all(cases(target) for target in c.PAGE_TARGETS)


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


def test_page_targets_settle_at_once():
    for target_id, value in INPUTS.items():
        assert registry.get(target_id).page(context(0), (value,)).settle_ms == 0


def test_registered_targets_match_the_targets_file():
    rows = tomllib.loads((ROOT / "configs" / "targets.toml").read_text())["target"]
    urls = {row["id"]: row["url"] for row in rows}
    assert tuple(type(t) for t in map(registry.get, c.PAGE_TARGETS)) == PAGE_TARGET_TYPES
    for target_id in c.PAGE_TARGETS:
        target = registry.get(target_id)
        assert (target.id, target.engine, target.pool_eligible) == (target_id, "browser", False)
        assert target.version == VERSIONS.get(target_id, "1") and isinstance(target, PageTarget)
        assert target.build_request().url == urls[target_id]
    assert {t.id for t in registry.all() if t.engine == c.BROWSER_ENGINE} == set(c.RUN_TARGETS)


def test_other_targets_only_read_the_page():
    driver = FakeDriver(0)
    # google_search reads too, waiting only for a page read early (its own test).
    for target_id in (c.AMAZON_PDP, c.LINKEDIN_PUBLIC, c.INSTAGRAM_WEB):
        target = registry.get(target_id)
        spec = target.page(context(0), (INPUTS.get(target_id, {"site": "x", "handle": "a"}),))
        assert asyncio.run(target.interact(driver, spec)) is None
    assert driver.calls == []


def test_page_target_base_needs_its_parts():
    base = PageTarget("x", "https://www.example.com/")
    view = PageView("https://www.example.com/", 200, "", "")
    for call in (lambda: base.address("a"), lambda: base.wanted("a"), lambda: base.rules(view)):
        with pytest.raises(NotImplementedError):
            call()
    with pytest.raises(NotImplementedError):
        base.page(context(0), ("a",))


@dataclass(frozen=True, slots=True)
class Shown(PageTarget):
    """A page target whose page shows its content when `#content` is found."""

    MARKS = (j.Mark("challenge", "site_check", text=("site check",), paths=("/check/",)),)

    def shown(self, view):
        return bool(j.count(view, "#content"))

    def rules(self, view):
        return j.OK if view.title or view.text_head else j.failed("no_content", "blank")


@dataclass(frozen=True, slots=True)
class TitleRead(Shown):
    SHOWN_UNREAD = ("text_head",)


@dataclass(frozen=True, slots=True)
class ShownOk(Shown):
    SHOWN_OK = True


def test_a_shown_page_is_not_read_for_markers():
    target = Shown("x", "https://www.example.com/")
    page = PageView("https://www.example.com/", 200, "captcha", "site check", {"#content": 1})
    assert PageTarget("x", "u").shown(page) is False
    assert target.judge(replace(page, found={})).reason == "site_check"
    # Shown: the marks, the shared markers and the rules see a blank title and text...
    assert outcome(target.judge(page)) == ("error", "no_content", "blank")
    # ...only the SHOWN_UNREAD fields: here the title is still read.
    assert TitleRead("x", "u").judge(page).reason == "challenge_marker"
    # Statuses and marks by path or selector still count.
    assert target.judge(replace(page, status=403)).reason == "block_status"
    assert target.judge(replace(page, final_url="https://www.example.com/check/1")).reason == (
        "site_check"
    )
    # With SHOWN_OK the page is ok right after the 407 step.
    assert ShownOk("x", "u").judge(replace(page, status=403)) is j.OK
    assert ShownOk("x", "u").judge(replace(page, status=407)).reason == "proxy_auth_status"
    assert ShownOk("x", "u").judge(replace(page, found={})).reason == "site_check"
