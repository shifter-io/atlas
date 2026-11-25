"""LATENCY-DELTA-1 (#1386): the 30d latency change compares the two 30-day periods' basket p50, as
the 24h and 7d ones do, from the per-day store's basket countries (calculated_latency_country_days,
calculated v24) and the raw rows' days. A period with a day stored without them (before v24)
has no value, so the window keeps its 7-day or day change until 30 such days are stored."""

from datetime import timedelta

import pytest
from migration_cases import catalog
from scoring_cases import row
from test_latency_days import COUNTRIES, HOUR
from test_latency_store import AS_OF, TARGET, days, dec, span

from atlas.calculated import store
from atlas.calculated.latency_store import complete, day_records
from atlas.storage import connect, migrate

pytest_plugins = ["test_storage", "calculated_template"]

DAY = timedelta(days=1)
SETTINGS = {"basket": list(COUNTRIES), "aggregate": "mean", "penalty": "slowest_network"}
KEPT = {"pct": 0.5, "days": []}  # the window's 7-day or day change (latency_basket.deltas())
COUNTRY_ROWS = "SELECT provider,day,c.country,c.slots,d.as_of FROM calculated_latency_country_days "
COUNTRY_ROWS += "c JOIN calculated_latency_days d USING(provider,kind,target,day) ORDER BY 1,2,3"


@pytest.fixture
def database(calculated_database):
    return calculated_database


def changing(countries=COUNTRIES):
    """14 Nov to 13 Jan in countries: 1000 ms until 14 Dec, 1100 ms from 15 Dec."""
    return days(dec(1) - 17 * DAY, 31, 1000, countries) + days(dec(15), 30, 1100, countries)


def steady(ms, countries):
    return days(dec(1) - 17 * DAY, 61, ms, countries)


def build(conn, networks, as_of, held=()):
    """complete() over the 30d windows of networks ({provider: rows}) a build at as_of makes;
    their 30d changes."""
    since, windows = as_of - 30 * DAY, {}
    for provider, rows in networks.items():
        own = [r for r in rows if since <= r["bucket_start"] < as_of]
        records = day_records(own, TARGET, since, as_of, 30, SETTINGS)
        windows[provider] = {"_latency_days": records, "latency_day_delta": KEPT}
        windows[provider] |= {"_held": None} if provider in held else {}
    entries = [{"provider": p, "kind": "k", "windows": {"30d": w}} for p, w in windows.items()]
    complete(conn, entries, as_of)
    return {p: w["latency_day_delta"] for p, w in windows.items()}


def p50s(found):
    return [d["p50"] for d in found["days"]]


def test_the_30_day_change_compares_the_basket_p50_of_stored_and_raw_days(database):
    # p serves the basket; q only US and DE at 500 ms; h (held) only JP, which p and q lack.
    networks = {"p": changing(COUNTRIES[:4]), "q": steady(500, COUNTRIES[:2])}
    networks["h"] = steady(3000, ["JP"])
    with connect(database) as conn:
        for at in (dec(14), dec(20)):  # the raw days of these builds fill the store
            assert build(conn, networks, at, held={"h"})["p"] is KEPT
        found = build(conn, networks, AS_OF, held={"h"})
        stored = conn.execute(COUNTRY_ROWS).fetchall()
    assert {p: span(f) for p, f in found.items()} == {"p": [30, 30], "q": [30, 30], "h": [30, 30]}
    # JP is no measured network's: p and q leave it out. q's FR and GB count at p's latency.
    assert found["p"]["pct"] == pytest.approx(10.0) and p50s(found["p"]) == [1000, 1100]
    assert p50s(found["q"]) == [750, 800] and found["q"]["pct"] == pytest.approx(50 / 7.5)
    # h's own JP counts in its penalties: (4 × 1000 + 3000) / 5, then (4 × 1100 + 3000) / 5.
    assert p50s(found["h"]) == [1400, 1480]
    # One row a network, day and basket country it has a latency in, each slot weighing 1.
    assert {(p, c) for p, _, c, *_ in stored} == {
        *(("p", c) for c in COUNTRIES[:4]),
        *(("q", c) for c in COUNTRIES[:2]),
        ("h", "JP"),
    }
    assert all(slots == 1 for *_, slots, _ in stored)


def test_days_stored_before_the_basket_keep_the_windows_shorter_change(database):
    networks = {"p": changing()}
    with connect(database) as conn:
        for at in (dec(14), dec(20)):
            build(conn, networks, at)
        # As v23 stored them: no basket, a digest of the day without its countries.
        conn.execute("UPDATE calculated_latency_days SET basket=NULL,digest='v23'")
        conn.execute("DELETE FROM calculated_latency_country_days")
        assert build(conn, networks, AS_OF)["p"] is KEPT
        # The raw days (from 16 Dec) are written again with their countries, not the older ones.
        written = "SELECT min(day) FROM calculated_latency_days WHERE basket IS NOT NULL"
        assert conn.execute(written).fetchone() == (dec(16),)


def test_an_earlier_cutoff_never_replaces_a_later_ones_country_days(database):
    networks = {"p": changing()}
    with connect(database) as conn:
        build(conn, networks, dec(20))  # writes 20 Nov to 19 Dec
        before = conn.execute(COUNTRY_ROWS).fetchall()
        # A replay of an earlier cutoff with a late test of 1 Dec, a day the later one wrote: it
        # writes only the days the later one did not (14 to 19 Nov).
        late = {"p": [*networks["p"], row(dec(1) + 20 * HOUR, 4000, country="US")]}
        build(conn, late, dec(14))
        after = conn.execute(COUNTRY_ROWS).fetchall()
        assert [r for r in after if r[1] >= dec(1) - 11 * DAY] == before
        assert {r[1] for r in after} - {r[1] for r in before} == {
            dec(1) - n * DAY for n in range(12, 18)
        }
        build(conn, networks, dec(20) + 70 * DAY)  # the days before its 62 are forgotten
        assert conn.execute(COUNTRY_ROWS).fetchall() == []


def test_v23_layer_gains_the_basket_and_keeps_every_latency_day(
    databases, database, calculated_template
):
    with connect(database) as conn:
        build(conn, {"p": changing()}, dec(20))
        conn.execute("DROP TABLE calculated_latency_country_days")
        conn.execute("ALTER TABLE calculated_latency_days DROP COLUMN basket")
        conn.execute("UPDATE calculation_schema SET version=23,checksum=%s", (store.V23_CHECKSUM,))
        before = conn.execute("SELECT * FROM calculated_latency_days ORDER BY day").fetchall()
    result = migrate(database)
    assert (result["from_calculated_schema_version"], result["calculated_schema_version"]) == (
        23,
        24,
    )
    assert result["verified"] == list(store.V23_CHECKS)
    with connect(database) as conn:
        after = conn.execute("SELECT * FROM calculated_latency_days ORDER BY day").fetchall()
        assert after == [(*r, None) for r in before] and len(after) == 30
        countries = "SELECT count(*) FROM calculated_latency_country_days"
        assert conn.execute(countries).fetchone() == (0,)
        # The check counts a day the step would have changed.
        conn.execute(store.V23_SNAPSHOT)
        conn.execute("UPDATE calculated_latency_days SET slots=slots+1 WHERE day=%s", (dec(1),))
        check = store.V23_CHECKS["latency days kept with their basket"]
        assert conn.execute(check).fetchone() == (1,)
        conn.rollback()
    assert catalog(database) == catalog(databases(template=calculated_template))
