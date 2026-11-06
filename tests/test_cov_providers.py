"""COV-5: the refusals of providers/ that no other test reaches (configuration sections,
credential files, grammar checks, gateway and preset rules)."""

import dataclasses
import tomllib
import types
from pathlib import Path

import pytest

from proxy_index import GrammarProxy, RequestContext, load_provider
from proxy_index.providers import grammar as grammar_module
from proxy_index.providers import proxy as proxy_module
from proxy_index.providers.credentials import read_credentials
from proxy_index.providers.grammar import Grammar, load_grammar

ROOT = Path(__file__).resolve().parents[1]
MASSIVE = ROOT / "src/proxy_index/providers/grammars/massive.toml"
CONTEXT = RequestContext("run-one", 0, 0)
SECRET = "private-password"


def massive(**changes):
    """Massive's grammar data with these top-level tables replaced."""
    data = tomllib.loads(MASSIVE.read_text())
    data.update(changes)
    return data


def with_grammar(monkeypatch, grammar: Grammar) -> None:
    """GrammarProxy("massive", ...) checks and renders through `grammar` instead."""
    monkeypatch.setattr(proxy_module, "load_grammar", lambda provider: grammar)


def test_provider_config_sections_must_be_tables(tmp_path):
    config = tmp_path / "provider.toml"
    config.write_text('schema_version = 1\nprovider = "massive"\nparameters = "us"\n')
    with pytest.raises(ValueError, match="sections must be tables"):
        load_provider(config, environ={})


def test_a_credential_path_that_is_no_regular_file_is_refused():
    with pytest.raises(ValueError, match="regular file"):
        read_credentials("/dev/null", "massive")


def flag(name, **spec):
    """Massive's flags with `name` replaced by `spec`."""
    return {"flags": massive()["flags"] | {name: spec}}


ROTATING = {"host": "ok.example", "ports": {"http": 1}, "rotating_ports": {"http": [9, 1]}}


@pytest.mark.parametrize(
    "change,match",
    [
        ({"gateway": ROTATING}, "inclusive \\[low, high\\]"),
        # A flag that needs a session says requires_session, not requires = ["session"].
        (flag("zip", kind="token", format="z-{}", requires=["session"]), "requires_session"),
        (flag("zip", kind="token", format="z-{}", case="title"), "invalid rendering"),
        (flag("asn", kind="integer", format="a-{}", scale=1.5), "invalid rendering"),
        (
            {"session": {"flag": "session", "mode": "port", "ports": [1, 2]}},
            "either a session flag or session ports",
        ),
        ({"session": {"flag": "type"}}, "must have kind session"),
    ],
)
def test_grammar_refusals(change, match):
    with pytest.raises(ValueError, match=match):
        Grammar.parse(massive(**change))


def test_grammar_names_are_slugs_and_match_their_file(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="Unknown provider grammar"):
        load_grammar("Not_A_Slug")
    (tmp_path / "grammars").mkdir()
    (tmp_path / "grammars" / "renamed.toml").write_text(MASSIVE.read_text())
    files = types.SimpleNamespace(files=lambda package: tmp_path)
    monkeypatch.setattr(grammar_module, "resources", files)
    with pytest.raises(ValueError, match="does not match its file name"):
        load_grammar("renamed")


def test_choice_flags_accept_only_their_choices():
    mobile = GrammarProxy("massive", "fixture1", SECRET, parameters={"type": "mobile"})
    assert mobile.grammar is load_grammar("massive")
    assert mobile.resolve(CONTEXT).username == "fixture1-type-mobile"
    with pytest.raises(ValueError, match="Unsupported Massive type value"):
        GrammarProxy("massive", "fixture1", SECRET, parameters={"type": "satellite"})


def test_gateway_host_and_port_rules():
    host = load_grammar("massive").hosts["default"]
    named = GrammarProxy("massive", "fixture1", SECRET, gateway="default", host=host)
    assert (named.host, named.port) == (host, 65534)
    with pytest.raises(ValueError, match="either a named Massive gateway or a host"):
        GrammarProxy("massive", "fixture1", SECRET, gateway="default", host="other.example")
    with pytest.raises(ValueError, match="fixed gateway ports"):
        GrammarProxy("massive", "fixture1", SECRET, port=8080)


def test_sessions_need_a_generator_and_a_session_flag(monkeypatch):
    session = massive()["session"]
    del session["generate"]
    with_grammar(monkeypatch, Grammar.parse(massive(session=session)))
    fixed = GrammarProxy("massive", "fixture1", SECRET, parameters={"session": "abc"})
    assert fixed.resolve(CONTEXT).username == "fixture1-session-abc"
    with pytest.raises(ValueError, match="cannot be generated; supply a session"):
        GrammarProxy("massive", "fixture1", SECRET, session_policy="per_run")
    # A grammar without a session flag offers rotation only.
    with_grammar(monkeypatch, dataclasses.replace(load_grammar("massive"), session=None))
    with pytest.raises(ValueError, match="does not support sessions"):
        GrammarProxy("massive", "fixture1", SECRET, session_policy="per_run")


def test_preset_flags_repeat_the_provider_side_value_and_are_never_rendered(monkeypatch):
    flags = massive()["flags"]
    flags["country"] = {"kind": "country", "case": "upper", "preset": True}
    with_grammar(monkeypatch, Grammar.parse(massive(flags=flags)))
    listed = GrammarProxy(
        "massive", "fixture1", SECRET, parameters={"region": "NY"}, presets={"country": "us"}
    )
    # The preset satisfies region's `requires`, and only region reaches the username.
    assert listed.resolve(CONTEXT).username == "fixture1-subdivision-NY"
    assert dict(listed.presets) == {"country": "US"}
    same = GrammarProxy(
        "massive", "fixture1", SECRET, parameters={"country": "us"}, presets={"country": "US"}
    )
    assert same.resolve(CONTEXT).username == "fixture1"
    with pytest.raises(ValueError, match="set on the provider's side"):
        GrammarProxy("massive", "fixture1", SECRET, parameters={"country": "us"})
    with pytest.raises(ValueError, match="differs from the provider-side preset"):
        GrammarProxy(
            "massive", "fixture1", SECRET, parameters={"country": "us"}, presets={"country": "de"}
        )
