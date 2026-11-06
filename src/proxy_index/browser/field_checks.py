"""The shared field checks of the browser contracts (K-BP4) and the settings loader. No I/O.

Re-exported by `browser.contracts`, which callers import from. Errors name the field, never the
value (it can be a credential).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

# Shared field checks (the settings loader uses them too). `what` is the field's path.
# A record never carries page text, answers, cookies or credentials: those names are refused.
FORBIDDEN_FIELDS = frozenset(
    "text answer cookies username password credentials proxy token".split()
)


def check_fields(
    document: Any, names: tuple[str, ...], what: str, optional: tuple[str, ...] = ()
) -> dict[str, Any]:
    """The named fields of an object, required ones first; unknown fields are refused."""
    if not isinstance(document, dict):
        raise ValueError(f"{what}: must be a table or object")
    if forbidden := sorted(FORBIDDEN_FIELDS & set(document)):
        raise ValueError(f"{what}: must never hold {forbidden}")
    if unknown := sorted(set(document) - {*names, *optional}):
        raise ValueError(f"{what}: unknown fields {unknown}")
    if missing := [name for name in names if name not in document]:
        raise ValueError(f"{what}: missing fields {missing}")
    return {name: document[name] for name in (*names, *optional) if name in document}


def check_int(value: Any, what: str, low: int = 0, high: int = 2**63 - 1) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{what}: must be an integer from {low} to {high}")
    return value


def check_string(value: Any, what: str, pattern: str = "", form: str = "", size=512) -> str:
    """A non-blank string of at most `size` characters, matching `pattern` when given."""
    if not isinstance(value, str) or not 0 < len(value.strip()) <= size:
        raise ValueError(f"{what}: must be a non-blank string of at most {size} characters")
    if pattern and not re.fullmatch(pattern, value):
        raise ValueError(f"{what}: must be {form or 'in the documented form'}")
    return value


def optional(check: Callable[..., Any], value: Any, what: str, *args: Any) -> Any:
    return None if value is None else check(value, what, *args)
