"""COV-2: a synthetic launch resumed after a crash arms only the workers still coming up; one
already armed keeps its id and times (no cloud or SSH client at all)."""

from __future__ import annotations

from campaign_fakes import CID
from campaign_hour_cases import PRE_CREATE, SCHEDULED, FakeClock, make_spec

from atlas.campaign import launch
from atlas.campaign.contracts import DropletRecord, HourRecord, droplet_name

EARLIER = "2027-01-01T21:58:00.000Z"


def worker(network, **changes):
    return DropletRecord(network, "lon1", 0, droplet_name(CID, 0, "lon1", network), **changes)


async def test_a_resumed_synthetic_launch_arms_only_the_workers_still_coming_up(tmp_path):
    root = tmp_path / "campaign"
    root.mkdir()
    armed = worker(
        "net00", droplet_id="synthetic-0", status="armed", requested_at=EARLIER, armed_at=EARLIER
    )
    record = HourRecord(
        CID, 0, SCHEDULED, state="launching", fired_at=EARLIER, droplets=[armed, worker("net01")]
    )
    spec = make_spec(networks=("net00", "net01"), regions=("lon1",), synthetic=True)
    await launch.launch_hour(
        spec, record, root, cloud=None, remote_factory=None, clock=FakeClock(PRE_CREATE)
    )
    first, second = record.droplets
    assert (first.droplet_id, first.requested_at, first.armed_at) == (
        "synthetic-0",
        EARLIER,
        EARLIER,
    )
    now = "2027-01-01T21:58:30.000Z"
    assert (second.droplet_id, second.status) == ("synthetic-1", "armed")
    assert (second.requested_at, second.armed_at) == (now, now)
    assert record.state == "running" and record.launched_at == now
    assert record.cells == {"net00/lon1": "running", "net01/lon1": "running"}
    assert launch.load_hour(root, 0).to_dict() == record.to_dict()
