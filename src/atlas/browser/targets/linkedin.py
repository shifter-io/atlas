"""The LinkedIn public profile target of K-BP1 (linkedin_public); see browser.targets.pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ...targets import PageView
from ..contracts import LINKEDIN_PUBLIC
from ..judge import BLOCK_STATUS, OK, Mark, PageAssessment, count, failed, path_has
from . import PageTarget


@dataclass(frozen=True, slots=True)
class LinkedinPublic(PageTarget):
    """Public profile: ok when the name (h1) and the headline are present.

    LinkedIn's own failure pages: its security check (challenge), status 999 (block), the
    authwall, login or sign-up page, by redirect or served at the profile URL (login_wall, also
    when answered with 999), and the profile-unavailable page, an empty page or a page without
    the profile (no_content)."""

    id: str = LINKEDIN_PUBLIC
    url: str = "https://www.linkedin.com/in/"
    # 2: the try also stops waiting at the authwall's heading (AUTHWALL); same pass rule.
    # 3: the profile address has no trailing slash (see address()); same pass rule.
    version: str = "3"

    NAME = "h1.top-card-layout__title"
    HEADLINE = ".top-card-layout__headline"
    # The authwall and sign-up page's heading. Waited for only so that a try ends there at once
    # instead of after the page timeout (most tries end there); the rules never read it: the
    # page's path or title decides.
    AUTHWALL = "h1.authwall-join-form__title"
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
        """The profile's own address, /in/<name> without a trailing slash: LinkedIn answers
        /in/<name>/ with a redirect there that sets tracking cookies, and walls most such
        redirected visits (login_wall) whatever the network (LINKEDIN-SLASH-1)."""
        return f"{self.url}{quote(value, safe='')}"

    def wanted(self, value: Any) -> tuple[str, ...]:
        return (self.NAME, self.HEADLINE, self.AUTHWALL)

    def shown(self, view: PageView) -> bool:
        """The profile's name and headline: its title and text are then the member's own
        words, so a "captcha" or "rate limit" in them is no challenge or block mark (see
        PageTarget.judge). Statuses (407, 999, 403, ...) still count."""
        return bool(count(view, self.NAME) and count(view, self.HEADLINE))

    def judge(self, view: PageView) -> PageAssessment:
        """PageTarget.judge, except that a 999 on the authwall, login or sign-up page is that
        page (login_wall): LinkedIn serves its logged-out redirect with 999 as well. A 999
        without such a page stays a block, and the challenge marks still come first."""
        result = PageTarget.judge(self, view)
        if result.reason == BLOCK_STATUS and view.status in self.BLOCK_STATUSES:
            return self._login_wall(view) or result
        return result

    def rules(self, view: PageView) -> PageAssessment:
        if wall := self._login_wall(view):
            return wall
        if not count(view, self.NAME):
            return self._no_profile(view)
        return OK if count(view, self.HEADLINE) else failed("no_content", "no_headline")

    def _login_wall(self, view: PageView) -> PageAssessment | None:
        """A redirect to a login path, or a login page (by its title) where no profile is."""
        if path_has(view, self.LOGIN_PATHS):
            return failed("login_wall", "authwall_redirect")
        if not count(view, self.NAME) and view.title.strip().lower() in self.LOGIN_TITLES:
            return failed("login_wall", "authwall_page")
        return None

    def _no_profile(self, view: PageView) -> PageAssessment:
        if path_has(view, self.UNAVAILABLE_PATHS):
            return failed("no_content", "profile_unavailable")
        if not (view.title.strip() or view.text_head.strip()):
            return failed("no_content", "empty_page")
        return failed("no_content", "no_name")
