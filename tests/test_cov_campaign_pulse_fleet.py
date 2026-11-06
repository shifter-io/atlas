"""COV-2: a create answer that also holds a droplet the batch never asked for: the fleet records
only its own names and never adopts, arms or reports the stranger (fake cloud and SSH)."""

from __future__ import annotations

from datetime import UTC, datetime

from pulse_fleet_cases import make_root, reconcile, records, services, world

from proxy_index.campaign.contracts import PULSE_TAG, Droplet

STRANGER = Droplet(
    "999001",
    "pi-pulse-lon1-stranger",
    "lon1",
    "s-1vcpu-1gb-amd",
    "new",
    datetime(2027, 1, 1, tzinfo=UTC),
    (PULSE_TAG,),
)


async def test_a_droplet_the_batch_never_asked_for_is_not_recorded(tmp_path):
    root = make_root(tmp_path, networks=("shifter",), regions=("lon1",))
    fake, (remote, issuer, clock) = world(), services()

    def answer_with_a_stranger(cloud):
        original = cloud.create

        async def create(names, **kwargs):
            return [*await original(names, **kwargs), STRANGER]

        cloud.create = create

    report = await reconcile(fake, root, remote, issuer, clock, prepare=answer_with_a_stranger)
    assert report["created"] == ["pi-pulse-lon1-shifter"]
    assert set(records(root)) == {"pi-pulse-lon1-shifter"}
    assert records(root)["pi-pulse-lon1-shifter"].droplet_id != STRANGER.id
    assert [name for name, *_ in remote.pushed] == ["pi-pulse-lon1-shifter"]
    assert STRANGER.name not in str(report)
