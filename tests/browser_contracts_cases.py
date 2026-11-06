"""Shared cases of the browser contract tests: the slot, the run id and a checksum."""

from datetime import UTC, datetime

SLOT = datetime(2026, 11, 4, 13, 0, tzinfo=UTC)
RUN_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
SHA = "a" * 64
