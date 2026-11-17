"""The alert router of server-alerts.sh and mini-watch.sh (ALERT-POLICY-1, #1297).

Each check run hands it what it saw, one tab-separated line per observation on stdin:
    open  <object> <kind> <text> <since> <level>   a problem that is there now
    once  <object> <kind> <text>                   a one-off event (no recovery follows)
    ok    <object>                                 the object is fine
    sweep <prefix>                                 objects with the prefix not opened above are fine
The policy (policy.txt, next to this file) decides: page at once, page after a grace time when
it did not heal, or the daily digest only. A sent page gets one follow-up in its Slack thread
when it is fixed. Pages are limited per hour; the rest fold into one "N more alerts" page.

Dry run (the default): nothing is posted; every message is appended to router-dry-run.jsonl in
the state folder. Live: the file "alert-router" next to the webhook reads "live". With the file
"slack-bot" there too (a bot token and a channel id, mode 600), follow-ups go into the page's
thread; with the webhook only, a follow-up is a new message naming the page. Secrets reach curl
on its stdin, never argv. Python 3.9 (the minis' /usr/bin/python3), standard library only.
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple

POLICY = Path(__file__).resolve().parent / "policy.txt"
GENERIC = ("check-failed", "could-not-restart", "restarted", "network-down", "event", "info")
KEEP = 2 * 86400  # digest events older than this are dropped
POST_FAILED = 3  # exit code: a message could not be posted (it is tried again next run)


class Rule(NamedTuple):
    name: str
    action: str
    grace: int  # seconds
    repeat: int  # seconds
    todo: str


def load_policy(path: Path = POLICY) -> tuple[list[Rule], dict[str, int]]:
    rules, settings = [], {"max_pages_per_hour": 6, "digest_hour_utc": 4}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cells = [c.strip() for c in line.split("|")]
        if cells[0] == "set":
            settings[cells[1]] = int(cells[2])
            continue
        name, action, grace, repeat = cells[:4]
        if action not in ("now", "page", "digest"):
            raise ValueError(f"policy: unknown action {action!r} in {line!r}")
        todo = cells[4] if len(cells) > 4 else ""
        rules.append(Rule(name, action, int(grace) * 60, int(repeat) * 3600, todo))
    return rules, settings


def rule_for(rules: list[Rule], name: str) -> Rule:
    for rule in rules:
        if fnmatch.fnmatchcase(name, rule.name):
            return rule
    return Rule("*", "page", 900, 6 * 3600, "")


def hhmm(t: float) -> str:
    return time.strftime("%H:%M", time.gmtime(t))


def span(seconds: float) -> str:
    minutes = max(0, int(seconds) // 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min" if minutes % 60 else f"{minutes // 60} h"


class Slack:
    """Posts, or records what it would post (dry run). Returns the message's thread id."""

    def __init__(self, state: Path, webhook: Path, now: float) -> None:
        self.state, self.webhook, self.now = state, webhook, now
        mode = webhook.parent / "alert-router"
        self.live = mode.exists() and mode.read_text().strip() == "live"
        self.bot = webhook.parent / "slack-bot"
        self.sent = 0

    def post(self, kind: str, text: str, thread: str | None = None) -> str | None:
        self.sent += 1
        if not self.live:
            line = {"at": int(self.now), "kind": kind, "text": text, "thread": thread}
            with open(self.state / "router-dry-run.jsonl", "a") as out:
                out.write(json.dumps(line) + "\n")
            return thread or f"dry-{int(self.now)}-{self.sent}"
        if self.bot.exists():
            token, channel = self.bot.read_text().split()[:2]
            body: dict[str, Any] = {"channel": channel, "text": text}
            if thread:
                body["thread_ts"] = thread
            config = 'url = "https://slack.com/api/chat.postMessage"\n'
            config += f'header = "Authorization: Bearer {token}"\n'
            answer = json.loads(curl(config, body) or "{}")
            if not answer.get("ok"):
                raise OSError(f"Slack refused the message: {answer.get('error', 'no answer')}")
            return str(answer["ts"])
        url = self.webhook.read_text().split()[0]
        curl(f'url = "{url}"\n', {"text": text})
        return None


def curl(config: str, body: dict[str, Any]) -> str:
    command = ["curl", "-fsS", "--max-time", "10", "-K", "-"]
    command += ["-H", "Content-Type: application/json", "--data-binary", json.dumps(body)]
    done = subprocess.run(command, input=config, capture_output=True, text=True, timeout=30)
    if done.returncode != 0:
        raise OSError(f"curl exit {done.returncode}")
    return done.stdout


def group(item: dict[str, Any]) -> str:
    """The digest's line: the alert kind, or the object and its check for the generic kinds."""
    kind = item["kind"]
    return f"{item['object']} ({kind.replace('-', ' ')})" if kind in GENERIC else kind


def shown(item: dict[str, Any]) -> str:
    """The object as a person reads it: a manager alert row is named by its text."""
    return "manager alert" if item["object"].startswith("manager:") else item["object"]


class Router:
    def __init__(self, state: Path, title: str, slack: Slack, now: float) -> None:
        self.path, self.title, self.slack, self.now = state / "router.json", title, slack, now
        self.rules, self.settings = load_policy()
        saved = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.open: dict[str, dict[str, Any]] = saved.get("open", {})
        self.pages: list[float] = [t for t in saved.get("pages", []) if t > now - 3600]
        events = saved.get("events", [])
        self.events: list[dict[str, Any]] = [e for e in events if e["until"] > now - KEEP]
        self.followups: list[dict[str, Any]] = saved.get("followups", [])
        self.last_page: dict[str, float] = saved.get("last_page", {})
        self.digest_day: str = saved.get("digest_day", "")
        self.oneoffs: list[tuple[dict[str, Any], Rule]] = []
        self.failed = 0

    def save(self) -> None:
        keys = ("open", "pages", "events", "followups", "last_page", "digest_day")
        data = {k: getattr(self, k) for k in keys}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        tmp.replace(self.path)

    def rule(self, item: dict[str, Any]) -> Rule:
        return rule_for(self.rules, item["name"])

    def observe(self, lines: list[str]) -> None:
        seen, sweeps = set(), []
        for line in lines:
            cells = line.rstrip("\n").split("\t")
            what, obj = cells[0], cells[1] if len(cells) > 1 else ""
            if what == "open":
                seen.add(obj)
                self.opened(obj, *(cells[2:6] + [""] * 4)[:4])
            elif what == "once":
                self.once(obj, cells[2], cells[3] if len(cells) > 3 else "")
            elif what == "ok":
                self.resolve(obj)
            elif what == "sweep":
                sweeps.append(obj)
        for prefix in sweeps:
            for obj in [o for o in self.open if o.startswith(prefix) and o not in seen]:
                self.resolve(obj)

    def opened(self, obj: str, kind: str, text: str, since: str, level: str) -> None:
        item = self.open.get(obj)
        if item is None:
            start = float(since) if since.strip() else self.now
            item = self.open[obj] = {"object": obj, "kind": kind, "since": min(start, self.now)}
            item["name"] = obj if obj.startswith("manager:") else f"{kind}:{obj}"
            item["paged"] = None
        item["text"], item["level"] = text.strip(), int(level or 2)

    def once(self, obj: str, kind: str, text: str) -> None:
        item = {"object": obj, "kind": kind, "since": self.now, "text": text.strip(), "level": 2}
        item.update(name=f"{kind}:{obj}", once=True, paged=None)
        rule = self.rule(item)
        recent = self.now - self.last_page.get(item["name"], -1e12) < rule.repeat
        if rule.action == "digest" or recent:
            self.record(item, "once")
        else:
            self.oneoffs.append((item, rule))

    def resolve(self, obj: str) -> None:
        item = self.open.pop(obj, None)
        if item is None:
            return
        self.record(item, "paged" if item["paged"] else "healed")
        if item["paged"]:
            self.followups.append(item | {"until": self.now})

    def record(self, item: dict[str, Any], status: str) -> None:
        event = {"group": group(item), "since": item["since"], "until": self.now}
        self.events.append(event | {"status": status, "text": item["text"][:200]})

    def text(self, item: dict[str, Any], rule: Rule) -> str:
        what = item["kind"].replace("-", " ") if item["kind"] in GENERIC else ""
        head = f"<!everyone> {self.title}: {shown(item)}: {what + ': ' if what else ''}"
        since = f"Since {hhmm(item['since'])} UTC ({span(self.now - item['since'])})."
        lines = [head + item["text"], since]
        if rule.action == "page" and rule.grace:
            lines[-1] += f" It did not heal by itself within {span(rule.grace)}."
        if rule.todo:
            lines.append(f"What to do: {rule.todo.replace('<object>', item['object'])}")
        return "\n".join(lines)

    def page_one(self, item: dict[str, Any], rule: Rule, thread: str | None = None) -> str | None:
        try:
            ts = self.slack.post("page", self.text(item, rule), thread)
        except OSError as exc:
            print(f"router: posting failed: {exc}", file=sys.stderr)
            self.failed = 1
            return None
        self.pages.append(self.now)
        return ts or "webhook"

    def due(self) -> list[tuple[dict[str, Any], Rule]]:
        out = []
        for item in self.open.values():
            rule = self.rule(item)
            paged, worse = item["paged"], item["level"] > (item["paged"] or {}).get("level", 9)
            if rule.action == "digest":
                continue
            if paged is None and (rule.action == "now" or self.now - item["since"] >= rule.grace):
                out.append((item, rule))
            elif paged and (self.now - paged["at"] >= rule.repeat or worse):
                out.append((item, rule))  # a reminder, or worse than when it was paged
        return sorted(out, key=lambda pair: (pair[1].action != "now", pair[0]["since"]))

    def page_due(self) -> None:
        due = self.due() + self.oneoffs
        urgent = [pair for pair in due if pair[1].action == "now"]  # never held or folded
        rest = [pair for pair in due if pair[1].action != "now"]
        room = int(self.settings["max_pages_per_hour"]) - len(self.pages) - len(urgent)
        folded: list[tuple[dict[str, Any], Rule]] = []
        if len(rest) > room:  # one page says "N more"; with no room, open ones wait an hour
            keep = max(room - 1, 0)
            folded = rest[keep:] if room >= 1 else []
            rest = rest[:keep]
        for item, rule in urgent + rest:
            self.mark(item, self.page_one(item, rule, self.thread(item)))
        if folded:
            ts = self.fold(folded)
            for item, _ in folded:
                self.mark(item, ts)
        for item, _ in self.oneoffs:
            if item["paged"] is None:  # held by the limit or not posted: the digest keeps it
                self.record(item, "once")
        self.oneoffs = []

    def thread(self, item: dict[str, Any]) -> str | None:
        paged = item["paged"]
        return paged["thread"] if paged and paged["thread"] != "webhook" else None

    def mark(self, item: dict[str, Any], ts: str | None) -> None:
        if ts is None:
            return
        item["paged"] = {"at": self.now, "thread": self.thread(item) or ts, "level": item["level"]}
        self.last_page[item["name"]] = self.now
        if item.get("once"):
            self.record(item, "paged")

    def fold(self, folded: list[tuple[dict[str, Any], Rule]]) -> str | None:
        names = ", ".join(f"{shown(i)} ({i['text'][:60]})" for i, _ in folded[:10])
        text = f"<!everyone> {self.title}: {len(folded)} more alerts this hour: {names}"
        try:
            ts = self.slack.post("summary", text) or "webhook"
        except OSError as exc:
            print(f"router: posting failed: {exc}", file=sys.stderr)
            self.failed = 1
            return None
        self.pages.append(self.now)
        return ts

    def follow_up(self) -> None:
        waiting = []
        for item in self.followups:
            fixed = "restarted ok" if item["kind"] == "could-not-restart" else "recovered"
            text = f"{self.title}: {shown(item)}: {fixed} at {hhmm(item['until'])} UTC"
            text += f", after {span(item['until'] - item['since'])}."
            thread = item["paged"]["thread"]
            if thread == "webhook":  # no thread to reply in: name the page
                text += f" (Page of {hhmm(item['paged']['at'])} UTC: {item['text'][:80]})"
            try:
                self.slack.post("followup", text, None if thread == "webhook" else thread)
            except OSError as exc:
                print(f"router: posting failed: {exc}", file=sys.stderr)
                self.failed = 1
                waiting.append(item)
        self.followups = waiting

    def digest_text(self) -> str:
        groups: dict[str, list[float]] = {}
        still = [i for i in self.open.values() if self.rule(i).action == "digest"]
        now, quiet = self.now, [e for e in self.events if e["status"] != "paged"]  # pages told
        items = quiet + [dict(group=group(i), since=i["since"], until=now) for i in still]
        for event in items:
            groups.setdefault(event["group"], []).append(event["until"] - event["since"])
        lines = [f"{self.title}: daily digest of the last 24 hours (healed by itself or expected)."]
        for name, spans in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"- {name}: {len(spans)} time(s), the longest {span(max(spans))}")
        old = [i for i in self.open.values() if self.now - i["since"] > 86400]
        for item in old:
            since = time.strftime("%d %b %H:%M", time.gmtime(item["since"]))
            lines.append(f"Open for more than 24 hours: {group(item)} since {since} UTC")
        if len(lines) == 1:
            lines[0] = f"{self.title}: daily digest: all good, nothing in the last 24 hours."
        return "\n".join(lines)

    def digest(self, quiet_when_empty: bool) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(self.now))
        hour = time.gmtime(self.now).tm_hour
        if hour != self.settings["digest_hour_utc"] or day == self.digest_day:
            return
        text = self.digest_text()
        if not (quiet_when_empty and "all good" in text.split("\n")[0]):
            try:
                self.slack.post("digest", text)
            except OSError as exc:
                print(f"router: posting failed: {exc}", file=sys.stderr)
                self.failed = 1
                return
        self.events, self.digest_day = [], day

    def run(self, lines: list[str], silenced: bool, quiet_when_empty: bool) -> int:
        self.observe(lines)
        if silenced:  # pages wait for the silence to end; one-off events go to the digest
            for item, _ in self.oneoffs:
                self.record(item, "once")
        else:
            self.page_due()
            self.follow_up()
            self.digest(quiet_when_empty)
        self.save()
        return POST_FAILED if self.failed else 0


def main(argv: list[str]) -> int:
    """router.py <state dir> <title> <webhook file> [quiet]; observations on stdin."""
    state, title, webhook = Path(argv[1]), argv[2], Path(argv[3])
    now = float(os.environ.get("ATLAS_ALERTS_NOW") or time.time())
    silence = state / "silence"
    silenced = silence.exists() and float(silence.read_text().strip() or 0) > now
    router = Router(state, title, Slack(state, webhook, now), now)
    return router.run(sys.stdin.read().splitlines(), silenced, "quiet" in argv[4:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
