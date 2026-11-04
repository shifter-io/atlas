"""The Amazon product page target of K-BP1 (amazon_pdp); see browser.targets.pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ...targets import PageView
from ..contracts import AMAZON_PDP
from ..judge import OK, Mark, PageAssessment, count, failed
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
