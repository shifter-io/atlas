"""Campaign contracts and the DigitalOcean client against the in-process fake (no live calls).

Listing, creating, retries, rate limits and errors; the CLI and token sources are in
test_campaign_api_cli.py, the scoped operations in test_campaign_api_operations.py.
"""

from __future__ import annotations

import json
import logging
import time

import pytest
from aiohttp import web
from campaign_api_cases import OURS
from campaign_fakes import CAMPAIGN_PROJECT, SID, TOKEN, FakeDigitalOcean, Fault, running

from proxy_index.campaign import do_api, scope
from proxy_index.campaign.contracts import MANAGER_TAG, WORKER_TAG
from proxy_index.campaign.do_api import ApiError, ScopeError


async def test_list_tag_paginates_200_per_page_and_parses_droplets():
    fake = FakeDigitalOcean()
    for index in range(450):
        fake.add(f"pic-{SID}-h03-lon1-n{index}", OURS, ip=None if index else "203.0.113.9")
    fake.add("prod-1", ["prod"])
    async with running(fake) as cloud:
        found = await cloud.list_tag(OURS[1])
    assert len(found) == 450 and found[0].public_ipv4 == "203.0.113.9"
    assert "203.0.113.9" not in repr(found[0])
    pages = [q for m, p, q, _ in fake.requests if p == "/v2/droplets"]
    assert [q["page"] for q in pages] == ["1", "2", "3"]
    assert {q["per_page"] for q in pages} == {"200"} and {q["tag_name"] for q in pages} == {OURS[1]}


async def test_create_splits_into_batches_of_ten_and_sends_every_option():
    fake = FakeDigitalOcean()
    names = [f"pic-{SID}-h03-lon1-net{i}" for i in range(16)]
    bodies = []
    original = fake._create

    async def capture(request):
        bodies.append(await request.json())
        return await original(request)

    fake._create = capture
    async with running(fake) as cloud:
        created = await cloud.create(
            names, region="lon1", size="c-2", image="11", tags=OURS, ssh_keys=["9"], monitoring=True
        )
    assert [len(b["names"]) for b in bodies] == [10, 6] and len(created) == 16
    assert bodies[0]["ssh_keys"] == ["9"] and bodies[0]["monitoring"] is True
    assert bodies[0]["with_droplet_agent"] is False and bodies[0]["ipv6"] is False


async def test_create_refuses_unknown_flags():
    async with running(FakeDigitalOcean()) as cloud:
        with pytest.raises(ScopeError):
            await cloud.create(
                [f"pic-{SID}-h03-lon1-x"],
                region="lon1",
                size="c-2",
                image="1",
                tags=OURS,
                ssh_keys=["9"],
                backups=True,
            )


@pytest.mark.parametrize(
    ("names", "tags", "keys"),
    [
        (["web-01"], OURS, ["9"]),
        ([f"pic-{SID}-h03-lon1-x"], [WORKER_TAG], ["9"]),
        ([f"pic-{SID}-h03-lon1-x"], [*OURS, "prod"], ["9"]),
        ([f"pic-{SID}-h03-lon1-x"], [*OURS, MANAGER_TAG], ["9"]),
        ([f"pic-{SID}-h03-lon1-x"], OURS, []),
        ([], OURS, ["9"]),
        ([f"pic-{SID}-h03-lon1-x", "pic-89abcdef-h03-lon1-x"], OURS, ["9"]),
        (["pi-camp-manager-20270101-ab12"] * 2, [MANAGER_TAG], ["9"]),
        (["pi-camp-builder-20270101-ab12"], [MANAGER_TAG], ["9"]),
    ],
)
async def test_create_refuses_anything_without_all_markers(names, tags, keys):
    fake = FakeDigitalOcean()
    async with running(fake) as cloud:
        with pytest.raises(ScopeError):
            await cloud.create(
                names, region="lon1", size="c-2", image="1", tags=tags, ssh_keys=keys
            )
    assert not fake.requests


async def test_manager_and_builder_create_and_project_assignment_body():
    fake = FakeDigitalOcean()
    async with running(fake) as cloud:
        [manager] = await cloud.create(
            ["pi-camp-manager-20270101-ab12"],
            region="lon1",
            size="s-8vcpu-16gb",
            image="1",
            tags=[MANAGER_TAG],
            ssh_keys=["9"],
        )
        await cloud.assign_project(CAMPAIGN_PROJECT, [manager.id], guard=scope._GUARD)
        assert await cloud.project_droplet_ids(CAMPAIGN_PROJECT) == {manager.id}
        assert await cloud.project_id("Proxy Index") == CAMPAIGN_PROJECT
        assert await cloud.project_name(CAMPAIGN_PROJECT) == "Proxy Index"
        assert await cloud.project_name("44444444-4444-4444-8444-444444444444") is None
        for bad in ([], ["do:droplet:1"]):
            with pytest.raises(ScopeError):
                await cloud.assign_project(CAMPAIGN_PROJECT, bad, guard=scope._GUARD)
        with pytest.raises(ScopeError):
            await cloud.project_id("Missing")
        assert await cloud.get("424242") is None
        assert cloud.created_ids == {manager.id}
    assert manager.id in fake.projects[CAMPAIGN_PROJECT]


async def test_list_tag_refuses_tags_outside_our_prefix():
    fake = FakeDigitalOcean()
    async with running(fake) as cloud:
        for tag in ("prod", "", "camp-x"):
            with pytest.raises(ScopeError):
                await cloud.list_tag(tag)
    assert not fake.requests


@pytest.mark.parametrize(
    ("fault", "sleeps"),
    [
        (Fault("GET", "/v2/account", 429, {"Retry-After": "5"}), [5.0]),
        (Fault("GET", "/v2/account", 429, {"Retry-After": "900"}), [60.0]),
        (Fault("GET", "/v2/account", 429), [1]),
        (Fault("GET", "/v2/account", 503, times=2), [1, 2]),
    ],
)
async def test_retries_honour_rate_limits_and_back_off(fault, sleeps):
    fake, slept = FakeDigitalOcean(), []
    fake.faults.append(fault)

    async def sleep(seconds):
        slept.append(seconds)

    async with running(fake, sleep=sleep) as cloud:
        assert await cloud.account() == {"droplet_limit": 10000, "status": "active"}
        assert cloud.ratelimit_remaining == 4321
    assert slept == sleeps


async def test_rate_limit_reset_time_is_honoured():
    fake, slept = FakeDigitalOcean(), []
    reset = str(int(time.time()) + 30)
    fake.faults.append(Fault("GET", "/v2/account", 429, {"ratelimit-reset": reset}))

    async def sleep(seconds):
        slept.append(seconds)

    async with running(fake, sleep=sleep) as cloud:
        await cloud.account()
    assert len(slept) == 1 and 1.0 <= slept[0] <= 31


async def test_a_partial_create_keeps_the_droplets_already_created():
    """S1: batch 2 fails; the 10 droplets of batch 1 are returned on the error, not lost."""
    fake = FakeDigitalOcean()
    names = [f"pic-{SID}-h03-lon1-net{i}" for i in range(16)]
    original, calls = fake._create, []

    async def second_fails(request):
        calls.append(1)
        if len(calls) == 2:
            return web.json_response({"id": "unprocessable"}, status=422)
        return await original(request)

    fake._create = second_fails
    async with running(fake) as cloud:
        with pytest.raises(do_api.CreateError) as stopped:
            await cloud.create(
                names, region="lon1", size="c-2", image="1", tags=OURS, ssh_keys=["9"]
            )
    error = stopped.value
    assert [d.name for d in error.created] == names[:10] and error.pending == names[10:]
    assert not error.uncertain and error.status == 422
    assert {d.id for d in error.created} == set(fake.droplets) == cloud.created_ids


async def test_an_uncertain_create_is_never_retried_and_can_be_adopted():
    """S1: a create answered 5xx may exist; one POST only, then adopt by exact name."""
    fake = FakeDigitalOcean()
    fake.create_then_fail = 1
    fake.add(f"pic-{SID}-h03-lon1-other", OURS)  # same tag, another name: not adopted
    name = f"pic-{SID}-h03-lon1-shifter"
    async with running(fake) as cloud:
        with pytest.raises(do_api.CreateError) as stopped:
            await cloud.create(
                [name], region="lon1", size="c-2", image="1", tags=OURS, ssh_keys=["9"]
            )
        assert stopped.value.uncertain and stopped.value.pending == [name]
        assert not stopped.value.created and not cloud.created_ids
        [adopted] = await cloud.adopt_uncertain([name], OURS[2])
    posts = [r for r in fake.requests if r[:2] == ("POST", "/v2/droplets")]
    assert len(posts) == 1 and len(fake.droplets) == 2
    assert adopted.name == name and cloud.created_ids == {adopted.id}


async def test_pagination_without_a_total_is_an_error():
    """m5: a page with no meta.total never silently ends a listing."""
    fake = FakeDigitalOcean()
    fake.add(f"pic-{SID}-h03-lon1-x", OURS)
    original = fake._list

    async def no_meta(request):
        response = await original(request)
        body = json.loads(response.body)
        body.pop("meta")
        return web.json_response(body)

    fake._list = no_meta
    async with running(fake) as cloud:
        with pytest.raises(ApiError):
            await cloud.list_tag(OURS[1])


async def test_errors_after_retries_or_on_4xx_carry_no_token(caplog):
    caplog.set_level(logging.DEBUG)
    fake, slept = FakeDigitalOcean(), []
    fake.faults += [Fault("GET", "/v2/account", 500, times=5), Fault("POST", "/v2/droplets", 422)]

    async def sleep(seconds):
        slept.append(seconds)

    async with running(fake, sleep=sleep) as cloud:
        with pytest.raises(ApiError) as exhausted:
            await cloud.account()
        with pytest.raises(ApiError) as rejected:
            await cloud.create(
                [f"pic-{SID}-h03-lon1-x"],
                region="lon1",
                size="c-2",
                image="1",
                tags=OURS,
                ssh_keys=["9"],
            )
        text = " ".join(
            [
                repr(cloud),
                str(exhausted.value),
                str(rejected.value),
                repr(exhausted.value.__context__),
                caplog.text,
            ]
        )
    assert slept == [1, 2, 4, 8] and rejected.value.status == 422
    assert TOKEN not in text and "Bearer" not in text
    assert exhausted.value.__cause__ is None and rejected.value.__cause__ is None


async def test_connection_errors_are_retried_then_raised():
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    cloud = do_api.DigitalOcean(lambda: TOKEN, base_url="http://127.0.0.1:9", sleep=sleep)
    async with cloud:
        with pytest.raises(ApiError):
            await cloud.account()
    assert slept == [1, 2, 4, 8]


async def test_the_limiter_allows_at_most_the_configured_requests_per_minute():
    now, slept = [0.0], []

    async def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    async with running(
        FakeDigitalOcean(), sleep=sleep, clock=lambda: now[0], per_minute=2
    ) as cloud:
        for _ in range(3):
            await cloud.account()
            now[0] += 1
        now[0] += 120
        await cloud.account()
    assert slept == [58.0]
