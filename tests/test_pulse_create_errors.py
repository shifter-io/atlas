"""PULSE-FLEET-FIX-1: why a pulse create failed (pulse_fleet.create_failure); no network.
DigitalOcean's error answer itself is in test_campaign_api_errors.py (the scope gate's files)."""

from __future__ import annotations

import pytest

from atlas.campaign import pulse_fleet
from atlas.campaign.do_api import ApiError, CreateError, OldImageError, ScopeError, Uncertain

PATH = "/v2/droplets"


class Traced:
    def http_trace(self, droplet_ids):
        return ["POST /v2/droplets 429"]


def stopped(error: ApiError, code: str = "", detail: str = "") -> CreateError:
    error.code, error.detail = code, detail
    return CreateError(error, [], ["atlas-pulse-lon1-shifter"])


@pytest.mark.parametrize(
    "error, kind, message",
    [
        (None, "not_returned", "not confirmed: failed not in the answer"),
        (stopped(ApiError("POST", PATH, 429)), "http_429", "POST /v2/droplets failed (429)"),
        (
            stopped(ApiError("POST", PATH, None, "retries exhausted")),
            "no_answer",
            "POST /v2/droplets failed (create stopped)",
        ),
        (
            stopped(Uncertain("POST", PATH, None, "outcome unknown")),
            "uncertain",
            "POST /v2/droplets failed (create stopped)",
        ),
        (
            stopped(ApiError("POST", PATH, 422), "unprocessable_entity", "bad image"),
            "unprocessable_entity",
            "POST /v2/droplets failed (422): bad image",
        ),
        (
            OldImageError("image 1 (an old name) was built before the rename to Atlas"),
            "OldImageError",
            "image 1 (an old name) was built before the rename to Atlas",
        ),
        (ScopeError("unknown create flag"), "ScopeError", "unknown create flag"),
    ],
)
def test_every_failed_create_has_a_class_and_a_message(error, kind, message):
    found, detail = pulse_fleet.create_failure(Traced(), error)
    assert (found, detail["message"]) == (kind, message)
    assert detail["http"] == ["POST /v2/droplets 429"]
