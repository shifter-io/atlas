"""`proxy-index dbip update`: keep the local DB-IP MMDB current (DBIP-DAILY-1).

DB-IP releases are dated files on a CDN (`<base URL>/dbip-full-YYYY-MM-DD.mmdb`, source.py).
`update` finds the newest one (tomorrow back to the installed release, or `lookback_days` when
none is installed) and downloads it only when it differs from the installed release. It
verifies it (the announced size, at least `min_bytes` and half the installed release, the
MaxMind reader and a spread of test lookups) and renames it over `dest/dbip-full.mmdb` in one
step: the file always has the same name, and the download goes to a temporary file in the same
directory, never into the file in use (files.py). It records the release in `dbip_releases`.
A second run on the same release downloads and changes nothing. Runs already stored keep the
release they were enriched with (calculated/calculator.py).

The orchestrator runs it once a day on its server (jobs.py, `dbip_update`) and each pulse node
runs it from a daily timer (campaign/pulse_fleet.py); long-lived readers reopen the replaced
file (enrichment.LocalEnricher.reload).

Exit codes: 0 updated or unchanged, 2 configuration, 3 probe, download, verification or
recording failed, 4 another update holds the directory's lock. Every run prints one JSON
health line.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..enrichment import CURRENT
from .files import PARTIAL, STATE, directory_lock, inspect, install, read_state, write_json
from .source import (
    BASE_URL_ENV,
    PREFIX,
    Busy,
    ConfigError,
    Release,
    UpdateError,
    base_url,
    download,
    newest,
)

__all__ = [
    "DEFAULT_CHECKS",
    "Busy",
    "Checks",
    "ConfigError",
    "UpdateError",
    "base_url",
    "update",
]


@dataclass(frozen=True, slots=True)
class Checks:
    min_bytes: int = 100 << 20  # a full DB-IP MMDB is over a gigabyte
    min_coverage: float = 0.9  # share of the test lookups that must find a country
    shrink_limit: float = 0.5  # a release under half the installed one's size is refused
    lookback_days: int = 62  # how far back to look for a release when none is installed


DEFAULT_CHECKS = Checks()
_RELEASE = ("file", "sha256", "database_type", "build_epoch", "built_at")


def update(
    dest: Path,
    base: str,
    *,
    database_file: str | Path | None = None,
    checks: Checks = DEFAULT_CHECKS,
    today: date | None = None,
) -> dict[str, Any]:
    """Bring `dest` to the newest release under `base`; the health record (public fields).

    Raises UpdateError before anything is installed; a failure to record an installed release
    is returned as `error` (the next run records it)."""
    dest.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).replace(microsecond=0)
    today = today or now.date()
    with directory_lock(dest):
        state = read_state(dest)
        oldest = today - timedelta(days=checks.lookback_days)
        if state:
            oldest = max(oldest, date.fromisoformat(state["source"]["date"]))
        # From tomorrow: a release dated in a time zone ahead of UTC is found the same day.
        release = newest(base, today + timedelta(days=1), oldest)
        if release is None and not state:
            raise UpdateError(
                f"probe: no {PREFIX}YYYY-MM-DD.mmdb in the last {checks.lookback_days} days"
            )
        if release is None or release.identity() == state.get("source"):
            state["checked_at"] = now.isoformat()
            write_json(dest / STATE, state)
            result = _health("unchanged", state)
        else:
            result = _replace(dest, release, state, checks, now)
    result["recorded"] = None
    if database_file:
        try:
            record(database_file, {k: result[k] for k in _RELEASE})
            result["recorded"] = True
        except UpdateError as exc:
            result |= {"recorded": False, "error": str(exc)}
    return result


def _health(status: str, state: dict[str, Any]) -> dict[str, Any]:
    return {"dbip": status, **{k: state[k] for k in (*_RELEASE, "source")}}


def _replace(
    dest: Path, release: Release, state: dict[str, Any], checks: Checks, now: datetime
) -> dict[str, Any]:
    partial = dest / PARTIAL
    try:
        size, sha256 = download(release, partial)
        _check_size(size, state, checks)
        record_ = inspect(partial, sha256, checks.min_coverage)
        status = "unchanged" if state.get("sha256") == sha256 else "updated"  # same bytes?
        if status == "updated":
            install(dest, partial)
    finally:
        partial.unlink(missing_ok=True)
    stamp = now.isoformat()
    new_state = record_ | {"file": CURRENT, "size": size, "source": release.identity()}
    new_state["updated_at"] = state.get("updated_at") if status == "unchanged" else stamp
    new_state["checked_at"] = stamp
    write_json(dest / STATE, new_state)
    return _health(status, new_state)


def _check_size(size: int, state: dict[str, Any], checks: Checks) -> None:
    if size < checks.min_bytes:
        raise UpdateError(f"verify: {size} bytes, under the {checks.min_bytes} minimum")
    if state and size < state["size"] * checks.shrink_limit:
        raise UpdateError(f"verify: {size} bytes, under half the installed release")


def record(database_file: str | Path, release: dict[str, Any]) -> None:
    """Insert the release into dbip_releases (once; enrichment imports insert the same row)."""
    from psycopg.types.json import Jsonb

    from ..storage import connect

    try:
        with connect(database_file) as conn:
            conn.execute(
                "INSERT INTO dbip_releases(sha256,metadata) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (release["sha256"], Jsonb(release)),
            )
    except Exception as exc:  # noqa: BLE001 - connection errors may carry the DSN
        raise UpdateError(f"record: {type(exc).__name__}") from None


def register(commands) -> None:
    parser = commands.add_parser("dbip", help="Keep the local DB-IP MMDB current (operator)")
    actions = parser.add_subparsers(dest="dbip_action", required=True)
    run = actions.add_parser(
        "update", help="Download, verify and install the newest dated MMDB when it changed"
    )
    run.add_argument("--dest", type=Path, required=True, help="Directory of dbip-full.mmdb")
    run.add_argument(
        "--base-url", help=f"CDN directory of dbip-full-YYYY-MM-DD.mmdb (default ${BASE_URL_ENV})"
    )
    run.add_argument(
        "--database-file",
        default=os.environ.get("PROXY_INDEX_DATABASE_FILE"),
        help="Record the release in dbip_releases (default $PROXY_INDEX_DATABASE_FILE)",
    )
    run.add_argument("--min-bytes", type=int, default=DEFAULT_CHECKS.min_bytes)
    run.add_argument("--min-coverage", type=float, default=DEFAULT_CHECKS.min_coverage)
    run.add_argument("--lookback-days", type=int, default=DEFAULT_CHECKS.lookback_days)


def dispatch(args) -> int:
    checks = Checks(args.min_bytes, args.min_coverage, lookback_days=args.lookback_days)
    try:
        base = base_url(args.base_url)
        result = update(
            Path(args.dest).expanduser(), base, database_file=args.database_file, checks=checks
        )
    except UpdateError as exc:
        print(json.dumps({"dbip": "failed", "error": str(exc)}))
        return exc.code
    except OSError as exc:
        print(json.dumps({"dbip": "failed", "error": f"files: {type(exc).__name__}"}))
        return 3
    print(json.dumps(result))
    return 3 if result.get("error") else 0
