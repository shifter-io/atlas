"""NO-EXIT-404-1: a gateway's "no exit in this country" 404 on the echo targets is not measured.

The owner's rule (2026-11-23), the same for every network: on the targets of the methodology's
no_exit_answers (generic_http and generic_https), a 404 the network's gateway sends by itself
means it has no exit in the requested country. Such an answer is not a try: it is left out of
the tries, successes and failures, the success rate, the Success x/10, the block and challenge
rates, the error categories, the pulse slots (uptime, slot coverage), detection (no down or
success_drop, no baseline), the pulse evaluator's windows and the outage cleanup's pulses. A
404 on any other target (the browser targets) stays a failure.

Since this rule the transport records the gateway's 404 as outcome.NO_EXIT and any other 404 of
an echo target (inside the tunnel, or with a body: an exit device's own page, an ISP's block
page, a page on the path) as outcome.TARGET_404, a failure under its own code
(outcome.echo_404). The setting's `scope` says which 404s are left out (SCOPES; the owner's
choice is one setting):

- "gateway" (the default): the gateway's own. A bucket or run summary recorded since the rule
  leaves out exactly its no_exit answers. One stored before it has neither code: it leaves out
  its 404s answered to a CONNECT (as many as its proxy_connect errors) and its plain 404s (as
  many as its http_status errors) when they came with no body: its body bytes, less ANSWER_BYTES
  per success, are under EMPTY_BYTES per such 404 (the gateways' no-exit 404 is about 90 bytes
  on the wire with an empty body; the other 404s carry pages of about 100 to 7,400 bytes).
- "every": every 404 of the targets, whoever sent it.

split() gives the left-out answers' error codes (STRIPPED order). A row without status codes
(a pulse copy made before the rule, rollups.py) leaves out the codes recorded since the rule; a
recount copies every hour again, so one recount applies the rule to the whole stored window. No
stored row is edited.
"""

from __future__ import annotations

from functools import cache

from .outcome import NO_EXIT, NO_EXIT_STATUS, TARGET_404

STATUS = str(NO_EXIT_STATUS)
SCOPES = ("gateway", "every")
ECHO = frozenset({"generic_http", "generic_https"})
EMPTY_BYTES = 20  # a stored plain 404 is the gateway's under this many body bytes
ANSWER_BYTES = 16  # an echo success's body at most: an IPv4 address and a newline
# The error codes a left-out 404 can be recorded under, in the order its 404s are given to them:
# a stored row's 404s go to its http_status and body_too_large answers (a plain answer) before
# its proxy_connect ones (a refused CONNECT), so a 404 page never takes a real connection
# failure's place; those the transport counts as proxy errors.
STRIPPED = (NO_EXIT, TARGET_404, "http_status", "body_too_large", "proxy_connect")
PROXY = (NO_EXIT, "proxy_connect")
INVALID = "Invalid no_exit_answers"


def rule(config) -> tuple[frozenset[str], str]:
    """The methodology's no_exit_answers: its targets (none without the setting) and scope;
    ValueError on an invalid one. Only the echo targets can have the gateway's own 404."""
    section = config.get("no_exit_answers", {})
    if not isinstance(section, dict) or set(section) - {"targets", "scope"}:
        raise ValueError(INVALID)
    found, scope = section.get("targets", []), section.get("scope", SCOPES[0])
    if not isinstance(found, list) or not set(found) <= ECHO or len(set(found)) != len(found):
        raise ValueError(INVALID)
    if scope not in SCOPES:
        raise ValueError(INVALID)
    return frozenset(found), scope


def targets(config) -> frozenset[str]:
    return rule(config)[0]


def split(counters: dict, scope: str = SCOPES[0]) -> dict[str, int]:
    """A bucket's or run summary's left-out 404s per error code (module docstring)."""
    errors = counters.get("errors") or {}

    def error(code):
        return int(errors.get(code) or 0)

    statuses = counters.get("status_codes")
    if statuses is None:
        codes = (NO_EXIT, TARGET_404) if scope == "every" else (NO_EXIT,)
        return {code: error(code) for code in codes if error(code)}
    left, parts = int(statuses.get(STATUS) or 0), {}
    for code in STRIPPED:
        parts[code] = min(left, error(code))
        left -= parts[code]
    if scope == "gateway" and (error(NO_EXIT) or error(TARGET_404)):  # since the rule: exact
        parts = {NO_EXIT: parts[NO_EXIT]}
    elif scope == "gateway":  # stored before it: a CONNECT's, and a plain one without a body
        plain = parts["http_status"]
        body = int(counters.get("response_body_bytes") or 0)
        body -= ANSWER_BYTES * int(counters.get("successes") or 0)
        empty = plain if body < EMPTY_BYTES * plain else 0
        parts = {"http_status": empty, "proxy_connect": parts["proxy_connect"]}
    return {k: v for k, v in parts.items() if v}


def count(counters: dict, scope: str = SCOPES[0]) -> int:
    """How many of a bucket's or run summary's answers are left out (split())."""
    return sum(split(counters, scope).values())


def strip(counters: dict, parts: dict[str, int]) -> dict:
    """The counters without the left-out 404s of split(): fewer completed tries, those error
    codes and, for those the transport called proxy errors (PROXY), fewer proxy errors."""
    errors = {k: v - parts.get(k, 0) for k, v in counters["errors"].items()}
    found = {"completed": counters["completed"] - sum(parts.values())}
    found["errors"] = {k: v for k, v in errors.items() if v}
    if "proxy_errors" in counters:
        proxy = sum(parts.get(code, 0) for code in PROXY)
        found["proxy_errors"] = max(0, counters["proxy_errors"] - proxy)
    return counters | found


def exclude(config, runs, buckets):
    """The calculation's bucket rows without the gateway's 404s of the rule's targets, and the
    answers left out per run key. A row left with no try is dropped."""
    wanted, scope = rule(config)
    keys = {r["run_key"] for r in runs if r["scope"]["target"]["id"] in wanted}
    kept, gone = [], {}
    for b in buckets:
        parts = split(b["counters"], scope) if b["run_key"] in keys else {}
        n = sum(parts.values())
        if n:
            gone[b["run_key"]] = gone.get(b["run_key"], 0) + n
            b = b | {"counters": strip(b["counters"], parts)}
        if b["counters"]["completed"] > 0 or not n:
            kept.append(b)
    return kept, gone


def slots(pulses, gone):
    """The pulse slots (scheduled pulse runs) with their runs' left-out answers taken from their
    requests; a slot left with no request was not measured and is dropped."""
    found = []
    for p in pulses:
        n = gone.get(p["run_key"], 0)
        if n and p["requests"] - n <= 0:
            continue
        found.append(p | {"requests": p["requests"] - n} if n else p)
    return found


def sql(row: str, target: str, proxy: bool = False) -> str:
    """split() in SQL over a row of the live rule (live()) of a target, summed (proxy: only its
    PROXY codes), 0 for a target the rule does not name. row: "r.summary" (a run summary, jsonb)
    or a run_buckets alias."""
    names, scope = live()
    summary = row.endswith("summary")

    def code(column, key):
        field = f"{row}->'{column}'" if summary else f"{row}.{column}"
        return f"coalesce(({field}->>'{key}')::bigint,0)"

    def number(key):
        return f"coalesce(({row}->>'{key}')::bigint,0)" if summary else f"{row}.{key}"

    found, before, parts = code("status_codes", STATUS), "0", {}
    for key in STRIPPED:  # each code's share of the 404s, as split() gives them
        parts[key] = f"greatest(0,least({code('errors', key)},{found}-({before})))"
        before += f"+{code('errors', key)}"
    body = f"{number('response_body_bytes')}-{ANSWER_BYTES}*{number('successes')}"
    plain = parts["http_status"]
    empty = f"CASE WHEN {body}<{EMPTY_BYTES}*{plain} THEN {plain} ELSE 0 END"
    kept = ("proxy_connect",) if proxy else ("http_status", "proxy_connect")
    if scope == "every":
        left = "+".join(parts[k] for k in (PROXY if proxy else STRIPPED))
    else:
        older = "+".join(empty if k == "http_status" else parts[k] for k in kept)
        recorded = f"{code('errors', NO_EXIT)}>0 OR {code('errors', TARGET_404)}>0"
        left = f"CASE WHEN {recorded} THEN {parts[NO_EXIT]} ELSE {older} END"
    listed = ",".join(sorted(names))
    return f"(CASE WHEN {target}=ANY('{{{listed}}}'::text[]) THEN {left} ELSE 0 END)"


@cache
def live() -> tuple[frozenset[str], str]:
    """The rule in the methodology file the live detection reads (detect.py)."""
    from .calculated import policy
    from .regions import CONFIGS

    return rule(policy.read(CONFIGS / "methodology.toml"))


def run_sql() -> str:
    """A batch run's left-out 404s from its run summary (r: test_runs, b: test_batches)."""
    return sql("r.summary", "b.target_id")


def measured(run: dict) -> dict:
    """A batch run (detect's RUNS row) with its gateway 404s (`not_measured`, run_sql()) taken
    from its requests: they are no tries."""
    n = int(run.get("not_measured") or 0)
    return run | {"requests": run["requests"] - n} if n else run
