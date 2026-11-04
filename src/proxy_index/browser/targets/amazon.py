"""The Amazon product page target of K-BP1 (amazon_pdp); see browser.targets.pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ...targets import PageView
from ..contracts import AMAZON_PDP
from ..judge import OK, Mark, PageAssessment, count, failed, path_has
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
    # Amazon's 503 and 404 error pages link to the "dogs of Amazon" gallery. Waiting for the
    # link ends the wait on them; it marks nothing, since the 404 page has it too.
    DOGS_LINK = 'a[href*="/dogsofamazon"]'
    SIGN_IN = ("/ap/signin",)
    NOT_FOUND_STATUS = 404
    MARKS = (
        Mark(
            "challenge",
            "captcha_page",
            text=(
                "type the characters you see",
                "enter the characters you see",
                # The robot check's own wording, also when its form is not counted.
                "we just need to make sure you",
                "to discuss automated access to amazon data",
            ),
            paths=("/errors/validatecaptcha",),
            found=(CAPTCHA_FORM,),
        ),
        # The "dogs" error page Amazon serves to refused clients, by its own title: a plain
        # "something went wrong" is also the text of a shown page's small error notes.
        Mark("blocked", "dogs_page", text=("sorry! something went wrong",)),
    )
    BLOCK_STATUSES = frozenset({503})

    def address(self, value: Any) -> str:
        return f"{self.url}{quote(value, safe='')}"

    def wanted(self, value: Any) -> tuple[str, ...]:
        return (self.TITLE, *self.PRICES, self.CAPTCHA_FORM, self.DOGS_LINK)

    def rules(self, view: PageView) -> PageAssessment:
        if path_has(view, self.SIGN_IN):
            return failed("login_wall", "login_redirect")
        if view.status == self.NOT_FOUND_STATUS:
            # An ASIN Amazon does not know (or no longer sells): a stale input, not a refusal.
            return failed("no_content", "not_found")
        title, price = count(view, self.TITLE), count(view, *self.PRICES)
        if title and price:
            return OK
        if title:
            return failed("no_content", "no_price")
        if price:
            return failed("no_content", "no_title")
        if not (view.title.strip() or view.text_head.strip()):
            return failed("no_content", "empty_page")
        return failed("no_content", "no_product")
