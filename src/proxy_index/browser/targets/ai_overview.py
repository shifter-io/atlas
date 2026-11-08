"""Google AI Overview, an AI answer source of K-BP1 (D4); its shared base is ai.AiSource."""

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

    ANSWER = ('div[aria-label="AI Overview"]', 'div[data-attrid="AI Overview"]')
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
