"""Google AI Overview, an AI answer source of K-BP1 (D4); its shared base is ai.AiSource.

Version 2 (#677) reads the overview where Google puts it today, in `#m-x-content` around the
`[data-subtree="aimc"]` answer, as the owner's Pulse proof of concept does; version 1's
`div[aria-label="AI Overview"]` and `div[data-attrid="AI Overview"]` matched no live page, so
every overview was judged no_overview. The aria-label one stays for older pages, and the POC's
`[data-attrid="AIOverview"]` is read too. An overview Google is still writing shows a heading
"Thinking", "Searching" or "Generating" (LOADING) over a short placeholder: it counts as no text
yet, so the wait goes on until the answer replaces it instead of judging the placeholder short.
The overview is read as shown: its "Show more" control is not clicked, since its visible part
is far over MIN_CHARS. A challenge is still google_search's /sorry page and captcha form.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...targets import PageDriver, PageSpec, PageView
from ..contracts import AI_OVERVIEW
from ..judge import PageAssessment, count, failed, host_of, selector_list
from .ai import ANSWER_CHARS, ANSWER_SENTENCES, AiSource
from .pages import GoogleSearch


@dataclass(frozen=True, slots=True)
class AiOverview(AiSource):
    """Google results page for a rotating question: ok when an AI Overview block with at
    least MIN_CHARS characters of text is shown."""

    id: str = AI_OVERVIEW
    url: str = "https://www.google.com/search"
    version: str = "2"

    ANSWER = (
        "#m-x-content",
        '[data-subtree="aimc"]',
        '[data-attrid="AIOverview"]',
        'div[aria-label="AI Overview"]',
    )
    LOADING = '[role="heading"]:text-matches("^(thinking|searching|generating)[^a-z]*$", "i")'
    # The results page is google_search's: its selectors, marks and URL have one owner.
    RESULTS = GoogleSearch.RESULTS
    CAPTCHA_FORM = GoogleSearch.CAPTCHA_FORM
    CONSENT_FORM = GoogleSearch.CONSENT_FORM
    MARKS = GoogleSearch.MARKS
    # The overview loads after the results: wait this long for it to start.
    APPEAR_S = 5

    def address(self, value: Any) -> str:
        return GoogleSearch.address(self, value)

    def wanted(self, value: Any) -> tuple[str, ...]:
        return (*self.ANSWER, self.RESULTS, self.CAPTCHA_FORM, self.CONSENT_FORM)

    async def interact(self, driver: PageDriver, spec: PageSpec) -> dict[str, int] | None:
        """With results shown, wait up to APPEAR_S for an overview, then for it to stop
        growing. The whole wait, and answer_ms, run from the start of interact()."""
        start, budget, overview = self.clock(), self.budget_s(), selector_list(*self.ANSWER)
        if not await driver.count(overview):
            if not await driver.count(self.RESULTS):
                return None
            if not await driver.wait_for(overview, round(min(self.APPEAR_S, budget) * 1000)):
                return {ANSWER_CHARS: 0, ANSWER_SENTENCES: 0}
        return await self.read_answer(driver, budget, start)

    async def _answer_size(self, driver: PageDriver) -> tuple[int, int]:
        """No text while a LOADING heading shows: the overview is not written yet."""
        if await driver.count(self.LOADING):
            return 0, 0
        return await AiSource._answer_size(self, driver)

    def rules(self, view: PageView) -> PageAssessment:
        if host_of(view).startswith("consent."):
            return failed("no_content", "consent_page")
        if count(view, *self.ANSWER) or view.found.get(ANSWER_CHARS):
            return failed("no_answer", "short_overview")
        if count(view, self.RESULTS):
            return failed("no_answer", "no_overview")
        if count(view, self.CONSENT_FORM):
            return failed("no_content", "consent_page")
        return failed("no_content", "no_results")
