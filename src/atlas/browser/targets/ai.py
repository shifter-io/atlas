"""The AI answer sources of K-BP1 (D4): their shared bases, and Gemini.

Each source decides whether an AI answer appeared, logged out, and how long it took. interact()
reads the answer's length and sentence count through the PageDriver and returns only those
numbers (and the time), which the transport adds to PageView.found. The answer text never
leaves interact(): it is not logged, recorded or returned, and an ok try gets no screenshot.
The selectors and marks are this version's reading of the public pages; a change to one bumps
the target's `version`. Google AI Overview and ChatGPT live in their own modules (ai_overview,
chatgpt); ai_types gathers all three, and atlas.targets registers them from there.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import urlsplit

from ...models import RequestContext
from ...targets import PageDriver, PageSpec, PageView
from ..contracts import AI_GEMINI
from ..judge import (
    Mark,
    PageAssessment,
    count,
    failed,
    host_of,
    path_has,
    text_selector,
)
from . import PageTarget, choose

# The numbers interact() returns; the judge reads them from PageView.found.
ANSWER_CHARS = "answer_chars"
ANSWER_SENTENCES = "answer_sentences"
ANSWER_MS = "answer_ms"
MIN_CHARS = 80
ANSWER_WAIT_S = 30
# An answer is complete once its length stays the same for STABLE_S, read every POLL_S.
STABLE_S = 2.0
POLL_S = 0.5
# Latin marks need whitespace after them (3.14 is one number); CJK marks never do.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|(?<=[。！？])\s*")


def sentence_count(text: str) -> int:
    """Parts of the text split after a sentence-ending mark, that hold a word."""
    return sum(1 for part in _SENTENCE_BREAK.split(text.strip()) if re.search(r"\w", part))


@dataclass(frozen=True, slots=True)
class AskSpec(PageSpec):
    """A page with the question a chat source types into its prompt box."""

    question: str = ""


@dataclass(frozen=True, slots=True)
class AiSource(PageTarget):
    """An AI answer source: rotates its question inputs, records its own id as the try's
    `source`, and waits for the answer to stop growing. The host binds `answer_wait_s` and
    `page_timeout_s` (the try's page timeout) from the settings with dataclasses.replace; the
    wait ends at the smaller one. `clock` and `sleep` are replaced only by tests."""

    answer_wait_s: int | None = ANSWER_WAIT_S
    page_timeout_s: int | None = None
    clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)
    sleep: Callable[[float], Awaitable[Any]] = field(
        default=asyncio.sleep, repr=False, compare=False
    )

    # The answer's containers, the signs that end the wait early, consent buttons to close.
    ANSWER: ClassVar[tuple[str, ...]] = ()
    STOPS: ClassVar[tuple[str, ...]] = ()
    DISMISS: ClassVar[tuple[str, ...]] = ()
    MIN_SENTENCES: ClassVar[int] = 1
    SHOWN_OK = True

    def try_fields(self, context: RequestContext, inputs: tuple[Any, ...]) -> dict[str, Any]:
        return {"input": choose(context, inputs), "source": self.id}

    def budget_s(self) -> float:
        """The wait: answer_wait_s (ANSWER_WAIT_S when unset), cut to page_timeout_s if set."""
        wait = ANSWER_WAIT_S if self.answer_wait_s is None else self.answer_wait_s
        return float(wait if self.page_timeout_s is None else min(wait, self.page_timeout_s))

    async def dismiss(self, driver: PageDriver) -> None:
        """Close one consent or cookie dialog, if one is shown: one click, nothing else."""
        for selector in self.DISMISS:
            if await driver.count(selector):
                await driver.click(selector)
                return

    async def stopped(self, driver: PageDriver) -> bool:
        return any([await driver.count(selector) for selector in self.STOPS])

    async def _answer_size(self, driver: PageDriver) -> tuple[int, int]:
        """(characters, sentences) of the first answer container shown; the text stays here."""
        for selector in self.ANSWER:
            if await driver.count(selector):
                text = (await driver.text(selector)).strip()
                return len(text), sentence_count(text)
        return 0, 0

    async def read_answer(
        self, driver: PageDriver, budget_s: float, start: float | None = None
    ) -> dict[str, int]:
        """Poll the answer until its length is unchanged for STABLE_S, a stop sign shows while
        no answer has appeared, or budget_s passes since `start` (default now). A stop sign
        replaces an answer, never interrupts one: the text stops match the whole body, which
        holds the answer's own words. answer_ms runs from `start` to the answer's last growth."""
        start = self.clock() if start is None else start
        size, sentences, grown = 0, 0, start
        while True:
            now = self.clock()
            chars, parts = await self._answer_size(driver)
            if chars != size:
                size, sentences, grown = chars, parts, now
            elif size and now - grown >= STABLE_S:
                break
            if now - start >= budget_s or (not size and await self.stopped(driver)):
                break
            await self.sleep(POLL_S)
        found = {ANSWER_CHARS: size, ANSWER_SENTENCES: sentences}
        return found | ({ANSWER_MS: round((grown - start) * 1000)} if size else {})

    def shown(self, view: PageView) -> bool:
        """A complete answer, which is ok right after the 407 step (SHOWN_OK): the visible
        text the markers read holds the answer itself, and the status is the first
        navigation's (a challenge that cleared by itself was a 403); neither undoes an answer
        that was read."""
        found = view.found
        chars, sentences = found.get(ANSWER_CHARS, 0), found.get(ANSWER_SENTENCES, 0)
        return chars >= MIN_CHARS and sentences >= self.MIN_SENTENCES


@dataclass(frozen=True, slots=True)
class ChatSource(AiSource):
    """A chat app: open it, close a consent dialog if one covers the prompt, type the one
    fixed question, submit, and wait for the reply to stop growing. A sign-in before the
    answer (a redirect to the sign-in host or a dialog over the prompt) is a login wall."""

    PROMPT: ClassVar[str] = ""
    LOGIN_HOSTS: ClassVar[tuple[str, ...]] = ()
    LOGIN_PATHS: ClassVar[tuple[str, ...]] = ()
    LOGIN_DIALOGS: ClassVar[tuple[str, ...]] = ()

    def page(self, context: RequestContext, inputs: tuple[Any, ...]) -> AskSpec:
        spec = PageTarget.page(self, context, inputs)
        return AskSpec(spec.url, spec.wait_for, spec.settle_ms, choose(context, inputs))

    def address(self, value: Any) -> str:
        return self.url

    def wanted(self, value: Any) -> tuple[str, ...]:
        return (self.PROMPT, *self.LOGIN_DIALOGS, *self.STOPS)

    def login_redirect(self, view: PageView) -> bool:
        return host_of(view) in self.LOGIN_HOSTS or path_has(view, self.LOGIN_PATHS)

    async def stopped(self, driver: PageDriver) -> bool:
        """A sign-in redirect or dialog, or one of the STOPS: asking or waiting ends."""
        if self.login_redirect(PageView(await driver.url(), None, "", "")):
            return True
        return any([await driver.count(selector) for selector in self.LOGIN_DIALOGS + self.STOPS])

    async def interact(self, driver: PageDriver, spec: PageSpec) -> dict[str, int] | None:
        if not isinstance(spec, AskSpec) or not spec.question:
            raise TypeError("A chat source needs the AskSpec its page() built")
        await self.dismiss(driver)
        if await self.stopped(driver) or not await driver.count(self.PROMPT):
            return None
        await driver.fill(self.PROMPT, spec.question)
        await driver.press(self.PROMPT, "Enter")
        return await self.read_answer(driver, self.budget_s())

    def rules(self, view: PageView) -> PageAssessment:
        if self.login_redirect(view):
            return failed("login_wall", "login_redirect")
        if count(view, *self.LOGIN_DIALOGS):
            return failed("login_wall", "login_dialog")
        if ANSWER_CHARS not in view.found:
            return failed("no_content", "no_prompt")
        if view.found[ANSWER_CHARS]:
            return failed("no_answer", "short_answer")
        return failed("no_answer", "no_reply")


@dataclass(frozen=True, slots=True)
class Gemini(ChatSource):
    """Gemini logged out: ok when the response has at least MIN_CHARS characters. No reply
    while the page is still the new chat (NEW_CHAT) is a question never sent: Gemini moves a
    sent one to its conversation's address, /app/<id>. That is our failure, not Gemini's."""

    id: str = AI_GEMINI
    url: str = "https://gemini.google.com/app"

    PROMPT = "rich-textarea .ql-editor"
    ANSWER = ("model-response message-content", ".model-response-text")
    LOGIN_HOSTS = ("accounts.google.com",)
    LOGIN_DIALOGS = ('div[role="dialog"]:has-text("Sign in")',)
    STOPS = (text_selector("unusual traffic"), "form#captcha-form")
    DISMISS = ('button:has-text("Reject all")', 'button:has-text("Accept all")')
    NEW_CHAT = "/app"
    # Google's "unusual traffic" page is a shared block marker; here it is the challenge.
    MARKS = (
        Mark(
            "challenge",
            "unusual_traffic",
            text=("unusual traffic",),
            paths=("/sorry/",),
            found=("form#captcha-form",),
        ),
    )

    def rules(self, view: PageView) -> PageAssessment:
        result = ChatSource.rules(self, view)
        new_chat = urlsplit(view.final_url).path.rstrip("/") == self.NEW_CHAT
        if result.reason == "no_reply" and new_chat:
            return failed("browser_error", "send_failed")
        return result
