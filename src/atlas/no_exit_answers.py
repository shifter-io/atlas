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

The left-out answers leave the error categories in STRIPPED order. A row without status codes
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
# The error codes a left-out 404 can be recorded under, in the order they give it up; those the
# transport counts as proxy errors.
STRIPPED = (NO_EXIT, TARGET_404, "proxy_connect", "http_status", "body_too_large")
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


def count(counters: dict, scope: str = SCOPES[0]) -> int:
    """How many of a bucket's or run summary's answers are left out (module docstring)."""
    errors = counters.get("errors") or {}

    def error(code):
        return int(errors.get(code) or 0)

    statuses = counters.get("status_codes")
    if statuses is None:
        return error(NO_EXIT) + (error(TARGET_404) if scope == "every" else 0)
    found = int(statuses.get(STATUS) or 0)
    if scope == "every":
        return found
    if error(NO_EXIT) or error(TARGET_404):  # recorded since the rule: exact
        return error(NO_EXIT)
    connect = min(found, error("proxy_connect"))
    plain = min(found - connect, error("http_status"))
    body = int(counters.get("response_body_bytes") or 0)
    body -= ANSWER_BYTES * int(counters.get("successes") or 0)
    return connect + (plain if body < EMPTY_BYTES * plain else 0)


def strip(counters: dict, n: int) -> dict:
    """The counters without n left-out 404s: fewer completed tries, their error codes (STRIPPED
    order) and, for those the transport called proxy errors (PROXY), fewer proxy errors."""
    errors, left, proxy = dict(counters["errors"]), n, 0
    for code in STRIPPED:
        taken = min(left, int(errors.get(code) or 0))
        if taken:
            errors[code] -= taken
            errors = {k: v for k, v in errors.items() if v}
            left -= taken
            proxy += taken if code in PROXY else 0
    found = counters | {"completed": counters["completed"] - n, "errors": errors}
    if "proxy_errors" in counters:
        found["proxy_errors"] = max(0, counters["proxy_errors"] - proxy)
    return found


def exclude(config, runs, buckets):
    """The calculation's bucket rows without the gateway's 404s of the rule's targets, and the
    answers left out per run key. A row left with no try is dropped."""
    wanted, scope = rule(config)
    keys = {r["run_key"] for r in runs if r["scope"]["target"]["id"] in wanted}
    kept, gone = [], {}
    for b in buckets:
        n = count(b["counters"], scope) if b["run_key"] in keys else 0
        if n:
            gone[b["run_key"]] = gone.get(b["run_key"], 0) + n
            b = b | {"counters": strip(b["counters"], n)}
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


def sql(row: str, target: str) -> str:
    """count() in SQL over a row of the live rule (live()) of a target, 0 for a target the rule
    does not name. row: "r.summary" (a run summary, jsonb) or a run_buckets alias."""
    names, scope = live()
    summary = row.endswith("summary")

    def code(column, key):
        field = f"{row}->'{column}'" if summary else f"{row}.{column}"
        return f"coalesce(({field}->>'{key}')::bigint,0)"

    def number(key):
        return f"coalesce(({row}->>'{key}')::bigint,0)" if summary else f"{row}.{key}"

    found, no_exit = code("status_codes", STATUS), code("errors", NO_EXIT)
    connect = f"least({found},{code('errors', 'proxy_connect')})"
    plain = f"least({found}-{connect},{code('errors', 'http_status')})"
    body = f"{number('response_body_bytes')}-{ANSWER_BYTES}*{number('successes')}"
    gateway = (
        f"CASE WHEN {no_exit}>0 OR {code('errors', TARGET_404)}>0 THEN {no_exit} "
        f"ELSE {connect}+CASE WHEN {body}<{EMPTY_BYTES}*{plain} THEN {plain} ELSE 0 END END"
    )
    left = found if scope == "every" else gateway
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
