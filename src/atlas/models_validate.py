"""RunConfig's self-contained checks (models.py calls them from RunConfig.__post_init__)."""

from __future__ import annotations

import math
from typing import Any
from urllib.parse import urlsplit


def normalized_target_url(target: str, value: object) -> str:
    """A generic target's URL, normalised; it must use the target's scheme, without
    credentials or a fragment."""
    try:
        expected = {"generic_http": "http", "generic_https": "https"}[target]
        if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError
        url = urlsplit(value)
        if (
            url.scheme != expected
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.fragment
            or (url.port is not None and not 1 <= url.port <= 65535)
        ):
            raise ValueError
        return url.geturl()
    except (KeyError, ValueError):
        raise ValueError(
            "Target URL must match the generic target protocol, without credentials or a fragment"
        ) from None


def check_limits(config: Any) -> None:
    """A RunConfig's stop rule, counts and durations."""
    if config.stop_rule not in ("count", "unique_ratio"):
        raise ValueError("stop_rule must be 'count' or 'unique_ratio'")
    if config.requests is None and config.stop_rule == "count":
        raise ValueError("A count stop rule requires a request count")
    if config.cap_seconds is not None and config.stop_rule != "unique_ratio":
        raise ValueError("cap_seconds requires the unique-ratio census rule")
    low = (config.low_requests, config.low_seconds)
    if low != (None, None) and config.stop_rule != "unique_ratio":
        raise ValueError("low_requests and low_seconds require the unique-ratio census rule")
    for name in ("requests", "concurrency", "min_sample", "low_requests"):
        value = getattr(config, name)
        if value is None and name in ("requests", "low_requests"):
            continue
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    start = config.start_concurrency
    if start is not None and (type(start) is not int or not 0 < start <= config.concurrency):
        raise ValueError("start_concurrency must be a positive integer up to concurrency")
    most = config.grow_ceiling
    if most is not None and (
        type(most) is not int or not (start or 1) <= most <= config.concurrency
    ):
        raise ValueError("grow_ceiling must be an integer from start_concurrency to concurrency")
    for name in (
        "timeout_seconds",
        "requests_per_second",
        "stall_seconds",
        "cap_seconds",
        "low_seconds",
    ):
        value = getattr(config, name)
        if value is None and name in ("timeout_seconds", "stall_seconds"):
            raise ValueError(f"{name} must be finite and positive")
        if value is not None and (
            type(value) not in (float, int) or not math.isfinite(value) or value <= 0
        ):
            raise ValueError(f"{name} must be finite and positive")
