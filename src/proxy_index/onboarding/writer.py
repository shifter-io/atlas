"""Bounded asynchronous intake, one disk writer, immutable gzip JSONL chunks."""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .files import atomic_json, digest, private_dir, sync_dir
from .manifest import timestamp


@dataclass(frozen=True)
class WriterBounds:
    """A chunk closes at ``chunk_rows`` rows or ``chunk_seconds`` seconds; the intake holds
    at most ``queue_size`` observations."""

    chunk_rows: int = 100_000
    chunk_seconds: float = 5.0
    queue_size: int = 8192


class ObservationWriter:
    def __init__(
        self,
        directory: Path,
        *,
        country: str,
        scheduled: str,
        started: str,
        deadline: float,
        bounds: WriterBounds | None = None,
    ):
        bounds = bounds or WriterBounds()
        if bounds.chunk_rows <= 0 or bounds.chunk_seconds <= 0 or bounds.queue_size <= 0:
            raise ValueError("Writer bounds must be positive")
        self.directory = directory
        self.context = {
            "country_code": country.lower(),
            "round_scheduled_at": scheduled,
            "country_started_at": started,
        }
        self.deadline = deadline
        self.chunk_rows, self.chunk_seconds = bounds.chunk_rows, bounds.chunk_seconds
        self.queue: queue.Queue = queue.Queue(maxsize=bounds.queue_size)
        self.error: BaseException | None = None
        self.rows = 0
        self.chunks = 0
        self.peak_queue = 0
        self._closing = threading.Event()
        self._done = threading.Event()
        private_dir(directory)
        if list(directory.glob("observations-*")):
            raise ValueError("A country observation directory cannot be reused")
        self._thread = threading.Thread(target=self._write, name="onboarding-export", daemon=True)
        self._thread.start()

    def check(self) -> None:
        if self.error is not None:
            raise OSError("Onboarding writer failed; finalized chunks were preserved") from None

    async def observe(self, ip: str, observed: float) -> None:
        if observed >= self.deadline:
            return
        while True:
            self.check()
            if self._closing.is_set():
                raise RuntimeError("Observation writer is closed")
            try:
                self.queue.put_nowait((ip, observed))
                self.peak_queue = max(self.peak_queue, self.queue.qsize())
                return
            except queue.Full:
                await asyncio.sleep(0.001)

    async def close(self) -> None:
        self._closing.set()
        # Wake an idle writer now rather than after its queue poll times out. A
        # full queue needs no wake-up: the writer stops once it has drained it.
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
        await asyncio.to_thread(self._done.wait)
        self._thread.join()
        self.check()

    def _write(self) -> None:
        chunk = _Chunk()
        try:
            while True:
                if self._closing.is_set() and self.queue.empty():
                    break
                try:
                    item = self.queue.get(timeout=0.1)
                except queue.Empty:
                    item = None
                if item is not None:
                    self._record(chunk, item)
                if chunk.due(self.chunk_rows, self.chunk_seconds):
                    self._commit(chunk)
            self._commit(chunk)
        except BaseException as exc:
            self.error = exc
        finally:
            # Failed/uncommitted chunks intentionally remain visibly partial.
            if chunk.zipped is not None:
                try:
                    chunk.zipped.close()
                except OSError:
                    pass
            try:
                if chunk.raw is not None:
                    chunk.raw.close()
            except OSError:
                pass
            finally:
                self._done.set()

    def _record(self, chunk: _Chunk, item: tuple[str, float]) -> None:
        if chunk.raw is None:
            chunk.open(self.directory / f"observations-{self.chunks + 1:06d}.jsonl.gz.partial")
        ip, observed = item
        chunk.write({"ip": ip, **self.context, "observed_at": timestamp(observed)})

    def _commit(self, chunk: _Chunk) -> None:
        if chunk.raw is None:
            return
        self.rows += chunk.commit(self.directory)
        self.chunks += 1


class _Chunk:
    """The writer's open ``.partial`` gzip chunk, renamed and given its sidecar on commit."""

    def __init__(self) -> None:
        self.raw = self.zipped = self.partial = None
        self.count = 0
        self.opened = 0.0

    def open(self, partial: Path) -> None:
        self.partial = partial
        fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        self.raw = os.fdopen(fd, "wb")
        self.zipped = gzip.GzipFile(
            filename="", fileobj=self.raw, mode="wb", mtime=0, compresslevel=1
        )
        self.opened = time.monotonic()

    def write(self, row: dict) -> None:
        self.zipped.write((json.dumps(row, separators=(",", ":")) + "\n").encode())
        self.count += 1

    def due(self, rows: int, seconds: float) -> bool:
        return self.raw is not None and (
            self.count >= rows or time.monotonic() - self.opened >= seconds
        )

    def commit(self, directory: Path) -> int:
        """Finalize the open chunk and its ``.meta.json`` sidecar; return its row count."""
        self.zipped.close()
        self.raw.flush()
        os.fsync(self.raw.fileno())
        self.raw.close()
        destination = self.partial.with_suffix("")
        self.partial.replace(destination)
        sync_dir(directory)
        atomic_json(
            destination.with_suffix(".meta.json"),
            {
                "schema_version": 1,
                "file": destination.name,
                "rows": self.count,
                "sha256": digest(destination),
            },
        )
        count = self.count
        self.raw = self.zipped = self.partial = None
        self.count = 0
        return count


class SummarySink:
    """Avoid normal ingestible artifacts; the scheduler persists its own summary."""

    def start(self, run_id):
        self.run_id = run_id

    def observations(self, rows):
        pass

    def geo(self, rows):
        pass

    def finish(self, summary):
        self.summary = summary

    def abort(self):
        pass
