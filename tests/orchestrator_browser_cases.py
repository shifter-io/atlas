"""Shared helpers of the browser pulse planner tests (test_orchestrator_browser.py): the example
settings, browser node tokens, browser batch reads and the census and pulse runs at noon."""

from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from orchestrator_cases import prepare, query

from atlas.browser.contracts import RUN_TARGETS
from atlas.orchestrator import batches
from atlas.orchestrator.db import Database, put_setting
from atlas.orchestrator.nodes import issue_token

EXAMPLE = Path(__file__).resolve().parents[1] / "configs" / "browser-pulse.example.toml"
# An even UTC hour: the slot of the hourly and of the 120-minute targets alike.
HOUR = datetime(2026, 10, 1, 12, tzinfo=UTC)
LEAD = timedelta(seconds=60)  # the example's lead_seconds
SLOW = ("ai_chatgpt", "ai_gemini")  # every 120 minutes in the fixture
HOURLY = tuple(target for target in RUN_TARGETS if target not in SLOW)
NOON = datetime(2026, 10, 1, 11, 59, 30, tzinfo=UTC)


def settings_text(enabled=True):
    """The example settings, enabled, with the last two of its 8 targets every 120 minutes."""
    text = EXAMPLE.read_text()
    assert "enabled = false" in text
    text = text.replace("enabled = false", f"enabled = {str(enabled).lower()}", 1)
    for target in SLOW:
        block = f'id = "{target}"\ntries = 33\nevery_minutes = 60'
        assert block in text
        text = text.replace(block, block.replace("= 60", "= 120"))
    return text


async def configure(db, path, enabled=True):
    async with db.connection() as conn:
        await put_setting(conn, "browser.enabled", enabled)
        await put_setting(conn, "browser.settings_file", str(path) if path else None)


def settings_file(tmp_path, text=None):
    path = tmp_path / "browser-pulse.toml"
    path.write_text(settings_text() if text is None else text)
    return path


def browser_nodes(database):
    """alpha: a browser node and a fra1 node; beta: only a browser node; gamma: only fra1."""
    return {
        "alpha": issue_token(
            database, provider="alpha", product="res", region="browser", host="browser-1"
        ),
        "alpha-fra1": issue_token(database, provider="alpha", product="res", region="fra1"),
        "beta": issue_token(
            database, provider="beta", product="res", region="browser-2", host="browser-2"
        ),
        "gamma": issue_token(database, provider="gamma", product="res", region="fra1"),
    }


def browser_batches(database, after=0):
    return query(
        database,
        "SELECT batch_id,target_id,country_code,region_id,scheduled_for,start_at,status "
        "FROM test_batches WHERE kind='browser' AND batch_id>%s ORDER BY start_at,batch_id",
        (after,),
    )


def targets(hourly=6, slow=2):
    return [(f"h{i}", 60) for i in range(hourly)] + [(f"s{i}", 120) for i in range(slow)]


async def planned_runs(config, with_browser):
    """Census and pulse at noon with alpha in fra1, and beta in the browser region or not."""
    prepare(config, ("DE",), DE=time(12))
    issue_token(config, provider="alpha", product="res", region="fra1")
    if with_browser:
        issue_token(config, provider="beta", product="res", region="browser", host="browser-1")
        issue_token(config, provider="alpha", product="res", region="browser", host="browser-2")
    pool = Database(config)
    try:
        census = await batches.census_plan(pool, NOON)
        pulse = await batches.pulse_plan(pool, NOON + timedelta(seconds=30))
    finally:
        await pool.close()
    runs = query(
        config,
        "SELECT b.kind,b.country_code,b.region_id,b.target_id,b.scheduled_for,b.start_at,"
        "p.provider,p.name,r.status,r.error,r.node_id IS NULL FROM batch_runs r "
        "JOIN test_batches b USING(batch_id) JOIN products p USING(product_id) ORDER BY 1,2,3,4,7",
    )
    return census, pulse, runs
