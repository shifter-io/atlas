"""The Cloudflare managed challenge target of K-BP1 (cloudflare_challenge); see
browser.targets.pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...targets import PageDriver, PageSpec, PageView
from ..contracts import CLOUDFLARE_CHALLENGE
from ..judge import (
    OK,
    TEXT_SELECTOR,
    Mark,
    PageAssessment,
    failed,
    selector_list,
    selectors,
    text_selector,
)
from . import PageTarget


@dataclass(frozen=True, slots=True)
class CloudflareChallenge(PageTarget):
    """A page behind a managed challenge (input `{url, expect}`): ok when, within
    `challenge_wait_s`, the challenge cleared by itself and the page shows `expect`. The host
    binds `challenge_wait_s` from the settings with dataclasses.replace."""

    id: str = CLOUDFLARE_CHALLENGE
    url: str = "https://challenges.cloudflare.com/"
    challenge_wait_s: int = 30

    CHALLENGE = ("#challenge-form", "#challenge-stage")
    ERROR_DETAILS = "#cf-error-details"
    MARKS = (
        # Its title "Attention Required!" is a shared challenge marker: the site's mark wins.
        Mark("blocked", "cf_block", text=("you have been blocked", "error code: 1020")),
        Mark("challenge", "challenge_page", found=CHALLENGE),
    )

    def address(self, value: Any) -> str:
        return value["url"]

    def wanted(self, value: Any) -> tuple[str, ...]:
        return (text_selector(value["expect"]), *self.CHALLENGE, self.ERROR_DETAILS)

    async def interact(self, driver: PageDriver, spec: PageSpec) -> None:
        """Wait up to challenge_wait_s for the expected text or an error page: a managed
        challenge clears by itself and then loads the real page."""
        expect = selectors(spec.wait_for)[0]
        if not await driver.count(expect):
            either = selector_list(expect, self.ERROR_DETAILS)
            await driver.wait_for(either, self.challenge_wait_s * 1000)

    def rules(self, view: PageView) -> PageAssessment:
        expected = sum(n for key, n in view.found.items() if key.startswith(TEXT_SELECTOR))
        return OK if expected else failed("no_content", "no_expect")
