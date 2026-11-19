"""STANDING-FREEZE-1 through published builds, PostgreSQL 17: a network with no new run for
freeze_after_hours keeps the standing of the newest published build in which it was still
measured, is no peer of the measured networks, and lifts only once measured and covered again."""

from datetime import UTC, datetime

import pytest
from calculated_cases import lenient_policy
from calculated_network_scores_cases import run
from test_calculated_hourly_reads import HOUR, History, floor_hour

from atlas.calculated import standing, store
from atlas.calculated.calculator import calculate

pytest_plugins = ["test_storage"]

PROVIDERS = ("alpha", "beta", "gamma", "delta")
# What a frozen window keeps unchanged from the build its values are from.
KEPT = ("metrics", "series", "period", "uptime", "readiness")


def policy(tmp_path, hours=3, lift=0.9):
    """The lenient test policy with the freeze on (lenient_policy() turns it off)."""
    text = lenient_policy(tmp_path).read_text()
    text = text.replace("freeze_after_hours = 0", f"freeze_after_hours = {hours}", 1)
    text = text.replace("lift_coverage = 0.9", f"lift_coverage = {lift}", 1)
    path = tmp_path / f"standing-{hours}-{lift}.toml"
    path.write_text(text)
    return path


def networks(database, build):
    head = store.read_head(database, build["snapshot_id"])
    return {n["provider"]: n for n in head["networks"]}


def assert_frozen_from(net, source, values_from, since):
    for label, w in net["windows"].items():
        old = source["windows"][label]
        assert w["standing"]["status"] == standing.FROZEN, label
        assert w["standing"]["values_from"] == values_from.isoformat()
        assert w["standing"]["frozen_since"] == since.isoformat()
        assert {k: w[k] for k in KEPT} == {k: old[k] for k in KEPT}, label
        new, before = w["scores"]["balanced"], old["scores"]["balanced"]
        assert {k: new[k] for k in ("score", "grade", "components", "state")} == {
            k: before[k] for k in ("score", "grade", "components", "state")
        }
        if "success_overall" in old:
            assert w["success_overall"]["score"] == old["success_overall"]["score"]


@pytest.mark.asyncio
async def test_a_stopped_network_keeps_its_standing_until_measured_again(database, tmp_path):
    """delta stops after the first build: five hours later it is frozen at that build's values,
    no peer of the others (they rank 3 for a target, its frozen overall among 4), and carried
    unchanged by the next build. Measured again it stays frozen while its windows are not
    covered (lift_coverage 0.9), and lifts once covered (lift_coverage 0 here)."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    h = History(database, tmp_path, end)
    for provider in PROVIDERS:
        await run(h, provider, HOUR, 150)
    on = policy(tmp_path)
    first = networks(database, calculate(database, on, as_of=end, publish=True))
    assert {w["standing"]["status"] for n in first.values() for w in n["windows"].values()} == {
        standing.MEASURED
    }
    for provider in PROVIDERS[:3]:
        await run(h, provider, -4 * HOUR, 150)
    later = end + 5 * HOUR
    second = networks(database, calculate(database, on, as_of=later, publish=True))
    assert_frozen_from(second["delta"], first["delta"], end, later)
    for provider in PROVIDERS[:3]:
        w = second[provider]["windows"]["24h"]
        assert w["standing"]["status"] == standing.MEASURED
        assert w["targets"]["generic_http"]["scores"]["success"]["ranked_of"] == 3
        assert all("success_score" in p for p in w["series"])
    frozen = second["delta"]["windows"]["24h"]
    if frozen["success_overall"]["rank"] is not None:
        assert frozen["success_overall"]["ranked_of"] == 4
    third = networks(database, calculate(database, on, as_of=later + HOUR, publish=True))
    assert_frozen_from(third["delta"], first["delta"], end, later)
    await run(h, "delta", -6 * HOUR, 150)
    again = networks(database, calculate(database, on, as_of=later + 2 * HOUR, publish=True))
    assert_frozen_from(again["delta"], first["delta"], end, later)
    assert again["delta"]["windows"]["24h"]["standing"]["last_measured"] > end.isoformat()
    lifted = calculate(database, policy(tmp_path, lift=0), as_of=later + 2 * HOUR, publish=True)
    windows = networks(database, lifted)["delta"]["windows"].values()
    assert {w["standing"]["status"] for w in windows} == {standing.MEASURED}


@pytest.mark.asyncio
async def test_the_first_frozen_build_looks_back_for_the_last_measured_values(database, tmp_path):
    """Builds made before the rule (freeze off) published delta's stale windows; the first build
    with the rule freezes it at the newest published build in which it was still measured, not at
    the stale one before (retention keeps the newest build of each UTC day: the first build is the
    last of its day). epsilon, measured in no published build, is stopped: no score."""
    end = floor_hour(datetime.now(UTC)).replace(hour=23)
    h = History(database, tmp_path, end)
    for provider in PROVIDERS:
        await run(h, provider, HOUR, 150)
    await run(h, "epsilon", 50 * HOUR, 150)
    off = policy(tmp_path, hours=0)
    first = networks(database, calculate(database, off, as_of=end, publish=True))
    for provider in PROVIDERS[:3]:
        await run(h, provider, -3 * HOUR, 150)
    calculate(database, off, as_of=end + 4 * HOUR, publish=True)
    later = end + 5 * HOUR
    found = networks(database, calculate(database, policy(tmp_path), as_of=later, publish=True))
    assert_frozen_from(found["delta"], first["delta"], end, later)
    for w in found["epsilon"]["windows"].values():
        assert w["standing"]["status"] == standing.STOPPED
        assert w["scores"] == {} and "success_overall" not in w
