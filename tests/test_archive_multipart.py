"""ARCHIVE-UPLOAD-ROBUST-1: big files go to Spaces in parts, a failed part is sent again on its
own (reset, hang, 5xx), every request times out, the object is verified, and raw_upload goes on
past a network-hour that fails, with one alert per failure kind and hour."""

import hashlib
import json
import os
import time
from datetime import UTC, datetime, timedelta

import pytest
from multipart_cases import parts_bucket
from orchestrator_cases import query
from test_archive_rawhours import name, raw

from atlas.archive import multipart
from atlas.archive import spaces as spaces_module
from atlas.archive.export import file_digests
from atlas.archive.multipart import SpacesError, spans, timed
from atlas.archive.rawfiles import spool
from atlas.archive.rawhours import UploadFailed, object_key, upload_hours
from atlas.archive.spaces import ArchiveChanged, Spaces
from atlas.orchestrator.scheduler import Scheduler

pytest_plugins = ["test_storage", "orchestrator_cases"]

HOUR = datetime(2026, 10, 1, 10, tzinfo=UTC)
KEY = "raw-files/2026/10/01/10/soax.tar.zst"


@pytest.fixture
def small_parts(monkeypatch):
    """Parts of 40 kB for files over 100 kB, so a test file of 150 kB has 4."""
    monkeypatch.setattr(multipart, "THRESHOLD", 100_000)
    monkeypatch.setattr(multipart, "PART_BYTES", 40_000)


def big_file(tmp_path, size=150_000, called="big"):
    path = tmp_path / f"{called}.tar.zst"
    path.write_bytes(os.urandom(size))
    return path


def parts_of(log):
    return sorted(label for label in log if label.startswith("part "))


def test_a_big_file_goes_in_parts_and_is_verified(tmp_path, small_parts):
    path = big_file(tmp_path)
    digests = file_digests(path)
    with parts_bucket(tmp_path) as (bucket, settings):
        spaces = Spaces.from_file(settings)
        found = spaces.put_verified(KEY, path, digests)
        assert bucket.objects[f"/{KEY}"][0] == path.read_bytes()
        assert bucket.objects[f"/{KEY}"][2] == "private"
        others = [label for label in bucket.state["log"] if not label.startswith("part ")]
        assert others == ["head", "create", "complete", "head"]
        assert parts_of(bucket.state["log"]) == ["part 1", "part 2", "part 3", "part 4"]
        assert found["md5"] == digests["md5"] and found["sha256"] == digests["sha256"]
        assert spaces.head(KEY)["md5"] == multipart.multipart_etag(path, digests["bytes"])
        # A rerun finds the object made of parts and verifies it without sending it again.
        assert spaces.put_verified(KEY, path, digests)["bytes"] == 150_000
        assert bucket.state["log"].count("create") == 1
        # Other bytes at the key are still refused.
        other = big_file(tmp_path, called="other")
        with pytest.raises(ArchiveChanged):
            spaces.put_verified(KEY, other, file_digests(other))


def test_only_the_failed_part_is_sent_again_after_a_reset_and_a_500(tmp_path, small_parts):
    path = big_file(tmp_path)
    with parts_bucket(tmp_path) as (bucket, settings):
        bucket.state["faults"] = {"part 2": ["reset", "500"]}
        slept = []
        spaces = Spaces.from_file(settings, attempts=4)
        spaces.sleep = slept.append
        spaces.put_verified(KEY, path, file_digests(path))
        sent = parts_of(bucket.state["log"])
        assert sent == ["part 1", "part 2", "part 2", "part 2", "part 3", "part 4"]
        assert slept == [2.0, 8.0] and bucket.state["aborted"] == 0
        assert bucket.objects[f"/{KEY}"][0] == path.read_bytes()


def test_a_hung_part_times_out_and_is_sent_again(tmp_path, small_parts, monkeypatch):
    monkeypatch.setattr(spaces_module, "TIMEOUT_S", 0.3)
    path = big_file(tmp_path)
    with parts_bucket(tmp_path) as (bucket, settings):
        bucket.state["faults"] = {"part 3": ["hang"]}
        spaces = Spaces.from_file(settings, attempts=2)
        spaces.sleep = lambda s: None
        began = time.monotonic()
        spaces.put_verified(KEY, path, file_digests(path))
        assert time.monotonic() - began < 1.8  # did not wait for the hang to end
        assert parts_of(bucket.state["log"]).count("part 3") == 2
        assert bucket.objects[f"/{KEY}"][0] == path.read_bytes()


def test_a_part_failing_every_try_aborts_the_upload(tmp_path, small_parts):
    path = big_file(tmp_path)
    with parts_bucket(tmp_path) as (bucket, settings):
        bucket.state["faults"] = {"part 2": ["500"] * 4}
        spaces = Spaces.from_file(settings, attempts=4)
        spaces.sleep = lambda s: None
        with pytest.raises(SpacesError, match="answered InternalError") as failed:
            spaces.put_verified(KEY, path, file_digests(path))
        assert failed.value.kind == "InternalError" and failed.value.transient
        assert parts_of(bucket.state["log"]).count("part 2") == 4
        assert bucket.state["aborted"] == 1 and bucket.state["uploads"] == {}
        assert f"/{KEY}" not in bucket.objects


def test_a_refused_part_is_not_sent_again(tmp_path, small_parts):
    path = big_file(tmp_path)
    with parts_bucket(tmp_path) as (bucket, settings):
        bucket.state["faults"] = {"part 1": ["bad"], "abort": ["bad"]}
        spaces = Spaces.from_file(settings, attempts=4)
        spaces.sleep = lambda s: pytest.fail("a 400 is not retried")
        with pytest.raises(SpacesError, match="BadDigest") as failed:
            spaces.put_file(KEY, path, file_digests(path))
        assert not failed.value.transient
        assert parts_of(bucket.state["log"]).count("part 1") == 1
        # A failed abort is only logged: the part's error is the one raised.
        assert bucket.state["log"][-1] == "abort" and bucket.state["aborted"] == 0


def test_a_completion_answering_an_error_is_sent_again(tmp_path, small_parts):
    path = big_file(tmp_path)
    with parts_bucket(tmp_path) as (bucket, settings):
        bucket.state["faults"] = {"complete": ["error200"], "create": ["500"]}
        spaces = Spaces.from_file(settings, attempts=2)
        spaces.sleep = lambda s: None
        spaces.put_verified(KEY, path, file_digests(path))
        assert bucket.state["log"].count("complete") == 2
        assert bucket.state["log"].count("create") == 2
        assert bucket.objects[f"/{KEY}"][0] == path.read_bytes()


def test_a_small_file_is_one_put_sent_again_after_a_reset(tmp_path):
    path = big_file(tmp_path, 1000)
    with parts_bucket(tmp_path) as (bucket, settings):
        bucket.state["faults"] = {"put": ["reset"], "head": ["500"]}
        spaces = Spaces.from_file(settings, attempts=2)
        spaces.sleep = lambda s: None
        found = spaces.put_verified(KEY, path, file_digests(path))
        assert bucket.state["log"] == ["head", "head", "put", "put", "head"]
        assert found["md5"] == hashlib.md5(path.read_bytes()).hexdigest()


def test_a_body_past_its_deadline_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(multipart, "DEADLINE_S", -1.0)
    with pytest.raises(TimeoutError):
        list(timed([b"a"]))
    path = big_file(tmp_path, 1000)
    with parts_bucket(tmp_path) as (bucket, settings):
        with pytest.raises(SpacesError, match="PUT failed: TimeoutError"):
            Spaces.from_file(settings).put_file(KEY, path, file_digests(path))


def test_a_file_over_64_mb_goes_in_16_mb_parts(tmp_path):
    path = tmp_path / "large.tar.zst"
    block = os.urandom(1 << 20)
    path.write_bytes(block * 64 + b"x")  # just over the threshold
    with parts_bucket(tmp_path) as (bucket, settings):
        Spaces.from_file(settings).put_verified(KEY, path, file_digests(path))
        assert parts_of(bucket.state["log"]) == [f"part {n}" for n in range(1, 6)]
        assert bucket.state["etags"][f"/{KEY}"].endswith("-5")
    # Spaces' limits: at most 10,000 parts, each at least 16 MiB.
    huge = 200 << 30
    assert len(spans(huge)) <= 10_000 and sum(length for _, length in spans(huge)) == huge
    assert min(length for _, length in spans(huge)[:-1]) >= 16 << 20


def test_raw_upload_goes_on_past_a_failed_network_hour(tmp_path):
    root = tmp_path / "spool"
    spool(root, HOUR, "decodo", name(), raw(tmp_path), move=True)
    soax = spool(root, HOUR, "soax", name(), raw(tmp_path, b"soax"), move=True)
    later = HOUR + timedelta(hours=1)
    spool(root, later, "decodo", name(), raw(tmp_path, b"later"), move=True)
    with parts_bucket(tmp_path) as (bucket, settings):
        spaces = Spaces.from_file(settings)
        bucket.state["faults"] = {"put": ["reset"]}  # the first PUT: decodo at 10
        now = HOUR + timedelta(hours=3)
        with pytest.raises(UploadFailed, match="decodo 2026-10-01T10: Spaces PUT failed") as err:
            upload_hours(root, spaces, now)
        # A reset reads as ConnectionResetError (RemoteDisconnected when the close came first).
        assert err.value.kind in ("ConnectionResetError", "RemoteDisconnected")
        assert err.value.dedupe_key == f"job_failed:raw_upload:{err.value.kind}:2026-10-01T13"
        assert sorted(bucket.objects) == [
            f"/{object_key(HOUR, 'soax')}",
            f"/{object_key(later, 'decodo')}",
        ]
        assert json.loads((soax.parent / "uploaded.json").read_text())["parts"][0]["key"]
        assert not (root / "2026/10/01/10/decodo/uploaded.json").exists()
        # The next run uploads it.
        done = upload_hours(root, spaces, now)
        assert done["uploaded"] == [object_key(HOUR, "decodo")]


def test_upload_failed_names_its_kinds_and_at_most_five_hours():
    failed = [(f"n{i} 2026-10-01T10", SpacesError("x", "TimeoutError", True)) for i in range(6)]
    failed.append(("m 2026-10-01T11", ArchiveChanged("archive_changed: m")))
    error = UploadFailed(failed, datetime(2026, 10, 1, 12, 59, tzinfo=UTC))
    assert error.kind == "TimeoutError+archive_changed"
    assert str(error).startswith("7 network-hour(s) not uploaded: n0 2026-10-01T10: x;")
    assert str(error).endswith("; 2 more")
    assert error.dedupe_key.endswith(":2026-10-01T12")


async def test_job_failed_is_one_alert_per_failure_kind_and_hour(database, db):
    scheduler = Scheduler(db)
    hours = iter(["2026-10-01T12", "2026-10-01T12", "2026-10-01T13"])

    async def failing():
        error = RuntimeError("upload")
        error.kind, error.dedupe_key = "TimeoutError", f"job_failed:raw_upload:x:{next(hours)}"
        raise error

    scheduler.add("raw_upload", failing, every_s=300)
    for _ in range(3):
        await scheduler.run_now("raw_upload")
    alerts = query(database, "SELECT message,dedupe_key,occurrences FROM alerts ORDER BY 2")
    message = "raw_upload failed: RuntimeError (TimeoutError)"
    assert alerts == [
        (message, "job_failed:raw_upload:x:2026-10-01T12", 2),
        (message, "job_failed:raw_upload:x:2026-10-01T13", 1),
    ]


async def test_a_run_going_on_past_its_limit_raises_job_stuck(database, db):
    scheduler = Scheduler(db)

    async def job():
        return {}

    task = scheduler.add("raw_upload", job, every_s=300)
    task.running, task.run_id = True, 41
    task.began = time.perf_counter() - 10
    await scheduler.run_now("raw_upload")  # no limit: only the skip
    task.stuck_after_s = 3600
    await scheduler.run_now("raw_upload")  # under the limit
    assert query(database, "SELECT kind FROM alerts") == []
    task.stuck_after_s = 5
    await scheduler.run_now("raw_upload")
    await scheduler.run_now("raw_upload")
    assert query(database, "SELECT kind,message,dedupe_key,occurrences FROM alerts") == [
        ("job_stuck", "raw_upload still running after 0 min", "job_stuck:raw_upload:41", 2)
    ]
    statuses = query(database, "SELECT status,error FROM job_runs")
    assert statuses == [("skipped", "still running")] * 4
