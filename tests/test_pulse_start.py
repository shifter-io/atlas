"""PULSE-ARM-1: a pulse node whose start lost its SSH session is confirmed by its heartbeat, or
started once more, before it is failed (campaign.pulse_start). Fake cloud and fake SSH; the
database is the test's own; nothing leaves the machine."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import test_pulse_ondemand
from orchestrator_cases import query
from pulse_fleet_cases import NODE, FakeClock, Remote, make_root, reconcile, records, world
from test_pulse_ondemand import FRA1, LON1, HourClock, ready
from test_pulse_replace import _gb_runs_from, run

from atlas.campaign import pulse_start
from atlas.campaign.contracts import Droplet
from atlas.campaign.fanout import RemoteError
from atlas.campaign.pulse_fleet import load_nodes
from atlas.campaign.pulse_start import HEARTBEAT_POLL_SECONDS, HEARTBEAT_WAIT_SECONDS, Start
from atlas.orchestrator.nodes import issue_token
from atlas.orchestrator.pulse_hour import heartbeat_since
from atlas.storage import connect

pytest_plugins = ["test_storage", "orchestrator_cases"]

LOST = "push and start failed (exit 255)"


class LostRemote(Remote):
    """The push and start of a droplet named in `lost` fails over SSH its first `lost[name]`
    times; `started` names those whose node ran all the same (`on_start` then heartbeats)."""

    def __init__(self, lost, started=(), on_start=None):
        super().__init__()
        self.lost, self.started, self.on_start = dict(lost), set(started), on_start
        self.tries: list[str] = []

    async def push_start(self, droplet, payload, command):
        self.tries.append(droplet.name)
        if self.lost.get(droplet.name, 0) > 0:
            self.lost[droplet.name] -= 1
            if droplet.name in self.started and self.on_start:
                self.on_start(droplet.name)
            raise RemoteError(LOST)
        await super().push_start(droplet, payload, command)


class Beats:
    """A heartbeat reader that sees one from the `after`-th read on (None: never)."""

    def __init__(self, after=None):
        self.after, self.reads = after, 0

    async def __call__(self, node_id, since):
        self.reads += 1
        return self.after is not None and self.reads >= self.after


ISSUED = datetime(2026, 11, 20, 8, 57, tzinfo=UTC)
DROPLET = Droplet("1", NODE, "lon1", "s-1vcpu-2gb-amd", "active", ISSUED, (), "203.0.113.5")
START = Start(DROPLET, b"tar", ["true"], 7, ISSUED)
READS_PER_WAIT = int(HEARTBEAT_WAIT_SECONDS // HEARTBEAT_POLL_SECONDS) + 1


async def test_a_lost_reply_whose_node_heartbeats_is_armed_without_a_second_start():
    remote, clock, beats = LostRemote({NODE: 1}), FakeClock(), Beats(after=3)
    assert await pulse_start.push_start(remote, clock, beats, START) == "heartbeat"
    assert remote.tries == [NODE] and beats.reads == 3


async def test_a_lost_session_without_a_heartbeat_is_started_once_more():
    remote, clock, beats = LostRemote({NODE: 1}), FakeClock(), Beats()
    assert await pulse_start.push_start(remote, clock, beats, START) == "retried"
    assert remote.tries == [NODE, NODE] and beats.reads == READS_PER_WAIT
    assert clock.t - 1000.0 <= HEARTBEAT_WAIT_SECONDS


async def test_a_retry_whose_reply_is_lost_too_is_still_confirmed_by_a_heartbeat():
    remote, clock, beats = LostRemote({NODE: 2}), FakeClock(), Beats(after=READS_PER_WAIT + 2)
    assert await pulse_start.push_start(remote, clock, beats, START) == "heartbeat"
    assert remote.tries == [NODE, NODE]


async def test_a_start_that_really_failed_is_failed_within_the_bound():
    remote, clock, beats = LostRemote({NODE: 5}), FakeClock(), Beats()
    with pytest.raises(RemoteError, match="exit 255"):
        await pulse_start.push_start(remote, clock, beats, START)
    assert remote.tries == [NODE, NODE]  # the start and one retry, never more
    assert clock.t - 1000.0 <= 2 * HEARTBEAT_WAIT_SECONDS


@pytest.mark.parametrize(
    ("beats", "start"), [(None, START), (Beats(after=1), replace(START, node_id=None))]
)
async def test_without_a_heartbeat_reader_or_node_row_an_ssh_failure_is_only_retried(beats, start):
    remote, clock = LostRemote({NODE: 1}), FakeClock()
    assert await pulse_start.push_start(remote, clock, beats, start) == "retried"
    assert clock.t == 1000.0  # no wait at all


async def test_an_os_error_is_unconfirmed_too():
    class Gone(Remote):
        async def push_start(self, droplet, payload, command):
            raise OSError("ssh could not run")

    with pytest.raises(OSError):
        await pulse_start.push_start(Gone(), FakeClock(), Beats(), START)


async def test_an_error_that_is_not_ssh_fails_at_once():
    class Broken(Remote):
        async def push_start(self, droplet, payload, command):
            raise ValueError("bad bundle")

    beats = Beats(after=1)
    with pytest.raises(ValueError):
        await pulse_start.push_start(Broken(), FakeClock(), beats, START)
    assert beats.reads == 0


async def test_the_fleet_without_a_database_retries_a_lost_session_and_records_how(tmp_path):
    root, remote = make_root(tmp_path), LostRemote({NODE: 1})
    report = await reconcile(world(), root, remote)
    assert report["created"] == [NODE] and report["failed"] == []
    record = records(root)[NODE]
    assert (record.status, record.error_class, record.error_detail) == (
        "armed", None, {"start": "retried"}
    )  # fmt: skip
    assert remote.tries == [NODE, NODE] and len(remote.pushed) == 1


async def test_heartbeat_since_counts_only_a_heartbeat_after_the_token(database, db):
    await ready(database, db)
    issued = issue_token(database, provider="testnet", product="residential", region="lon1")
    node, since = issued["node_id"], datetime.now(UTC)
    with connect(database) as conn:
        conn.execute("UPDATE nodes SET last_heartbeat=NULL WHERE node_id=%s", (node,))
    assert not await heartbeat_since(db, node, since)  # never heartbeated
    with connect(database) as conn:
        conn.execute(
            "UPDATE nodes SET last_heartbeat=%s WHERE node_id=%s",
            (since - timedelta(minutes=1), node),
        )
    assert not await heartbeat_since(db, node, since)  # the droplet before this token
    with connect(database) as conn:
        conn.execute("UPDATE nodes SET last_heartbeat=now() WHERE node_id=%s", (node,))
    assert await heartbeat_since(db, node, since)
    assert not await heartbeat_since(db, node + 1000, since)  # no such node


class DbIssuer:
    """The real token issue on the test's database (the fleet's database_file is a stub)."""

    def __init__(self, database):
        self.database = database

    def __call__(self, database_file, *, provider, product, region):
        return issue_token(self.database, provider=provider, product=product, region=region)


def _heartbeat(database):
    def beat(name):  # the node service started on the droplet: it heartbeats
        region = name.split("-")[2]
        with connect(database) as conn:
            conn.execute(
                "UPDATE nodes n SET last_heartbeat=now(), status='online' FROM runner_regions r "
                "WHERE r.region_id=n.region_id AND r.slug=%s",
                (region,),
            )

    return beat


async def _hour(tmp_path, database, db, remote, monkeypatch):
    await ready(database, db)
    monkeypatch.setattr(test_pulse_ondemand, "Issuer", lambda: DbIssuer(database))
    fake = world()
    root, outcome, calls = await run(tmp_path, fake, db, HourClock(database), remote, monkeypatch)
    return load_nodes(root / "hours" / "20261001T12"), outcome, calls


async def test_an_hour_node_whose_start_reply_was_lost_but_heartbeats_gets_its_runs(
    tmp_path, database, db, monkeypatch
):
    """The first case (a stalled path): the node runs, its start's reply never came."""
    remote = LostRemote({LON1: 1}, started={LON1}, on_start=_heartbeat(database))
    ledger, outcome, calls = await _hour(tmp_path, database, db, remote, monkeypatch)
    assert remote.tries.count(LON1) == 1  # confirmed by its heartbeat, never pushed again
    assert ledger[LON1].error_detail == {"start": "heartbeat"} and ledger[LON1].attempts == 1
    assert outcome["replaced"] == [] and outcome["handed"] == [] and outcome["lost"] == []
    assert calls == [["fra1", "lon1"]]  # opened with the others, at once
    assert _gb_runs_from(database) == [("lon1",)]  # GB's runs went to the lon1 node
    assert (outcome["state"], outcome["armed"]) == ("done", 2)
    assert (outcome["opened"]["created"], outcome["opened"]["runs"]) == (4, 4)


async def test_an_hour_node_whose_start_session_was_lost_is_started_again_and_gets_its_runs(
    tmp_path, database, db, monkeypatch
):
    """The second case (one SSH session lost its path): the node never started."""
    remote = LostRemote({LON1: 1})
    ledger, outcome, calls = await _hour(tmp_path, database, db, remote, monkeypatch)
    assert remote.tries.count(LON1) == 2 and remote.tries.count(FRA1) == 1
    assert ledger[LON1].error_detail == {"start": "retried"} and ledger[LON1].attempts == 1
    assert outcome["replaced"] == [] and calls == [["fra1", "lon1"]]
    assert _gb_runs_from(database) == [("lon1",)]
    assert (outcome["state"], outcome["armed"]) == ("done", 2)


async def test_an_hour_node_whose_start_really_failed_is_still_failed_and_replaced(
    tmp_path, database, db, monkeypatch
):
    remote = LostRemote({LON1: 2})  # the droplet's start and its retry; the replacement starts
    ledger, outcome, calls = await _hour(tmp_path, database, db, remote, monkeypatch)
    [replaced] = outcome["replaced"]
    assert (replaced["name"], replaced["reason"], replaced["armed"]) == (LON1, "start_failed", True)
    assert ledger[LON1].attempts == 2 and calls == [["fra1"], ["lon1"]]
    assert query(database, "SELECT kind FROM alerts") == []
