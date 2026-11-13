"""CONC-1: `campaign probe` step results and the step-down rule.

Each step's run summary is classified into shares of completed requests, and the step-down
rule compares a step with the floor; runs on a probe worker and the per-network concurrency
cap are in test_campaign_probe_runs.py.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from campaign_probe_cases import IP, classified, summary

from atlas.campaign import probe as probing
from atlas.campaign.probe import ProbeInputs, reasons, step_result


def test_step_result_classifies_every_failure_as_a_share_of_completed_requests():
    found = step_result(
        2000,
        25_000,
        summary(
            20_000,
            statuses={"429": 400, "469": 100, "502": 50, "503": 30, "504": 20},
            errors={"proxy_connect": 300, "connection": 100, "timeout": 200, "tls": 40},
        ),
    )
    assert found == {
        "concurrency": 2000,
        "requested": 25_000,
        "completed": 20_000,
        "success_pct": 93.8,
        "rate_limited_pct": 2.5,
        "gateway_5xx_pct": 0.5,
        "proxy_connect_pct": 2.0,
        "timeout_pct": 1.0,
        "other_pct": 0.2,
        "rps": 900.0,
        "latency_p50_ms": 820,
        "latency_p95_ms": 2400,
        "unique_ips": 24_000,
        "stop_reason": None,
        # CTRY-1 keys; this summary has no answer classes, so nothing is answered away.
        "answered": 20_000,
        "no_inventory_pct": 0.0,
        "other_country_pct": None,
        "duplicates_pct": 0.0,
        "real_error_pct": 3.7,
    }
    assert not IP.search(json.dumps(found))
    empty = step_result(500, 25_000, summary(0, successes=0) | {"latency_ms": None})
    assert empty["success_pct"] == 0.0 and empty["latency_p50_ms"] is None


def test_step_result_answer_classes():
    """CTRY-1: rate limits come from the runner's signals (the CONNECT refusals among them
    leave proxy_connect), no-IP answers leave other, and real errors are of answered."""
    found = step_result(500, 1000, classified())
    assert {
        k: found[k]
        for k in (
            "completed",
            "answered",
            "success_pct",
            "rate_limited_pct",
            "gateway_5xx_pct",
            "proxy_connect_pct",
            "timeout_pct",
            "no_inventory_pct",
            "other_pct",
            "other_country_pct",
            "duplicates_pct",
            "real_error_pct",
        )
    } == {
        "completed": 1000,
        "answered": 950,
        "success_pct": 80.0,
        "rate_limited_pct": 5.0,
        "gateway_5xx_pct": 1.0,
        "proxy_connect_pct": 2.5,
        "timeout_pct": 2.0,
        "no_inventory_pct": 0.5,
        "other_pct": 9.0,
        "other_country_pct": 1.0,
        "duplicates_pct": 9.0,
        "real_error_pct": 15.263,
    }
    unchecked = step_result(500, 1000, classified(other_country=None, unique_ips=None))
    assert (unchecked["other_country_pct"], unchecked["duplicates_pct"]) == (None, None)
    assert step_result(500, 0, classified(completed=0, rate_limited={}))["real_error_pct"] == 0.0


def test_step_result_unchanged_for_old_summaries():
    """A CONC-1-era summary (no answer classes) keeps every share it had before CTRY-1."""
    from atlas.models import RunSummary

    old = RunSummary(
        run_id="r",
        schema_version="1",
        framework_version="x",
        target={},
        config={},
        started_at="",
        finished_at="",
        duration_seconds=10.0,
        status="completed",
        requested=1000,
        started=1000,
        completed=1000,
        cancelled=0,
        not_started=0,
        successes=900,
        total_errors=100,
        errors={"http_status": 40, "proxy_connect": 25, "timeout": 20, "tls": 15},
        status_codes={"200": 900, "429": 30, "503": 10},
        success_rate=0.9,
        requests_per_second=100.0,
        unique_ips=850,
        pool_eligible=True,
        latency_ms={"p50": 700, "p90": 1500, "p95": 1900, "p99": 3000},
        latency_histogram={},
        response_body_bytes=0,
        buckets=[],
    ).to_dict()
    assert step_result(1000, 1000, old) == {
        "concurrency": 1000,
        "requested": 1000,
        "completed": 1000,
        "success_pct": 90.0,
        "rate_limited_pct": 3.0,
        "gateway_5xx_pct": 1.0,
        "proxy_connect_pct": 2.5,
        "timeout_pct": 2.0,
        "other_pct": 1.5,
        "rps": 100.0,
        "latency_p50_ms": 700,
        "latency_p95_ms": 1900,
        "unique_ips": 850,
        "stop_reason": None,
        "answered": 1000,
        "no_inventory_pct": 0.0,
        "other_country_pct": None,
        "duplicates_pct": 5.0,
        "real_error_pct": 7.0,
    }


def test_incomplete_counts_answered_requests():
    """The goal is answered requests: 24,000 completed with 400 rate-limited is 23,600
    answered, below 95% of 25,000; without answer classes it is complete."""
    base = step_result(500, 25_000, summary())
    short = summary(24_000, statuses={"429": 400}) | {"rate_limited": {"429": 400}}
    assert reasons(step_result(2000, 25_000, short), base) == ["incomplete"]
    assert reasons(step_result(2000, 25_000, summary(24_000, statuses={"429": 400})), base) == []
    enough = summary(25_400, statuses={"429": 400}) | {"rate_limited": {"429": 400}}
    assert reasons(step_result(2000, 25_000, enough), base) == []


def test_run_command_counts_answered_requests():
    argv = probing.run_command("geonode", 500, 25_000, "US", "nyc3")
    timeout = argv.index("--timeout")
    assert argv[timeout + 1 : timeout + 3] == [str(probing.TIMEOUT_SECONDS), "--answer-classes"]
    assert argv[argv.index("--provider-config") + 1].endswith("/probe/geonode.toml")


@pytest.mark.parametrize("count", [0, 1, 2])
def test_a_step_adds_the_country_check_only_with_one_image_database(tmp_path, count):
    """CTRY-2: `--mmdb` is appended when the image holds exactly one MMDB."""
    import subprocess

    databases = tmp_path / "db-ip"
    databases.mkdir()
    for index in range(count):
        (databases / f"dbip-{index}.mmdb").write_bytes(b"x")
    reply = subprocess.run(
        ["sh", "-c", probing.launcher(str(databases)), "printf", "%s\n", "run", "--answer-classes"],
        capture_output=True,
        text=True,
        check=True,
    )
    extra = ["--mmdb", str(databases / "dbip-0.mmdb")] if count == 1 else []
    assert reply.stdout.splitlines() == ["run", "--answer-classes", *extra]
    assert probing.run_command("soax", 500, 25_000, "US", "nyc3")[2] == probing.launcher(
        "/opt/proxy-index-worker/db-ip"
    )


def test_the_step_down_rule():
    base = step_result(500, 25_000, summary(errors={"timeout": 100}))  # 0.4% timeouts
    assert reasons(base, base) == []
    assert reasons({"run_failed": "RemoteError"}, base) == ["run_failed"]
    cases = {
        "rate_limited": summary(statuses={"429": 501}),  # 2.004%
        "proxy_connect": summary(errors={"proxy_connect": 400, "connection": 101}),
        "incomplete": summary(23_000),
        # 0.4% at the floor: up to max(0.8, 1.4) = 1.4% is noise, above it a slowdown.
        "timeouts_5xx": summary(statuses={"504": 100}, errors={"timeout": 276}),
    }
    for reason, run in cases.items():
        assert reasons(step_result(2000, 25_000, run), base) == [reason]
    assert reasons(step_result(2000, 25_000, summary(statuses={"429": 500})), base) == []
    assert (
        reasons(
            step_result(2000, 25_000, summary(statuses={"504": 100}, errors={"timeout": 250})), base
        )
        == []
    )
    high = step_result(500, 25_000, summary(errors={"timeout": 2500}))  # 10% at the floor
    assert reasons(step_result(2000, 25_000, summary(errors={"timeout": 3250})), high) == []
    assert reasons(step_result(2000, 25_000, summary(errors={"timeout": 5001})), high) == [
        "timeouts_5xx",
        "success_drop",
    ]


def test_s2_every_failure_class_and_a_success_drop_count():
    """S2: 407s, DNS, TLS, protocol errors, other statuses and blocks can no longer pass."""
    base = step_result(500, 25_000, summary(errors={"tls": 50}))  # 0.2% other at the floor
    cases = {
        # The reviewer's case: 30% 407, 4% DNS, 4% protocol errors.
        ("proxy_connect", "other_errors", "success_drop"): summary(
            errors={"proxy_auth": 7500, "dns": 1000, "protocol": 1000}
        ),
        ("proxy_connect",): summary(errors={"proxy_auth": 501}),  # a 407 storm
        ("other_errors",): summary(errors={"protocol": 150, "http_status": 151}),  # 1.204%
        ("other_errors", "success_drop"): summary(errors={"blocked": 450, "challenge": 450}),
        # Every class under its own limit, yet 5.5 points less success than the baseline.
        ("success_drop",): summary(
            statuses={"429": 475}, errors={"proxy_connect": 475, "timeout": 240, "tls": 225}
        ),
    }
    for expected, run in cases.items():
        assert reasons(step_result(2000, 25_000, run), base) == list(expected), expected
    assert reasons(step_result(2000, 25_000, summary(errors={"tls": 299})), base) == []
    assert (
        step_result(2000, 25_000, summary(errors={"proxy_auth": 5, "dns": 5}))["proxy_connect_pct"]
        == 0.04
    )


def test_the_ladder_steps_down_to_just_above_the_floor(tmp_path):
    inputs = ProbeInputs(tmp_path, tmp_path)
    assert inputs.ladder() == [2000, 1500, 1000]
    assert replace(inputs, start=1200, step=300).ladder() == [1200, 900, 600]
    assert replace(inputs, start=500).ladder() == []


def test_m7_a_real_run_summary_is_classified():
    """m7: built by the runner's own RunSummary, so a schema change breaks this test."""
    from atlas.models import RunSummary

    real = RunSummary(
        run_id="r",
        schema_version="1",
        framework_version="x",
        target={},
        config={},
        started_at="",
        finished_at="",
        duration_seconds=10.0,
        status="completed",
        requested=1000,
        started=1000,
        completed=1000,
        cancelled=0,
        not_started=0,
        successes=900,
        total_errors=100,
        errors={"http_status": 40, "proxy_connect": 25, "timeout": 20, "tls": 15},
        status_codes={"200": 900, "429": 30, "503": 10},
        success_rate=0.9,
        requests_per_second=100.0,
        unique_ips=850,
        pool_eligible=True,
        latency_ms={"p50": 700, "p90": 1500, "p95": 1900, "p99": 3000},
        latency_histogram={},
        response_body_bytes=0,
        buckets=[],
    ).to_dict()
    found = step_result(1000, 1000, real)
    assert (
        found["success_pct"],
        found["rate_limited_pct"],
        found["gateway_5xx_pct"],
        found["proxy_connect_pct"],
        found["timeout_pct"],
        found["other_pct"],
    ) == (90.0, 3.0, 1.0, 2.5, 2.0, 1.5)
    assert (found["rps"], found["latency_p50_ms"], found["latency_p95_ms"]) == (100.0, 700, 1900)
