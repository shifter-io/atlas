"""NO-EXIT-404-1: a gateway's "no exit in this country" 404 on the echo targets is not measured.

The owner's rule (2026-11-23), the same for every network: on the targets of the methodology's
[no_exit_answers] (generic_http and generic_https), a 404 the network's gateway sends by itself
means it has no exit in the requested country. Such an answer is not a try: it is left out of
the tries, successes and failures, the success rate, the Success x/10, the block and challenge
rates, the error categories, the pulse slots (uptime, slot coverage), detection (no down or
success_drop, no baseline), the pulse evaluator's windows and the outage cleanup's pulses. A
404 on any other target (the browser targets) stays a failure.

Since this rule the transport records the gateway's 404 as outcome.NO_EXIT and any other 404 of
an echo target (inside the tunnel, or with a body: an exit device's own page, an ISP's block
page) as outcome.TARGET_404, a failure under its own code (outcome.echo_404). A bucket or run
summary then leaves out

    min(its 404s - its target_404 answers, its no_exit + proxy_connect + http_status answers)

which is exactly its no_exit answers. A row stored before the rule has neither code: every 404
it answered as a proxy_connect (a CONNECT refused) or an http_status (a plain answer) is left
out, a 404 counted under another code (body_too_large: a large block page) is kept. The left-out
answers leave the error categories in GATEWAY order. A row without status codes (a pulse copy
made before the rule, rollups.py) leaves out its no_exit answers; a recount copies every hour
again, so one recount applies the rule to the whole stored window. No stored row is edited.
"""

from __future__ import annotations

from functools import cache

from .outcome import NO_EXIT, NO_EXIT_STATUS, TARGET_404

STATUS = str(NO_EXIT_STATUS)
# The error codes a gateway's 404 can be recorded under, in the order they give it up.
GATEWAY = (NO_EXIT, "proxy_connect", "http_status")
INVALID = "Invalid no_exit_answers"


def targets(config) -> frozenset[str]:
    """The methodology's [no_exit_answers] targets (none without the setting); ValueError on an
    invalid one. Only the echo targets can have the gateway's own 404."""
    section = config.get("no_exit_answers", {})
    if not isinstance(section, dict) or set(section) - {"targets"}:
        raise ValueError(INVALID)
    found = section.get("targets", [])
    echo = {"generic_http", "generic_https"}
    if not isinstance(found, list) or not set(found) <= echo or len(set(found)) != len(found):
        raise ValueError(INVALID)
    return frozenset(found)


def count(counters: dict) -> int:
    """How many of a bucket's or run summary's answers are the gateway's 404 (module docstring)."""
    errors = counters.get("errors") or {}
    gateway = sum(int(errors.get(code) or 0) for code in GATEWAY)
    statuses = counters.get("status_codes")
    if statuses is None:
        return int(errors.get(NO_EXIT) or 0)
    found = int(statuses.get(STATUS) or 0) - int(errors.get(TARGET_404) or 0)
    return max(0, min(found, gateway))


def strip(counters: dict, n: int) -> dict:
    """The counters without n of the gateway's 404s: fewer completed tries, their error codes
    (GATEWAY order) and, for those the transport called proxy errors (no_exit, proxy_connect),
    fewer proxy errors."""
    errors, left, proxy = dict(counters["errors"]), n, 0
    for code in GATEWAY:
        taken = min(left, int(errors.get(code) or 0))
        if taken:
            errors[code] -= taken
            errors = {k: v for k, v in errors.items() if v}
            left -= taken
            proxy += taken if code != "http_status" else 0
    found = counters | {"completed": counters["completed"] - n, "errors": errors}
    if "proxy_errors" in counters:
        found["proxy_errors"] = max(0, counters["proxy_errors"] - proxy)
    return found


def exclude(config, runs, buckets):
    """The calculation's bucket rows without the gateway's 404s of the rule's targets, and the
    answers left out per run key. A row left with no try is dropped."""
    wanted = targets(config)
    keys = {r["run_key"] for r in runs if r["scope"]["target"]["id"] in wanted}
    kept, gone = [], {}
    for b in buckets:
        n = count(b["counters"]) if b["run_key"] in keys else 0
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


def sql(statuses: str, errors: str, target: str) -> str:
    """count() in SQL over a row's status codes and errors (jsonb) of a target, 0 for a target
    the rule does not name."""
    names = ",".join(sorted(live()))

    def code(column, key):
        return f"coalesce(({column}->>'{key}')::bigint,0)"

    gateway = "+".join(code(errors, key) for key in GATEWAY)
    found = f"{code(statuses, STATUS)}-{code(errors, TARGET_404)}"
    return (
        f"(CASE WHEN {target}=ANY('{{{names}}}'::text[]) "
        f"THEN greatest(0,least({found},{gateway})) ELSE 0 END)"
    )


@cache
def live() -> frozenset[str]:
    """The rule's targets in the methodology file the live detection reads (detect.py)."""
    from .calculated import policy
    from .regions import CONFIGS

    return targets(policy.read(CONFIGS / "methodology.toml"))


def run_sql() -> str:
    """A batch run's gateway 404s from its run summary (r: test_runs, b: test_batches)."""
    return sql("r.summary->'status_codes'", "r.summary->'errors'", "b.target_id")


def measured(run: dict) -> dict:
    """A batch run (detect's RUNS row) with its gateway 404s (`not_measured`, run_sql()) taken
    from its requests: they are no tries."""
    n = int(run.get("not_measured") or 0)
    return run | {"requests": run["requests"] - n} if n else run
