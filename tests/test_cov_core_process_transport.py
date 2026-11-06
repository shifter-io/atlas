"""process_transport.py paths the spawned-worker tests do not reach (COV-7): oversized and
unexpected worker messages, a coordinator whose worker already failed, a send that fails, a
worker connection that cannot be opened, and the cleanup of a worker that ignores terminate.

Stand-in sockets, writers and children replace spawned processes, so each failure is exact."""

import asyncio
import pickle
import socket
import struct
from types import SimpleNamespace

import pytest

from proxy_index.models import ProxyConfig, RequestContext, RunConfig
from proxy_index.process_transport import ProcessTransport, receive
from proxy_index.targets import registry

TARGET = registry.get("generic_http")


def transport():
    config = RunConfig("generic_http", ProxyConfig("http", "localhost", 1), processes=1)
    return ProcessTransport(config, TARGET)


def framed(message):
    data = pickle.dumps(message, protocol=5)
    return struct.pack("!I", len(data)) + data


def reader_of(data):
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def test_an_oversized_worker_message_is_refused_before_it_is_read():
    with pytest.raises(RuntimeError, match="Invalid worker message"):
        await receive(reader_of(struct.pack("!I", 1024 * 1024 + 1)))


async def test_a_worker_whose_first_message_is_not_ready_fails_the_start():
    engine = transport()
    with pytest.raises(RuntimeError, match="Worker startup failed"):
        await engine._ready([reader_of(framed(("error", 0)))])
    assert engine.pids == [] and engine.readers == []


async def test_no_request_is_sent_once_a_worker_has_failed():
    engine = transport()
    engine.failure = True
    with pytest.raises(RuntimeError, match="exited unexpectedly"):
        await engine.execute(TARGET, context=RequestContext("run", 0, 0))
    assert engine.pending == {}


class BrokenWriter:
    def __init__(self):
        self.sent = []

    def write(self, data):
        self.sent.append(data)

    async def drain(self):
        raise ConnectionResetError


async def test_a_request_that_cannot_be_sent_leaves_nothing_pending():
    engine = transport()
    writer = BrokenWriter()
    engine.writers = [writer]
    with pytest.raises(ConnectionResetError):
        await engine.execute(TARGET, context=RequestContext("run", 4, 0))
    assert writer.sent == [framed(RequestContext("run", 4, 0))]
    assert engine.pending == {}


async def test_a_worker_connection_that_cannot_open_closes_its_socket(monkeypatch):
    pairs = []
    real = socket.socketpair

    def socketpair():
        pairs.append(real())
        return pairs[-1]

    async def refuse(**kwargs):
        raise OSError("no connection")

    started = []
    context = SimpleNamespace(
        Process=lambda **kwargs: SimpleNamespace(start=lambda: started.append(kwargs["name"]))
    )
    monkeypatch.setattr("proxy_index.process_transport.socket.socketpair", socketpair)
    monkeypatch.setattr("proxy_index.process_transport.asyncio.open_connection", refuse)
    engine = transport()
    with pytest.raises(OSError, match="no connection"):
        await engine._spawn(context, 0)
    assert started == ["http-worker-0"] and len(engine.children) == 1
    assert engine.writers == []
    assert [side.fileno() for side in pairs[0]] == [-1, -1]


class Stubborn:
    """A child that ignores terminate and only a kill stops."""

    def __init__(self):
        self.alive = True
        self.events = []

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.events.append("terminate")

    def join(self, timeout=None):
        self.events.append(("join", timeout))

    def kill(self):
        self.events.append("kill")
        self.alive = False

    def close(self):
        self.events.append("close")


async def test_exit_kills_a_worker_that_ignores_terminate_and_cancels_pending():
    engine = transport()
    child = Stubborn()
    engine.children = [child]
    waiting = asyncio.get_running_loop().create_future()
    engine.pending[3] = waiting
    await engine.__aexit__()
    assert child.events == ["terminate", ("join", 2), "kill", ("join", None), "close"]
    assert engine.children == [] and engine.pending == {}
    assert waiting.cancelled() and engine.aborted
