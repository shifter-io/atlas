"""What a request is counted as.

An HTTP 200 is not a success. A challenge page, a block page and a queue holding
page all return 200, which is how status-code-based success rates end up above
99% for networks that deliver nothing usable. Each target declares a written
pass condition (the echo-IP targets require a bare IP body); anything that
fails it is classified here.

Gateway-level codes are separated from target-level ones, because they mean
something entirely different: a 407 on one country and not another is a
statement about the network's coverage, not about the target.

Ported from collector/src/milf/engine/outcome.py. The framework adds ERROR, the
verdict for everything that is neither the target's nor the gateway's answer
(TLS, DNS, a dropped connection, a malformed body).
"""

from __future__ import annotations

from enum import StrEnum


class Verdict(StrEnum):
    """The outcomes every request lands in."""

    # The target's written pass condition was met.
    OK = "ok"
    # A CAPTCHA or interstitial arrived instead of the page. Counted as failure.
    CHALLENGE = "challenge"
    # Refused outright: 403, 429, a block page.
    BLOCK = "block"
    # No usable response inside the target's budget.
    TIMEOUT = "timeout"
    # The gateway itself failed: auth, no inventory, exhausted plan, tunnel error.
    PROXY_ERROR = "proxy_error"
    # Anything else: TLS, DNS, a dropped connection, an unusable body.
    ERROR = "error"


class GatewayCode(StrEnum):
    """Why the gateway refused, when it did."""

    # 407 while the same credentials work elsewhere: the country flag is unknown.
    UNSUPPORTED_GEO = "unsupported_geo"
    # 502 under strict matching: no addresses match the filter right now.
    NO_INVENTORY = "no_inventory"
    # 509: the plan's bandwidth is gone.
    BANDWIDTH_EXHAUSTED = "bandwidth_exhausted"
    # 407 everywhere: the credentials are wrong.
    BAD_CREDENTIALS = "bad_credentials"
    # Anything else the gateway said.
    GATEWAY_ERROR = "gateway_error"


# Status codes the gateway returns about itself rather than proxying.
GATEWAY_STATUSES: dict[int, GatewayCode] = {
    407: GatewayCode.UNSUPPORTED_GEO,
    502: GatewayCode.NO_INVENTORY,
    509: GatewayCode.BANDWIDTH_EXHAUSTED,
}

# Markers that identify a challenge even under a 200. Lower-cased comparison
# against the first few KB of the body.
CHALLENGE_MARKERS = (
    "captcha",
    "cf-challenge",
    "cf_chl_opt",
    "just a moment",
    "checking your browser",
    "attention required",
    "are you a robot",
    "verify you are human",
    "px-captcha",
    "recaptcha",
    "hcaptcha",
    "queue-it",
    "waiting room",
)

BLOCK_MARKERS = (
    "access denied",
    "request blocked",
    "forbidden",
    "you have been blocked",
    "unusual traffic",
    "rate limit",
)

BLOCK_STATUSES = frozenset({401, 403, 404, 405, 410, 418, 429, 451})

# Error codes whose verdict follows from the code alone.
TIMEOUT_CODES = frozenset({"timeout"})
PROXY_ERROR_CODES = frozenset({"proxy_auth", "proxy_connect"})
# Fine-grained error codes for a 200 whose body is a challenge or block page.
BODY_CODES = {"challenge": "challenge", "block": "blocked"}


def classify_status(status: int) -> Verdict:
    """Verdict implied by a status code alone, before the body is inspected."""
    if status in GATEWAY_STATUSES:
        return Verdict.PROXY_ERROR
    if status in BLOCK_STATUSES:
        return Verdict.BLOCK
    if 500 <= status < 600:
        # The target is broken rather than refusing us. Still not a success.
        return Verdict.BLOCK
    if 200 <= status < 400:
        return Verdict.OK
    return Verdict.BLOCK


def sniff_body(body: str | bytes) -> Verdict | None:
    """Look for a challenge or block page behind a 200. None means "looks fine"."""
    if isinstance(body, bytes):
        body = body[:4096].decode("utf-8", errors="replace")
    lowered = body[:4096].lower()
    if any(marker in lowered for marker in CHALLENGE_MARKERS):
        return Verdict.CHALLENGE
    if any(marker in lowered for marker in BLOCK_MARKERS):
        return Verdict.BLOCK
    return None


def gateway_code(status: int, *, geo_requested: bool) -> GatewayCode:
    """Interpret a gateway status.

    A 407 without a country flag can only mean the credentials are wrong; with
    one it much more often means the flag named a country the network does not
    carry, which is a coverage finding rather than a configuration error.
    """
    code = GATEWAY_STATUSES.get(status, GatewayCode.GATEWAY_ERROR)
    if code is GatewayCode.UNSUPPORTED_GEO and not geo_requested:
        return GatewayCode.BAD_CREDENTIALS
    return code


def failed_response(status: int, body: bytes) -> Verdict:
    """Verdict for a response that failed its target's pass condition.

    The body is sniffed first: a Cloudflare interstitial arrives as a 403 or a
    503 as often as a 200. A gateway status stays a proxy error whatever the
    body says. A status that is not itself a refusal (a 201, a redirect, a 200
    with a malformed body) is a plain error.
    """
    if status in GATEWAY_STATUSES:
        return Verdict.PROXY_ERROR
    sniffed = sniff_body(body)
    if sniffed is not None:
        return sniffed
    verdict = classify_status(status)
    return Verdict.ERROR if verdict is Verdict.OK else verdict


def from_error(error: str | None) -> Verdict:
    """Default verdict for an error code produced without a response to inspect."""
    if error is None:
        return Verdict.OK
    if error in TIMEOUT_CODES:
        return Verdict.TIMEOUT
    if error in PROXY_ERROR_CODES:
        return Verdict.PROXY_ERROR
    if error == "challenge":
        return Verdict.CHALLENGE
    if error == "blocked":
        return Verdict.BLOCK
    return Verdict.ERROR
