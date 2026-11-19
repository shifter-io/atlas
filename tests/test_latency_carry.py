"""LATENCY-ALWAYS-1: a window whose latency change has no value this build keeps the last value
computed for its network (the current build's), with its own periods; a network never computed
keeps its null and reason; a later day's value is never carried back."""

from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb
from test_latency_days import COUNTRIES, day, span

from atlas.calculated.latency_carry import carry
from atlas.calculated.latency_days import DAY_NOT_COVERED, NEEDS_TWO_DAYS, latency_day_delta
from atlas.calculated.latency_store import complete
from atlas.storage import connect

pytest_plugins = ["test_storage", "calculated_template"]

AS_OF = datetime(2026, 11, 17, 3, tzinfo=UTC)  # compares 15 and 16 November
DAY = timedelta(days=1)


@pytest.fixture
def database(calculated_database):
    return calculated_database


def nov(d):
    return datetime(2026, 11, d, tzinfo=UTC)


def stored(value):
    """value as the payload's JSON holds it."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: stored(v) for k, v in value.items()}
    if isinstance(value, list):
        return [stored(v) for v in value]
    return value


def publish(conn, windows, provider="shifter", as_of=AS_OF - DAY):
    """Make a build whose network `provider` has these windows ({label: latency_day_delta})
    the current one."""
    conn.execute("INSERT INTO methodologies VALUES ('m','{}') ON CONFLICT DO NOTHING")
    build = conn.execute(
        "INSERT INTO calculation_builds(methodology_id,as_of,input_hash) VALUES ('m',%s,%s) "
        "RETURNING build_id",
        (as_of, str(as_of)),
    ).fetchone()[0]
    network = {"provider": provider, "kind": "residential"}
    network["windows"] = {k: {"latency_day_delta": stored(v)} for k, v in windows.items()}
    conn.execute(
        "INSERT INTO calculated_snapshots(build_id,content_hash,payload) VALUES (%s,'h',%s)",
        (build, Jsonb({"networks": [network]})),
    )
    conn.execute(
        "INSERT INTO current_calculation VALUES (true,%s) ON CONFLICT(singleton) "
        "DO UPDATE SET build_id=excluded.build_id",
        (build,),
    )


def entry(windows, provider="shifter"):
    """A network entry of the build being made, with these windows' latency changes."""
    return {
        "provider": provider,
        "kind": "residential",
        "windows": {k: {"latency_day_delta": v} for k, v in windows.items()},
    }


# 14 and 15 Nov tested in full: the change the 16 Nov build made (+10%).
COMPUTED = latency_day_delta(
    day(nov(13), 900) + day(nov(14), 1000) + day(nov(15), 1100), AS_OF - DAY, 30
)
# 16 Nov tested in 2 of 5 countries: no change for the 17 Nov build.
UNCOVERED = latency_day_delta(
    day(nov(14), 1000) + day(nov(15), 1100) + day(nov(16), 1200, COUNTRIES[:2]), AS_OF, 30
)
NEW = latency_day_delta(day(nov(16), 1200), AS_OF, 30)


def test_a_window_without_a_value_keeps_the_last_one_computed(database):
    assert COMPUTED["pct"] == pytest.approx(10.0) and UNCOVERED["reason"] == DAY_NOT_COVERED
    with connect(database) as conn:
        publish(conn, {"24h": COMPUTED, "7d": COMPUTED, "30d": COMPUTED})
        networks = [entry({"24h": UNCOVERED, "7d": UNCOVERED, "30d": UNCOVERED})]
        carry(conn, networks)
    for w in networks[0]["windows"].values():
        found = w["latency_day_delta"]
        assert found == COMPUTED | {"carried": True} and span(found) == [1, 1]
        assert found["days"][1]["start"] == nov(15)  # the day it is for: 15 Nov


def test_complete_carries_and_every_build_of_the_day_keeps_it(database):
    with connect(database) as conn:
        publish(conn, {"24h": COMPUTED})
        first = entry({"24h": UNCOVERED})
        complete(conn, [first], AS_OF)
        assert first["windows"]["24h"]["latency_day_delta"]["pct"] == pytest.approx(10.0)
        # The next build of the day reads the build that carried it: the same value.
        publish(conn, {"24h": first["windows"]["24h"]["latency_day_delta"]}, as_of=AS_OF)
        later = entry({"24h": UNCOVERED})
        complete(conn, [later], AS_OF + 20 * timedelta(hours=1))
    assert later["windows"]["24h"]["latency_day_delta"] == COMPUTED | {"carried": True}


def test_a_network_never_computed_keeps_its_null_and_reason(database):
    with connect(database) as conn:
        publish(conn, {"24h": UNCOVERED}, provider="shifter")
        networks = [entry({"24h": NEW}, provider="newcomer"), entry({"24h": UNCOVERED})]
        carry(conn, networks)
    assert networks[0]["windows"]["24h"]["latency_day_delta"]["reason"] == NEEDS_TWO_DAYS
    assert networks[1]["windows"]["24h"]["latency_day_delta"] == UNCOVERED


def test_a_later_days_value_is_never_carried_back(database):
    later = latency_day_delta(
        day(nov(14), 1000) + day(nov(15), 1100) + day(nov(16), 1200) + day(nov(17), 1300),
        AS_OF + DAY,
        30,
    )
    with connect(database) as conn:
        publish(conn, {"24h": later}, as_of=AS_OF + DAY)
        networks = [entry({"24h": UNCOVERED})]
        carry(conn, networks)
    assert networks[0]["windows"]["24h"]["latency_day_delta"] == UNCOVERED
