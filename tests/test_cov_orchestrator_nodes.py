"""Node identity (orchestrator/nodes.py): the refused tokens, the unseeded database, and a browser
node, whose region row the first token creates, rotated with its host kept."""

import pytest

from atlas.browser.contracts import BROWSER_REGION_ID
from atlas.orchestrator.nodes import issue_token, token_hash
from atlas.storage import connect, seed

pytest_plugins = ["test_storage"]


def rows(config, statement):
    with connect(config) as conn:
        return conn.execute(statement).fetchall()


@pytest.mark.parametrize(("provider", "product"), [("", "res"), ("alpha", "")])
def test_a_node_needs_a_provider_and_a_product(tmp_path, provider, product):
    # Refused before the credentials file is opened.
    with pytest.raises(ValueError, match="needs a provider and a product"):
        issue_token(
            str(tmp_path / "missing.toml"), provider=provider, product=product, region="fra1"
        )


@pytest.mark.parametrize("host", [None, "", "browser", "browser-1;x", "host-1"])
def test_a_browser_node_needs_its_logical_host_name(tmp_path, host):
    with pytest.raises(ValueError, match="browser-<n>"):
        issue_token(
            str(tmp_path / "missing.toml"),
            provider="alpha",
            product="res",
            region="browser",
            host=host,
        )


def test_an_unseeded_database_refuses_a_cloud_node_and_writes_nothing(database):
    with pytest.raises(ValueError, match="Runner regions are not seeded"):
        issue_token(database, provider="alpha", product="res", region="fra1")
    assert rows(database, "SELECT count(*) FROM providers") == [(0,)]
    assert rows(database, "SELECT count(*) FROM nodes") == [(0,)]


def test_a_browser_node_creates_its_region_and_keeps_its_host_on_rotation(database):
    first = issue_token(
        database, provider="alpha", product="res", region="browser-2", host="browser-2"
    )
    assert (first["region"], first["provider"], first["product"], first["rotated"]) == (
        "browser",
        "alpha",
        "res",
        False,
    )
    assert rows(database, "SELECT region_id,slug FROM runner_regions") == [
        (BROWSER_REGION_ID, "browser")
    ]
    # The same node again, by its region's name this time: the token's hash is replaced.
    second = issue_token(
        database, provider="alpha", product="res", region="browser", host="browser-2"
    )
    assert second["rotated"] and second["node_id"] == first["node_id"]
    assert second["token"] != first["token"]
    assert rows(database, "SELECT host,token_hash FROM nodes") == [
        ("browser-2", token_hash(second["token"]))
    ]
    assert rows(database, "SELECT provider,name FROM products") == [("alpha", "res")]


def test_a_cloud_node_rotated_without_a_host_keeps_the_stored_one(database):
    seed(database)
    first = issue_token(database, provider="alpha", product="res", region="fra1", host="edge-a")
    second = issue_token(database, provider="alpha", product="res", region="fra1")
    assert (second["node_id"], second["region"], second["rotated"]) == (
        first["node_id"],
        "fra1",
        True,
    )
    assert rows(database, "SELECT host,token_hash FROM nodes") == [
        ("edge-a", token_hash(second["token"]))
    ]
