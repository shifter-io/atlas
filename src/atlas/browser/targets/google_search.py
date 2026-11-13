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
    """Search results page: ok when the results container holds at least 3 organic links.

    Failures, after the shared steps and the /sorry captcha mark: the consent page or form
    (consent_page), a redirect to the Google Accounts sign-in (login_redirect), a page that
    loaded with no title and no text (empty_page), Google's own "did not match any documents"
    page (no_results), and otherwise too few organic links (few_results)."""

    id: str = GOOGLE_SEARCH
    url: str = "https://www.google.com/search"

    RESULTS = "#search a h3"
    MIN_RESULTS = 3
    CAPTCHA_FORM = "form#captcha-form"
    CONSENT_FORM = 'form[action*="consent.google"]'
    LOGIN_HOST = "accounts.google.com"
    NO_RESULTS = "did not match any documents"
    MARKS = (Mark("challenge", "sorry_page", paths=("/sorry/",), found=(CAPTCHA_FORM,)),)

    def address(self, value: Any) -> str:
        return f"{self.url}?{urlencode({'q': value, 'hl': 'en', 'gl': 'us'})}"

    def wanted(self, value: Any) -> tuple[str, ...]:
        return (self.RESULTS, self.CAPTCHA_FORM, self.CONSENT_FORM)

    def rules(self, view: PageView) -> PageAssessment:
        # The transport accepts a consent wall once, when it hides the results; a consent
        # banner left beside the results does not count against them.
        host = host_of(view)
        if host.startswith("consent."):
            return failed("no_content", "consent_page")
        if host == self.LOGIN_HOST:
            return failed("login_wall", "login_redirect")
        if count(view, self.RESULTS) >= self.MIN_RESULTS:
            return OK
        if count(view, self.CONSENT_FORM):
            return failed("no_content", "consent_page")
        if not (view.title.strip() or view.text_head.strip()):
            return failed("no_content", "empty_page")
        if self.NO_RESULTS in view.text_head.lower():
            return failed("no_content", "no_results")
        return failed("no_content", "few_results")
