"""CAMP-1l: a worker we just created is never refused by our own guard because the API lags.

Twice (nyc3, then lon1) the fresh GET about a second after the create did not yet show the
droplet consistently. `assign_and_verify_project` raised `OwnershipError`, the launch handed the
worker to the plain guarded delete as `start_failed`, the guard refused it (project=false) and
it stayed alive. The launch was a `tick --now` 45 minutes past HH:00, so no replacement ran
either, and only the exception class was kept. Here a lagging read of our own create settles
(about 30 s), any failure before a confirmed assignment takes the own-unassigned path, the
replacement window starts at the launch, and the full error is kept. Offline: fakes only.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from campaign_fakes import (
    CAMPAIGN_PROJECT,
    CID,
    EARLY_TIME,
    MANAGER_ID,
    SID,
    SPEC_TIME,
    FakeDigitalOcean,
    Fault,
    running,
)
from campaign_hour_cases import NETWORKS, SCHEDULED, launched, make_spec

from atlas.campaign import launch, scope
from atlas.campaign.contracts import (
    WORKER_TAG,
    DropletRecord,
    HourRecord,
    Scope,
    audit_path,
    droplet_name,
    hour_path,
)
from atlas.campaign.scope import OwnershipError, assign_and_verify_project

FIRST = "700001"  # the fake's first droplet id: the hour's only worker
# `tick --now` 45 minutes past the slot (the lon1 run fired at HH:45:42).
NOW_LATE = datetime.fromisoformat("2027-01-01T22:45:42+00:00").timestamp()
CELL = "net00/lon1"
SCOPE = Scope(CID, SID, CAMPAIGN_PROJECT, MANAGER_ID, datetime.fromisoformat(SPEC_TIME))
OURS = [WORKER_TAG, f"atlas-camp-{SID}", f"atlas-camp-{SID}-h03"]
NAME = f"atlas-{SID}-h03-lon1-shifter"
SETTLE_READS = 6  # the first read and five more over about 30 s


def lon1_spec(**changes):
    return make_spec(NETWORKS[:1], ("lon1",), **changes)


def audit_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize(
    "stale",
    [{"tags": []}, {"tags": [WORKER_TAG]}, {"name": ""}, {"created_at": EARLY_TIME}],
    ids=["no_tags", "some_tags", "no_name", "created_at"],
)
async def test_lon1_a_create_not_yet_shown_is_read_again_then_assigned(tmp_path, stale):
    """The lon1 sequence: the first fresh GETs after the create lag. Before CAMP-1l the first
    read raised, the plain guard refused the worker (project=false) and it stayed alive."""
    fake = FakeDigitalOcean()
    fake.stale[FIRST] = [stale, stale]
    record, *_ = await launched(tmp_path, fake, lon1_spec(), start=NOW_LATE)
    [worker] = record.droplets
    assert (worker.status, worker.droplet_id) == ("armed", FIRST) and worker.assigned_at
    assert FIRST in fake.projects[CAMPAIGN_PROJECT] and not fake.deleted
    assert not {a["kind"] for a in record.alerts} & {
        "launch_error",
        "start_failed",
        "destroy_refused",
        "cell_failed",
    }


async def test_lon1_a_create_that_never_settles_is_removed_as_own_and_replaced(tmp_path):
    """Still not shown after about 30 s: assign_failed with the ledger mark, removed on the
    own-unassigned path (every other check on a fresh GET), and replaced although the launch
    ran 45 minutes past HH:00. The full error is in the ledger and the audit."""
    fake = FakeDigitalOcean()
    fake.stale[FIRST] = [{"tags": []}] * SETTLE_READS
    record, root, remote, _ = await launched(tmp_path, fake, lon1_spec(), start=NOW_LATE)
    first, replacement = record.droplets
    assert (first.status, first.droplet_id) == ("destroyed", FIRST)
    assert first.assign_failed_at and first.assigned_at is None
    assert fake.deleted == [("droplet", FIRST)]
    last = audit_lines(audit_path(root))[-1]
    assert (last["path"], last["result"]) == ("own_unassigned", "deleted")
    assert all(last["checks"].values())
    assert replacement.replaced_from == 0 and replacement.status == "armed"
    assert record.cells == {CELL: "running"} and [p[0] for p in remote.pushes] == [replacement.name]
    detail = first.error_detail
    assert (detail["error"], detail["failed"]) == ("Unconfirmed", ["tags"])
    assert "POST /v2/droplets 202" in detail["http"]
    assert f"GET /v2/droplets/{FIRST} 200" in detail["http"]
    assert last["cause"] == detail
    assert "destroy_refused" not in {a["kind"] for a in record.alerts}


@pytest.mark.parametrize(
    "error",
    [
        OwnershipError("droplet", FIRST, ["tags", "name"]),
        RuntimeError("ssh root@203.0.113.10 refused; Authorization: Bearer dop_v1_" + "a" * 64),
    ],
    ids=["ownership", "other"],
)
async def test_any_failure_before_assignment_takes_the_own_unassigned_path(
    tmp_path, monkeypatch, error
):
    """The observed escape: the assignment step raised for our own fresh worker. Before CAMP-1l
    it went to the plain retire (project=true required): destroy_refused, left alive."""

    async def raising(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(launch.scope, "assign_and_verify_project", raising)
    record, root, *_ = await launched(
        tmp_path, fake := FakeDigitalOcean(), lon1_spec(), start=NOW_LATE
    )
    first = record.droplets[0]
    assert (first.status, first.droplet_id) == ("destroyed", FIRST)
    assert first.assign_failed_at and ("droplet", FIRST) in fake.deleted
    assert not fake.droplets and {a["path"] for a in audit_lines(audit_path(root))} == {
        "own_unassigned"
    }
    assert "destroy_refused" not in {a["kind"] for a in record.alerts}
    assert len(record.droplets) == 1 + launch.REPLACEMENTS_PER_CELL  # replaced, though late
    detail = first.error_detail
    assert first.error_class == detail["error"] == type(error).__name__
    if isinstance(error, OwnershipError):
        assert detail["failed"] == ["tags", "name"]
        assert detail["message"] == f"refused droplet {FIRST}: failed tags, name"
    stored = hour_path(root, 0).read_text() + audit_path(root).read_text()
    assert "203.0.113.10" not in stored and "dop_v1_" not in stored


@pytest.mark.parametrize("outcome", ["destroy_refused", "destroy_failed"])
async def test_an_own_unassigned_removal_that_does_not_happen_is_flagged(
    tmp_path, monkeypatch, outcome
):
    """Refused (here: older than 30 min) is final with a high alert; an error stays for the
    next pass (destroy_failed). Either way the worker was never handed to the plain retire."""

    async def raising(*_args, **_kwargs):
        raise OwnershipError("droplet", FIRST, ["tags"])

    fake = FakeDigitalOcean()
    monkeypatch.setattr(launch.scope, "assign_and_verify_project", raising)
    if outcome == "destroy_refused":
        monkeypatch.setattr(scope, "utcnow", lambda: datetime.fromisoformat("2027-01-01T23:00Z"))
    else:
        fake.faults.append(Fault("DELETE", f"/v2/droplets/{FIRST}", 500, times=5))
    record, root, *_ = await launched(
        tmp_path, fake, lon1_spec(replace_until_minutes=0), start=NOW_LATE
    )
    [worker] = record.droplets
    assert worker.status == outcome and worker.assign_failed_at and FIRST in fake.droplets
    assert ("destroy_refused" in {a["kind"] for a in record.alerts}) == (
        outcome == "destroy_refused"
    )
    assert {a["path"] for a in audit_lines(audit_path(root))} == {
        "normal" if outcome == "destroy_refused" else "own_unassigned"
    }
    assert record.traffic["net00"]["worker_hours"] == 0


async def test_a_recorded_worker_this_client_did_not_create_keeps_the_plain_guard(tmp_path):
    """Review m2: a resumed launch whose recorded id the listing does not show again (no hour
    tag here), so it is not in created_ids. Its assignment refuses it; it goes to the plain
    guard (refused, alerted, never marked) and the launch still completes."""
    fake = FakeDigitalOcean()
    name = droplet_name(CID, 0, "lon1", "net00")
    orphan = fake.add(name, [WORKER_TAG, f"atlas-camp-{SID}"], project=None)
    worker = DropletRecord(
        "net00",
        "lon1",
        0,
        name,
        orphan,
        size="c-2",
        image_id="snap-lon1",
        attempts=1,
        requested_at=SCHEDULED,
    )
    record = HourRecord(
        CID,
        0,
        SCHEDULED,
        state="launching",
        droplets=[worker],
        traffic={"net00": dict.fromkeys(launch.TRAFFIC_KEYS, 0)},
    )
    record, root, *_ = await launched(
        tmp_path, fake, lon1_spec(replace_until_minutes=0), start=NOW_LATE, record=record
    )
    assert record.state == "running" and record.droplets == [worker]
    assert (worker.status, worker.error_class) == ("destroy_refused", "OwnershipError")
    assert worker.assign_failed_at is None and orphan in fake.droplets and not fake.deleted
    assert {a["kind"] for a in record.alerts} >= {"start_failed", "destroy_refused"}
    assert [a["path"] for a in audit_lines(audit_path(root))] == ["normal"]
    assert not [r for r in fake.requests if r[0] == "POST"]


async def test_a_now_launch_past_hh15_still_replaces_a_failed_create(tmp_path):
    """The window counts from the launch (or the slot, if later), not from HH:00 alone."""
    fake = FakeDigitalOcean()
    fake.faults.append(Fault("POST", "/v2/droplets", 500, times=2))  # both create attempts
    record, *_ = await launched(tmp_path, fake, lon1_spec(), start=NOW_LATE)
    first, replacement = record.droplets
    assert first.status == "create_failed" and replacement.status == "armed"
    assert record.cells == {CELL: "running"} and "cell_failed" not in {
        a["kind"] for a in record.alerts
    }


async def test_the_window_from_the_launch_still_closes(tmp_path):
    fake = FakeDigitalOcean()
    fake.faults.append(Fault("POST", "/v2/droplets", 500, times=2))
    record, *_ = await launched(tmp_path, fake, lon1_spec(replace_until_minutes=0), start=NOW_LATE)
    assert [d.status for d in record.droplets] == ["create_failed"]
    assert record.cells == {CELL: "failed_ours"}
    assert "cell_failed" in {a["kind"] for a in record.alerts}


@pytest.mark.parametrize(
    ("tags", "unreadable"),
    [([], False), (OURS, False), (OURS, True)],
    ids=["not_shown", "our_name_and_tags", "unreadable"],
)
async def test_a_droplet_we_did_not_create_is_refused_at_once(tmp_path, tags, unreadable):
    """Settling is only for our own creates: a recorded lookalike that this client did not
    create is refused on the first read, even when every read fails (review m1), with no
    wait, no POST and no delete."""
    fake, sleeps = FakeDigitalOcean(), []

    async def sleep(seconds):
        sleeps.append(seconds)

    lookalike = fake.add(NAME, tags, project=None)
    if unreadable:  # every request try of every settle read
        fake.faults.append(Fault("GET", f"/v2/droplets/{lookalike}", 500, times=SETTLE_READS * 5))
    records = {lookalike: DropletRecord("shifter", "lon1", 3, NAME, lookalike)}
    async with running(fake) as cloud:
        await cloud.create(
            [f"atlas-{SID}-h03-fra1-shifter"],
            region="fra1",
            size="c-2",
            image="1",
            tags=OURS,
            ssh_keys=["9"],
        )  # our own, another id
        with pytest.raises(OwnershipError) as refused:
            await assign_and_verify_project(
                cloud, SCOPE, [lookalike], records, tmp_path / "a", retry=scope.Retry(sleep=sleep)
            )
    assert "own_create" in refused.value.failed and not sleeps
    assert records[lookalike].assign_failed_at is None and not fake.deleted
    assert not [r for r in fake.requests if r[0] == "POST" and "/projects/" in r[1]]


async def test_only_the_lagging_droplet_is_read_again(tmp_path):
    fake, sleeps = FakeDigitalOcean(), []

    async def sleep(seconds):
        sleeps.append(seconds)

    async with running(fake) as cloud:
        made = await cloud.create(
            [NAME, f"atlas-{SID}-h03-lon1-geonode"],
            region="lon1",
            size="c-2",
            image="1",
            tags=OURS,
            ssh_keys=["9"],
        )
        ids = sorted(d.id for d in made)
        records = {d.id: DropletRecord("n", "lon1", 3, d.name, d.id) for d in made}
        fake.stale[ids[1]] = [{"tags": []}] * 2
        confirmed, failed = await assign_and_verify_project(
            cloud, SCOPE, ids, records, tmp_path / "a", retry=scope.Retry(sleep=sleep)
        )
    assert confirmed == set(ids) and not failed and sleeps == [1.0, 2.0]
    reads = [r[1] for r in fake.requests if r[0] == "GET" and r[1].startswith("/v2/droplets/")]
    assert reads.count(f"/v2/droplets/{ids[0]}") == 1
    assert reads.count(f"/v2/droplets/{ids[1]}") == 3


def test_the_settle_is_bounded_to_about_30_seconds():
    assert sum(scope.SETTLE_DELAYS) == 30.0 and 1 + len(scope.SETTLE_DELAYS) == SETTLE_READS
    assert scope.SETTLING == {"readable", "exists", "tags", "name", "created_after"}


async def test_the_stored_error_has_no_address_or_token():
    async with running(FakeDigitalOcean()) as cloud:
        detail = scope.error_detail(
            cloud,
            ["1"],
            RuntimeError(
                "to 203.0.113.10 and 2001:db8::1 with Bearer dop_v1_x token "
                + "f" * 40
                + " x" * 300
            ),
        )
    assert detail["message"].startswith("to [removed] and [removed] with [removed] token [removed]")
    assert len(detail["message"]) == scope.MESSAGE_LIMIT and detail["failed"] == []
    assert (detail["error"], detail["http"]) == ("RuntimeError", [])


async def test_remove_unassigned_refuses_an_id_without_its_record(tmp_path):
    fake = FakeDigitalOcean()
    async with running(fake) as cloud:
        [droplet] = await cloud.create(
            [NAME], region="lon1", size="c-2", image="1", tags=OURS, ssh_keys=["9"]
        )
        with pytest.raises(OwnershipError) as refused:
            await scope.remove_unassigned(cloud, SCOPE, {}, tmp_path / "a", {droplet.id: {}})
    assert refused.value.failed == ["own_create"] and droplet.id in fake.droplets
    assert not fake.deleted and not (tmp_path / "a").exists()
