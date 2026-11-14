"""DigitalOcean's error answer (PULSE-FLEET-FIX-1): the `id` and `message` of a 4xx body, kept
on do_api's ApiError as `code` and `detail` so a refused create says why. Never a header or
the token: only these two fields of the body, cut to a bounded length."""

from __future__ import annotations

import aiohttp

CODE_LIMIT, DETAIL_LIMIT = 80, 300


async def error_body(response) -> tuple[str, str]:
    """DigitalOcean's error `id` and `message` from a 4xx answer; empty when it has none."""
    try:
        raw = await response.json(content_type=None)
    except (aiohttp.ClientError, ValueError):
        return "", ""
    if not isinstance(raw, dict):
        return "", ""
    return str(raw.get("id") or "")[:CODE_LIMIT], str(raw.get("message") or "")[:DETAIL_LIMIT]
