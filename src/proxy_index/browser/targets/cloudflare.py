"""The Cloudflare managed challenge target of K-BP1 (cloudflare_challenge); see
browser.targets.pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...targets import PageDriver, PageSpec, PageView
from ..contracts import CLOUDFLARE_CHALLENGE, PROXY_AUTH_STATUS
from ..judge import (
    OK,
    TEXT_SELECTOR,
    Mark,
    PageAssessment,
    count,
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

    # The challenge page: its form and stage until 2023; since then a page that the challenge
    # platform's orchestrate script builds. A normal page of a protected site may load the
    # platform's detection script (/cdn-cgi/challenge-platform/scripts/jsd/), never orchestrate.
    CHALLENGE = (
        "#challenge-form",
        "#challenge-stage",
        'script[src*="/cdn-cgi/challenge-platform/"][src*="/orchestrate/"]',
    )
    # Every Cloudflare error page: the 1xxx access refusals and the origin's 52x errors.
    ERROR_DETAILS = "#cf-error-details"
    MARKS = (
        # The block page's title "Attention Required!" is a shared challenge marker, and the
        # 1xxx refusals (1020 firewall rule, 1015 rate limit, 1009 country) all read "Access
        # denied | <host> used Cloudflare to restrict access": the site's mark wins.
        Mark(
            "blocked",
            "cf_block",
            text=(
                "you have been blocked",
                "used cloudflare to restrict access",
                "error code: 1020",
            ),
        ),
        Mark(
            "challenge",
            "challenge_page",
            text=(
                "verify you are human by completing the action below",
                "verifying you are human. this may take a few seconds",
                "needs to review the security of your connection before proceeding",
                "checking if the site connection is secure",
            ),
            found=CHALLENGE,
        ),
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

    def judge(self, view: PageView) -> PageAssessment:
        """The shared judge, except that a page showing `expect` with no Cloudflare sign (and
        no 407) is ok before the shared markers and statuses: the try's status is the first
        response's, and a managed challenge answers 403 and, once cleared, loads the real page
        in the same tab with no status of its own. The real page's own words ("captcha" on a
        captcha vendor's page) are no challenge either."""
        cleared = self.shown(view) and not any(mark.seen(view) for mark in self.MARKS)
        if cleared and view.status != PROXY_AUTH_STATUS:
            return OK
        return PageTarget.judge(self, view)

    def rules(self, view: PageView) -> PageAssessment:
        if self.shown(view):
            return OK
        if count(view, self.ERROR_DETAILS):
            return failed("no_content", "cf_error_page")
        if not (view.title.strip() or view.text_head.strip()):
            return failed("no_content", "empty_page")
        return failed("no_content", "no_expect")

    @staticmethod
    def shown(view: PageView) -> bool:
        """Whether the page shows the input's expected text."""
        return any(n for key, n in view.found.items() if key.startswith(TEXT_SELECTOR))
