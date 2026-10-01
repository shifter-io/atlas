"""Streaming artifacts: the summary is the commit marker for a finished run."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Protocol

from .models import RunSummary


class ResultSink(Protocol):
    def start(self, run_id: str) -> None: ...
    def observations(self, rows: list[dict[str, Any]]) -> None: ...
    def finish(self, summary: RunSummary) -> None: ...
    def abort(self) -> None: ...


class JsonSink:
    """One exclusive directory per run; existing artifacts are never overwritten."""

    def __init__(self, output: str | Path = "results"):
        self.output = Path(output)
        self.directory: Path | None = None
        self._stream = None
        self._rows = 0

    def start(self, run_id: str) -> None:
        if self._stream is not None and not self._stream.closed:
            raise RuntimeError("A sink cannot serve concurrent runs")
        self._rows = 0
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

    def finish(self, summary: RunSummary) -> None:
        assert self._stream is not None and self.directory is not None
        self._stream.close()
        source = self.directory / "ips.jsonl.gz.partial"
        destination = self.directory / "ips.jsonl.gz"
        source.replace(destination)
        with destination.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        summary.artifacts = {
            "ips": {"file": destination.name, "rows": self._rows, "sha256": checksum},
        }
        temporary = self.directory / "summary.json.partial"
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(summary.to_dict(), stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.directory / "summary.json")

    def abort(self) -> None:
        if self._stream is not None:
            self._stream.close()
