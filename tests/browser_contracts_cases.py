"""Shared cases of the browser contract tests: the slot, the run id, a checksum and the fields of
a valid try record."""

from datetime import UTC, datetime

SLOT = datetime(2026, 11, 4, 13, 0, tzinfo=UTC)
RUN_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
SHA = "a" * 64


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
