"""scripts/server-alerts/router.py (ALERT-POLICY-1, #1297): the policy of every Slack sender.
Grace and dedupe timing, recovery in the page's thread, the 407 page at once, the rate limit, the
daily digest, dry run versus live posting, and a post that fails."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("router", ROOT / "scripts/server-alerts/router.py")
router = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(router)

T0 = 1_800_000_000  # 2027-01-15 08:00 UTC
MIN = 60
TITLE = "Atlas production on canada-test"
LOGIN = "manager:proxy_login_refused:proxyempire"


class FakeSlack:
    """Records posts; each page's thread id is its own number."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, str, str | None]] = []
        self.down = False

    def post(self, kind: str, text: str, thread: str | None = None) -> str | None:
        if self.down:
            raise OSError("Slack is down")
        self.posts.append((kind, text, thread))
        return thread or f"ts-{len(self.posts)}"


class World:
    def __init__(self, tmp: Path) -> None:
        self.state, self.slack, self.now = tmp, FakeSlack(), T0

    def run(self, *lines: str, silenced: bool = False, quiet: bool = False) -> list[tuple]:
        before = len(self.slack.posts)
        r = router.Router(self.state, TITLE, self.slack, self.now)
        self.code = r.run(list(lines), silenced, quiet)
        return self.slack.posts[before:]

    def later(self, minutes: float) -> None:
        self.now += minutes * MIN


def opened(obj: str, kind: str = "check-failed", text: str = "broken", since: str = "", level=2):
    return f"open\t{obj}\t{kind}\t{text}\t{since}\t{level}"


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_a_problem_that_heals_within_its_grace_never_pages(world: World) -> None:
    assert world.run(opened("disk", text="the disk is 90% full")) == []
    world.later(14)
    assert world.run() == []
    world.later(0.5)
    assert world.run("ok\tdisk") == []
    events = json.loads((world.state / "router.json").read_text())["events"]
    assert [(e["group"], e["status"]) for e in events] == [("disk (check failed)", "healed")]


def test_a_problem_still_open_after_its_grace_pages_once_and_recovers_in_its_thread(world):
    world.run(opened("disk", text="the disk is 90% full"))
    world.later(15)
    [(kind, text, thread)] = world.run()
    assert kind == "page" and thread is None
    assert text.startswith(f"<!everyone> {TITLE}: disk: check failed: the disk is 90% full\n")
    assert "Since 08:00 UTC (15 min). It did not heal by itself within 15 min." in text
    assert "What to do: " in text
    world.later(60)
    assert world.run(opened("disk")) == []  # no repeat while open
    world.later(10)
    [(kind, text, thread)] = world.run("ok\tdisk")
    assert (kind, thread) == ("followup", "ts-1")
    assert text == f"{TITLE}: disk: recovered at 09:25 UTC, after 1 h 25 min."
    world.later(10)
    assert world.run("ok\tdisk") == []  # one follow-up only


def test_a_dead_unit_pages_and_its_restart_is_the_follow_up(world: World) -> None:
    unit = "atlas-orchestrator.service"
    world.run(opened(unit, kind="could-not-restart", text="the unit is failed (result: exit)"))
    world.later(16)
    [(_, text, _)] = world.run(opened(unit, kind="could-not-restart"))
    assert "could not restart" in text and f"journalctl --user -u {unit}," in text
    world.later(4)
    [(kind, text, thread)] = world.run(f"ok\t{unit}")
    assert (kind, thread) == ("followup", "ts-1")
    assert text == f"{TITLE}: {unit}: restarted ok at 08:20 UTC, after 20 min."


def test_a_restart_that_worked_is_digest_only(world: World) -> None:
    assert world.run("once\tatlas-api@blue.service\trestarted\trestarted 1 time(s)") == []
    world.now = T0 + 20 * 3600  # the next 04:00 UTC
    [(kind, text, _)] = world.run()
    assert kind == "digest" and "atlas-api@blue.service (restarted): 1 time(s)" in text


def test_login_refused_pages_at_once_even_past_the_hourly_limit(world: World) -> None:
    flood = [opened(f"mini browser-{n}", text="x", since=str(T0 - 3600)) for n in range(6)]
    assert len(world.run(*flood)) == 6  # the hour's 6 pages are used
    login = opened(LOGIN, kind="proxy_login_refused", text="proxyempire refused our login (407)")
    [(kind, text, _)] = world.run(login)
    assert kind == "page"
    assert text.startswith(f"<!everyone> {TITLE}: manager alert: proxyempire refused our login")
    assert "It did not heal" not in text and "resume-network" in text


def test_the_same_open_problem_repeats_after_6_hours_or_when_it_gets_worse(world: World) -> None:
    world.run(opened(LOGIN, kind="proxy_login_refused"))
    world.later(60)
    assert world.run(opened(LOGIN, kind="proxy_login_refused")) == []
    [(_, _, thread)] = world.run(opened(LOGIN, kind="proxy_login_refused", level=3))
    assert thread == "ts-1"  # worse: paged again, in the same thread
    world.later(5 * 60 + 59)
    assert world.run(opened(LOGIN, kind="proxy_login_refused", level=3)) == []
    world.later(1)
    [(_, _, thread)] = world.run(opened(LOGIN, kind="proxy_login_refused", level=3))
    assert thread == "ts-1"


def test_more_pages_than_the_hourly_limit_fold_into_one_summary(world: World) -> None:
    lines = [opened(f"check {n}", text=f"problem {n}", since=str(T0 - 3600)) for n in range(10)]
    posts = world.run(*lines)
    assert [kind for kind, _, _ in posts] == ["page"] * 5 + ["summary"]
    assert posts[-1][1].startswith(f"<!everyone> {TITLE}: 5 more alerts this hour: check 5 ")
    [(kind, text, thread)] = world.run("ok\tcheck 7")
    assert (kind, thread) == ("followup", "ts-6")  # in the summary's thread
    world.run(opened("check 11", since=str(T0 - 3600)))
    assert world.run() == []  # the hour is full: it waits
    world.later(61)
    [(kind, text, _)] = world.run()
    assert kind == "page" and "check 11" in text


def test_expected_conditions_never_page_and_reach_the_digest(world: World) -> None:
    gap = opened("manager:pulse_runs_open:20270115T07:proxy-cheap/lon1", kind="pulse_runs_open")
    cover = opened("manager:coverage_missing:soax:20270115", kind="coverage_missing")
    assert world.run(gap, cover, "sweep\tmanager:") == []
    world.later(8 * 60)
    assert world.run(cover, "sweep\tmanager:") == []  # the gap was acknowledged: healed
    world.now = T0 + 20 * 3600 + 30
    [(kind, text, thread)] = world.run(cover, "sweep\tmanager:")
    assert kind == "digest" and thread is None and "<!everyone>" not in text
    assert text.splitlines()[1:] == [
        "- pulse_runs_open: 1 time(s), the longest 8 h",
        "- coverage_missing: 1 time(s), the longest 20 h",
    ]
    world.later(2)
    assert world.run(cover, "sweep\tmanager:") == []  # once a day


def test_the_digest_names_what_is_open_for_more_than_a_day(world: World) -> None:
    world.run(opened("manager:coverage_missing:soax", kind="coverage_missing"))
    world.now = T0 + 44 * 3600  # the second 04:00 UTC
    [(_, text, _)] = world.run()
    assert "Open for more than 24 hours: coverage_missing since 15 Jan 08:00 UTC" in text


def test_an_empty_digest_says_all_good_or_is_quiet_on_a_mini(world: World) -> None:
    world.now = T0 + 20 * 3600
    [(_, text, _)] = world.run()
    assert text == f"{TITLE}: daily digest: all good, nothing in the last 24 hours."
    world.now += 86400
    assert world.run(quiet=True) == []


def test_a_failed_post_is_kept_and_sent_by_the_next_run(world: World) -> None:
    world.run(opened(LOGIN, kind="proxy_login_refused"))  # paged
    world.slack.down = True
    world.run(opened("disk", since=str(T0 - 3600)), f"ok\t{LOGIN}")
    assert world.code == router.POST_FAILED
    world.slack.down = False
    posts = world.run()
    assert [kind for kind, _, _ in posts] == ["page", "followup"]


def test_silence_holds_pages_until_it_ends(world: World) -> None:
    assert world.run(opened(LOGIN, kind="proxy_login_refused"), silenced=True) == []
    [(kind, _, _)] = world.run()
    assert kind == "page"


def test_the_policy_table(tmp_path: Path) -> None:
    rules, settings = router.load_policy()
    assert settings == {"max_pages_per_hour": 6, "digest_hour_utc": 4}
    expect = {
        "manager:proxy_login_refused:proxyempire": ("now", 0),
        "manager:network_paused:x": ("now", 0),
        "manager:pulse_runs_open:20261116T22:proxy-cheap/lon1": ("digest", 0),
        "manager:coverage_summary": ("digest", 0),
        "manager:job_failed:raw_upload:reset:20261116T17": ("page", 180 * MIN),
        "manager:job_failed:coverage": ("page", 15 * MIN),
        "check-failed:queue": ("page", 60 * MIN),
        "network-down:network proxy-cheap residential": ("page", 30 * MIN),
        "restarted:atlas-api@blue.service": ("digest", 0),
        "event:atlas-hourly-1.service": ("digest", 0),
    }
    for name, (action, grace) in expect.items():
        rule = router.rule_for(rules, name)
        assert (rule.action, rule.grace) == (action, grace), name
    bad = tmp_path / "policy.txt"
    bad.write_text("x* | sometimes | 0 | 6 |\n")
    with pytest.raises(ValueError, match="unknown action"):
        router.load_policy(bad)


FAKE_CURL = r"""
import json, os, sys
from pathlib import Path
fake = Path(os.environ["FAKE"])
with open(fake / "curl.jsonl", "a") as out:
    out.write(json.dumps({"config": sys.stdin.read(), "argv": sys.argv[1:]}) + "\n")
if (fake / "slack-down").exists():
    sys.exit(22)
print(json.dumps({"ok": True, "ts": "1800000000.0001"}))
"""


def main_run(tmp: Path, monkeypatch, lines: str, now: int) -> int:
    monkeypatch.setenv("ATLAS_ALERTS_NOW", str(now))
    monkeypatch.setattr(sys, "stdin", io.StringIO(lines))
    return router.main(["router.py", str(tmp / "state"), TITLE, str(tmp / "cfg/slack-webhook")])


@pytest.fixture
def config(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / "state").mkdir()
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "slack-webhook").write_text("https://hooks.example.invalid/services/T0KEN-x\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(f"#!{sys.executable}\n{FAKE_CURL}")
    (bin_dir / "curl").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE", str(tmp_path))
    return cfg


def test_dry_run_records_what_it_would_post_and_posts_nothing(tmp_path, monkeypatch, config):
    assert main_run(tmp_path, monkeypatch, opened(LOGIN, kind="proxy_login_refused"), T0) == 0
    assert main_run(tmp_path, monkeypatch, f"ok\t{LOGIN}\n", T0 + 600) == 0
    lines = (tmp_path / "state/router-dry-run.jsonl").read_text().splitlines()
    page, follow = (json.loads(line) for line in lines)
    assert page["kind"] == "page" and follow["kind"] == "followup"
    assert follow["thread"] == f"dry-{T0}-1"  # paired with its page
    assert not (tmp_path / "curl.jsonl").exists()


def test_live_with_the_webhook_names_the_page_in_its_follow_up(tmp_path, monkeypatch, config):
    (config / "alert-router").write_text("live\n")
    main_run(tmp_path, monkeypatch, opened(LOGIN, kind="proxy_login_refused", text="407"), T0)
    main_run(tmp_path, monkeypatch, f"ok\t{LOGIN}\n", T0 + 600)
    calls = [json.loads(line) for line in (tmp_path / "curl.jsonl").read_text().splitlines()]
    assert all("T0KEN" not in " ".join(c["argv"]) for c in calls)  # the URL is on stdin only
    assert calls[0]["config"] == 'url = "https://hooks.example.invalid/services/T0KEN-x"\n'
    follow = json.loads(calls[1]["argv"][calls[1]["argv"].index("--data-binary") + 1])
    assert follow["text"].endswith("(Page of 08:00 UTC: 407)")


def test_live_with_a_bot_replies_in_the_pages_thread(tmp_path, monkeypatch, config):
    (config / "alert-router").write_text("live\n")
    (config / "slack-bot").write_text("xoxb-not-a-token\nC0CHANNEL\n")
    main_run(tmp_path, monkeypatch, opened(LOGIN, kind="proxy_login_refused"), T0)
    main_run(tmp_path, monkeypatch, f"ok\t{LOGIN}\n", T0 + 600)
    calls = [json.loads(line) for line in (tmp_path / "curl.jsonl").read_text().splitlines()]
    assert "Authorization: Bearer xoxb-not-a-token" in calls[0]["config"]
    assert all("xoxb" not in " ".join(c["argv"]) for c in calls)
    bodies = [json.loads(c["argv"][c["argv"].index("--data-binary") + 1]) for c in calls]
    assert bodies[0]["channel"] == "C0CHANNEL" and "thread_ts" not in bodies[0]
    assert bodies[1]["thread_ts"] == "1800000000.0001"


def test_live_slack_down_exits_3_and_retries(tmp_path, monkeypatch, config):
    (config / "alert-router").write_text("live\n")
    (tmp_path / "slack-down").write_text("")
    assert main_run(tmp_path, monkeypatch, opened(LOGIN, kind="proxy_login_refused"), T0) == 3
    (tmp_path / "slack-down").unlink()
    assert main_run(tmp_path, monkeypatch, "", T0 + 120) == 0
    assert len((tmp_path / "curl.jsonl").read_text().splitlines()) == 2


def test_the_digest_leaves_out_what_was_paged(world: World) -> None:
    world.run(opened(LOGIN, kind="proxy_login_refused"), opened("load", since=str(T0)))
    world.later(10)
    world.run(f"ok\t{LOGIN}", "ok\tload")
    world.now = T0 + 20 * 3600
    [(_, text, _)] = world.run()
    assert text.splitlines()[1:] == ["- load (check failed): 1 time(s), the longest 10 min"]


def test_a_job_row_that_fails_again_after_healing_gets_its_grace_again(world: World) -> None:
    """job_failed:<job> is one row per job: a new failure bumps it and keeps its old first_at."""
    row = opened("manager:job_failed:raw_upload", kind="job_failed", since=str(T0))
    world.run(row)
    world.later(60)
    assert world.run("sweep\tmanager:") == []  # the next run was ok
    world.later(15 * 60)
    assert world.run(row, "sweep\tmanager:") == []  # fails again; first_at is 16 h old
    world.later(179)
    assert world.run(row, "sweep\tmanager:") == []
    world.later(1)
    [(kind, text, _)] = world.run(row, "sweep\tmanager:")
    assert kind == "page" and "Since 00:00 UTC (3 h)" in text


def test_a_bot_file_without_a_channel_is_a_failed_post(tmp_path, monkeypatch, config):
    (config / "alert-router").write_text("live\n")
    (config / "slack-bot").write_text("xoxb-only\n")
    assert main_run(tmp_path, monkeypatch, opened(LOGIN, kind="proxy_login_refused"), T0) == 3
