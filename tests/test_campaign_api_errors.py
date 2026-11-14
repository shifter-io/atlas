"""PULSE-FLEET-FIX-1: a 4xx answer keeps DigitalOcean's error id and message on the ApiError
(and a CreateError), never a header or the token (fake DigitalOcean; nothing leaves the
machine)."""

from __future__ import annotations

import pytest
from campaign_api_cases import OURS
from campaign_fakes import SID, FakeDigitalOcean, Fault, running

from atlas.campaign import do_errors
from atlas.campaign.do_api import CreateError

MESSAGE = "You specified an invalid image for Droplet creation."


async def test_a_refused_create_carries_digitaloceans_id_and_message():
    fake = FakeDigitalOcean()
    body = {"id": "unprocessable_entity", "message": MESSAGE}
    fake.faults.append(Fault("POST", "/v2/droplets", 422, body=body))
    async with running(fake) as cloud:
        with pytest.raises(CreateError) as caught:
            await cloud.create(
                [f"atlas-{SID}-h03-lon1-net1"],
                region="lon1",
                size="c-2",
                image="1",
                tags=list(OURS),
                ssh_keys=["9"],
            )
    error = caught.value
    assert (error.status, error.code, error.detail) == (422, "unprocessable_entity", MESSAGE)
    assert str(error) == "POST /v2/droplets failed (422)" and not error.uncertain


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
async def test_an_error_answer_gives_digitaloceans_id_and_message_or_nothing(raw, found):
    assert await do_errors.error_body(Answer(raw)) == found
