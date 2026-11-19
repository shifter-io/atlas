"""DAILY-OVERTIME-1 (#1152): a daily country test runs until it is finished, whatever the clock.

The hour's stops (a budgeted network before its pulses, every worker at the safety stop), the
stop of the live workers and the runner's limit, offline against the fake cloud and remote.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from campaign_fakes import CID, FakeDigitalOcean, running
from campaign_hour_cases import PRE_CREATE, SCHEDULED, FakeClock, FakeRemote, make_root, make_spec
from daily_cases import NOW, Borrowed, hours, inputs, template

from atlas.campaign import launch, pulse_late
from atlas.campaign.contracts import HourRecord, droplet_name, hour_path
from atlas.campaign.tick import save_hour
from atlas.daily import overtime
from atlas.daily.runner import run_hour, time_limit
from atlas.onboarding.files import read_json

BEGINS = datetime(2027, 1, 1, 22, tzinfo=UTC)


class StoppingRemote(FakeRemote):
    """Records each round stop; a name in `refuses` fails its stop."""

    def __init__(self, clock, refuses=()):
        super().__init__(clock)
        self.stopped, self.refuses = [], set(refuses)

    async def stop(self, droplet):
        if droplet.name in self.refuses:
            raise OSError("ssh failed")
        self.stopped.append(droplet.name)


def test_the_limit_is_the_safety_stop_plus_its_grace_whatever_the_slot():
    assert overtime.SAFETY == timedelta(hours=6) and overtime.STOP_GRACE == timedelta(minutes=15)
    assert time_limit(BEGINS, BEGINS - timedelta(minutes=2)) == (6 * 60 + 17) * 60
    assert time_limit(BEGINS, BEGINS + timedelta(hours=7)) == 0.0


def test_a_budgeted_network_stops_before_its_pulses_every_worker_at_the_safety_stop():
    # LOWAVAIL-PACE-2 (#1247): a network without a budget is stopped at HH:45.
    shifter = (BEGINS + timedelta(minutes=45), frozenset({"shifter"}))
    assert overtime.stops(BEGINS, ("shifter",)) == [shifter, (BEGINS + overtime.SAFETY, None)]
    assert overtime.BUDGET_STOP == timedelta(minutes=27)  # its pulse droplets come at HH:30
    assert overtime.stops(BEGINS, ("proxy-cheap", "shifter")) == [
        (BEGINS + timedelta(minutes=27), frozenset({"proxy-cheap"})),
        shifter,
        (BEGINS + overtime.SAFETY, None),
    ]
    safety_only = [(BEGINS + overtime.SAFETY, None)]
    assert overtime.stops(BEGINS, ("proxy-cheap",), frozenset()) == safety_only


def test_a_budget_stopped_daily_droplet_lives_under_30_minutes():
    # BUDGET-STOP-EARLIER-1 (#1293): created create_lead_seconds (90 s) before its hour, stopped
    # at BUDGET_STOP and removed within about 15 s after (16-17 Nov: 32.9 to 36.8 minutes at the
    # HH:35 stop), with the gate still after the stop.
    lead = timedelta(seconds=make_spec().create_lead_seconds)
    assert lead + overtime.BUDGET_STOP + timedelta(minutes=1) < timedelta(minutes=30)
    assert overtime.BUDGET_STOP < pulse_late.GATE_LATEST


def recorded(root, spec, statuses):
    """The hour's record with one worker per (network, region, status)."""
    record = HourRecord(CID, 0, SCHEDULED, state="running")
    for index, (network, region, status) in enumerate(statuses, 1):
        record.traffic[network] = dict.fromkeys(launch.TRAFFIC_KEYS, 0)
        name = droplet_name(CID, 0, region, network)
        worker = launch.DropletRecord(network, region, 0, name, droplet_id=index, status=status)
        record.droplets.append(worker)
    save_hour(root, record)
    return [d.name for d in record.droplets]


async def test_only_the_live_workers_of_the_named_networks_are_stopped(tmp_path):
    spec = make_spec()
    root = make_root(tmp_path, spec)
    a, b = spec.networks
    workers = [(a, "lon1", "running"), (a, "fra1", "armed"), (b, "lon1", "running")]
    names = recorded(root, spec, [*workers, (b, "fra1", "destroyed")])
    remote = StoppingRemote(FakeClock(PRE_CREATE), refuses={names[1]})
    assert await overtime.stop_live(root, spec, remote, frozenset({a})) == [names[0]]
    assert remote.stopped == [names[0]]  # names[1] failed: left to the limit
    remote.refuses.clear()
    assert await overtime.stop_live(root, spec, remote) == names[:3]  # never the destroyed one


async def test_an_hour_past_its_safety_stop_stops_its_workers_then_removes_them(tmp_path):
    root = template(tmp_path)
    fake, clock = FakeDigitalOcean(), FakeClock(PRE_CREATE)
    remote = StoppingRemote(clock)
    async with running(fake) as cloud:

        async def launcher(spec, record, hour_root):
            await launch.launch_hour(
                spec, record, hour_root, cloud=cloud, remote_factory=lambda h: remote, clock=clock
            )

        async def harvester(spec, record, hour_root):  # the rounds never end on their own
            return record

        def services(hour_root, spec):
            return Borrowed(cloud), lambda hour: remote, launcher, harvester

        due = inputs(tmp_path, root, GB=22, DE=22)
        outcome = await run_hour(due, services=services, now=NOW, clock=clock, limit=1.0)
    (hour_root,) = hours(root)
    names = sorted(d["name"] for d in read_json(hour_path(hour_root, 0))["droplets"])
    # The HH:45 cell stop first (LOWAVAIL-PACE-2), then the safety stop: these fake rounds
    # never end, so both stop them.
    assert sorted(remote.stopped[:2]) == names and len(names) == 2
    assert sorted(set(outcome["stopped"])) == names  # stopped first, so their countries are kept
    assert outcome["error"].startswith("stopped at the time limit") and outcome["leftover"] == []
