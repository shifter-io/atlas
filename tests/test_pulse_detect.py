"""detect's per-country recovery leaves a network outage (source 'pulse', no batch) open.

Standalone, without the pulse evaluator, so it also runs on a tree without it."""

from datetime import UTC, datetime

from census_pulse_cases import product
from orchestrator_cases import prepare, query

from proxy_index import detect
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]

W0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def test_a_healthy_country_pulse_does_not_close_a_network_outage(database):
    prepare(database, ("DE",))
    with connect(database) as conn:
        alpha = product(conn, "alpha")
        conn.execute(
            "INSERT INTO outages(product_id,country_code,region_id,source,started_at) "
            "SELECT %s,country_code,region_id,'pulse',%s FROM countries WHERE country_code='DE'",
            (alpha, W0),
        )
    with connect(database) as conn:
        later = {"started_at": W0.replace(hour=13), "start_at": W0.replace(hour=13)}
        assert detect._close_outages(conn.cursor(), {**later, "country_code": "DE"}, alpha) == []
    assert query(database, "SELECT ended_at FROM outages") == [(None,)]
