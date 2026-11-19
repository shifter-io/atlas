"""The X profile target of K-BP1 (x_web); see browser.targets.pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ...targets import PageView
from ..contracts import X_WEB
from ..judge import OK, PageAssessment, count, failed, path_has
from . import PageTarget


@dataclass(frozen=True, slots=True)
class XWeb(PageTarget):
    """Public X profile (input: a handle): ok when the profile header and at least one post
    are shown, with no login sheet over them. A redirect to X's login flow or its login sheet
    is a login wall; a page without the header or without a post has no content."""

    id: str = X_WEB
    url: str = "https://x.com/"
    # 2: the try waits for READY (header and post together), not for either one.
    version: str = "2"

    # X's logged-out profile page (2026-11): the name as the main column's h1, the latest posts
    # as articles, then a "See <name>'s full profile" sign-up box beside them that every
    # logged-out visitor gets, so it is no login wall. The older app's test ids still count.
    HEADERS = ('[data-testid="UserName"]', "main h1")
    POSTS = ('article[data-testid="tweet"]', "main article")
    DIALOG = '[data-testid="sheetDialog"]'
    LOGIN_PATHS = ("/i/flow/login", "/i/jf/onboarding", "/login")
    # The header shows before the posts: read the page once both are there, or the login sheet.
    READY = (f"html:has({', '.join(HEADERS)}):has({', '.join(POSTS)})", DIALOG)
    SHOWN_UNREAD = ("text_head",)

    def address(self, value: Any) -> str:
        return f"{self.url}{quote(value, safe='')}"

    def wanted(self, value: Any) -> tuple[str, ...]:
        return (*self.HEADERS, *self.POSTS, self.DIALOG)

    def shown(self, view: PageView) -> bool:
        """The profile header and a post: the visible text is then the profile's own, so the
        markers no longer read it (SHOWN_UNREAD); the title is still read."""
        return bool(count(view, *self.HEADERS) and count(view, *self.POSTS))

    def rules(self, view: PageView) -> PageAssessment:
        if path_has(view, self.LOGIN_PATHS):
            return failed("login_wall", "login_redirect")
        if count(view, self.DIALOG):
            return failed("login_wall", "login_dialog")
        if not count(view, *self.HEADERS):
            return failed("no_content", "no_profile")
        return OK if count(view, *self.POSTS) else failed("no_content", "no_posts")
