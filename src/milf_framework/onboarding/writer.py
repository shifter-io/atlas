"""Bounded asynchronous intake, one disk writer, immutable gzip JSONL chunks."""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import queue
import threading
import time
from pathlib import Path

from .files import atomic_json, digest, private_dir, sync_dir
from .manifest import timestamp


class ObservationWriter:
    def __init__(
        self,
        directory: Path,
        *,
        country: str,
        scheduled: str,
        started: str,
        deadline: float,
        chunk_rows: int = 100_000,
        chunk_seconds: float = 5.0,
        queue_size: int = 8192,
    ):
        if chunk_rows <= 0 or chunk_seconds <= 0 or queue_size <= 0:
            raise ValueError("Writer bounds must be positive")
        self.directory = directory
        self.context = {
            "country_code": country.lower(),
            "round_scheduled_at": scheduled,
            "country_started_at": started,
        }
        self.deadline = deadline
        self.chunk_rows, self.chunk_seconds = chunk_rows, chunk_seconds
        self.queue: queue.Queue = queue.Queue(maxsize=queue_size)
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
        await asyncio.to_thread(self._done.wait)
        self._thread.join()
        self.check()

    def _write(self) -> None:
        raw = zipped = None
        partial = None
        count = 0
        opened = 0.0

        def commit() -> None:
            nonlocal raw, zipped, partial, count
            if raw is None:
                return
            zipped.close()
            raw.flush()
            os.fsync(raw.fileno())
            raw.close()
            destination = partial.with_suffix("")
            partial.replace(destination)
            sync_dir(self.directory)
            atomic_json(
                destination.with_suffix(".meta.json"),
                {
                    "schema_version": 1,
                    "file": destination.name,
                    "rows": count,
                    "sha256": digest(destination),
                },
            )
            self.chunks += 1
            self.rows += count
            raw = zipped = partial = None
            count = 0

        try:
            while True:
                if self._closing.is_set() and self.queue.empty():
                    break
                try:
                    item = self.queue.get(timeout=0.1)
                except queue.Empty:
                    item = None
                if item is not None:
                    if raw is None:
                        partial = self.directory / (
                            f"observations-{self.chunks + 1:06d}.jsonl.gz.partial"
                        )
                        fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                        raw = os.fdopen(fd, "wb")
                        zipped = gzip.GzipFile(
                            filename="", fileobj=raw, mode="wb", mtime=0, compresslevel=1
                        )
                        opened = time.monotonic()
                    ip, observed = item
                    row = {"ip": ip, **self.context, "observed_at": timestamp(observed)}
                    zipped.write((json.dumps(row, separators=(",", ":")) + "\n").encode())
                    count += 1
                if raw is not None and (
                    count >= self.chunk_rows or time.monotonic() - opened >= self.chunk_seconds
                ):
                    commit()
            commit()
        except BaseException as exc:
            self.error = exc
        finally:
            # Failed/uncommitted chunks intentionally remain visibly partial.
            if zipped is not None:
                try:
                    zipped.close()
                except OSError:
                    pass
            try:
                if raw is not None:
                    raw.close()
            except OSError:
                pass
            finally:
                self._done.set()


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
