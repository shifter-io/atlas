"""The Instagram or X profile target of K-BP1 (instagram_web); see browser.targets.pages."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, NamedTuple
from urllib.parse import quote

from ...targets import PageView
from ..contracts import INSTAGRAM_WEB
from ..judge import OK, Mark, PageAssessment, count, failed, path_has
from . import PageTarget


class Site(NamedTuple):
    """One site's selectors: the profile header, its posts (any counts) and a login dialog."""

    header: str
    posts: tuple[str, ...]
    dialog: str


@dataclass(frozen=True, slots=True)
class InstagramWeb(PageTarget):
    """Social web app profile, on Instagram or X (input `site`): ok when the profile header
    and at least one post (on Instagram a photo or a reel) are shown, with no login dialog
    over them."""

    id: str = INSTAGRAM_WEB
    url: str = "https://www.instagram.com/"

    PROFILES = {"instagram": "https://www.instagram.com/{}/", "x": "https://x.com/{}"}
    SITES = {
        "instagram": Site(
            "header h2",
            ('main a[href*="/p/"]', 'main a[href*="/reel/"]'),
            'div[role="dialog"] input[name="username"]',
        ),
        "x": Site(
            '[data-testid="UserName"]',
            ('article[data-testid="tweet"]',),
            '[data-testid="sheetDialog"]',
        ),
    }
    LOGIN_PATHS = ("/accounts/login", "/i/flow/login")
    MARKS = (
        # Instagram's checkpoint ("Help us confirm it's you"); `/challenge/` and below only,
        # so an X profile at /challenge stays a profile.
        Mark("challenge", "checkpoint_page", paths=("/challenge/",)),
        # Instagram's rate-limit page, served with a 200.
        Mark("blocked", "wait_page", text=("please wait a few minutes before you try again",)),
    )

    def address(self, value: Any) -> str:
        return self.PROFILES[value["site"]].format(quote(value["handle"], safe=""))

    def wanted(self, value: Any) -> tuple[str, ...]:
        site = self.SITES[value["site"]]
        return (site.header, *site.posts, site.dialog)

    def judge(self, view: PageView) -> PageAssessment:
        """The shared judge, except that once the profile header and a post are shown the
        visible text is the profile's own (bio, posts): the markers no longer read it, so a
        post saying "captcha" or "access denied" is not a challenge or block page."""
        header, posts, _ = self._counts(view)
        if header and posts:
            view = replace(view, text_head="")
        return PageTarget.judge(self, view)

    def rules(self, view: PageView) -> PageAssessment:
        header, posts, dialog = self._counts(view)
        if path_has(view, self.LOGIN_PATHS):
            return failed("login_wall", "login_redirect")
        if dialog:
            return failed("login_wall", "login_dialog")
        if not header:
            return failed("no_content", "no_profile")
        return OK if posts else failed("no_content", "no_posts")

    def _counts(self, view: PageView) -> tuple[int, int, int]:
        """The header, post and login dialog counts; a page counts only its own site's."""
        sites = self.SITES.values()
        return (
            count(view, *(site.header for site in sites)),
            count(view, *(post for site in sites for post in site.posts)),
            count(view, *(site.dialog for site in sites)),
        )
