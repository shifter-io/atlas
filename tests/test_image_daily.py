"""IMAGE-DAILY-1: the worker image rebuilt with the manager's DB-IP release, the template and the
pulse fleet switched at a quiet minute (fake DigitalOcean and fake SSH; no cloud call)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from campaign_fakes import FakeDigitalOcean, running
from image_daily_cases import (
    AT_0550,
    CID,
    FINGERPRINT,
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

from atlas import cli as main_cli
from atlas.campaign import image_daily, image_daily_cli, ops_api, scope
from atlas.campaign.image_daily import Context, DailyImageError, plan, quiet_delay
from atlas.campaign.tick import load_spec
from atlas.regions import REGION_IDS


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    """The DB-IP reader and project assignment without real pauses."""
    monkeypatch.setattr("maxminddb.open_database", lambda _path: Reader())
    original = ops_api.assign_and_verify_project

    async def assign(*args, **kwargs):
        return await original(*args, retry=scope.Retry(sleep=Clock().sleep), **kwargs)

    monkeypatch.setattr(ops_api, "assign_and_verify_project", assign)


@pytest.fixture
def paths(tmp_path):
    return manager(tmp_path)


async def apply(fake, paths, shell=None, clock=None, alerts=None):
    clock, alerts = clock or Clock(), alerts or Alerts([])
    async with running(fake) as cloud:
        context = Context(cloud, paths, alerts, clock, shell or Shell())
        return await image_daily.run(context), clock, alerts


def command(paths, *extra):
    return ["campaign", "image-daily", "--root", str(paths.template.parents[1]), *extra]


def fleet_images(paths):
    return json.loads(paths.fleet.read_text())["image_by_region"]


def template_images(paths):
    spec = load_spec(paths.template)
    return spec.image_name, set(spec.image_by_region.values())


def test_nothing_to_do_when_the_image_holds_the_managers_release(paths):
    write_builds(paths, old_build(mmdb_sha256=RELEASE_SHA))
    found = plan(paths)
    assert found["action"] == "none" and found["image"]["id"] == OLD_ID
    assert found["release"] == {
        "name": "dbip-full-2026-11-15.mmdb",
        "date": "2026-11-15",
        "sha256": RELEASE_SHA,
    }


def test_a_new_release_plans_a_build_named_by_its_date(paths):
    found = plan(paths)
    assert found["action"] == "build" and found["new_image"] == NEW_NAME
    assert found["targets"] == {"daily_template": load_spec(paths.template).campaign_id} | {
        "pulse_fleet": "fleet.json"
    }


def test_the_dry_run_prints_the_plan_and_changes_nothing(paths, capsys):
    before = (paths.template / "campaign-spec.json").read_bytes(), paths.fleet.read_bytes()
    assert main_cli.main(command(paths)) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["applied"] is False and printed["new_image"] == NEW_NAME
    assert printed["image"]["name"] == OLD_NAME and printed["candidates"] == []
    after = (paths.template / "campaign-spec.json").read_bytes(), paths.fleet.read_bytes()
    assert after == before


@pytest.mark.parametrize(
    ("at", "seconds"),
    [((5, 10), 300), ((5, 15), 0), ((5, 44), 0), ((5, 45), 1800), ((5, 59), 960)],
)
def test_the_switch_waits_for_a_quiet_minute(at, seconds):
    assert quiet_delay(datetime(2026, 11, 15, *at, tzinfo=UTC)) == seconds


async def test_a_new_release_is_built_copied_and_switched_at_a_quiet_minute(paths):
    fake = FakeDigitalOcean()
    old_image(fake)
    outcome, clock, alerts = await apply(fake, paths)
    [new_id] = [i for i in fake.images if i != OLD_ID]
    assert outcome["state"] == "switched" and outcome["new_image"] == {
        "id": new_id,
        "name": NEW_NAME,
    }
    assert sorted(fake.images[new_id]["regions"]) == sorted(REGION_IDS)
    assert template_images(paths) == (NEW_NAME, {new_id})
    assert set(fleet_images(paths).values()) == {new_id}
    switched_at = datetime.fromtimestamp(clock.t, UTC)
    assert (switched_at.hour, switched_at.minute) == (6, 15)  # built by 05:5x, switched at :15
    [_, new] = json.loads(paths.images.read_text())["builds"]
    assert new["state"] == "built" and new["mmdb_sha256"] == RELEASE_SHA
    assert new["requirements_sha256"] == FINGERPRINT and new["snapshot_name"] == NEW_NAME
    assert not fake.droplets and OLD_ID not in fake.images  # builder and old image gone
    assert alerts.sent == [] and outcome["failures"] == []


async def test_a_recorded_build_of_the_release_is_finished_instead_of_built_again(paths):
    """A build whose copies failed is copied to its missing regions and taken."""
    fake = FakeDigitalOcean()
    old_image(fake)
    old_image(fake, "600", NEW_NAME, regions=["lon1"])
    partial = old_build(
        started_at="2026-11-15T05:45:00+00:00",
        state="failed",
        snapshot_id="600",
        snapshot_name=NEW_NAME,
        mmdb_sha256=RELEASE_SHA,
    )
    write_builds(paths, old_build(), partial)
    assert plan(paths)["action"] == "reuse"
    outcome, _, _ = await apply(fake, paths, clock=Clock(datetime(2026, 11, 15, 6, 20, tzinfo=UTC)))
    assert outcome["state"] == "switched" and outcome["new_image"]["id"] == "600"
    assert not [p for m, p, _, _ in fake.requests if (m, p) == ("POST", "/v2/droplets")]
    assert sorted(fake.images["600"]["regions"]) == sorted(REGION_IDS)
    builds = json.loads(paths.images.read_text())["builds"]
    assert builds[-1]["state"] == "built" and sorted(builds[-1]["regions"]) == sorted(REGION_IDS)
    assert template_images(paths) == (NEW_NAME, {"600"})


async def test_a_recorded_build_gone_from_the_cloud_is_built_again(tmp_path):
    paths = manager(tmp_path, fleet=False)
    gone = old_build(snapshot_id="600", snapshot_name=NEW_NAME, mmdb_sha256=RELEASE_SHA)
    write_builds(paths, old_build(), gone)
    fake = FakeDigitalOcean()
    old_image(fake)
    outcome, _, _ = await apply(fake, paths)
    [new_id] = [i for i in fake.images if i != OLD_ID]
    assert outcome["state"] == "switched" and outcome["new_image"]["id"] == new_id != "600"
    assert outcome["switched"] == {"daily_template": CID, "pulse_fleet": None}


def test_without_an_image_inventory_a_build_is_planned(paths):
    paths.images.unlink()
    found = plan(paths)
    assert found["image"]["mmdb_sha256"] is None and found["action"] == "build"


async def test_two_failed_builds_keep_the_old_image_and_alert_once(paths):
    fake = FakeDigitalOcean()
    old_image(fake)
    outcome, clock, alerts = await apply(fake, paths, Shell("broken"))
    assert outcome["state"] == "failed"
    assert outcome["failures"] == ["build: OpsError", "build: OpsError"]
    assert image_daily.RETRY_SECONDS in clock.sleeps
    assert template_images(paths) == (OLD_NAME, {OLD_ID})
    assert set(fleet_images(paths).values()) == {int(OLD_ID)}
    assert not fake.droplets  # each builder removed through the guard
    [(day, message)] = alerts.sent
    assert str(day) == "2026-11-15" and "the old one stays: build: OpsError" in message
    assert "apt output" not in message and "10.1.2.3" not in message


async def test_a_failed_build_is_tried_once_more_and_then_switched(paths):
    fake = FakeDigitalOcean()
    old_image(fake)
    outcome, clock, alerts = await apply(fake, paths, Shell("broken", "clean"))
    assert outcome["state"] == "switched" and outcome["failures"] == ["build: OpsError"]
    assert image_daily.RETRY_SECONDS in clock.sleeps and alerts.sent == []
    assert template_images(paths)[0] == NEW_NAME


async def test_an_image_missing_in_a_region_is_never_switched_to(paths):
    fake = FakeDigitalOcean()
    old_image(fake)
    old_image(fake, "600", NEW_NAME, regions=["lon1"])
    write_builds(
        paths,
        old_build(),
        old_build(snapshot_id="600", snapshot_name=NEW_NAME) | {"mmdb_sha256": RELEASE_SHA},
    )
    fake.failing_actions.add("transfer")
    outcome, _, alerts = await apply(fake, paths)
    assert outcome["state"] == "failed" and template_images(paths) == (OLD_NAME, {OLD_ID})
    assert outcome["failures"][-1].startswith("reuse: ")
    assert len(alerts.sent) == 1


async def test_verify_refuses_an_image_not_in_the_fleets_region(paths):
    fake = FakeDigitalOcean()
    old_image(fake, "600", NEW_NAME, regions=[r for r in REGION_IDS if r != "fra1"])
    async with running(fake) as cloud:
        context = Context(cloud, paths, Alerts([]), Clock())
        with pytest.raises(DailyImageError, match=r"missing \['fra1'\]"):
            await image_daily.verify(context, {"id": "600", "name": NEW_NAME})


async def test_a_fleet_file_the_pulses_refuse_stops_the_switch_before_any_write(paths):
    raw = json.loads(paths.fleet.read_text()) | {"size": "s-8vcpu-16gb"}
    paths.fleet.write_text(json.dumps(raw))
    with pytest.raises(DailyImageError, match="nothing was switched"):
        image_daily.switch(paths, {"id": "600", "name": NEW_NAME})
    assert template_images(paths) == (OLD_NAME, {OLD_ID})


def test_the_fleet_switches_first_so_a_failed_template_write_is_tried_again(paths, monkeypatch):
    written = []

    def failing(path, data):
        if path.name == "campaign-spec.json":
            raise OSError("disk full")
        written.append(path.name)
        path.write_bytes(data)

    monkeypatch.setattr(image_daily, "atomic_bytes", failing)
    with pytest.raises(OSError):
        image_daily.switch(paths, {"id": "600", "name": NEW_NAME})
    assert written == ["fleet.json"] and set(fleet_images(paths).values()) == {"600"}
    assert plan(paths)["action"] == "build"  # the template still names the old release


def test_without_a_fleet_only_the_template_switches(tmp_path):
    paths = manager(tmp_path, fleet=False)
    found = image_daily.switch(paths, {"id": "600", "name": NEW_NAME})
    assert found["pulse_fleet"] is None and template_images(paths) == (NEW_NAME, {"600"})


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("requirements", "no requirements.txt beside the wheel"),
        ("current", "no current wheel"),
        ("recorded", "is not the recorded one"),
        ("release", "no installed DB-IP release"),
    ],
)
def test_missing_inputs_refuse_before_any_work(paths, change, message):
    current = paths.wheels / "current.json"
    if change == "requirements":
        next(paths.wheels.glob("*/requirements.txt")).unlink()
    elif change == "current":
        current.unlink()
    elif change == "recorded":
        current.write_text(
            json.dumps(json.loads(current.read_text()) | {"requirements_sha256": "f" * 64})
        )
    else:
        (paths.dbip / "release.json").unlink()
    with pytest.raises(DailyImageError, match=message):
        plan(paths)


def test_a_template_naming_two_images_refuses(paths):
    spec_file = paths.template / "campaign-spec.json"
    raw = json.loads(spec_file.read_text())
    raw["image_by_region"]["nyc3"] = "501"
    spec_file.write_text(json.dumps(raw))
    with pytest.raises(DailyImageError, match="more than one image"):
        plan(paths)


async def test_a_second_run_at_the_same_time_refuses(paths):
    from atlas.onboarding.files import lock

    with lock(paths.images.parent / ".image-daily.lock"):
        async with running(FakeDigitalOcean()) as cloud:
            with pytest.raises(DailyImageError, match="another image-daily run"):
                await image_daily.run(Context(cloud, paths, Alerts([]), Clock()))


def test_apply_needs_the_database_file_for_the_alert(paths, capsys):
    assert main_cli.main(command(paths, "--apply")) == 3
    assert "needs --database-file" in json.loads(capsys.readouterr().out)["error"]


def test_a_refused_plan_exits_3_naming_no_path(paths, capsys):
    (paths.dbip / "release.json").unlink()
    assert main_cli.main(command(paths)) == 3
    printed = json.loads(capsys.readouterr().out)
    assert printed["state"] == "refused" and str(paths.dbip) not in printed["error"]


def test_apply_runs_with_the_token_file_and_prints_the_outcome(paths, capsys, monkeypatch):
    token = paths.images.parent / "do-token"
    token.write_text("token-for-tests-only\n")
    token.chmod(0o600)
    seen = []

    async def fake_run(context):
        seen.append(context)
        return {"state": "failed", "candidates": [{"snapshot_name": NEW_NAME}]}

    monkeypatch.setattr(image_daily, "run", fake_run)
    monkeypatch.setattr(image_daily_cli.cli, "cancel_on_signals", lambda: None)
    database = paths.images.parent / "database.toml"
    assert main_cli.main(command(paths, "--apply", "--database-file", str(database))) == 3
    printed = capsys.readouterr().out
    assert json.loads(printed)["candidates"] == [NEW_NAME] and "token-for-tests" not in printed
    assert seen[0].paths.images == paths.images


async def test_the_alert_is_one_major_row_per_day(monkeypatch):
    calls = []

    class Database:
        def __init__(self, credentials_file):
            calls.append(("open", credentials_file))

        def transaction(self):
            database = self

            class Transaction:
                async def __aenter__(self):
                    return database

                async def __aexit__(self, *_):
                    return False

            return Transaction()

        async def close(self):
            calls.append(("close",))

    async def raise_alert(conn, kind, message, **values):
        calls.append((kind, message, values))
        return True

    monkeypatch.setattr("atlas.orchestrator.db.Database", Database)
    monkeypatch.setattr("atlas.orchestrator.events.raise_alert", raise_alert)
    await image_daily_cli._alert("/db.toml")(AT_0550.date(), "worker image not rebuilt")
    assert calls == [
        ("open", "/db.toml"),
        (
            image_daily.ALERT,
            "worker image not rebuilt",
            {
                "severity": "major",
                "dedupe_key": "image_daily_failed:20261115",
                "data": {"day": "2026-11-15"},
            },
        ),
        ("close",),
    ]
    assert image_daily_cli.ALERT == image_daily.ALERT
