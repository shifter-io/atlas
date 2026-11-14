"""The Instagram profile target of K-BP1 (instagram_web); see browser.targets.pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ...targets import PageView
from ..contracts import INSTAGRAM_WEB
from ..judge import OK, Mark, PageAssessment, count, failed, path_has
from . import PageTarget


@dataclass(frozen=True, slots=True)
class InstagramWeb(PageTarget):
    """Instagram profile (input `{site = "instagram", handle}`): ok when the profile header
    and at least one post (a photo or a reel) are shown, with no login dialog over them. X
    profiles are their own target, x_web."""

    id: str = INSTAGRAM_WEB
    url: str = "https://www.instagram.com/"

    HEADER = "header h2"
    POSTS = ('main a[href*="/p/"]', 'main a[href*="/reel/"]')
    DIALOG = 'div[role="dialog"] input[name="username"]'
    LOGIN_PATHS = ("/accounts/login",)
    SHOWN_UNREAD = ("text_head",)
    MARKS = (
        # Instagram's checkpoint ("Help us confirm it's you").
        Mark("challenge", "checkpoint_page", paths=("/challenge/",)),
        # Instagram's rate-limit page, served with a 200.
        Mark("blocked", "wait_page", text=("please wait a few minutes before you try again",)),
    )

    def address(self, value: Any) -> str:
        return f"{self.url}{quote(value['handle'], safe='')}/"

    def wanted(self, value: Any) -> tuple[str, ...]:
        return (self.HEADER, *self.POSTS, self.DIALOG)

    def shown(self, view: PageView) -> bool:
        """The profile header and a post: the visible text is then the profile's own (bio,
        posts), so the markers no longer read it (SHOWN_UNREAD) and a post saying "captcha"
        or "access denied" is not a challenge or block page. The title is still read."""
        return bool(count(view, self.HEADER) and count(view, *self.POSTS))

    def rules(self, view: PageView) -> PageAssessment:
        if path_has(view, self.LOGIN_PATHS):
            return failed("login_wall", "login_redirect")
        if count(view, self.DIALOG):
            return failed("login_wall", "login_dialog")
        if not count(view, self.HEADER):
            return failed("no_content", "no_profile")
        return OK if count(view, *self.POSTS) else failed("no_content", "no_posts")
