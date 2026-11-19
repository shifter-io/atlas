"""ChatGPT, an AI answer source of K-BP1 (D4); its shared base is ai.ChatSource.

Version 2 (CHATGPT-FIX-1). Logged out, https://chatgpt.com/ now serves a lightweight client:
its prompt box is `textarea[name="prompt"]` (no `#prompt-textarea`), a sent question moves the
page to /uc/<id>, and the reply grows in `li[data-message-role="assistant"]`. The React app's
prompt box and reply are still read, for the visitors it is served to. /new, which once served
the React app, now redirects logged-out visitors to the sign-in page, so the entry stays /.
The client sends a question only once its own checks pass (sentinel: turnstile, proof of
work), and on a fresh profile's first load it mostly refuses: Enter then sends nothing and
the page stays at /. So when within SEND_WAIT_S no user turn, reply or stop sign shows and
the page is still /, interact() reloads it once and asks again (live, 15 of 16 such tries
sent after the reload). A question that still never left the page (no reply while the page
is /) is browser_error (send_failed), as Gemini's is, not no_reply.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from ...targets import PageDriver, PageSpec, PageView
from ..contracts import AI_CHATGPT
from ..judge import Mark, PageAssessment, failed, selector_list, text_selector
from .ai import AskSpec, ChatSource

TOO_MANY = "too many requests"
# How long a sent question takes to show as a user turn; the reload's page timeout when the
# host binds none.
SEND_WAIT_S = 6
RELOAD_S = 45


@dataclass(frozen=True, slots=True)
class ChatGpt(ChatSource):
    """ChatGPT logged out: ok when the assistant's reply has at least MIN_CHARS characters
    and at least 2 sentences."""

    id: str = AI_CHATGPT
    url: str = "https://chatgpt.com/"
    version: str = "2"

    # The React app's prompt box, then the lightweight client's; only one is ever shown.
    PROMPT = selector_list("#prompt-textarea", 'textarea[name="prompt"]')
    ANSWER = ('[data-message-author-role="assistant"]', 'li[data-message-role="assistant"]')
    # The lightweight client marks a finished reply; the React app's reply is read until stable.
    COMPLETE = 'li[data-message-role="assistant"][data-message-complete]'
    LOGIN_HOSTS = ("auth.openai.com", "auth0.openai.com")
    LOGIN_PATHS = ("/auth/login",)
    LOGIN_DIALOGS = (
        'div[role="dialog"]:has([data-testid="login-button"])',
        'div[role="dialog"]:has-text("Log in or sign up")',
    )
    STOPS = (text_selector("Too many requests"), "#challenge-form")
    DISMISS = ('button:has-text("Reject non-essential")', 'button:has-text("Accept all")')
    MIN_SENTENCES = 2
    MARKS = (
        Mark("challenge", "challenge_page", found=("#challenge-form",)),
        Mark("blocked", "too_many_requests", text=(TOO_MANY,)),
    )
    # The page a question is typed on; a sent one moves to /c/<id> or /uc/<id> and shows
    # as a user turn.
    NEW_CHAT = "/"
    USER_TURN = ('[data-message-author-role="user"]', 'li[data-message-role="user"]')

    async def interact(self, driver: PageDriver, spec: PageSpec) -> dict[str, int] | None:
        """ChatSource's ask, and once more after a reload when the question never left the
        page. answer_ms and the answer wait run from the Enter that sent it."""
        if not isinstance(spec, AskSpec) or not spec.question:
            raise TypeError("A chat source needs the AskSpec its page() built")
        for reload in (False, True):
            if reload:
                timeout_ms = 1000 * (self.page_timeout_s or RELOAD_S)
                await driver.goto(self.url, timeout_ms)
                await driver.wait_for(selector_list(*self.wanted(None)), timeout_ms)
            await self.dismiss(driver)
            if await self.stopped(driver) or not await driver.count(self.PROMPT):
                return None
            await driver.fill(self.PROMPT, spec.question)
            await driver.press(self.PROMPT, "Enter")
            start = self.clock()
            if await self.sent(driver):
                break
        return await self.read_answer(driver, self.budget_s(), start)

    async def sent(self, driver: PageDriver) -> bool:
        """Whether the question left the page within SEND_WAIT_S: a user turn, a reply or a
        stop sign shows, or the page moved off the new chat."""
        shown = selector_list(*self.USER_TURN, *self.ANSWER, *self.LOGIN_DIALOGS, *self.STOPS)
        if await driver.wait_for(shown, SEND_WAIT_S * 1000):
            return True
        return self.left_new_chat(await driver.url()) or await self.stopped(driver)

    def left_new_chat(self, url: str) -> bool:
        return (urlsplit(url).path or "/") != self.NEW_CHAT

    def rules(self, view: PageView) -> PageAssessment:
        result = ChatSource.rules(self, view)
        if result.reason == "no_reply" and not self.left_new_chat(view.final_url):
            return failed("browser_error", "send_failed")
        return result
