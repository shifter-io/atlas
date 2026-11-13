"""K-BP2's network error tables: Firefox's and Chrome's error names to error codes.

contracts.classify_navigation_error reads them (docs/browser.html, Outcomes); exact names win
over prefixes. Chrome's names are `net::ERR_*` without the `net::` (driver.error_name).
"""

from __future__ import annotations

from ..models import ErrorCode

FIREFOX_ERRORS: dict[str, ErrorCode] = {
    "NS_ERROR_UNKNOWN_PROXY_HOST": "proxy_connect",
    "NS_ERROR_PROXY_AUTHENTICATION_FAILED": "proxy_auth",
    "NS_ERROR_NET_RESET": "connection",
    "NS_ERROR_NET_INTERRUPT": "connection",
    "NS_ERROR_CONNECTION_REFUSED": "connection",
    "NS_ERROR_UNKNOWN_HOST": "dns",
    "NS_ERROR_NET_TIMEOUT": "timeout",
    "TimeoutError": "timeout",  # Playwright's navigation and wait timeouts
    "NS_ERROR_NET_INADEQUATE_SECURITY": "tls",
}
FIREFOX_ERROR_PREFIXES: tuple[tuple[str, ErrorCode], ...] = (
    ("NS_ERROR_PROXY_", "proxy_connect"),
    ("SEC_ERROR_", "tls"),
    ("SSL_ERROR_", "tls"),
)
CHROME_ERRORS: dict[str, ErrorCode] = {
    "ERR_PROXY_CONNECTION_FAILED": "proxy_connect",
    "ERR_TUNNEL_CONNECTION_FAILED": "proxy_connect",
    "ERR_PROXY_AUTH_UNSUPPORTED": "proxy_auth",
    "ERR_PROXY_AUTH_REQUESTED": "proxy_auth",
    "ERR_INVALID_AUTH_CREDENTIALS": "proxy_auth",
    "ERR_CONNECTION_RESET": "connection",
    "ERR_CONNECTION_CLOSED": "connection",
    "ERR_CONNECTION_REFUSED": "connection",
    "ERR_CONNECTION_ABORTED": "connection",
    "ERR_EMPTY_RESPONSE": "connection",
    "ERR_HTTP2_PROTOCOL_ERROR": "connection",
    "ERR_NAME_NOT_RESOLVED": "dns",
    "ERR_TIMED_OUT": "timeout",
    "ERR_CONNECTION_TIMED_OUT": "timeout",
}
CHROME_ERROR_PREFIXES: tuple[tuple[str, ErrorCode], ...] = (
    ("ERR_SSL_", "tls"),
    ("ERR_CERT_", "tls"),
)
# Both engines' names; no name or prefix belongs to both.
ERRORS = FIREFOX_ERRORS | CHROME_ERRORS
ERROR_PREFIXES = FIREFOX_ERROR_PREFIXES + CHROME_ERROR_PREFIXES
