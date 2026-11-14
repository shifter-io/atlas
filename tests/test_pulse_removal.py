"""PULSE-COMPACT-1: an hour's pulse droplets are removed REMOVE_CONCURRENCY at once through the
guarded path (campaign.pulse_removal.retire_many). The guarded call is a fake that takes 12 s,
scaled 1:100 in real time; nothing leaves the machine."""

from __future__ import annotations

import asyncio
import math
import time

from pulse_fleet_cases import FakeClock

from atlas.campaign import pulse_fleet, pulse_removal, scope
from atlas.campaign.contracts import DropletRecord

SCALE = 0.01  # a fake API second takes 10 ms here
TAKES = 12.0  # seconds per guarded removal, as in production (about 12.5 s)


class Cloud:
    """Only what retire_many reads itself: the project and the read until gone."""

    def __init__(self):
        self.gone = set()

    async def project_id(self, name):
        return "dev-project"

    async def get(self, rid):
        return None if rid in self.gone else object()


def ledger(root, count):
    records = {}
    for i in range(count):
        region = f"r{i:03}"
        name = f"atlas-pulse-{region}-shifter"
        records[name] = DropletRecord("shifter", region, 12, name, str(1000 + i), status="armed")
    pulse_fleet.save_nodes(root, records)
    return records


def guarded_fake(cloud, calls, live, fail=None):
    async def retire_pulse_node(_cloud, rid, project_id, ledger, audit_path):
        assert ledger.has("droplet", rid) and project_id == "dev-project"  # its own id only
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        try:
            await asyncio.sleep(TAKES * SCALE)
            if fail and rid in fail:
                raise fail[rid]
        finally:
            live["now"] -= 1
        calls.append(rid)
        cloud.gone.add(rid)
        return "deleted"

    return retire_pulse_node


async def test_144_droplets_go_n_at_a_time_in_two_minutes_not_half_an_hour(tmp_path, monkeypatch):
    cloud, calls, live = Cloud(), [], {"now": 0, "max": 0}
    monkeypatch.setattr(scope, "retire_pulse_node", guarded_fake(cloud, calls, live))
    records = ledger(tmp_path, 144)
    began = time.monotonic()
    reasons = await pulse_removal.retire_many(tmp_path, records, cloud, FakeClock())
    took = (time.monotonic() - began) / SCALE  # in the fake API's seconds
    n = pulse_removal.REMOVE_CONCURRENCY
    assert reasons == dict.fromkeys(sorted(records), None)
    assert sorted(calls) == sorted(r.droplet_id for r in records.values())  # each id once
    assert live["max"] == n == 16
    # 12 s x ceil(144 / 16) = 108 s, where one at a time took 144 x 12 s, about 29 minutes.
    expected = TAKES * math.ceil(144 / n)
    assert expected <= took < 2 * expected
    assert {r.status for r in pulse_fleet.load_nodes(tmp_path).values()} == {"destroyed"}


async def test_a_refusal_or_an_error_of_one_never_stops_the_others(tmp_path, monkeypatch):
    cloud, calls, live = Cloud(), [], {"now": 0, "max": 0}
    fail = {"1001": scope.OwnershipError("droplet", "1001", ["tags"]), "1002": RuntimeError()}
    monkeypatch.setattr(scope, "retire_pulse_node", guarded_fake(cloud, calls, live, fail))
    records = ledger(tmp_path, 4)
    names = sorted(records) + ["atlas-pulse-zzz-shifter"]  # one not in the ledger
    reasons = await pulse_removal.retire_many(tmp_path, names, cloud, FakeClock())
    assert reasons == {
        "atlas-pulse-r000-shifter": None,
        "atlas-pulse-r001-shifter": "OwnershipError",
        "atlas-pulse-r002-shifter": "RuntimeError",
        "atlas-pulse-r003-shifter": None,
        "atlas-pulse-zzz-shifter": "not recorded",
    }
    saved = {n: r.status for n, r in pulse_fleet.load_nodes(tmp_path).items()}
    assert saved == {
        "atlas-pulse-r000-shifter": "destroyed",
        "atlas-pulse-r001-shifter": "destroy_refused",
        "atlas-pulse-r002-shifter": "destroy_failed",
        "atlas-pulse-r003-shifter": "destroyed",
    }
    assert await pulse_removal.retire_many(tmp_path, [], cloud, FakeClock()) == {}


async def test_a_project_lookup_failure_is_each_nodes_reason(tmp_path):
    class Down(Cloud):
        async def project_id(self, name):
            raise ConnectionError("api down")

    records = ledger(tmp_path, 2)
    reasons = await pulse_removal.retire_many(tmp_path, records, Down(), FakeClock())
    assert reasons == dict.fromkeys(sorted(records), "ConnectionError")
