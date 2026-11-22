"""Streaming artifacts: the summary is the commit marker for a finished run."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Protocol

from . import refusals as refused
from .models import RunSummary


class ResultSink(Protocol):
    def start(self, run_id: str) -> None: ...
    def observations(self, rows: list[dict[str, Any]]) -> None: ...
    # Called only when the run has an enricher; an empty list still creates the artifact.
    def geo(self, rows: list[dict[str, Any]]) -> None: ...
    def finish(self, summary: RunSummary) -> None: ...
    def abort(self) -> None: ...

    # Optional: `refusals(rows)` keeps the run's refused-answer samples (refusals.py), called
    # before finish only when there are some; a sink without it keeps none.


class JsonSink:
    """One exclusive directory per run; existing artifacts are never overwritten."""

    def __init__(self, output: str | Path = "results"):
        self.output = Path(output)
        self.directory: Path | None = None
        self._stream = None
        self._rows = 0
        self._geo_stream = None
        self._geo_rows = 0

    def start(self, run_id: str) -> None:
        if self._stream is not None and not self._stream.closed:
            raise RuntimeError("A sink cannot serve concurrent runs")
        self._rows = 0
        self._geo_stream = None
        self._geo_rows = 0
        self.directory = self.output / run_id
        self.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        path = self.directory / "ips.jsonl.gz.partial"
        self._stream = gzip.open(path, "wt", encoding="utf-8")
        path.chmod(0o600)

    def observations(self, rows: list[dict[str, Any]]) -> None:
        assert self._stream is not None
        for row in rows:
            self._stream.write(json.dumps(row, separators=(",", ":")) + "\n")
            self._rows += 1
        self._stream.flush()

    def geo(self, rows: list[dict[str, Any]]) -> None:
        assert self.directory is not None
        if self._geo_stream is None:
            path = self.directory / "geo.jsonl.gz.partial"
            self._geo_stream = gzip.open(path, "xt", encoding="utf-8")
            path.chmod(0o600)
        for row in rows:
            self._geo_stream.write(json.dumps(row, separators=(",", ":")) + "\n")
            self._geo_rows += 1
        self._geo_stream.flush()

    def refusals(self, rows: list[dict[str, Any]]) -> None:
        """refusals.FILE beside the IP file; it is not listed in the summary's artifacts."""
        assert self.directory is not None
        refused.write(self.directory, rows)

    def _publish(self, name: str, stream, rows: int) -> dict[str, Any]:
        stream.close()
        assert self.directory is not None
        destination = self.directory / f"{name}.jsonl.gz"
        (self.directory / f"{name}.jsonl.gz.partial").replace(destination)
        with destination.open("rb") as source:
            checksum = hashlib.file_digest(source, "sha256").hexdigest()
        return {"file": destination.name, "rows": rows, "sha256": checksum}

    def finish(self, summary: RunSummary) -> None:
        assert self._stream is not None and self.directory is not None
        # The runner may pre-fill descriptive keys (the geo DB-IP release);
        # file, rows and checksum always come from the sink.
        given = summary.artifacts
        artifacts = {"ips": self._publish("ips", self._stream, self._rows)}
        if self._geo_stream is not None:
            artifacts["geo"] = given.get("geo", {}) | self._publish(
                "geo",
                self._geo_stream,
                self._geo_rows,
            )
        summary.artifacts = artifacts
        temporary = self.directory / "summary.json.partial"
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(summary.to_dict(), stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.directory / "summary.json")

    def abort(self) -> None:
        for stream in (self._stream, self._geo_stream):
            if stream is not None:
                stream.close()
