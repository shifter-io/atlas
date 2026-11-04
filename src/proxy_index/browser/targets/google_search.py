"""The Google search results target of K-BP1 (google_search); see browser.targets.pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from ...targets import PageView
from ..contracts import GOOGLE_SEARCH
from ..judge import OK, Mark, PageAssessment, count, failed, host_of
from . import PageTarget


@dataclass(frozen=True, slots=True)
class GoogleSearch(PageTarget):
    """Search results page: ok when the results container holds at least 3 organic links."""

    id: str = GOOGLE_SEARCH
    url: str = "https://www.google.com/search"

    RESULTS = "#search a h3"
    MIN_RESULTS = 3
    CAPTCHA_FORM = "form#captcha-form"
    CONSENT_FORM = 'form[action*="consent.google"]'
    MARKS = (Mark("challenge", "sorry_page", paths=("/sorry/",), found=(CAPTCHA_FORM,)),)

    def address(self, value: Any) -> str:
        return f"{self.url}?{urlencode({'q': value, 'hl': 'en', 'gl': 'us'})}"

    def wanted(self, value: Any) -> tuple[str, ...]:
        return (self.RESULTS, self.CAPTCHA_FORM, self.CONSENT_FORM)

    def rules(self, view: PageView) -> PageAssessment:
        # The transport accepts a consent wall once, when it hides the results; a consent
        # banner left beside the results does not count against them.
        if host_of(view).startswith("consent."):
            return failed("no_content", "consent_page")
        if count(view, self.RESULTS) >= self.MIN_RESULTS:
            return OK
        if count(view, self.CONSENT_FORM):
            return failed("no_content", "consent_page")
        return failed("no_content", "few_results")
