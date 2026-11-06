"""The grammar schema: refused changes, served countries, the testing table's answer classes and
the campaign concurrency."""

import tomllib

import pytest
from grammars_cases import ROOT

from proxy_index.providers.grammar import Grammar, load_grammar

@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 2},
        {"unknown": True},
        {"credential": {"flags_in": "port"}},
        {"flags": {"country": {"kind": "unknown", "format": "c-{}"}}},
        {"flags": {"region": {"kind": "name", "format": "r-{}", "requires": ["nothing"]}}},
        {"session": {"flag": "session", "generate": "emoji"}},
        {"gateway": {"host": "bad/host", "ports": {"http": 1}}},
        {"gateway": {"host": "ok.example", "ports": {"https": 1}}},
        {"gateway": {"host": "ok.example"}},
        {"credential": {"flags_in": "username", "typo": 1}},
        {"flags": {"country": {"kind": "country", "format": "c-{}", "require_session": True}}},
        {"session": {"flag": "session", "ttl_flag": "missing"}},
        {"session": {"flag": "missing"}},
        {"session": {"flag": "session", "length": 0}},
        {"id": None},
        {"flags": {"country": {"kind": "token", "format": "c-{}", "pattern": "["}}},
        {"session": {"flag": "session", "generate": "alnum", "length": 300}},
        {"flags": {"country": {"kind": "country", "format": "c-{}", "preset": True}}},
        {"session": {"flag": "session", "slot_flag": "session"}},  # Port grammars only.
        {"session": {"flag": "session", "no_session_policy": "per_request"}},
    ],
)
def test_grammar_schema_is_validated(change):
    data = tomllib.loads((ROOT / "src/proxy_index/providers/grammars/massive.toml").read_text())
    data.update(change)
    with pytest.raises(ValueError):
        Grammar.parse(data)


@pytest.mark.parametrize("flag,countries", [("country", ["us"]), ("region", ["US"])])
def test_served_countries_are_upper_case_iso_codes_of_a_country_flag(flag, countries):
    data = tomllib.loads((ROOT / "src/proxy_index/providers/grammars/massive.toml").read_text())
    data["flags"][flag]["countries"] = countries
    with pytest.raises(ValueError, match="countries must be ISO codes"):
        Grammar.parse(data)

def test_answer_classes_from_testing_table():
    from proxy_index.models import AnswerClasses
    from proxy_index.providers.grammar import answer_classes

    assert answer_classes("geonode") == AnswerClasses(("429", "469"), ("465",))
    assert answer_classes("dataimpulse") == AnswerClasses(("429", "proxy_connect"), ())
    assert answer_classes("soax") == AnswerClasses(("429",), ())
    data = tomllib.loads((ROOT / "src/proxy_index/providers/grammars/massive.toml").read_text())
    data["testing"] = {
        "rate_limit_statuses": [430, 599],
        "no_inventory_statuses": [400],
        "rate_limit_connect": False,
    }
    parsed = Grammar.parse(data)
    assert (parsed.rate_limit_statuses, parsed.no_inventory_statuses) == ((430, 599), (400,))
    assert parsed.rate_limit_connect is False


@pytest.mark.parametrize(
    "testing",
    [
        {"rate_limit_statuses": [429]},
        {"rate_limit_statuses": [399]},
        {"rate_limit_statuses": [600]},
        {"rate_limit_statuses": [469, 469]},
        {"rate_limit_statuses": 469},
        {"rate_limit_statuses": [True]},
        {"rate_limit_statuses": ["469"]},
        {"no_inventory_statuses": [429]},
        {"rate_limit_statuses": [465], "no_inventory_statuses": [465]},
        {"rate_limit_connect": 1},
        {"rate_limit_connect": "yes"},
        {"rate_limits": [469]},
    ],
)
def test_testing_table_rejects_bad_answer_classes(testing):
    data = tomllib.loads((ROOT / "src/proxy_index/providers/grammars/massive.toml").read_text())
    data["testing"] = testing
    with pytest.raises(ValueError, match="Grammar|grammar"):
        Grammar.parse(data)


def test_geonode_campaign_concurrency_is_500():
    from proxy_index.campaign.planspec import network_concurrency

    assert load_grammar("geonode").max_concurrency == 500
    assert network_concurrency("geonode", 2000) == 500
    assert network_concurrency("geonode", 250) == 250
