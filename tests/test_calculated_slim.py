import copy
import json
from datetime import UTC, datetime

import pytest
from aiohttp.test_utils import TestClient, TestServer
from census_pulse_cases import scenario
from test_calculated import POLICY, lenient_policy, payload_mismatches

from proxy_index.calculated import store
from proxy_index.calculated.aggregate import BREAKDOWNS
from proxy_index.calculated.api import app
from proxy_index.calculated.calculator import calculate
from proxy_index.calculated.policy import PAYLOAD, load, payload_limits
from proxy_index.calculated.slim import POOL_LISTS, slim
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]

TINY = {name: 1 for name in PAYLOAD if name not in ("country", "countries_breakdown")}
LISTS = (*POOL_LISTS, "list_entries")


def rows(n, **label):
    return [label | {"ips": n - i, "observations": n - i, "completed": n - i} for i in range(n)]


def synthetic(n):
    """A one-product payload whose every list has n rows, with totals counted from all of them."""
    window = {
        "metrics": {"completed": n * (n + 1) // 2},
        "breakdowns": {name: rows(n, country_code="DE") for name in BREAKDOWNS},
        "pool": {"unique_ips": n, "countries": 1, "asns": n}
        | {name: rows(n, country_code="DE") for name in POOL_LISTS},
    }
    return {"schema_version": 2, "products": [{"product_id": 1, "windows": {"30d": window}}]}


def test_slim_caps_every_list_keeps_totals_and_counts_and_leaves_the_input_alone():
    payload = synthetic(3000)
    before = copy.deepcopy(payload)
    result = slim(payload, {})
    assert payload == before
    w, full = result["products"][0]["windows"]["30d"], payload["products"][0]["windows"]["30d"]
    for name, rows_ in w["breakdowns"].items():
        assert rows_ == full["breakdowns"][name][: PAYLOAD[name]]
    assert w["breakdown_entries"] == dict.fromkeys(BREAKDOWNS, 3000)
    for name in POOL_LISTS:
        assert w["pool"][name] == full["pool"][name][: PAYLOAD[name]]
    assert w["pool"]["list_entries"] == dict.fromkeys(POOL_LISTS, 3000)
    # Totals are the full data's; the snapshot keeps them as they were.
    assert {k: w["pool"][k] for k in ("unique_ips", "countries", "asns")} == {
        "unique_ips": 3000,
        "countries": 1,
        "asns": 3000,
    }
    assert w["metrics"] == full["metrics"]
    # The published size is bounded by the caps, not by the long tail.
    assert len(json.dumps(payload)) > 2_000_000
    assert len(json.dumps(result)) < 250_000


def test_slim_uses_the_payload_settings_and_older_windows_without_breakdowns():
    payload = synthetic(5)
    result = slim(payload, {"payload": {"city_asn": 0, "geo": 2}})
    w = result["products"][0]["windows"]["30d"]
    assert w["breakdowns"]["city_asn"] == [] and w["breakdown_entries"]["city_asn"] == 5
    assert len(w["pool"]["geo"]) == 2 and len(w["breakdowns"]["country"]) == 5
    del payload["products"][0]["windows"]["30d"]["breakdowns"]
    w = slim(payload, {"payload": {"geo": 1}})["products"][0]["windows"]["30d"]
    assert "breakdowns" not in w and "breakdown_entries" not in w
    assert len(w["pool"]["geo"]) == 1 and w["pool"]["list_entries"]["geo"] == 5


@pytest.mark.parametrize("setting", ["city_asn = -1", "city_asn = 2.5", "cities = 3"])
def test_payload_settings_are_validated(tmp_path, setting):
    policy = tmp_path / "methodology.toml"
    policy.write_text(POLICY.read_text() + f"\n[payload]\n{setting}\n")
    with pytest.raises(ValueError, match="Invalid payload settings|Unknown methodology"):
        load(policy)


def test_default_caps_leave_the_methodology_id_alone(tmp_path):
    config, method_id = load(POLICY)
    assert "payload" not in config and payload_limits(config) == PAYLOAD
    policy = tmp_path / "methodology.toml"
    policy.write_text(POLICY.read_text() + "\n[payload]\ncity_asn = 10\n")
    config, other = load(policy)
    assert other != method_id and payload_limits(config)["city_asn"] == 10


def tiny_policy(tmp_path):
    policy = lenient_policy(tmp_path)
    caps = "".join(f"{name} = {n}\n" for name, n in TINY.items())
    policy.write_text(policy.read_text() + f"\n[payload]\n{caps}")
    return policy


async def test_published_snapshot_is_capped_while_typed_rows_and_totals_stay_whole(
    database, tmp_path
):
    as_of, _ = await scenario(database, tmp_path)
    as_of = datetime.fromtimestamp(as_of.timestamp() // 300 * 300, UTC)
    whole = calculate(database, lenient_policy(tmp_path), as_of=as_of)
    capped = calculate(database, tiny_policy(tmp_path), as_of=as_of, publish=True)
    # The stored and served snapshot is the capped payload calculate() returned.
    assert store.read(database) == capped
    assert payload_mismatches(database, int(capped["snapshot_id"])) == []
    truncated = 0
    for full, small in zip(whole["products"], capped["products"], strict=True):
        for label, w in small["windows"].items():
            f = full["windows"][label]
            for name, rows_ in w["breakdowns"].items():
                assert rows_ == f["breakdowns"][name][: (PAYLOAD | TINY)[name]]
                assert w["breakdown_entries"][name] == len(f["breakdowns"][name])
                truncated += len(rows_) < len(f["breakdowns"][name])
            for name in POOL_LISTS:
                assert w["pool"][name] == f["pool"][name][: (PAYLOAD | TINY)[name]]
                assert w["pool"]["list_entries"][name] == len(f["pool"][name])
            # Every total and metric is the uncapped calculation's.
            totals = lambda pool: {k: v for k, v in pool.items() if k not in LISTS}  # noqa: E731
            assert totals(w["pool"]) == totals(f["pool"])
            assert w["metrics"] == f["metrics"] and w["series"] == f["series"]
    assert truncated
    with connect(database) as conn:
        typed = conn.execute(
            "SELECT count(*) FROM calculated_list_rows JOIN calculated_windows w "
            "USING(list_id) WHERE w.build_id=%s",
            (int(capped["snapshot_id"]),),
        ).fetchone()[0]
    assert typed == sum(
        n
        for p in capped["products"]
        for w in p["windows"].values()
        for n in w["breakdown_entries"].values()
    )
    async with TestClient(TestServer(app(database))) as client:
        body = await (await client.get("/v1/breakdowns?window=24h")).json()
    assert all(d["dimension_entries"] and "list_entries" in d["breakdowns"] for d in body["data"])
