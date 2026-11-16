"""The browser pulse settings file (K-BP3): the example loads and every rule refuses bad input."""

import hashlib
import json
import re
import tomllib
from datetime import datetime
from pathlib import Path

import pytest

from atlas.browser.chrome import DEFAULT_EXECUTABLE
from atlas.browser.settings import load_settings, settings_sha256
from atlas.errors import UserError

EXAMPLE = Path(__file__).resolve().parents[1] / "configs" / "browser-pulse.example.toml"


def test_example_loads():
    settings = load_settings(EXAMPLE)
    assert settings.schema == 2 and settings.enabled is False
    assert settings.schedule.countries == ("US",) and settings.schedule.lead_seconds == 60
    assert settings.browser.browsers_per_host == 10
    assert settings.browser.block_resources == ("image", "media", "font")
    assert settings.outbox.dir == "outbox" and settings.outbox.max_pending_gb == 20.0
    assert settings.chrome.executable == DEFAULT_EXECUTABLE
    assert settings.targets_for_planner() == (
        ("amazon_pdp", 100, 60),
        ("google_search", 100, 60),
        ("linkedin_public", 100, 60),
        ("instagram_web", 100, 60),
        ("x_web", 100, 60),
        ("cloudflare_challenge", 100, 60),
        ("ai_overview", 34, 60),
        ("ai_chatgpt", 33, 60),
        ("ai_gemini", 33, 60),
    )
    social = settings.targets[3].inputs
    assert [dict(item) for item in social] == [{"site": "instagram", "handle": "example"}]
    assert settings.targets[4].inputs == ("example",)
    gemini = settings.targets[-1]
    assert (gemini.timeout_s, gemini.answer_wait_s, gemini.block_resources) == (60, 60, None)
    assert settings.targets[-2].block_resources == ()
    assert [host.name for host in settings.hosts] == ["browser-1", "browser-2", "browser-3"]
    assert settings.sha256 == settings_sha256(EXAMPLE)
    assert settings.sha256 == hashlib.sha256(EXAMPLE.read_bytes()).hexdigest()


def test_host_view_and_untargeted():
    settings = load_settings(EXAMPLE)
    assert settings.is_untargeted("example-untargeted/residential")
    assert not settings.is_untargeted("example-a/residential")
    third = settings.host("browser-3")
    assert [host.name for host in third.hosts] == ["browser-3"]
    assert third.untargeted == ("example-untargeted/residential",)
    assert third.targets == settings.targets and third.browser == settings.browser
    first = settings.host("browser-1")
    assert first.hosts[0].networks == ("example-a/residential", "example-b/residential")
    assert first.untargeted == () and not first.is_untargeted("example-untargeted/residential")
    with pytest.raises(UserError, match="^host: "):
        settings.host("browser-9")


def document():
    return tomllib.loads(EXAMPLE.read_text())


def target(doc, target_id):
    return next(entry for entry in doc["target"] if entry["id"] == target_id)


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(map(toml_value, value)) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k)} = {toml_value(v)}" for k, v in value.items()) + "}"
    if isinstance(value, datetime):  # a TOML date-time: with its offset, or local
        return value.isoformat()
    # JSON strings and numbers are valid TOML.
    return json.dumps(value)


def write(tmp_path, doc):
    """The document as TOML: plain keys first, then tables and arrays of tables."""
    tables = {k: v for k, v in doc.items() if isinstance(v, dict)}
    arrays = {k: v for k, v in doc.items() if v and isinstance(v, list) and isinstance(v[0], dict)}
    lines = [f"{k} = {toml_value(v)}" for k, v in doc.items() if k not in tables | arrays]
    for key, value in tables.items():
        lines += [f"[{key}]", *(f"{json.dumps(k)} = {toml_value(v)}" for k, v in value.items())]
    for key, entries in arrays.items():
        for entry in entries:
            lines += [
                f"[[{key}]]",
                *(f"{json.dumps(k)} = {toml_value(v)}" for k, v in entry.items()),
            ]
    path = tmp_path / "settings.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


SECRET = "secret-profile-value"


def mutate(doc, change):
    change(doc)
    return doc


CASES = [
    # Per-target tries and interval: required and bounded.
    (lambda d: target(d, "amazon_pdp").pop("tries"), r"target\[0\]: missing fields \['tries'\]"),
    (lambda d: target(d, "google_search").pop("every_minutes"), r"target\[1\]: missing fields"),
    (lambda d: target(d, "amazon_pdp").update(tries=0), r"target\[0\]\.tries: .* 1 to 10000"),
    (lambda d: target(d, "amazon_pdp").update(tries=10_001), r"target\[0\]\.tries"),
    (lambda d: target(d, "amazon_pdp").update(tries=True), r"target\[0\]\.tries"),
    (lambda d: target(d, "ai_gemini").update(every_minutes=4), r"target\[8\]\.every_minutes"),
    (lambda d: target(d, "ai_gemini").update(every_minutes=1441), r"target\[8\]\.every_minutes"),
    # Networks: one host each; the untargeted list names only networks a host runs.
    (
        lambda d: d["host"][1]["networks"].append("example-a/residential"),
        r"host\[1\]\.networks\[2\]: is listed twice",
    ),
    (
        lambda d: d["host"][0]["networks"].append("example-a/residential"),
        r"host\[0\]\.networks\[2\]: is listed twice",
    ),
    (
        lambda d: d["host"][0]["networks"].append(f"{SECRET}/Residential"),
        r"host\[0\]\.networks\[2\]: must be provider/product",
    ),
    (
        lambda d: d["networks"].update(untargeted=["example-z/residential"]),
        r"networks\.untargeted\[0\]: is a network no host runs",
    ),
    (
        lambda d: d["networks"].update(untargeted=["example-untargeted/residential"] * 2),
        r"networks\.untargeted\[1\]: is listed twice",
    ),
    (lambda d: d["networks"].update(other=[]), r"networks: unknown fields \['other'\]"),
    (lambda d: d["host"][1].update(name="browser-1"), r"host\[1\]\.name: is used by another"),
    (lambda d: d["host"][1].update(name="studio-host"), r"host\[1\]\.name: must be browser-<n>"),
    # Targets must be known browser run targets, once each.
    (lambda d: target(d, "amazon_pdp").update(id="generic_http"), r"target\[0\]\.id: is not"),
    (lambda d: target(d, "amazon_pdp").update(id="ai_answers"), r"target\[0\]\.id: is not"),
    (lambda d: target(d, "amazon_pdp").update(id="unknown_site"), r"target\[0\]\.id: is not"),
    (lambda d: target(d, "amazon_pdp").update(id=["x"]), r"target\[0\]\.id: is not"),
    (lambda d: d["target"].append(dict(d["target"][0])), r"target\[9\]: is listed twice"),
    # Input shapes.
    (
        lambda d: target(d, "amazon_pdp").update(inputs=["b00example"]),
        r"inputs\[0\]: must be an ASIN",
    ),
    (lambda d: target(d, "amazon_pdp").update(inputs=[]), r"target\[0\]\.inputs: must be a list"),
    (lambda d: target(d, "amazon_pdp").update(inputs="0306406152"), r"target\[0\]\.inputs"),
    (
        lambda d: target(d, "linkedin_public").update(inputs=["Example_Profile"]),
        r"target\[2\]\.inputs\[0\]: must be a profile slug",
    ),
    (lambda d: target(d, "linkedin_public").update(inputs=["ab"]), r"target\[2\]\.inputs\[0\]"),
    (
        lambda d: target(d, "instagram_web").update(inputs=[{"site": "tiktok", "handle": "x"}]),
        r"target\[3\]\.inputs\[0\]\.site: must be one of",
    ),
    (
        lambda d: target(d, "instagram_web").update(inputs=[{"site": "x", "handle": "example"}]),
        r"target\[3\]\.inputs\[0\]\.site: must be one of \['instagram'\]",
    ),
    (
        lambda d: target(d, "instagram_web").update(
            inputs=[{"site": "instagram", "handle": "a b"}]
        ),
        r"target\[3\]\.inputs\[0\]\.handle",
    ),
    (
        lambda d: target(d, "x_web").update(inputs=["a.b"]),
        r"target\[4\]\.inputs\[0\]: must be an X handle",
    ),
    (lambda d: target(d, "x_web").update(inputs=["a" * 16]), r"target\[4\]\.inputs\[0\]"),
    (
        lambda d: target(d, "x_web").update(inputs=[{"site": "x", "handle": "example"}]),
        r"target\[4\]\.inputs\[0\]",
    ),
    (
        lambda d: target(d, "instagram_web").update(inputs=["example"]),
        r"target\[3\]\.inputs\[0\]: must be a table",
    ),
    (
        lambda d: target(d, "cloudflare_challenge").update(
            inputs=[{"url": "http://example.com/", "expect": "x"}]
        ),
        r"target\[5\]\.inputs\[0\]\.url: must be an https URL",
    ),
    (
        lambda d: target(d, "cloudflare_challenge").update(
            inputs=[{"url": f"https://{SECRET}:pw@example.invalid/", "expect": "x"}]
        ),
        r"target\[5\]\.inputs\[0\]\.url",
    ),
    (
        lambda d: target(d, "cloudflare_challenge").update(
            inputs=[{"url": "https://example.com/"}]
        ),
        r"target\[5\]\.inputs\[0\]: missing fields \['expect'\]",
    ),
    (
        lambda d: target(d, "ai_chatgpt").update(inputs=["one", "two"]),
        r"target\[7\]\.inputs: must be a list of 1 to 1",
    ),
    (
        lambda d: target(d, "ai_gemini").update(inputs=["one", "two"]),
        r"target\[8\]\.inputs: must be a list of 1 to 1",
    ),
    (lambda d: target(d, "google_search").update(inputs=["   "]), r"target\[1\]\.inputs\[0\]"),
    # Optional per-target fields.
    (lambda d: target(d, "ai_gemini").update(timeout_s=1), r"target\[8\]\.timeout_s"),
    (
        lambda d: target(d, "ai_chatgpt").update(block_resources=["document"]),
        r"block_resources\[0\]",
    ),
    (
        lambda d: target(d, "ai_chatgpt").update(budget=1),
        r"target\[7\]: unknown fields \['budget'\]",
    ),
    # Host-side settings.
    (lambda d: d["browser"].update(browsers_per_host=0), r"browser\.browsers_per_host: .* 1 to 16"),
    (lambda d: d["browser"].update(browsers_per_host=17), r"browser\.browsers_per_host"),
    (lambda d: d["browser"].update(headless="yes"), r"browser\.headless: must be one of"),
    (lambda d: d["browser"].update(screenshots="some"), r"browser\.screenshots: must be one of"),
    (
        lambda d: d["browser"].update(block_resources=["font", "font"]),
        r"block_resources\[1\]: is listed",
    ),
    (lambda d: d["browser"].update(locale="english"), r"browser\.locale"),
    (lambda d: d["browser"].pop("recycle_after"), r"browser: missing fields \['recycle_after'\]"),
    (lambda d: d["outbox"].update(dir="/var/outbox"), r"outbox\.dir: must be a relative path"),
    (lambda d: d["outbox"].update(dir="../outbox"), r"outbox\.dir"),
    (lambda d: d["outbox"].update(max_pending_gb=0), r"outbox\.max_pending_gb"),
    (lambda d: d["outbox"].update(max_pending_gb=True), r"outbox\.max_pending_gb"),
    # Orchestrator settings and the file itself.
    (
        lambda d: d["schedule"].update(countries=["XX"]),
        r"schedule\.countries\[0\]: must be a known",
    ),
    (lambda d: d["schedule"].update(countries=["us"]), r"schedule\.countries\[0\]"),
    (lambda d: d["schedule"].update(countries=["US", "US"]), r"schedule\.countries\[1\]"),
    (lambda d: d["schedule"].update(lead_seconds=-1), r"schedule\.lead_seconds"),
    (lambda d: d.update(schema=1), r"^schema: must be 2$"),
    (lambda d: d.update(schema="2"), r"^schema: must be 2$"),
    (lambda d: d.update(enabled=1), r"^enabled: must be one of"),
    (lambda d: d.update(tries=100), r"^settings: unknown fields \['tries'\]"),
    (lambda d: d.update(every_minutes=60), r"^settings: unknown fields"),
    (lambda d: d.pop("host"), r"^settings: missing fields \['host'\]"),
    (lambda d: d.update(target=[]), r"^target: must be a list"),
    (lambda d: d.update(chrome={"executable": "Google Chrome"}), r"chrome\.executable: .*absolute"),
    (lambda d: d.update(chrome={"path": "/x"}), r"^chrome: unknown fields \['path'\]"),
    # The engine: Camoufox or Chrome, for every target or one; nothing else.
    (lambda d: d["browser"].update(engine="firefox"), r"^browser\.engine: must be one of"),
    (lambda d: d["browser"].update(engine=True), r"^browser\.engine: must be one of"),
    (lambda d: target(d, "amazon_pdp").update(engine="edge"), r"^target\[0\]\.engine: must be"),
    (lambda d: target(d, "amazon_pdp").update(engine=""), r"^target\[0\]\.engine: must be"),
]


@pytest.mark.parametrize("change, message", CASES)
def test_each_rule_refuses_with_its_field_path(tmp_path, change, message):
    path = write(tmp_path, mutate(document(), change))
    with pytest.raises(UserError, match=message) as refused:
        load_settings(path)
    # Errors name the field, never the value.
    assert SECRET not in str(refused.value) and "b00example" not in str(refused.value)


def test_the_file_must_be_readable_toml(tmp_path):
    with pytest.raises(UserError, match="cannot be read"):
        load_settings(tmp_path / "missing.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text(f'schema = 2\nsecret = "{SECRET}\n')
    with pytest.raises(UserError, match="not valid UTF-8 TOML") as refused:
        load_settings(bad)
    assert SECRET not in str(refused.value)
    bad.write_bytes(b"schema = 2\n\xff\n")
    with pytest.raises(UserError, match="not valid UTF-8 TOML"):
        load_settings(bad)


def test_targets_must_be_browser_rows_of_the_targets_file(tmp_path):
    # A targets file without the browser rows makes every settings target unknown.
    targets = tmp_path / "targets.toml"
    targets.write_text('[[target]]\nid = "amazon_pdp"\nlabel = "A"\nurl = "https://x/"\n')
    with pytest.raises(UserError, match=re.escape("target[0].id: is not a browser run target")):
        load_settings(EXAMPLE, targets_path=targets)


def test_minimal_settings(tmp_path):
    doc = document()
    doc["target"] = [target(doc, "ai_chatgpt")]
    doc["host"] = [{"name": "browser-1", "networks": ["example-a/residential"]}]
    del doc["networks"]
    doc.pop("chrome", None)
    settings = load_settings(write(tmp_path, doc))
    assert settings.chrome.executable == DEFAULT_EXECUTABLE
    assert settings.targets_for_planner() == (("ai_chatgpt", 33, 60),)
    assert settings.untargeted == () and not settings.is_untargeted("example-a/residential")


def test_the_engine_defaults_to_camoufox_and_a_target_overrides_it(tmp_path):
    settings = load_settings(EXAMPLE)
    assert settings.browser.engine == "camoufox" and settings.engines() == ("camoufox",)
    assert {settings.engine(t.id) for t in settings.targets} == {"camoufox"}
    doc = document()
    target(doc, "amazon_pdp")["engine"] = "chrome"
    settings = load_settings(write(tmp_path, doc))
    assert settings.engine("amazon_pdp") == "chrome" and settings.engine("ai_chatgpt") == "camoufox"
    assert settings.engines() == ("camoufox", "chrome")
    doc["browser"]["engine"] = "chrome"
    target(doc, "ai_chatgpt")["engine"] = "camoufox"
    settings = load_settings(write(tmp_path, doc))
    assert settings.browser.engine == "chrome" and settings.engine("ai_chatgpt") == "camoufox"
    assert settings.engine("google_search") == "chrome"
    assert settings.host("browser-1").engine("amazon_pdp") == "chrome"
