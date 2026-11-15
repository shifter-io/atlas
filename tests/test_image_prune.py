"""IMAGE-DAILY-1: after the switch the old worker images are removed through the scope guard,
only once nothing uses them, and exactly one is kept (fake DigitalOcean; no cloud call)."""

from __future__ import annotations

import json

import pytest
from campaign_fakes import FakeDigitalOcean, Fault, running
from image_daily_cases import (
    NEW_NAME,
    OLD_ID,
    OLD_NAME,
    RELEASE_SHA,
    Alerts,
    Clock,
    Reader,
    Shell,
    manager,
    old_build,
    old_image,
    write_builds,
)

from atlas.campaign import image_daily, image_prune, ops_api, scope
from atlas.campaign.contracts import PULSE_TAG
from atlas.campaign.image_daily import Context
from atlas.regions import REGION_IDS

LEGACY = "pi-camp-worker-0.9.1-0123456789ab-20261001"


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr("maxminddb.open_database", lambda _path: Reader())
    original = ops_api.assign_and_verify_project

    async def assign(*args, **kwargs):
        return await original(*args, retry=scope.Retry(sleep=Clock().sleep), **kwargs)

    monkeypatch.setattr(ops_api, "assign_and_verify_project", assign)


@pytest.fixture
def paths(tmp_path):
    return manager(tmp_path)


def records(paths):
    return {b["started_at"]: b for b in json.loads(paths.images.read_text())["builds"]}


def audit(paths):
    lines = paths.images.with_name("campaign-ops-audit.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


async def apply(fake, paths, clock=None, alerts=None):
    clock, alerts = clock or Clock(), alerts or Alerts([])
    async with running(fake) as cloud:
        outcome = await image_daily.run(Context(cloud, paths, alerts, clock, Shell()))
    return outcome, clock, alerts


def worker_from(fake, image_id, tag=PULSE_TAG):
    droplet = fake.add("atlas-pulse-lon1-shifter", [tag])
    fake.droplets[droplet]["image"] = {"id": int(image_id)}
    return droplet


async def test_after_the_switch_the_old_image_goes_through_the_guard(paths):
    fake = FakeDigitalOcean()
    old_image(fake)
    outcome, _, alerts = await apply(fake, paths)
    found = outcome["prune"]
    assert outcome["state"] == "switched" and found["removed"] == [OLD_ID]
    assert found["exactly_one"] is True and found["kept"]["name"] == NEW_NAME
    assert OLD_ID not in fake.images and ("snapshot", OLD_ID) in fake.deleted
    old = records(paths)["2026-11-13T10:00:00+00:00"]
    assert "snapshot_id" not in old and old["removed_snapshot_id"] == OLD_ID
    assert [(a["kind"], a["result"]) for a in audit(paths) if a["id"] == OLD_ID] == [
        ("snapshot", "attempt"),
        ("snapshot", "deleted"),
    ]
    assert alerts.sent == []


async def test_the_old_image_waits_for_the_hour_launched_before_the_switch(paths):
    fake = FakeDigitalOcean()
    old_image(fake)
    droplet = worker_from(fake, OLD_ID)

    class Ending(Clock):
        async def sleep(self, seconds):
            await super().sleep(seconds)
            if self.sleeps.count(image_prune.WAIT_POLL_SECONDS) == 2:
                fake.droplets.pop(droplet, None)  # the hour ended and removed its droplet

    outcome, clock, _ = await apply(fake, paths, Ending())
    assert outcome["prune"]["removed"] == [OLD_ID] and OLD_ID not in fake.images
    assert clock.sleeps.count(image_prune.WAIT_POLL_SECONDS) == 2


async def test_an_image_still_in_use_after_the_wait_is_left_for_the_next_run(paths):
    fake = FakeDigitalOcean()
    old_image(fake)
    worker_from(fake, OLD_ID, "atlas-camp-worker")
    outcome, clock, alerts = await apply(fake, paths)
    found = outcome["prune"]
    assert found["removed"] == [] and found["held"] == {OLD_ID: "in use"}
    assert OLD_ID in fake.images and found["exactly_one"] is False
    waited = clock.sleeps.count(image_prune.WAIT_POLL_SECONDS) * image_prune.WAIT_POLL_SECONDS
    assert waited == image_prune.WAIT_LIMIT_SECONDS and alerts.sent == []


async def test_an_image_another_environments_template_names_is_kept_and_reported(paths, tmp_path):
    staging = manager(tmp_path / "other", fleet=False)
    target = tmp_path / "staging"
    staging.template.parents[1].rename(target)
    (target / "daily" / "template").unlink()
    (target / "daily" / "template").symlink_to(next((target / "campaigns").iterdir()))
    fake = FakeDigitalOcean()
    old_image(fake)
    outcome, _, alerts = await apply(fake, paths)
    found = outcome["prune"]
    assert found["removed"] == [] and found["referenced_elsewhere"] == [OLD_ID]
    assert OLD_ID in fake.images
    [(_, message)] = alerts.sent
    assert f"another template or fleet names {OLD_ID}" in message


async def test_an_unrecorded_old_worker_snapshot_is_only_reported(paths):
    fake = FakeDigitalOcean()
    old_image(fake)
    old_image(fake, "400", LEGACY)
    outcome, _, alerts = await apply(fake, paths)
    found = outcome["prune"]
    assert found["removed"] == [OLD_ID] and "400" in fake.images
    assert found["unrecorded"] == [LEGACY] and found["exactly_one"] is False
    [(_, message)] = alerts.sent
    assert f"not recorded, left alone: {LEGACY}" in message


async def test_a_recorded_image_with_another_name_is_never_removed(paths):
    fake = FakeDigitalOcean()
    old_image(fake, OLD_ID, LEGACY)
    write_builds(paths, old_build(snapshot_name=LEGACY))
    outcome, _, alerts = await apply(fake, paths)
    found = outcome["prune"]
    assert found["held"] == {OLD_ID: "not an atlas-camp-worker image"}
    assert OLD_ID in fake.images and found["unrecorded"] == []
    [(_, message)] = alerts.sent
    assert f"recorded but not named atlas-camp-worker-*, left alone: {OLD_ID}" in message


async def test_nothing_to_build_still_finishes_an_earlier_removal(paths):
    fake = FakeDigitalOcean()
    old_image(fake, "600", NEW_NAME)
    old_image(fake)
    current = old_build(
        started_at="2026-11-15T05:45:00+00:00",
        snapshot_id="600",
        snapshot_name=NEW_NAME,
        mmdb_sha256=RELEASE_SHA,
    )
    write_builds(paths, old_build(), current)
    spec = paths.template / "campaign-spec.json"
    raw = json.loads(spec.read_text())
    raw |= {"image_by_region": dict.fromkeys(raw["image_by_region"], "600"), "image_name": NEW_NAME}
    spec.write_text(json.dumps(raw))
    fleet = json.loads(paths.fleet.read_text())
    paths.fleet.write_text(json.dumps(fleet | {"image_by_region": {"lon1": 600, "fra1": 600}}))
    outcome, _, _ = await apply(fake, paths)
    assert outcome["state"] == "unchanged" and outcome["prune"]["removed"] == [OLD_ID]
    assert sorted(fake.images) == ["600"]


async def test_a_prune_error_keeps_the_switch_and_alerts_once(paths):
    fake = FakeDigitalOcean()
    old_image(fake)
    fake.faults.append(Fault("GET", "/v2/snapshots", 403, times=1))
    outcome, _, alerts = await apply(fake, paths)
    assert outcome["state"] == "switched" and outcome["prune"] == {"error": "prune: ApiError"}
    [(_, message)] = alerts.sent
    assert message.endswith("prune: ApiError")


async def test_the_kept_image_must_be_everywhere_before_anything_goes(paths):
    fake = FakeDigitalOcean()
    old_image(fake)
    old_image(fake, "600", NEW_NAME, regions=["lon1"])
    async with running(fake) as cloud:
        context = Context(cloud, paths, Alerts([]), Clock())
        with pytest.raises(image_daily.DailyImageError, match="not in every region"):
            await image_prune.prune(context, {"id": "600", "name": NEW_NAME})
    assert OLD_ID in fake.images and sorted(REGION_IDS) != fake.images["600"]["regions"]


async def test_a_refused_removal_is_reported(paths, monkeypatch):
    async def refuse(*_args, **_kwargs):
        raise scope.OwnershipError("snapshot", OLD_ID, ["name"])

    monkeypatch.setattr(ops_api, "release", refuse)
    fake = FakeDigitalOcean()
    old_image(fake)
    outcome, _, alerts = await apply(fake, paths)
    assert outcome["prune"]["refused"] == [OLD_ID] and OLD_ID in fake.images
    assert outcome["prune"]["held"] == {OLD_ID: "refused: name"}
    [(_, message)] = alerts.sent
    assert f"refused {OLD_ID}" in message and OLD_NAME not in message
