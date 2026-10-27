"""Database.drain: waits for the batch-close hooks and never spins (no database needed)."""

import asyncio

import pytest

from proxy_index.orchestrator.db import Database


@pytest.fixture
def pool(tmp_path):
    credentials = tmp_path / "db.toml"
    credentials.write_text('[postgres]\nhost = "localhost"\ndbname = "unused"\n')
    credentials.chmod(0o600)
    return Database(credentials)


@pytest.fixture
def spin_guard(monkeypatch):
    """Fail instead of hanging when drain keeps calling gather() without the hooks changing."""
    gather = asyncio.gather
    calls = 0

    def counted(*aws, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 100:
            raise AssertionError("Database.drain spins on finished hooks")
        return gather(*aws, **kwargs)

    monkeypatch.setattr(asyncio, "gather", counted)


async def test_drain_returns_when_a_hook_finished_in_the_same_step(pool, spin_guard):
    seen = []

    async def hook(batch_ids):
        seen.append(batch_ids)

    pool.on_close["probe"] = hook
    pool._notify([7])
    # The hook runs to completion in the next loop step; its done callback (which removes it
    # from the pending hooks) is queued for the step after, so drain starts in that window.
    await asyncio.sleep(0)
    assert pool._hooks and all(task.done() for task in pool._hooks)
    await pool.drain()
    assert seen == [[7]] and not pool._hooks


async def test_drain_waits_for_slow_hooks_and_the_hooks_they_start(pool, spin_guard):
    seen = []
    release = asyncio.Event()

    async def first(batch_ids):
        await release.wait()
        seen.append(("first", batch_ids))
        pool._notify([batch_ids[0] + 1])

    async def second(batch_ids):
        seen.append(("second", batch_ids))

    pool.on_close["probe"] = first
    pool._notify([1])
    pool.on_close["probe"] = second
    asyncio.get_running_loop().call_later(0.05, release.set)
    await pool.drain()
    assert seen == [("first", [1]), ("second", [2])]
    assert not pool._hooks
