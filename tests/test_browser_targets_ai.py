"""The three AI answer sources (BP-AI-TARGETS; docs/browser.html, "What counts as success").

A scripted fake PageDriver replays each fixture's page states on a fake clock, so a 30-second
wait takes no real time. Every fixture answer carries MARKER, which must never leave interact().
"""

import asyncio
import json
import logging
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from proxy_index.browser import contracts as c
from proxy_index.browser import judge as j
from proxy_index.browser.settings import INPUT_SHAPES, load_settings
from proxy_index.browser.targets import PageTarget
from proxy_index.browser.targets.ai import (
    AI_TARGET_TYPES,
    POLL_S,
    AiOverview,
    AskSpec,
    sentence_count,
)
from proxy_index.errors import UserError
from proxy_index.models import RequestContext
from proxy_index.targets import BrowserTarget, PageSpec, PageView, registry

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "browser"
EXAMPLE = ROOT / "configs" / "browser-pulse.example.toml"
RUN_ID = "00000000-0000-4000-8000-000000000000"
MARKER = "PRIVATE-ANSWER-MARKER"
QUESTION = "Why is the sky blue?"


class FakeClock:
    """Whole milliseconds, so a scripted state at t = 3.0 is reached exactly."""

    def __init__(self):
        self.ms = 0

    def __call__(self):
        return self.ms / 1000

    async def sleep(self, seconds):
        self.ms += round(seconds * 1000)


class ScriptedDriver:
    """A PageDriver over a fixture: the loaded page, then timeline entries that apply from
    their time `t` (seconds since the start, or since Enter was pressed when after_submit)."""

    def __init__(self, document, clock):
        self.page, self.timeline, self.clock = document["page"], document["timeline"], clock
        self.calls, self.clicked, self.submitted_ms = [], set(), None

    def state(self):
        page = self.page
        state = {**page, "counts": dict(page["counts"]), "texts": dict(page["texts"])}
        for entry in self.timeline:
            start = self.submitted_ms if entry.get("after_submit") else 0
            if start is None or self.clock.ms < start + round(entry["t"] * 1000):
                continue
            state["counts"].update(entry.get("counts", {}))
            state["texts"].update(entry.get("texts", {}))
            state.update({k: entry[k] for k in ("url", "title", "text_head") if k in entry})
        return state

    async def count(self, selector):
        counts = self.state()["counts"]
        return sum(counts.get(s, 0) for s in j.selectors(selector) if s not in self.clicked)

    async def text(self, selector):
        return self.state()["texts"].get(selector, "")

    async def url(self):
        return self.state()["url"]

    async def title(self):
        return self.state()["title"]

    async def wait_for(self, selector, timeout_ms):
        self.calls.append(("wait_for", selector, timeout_ms))
        end = self.clock.ms + timeout_ms
        while not await self.count(selector):
            if self.clock.ms >= end:
                return False
            self.clock.ms += 100
        return True

    async def click(self, selector):
        self.calls.append(("click", selector))
        self.clicked.add(selector)

    async def fill(self, selector, value):
        assert await self.count(selector), "fill on a missing prompt"
        self.calls.append(("fill", selector, value))

    async def press(self, selector, key):
        self.calls.append(("press", selector, key))
        self.submitted_ms = self.clock.ms

    async def goto(self, url, timeout_ms):  # pragma: no cover - the transport navigates
        raise AssertionError("interact() never navigates")

    async def screenshot(self):  # pragma: no cover - never on an AI try
        raise AssertionError("interact() never takes a screenshot")


def context(i):
    return RequestContext(RUN_ID, i, 0)


def cases(target_id):
    return sorted((FIXTURES / target_id).glob("*.json"))


AI_CASES = [path for target_id in c.AI_SOURCES for path in cases(target_id)]


def bound(target_id, clock, **settings):
    return replace(registry.get(target_id), clock=clock, sleep=clock.sleep, **settings)


def run(path, **settings):
    """The fixture played through interact(), the PageView the transport builds from it (its
    visible text holds the answer, as a real page's would), the judge's result and the driver."""
    document, clock = json.loads(path.read_text()), FakeClock()
    target = bound(path.parent.name, clock, **settings)
    spec = target.page(context(0), (QUESTION,))
    driver = ScriptedDriver(document, clock)
    numbers = asyncio.run(target.interact(driver, spec))
    state = driver.state()
    text_head = " ".join([state["text_head"], *state["texts"].values()])[:4096]
    found = {s: asyncio.run(driver.count(s)) for s in j.selectors(spec.wait_for)}
    found |= numbers or {}
    view = PageView(state["url"], state["status"], state["title"], text_head, found)
    return document["expect"], view, target.judge(view), driver, clock, numbers


def outcome(assessment):
    return assessment.verdict, assessment.error, assessment.reason


def test_every_source_has_its_cases():
    names = {target_id: {path.stem for path in cases(target_id)} for target_id in c.AI_SOURCES}
    assert names == {
        c.AI_OVERVIEW: {
            *("ok", "late", "absent", "short", "sorry", "recaptcha", "block_page", "no_results"),
            *("consent", "consent_form", "answer_names_markers", "empty_overview"),
        },
        c.AI_CHATGPT: {
            *("ok", "cookie_dialog", "login_modal", "login_after_submit", "auth_redirect"),
            *("cloudflare", "too_many_requests", "status_429", "no_reply", "one_sentence"),
            *("no_prompt", "challenge_after_submit", "answer_names_markers", "challenge_cleared"),
            *("stream_names_limit", "login_after_answer"),
        },
        c.AI_GEMINI: {
            *("ok", "one_sentence_ok", "sign_in_redirect", "sign_in_dialog"),
            *("unusual_traffic", "no_reply", "answer_names_markers", "stream_names_traffic"),
        },
    }


@pytest.mark.parametrize("path", AI_CASES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_fixture_outcome(path):
    expect, view, result, driver, clock, numbers = run(path)
    assert outcome(result) == (expect["verdict"], expect["error"], expect["reason"])
    asked = [call for call in driver.calls if call[0] in ("fill", "press")]
    if expect["asked"]:
        prompt = registry.get(path.parent.name).PROMPT
        assert asked == [("fill", prompt, QUESTION), ("press", prompt, "Enter")]
    else:
        assert asked == []
    for key in ("answer_chars", "answer_ms"):
        if key in expect:
            assert view.found[key] == expect[key]
    if expect["verdict"] != "ok" and "answer_chars" in expect and not expect["answer_chars"]:
        assert "answer_ms" not in view.found
    if "waited_s" in expect:
        assert clock() == expect["waited_s"]
    clicks = [call[1] for call in driver.calls if call[0] == "click"]
    assert clicks == ([expect["clicked"]] if "clicked" in expect else [])


@pytest.mark.parametrize("path", AI_CASES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_no_answer_text_leaves_the_target(path, caplog, capsys):
    """The marker is on the page (the judge's text head holds it) but nowhere in the outputs:
    not in the numbers interact() returns, the judge's result or the try record."""
    caplog.set_level(logging.DEBUG)
    expect, view, result, _, _, numbers = run(path)
    target = registry.get(path.parent.name)
    fields = target.try_fields(context(0), (QUESTION,))
    assert fields == {"input": QUESTION, "source": target.id}
    record = c.try_record(
        i=0,
        **fields,
        verdict=result.verdict,
        error=result.error,
        status=view.status,
        final_url=view.final_url,
        timings_ms=dict.fromkeys(c.TIMINGS, 1),
        bytes_down_est=None,
        reason=result.reason,
        screenshot=None,
    )
    for value in numbers or {}:
        assert type(numbers[value]) is int and value.startswith("answer_")
    outputs = [json.dumps(record), repr(result), repr(numbers), caplog.text, *capsys.readouterr()]
    assert all(MARKER not in output for output in outputs)
    if any(MARKER in text for text in json.loads(path.read_text())["page"]["texts"].values()):
        assert MARKER in view.text_head


def test_the_wait_ends_at_the_page_timeout_and_answer_wait():
    path = FIXTURES / c.AI_CHATGPT / "no_reply.json"
    assert run(path, page_timeout_s=10)[4]() == 10
    assert run(path, answer_wait_s=7)[4]() == 7
    assert run(path, answer_wait_s=7, page_timeout_s=60)[4]() == 7
    overview = run(FIXTURES / c.AI_OVERVIEW / "absent.json", answer_wait_s=2)
    assert overview[3].calls == [("wait_for", ", ".join(AiOverview.ANSWER), 2000)]
    assert outcome(overview[2]) == ("error", "no_answer", "no_overview")
    # An overview that appears late keeps growing within what is left of the budget, and
    # its answer_ms counts from the start of the wait, the appearing time included.
    late = run(FIXTURES / c.AI_OVERVIEW / "late.json", answer_wait_s=4)
    assert late[4]() == 4 and late[5]["answer_chars"] > 0 and late[5]["answer_ms"] == 3000
    # A setting left unset keeps the default wait.
    assert run(path, answer_wait_s=None, page_timeout_s=None)[4]() == 30
    assert run(path, answer_wait_s=None, page_timeout_s=12)[4]() == 12


def test_a_growing_answer_waits_until_it_is_stable():
    expect, view, result, _, clock, numbers = run(FIXTURES / c.AI_GEMINI / "ok.json")
    # Last growth at 1.5 s, then unchanged for 2 s, read every POLL_S.
    assert numbers["answer_ms"] == 1500 and clock() == 1.5 + 2.0
    assert numbers["answer_sentences"] == 2 and POLL_S == 0.5
    assert outcome(result) == ("ok", None, None)


def test_a_read_answer_is_ok_whatever_the_page_says_but_a_407_comes_first():
    """The answer is in the text the markers read, and the status is the first navigation's:
    neither undoes it (the answer_names_markers and challenge_cleared fixtures). A 407 is
    still the proxy's refusal, answer or not."""
    answer = {"answer_chars": 200, "answer_sentences": 3}
    for target_id in c.AI_SOURCES:
        target = registry.get(target_id)
        page = PageView(target.url, 403, "Just a moment...", "captcha rate limit", answer)
        assert outcome(target.judge(page)) == ("ok", None, None)
        refused = PageView(target.url, 407, "", "", answer)
        assert outcome(target.judge(refused)) == ("proxy_error", "proxy_auth", "proxy_auth_status")


def test_sentence_count():
    assert sentence_count("") == sentence_count("  ...  ") == 0
    assert sentence_count("No end mark") == 1
    assert sentence_count("Pi is 3.14 exactly. Really!") == 2
    assert sentence_count("One. Two? Three! four") == 4
    assert sentence_count("空は青い。 光が散乱する。") == 2
    assert sentence_count("空は青い。光が散乱する。") == 2
    assert sentence_count("Done! 次。") == 2


def test_chat_sources_ask_the_one_fixed_question(tmp_path):
    for target_id in (c.AI_CHATGPT, c.AI_GEMINI):
        assert INPUT_SHAPES[target_id][1] == 1
        target = registry.get(target_id)
        for i in range(5):
            spec = target.page(context(i), (QUESTION,))
            assert isinstance(spec, AskSpec) and spec.question == QUESTION
            assert spec.url == target.url and spec.settle_ms == 0
            assert j.selectors(spec.wait_for)[0] == target.PROMPT
        driver = ScriptedDriver({"page": {}, "timeline": []}, FakeClock())
        with pytest.raises(TypeError, match="AskSpec"):
            asyncio.run(target.interact(driver, PageSpec(target.url)))
    # The settings refuse a second question, so every try on every network asks the same.
    one = 'inputs = ["example question"]   # exactly one fixed question'
    two = 'inputs = ["example question", "another question"]'
    head, chatgpt, gemini = EXAMPLE.read_text().split(one)
    path = tmp_path / "settings.toml"
    changed = {6: head + two + chatgpt + one + gemini, 7: head + one + chatgpt + two + gemini}
    for index, text in changed.items():
        path.write_text(text)
        refused = rf"target\[{index}\]\.inputs: must be a list of 1 to 1"
        with pytest.raises(UserError, match=refused):
            load_settings(path)


def test_overview_rotates_its_questions():
    target = registry.get(c.AI_OVERVIEW)
    questions = ("why is the sky blue", "how do rainbows form", "what is a tide")
    for i in range(7):
        spec = target.page(context(i), questions)
        query = questions[i % 3].replace(" ", "+")
        assert spec.url == f"https://www.google.com/search?q={query}&hl=en&gl=us"
        assert target.try_fields(context(i), questions) == {
            "input": questions[i % 3],
            "source": c.AI_OVERVIEW,
        }
    settings = load_settings(EXAMPLE)
    overview = next(t for t in settings.targets if t.id == c.AI_OVERVIEW)
    assert overview.answer_wait_s == 30


def test_the_overview_shares_google_search_selectors_marks_and_url():
    overview, search = registry.get(c.AI_OVERVIEW), registry.get(c.GOOGLE_SEARCH)
    for name in ("RESULTS", "CAPTCHA_FORM", "CONSENT_FORM", "MARKS"):
        assert getattr(overview, name) is getattr(search, name)
    for query in ("why is the sky blue", "a & b"):
        assert overview.address(query) == search.address(query)


def test_registered_sources_match_the_targets_file():
    rows = tomllib.loads((ROOT / "configs" / "targets.toml").read_text())["target"]
    urls = {row["id"]: row["url"] for row in rows}
    assert tuple(type(registry.get(t)) for t in c.AI_SOURCES) == AI_TARGET_TYPES
    for target_id in c.AI_SOURCES:
        target = registry.get(target_id)
        assert (target.id, target.engine, target.pool_eligible) == (target_id, "browser", False)
        assert target.version == "1" and isinstance(target, PageTarget)
        assert target.build_request().url == urls[target_id]
        assert target.answer_wait_s == 30 and target.page_timeout_s is None
    assert {t.id for t in registry.all() if t.engine == c.BROWSER_ENGINE} == set(c.RUN_TARGETS)
    assert c.AI_ANSWERS not in {t.id for t in registry.all()}


def test_a_target_that_only_reads_the_page_adds_nothing_to_found():
    base = BrowserTarget("x", "https://www.example.com/")
    assert asyncio.run(base.interact(None, PageSpec(base.url))) is None
