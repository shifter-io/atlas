"""Shared classification of what a browser try saw (K-BP2; docs/browser.html, "success").

Every browser target judges its PageView through judge(): a main-document 407 first, then the
site's own challenge and block marks, the shared challenge and block markers of outcome.py on the
title and the first 4 KB of visible text, the block statuses, and only then the target's own
pass condition. Navigation errors never reach a judge: the transport maps them first. The result
is a PageAssessment, an Assessment with a short lower-case `reason` that the try records.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..outcome import BLOCK_STATUSES, Verdict, sniff_body
from ..targets import Assessment, PageView
from .contracts import CODE_VERDICTS, PROXY_AUTH_STATUS, TOKEN

# The reasons of the shared steps; a target names its own (captcha_page, no_price, ...).
PROXY_AUTH = "proxy_auth_status"
CHALLENGE_MARKER = "challenge_marker"
BLOCK_MARKER = "block_marker"
BLOCK_STATUS = "block_status"


@dataclass(frozen=True, slots=True)
class PageAssessment(Assessment):
    """An Assessment whose verdict is the one its error code implies (ok: no code), plus the
    judge's `reason`, a lower-case enum such as `captcha_page`; None for a success."""

    reason: str | None = None

    def __post_init__(self) -> None:
        if self.verdict != (CODE_VERDICTS.get(self.error) if self.error else "ok"):
            raise ValueError("verdict: must be the one its error code implies")
        if self.reason is not None and not re.fullmatch(TOKEN, self.reason):
            raise ValueError("reason: must be a lower-case enum")


OK = PageAssessment(verdict="ok")


def failed(code: str, reason: str) -> PageAssessment:
    """A failed try with this K-BP2 error code (its verdict follows from it) and reason."""
    if code not in CODE_VERDICTS:
        raise ValueError("error: not a browser error code")
    return PageAssessment(error=code, verdict=CODE_VERDICTS[code], reason=reason)


@dataclass(frozen=True, slots=True)
class Mark:
    """A site's own sign of a challenge or block page (code `challenge` or `blocked`): any of
    its lower-case texts in the title or visible text, path prefixes of the final URL (see
    path_has), or selectors found on the page."""

    code: str
    reason: str
    text: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    found: tuple[str, ...] = ()

    def seen(self, view: PageView) -> bool:
        head = f"{view.title}\n{view.text_head}".lower()
        return (
            any(text in head for text in self.text)
            or path_has(view, self.paths)
            or any(view.found.get(selector, 0) > 0 for selector in self.found)
        )


def path_has(view: PageView, prefixes: Collection[str]) -> bool:
    """Whether the final URL's lower-cased path starts with any of these whole path segments:
    `/login` matches `/login` and `/login/...`, never `/loginov` or `/in/login`; a prefix
    ending in `/` matches anything below it."""
    path = urlsplit(view.final_url).path.lower()
    return any(
        path == prefix or path.startswith(prefix if prefix.endswith("/") else f"{prefix}/")
        for prefix in prefixes
    )


def host_of(view: PageView) -> str:
    return (urlsplit(view.final_url).hostname or "").lower()


def count(view: PageView, *selectors: str) -> int:
    """The total match count of these selectors (a selector the page did not report is 0)."""
    return sum(view.found.get(selector, 0) for selector in selectors)


def judge(
    view: PageView,
    rules: Callable[[PageView], PageAssessment],
    marks: tuple[Mark, ...] = (),
    block_statuses: Collection[int] = (),
) -> PageAssessment:
    """The try's outcome: 407, then challenge (site marks, shared markers), then block (site
    marks, shared markers, statuses), then `rules`, the target's own pass condition."""
    if view.status == PROXY_AUTH_STATUS:
        return failed("proxy_auth", PROXY_AUTH)
    for mark in marks:
        if mark.seen(view):
            return failed(mark.code, mark.reason)
    sniffed = {sniff_body(view.title), sniff_body(view.text_head)}
    if Verdict.CHALLENGE in sniffed:
        return failed("challenge", CHALLENGE_MARKER)
    if Verdict.BLOCK in sniffed:
        return failed("blocked", BLOCK_MARKER)
    if view.status in BLOCK_STATUSES or view.status in block_statuses:
        return failed("blocked", BLOCK_STATUS)
    return rules(view)


# PageView.found holds one count per selector of the target's PageSpec.wait_for, a CSS
# selector list: the transport counts each selector that selectors() returns, under its text.
_TOKENS = re.compile(r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[()\[\],]""")


def selectors(wait_for: str | None) -> tuple[str, ...]:
    """The selectors of a selector list, split at its top-level commas (commas inside quotes,
    brackets or parentheses stay), each stripped; an empty or absent list has none."""
    parts, depth, start = [], 0, 0
    for token in _TOKENS.finditer(wait_for or ""):
        mark = token.group()
        if mark in "([":
            depth += 1
        elif mark in ")]":
            depth -= 1
        elif mark == "," and depth == 0:
            parts.append(wait_for[start : token.start()])
            start = token.end()
    parts.append((wait_for or "")[start:])
    return tuple(part.strip() for part in parts if part.strip())


def selector_list(*parts: str) -> str:
    """The wait_for list of these selectors; selectors() gives them back unchanged."""
    return ", ".join(parts)


TEXT_SELECTOR = "body:has-text("


def text_selector(text: str) -> str:
    """A selector matching the body when its text contains `text` (whitespace collapsed), so a
    judge can read an input's expected text from PageView.found."""
    return f"{TEXT_SELECTOR}{json.dumps(' '.join(text.split()), ensure_ascii=False)})"
