"""The LinkedIn public profile target of K-BP1 (linkedin_public); see browser.targets.pages."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote

from ...targets import PageView
from ..contracts import LINKEDIN_PUBLIC
from ..judge import OK, Mark, PageAssessment, count, failed, judge, path_has
from . import PageTarget


@dataclass(frozen=True, slots=True)
class LinkedinPublic(PageTarget):
    """Public profile: ok when the name (h1) and the headline are present.

    LinkedIn's own failure pages: its security check (challenge), status 999 (block), the
    authwall, login or sign-up page, by redirect or served at the profile URL (login_wall), and
    the profile-unavailable page, an empty page or a page without the profile (no_content)."""

    id: str = LINKEDIN_PUBLIC
    url: str = "https://www.linkedin.com/in/"

    NAME = "h1.top-card-layout__title"
    HEADLINE = ".top-card-layout__headline"
    LOGIN_PATHS = ("/authwall", "/login", "/signup", "/uas/")
    # The titles of the sign-up and login pages, for one served in place of the profile.
    LOGIN_TITLES = (
        "sign up | linkedin",
        "sign in | linkedin",
        "linkedin login, sign in | linkedin",
    )
    UNAVAILABLE_PATHS = ("/in/unavailable",)
    MARKS = (
        Mark(
            "challenge",
            "checkpoint_page",
            text=("security verification | linkedin", "quick security check"),
            paths=("/checkpoint/",),
        ),
    )
    # LinkedIn answers refused clients with its own status 999.
    BLOCK_STATUSES = frozenset({999})

    def address(self, value: Any) -> str:
        return f"{self.url}{quote(value, safe='')}/"

    def wanted(self, value: Any) -> tuple[str, ...]:
        return (self.NAME, self.HEADLINE)

    def judge(self, view: PageView) -> PageAssessment:
        """The shared judge, except that a page showing the profile's name and headline is the
        profile: its title and text are the member's own words, so a "captcha" or "rate limit"
        in them is no challenge or block mark. Statuses (407, 999, 403, ...) still count."""
        if count(view, self.NAME) and count(view, self.HEADLINE):
            view = replace(view, title="", text_head="")
        return judge(view, self.rules, self.MARKS, self.BLOCK_STATUSES)

    def rules(self, view: PageView) -> PageAssessment:
        if path_has(view, self.LOGIN_PATHS):
            return failed("login_wall", "authwall_redirect")
        if not count(view, self.NAME):
            return self._no_profile(view)
        return OK if count(view, self.HEADLINE) else failed("no_content", "no_headline")

    def _no_profile(self, view: PageView) -> PageAssessment:
        if view.title.strip().lower() in self.LOGIN_TITLES:
            return failed("login_wall", "authwall_page")
        if path_has(view, self.UNAVAILABLE_PATHS):
            return failed("no_content", "profile_unavailable")
        if not (view.title.strip() or view.text_head.strip()):
            return failed("no_content", "empty_page")
        return failed("no_content", "no_name")
