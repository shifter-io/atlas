"""CPU-parallel HTTP execution with one authoritative runner/coordinator.

Private inherited socket pairs carry bounded request/result messages. Workers
never schedule replacement requests, count unique IPs, or write result files.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp
import os
import pickle
import signal
import socket
import struct
import time
from dataclasses import replace

from .transport import HttpTransport

log = logging.getLogger(__name__)


def process_count(config):
    available = (
        len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    )
    return min(
        config.processes or available,
        config.concurrency,
        config.requests if config.requests is not None else config.concurrency,
    )


async def receive(reader):
    size = struct.unpack("!I", await reader.readexactly(4))[0]
    if size > 1024 * 1024:
        raise RuntimeError("Invalid worker message")
    # Only our own spawned child/parent can access this private socket.
    return pickle.loads(await reader.readexactly(size))


def send(writer, message):
    data = pickle.dumps(message, protocol=5)
    writer.write(struct.pack("!I", len(data)) + data)


async def _respond(transport, target, context, deadline):
    """The worker's answer to one request: its result, or "expired" past the deadline."""
    if deadline is not None and time.perf_counter() >= deadline:
        return ("expired", context.request_index)
    remaining = max(0, deadline - time.perf_counter()) if deadline is not None else None
    async with asyncio.timeout(remaining):
        result = await transport.execute(target, context=context)
    return ("result", context.request_index, result, time.perf_counter())


async def _dispatch(reader, execute):
    """Run `execute` for each received request until the coordinator stops or leaves."""
    tasks = set()
    try:
        while True:
            context = await receive(reader)
            if context is None:
                break
            task = asyncio.create_task(execute(context))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    except (EOFError, ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _serve(sock, config, target, factory, deadline):
    reader, writer = await asyncio.open_connection(sock=sock)
    slots = asyncio.Semaphore(config.concurrency)
    async with factory(config) as transport:
        send(writer, ("ready", os.getpid()))
        await writer.drain()

        async def execute(context):
            try:
                async with slots:
                    send(writer, await _respond(transport, target, context, deadline))
                await writer.drain()
            except TimeoutError:
                send(writer, ("expired", context.request_index))
                await writer.drain()
            except Exception as exc:  # boundary: one failed request must not stop the worker
                # Do not serialize or log exception strings that might contain credentials.
                log.warning("HTTP worker request failed (%s)", type(exc).__name__)
                send(writer, ("error", context.request_index))
                await writer.drain()

        await _dispatch(reader, execute)
    writer.close()
    await writer.wait_closed()


def _entry(sock, config, target, factory, deadline):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        asyncio.run(_serve(sock, config, target, factory, deadline))
    except Exception as exc:  # boundary: a worker process exits with a status, not a traceback
        # Parent diagnoses an EOF as a failed worker. Never print config/tracebacks.
        log.error("HTTP worker stopped (%s)", type(exc).__name__)
        raise SystemExit(1) from None
    finally:
        sock.close()


class ProcessTransport:
    """Each fixed concurrency slot belongs to one independently executing process."""

    def __init__(self, config, target, *, factory=HttpTransport, deadline=None):
        self.config, self.target, self.factory = config, target, factory
        self.deadline = deadline
        self.count = process_count(config)
        self.children = []
        self.writers = []
        self.readers = []
        self.pending = {}
        self.failure = False
        self.pids = []
        self.aborted = False

    def abort(self):
        """Immediate network stop, independent of the coordinator's output drain."""
        if not self.aborted:
            self.aborted = True
            for child in self.children:
                if child.is_alive():
                    child.terminate()

    async def __aenter__(self):
        context = mp.get_context("spawn")
        try:
            # Launch all children before waiting for their ready messages.
            connections = []
            for index in range(self.count):
                connections.append(await self._spawn(context, index))
            await self._ready(connections)
            return self
        except BaseException:
            await self.__aexit__()
            raise

    async def _spawn(self, context, index):
        """Start worker `index`; return the reader of its private socket."""
        parent, child = socket.socketpair()
        parent.setblocking(False)
        child.setblocking(False)
        slots = (self.config.concurrency + self.count - 1 - index) // self.count
        process = context.Process(
            target=_entry,
            name=f"http-worker-{index}",
            daemon=True,
            args=(
                child,
                # The coordinator's workers hold a paced run's in-flight limit
                # (RunConfig.start_concurrency); a child serves its share of slots.
                replace(self.config, concurrency=slots, processes=1, start_concurrency=None),
                self.target,
                self.factory,
                self.deadline,
            ),
        )
        try:
            process.start()
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        self.children.append(process)
        try:
            reader, writer = await asyncio.open_connection(sock=parent)
        except BaseException:
            parent.close()
            raise
        self.writers.append(writer)
        return reader

    async def _ready(self, connections):
        """Wait for every worker's ready message, then start reading its answers."""
        async with asyncio.timeout(30):
            for reader in connections:
                message = await receive(reader)
                if message[0] != "ready":
                    raise RuntimeError("Worker startup failed")
                self.pids.append(message[1])
                self.readers.append(asyncio.create_task(self._read(reader)))

    async def _read(self, reader):
        try:
            while True:
                self._settle(await receive(reader))
        except (EOFError, ConnectionError, asyncio.IncompleteReadError):
            self.failure = True
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("HTTP worker exited unexpectedly"))

    def _settle(self, message):
        """Resolve the pending request a worker message answers."""
        future = self.pending.pop(message[1], None)
        if future is None or future.done():
            return
        if message[0] == "result":
            future.set_result((message[2], message[3]))
        elif message[0] == "expired":
            future.cancel()
        else:
            future.set_exception(RuntimeError("HTTP worker request failed"))

    async def execute(self, target, *, context):
        if self.failure:
            raise RuntimeError("HTTP worker exited unexpectedly")
        future = asyncio.get_running_loop().create_future()
        self.pending[context.request_index] = future
        writer = self.writers[context.worker_index % self.count]
        try:
            send(writer, context)
            await writer.drain()
            result, completed_at = await future
            return replace(result, completed_at=completed_at)
        finally:
            self.pending.pop(context.request_index, None)
            if not future.done():
                future.cancel()

    async def __aexit__(self, *args):
        for writer in self.writers:
            writer.close()
        for reader in self.readers:
            reader.cancel()
        await asyncio.gather(*self.readers, return_exceptions=True)
        # Stop network I/O immediately; do not let children drain a queued backlog.
        self.abort()
        for child in self.children:
            await asyncio.to_thread(child.join, 2)
            if child.is_alive():
                child.kill()
                await asyncio.to_thread(child.join)
            child.close()
        self.children.clear()
        for future in self.pending.values():
            if not future.done():
                future.cancel()
        self.pending.clear()
