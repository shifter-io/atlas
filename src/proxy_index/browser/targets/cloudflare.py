"""The Cloudflare managed challenge target of K-BP1 (cloudflare_challenge); see
browser.targets.pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...targets import PageDriver, PageSpec, PageView
from ..contracts import CLOUDFLARE_CHALLENGE
from ..judge import (
    BLOCK_STATUS,
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

    SHOWN_OK = True
    # A managed challenge's first response; the page it clears to loads with no status of its own.
    CHALLENGE_STATUS = 403

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
        """PageTarget.judge, except that a 403 page with visible text and no Cloudflare sign or
        shared marker is the page a cleared challenge loaded, judged by the rules (no_expect
        when it lacks `expect`), not a refusal. A blank 403 stays a block."""
        result = PageTarget.judge(self, view)
        if (
            result.reason == BLOCK_STATUS
            and view.status == self.CHALLENGE_STATUS
            and view.text_head.strip()
        ):
            return self.rules(view)
        return result

    def shown(self, view: PageView) -> bool:
        """The page shows `expect` with no Cloudflare sign: ok right after the 407 step
        (SHOWN_OK), before the shared markers and statuses. The try's status is the first
        response's, and a managed challenge answers 403 and, once cleared, loads the real page
        in the same tab with no status of its own. The real page's own words ("captcha" on a
        captcha vendor's page) are no challenge either."""
        return self.expected(view) and not any(mark.seen(view) for mark in self.MARKS)

    def rules(self, view: PageView) -> PageAssessment:
        if self.expected(view):
            return OK
        if count(view, self.ERROR_DETAILS):
            return failed("no_content", "cf_error_page")
        if not (view.title.strip() or view.text_head.strip()):
            return failed("no_content", "empty_page")
        return failed("no_content", "no_expect")

    @staticmethod
    def expected(view: PageView) -> bool:
        """Whether the page shows the input's expected text."""
        return any(n for key, n in view.found.items() if key.startswith(TEXT_SELECTOR))
