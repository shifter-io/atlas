"""Browser pulse contracts: names, outcomes, keys, the ingest rule and the target protocol.

Try records and the manifest are in `test_browser_contracts_records.py`; the shared slot, run id
and checksum are in `browser_contracts_cases.py`.
"""

import asyncio
import typing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from browser_contracts_cases import RUN_ID, SHA, SLOT

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
    # The HTTP targets are still the two echo targets; browser targets register beside them.
    assert {t.id for t in registry.all() if t.engine == "http"} == {"generic_http", "generic_https"}


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
