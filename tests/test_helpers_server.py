"""CI-BUDGET-1: `helpers.server` ends its handlers when its block ends, so a handler that holds
its connection (a stalled upstream) does not hold the test until it gives up."""

import asyncio
import time

from helpers import server


async def test_leaving_the_server_ends_a_handler_that_holds_its_connection():
    held = asyncio.Event()

    async def stall(reader, writer):
        held.set()
        await asyncio.sleep(60)

    started = time.monotonic()
    async with server(stall) as port:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.wait_for(held.wait(), 5)
    assert time.monotonic() - started < 5
    writer.close()
