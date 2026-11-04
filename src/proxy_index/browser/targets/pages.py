"""The five page targets of K-BP1 and their pass conditions ("real content, not just 200").

Each target names its page per input, the selectors the transport waits for and counts, and
its own rules after browser.judge's shared steps. The selectors and marks are this version's
reading of the public pages; a change to one bumps the target's `version`. Each target lives
in its own module; this one gathers them, and proxy_index.targets registers them from here.
"""

from __future__ import annotations

from .amazon import AmazonPdp
from .cloudflare import CloudflareChallenge
from .google_search import GoogleSearch
from .instagram import InstagramWeb
from .linkedin import LinkedinPublic

__all__ = [
    "PAGE_TARGET_TYPES",
    "AmazonPdp",
    "CloudflareChallenge",
    "GoogleSearch",
    "InstagramWeb",
    "LinkedinPublic",
]

PAGE_TARGET_TYPES = (AmazonPdp, GoogleSearch, LinkedinPublic, InstagramWeb, CloudflareChallenge)
