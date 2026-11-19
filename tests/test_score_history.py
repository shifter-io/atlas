"""ATLAS-SCORE-SPARK-1: the Atlas Score history (calculated v19, its source v21) and each network
window's atlas_score_spark (score_history.py)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from calculated_cases import lenient_policy
from psycopg.types.json import Jsonb
from test_calculated_hourly_reads import floor_hour
from test_calculated_incremental import history

from atlas.calculated import score_history, store
from atlas.calculated.calculator import calculate
from atlas.calculated.score_history import KEEP_DAYS, SLOTS, points, record, spark
from atlas.storage import connect

pytest_plugins = ["test_storage", "calculated_template"]

MINUTE, HOUR, DAY = timedelta(minutes=1), timedelta(hours=1), timedelta(days=1)
AS_OF = datetime(2026, 11, 20, 15, 10, tzinfo=UTC)
MIGRATION = Path(score_history.__file__).with_name("migrate_v18_v19.sql")
STORED = "SELECT provider,kind,period,as_of,score,methodology_id FROM calculated_score_history"


@pytest.fixture
def database(calculated_database):
    return calculated_database


def at(spark_points):
    return [p["at"] for p in spark_points]


def scores(spark_points):
    return [(p["score"], p["stored"]) for p in spark_points]


def test_the_24h_spark_is_24_hours_ending_with_its_own_score():
    records = {AS_OF - HOUR: 80.0, AS_OF - 3 * HOUR: None, AS_OF - 23 * HOUR: 70.0}
    records[AS_OF - 30 * HOUR] = 60.0  # before the 24 hours
    records[AS_OF - 10 * HOUR - 30 * MINUTE] = 72.0  # an off-grid build: its hour's
    records[AS_OF - 10 * HOUR - 5 * MINUTE] = 71.0  # the newest of that hour wins
    records[AS_OF - 20 * MINUTE] = 99.0  # the own slot: the build's score wins
    found = points(records, 81.5, AS_OF, "24h")
    assert at(found) == [AS_OF - (23 - i) * HOUR for i in range(24)]
    assert found[-1] == {"at": AS_OF, "score": 81.5, "stored": True}
    assert found[-2] == {"at": AS_OF - HOUR, "score": 80.0, "stored": True}
    # A build that published no score is stored; an hour without a build is not.
    assert scores(found[-4:-2]) == [(None, True), (None, False)]
    assert scores([found[0], found[13]]) == [(70.0, True), (71.0, True)]
    assert sum(p["stored"] for p in found) == 5


def test_the_day_sparks_take_each_utc_days_last_build():
    today = datetime(2026, 11, 20, tzinfo=UTC)
    records = {
        today - DAY + 23 * HOUR + 10 * MINUTE: 75.0,  # 19 Nov, its last build
        today - DAY + 5 * HOUR: 10.0,
        today - 2 * DAY: 74.0,  # 18 Nov 00:00 is 18 Nov's
        today + 2 * HOUR: 99.0,  # today's earlier build: the own score wins
        today - 7 * DAY + HOUR: 50.0,  # 13 Nov: the 30d spark's, before the 7 days
    }
    week = points(records, 76.0, AS_OF, "7d")
    assert at(week) == [today - (5 - i) * DAY for i in range(6)] + [AS_OF]
    assert scores(week[-3:]) == [(74.0, True), (75.0, True), (76.0, True)]
    assert not any(p["stored"] for p in week[:-3])
    month = points(records, 76.0, AS_OF, "30d")
    assert len(month) == SLOTS["30d"] and month[0]["at"] == today - 28 * DAY
    assert scores(month)[-8] == (50.0, True)


def test_a_window_without_records_has_only_its_own_point():
    for label, count in SLOTS.items():
        found = points({}, None, AS_OF, label)
        assert len(found) == count and found[-1] == {"at": AS_OF, "score": None, "stored": True}
        assert not any(p["stored"] for p in found[:-1])


def network(provider, scores_by_window):
    """A head network with each window's balanced score."""
    windows = {w: {"scores": {"balanced": {"score": s}}} for w, s in scores_by_window.items()}
    return {
        "id": f"{provider}:residential:network",
        "provider": provider,
        "kind": "residential",
        "windows": windows,
    }


def publish(conn, as_of, networks, *, published=True, method="m1"):
    """A stored build at as_of with networks in its head, made current when published; each
    methodology's definition names it, so two methodologies differ in an Atlas Score setting."""
    conn.execute(
        "INSERT INTO methodologies VALUES (%s,%s,now()) ON CONFLICT DO NOTHING",
        (method, Jsonb({"name": method})),
    )
    build = conn.execute(
        "INSERT INTO calculation_builds(methodology_id,as_of,input_hash,published) "
        "VALUES (%s,%s,%s,%s) RETURNING build_id",
        (method, as_of, f"{as_of}:{method}", published),
    ).fetchone()[0]
    payload = {"as_of": as_of.isoformat(), "methodology": {"id": method}, "networks": networks}
    conn.execute(
        "INSERT INTO calculated_snapshots(build_id,content_hash,payload) VALUES (%s,'h',%s)",
        (build, Jsonb(payload)),
    )
    if published:
        conn.execute(
            "INSERT INTO current_calculation VALUES (true,%s) ON CONFLICT(singleton) "
            "DO UPDATE SET build_id=excluded.build_id",
            (build,),
        )
    return build


def test_each_publication_records_its_scores_for_the_later_builds(database):
    windows = {"24h": 80.0, "7d": 81.0, "30d": None}
    with connect(database) as conn:
        publish(conn, AS_OF - 2 * HOUR, [network("alpha", windows)])
        record(conn)
        publish(conn, AS_OF - HOUR, [network("alpha", windows | {"24h": 82.0})], method="m2")
        record(conn)
        rows = conn.execute(STORED + " ORDER BY as_of,period").fetchall()
        first = [("alpha", "residential", p, AS_OF - 2 * HOUR, s, "m1") for p, s in windows.items()]
        assert sorted(rows[:3]) == sorted(first) and len(rows) == 6
        # Published again at the same cutoff: the newer publication replaces its scores.
        publish(conn, AS_OF - HOUR, [network("alpha", windows | {"24h": 83.0})], method="m3")
        record(conn)
        assert conn.execute(STORED + " WHERE period='24h' ORDER BY as_of").fetchall()[-1][4:] == (
            83.0,
            "m3",
        )
        # The next build reads them, each window its own range's, and adds its own score; a
        # record of another methodology (m1's) fills its slot marked as the earlier rule's
        # (SCORE-HISTORY-2; it was left out, SCORE-HISTORY-BACKFILL-1).
        built = [network("alpha", {"24h": 84.0, "7d": 85.0}), network("beta", {"24h": None})]
        spark(conn, built, AS_OF, "m3")
    alpha, beta = (n["windows"] for n in built)
    found = alpha["24h"]["atlas_score_spark"]
    assert scores(found[-3:]) == [(80.0, True), (83.0, True), (84.0, True)]
    assert [p.get("earlier_rule", False) for p in found[-3:]] == [True, False, False]
    assert sum(p.get("earlier_rule", False) for p in found) == 1
    assert scores(alpha["7d"]["atlas_score_spark"][-2:]) == [(None, False), (85.0, True)]
    # A network without records has its own point only.
    assert [p["stored"] for p in beta["24h"]["atlas_score_spark"]] == [False] * 23 + [True]


def define(conn, method, **settings):
    """A stored methodology definition: a weight and the settings given."""
    definition = {"weights": {"speed": 0.2}} | settings
    conn.execute("INSERT INTO methodologies VALUES (%s,%s,now())", (method, Jsonb(definition)))


def test_a_display_only_methodology_change_keeps_the_points(database):
    """SCORE-HISTORY-1: "new" differs from "old" only in [success_score] (SUCCESS-ALL-8-1), so
    their Atlas Scores are the same and the spark keeps old's points as its own; "other" changes
    a weight: its record fills its slot as the earlier rule's (SCORE-HISTORY-2). A methodology
    without a stored definition has no other with its Atlas Scores."""
    insert = "INSERT INTO calculated_score_history VALUES (%s,%s,%s,%s,%s,%s,'build')"
    with connect(database) as conn:
        define(conn, "old", success_score={"basis": "daily_tests", "slope": 1.0})
        define(conn, "new", success_score={"basis": "card_targets", "slope": 1.0})
        define(conn, "other", weights={"speed": 0.3})
        for back, score, method in ((1, 80.0, "old"), (2, 70.0, "other"), (3, 60.0, "old")):
            row = ("alpha", "residential", "24h", AS_OF - back * HOUR, score, method)
            conn.execute(insert, row)
        since = AS_OF - DAY
        assert score_history.same_scores(conn, "new", since, AS_OF) == ["new", "old"]
        assert score_history.same_scores(conn, "other", since, AS_OF) == ["other"]
        assert score_history.same_scores(conn, "unknown", since, AS_OF) == ["unknown"]
        built = [network("alpha", {"24h": 90.0})]
        spark(conn, built, AS_OF, "new")
    found = built[0]["windows"]["24h"]["atlas_score_spark"][-4:]
    assert scores(found) == [(60.0, True), (70.0, True), (80.0, True), (90.0, True)]
    assert [p.get("earlier_rule", False) for p in found] == [False, True, False, False]


def test_a_slot_prefers_the_builds_rule_and_marks_an_earlier_ones():
    """SCORE-HISTORY-2: in a slot with records of both, the build's rule wins even when the
    earlier one's is newer; a slot with only an earlier record shows it, marked; the own point
    is never marked."""
    records = {AS_OF - HOUR - 30 * MINUTE: 70.0, AS_OF - HOUR - 5 * MINUTE: 60.0}
    records |= {AS_OF - 2 * HOUR: 50.0, AS_OF - 3 * HOUR: None}
    earlier = {AS_OF - HOUR - 5 * MINUTE, AS_OF - 2 * HOUR, AS_OF - 3 * HOUR}
    found = points(records, 81.0, AS_OF, "24h", earlier)
    assert scores(found[-4:]) == [(None, True), (50.0, True), (70.0, True), (81.0, True)]
    marked = [p.get("earlier_rule", False) for p in found[-4:]]
    assert marked == [True, True, False, False]
    # Without earlier records the newest of each slot wins, as before.
    assert scores(points(records, 81.0, AS_OF, "24h")[-4:-1]) == [
        (None, True),
        (50.0, True),
        (60.0, True),
    ]


def test_records_before_31_whole_days_are_forgotten(database):
    with connect(database) as conn:
        old = datetime(2026, 10, 20, 23, 10, tzinfo=UTC)  # 31 days before 20 Nov 00:00 is kept
        publish(conn, old - DAY, [network("alpha", {"24h": 70.0})])
        record(conn)
        publish(conn, old, [network("alpha", {"24h": 71.0})])
        record(conn)
        publish(conn, AS_OF, [network("alpha", {"24h": 72.0})])
        record(conn)
        kept = [r[3] for r in conn.execute(STORED + " ORDER BY as_of").fetchall()]
    assert kept == [old, AS_OF] and old >= AS_OF.replace(hour=0, minute=0) - KEEP_DAYS * DAY


def test_the_migration_fills_the_history_from_the_published_builds(database):
    with connect(database) as conn:
        publish(conn, AS_OF - 2 * DAY, None)  # a head without networks (before payload v4)
        publish(conn, AS_OF - DAY, [network("alpha", {"24h": 70.0, "7d": None})], method="old")
        publish(conn, AS_OF - DAY, [network("alpha", {"24h": 71.0})], method="new")
        publish(conn, AS_OF - HOUR, [network("alpha", {"24h": 90.0})], published=False)
        publish(conn, AS_OF, [network("alpha", {"24h": 72.0})])
        conn.execute(MIGRATION.read_text())
        rows = conn.execute(STORED + " ORDER BY as_of,period").fetchall()
    # The newest build of a cutoff wins; a draft is not recorded; the scores stay as published.
    assert rows == [
        ("alpha", "residential", "24h", AS_OF - DAY, 71.0, "new"),
        ("alpha", "residential", "7d", AS_OF - DAY, None, "old"),
        ("alpha", "residential", "24h", AS_OF, 72.0, "m1"),
    ]


@pytest.mark.asyncio
async def test_the_next_build_draws_the_published_builds_score(database, tmp_path):
    """Two published builds an hour apart on the 3-day fixture: the second's 24h spark has the
    first's 24h score an hour before its own, and the history holds both builds' scores."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    first = calculate(database, policy, as_of=end - HOUR, mode="incremental", publish=True)
    second = calculate(database, policy, as_of=end, mode="incremental", publish=True)
    # The first build is expired by the second's publication (one build a UTC day is kept).
    before = {n["id"]: n["windows"] for n in first["networks"]}
    for net in store.read(database, second["snapshot_id"])["networks"]:
        trend = net["windows"]["24h"]["atlas_score_spark"]
        own, earlier = before[net["id"]]["24h"]["scores"]["balanced"]["score"], trend[-2]
        assert earlier == {"at": (end - HOUR).isoformat(), "score": own, "stored": True}
    with connect(database) as conn:
        count = conn.execute("SELECT count(*) FROM calculated_score_history").fetchone()[0]
    assert count == 2 * 3 * len(before)


@pytest.mark.asyncio
async def test_a_build_after_a_success_score_change_keeps_its_sparks_points(database, tmp_path):
    """SCORE-HISTORY-1 (the rehearsal of SUCCESS-ALL-8-1 lost every point): the second build's
    methodology differs from the first's in [success_score] alone, a new methodology id, and its
    24h spark still holds the first build's score an hour before its own."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    text = policy.read_text()
    old = '[success_score]\nbasis = "card_targets"\nslope = 1.0'
    assert old in text
    changed = tmp_path / "success-changed.toml"
    changed.write_text(text.replace(old, old.replace("1.0", "2.0")))
    first = calculate(database, policy, as_of=end - HOUR, mode="incremental", publish=True)
    second = calculate(database, changed, as_of=end, mode="incremental", publish=True)
    assert first["methodology"]["id"] != second["methodology"]["id"]
    before = {n["id"]: n["windows"] for n in first["networks"]}
    assert before
    for net in store.read(database, second["snapshot_id"])["networks"]:
        own = before[net["id"]]["24h"]["scores"]["balanced"]["score"]
        earlier = net["windows"]["24h"]["atlas_score_spark"][-2]
        assert earlier == {"at": (end - HOUR).isoformat(), "score": own, "stored": True}


@pytest.mark.asyncio
async def test_a_build_after_an_atlas_score_change_keeps_the_earlier_points_marked(
    database, tmp_path
):
    """SCORE-HISTORY-2 (one recount that changes the Atlas Score, LATENCY-BASKET-1): the second
    build's methodology differs in a setting the Atlas Score may read, and its 24h spark shows the
    first build's score an hour before its own, marked as the earlier rule's."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    text = policy.read_text()
    assert 'name = "Measured draft v2"' in text
    changed = tmp_path / "score-changed.toml"
    changed.write_text(text.replace('name = "Measured draft v2"', 'name = "Measured draft v3"'))
    first = calculate(database, policy, as_of=end - HOUR, mode="incremental", publish=True)
    second = calculate(database, changed, as_of=end, mode="incremental", publish=True)
    before = {n["id"]: n["windows"] for n in first["networks"]}
    assert before
    for net in store.read(database, second["snapshot_id"])["networks"]:
        own = before[net["id"]]["24h"]["scores"]["balanced"]["score"]
        trend = net["windows"]["24h"]["atlas_score_spark"]
        point = {"at": (end - HOUR).isoformat(), "score": own, "stored": True}
        assert trend[-2] == point | {"earlier_rule": True}
        assert "earlier_rule" not in trend[-1]
