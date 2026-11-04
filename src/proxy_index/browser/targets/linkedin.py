"""The LinkedIn public profile target of K-BP1 (linkedin_public); see browser.targets.pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ...targets import PageView
from ..contracts import LINKEDIN_PUBLIC
from ..judge import OK, PageAssessment, count, failed, path_has
from . import PageTarget


@dataclass(frozen=True, slots=True)
class LinkedinPublic(PageTarget):
    """Public profile: ok when the name (h1) and the headline are present."""

    id: str = LINKEDIN_PUBLIC
    url: str = "https://www.linkedin.com/in/"

    NAME = "h1.top-card-layout__title"
    HEADLINE = ".top-card-layout__headline"
    LOGIN_PATHS = ("/authwall", "/login", "/signup", "/uas/")
    # LinkedIn answers refused clients with its own status 999.
    BLOCK_STATUSES = frozenset({999})

    def address(self, value: Any) -> str:
        return f"{self.url}{quote(value, safe='')}/"

    def wanted(self, value: Any) -> tuple[str, ...]:
        return (self.NAME, self.HEADLINE)

    def rules(self, view: PageView) -> PageAssessment:
        if path_has(view, self.LOGIN_PATHS):
            return failed("login_wall", "authwall_redirect")
        if not count(view, self.NAME):
            return failed("no_content", "no_name")
        return OK if count(view, self.HEADLINE) else failed("no_content", "no_headline")
