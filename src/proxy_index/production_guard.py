"""PROD-AS-STAGING-1 (#817): production's proxy_reviews as a write target only with three things.

The owner's dated go (--owner-go "owner-go PRODUCTION-AS-STAGING <YYYY-MM-DD>"), the
acknowledgement --i-know-this-is-production, and PROXY_INDEX_ALLOW_PRODUCTION_TARGET=1 in the
environment. The scale scripts (scripts/scale/target.py) and `proxy-index daily audits` use it.
"""

from __future__ import annotations

import os
import re
from datetime import date

PRODUCTION = "proxy_reviews"
PRODUCTION_GO = re.compile(r"owner-go PRODUCTION-AS-STAGING (\d{4}-\d{2}-\d{2})")
PRODUCTION_GO_FORM = "'owner-go PRODUCTION-AS-STAGING <YYYY-MM-DD>'"
ACKNOWLEDGE = "--i-know-this-is-production"
ALLOW = "PROXY_INDEX_ALLOW_PRODUCTION_TARGET"
NEEDS = f"--owner-go {PRODUCTION_GO_FORM}, {ACKNOWLEDGE} and {ALLOW}=1 in the environment"


def go_date_refusal(owner_go: str, today: date, pattern: re.Pattern, form: str) -> str | None:
    """None when the go has the owner's form (`pattern`) and a date that is not after `today`."""
    match = pattern.fullmatch(owner_go)
    try:
        given = date.fromisoformat(match.group(1)) if match else None
    except ValueError:
        given = None
    if given is None:
        return f"the owner's go {owner_go!r} is not {form}"
    if given > today:
        return f"the owner's go is dated {given}, after today ({today})"
    return None


def production_refusal(owner_go: str | None, acknowledged: bool, today: date) -> str | None:
    """None only with all three: the owner's dated production go, the acknowledgement and the
    environment's ALLOW=1; else the refusal, naming the three and the first one missing."""
    needs = f"{PRODUCTION!r} is production's: a target only with {NEEDS}"
    if not acknowledged:
        return f"{needs}; {ACKNOWLEDGE} is missing"
    if os.environ.get(ALLOW) != "1":
        return f"{needs}; {ALLOW} is not 1"
    found = go_date_refusal(owner_go or "", today, PRODUCTION_GO, PRODUCTION_GO_FORM)
    return f"{needs}; {found}" if found else None
