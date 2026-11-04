"""The transport's view sink and scripts/browser_capture.py over fake browsers (no traffic)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from browser_site import SiteTarget
from test_browser_node import make_state
from test_browser_transport import FakeBrowser, Harness, Script

from proxy_index.browser.driver import NavigationError
from proxy_index.browser.judge import selector_list
from proxy_index.browser.targets import ai
from proxy_index.browser.targets.ai import AiOverview

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "browser_capture.py"
_spec = importlib.util.spec_from_file_location("browser_capture", SCRIPT)
capture = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = capture  # its dataclasses look their module up
_spec.loader.exec_module(capture)

SEEN: list = []


class Watching(SiteTarget):
    """SiteTarget that keeps every view its judge was given."""

    def judge(self, view):
        SEEN.append(view)
        return SiteTarget.judge(self, view)


def _masked(tries: list) -> list:
    """Records and screenshots with the measured timing values left out."""
    return [(record | {"timings_ms": sorted(record["timings_ms"])}, shot) for record, shot in tries]


@pytest.mark.parametrize("script", [Script(), Script(found=0), Script(error=NavigationError(None))])
async def test_a_sink_changes_neither_records_nor_screenshots(script):
    plain, sunk = Harness(script), Harness(script, view_sink=lambda i, view: None)
    for harness in (plain, sunk):
        for index in range(2):
            await harness.execute(index=index)
    assert plain.transport.view_sink is None
    assert _masked(sunk.recorder.tries) == _masked(plain.recorder.tries)


async def test_the_sink_gets_the_judged_view_once_per_try_after_its_record():
    SEEN.clear()
    calls: list = []
    harness = Harness()
    tries = harness.recorder.tries
    harness.transport.view_sink = lambda i, view: calls.append((i, view, len(tries)))
    for index in (3, 4):
        await harness.execute(Watching(), index=index)
    assert [(i, recorded) for i, _, recorded in calls] == [(3, 1), (4, 2)]
    assert [view for _, view, _ in calls] == SEEN
    assert all(sunk is judged for (_, sunk, _), judged in zip(calls, SEEN, strict=True))


async def test_a_navigation_error_gives_the_sink_nothing():
    calls: list = []
    harness = Harness(Script(error=NavigationError("NS_ERROR_NET_TIMEOUT")))
    harness.transport.view_sink = lambda i, view: calls.append(i)
    await harness.execute()
    assert calls == [] and len(harness.recorder.tries) == 1


class Fleet:
    def __init__(self, script: Script) -> None:
        self.script, self.browsers = script, []

    async def launch(self) -> FakeBrowser:
        self.browsers.append(FakeBrowser(self.script))
        return self.browsers[-1]


def run(tmp_path, *extra, script=None, target="amazon_pdp", tries=3, network="shifter/residential"):
    state = make_state(tmp_path)
    fleet = Fleet(script or Script())
    argv = ["--settings", str(state / "settings.toml"), "--state", str(state)]
    argv += ["--host", "browser-1", "--network", network, "--target", target]
    argv += ["--tries", str(tries), "--country", "us", "--out", str(tmp_path / "out"), *extra]
    return capture.main(argv, launch=fleet.launch), fleet


def files(tmp_path) -> dict[str, bytes]:
    out = tmp_path / "out"
    return {str(p.relative_to(out)): p.read_bytes() for p in out.rglob("*") if p.is_file()}


def test_each_try_writes_its_view_verdict_and_screenshot(tmp_path):
    status, fleet = run(tmp_path)
    assert status == 0 and len(fleet.browsers) == 1
    directory = tmp_path / "out/shifter/residential/amazon_pdp"
    names = sorted(p.name for p in directory.iterdir())
    expected = [f"{i:04d}.{kind}" for i in range(3) for kind in ("jpg", "json")]
    assert names == sorted([*expected, "summary.md", "tries.jsonl"])
    records = [json.loads(line) for line in (directory / "tries.jsonl").read_text().splitlines()]
    assert [r["i"] for r in records] == [0, 1, 2]
    for record in records:
        document = json.loads((directory / f"{record['i']:04d}.json").read_text())
        assert set(document["view"]) == {"final_url", "status", "title", "text_head", "found"}
        assert document["view"]["title"] == "Product" and document["view"]["status"] == 200
        assert document["judged"] == {k: record[k] for k in ("verdict", "error", "reason")}
        assert document["input"] == "0306406152" == record["input"]
        assert document["timings_ms"] == record["timings_ms"]
        assert document["bytes_down_est"] == 4321
        assert (directory / f"{record['i']:04d}.jpg").read_bytes() == b"jpeg"
    summary = (directory / "summary.md").read_text()
    assert "Tries: 3 of 3; page loads used: 3" in summary and "without a view" in summary
    assert "browser-1" in summary and "country: US" in summary


def test_a_navigation_error_has_no_view(tmp_path):
    status, _ = run(tmp_path, script=Script(error=NavigationError("NS_ERROR_NET_TIMEOUT")), tries=1)
    directory = tmp_path / "out/shifter/residential/amazon_pdp"
    document = json.loads((directory / "0000.json").read_text())
    assert status == 0 and document["view"] is None
    judged = {"verdict": "timeout", "error": "timeout", "reason": "ns_error_net_timeout"}
    assert document["judged"] == judged
    assert "(no page was read, a navigation error): 1" in (directory / "summary.md").read_text()


@pytest.mark.parametrize(
    ("extra", "tries"),
    [((), 51), ((), 0), (("--country", "gb"), 1), ((), 1)],
)
def test_requests_over_the_caps_are_refused_before_any_try(tmp_path, capsys, extra, tries):
    if tries == 1 and not extra:  # an existing directory: one capture per directory
        (tmp_path / "out/shifter/residential/amazon_pdp").mkdir(parents=True)
        (tmp_path / "out/shifter/residential/amazon_pdp/keep").write_text("earlier")
    status, fleet = run(tmp_path, *extra, tries=tries)
    assert status == 2 and fleet.browsers == [] and "browser_capture:" in capsys.readouterr().err
    assert set(files(tmp_path)) <= {"shifter/residential/amazon_pdp/keep"}


@pytest.mark.parametrize(
    ("target", "network"),
    [("instagram_web", "shifter/residential"), ("amazon_pdp", "example/residential")],
)
def test_a_target_or_network_the_host_does_not_run_is_refused(tmp_path, target, network):
    status, fleet = run(tmp_path, target=target, network=network)
    assert status == 2 and fleet.browsers == [] and not (tmp_path / "out").exists()


def test_another_country_needs_its_allow_flag():
    capture.check_request(1, "gb", "gb")
    with pytest.raises(capture.UserError):
        capture.check_request(1, "gb", "de")


ANSWER = "An answer sentence that the overview shows. " * 6
OVERVIEW = {
    selector_list(*AiOverview.ANSWER): 1,
    **dict.fromkeys(AiOverview.ANSWER, 1),
    AiOverview.RESULTS: 5,
}


def test_an_answered_ai_try_keeps_no_screenshot_and_no_answer_text(tmp_path, monkeypatch):
    monkeypatch.setattr(ai, "STABLE_S", 0)
    monkeypatch.setattr(ai, "POLL_S", 0)
    script = Script(text=ANSWER, found=OVERVIEW, url="https://www.google.com/search?q=x")
    status, _ = run(tmp_path, script=script, target="ai_overview", tries=2)
    directory = tmp_path / "out/shifter/residential/ai_overview"
    document = json.loads((directory / "0000.json").read_text())
    assert status == 0 and document["judged"]["verdict"] == "ok"
    chars = len(ANSWER.strip())
    assert document["view"]["text_head"] == f"<answer: {chars} chars>"
    assert document["view"]["found"]["answer_chars"] == chars
    assert not list(directory.glob("*.jpg"))
    assert not any(b"overview shows" in data for data in files(tmp_path).values())


def test_no_credential_token_or_shown_address_is_written(tmp_path):
    script = Script(title="Denied 2001:db8::7", text="Your IP: 203.0.113.7 is blocked", found=0)
    run(tmp_path, script=script, tries=2)
    written = b"".join(files(tmp_path).values())
    for secret in (b"secret", b"acct", b"token-shifter", b"203.0.113.7", b"2001:db8::7"):
        assert secret not in written
    document = json.loads((tmp_path / "out/shifter/residential/amazon_pdp/0000.json").read_text())
    assert document["view"]["title"] == "Denied <ip>"
    assert document["view"]["text_head"] == "Your IP: <ip> is blocked"


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("12:30:45 on 1.2", True),
        ("std::vector", True),
        ("198.51.100.1", False),
        ("from fe80::1 now", False),
        ("::1", False),
        ("2001:db8:0:0:1:0:0:1", False),
    ],
)
def test_scrub_replaces_addresses_only(text, kept):
    assert (capture.scrub(text) == text) is kept


TOKEN_URL = "https://user:pw@www.google.com/sorry/index?continue=x&q=EgQTOKENPROBE#fragprobe"


def test_the_view_url_keeps_only_scheme_host_and_path(tmp_path):
    run(tmp_path, script=Script(url=TOKEN_URL, found=0), tries=1)
    directory = tmp_path / "out/shifter/residential/amazon_pdp"
    document = json.loads((directory / "0000.json").read_text())
    assert document["view"]["final_url"] == "https://www.google.com/sorry/index"
    for probe in (b"TOKENPROBE", b"fragprobe", b"pw@", b"user:", b"continue="):
        assert not any(probe in data for data in files(tmp_path).values()), probe


@pytest.mark.parametrize(
    ("url", "written"),
    [
        ("http://203.0.113.7:8080/a?b=c", "http://<ip>:8080/a"),
        ("https://[2001:db8::7]/x#y", "https://[<ip>]/x"),
        ("about:blank", "about:blank"),
        ("about:neterror?e=dnsNotFound&u=https%3A//x/?q=TOKEN", "about:"),
        ("data:text/html,secret", "data:"),
        ("not a url", ""),
    ],
)
def test_view_url(url, written):
    assert capture.view_url(url) == written


def test_every_file_is_private_and_an_untargeted_network_says_so(tmp_path):
    run(tmp_path, network="proxy-cheap/residential", tries=1)
    directory = tmp_path / "out/proxy-cheap/residential/amazon_pdp"
    assert {p.stat().st_mode & 0o777 for p in directory.iterdir()} == {0o600}
    assert directory.stat().st_mode & 0o777 == 0o700
    summary = (directory / "summary.md").read_text()
    assert "country: US (untargeted network: no country parameter)" in summary
