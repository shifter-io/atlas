"""The Instagram or X profile target of K-BP1 (instagram_web); see browser.targets.pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ...targets import PageView
from ..contracts import INSTAGRAM_WEB
from ..judge import OK, PageAssessment, count, failed, path_has
from . import PageTarget


@dataclass(frozen=True, slots=True)
class InstagramWeb(PageTarget):
    """Social web app profile, on Instagram or X (input `site`): ok when the profile header
    and at least one post are shown, with no login dialog over them."""

    id: str = INSTAGRAM_WEB
    url: str = "https://www.instagram.com/"

    # Per site: profile URL, then the profile header, posts and a blocking login dialog.
    PROFILES = {"instagram": "https://www.instagram.com/{}/", "x": "https://x.com/{}"}
    SITES = {
        "instagram": (
            "header h2",
            'main a[href*="/p/"]',
            'div[role="dialog"] input[name="username"]',
        ),
        "x": (
            '[data-testid="UserName"]',
            'article[data-testid="tweet"]',
            '[data-testid="sheetDialog"]',
        ),
    }
    LOGIN_PATHS = ("/accounts/login", "/i/flow/login")

    def address(self, value: Any) -> str:
        return self.PROFILES[value["site"]].format(quote(value["handle"], safe=""))

    def wanted(self, value: Any) -> tuple[str, ...]:
        return self.SITES[value["site"]]

    def rules(self, view: PageView) -> PageAssessment:
        headers, posts, dialogs = zip(*self.SITES.values(), strict=True)
        if path_has(view, self.LOGIN_PATHS):
            return failed("login_wall", "login_redirect")
        if count(view, *dialogs):
            return failed("login_wall", "login_dialog")
        if not count(view, *headers):
            return failed("no_content", "no_profile")
        return OK if count(view, *posts) else failed("no_content", "no_posts")
