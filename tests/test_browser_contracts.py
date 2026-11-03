"""Browser pulse contracts: names, outcomes, records, the ingest rule and the target protocol."""

import asyncio
import typing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from proxy_index.browser import contracts as c
from proxy_index.models import ErrorCode, RequestContext
from proxy_index.orchestrator import batches
from proxy_index.orchestrator.db import Database
from proxy_index.orchestrator.nodes import issue_token
from proxy_index.outcome import from_error
from proxy_index.regions import load_targets
from proxy_index.storage import connect, seed
from proxy_index.targets import Assessment, BrowserTarget, PageSpec, PageView, registry

pytest_plugins = ["test_storage"]

SLOT = datetime(2026, 11, 4, 13, 0, tzinfo=UTC)
RUN_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
SHA = "a" * 64


def test_browser_error_codes_are_errors():
    codes = typing.get_args(ErrorCode)
    for code in ("login_wall", "no_content", "no_answer", "browser_error"):
        assert code in codes and from_error(code) == "error"


def test_outcome_table_matches_the_verdict_rules():
    # Every code of the K-BP2 table has the verdict the shared rules give it.
    for code, verdict in c.CODE_VERDICTS.items():
        assert code in typing.get_args(ErrorCode) and from_error(code) == verdict
    assert c.OUTCOMES["success"] == ("ok", (None,))
    assert set(c.CODE_VERDICTS) == {
        *("challenge", "blocked", "login_wall", "timeout", "proxy_connect", "proxy_auth"),
        *("connection", "dns", "no_content", "no_answer", "browser_error", "tls"),
    }


@pytest.mark.parametrize(
    "name, code, verdict",
    [
        ("NS_ERROR_PROXY_CONNECTION_REFUSED", "proxy_connect", "proxy_error"),
        ("NS_ERROR_PROXY_BAD_GATEWAY", "proxy_connect", "proxy_error"),
        ("NS_ERROR_UNKNOWN_PROXY_HOST", "proxy_connect", "proxy_error"),
        ("NS_ERROR_PROXY_AUTHENTICATION_FAILED", "proxy_auth", "proxy_error"),
        ("NS_ERROR_NET_RESET", "connection", "proxy_error"),
        ("NS_ERROR_NET_INTERRUPT", "connection", "proxy_error"),
        ("NS_ERROR_CONNECTION_REFUSED", "connection", "proxy_error"),
        ("NS_ERROR_UNKNOWN_HOST", "dns", "proxy_error"),
        ("NS_ERROR_NET_TIMEOUT", "timeout", "timeout"),
        ("TimeoutError", "timeout", "timeout"),
        ("SEC_ERROR_UNKNOWN_ISSUER", "tls", "error"),
        ("SSL_ERROR_BAD_CERT_DOMAIN", "tls", "error"),
        ("NS_ERROR_NET_INADEQUATE_SECURITY", "tls", "error"),
        ("NS_ERROR_FAILURE", "browser_error", "error"),
        ("", "browser_error", "error"),
        (None, "browser_error", "error"),
    ],
)
def test_navigation_error_map(name, code, verdict):
    assert c.classify_navigation_error(name) == (code, verdict)


def test_every_mapped_firefox_name():
    for name, code in c.FIREFOX_ERRORS.items():
        assert c.classify_navigation_error(name)[0] == code
    assert c.PROXY_AUTH_STATUS == 407


def test_target_ids_and_groups():
    assert c.BROWSER_TARGETS == (
        *("amazon_pdp", "google_search", "linkedin_public", "instagram_web"),
        *("cloudflare_challenge", "ai_answers", "ai_overview", "ai_chatgpt", "ai_gemini"),
    )
    assert c.TARGET_GROUPS == {"ai_answers": ("ai_overview", "ai_chatgpt", "ai_gemini")}
    assert c.AI_ANSWERS not in c.RUN_TARGETS and len(c.RUN_TARGETS) == 8
    assert (c.BROWSER_KIND, c.BROWSER_REGION, c.BROWSER_REGION_ID) == ("browser", "browser", 20)
    assert c.BROWSER_REGION_CLOUD == "local"


def test_network_slug():
    assert c.network_slug("Proxy-Cheap", "Residential") == "proxy-cheap.residential"
    assert c.network_slug("shifter", "isp_static") == "shifter.isp_static"
    for provider, product in [
        ("a/b", "c"),
        ("a b", "c"),
        ("..", "c"),
        ("a", "b.c"),
        ("", "c"),
        (None, "c"),
    ]:
        with pytest.raises(ValueError, match="network"):
            c.network_slug(provider, product)


def test_slot_stamp():
    assert c.slot_stamp(SLOT) == "2026-11-04T1300Z"
    dubai = timezone(timedelta(hours=4))
    assert c.slot_stamp(datetime(2026, 11, 4, 17, 5, tzinfo=dubai)) == "2026-11-04T1305Z"
    with pytest.raises(ValueError, match="timezone-aware"):
        c.slot_stamp(datetime(2026, 11, 4, 13, 0))
    with pytest.raises(ValueError, match="whole minute"):
        c.slot_stamp(SLOT.replace(second=1))
    with pytest.raises(ValueError, match="timezone-aware"):
        c.slot_stamp("2026-11-04T13:00Z")


def test_object_key_golden():
    key = c.object_key(c.ARCHIVE_PREFIX, "shifter.residential", "amazon_pdp", "US", SLOT)
    assert key == "browser-files/shifter.residential/amazon_pdp/us/2026-11-04T1300Z.tar.gz"
    untargeted = c.object_key(
        "browser-files-check", "proxy-cheap.residential", "ai_gemini", None, SLOT
    )
    assert untargeted == (
        "browser-files-check/proxy-cheap.residential/ai_gemini/untargeted/2026-11-04T1300Z.tar.gz"
    )
    assert c.object_key("a/b", "x.y", "ai_chatgpt", "de", SLOT).startswith("a/b/x.y/ai_chatgpt/de/")
    for args, message in [
        (("raw", "x.y", "amazon_pdp", "US", SLOT), "another archive"),
        (("raw-files/b", "x.y", "amazon_pdp", "US", SLOT), "another archive"),
        (("inputs", "x.y", "amazon_pdp", "US", SLOT), "another archive"),
        (("../b", "x.y", "amazon_pdp", "US", SLOT), "prefix"),
        (("/b", "x.y", "amazon_pdp", "US", SLOT), "prefix"),
        (("b", "x/y", "amazon_pdp", "US", SLOT), "network"),
        (("b", "x.y", "ai_answers", "US", SLOT), "target"),
        (("b", "x.y", "amazon_pdp", "USA", SLOT), "country"),
        (("b", "x.y", "amazon_pdp", "US", datetime(2026, 1, 1)), "slot"),
    ]:
        with pytest.raises(ValueError, match=message):
            c.object_key(*args)


def test_outbox_paths_and_bundle_members(tmp_path):
    bundle, sidecar = c.outbox_paths(tmp_path, RUN_ID)
    assert (bundle.name, sidecar.name) == (f"{RUN_ID}.tar.gz", f"{RUN_ID}.json")
    assert bundle.parent == tmp_path and c.outbox_paths("outbox", RUN_ID)[0] == Path(
        f"outbox/{RUN_ID}.tar.gz"
    )
    for bad in ("../x", RUN_ID.upper(), "", None):
        with pytest.raises(ValueError, match="run_id"):
            c.outbox_paths(tmp_path, bad)
    members = c.bundle_members()
    assert (members.manifest, members.tries) == ("browser/manifest.json", "browser/tries.jsonl")
    assert members.screenshot(7) == "browser/screenshots/0007.jpg"
    assert members.screenshot(9999) == "browser/screenshots/9999.jpg"
    assert members.is_member("browser/screenshots/0007.jpg") and members.is_member(members.tries)
    assert not members.is_member("browser/screenshots/7.jpg")
    assert not members.is_member("browser/../summary.json")
    for bad in (-1, 10_000, True, "1"):
        with pytest.raises(ValueError, match="i:"):
            members.screenshot(bad)


def try_fields(**changes):
    return {
        "i": 3,
        "input": "0306406152",
        "source": None,
        "verdict": "ok",
        "error": None,
        "status": 200,
        "final_url": "https://user:secret@www.example.invalid:8443/dp/0306406152?tag=x#reviews",
        "timings_ms": {"queue": 1, "context": 40, "navigate": 900, "settle": 500, "total": 1441},
        "bytes_down_est": 512_000,
        "reason": "content",
        "screenshot": None,
    } | changes


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


@pytest.mark.parametrize(
    "status, body, confirmed",
    [
        (200, {"ok": True, "ingested": True, "archived": {"key": "k", "sha256": SHA}}, True),
        (200, {"ok": True, "ingested": False}, True),
        (200, {"ingested": True, "archived": {"key": "k", "sha256": "b" * 64}}, False),
        (200, {"ingested": True, "archived": {"key": "k", "sha256": SHA.upper()}}, False),
        (200, {"ingested": True, "archived": {"key": "k"}}, False),
        (200, {"ingested": True, "archived": SHA}, False),
        (200, {"ingested": True}, False),
        (200, {"archived": {"sha256": SHA}}, False),
        (200, {"ingested": "false"}, False),
        (200, {"ingested": 0}, False),
        (200, None, False),
        (200, [SHA], False),
        (201, {"ingested": True, "archived": {"sha256": SHA}}, False),
        (409, {"error": "bundle_changed", "ingested": False}, False),
        (400, {"error": "checksum_mismatch"}, False),
        (429, {"ingested": False}, False),
        (503, {}, False),
        (True, {"ingested": False}, False),
    ],
)
def test_confirmed_is_the_only_delete_rule(status, body, confirmed):
    assert c.confirmed((status, body), SHA) is confirmed


def test_confirmed_needs_a_valid_local_checksum():
    body = {"ingested": True, "archived": {"sha256": "x"}}
    assert c.confirmed((200, body), "x") is False
    assert c.confirmed((200, {"ingested": True, "archived": {"sha256": None}}), None) is False


def test_ingest_names():
    assert (c.HEADER_SHA256, c.HEADER_BUNDLE_ID) == ("X-Content-SHA256", "X-Bundle-Id")
    assert (c.ERROR_CHECKSUM_MISMATCH, c.ERROR_BUNDLE_CHANGED) == (
        "checksum_mismatch",
        "bundle_changed",
    )
    assert (c.REPLY_INGESTED, c.REPLY_RUN_KEY, c.REPLY_ARCHIVED) == (
        "ingested",
        "run_key",
        "archived",
    )
    assert (c.REPLY_ARCHIVED_KEY, c.REPLY_ARCHIVED_SHA256) == ("key", "sha256")


def test_user_agent():
    assert c.user_agent("135.0.1", "macos") == (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:135.0) Gecko/20100101 Firefox/135.0"
    )
    assert c.user_agent("128.0", "linux") == (
        "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
    )
    for version in ("135", "135.0", "135.0.1", "135.0b9", "99.1.2"):
        major = c.engine_major(version)
        assert c.user_agent_major(c.user_agent(version, "macos")) == major
        assert f"Firefox/{major}.0" in c.user_agent(version, "linux")
    assert c.user_agent_major("Mozilla/5.0 Chrome/120.0") is None
    assert c.user_agent_major(None) is None
    for version, os, message in [
        ("x135", "macos", "engine_version"),
        ("", "macos", "engine_version"),
        ("135.0.1", "beos", "os"),
    ]:
        with pytest.raises(ValueError, match=message):
            c.user_agent(version, os)


class FixtureTarget(BrowserTarget):
    def page(self, context, inputs):
        return PageSpec(self.url + inputs[context.request_index % len(inputs)], "#title")

    def judge(self, view):
        return Assessment(verdict="ok" if view.found.get("title") else "error")


def test_browser_target_protocol():
    target = FixtureTarget("amazon_pdp", "https://example.com/dp/")
    assert (target.engine, target.pool_eligible, target.version) == ("browser", False, "1")
    assert target.build_request().url == "https://example.com/dp/"
    spec = target.page(RequestContext(RUN_ID, 1, 0), ("A", "B"))
    assert spec == PageSpec("https://example.com/dp/B", "#title", 0)
    assert asyncio.run(target.interact(None, spec)) is None
    view = PageView("https://example.com/dp/B", 200, "Title", "text", {"title": 1})
    assert target.judge(view).verdict == "ok" and view.timings_ms == {}
    with pytest.raises(TypeError, match="PageView"):
        target.assess(200, b"")
    base = BrowserTarget("x", "https://example.com/")
    with pytest.raises(NotImplementedError):
        base.page(RequestContext(RUN_ID, 0, 0), ("a",))
    with pytest.raises(NotImplementedError):
        base.judge(view)
    # No browser target is registered yet: the registry still holds the two echo targets.
    assert {t.id for t in registry.all()} == {"generic_http", "generic_https"}


def test_targets_file_has_the_browser_rows():
    rows = [row for row in load_targets() if row["engine"] == "browser"]
    assert tuple(row["target_id"] for row in rows) == c.BROWSER_TARGETS
    assert [row["sort_order"] for row in rows] == [30, 40, 50, 60, 70, 80, 81, 82, 83]
    assert not any(row["pulse_enabled"] or row["census"] for row in rows)
    assert all(row["label"] and row["pass_condition"] for row in rows)
    assert all(row["url"].startswith("https://") for row in rows)


@pytest.mark.parametrize("flag", ["pulse_enabled", "census"])
def test_load_targets_refuses_a_scheduled_browser_row(tmp_path, flag):
    path = tmp_path / "targets.toml"
    row = '[[target]]\nid = "x"\nlabel = "X"\nurl = "https://x/"\nengine = "browser"\n'
    path.write_text(row + "pulse_enabled = false\ncensus = false\n")
    assert load_targets(path)[0]["engine"] == "browser"
    path.write_text(
        row + ("pulse_enabled = false\n" if flag == "census" else "") + f"{flag} = true\n"
    )
    with pytest.raises(ValueError, match="browser target"):
        load_targets(path)


async def test_seed_and_pulse_plan_leave_browser_rows_unscheduled(database):
    seed(database)
    with connect(database) as conn:
        conn.execute("UPDATE countries SET enabled = country_code = 'DE'")
        rows = conn.execute(
            "SELECT target_id,engine,label IS NOT NULL,census,pulse_enabled FROM targets "
            "WHERE engine='browser' ORDER BY sort_order"
        ).fetchall()
    assert [row[0] for row in rows] == list(c.BROWSER_TARGETS)
    assert all(row[1:] == ("browser", True, False, False) for row in rows)
    issue_token(database, provider="alpha", product="res", region="fra1")
    db = Database(database)
    try:
        plan = await batches.pulse_plan(db, datetime(2026, 10, 1, 12, 31, tzinfo=UTC))
    finally:
        await db.close()
    assert plan["targets"] == ["generic_http", "generic_https"] and plan["created"] > 0
    with connect(database) as conn:
        assert conn.execute(
            "SELECT count(*) FROM test_batches WHERE target_id = ANY(%s)",
            (list(c.BROWSER_TARGETS),),
        ).fetchone() == (0,)
