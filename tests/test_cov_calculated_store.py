"""calculated/store.py: what validate_payload() refuses in each snapshot version, and the refusals
of initialize(), assign_role(), evidence() and publish() against a real database."""

import copy
import json
from datetime import datetime, timedelta

import pytest
from calculated_cases import POLICY
from test_storage import measurement

from atlas.calculated import store
from atlas.calculated.calculator import calculate
from atlas.calculated.store import validate_payload
from atlas.storage import connect, ingest

pytest_plugins = ["test_storage"]


def counters(completed=2, successes=1):
    return {"completed": completed, "successes": successes, "errors": completed - successes}


def window():
    """A reconciling window: one breakdown row, one target, a ready score."""
    return {
        "series": [{"uptime": 0.5}, {"uptime": None}],
        "breakdowns": {"country": [{"completed": 2, "successes": 1}]},
        "metrics": counters() | {"challenge_rate": 0, "block_rate": 0.5, "uptime": 1},
        "benchmark": counters(),
        "targets": {"generic_http": {"metrics": counters(1, 1)}},
        "pool": {"observations": 1},
        "scores": {"balanced": {"state": "ready", "score": 50}, "speed": {"state": "pending"}},
    }


def payload(version=2):
    return {
        "schema_version": version,
        "methodology": {"id": "m"},
        "events": [],
        "traffic": {"by_test": {"census": {}, "pulse": {}, "load": {}}},
        "products": [{"census": [], "windows": {"24h": window(), "7d": window()}}],
    }


def changed(version, edit):
    result = payload(version)
    edit(result, result["products"][0], result["products"][0]["windows"]["24h"])
    return result


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_a_reconciling_payload_of_every_version_is_accepted(version):
    assert validate_payload(payload(version)) is None
    # The groups can come apart from the head (a stored head checks them one at a time).
    head = payload(version)
    groups = head.pop("products")
    assert validate_payload(head | {"products": []}, iter(groups)) is None


def test_a_version_1_payload_needs_no_events_traffic_census_or_rates():
    def v1(p, product, w):
        del p["events"], p["traffic"], product["census"], w["metrics"]["uptime"]
        w["breakdowns"]["country"][0]["successes"] = 5  # breakdowns are not checked in v1

    assert validate_payload(changed(1, v1)) is None


@pytest.mark.parametrize(
    ("version", "edit", "message"),
    [
        (5, lambda p, product, w: None, "Invalid snapshot contract"),
        (2, lambda p, product, w: p["methodology"].update(id=""), "Invalid snapshot contract"),
        (2, lambda p, product, w: p.update(events={}), "Invalid snapshot contract"),
        (2, lambda p, product, w: p["traffic"].update(by_test={"census": {}}), "contract"),
        (2, lambda p, product, w: p["traffic"]["by_test"].update(other={}), "contract"),
        (2, lambda p, product, w: product.update(census=None), "Invalid snapshot contract"),
        (1, lambda p, product, w: w["series"].append({"uptime": 2}), "fractions or null"),
        (2, lambda p, product, w: w["series"].append({"uptime": "1"}), "fractions or null"),
        (
            2,
            lambda p, product, w: w["breakdowns"]["country"].append(
                {"completed": 1, "successes": 2}
            ),
            "Breakdown counters do not reconcile",
        ),
        (
            3,
            lambda p, product, w: w["breakdowns"]["country"].append(
                {"completed": 1, "successes": -1}
            ),
            "Breakdown counters do not reconcile",
        ),
        (2, lambda p, product, w: w["metrics"].update(block_rate=1.5), "rates must be fractions"),
        (2, lambda p, product, w: w["metrics"].update(uptime=-0.1), "rates must be fractions"),
        (1, lambda p, product, w: w["metrics"].update(errors=0), "Snapshot counters"),
        (2, lambda p, product, w: w["benchmark"].update(completed=-1, errors=-2), "counters"),
        (
            2,
            lambda p, product, w: w["targets"]["generic_http"]["metrics"].update(successes=0),
            "Snapshot counters do not reconcile",
        ),
        (2, lambda p, product, w: w["pool"].update(observations=2), "IP observations exceed"),
        (
            2,
            lambda p, product, w: w["scores"]["speed"].update(state="ready", score=None),
            "Ready score is missing",
        ),
    ],
)
def test_validate_payload_refuses_each_broken_invariant(version, edit, message):
    with pytest.raises(ValueError, match=message):
        validate_payload(changed(version, edit))


def test_a_stored_v3_window_names_its_list_instead_of_its_rows():
    def stored(lists):
        def edit(p, product, w):
            del w["breakdowns"]
            w["lists"] = lists

        return changed(3, edit)

    assert validate_payload(stored({"id": 7, "entries": {"country": 1, "asn": 0}})) is None
    for lists in (
        None,
        {"id": "7", "entries": {}},
        {"id": 7, "entries": []},
        {"id": 7, "entries": {"country": -1}},
    ):
        with pytest.raises(ValueError, match="Invalid snapshot contract"):
            validate_payload(stored(lists))


def test_a_payload_with_a_nonfinite_number_is_refused():
    broken = payload(2)
    broken["products"][0]["windows"]["24h"]["latency"] = float("nan")
    with pytest.raises(ValueError, match="JSON compliant"):
        validate_payload(broken)
    # Checked group by group, the groups are already JSON: they are not dumped again.
    groups = broken.pop("products")
    assert validate_payload(broken | {"products": []}, groups) is None


def test_validate_payload_does_not_change_the_payload():
    original = payload(2)
    kept = copy.deepcopy(original)
    validate_payload(original)
    assert original == kept


def test_initialize_refuses_a_calculated_layer_with_another_checksum(database):
    assert store.initialize(database) == store.initialize(database)
    with connect(database) as conn:
        conn.execute("UPDATE calculation_schema SET checksum='edited'")
    with pytest.raises(ValueError, match="explicit migration is required"):
        store.initialize(database)


async def test_assign_role_sets_and_replaces_the_role_of_a_stored_run(database, tmp_path):
    store.initialize(database)
    run = await measurement(tmp_path, requests=1)
    ingest(run, database)
    run_id = json.loads((run / "summary.json").read_text())["run_id"]
    with pytest.raises(ValueError, match="Unknown collection role"):
        store.assign_role(database, run_id, "observer")
    with pytest.raises(ValueError, match="Unknown run"):
        store.assign_role(database, "00000000-0000-4000-8000-000000000000", "monitor")
    assert store.assign_role(database, run_id, "benchmark") == {
        "run_id": run_id,
        "role": "benchmark",
    }
    store.assign_role(database, run_id, "pool_sampling")
    with connect(database) as conn:
        assert conn.execute(
            "SELECT r.run_id::text,c.role FROM calculation_roles c JOIN test_runs r USING(run_key)"
        ).fetchall() == [(run_id, "pool_sampling")]


def test_evidence_refuses_an_empty_list_a_non_record_and_a_bad_interval(database):
    store.initialize(database)
    record = {
        "product_id": 1,
        "cohort": "test",
        "metric": "uptime_pct",
        "value": 99.0,
        "period_start": "2026-09-01T00:00:00+00:00",
        "period_end": "2026-09-02T00:00:00+00:00",
        "valid_until": "2026-09-03T00:00:00+00:00",
        "source": "fixture",
    }
    with pytest.raises(ValueError, match="No evidence records"):
        store.evidence(database, [])
    with pytest.raises(ValueError, match="known metric and the documented fields"):
        store.evidence(database, ["uptime_pct"])
    for interval in (
        {"period_start": "2026-09-01T00:00:00"},  # no time zone
        {"period_end": "2026-09-01T00:00:00+00:00"},  # empty period
        {"valid_until": "2026-09-01T12:00:00+00:00"},  # valid only before the period ends
    ):
        with pytest.raises(ValueError, match="Invalid evidence interval"):
            store.evidence(database, record | interval)
    with pytest.raises(ValueError, match="comparable cohort"):
        store.evidence(database, record | {"cohort": " "})
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM metric_evidence").fetchone() == (0,)


async def test_publish_refuses_a_snapshot_older_than_the_current_one(database, tmp_path):
    store.initialize(database)
    run = await measurement(tmp_path, requests=2)
    ingest(run, database)
    end = datetime.fromisoformat(json.loads((run / "summary.json").read_text())["finished_at"])
    newer = calculate(database, POLICY, as_of=end + timedelta(minutes=10), publish=True)
    older = calculate(database, POLICY, as_of=end + timedelta(minutes=5))["snapshot_id"]
    with pytest.raises(ValueError, match="older than the current measurement cutoff"):
        store.publish(database, older)
    assert store.head(database)[0] == int(newer["snapshot_id"])
    # The same cutoff, or a later one, may be published.
    same = calculate(database, POLICY, as_of=end + timedelta(minutes=10))["snapshot_id"]
    assert store.publish(database, same)["published_snapshot"] == same
