"""SEC-MANAGER-1: an upload body has a total time limit (408, the file removed), and a refused
ingest reaches the node as a short class, never as the exception's text."""

import asyncio
import json
from types import SimpleNamespace

import psycopg
import pytest
from aiohttp import web

from proxy_index.orchestrator.api import _reason, _receive
from proxy_index.orchestrator.db import DEFAULTS


def request(chunks: list[bytes], stall: float = 0):
    """A request whose body yields `chunks`, then waits `stall` seconds before ending."""

    async def body(_size):
        for chunk in chunks:
            yield chunk
        await asyncio.sleep(stall)

    return SimpleNamespace(content=SimpleNamespace(iter_chunked=body))


async def test_a_body_received_in_time_is_hashed(tmp_path):
    path = tmp_path / "run.tar.gz"
    digests = await _receive(request([b"ab", b"c"]), path, limit=10, seconds=5)
    assert digests["bytes"] == 3 and path.read_bytes() == b"abc"


async def test_a_body_past_the_time_limit_is_408_and_its_file_goes(tmp_path):
    path = tmp_path / "run.tar.gz"
    with pytest.raises(web.HTTPRequestTimeout) as refused:
        await _receive(request([b"abc"], stall=5), path, limit=10, seconds=0.05)
    assert json.loads(refused.value.text) == {"error": "upload_timeout"}
    assert not path.exists()
    assert DEFAULTS["results.upload_timeout_seconds"] == 600


async def test_a_body_over_the_size_limit_is_413(tmp_path):
    with pytest.raises(web.HTTPRequestEntityTooLarge):
        await _receive(request([b"abc", b"def"]), tmp_path / "run.tar.gz", limit=4, seconds=5)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (psycopg.errors.CheckViolation('new row violates check constraint "x"'), "database_refused"),
        (psycopg.DataError("invalid input syntax"), "database_refused"),
        (KeyError("requests"), "missing_field"),
        (TypeError("unsupported operand"), "wrong_type"),
        (ValueError("successes exceed requests"), "invalid_data"),
    ],
)
def test_a_refused_ingest_is_named_by_its_class(error, reason):
    assert _reason(error) == reason
