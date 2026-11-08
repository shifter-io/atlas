"""Browser pulse contracts: the try record and the manifest of a run's bundle."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from browser_contracts_cases import RUN_ID, SHA, SLOT, try_fields

from proxy_index.browser import chrome
from proxy_index.browser import contracts as c
from proxy_index.browser.settings import load_settings


def test_try_record():
    record = c.try_record(**try_fields())
    assert record["final_url"] == "https://www.example.invalid:8443/dp/0306406152"
    assert c.page_url("http://Example.com") == "http://example.com/"
    assert c.page_url("https://[2001:db8::1]/a?b") == "https://[2001:db8::1]/a"
    failed = try_fields(
        verdict="error",
        error="login_wall",
        status=None,
        final_url=None,
        screenshot="browser/screenshots/0003.jpg",
        timings_ms=dict.fromkeys(c.TIMINGS),
    )
    assert c.try_record(**failed)["screenshot"] == "browser/screenshots/0003.jpg"
    assert c.try_record(**try_fields(verdict="proxy_error", error="dns"))["error"] == "dns"
    # LinkedIn's block answers 999 (K-BP2: block): its record is kept, so the run is not lost.
    blocked = try_fields(verdict="block", error="blocked", status=999, reason="block_status")
    assert c.try_record(**blocked)["status"] == 999


def test_try_record_keeps_a_chrome_fingerprint():
    fingerprint = chrome.fingerprint("run:0", "US").record()
    assert c.try_record(**try_fields(fingerprint=fingerprint))["fingerprint"] == fingerprint
    assert "fingerprint" not in c.try_record(**try_fields())
    assert set(c.FINGERPRINT_FIELDS) == set(fingerprint["values"])
    values = fingerprint["values"]
    for value, message in [
        (fingerprint | {"sha256": "0" * 64}, "fingerprint.sha256"),
        ({"values": values}, r"fingerprint: missing fields \['sha256'\]"),
        (fingerprint | {"values": {**values, "platform": "Win32"}}, "fingerprint.values: unknown"),
        (fingerprint | {"values": {**values, "locale": ""}}, "fingerprint.values.locale"),
        (fingerprint | {"values": {**values, "device_memory": -8}}, "values.device_memory"),
    ]:
        with pytest.raises(ValueError, match=message):
            c.try_record(**try_fields(fingerprint=value))


def test_try_record_takes_every_run_target_input_from_the_settings():
    """One try per run target, with each input exactly as the example settings give it."""
    settings = load_settings(
        Path(__file__).resolve().parents[1] / "configs/browser-pulse.example.toml"
    )
    assert {target.id for target in settings.targets} == set(c.RUN_TARGETS)
    for target in settings.targets:
        for value in target.inputs:
            record = c.try_record(**try_fields(input=value, source=target.id))
            # A table input is recorded as a plain object, so the line is JSON.
            assert json.loads(json.dumps(record))["input"] == (
                value if isinstance(value, str) else dict(value)
            )
    long_url = {"url": "https://example.invalid/" + "a" * 2000, "expect": "Example"}
    assert c.try_record(**try_fields(input=long_url))["input"] == long_url
    for value, message in [
        ({"site": "x"}, r"input: missing fields \['handle'\]"),
        ({"site": "x", "handle": "a", "extra": "b"}, "input: unknown fields"),
        ({"url": "https://example.invalid/", "expect": 1}, "input.expect"),
        ({"site": "x", "handle": "a" * 2049}, "input.handle"),
        ({"cookies": "x"}, "must never hold"),
        (["0306406152"], "input: must be"),
        ("a" * 2049, "input: must be"),
    ]:
        with pytest.raises(ValueError, match=message):
            c.try_record(**try_fields(input=value))


@pytest.mark.parametrize("field", sorted(c.FORBIDDEN_FIELDS))
def test_try_record_refuses_forbidden_fields(field):
    with pytest.raises(ValueError, match="must never hold"):
        c.try_record(**try_fields(**{field: "x"}))


def test_try_record_refusals():
    fields = try_fields()
    del fields["reason"]
    for bad, message in [
        (fields, "missing fields"),
        (try_fields(extra=1), "unknown fields"),
        (try_fields(verdict="error"), "verdict"),
        (try_fields(error="login_wall"), "verdict"),
        (try_fields(verdict="error", error="nonsense"), "verdict"),
        (try_fields(verdict="error", error=["dns"]), "error"),
        (try_fields(screenshot="browser/screenshots/0004.jpg"), "screenshot"),
        (try_fields(i=10_000), "i:"),
        (try_fields(status=99), "status"),
        (try_fields(status=1000), "status"),
        (try_fields(final_url="ftp://example.com/"), "final_url"),
        (try_fields(timings_ms={"total": 1}), "timings_ms"),
        (try_fields(timings_ms=try_fields()["timings_ms"] | {"total": -1}), "timings_ms.total"),
        (try_fields(reason="Has Spaces"), "reason"),
        (try_fields(input=""), "input"),
    ]:
        with pytest.raises(ValueError, match=message):
            c.try_record(**bad)


def manifest_fields(**changes):
    return {
        "run_id": RUN_ID,
        "batch_id": 41,
        "network": {"provider": "shifter", "product": "residential"},
        "target": "amazon_pdp",
        "country": "US",
        "requested_country": "US",
        "geo_targeting": True,
        "slot": SLOT,
        "started_at": SLOT + timedelta(seconds=61),
        "finished_at": "2026-11-04T13:40:00Z",
        "tries": 3,
        "outcomes": {
            "verdicts": {"ok": 1, "challenge": 1, "block": 0, "timeout": 0}
            | {"proxy_error": 0, "error": 1},
            "errors": dict.fromkeys(c.CODE_VERDICTS, 0) | {"challenge": 1, "no_content": 1},
        },
        "settings_sha256": SHA,
        "camoufox": {
            "release": "135.0.1-beta.24",
            "engine_version": "135.0.1",
            "user_agent": c.user_agent("135.0.1", "macos"),
        },
        "host": "browser-1",
        "files": [
            {"name": "browser/tries.jsonl", "bytes": 900, "sha256": SHA},
            {"name": "browser/screenshots/0001.jpg", "bytes": 4000, "sha256": "b" * 64},
        ],
    } | changes


def test_manifest():
    doc = c.manifest(**manifest_fields())
    assert (doc["format"], doc["version"]) == ("proxy-index-browser-raw", 1)
    assert doc["slot"] == "2026-11-04T13:00:00+00:00" and doc["started_at"].endswith("+00:00")
    assert c.check_manifest(doc) == doc
    # An untargeted network (D11) requests no country and says so.
    untargeted = c.manifest(**manifest_fields(requested_country=None, geo_targeting=False))
    assert untargeted["requested_country"] is None and untargeted["geo_targeting"] is False


def test_manifest_refusals():
    errors = manifest_fields()["outcomes"]["errors"]
    for changes, message in [
        ({"requested_country": None}, "requested_country"),
        ({"geo_targeting": False}, "requested_country"),
        ({"requested_country": "DE"}, "requested_country"),
        ({"geo_targeting": 1}, "geo_targeting"),
        ({"requested_country": "us"}, "requested_country"),
        ({"run_id": "x"}, "run_id"),
        ({"batch_id": 0}, "batch_id"),
        ({"network": {"provider": "a/b", "product": "c"}}, "network"),
        ({"network": {"provider": "a"}}, "missing fields"),
        ({"target": "ai_answers"}, "target"),
        ({"country": "usa"}, "country"),
        ({"host": "studio-host"}, "host"),
        ({"slot": datetime(2026, 11, 4, 13, 0)}, "slot"),
        ({"slot": "2026-11-04T13:00:30Z"}, "whole minute"),
        ({"started_at": "2026-11-04T17:00:00+04:00"}, "UTC"),
        ({"started_at": "yesterday"}, "ISO 8601"),
        ({"finished_at": "2026-11-04T13:00:00Z"}, "after"),
        ({"tries": 4}, "add up to tries"),
        ({"outcomes": {"verdicts": {}, "errors": errors}}, "missing fields"),
        (
            {"outcomes": manifest_fields()["outcomes"] | {"errors": errors | {"no_content": 0}}},
            "each failed verdict",
        ),
        ({"settings_sha256": "A" * 64}, "settings_sha256"),
        (
            {
                "camoufox": {
                    "release": "x",
                    "engine_version": "136.0",
                    "user_agent": "Firefox/135.0",
                }
            },
            "major",
        ),
        ({"files": []}, "tries.jsonl"),
        ({"files": "x"}, "files"),
        ({"files": [{"name": "browser/manifest.json", "bytes": 1, "sha256": SHA}]}, "manifest"),
        ({"files": [{"name": "summary.json", "bytes": 1, "sha256": SHA}]}, "member"),
        ({"text": "page text"}, "must never hold"),
        ({"cookies": "x"}, "must never hold"),
    ]:
        with pytest.raises(ValueError, match=message):
            c.manifest(**manifest_fields(**changes))
    tries = {"name": "browser/tries.jsonl", "bytes": 1, "sha256": SHA}
    with pytest.raises(ValueError, match="once"):
        c.manifest(**manifest_fields(files=[tries, tries]))
    for document in (
        manifest_fields() | {"format": "other", "version": 1},
        manifest_fields() | {"format": c.MANIFEST_FORMAT, "version": True},
        manifest_fields() | {"format": c.MANIFEST_FORMAT, "version": 2},
    ):
        with pytest.raises(ValueError, match="version 1"):
            c.check_manifest(document)
    with pytest.raises(ValueError, match="table or object"):
        c.check_manifest([])
