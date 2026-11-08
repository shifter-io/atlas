"""ChatGPT (BP-AI-TARGETS; docs/browser.html, "What counts as success").

Its fixture cases, and the tests it shares with Gemini as a chat source, run in
test_browser_targets_ai.py; the tests about ChatGPT alone go here.
"""

import asyncio
import json
from dataclasses import replace

from test_browser_targets_ai import (
    FIXTURES,
    QUESTION,
    FakeClock,
    ScriptedDriver,
    context,
    outcome,
    run,
)

from proxy_index.browser import contracts as c
from proxy_index.browser.targets.chatgpt import RELOAD_S, SEND_WAIT_S, ChatGpt
from proxy_index.targets import PageView, registry

CASES = FIXTURES / c.AI_CHATGPT


def reloads(driver):
    return [call for call in driver.calls if call[0] == "goto"]


def test_both_clients_prompt_boxes_and_replies_are_read():
    target = registry.get(c.AI_CHATGPT)
    assert target.version == "2" and target.url == "https://chatgpt.com/"
    assert target.PROMPT == '#prompt-textarea, textarea[name="prompt"]'
    assert 'li[data-message-role="assistant"]' in target.ANSWER


def test_a_refused_send_reloads_once_within_the_page_timeout():
    _, _, result, driver, clock, _ = run(CASES / "lightweight_not_sent.json", page_timeout_s=40)
    assert reloads(driver) == [("goto", "https://chatgpt.com/", 40_000)]
    assert outcome(result) == ("error", "browser_error", "send_failed")
    # The first send waits SEND_WAIT_S; the answer wait runs from the second Enter.
    assert clock() == SEND_WAIT_S + 30
    unbound = run(CASES / "lightweight_not_sent.json", page_timeout_s=None)[3]
    assert reloads(unbound) == [("goto", "https://chatgpt.com/", RELOAD_S * 1000)]


def test_a_sent_question_or_a_stop_sign_is_never_asked_again():
    sent = ("ok", "lightweight_ok", "no_reply", "login_after_submit", "challenge_after_submit")
    for name in sent:
        assert reloads(run(CASES / f"{name}.json")[3]) == []


class SignInOnReload(ScriptedDriver):
    """The reload lands on the sign-in page."""

    async def goto(self, url, timeout_ms):
        status = await super().goto(url, timeout_ms)
        self.page = {**self.page, "url": "https://auth.openai.com/log-in", "counts": {}}
        return status


def test_a_reload_that_lands_on_the_sign_in_page_is_a_login_wall():
    clock = FakeClock()
    target = replace(registry.get(c.AI_CHATGPT), clock=clock, sleep=clock.sleep)
    document = json.loads((CASES / "lightweight_not_sent.json").read_text())
    driver = SignInOnReload(document, clock)
    assert asyncio.run(target.interact(driver, target.page(context(0), (QUESTION,)))) is None
    view = PageView(asyncio.run(driver.url()), 200, "", "", {})
    assert outcome(target.judge(view)) == ("error", "login_wall", "login_redirect")


def test_no_reply_is_send_failed_only_while_the_page_is_the_new_chat():
    target = ChatGpt()
    found = {"answer_chars": 0, "answer_sentences": 0}
    for url, reason in (
        ("https://chatgpt.com/", "send_failed"),
        ("https://chatgpt.com", "send_failed"),
        ("https://chatgpt.com/uc/00000000-0000-4000-8000-000000000001", "no_reply"),
        ("https://chatgpt.com/c/00000000-0000-4000-8000-000000000001", "no_reply"),
    ):
        assert target.judge(PageView(url, 200, "ChatGPT", "", found)).reason == reason
