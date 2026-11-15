"""`atlas campaign image-daily`: the daily worker image rebuild on the manager (IMAGE-DAILY-1).

Without `--apply` it prints what a run would do (the manager's release, the image the template
names, the image it would build or take, the files it would switch) from the manager's files
only. `--apply` runs it (image_daily.run) with the DigitalOcean token file, never the keychain;
its alert goes into the orchestrator database's alerts table, which posts major rows to Slack.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import cli

if TYPE_CHECKING:
    from .image_daily import Alert, Paths

ALERT = "image_daily_failed"  # image_daily.ALERT, without importing it with the CLI


def _parsers(actions) -> None:
    daily = actions.add_parser(
        "image-daily", help="Rebuild the worker image with the manager's DB-IP release (dry run)"
    )
    daily.add_argument("--root", type=Path, help="the manager root (default $ATLAS_MANAGER_ROOT)")
    daily.add_argument("--dbip-dir", type=Path, help="the DB-IP directory (default <root>/db-ip)")
    daily.add_argument("--token-file", type=Path, help="default <root>/.secrets/do-token")
    daily.add_argument("--database-file", type=Path, help="the orchestrator's, for the alert")
    daily.add_argument("--apply", action="store_true", help="act; without it, only print")


def _alert(database_file: Path) -> Alert:
    async def alert(day: date, message: str) -> None:
        from ..orchestrator.db import Database
        from ..orchestrator.events import raise_alert

        db = Database(str(database_file))
        try:
            async with db.transaction() as conn:
                await raise_alert(
                    conn,
                    ALERT,
                    message,
                    severity="major",
                    dedupe_key=f"{ALERT}:{day:%Y%m%d}",
                    data={"day": day.isoformat()},
                )
        finally:
            await db.close()

    return alert


def summary(found: dict[str, Any]) -> dict[str, Any]:
    """The printed result: the candidates by name only."""
    names = [c.get("snapshot_name") for c in found.get("candidates", [])]
    return {k: v for k, v in found.items() if k != "candidates"} | {"candidates": names}


async def _apply(args, paths: Paths, root: Path) -> dict[str, Any]:
    from . import image_daily
    from .do_api import DigitalOcean, file_token

    token = args.token_file or root / ".secrets" / "do-token"
    async with DigitalOcean(file_token(token)) as cloud:
        context = image_daily.Context(cloud, paths, _alert(args.database_file))
        return await image_daily.run(context)


def _image_daily(args) -> int:
    from ..errors import UserError
    from . import image_daily
    from .collector import MANAGER_ROOT

    root = args.root or MANAGER_ROOT
    paths = image_daily.Paths.of(root, args.dbip_dir)
    try:
        if args.apply and args.database_file is None:
            raise image_daily.DailyImageError("--apply needs --database-file (for the alert)")
        if not args.apply:
            planned = image_daily.plan(paths)
            print(json.dumps(summary(planned) | {"applied": False}, indent=2, sort_keys=True))
            return 0
        cli.cancel_on_signals()
        found = asyncio.run(_apply(args, paths, root))
    except (image_daily.DailyImageError, UserError, OSError, ValueError) as error:
        message = str(error) if not isinstance(error, OSError) else type(error).__name__
        print(json.dumps({"applied": args.apply, "state": "refused", "error": message}))
        return 3
    print(json.dumps(summary(found) | {"applied": True}, indent=2, sort_keys=True))
    return 3 if found["state"] == "failed" else 0


cli.PARSERS.append(_parsers)
cli.HANDLERS.update({"image-daily": _image_daily})
