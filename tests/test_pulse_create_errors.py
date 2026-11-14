"""PULSE-FLEET-FIX-1: why a pulse create failed (pulse_fleet.create_failure) and DigitalOcean's
error answer (do_api); no network."""

from __future__ import annotations

import pytest

from atlas.campaign import do_api, pulse_fleet
from atlas.campaign.do_api import ApiError, CreateError, Uncertain

PATH = "/v2/droplets"


class Traced:
    def http_trace(self, droplet_ids):
        return ["POST /v2/droplets 429"]


def stopped(error: ApiError) -> CreateError:
    return CreateError(error, [], ["pi-pulse-lon1-shifter"])


@pytest.mark.parametrize(
    "error, kind, message",
    [
        (None, "not_returned", "not confirmed: failed not in the answer"),
        (stopped(ApiError("POST", PATH, 429)), "http_429", "POST /v2/droplets failed (429)"),
        (
            stopped(ApiError("POST", PATH, None, "retries exhausted")),
            "no_answer",
            "POST /v2/droplets failed (retries exhausted)",
        ),
        (
            stopped(Uncertain("POST", PATH, None, "outcome unknown")),
            "uncertain",
            "POST /v2/droplets failed (outcome unknown)",
        ),
        (
            stopped(ApiError("POST", PATH, 422, code="unprocessable_entity", detail="bad image")),
            "unprocessable_entity",
            "POST /v2/droplets failed (422): bad image",
        ),
    ],
)
def test_every_failed_create_has_a_class_and_a_message(error, kind, message):
    found, detail = pulse_fleet.create_failure(Traced(), error)
    assert (found, detail["message"]) == (kind, message)
    assert detail["http"] == ["POST /v2/droplets 429"]


class Answer:
    def __init__(self, raw):
        self.raw = raw

    async def json(self, content_type=None):
        if isinstance(self.raw, Exception):
            raise self.raw
        return self.raw


@pytest.mark.parametrize(
    "raw, found",
    [
        (
            {"id": "unprocessable_entity", "message": "bad image"},
            ("unprocessable_entity", "bad image"),
        ),
        ({"id": "x" * 200, "message": None}, ("x" * 80, "")),
        (ValueError("not json"), ("", "")),
        (["a list"], ("", "")),
    ],
)
async def test_an_error_answer_gives_digitalocean_id_and_message(raw, found):
    assert await do_api._error_body(Answer(raw)) == found
