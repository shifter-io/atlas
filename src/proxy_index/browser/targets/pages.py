"""The five page targets of K-BP1 and their pass conditions ("real content, not just 200").

Each target names its page per input, the selectors the transport waits for and counts, and
its own rules after browser.judge's shared steps. The selectors and marks are this version's
reading of the public pages; a change to one bumps the target's `version`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from ...targets import PageDriver, PageSpec, PageView
from ..contracts import (
    AMAZON_PDP,
    CLOUDFLARE_CHALLENGE,
    GOOGLE_SEARCH,
    INSTAGRAM_WEB,
    LINKEDIN_PUBLIC,
)
from ..judge import (
    OK,
    TEXT_SELECTOR,
    Mark,
    PageAssessment,
    count,
    failed,
    host_of,
    path_has,
    selector_list,
    selectors,
    text_selector,
)
from . import PageTarget


@dataclass(frozen=True, slots=True)
class AmazonPdp(PageTarget):
    """Marketplace product page: ok when the product title and a price are visible."""

    id: str = AMAZON_PDP
    url: str = "https://www.amazon.com/dp/"

    TITLE = "#productTitle"
    PRICES = (
        "#corePrice_feature_div .a-price",
        "#corePriceDisplay_desktop_feature_div .a-price",
        "#price_inside_buybox",
        "#price",
    )
    CAPTCHA_FORM = 'form[action*="validateCaptcha"]'
    MARKS = (
        Mark(
            "challenge",
            "captcha_page",
            text=("type the characters you see", "enter the characters you see"),
            paths=("/errors/validatecaptcha",),
            found=(CAPTCHA_FORM,),
        ),
        # The "dogs" error page Amazon serves to refused clients.
        Mark("blocked", "dogs_page", text=("something went wrong",)),
    )
    BLOCK_STATUSES = frozenset({503})

    def address(self, value: Any) -> str:
        return f"{self.url}{quote(value, safe='')}"

    def wanted(self, value: Any) -> tuple[str, ...]:
        return (self.TITLE, *self.PRICES, self.CAPTCHA_FORM)

    def rules(self, view: PageView) -> PageAssessment:
        title, price = count(view, self.TITLE), count(view, *self.PRICES)
        if title and price:
            return OK
        if title:
            return failed("no_content", "no_price")
        return failed("no_content", "no_title" if price else "no_product")


@dataclass(frozen=True, slots=True)
class GoogleSearch(PageTarget):
    """Search results page: ok when the results container holds at least 3 organic links."""

    id: str = GOOGLE_SEARCH
    url: str = "https://www.google.com/search"

    RESULTS = "#search a h3"
    MIN_RESULTS = 3
    CAPTCHA_FORM = "form#captcha-form"
    CONSENT_FORM = 'form[action*="consent.google"]'
    MARKS = (Mark("challenge", "sorry_page", paths=("/sorry/",), found=(CAPTCHA_FORM,)),)

    def address(self, value: Any) -> str:
        return f"{self.url}?{urlencode({'q': value, 'hl': 'en', 'gl': 'us'})}"

    def wanted(self, value: Any) -> tuple[str, ...]:
        return (self.RESULTS, self.CAPTCHA_FORM, self.CONSENT_FORM)

    def rules(self, view: PageView) -> PageAssessment:
        # The transport accepts a consent wall once, when it hides the results; a consent
        # banner left beside the results does not count against them.
        if host_of(view).startswith("consent."):
            return failed("no_content", "consent_page")
        if count(view, self.RESULTS) >= self.MIN_RESULTS:
            return OK
        if count(view, self.CONSENT_FORM):
            return failed("no_content", "consent_page")
        return failed("no_content", "few_results")


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


PAGE_TARGET_TYPES = (AmazonPdp, GoogleSearch, LinkedinPublic, InstagramWeb, CloudflareChallenge)
