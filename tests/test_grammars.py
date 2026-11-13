"""Every grammar against its golden case: credentials, sessions, metadata, rotation, countries
and the public configs.

Provider-specific rules are in `test_grammar_providers.py`, the grammar schema and its testing
table in `test_grammar_schema.py`; the golden table is in `grammars_cases.py`.
"""

import json
import pickle
import re
import tomllib

import pytest
from grammars_cases import CONTEXT, GOLDEN, ROOT, SECRET, proxy

from atlas import RequestContext, RunConfig, load_provider
from atlas.providers import NoCountryList, registry
from atlas.providers.grammar import grammar_ids, load_grammar
from atlas.regions import load_countries
from atlas.storage import ADAPTER


def test_every_grammar_has_a_golden_case_config_and_registry_entry():
    assert set(GOLDEN) == set(grammar_ids()) == set(registry.all())
    for provider in grammar_ids():
        data = tomllib.loads((ROOT / f"configs/{provider}.toml").read_text())
        assert data["provider"] == provider and "account" not in json.dumps(data["settings"])
        grammar = load_grammar(provider)
        assert ("account_env" in data["credentials"]) == grammar.account_required


@pytest.mark.parametrize("provider", sorted(GOLDEN))
def test_golden_credentials(provider):
    params, endpoint, username, password = GOLDEN[provider]
    resolved = proxy(provider, parameters=params).resolve(CONTEXT)
    assert (f"{resolved.host}:{resolved.port}", resolved.username, resolved.password) == (
        endpoint,
        username,
        password,
    )


@pytest.mark.parametrize("provider", sorted(GOLDEN))
def test_generated_sessions_are_valid_scoped_and_pickle_safe(provider):
    params = {k: v for k, v in GOLDEN[provider][0].items() if k != "session"}
    p = proxy(provider, parameters=params, session_policy="per_request")
    restored = pickle.loads(pickle.dumps(p))
    configs = [restored.resolve(RequestContext("one", i, 0)) for i in range(8)]
    assert len({(c.username, c.password, c.port) for c in configs}) == 8
    assert restored.resolve(RequestContext("one", 3, 0)) == p.resolve(RequestContext("one", 3, 0))
    if load_grammar(provider).session_mode != "port":  # A port is taken by request index.
        assert restored.resolve(RequestContext("two", 0, 0)) != configs[0]
    assert p.public() == restored.public()
    worker = proxy(provider, parameters=params, session_policy="per_worker")
    assert len({worker.resolve(RequestContext("one", i, i % 2)) for i in range(6)}) == 2


@pytest.mark.parametrize("provider", sorted(GOLDEN))
def test_public_metadata_matches_storage_and_redacts(provider):
    p = proxy(provider, parameters=GOLDEN[provider][0])
    public = RunConfig("generic_http", p).public()
    adapter = public["provider_config"]
    assert set(adapter) == {*ADAPTER, "gateway"} and set(adapter["gateway"]) == {"host", "port"}
    assert adapter["adapter"] == provider and public["provider"] == provider
    ttl = adapter["effective_ttl_seconds"]
    assert adapter["session_policy"] == "fixed" and (ttl is None or ttl > 0)
    text = json.dumps(public) + repr(p) + repr(p.resolve(CONTEXT))
    for private in ("fixture1", SECRET, "abc123", "abcd1234", "12345678", "abc_123"):
        assert private not in text


@pytest.mark.parametrize("provider", sorted(GOLDEN))
def test_rotation_never_sends_a_session(provider):
    params = {k: v for k, v in GOLDEN[provider][0].items() if k not in ("session", "ttl")}
    generated = load_grammar(provider).no_session_policy not in ("rotate", "gateway")
    p = proxy(provider, parameters=params, session_policy="gateway" if generated else None)
    first = p.resolve(RequestContext("one", 0, 0))
    assert all(p.resolve(RequestContext("one", i, 0)) == first for i in range(4))
    key = load_grammar(provider).flags.get("session")
    if key is not None and key.format != "{}":
        rendered = key.format.split("{}")[0]
        assert rendered not in f"{first.username} {first.password}"


@pytest.mark.parametrize("provider", sorted(GOLDEN))
def test_every_country_code_loads_even_when_it_equals_a_flag_key(provider):
    # Oxylabs' cc/st and DataImpulse's cr are also ISO codes (Cocos Islands, Sao Tome, Costa
    # Rica); a census round refused its whole region over one such country.
    if "country" not in load_grammar(provider).flags:
        pytest.skip("no country flag")
    for code in load_countries():
        try:
            proxy(provider, parameters={"country": code.lower()})
        except NoCountryList:
            continue


def test_country_codes_equal_to_a_flag_key_render_in_the_country_slot():
    for code, username in (("st", "customer-fixture1-cc-ST"), ("cc", "customer-fixture1-cc-CC")):
        assert proxy("oxylabs", parameters={"country": code}).resolve(CONTEXT).username == username
    assert proxy("dataimpulse", parameters={"country": "cr"}).resolve(CONTEXT).username == (
        "fixture1__cr.cr"
    )
    with pytest.raises(ValueError, match="ambiguous flag delimiter"):  # Other kinds still refuse.
        proxy("oxylabs", parameters={"country": "us", "city": "st"})


@pytest.mark.parametrize("provider", sorted(GOLDEN))
def test_run_configs_load_with_private_credentials(provider, tmp_path):
    grammar = load_grammar(provider)
    table = {"password": SECRET} | ({"account": "fixture1"} if grammar.account_required else {})
    path = tmp_path / "credentials.toml"
    lines = [f'["{provider}"]'] + [f'{key} = "{value}"' for key, value in table.items()]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    config = ROOT / f"configs/{provider}.toml"
    loaded = load_provider(config, credentials_file=path)
    assert loaded.id == provider
    # Every commented example in the public config must validate once uncommented.
    examples = {}
    section = None
    for line in config.read_text().splitlines():
        if line.startswith("["):
            section = line
        elif section == "[parameters]" and (match := re.match(r'# (\w+) = ("[^"]*"|\S+)', line)):
            examples[match[1]] = tomllib.loads(f"v = {match[2]}")["v"]
    for key, value in examples.items():
        params = {"country": "us"} if "country" in grammar.flags else {}
        for needed in grammar.flags[key].requires if key in grammar.flags else ():
            params[needed] = examples[needed]
        params[key] = value
        if key == grammar.ttl or (key in grammar.flags and grammar.flags[key].requires_session):
            load_provider(
                config, credentials_file=path, parameters=params, session_policy="per_request"
            )
        else:
            load_provider(config, credentials_file=path, parameters=params)


def test_requested_country_is_canonical_lowercase():
    for provider in ("massive", "oxylabs", "evomi", "infatica", "rayobyte", "proxy-cheap"):
        p = proxy(provider, parameters={"country": "us"})
        assert RunConfig("generic_http", p, requested_country="us").requested_country == "us"
