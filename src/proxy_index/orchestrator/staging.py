"""A staging database for trying the census/pulse loop end to end (proxy-index staging init).

`configs/staging.toml` names the database and the profile: a few countries, a census every few
minutes, frequent pulses, the load test's default reference countries, the staging methodology, HTML
reports and the nodes to issue tokens for. `init` reads the app
connection from <secrets>/database.toml and the admin connection from
<secrets>/postgres-admin.toml, creates the staging database owned by the app user (or with
`clean` drops and recreates it), writes <secrets>/staging-database.toml pointing at it, applies
both schema layers, seeds, applies the profile, and writes one token file per node under
<secrets>/staging-tokens/. It refuses to touch the production database: a staging name equal to
database.toml's dbname, to "proxy_reviews" or to a maintenance database is an error.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tomllib
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from .. import storage
from ..providers.credentials import read_credentials
from ..regions import CONFIGS, resolve_region
from .control import write_settings
from .db import DEFAULTS
from .nodes import issue_token

PRODUCTION = "proxy_reviews"
RESERVED = frozenset({PRODUCTION, "postgres", "template0", "template1"})
NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
SLUG = re.compile(r"^[A-Za-z0-9_.-]+$")
STAGING_FILE = CONFIGS / "staging.toml"


def _positive(section: dict, key: str, where: str) -> int:
    value = section.get(key)
    if type(value) is not int or value < 1:
        raise ValueError(f"[{where}] {key} must be a positive integer")
    return value


def load_profile(path: str | Path) -> dict[str, Any]:
    """Validate configs/staging.toml into a flat profile."""
    with open(path, "rb") as stream:
        data = tomllib.load(stream)
    name = data.get("database", {}).get("name")
    if not isinstance(name, str) or not NAME.match(name):
        raise ValueError("[database] name must be a lower-case PostgreSQL identifier")
    countries = data.get("scope", {}).get("countries")
    if (
        not isinstance(countries, list)
        or not countries
        or not all(isinstance(c, str) and re.fullmatch(r"[A-Za-z]{2}", c) for c in countries)
    ):
        raise ValueError("[scope] countries must list two-letter country codes")
    census, pulse = data.get("census", {}), data.get("pulse", {})
    defaults = data.get("load", {}).get("reference_defaults", {})
    scope = {c.upper() for c in countries}
    if not isinstance(defaults, dict) or not all(
        isinstance(region, str) and isinstance(code, str) and code.upper() in scope
        for region, code in defaults.items()
    ):
        raise ValueError("[load] reference_defaults must map regions to [scope] countries")
    for region in defaults:
        resolve_region(region)
    methodology = data.get("calculate", {}).get("methodology")
    if methodology is not None:
        if not isinstance(methodology, str) or not methodology:
            raise ValueError("[calculate] methodology must be a path")
        from ..calculated.policy import load as load_methodology

        load_methodology(Path(methodology).expanduser())
    switches = data.get("scheduler", {})
    for key in ("census", "pulse"):
        if type(switches.get(key, True)) is not bool:
            raise ValueError(f"[scheduler] {key} must be true or false")
    reports = data.get("reports", {}).get("dir")
    if reports is not None and (not isinstance(reports, str) or not reports):
        raise ValueError("[reports] dir must be a path")
    nodes = data.get("nodes", [])
    if not isinstance(nodes, list):
        raise ValueError("[[nodes]] must be an array of tables")
    for node in nodes:
        if not isinstance(node, dict) or not all(
            isinstance(node.get(key), str) and SLUG.match(node[key])
            for key in ("provider", "product", "region")
        ):
            raise ValueError("Every [[nodes]] entry needs provider, product and region")
        resolve_region(node["region"])
    return {
        "name": name,
        "countries": sorted({c.upper() for c in countries}),
        "census_every_minutes": _positive(census, "every_minutes", "census"),
        "census_concurrency": _positive(census, "concurrency", "census"),
        "census_min_sample": _positive(census, "min_sample", "census"),
        "pulse_interval_minutes": _positive(pulse, "interval_minutes", "pulse"),
        "pulse_requests": _positive(pulse, "requests", "pulse"),
        "scheduler": {key: switches.get(key, True) for key in ("census", "pulse")},
        "reference_defaults": {region: code.upper() for region, code in defaults.items()},
        "methodology": methodology,
        "reports_dir": reports,
        "nodes": [{key: node[key] for key in ("provider", "product", "region")} for node in nodes],
    }


def _write_private(path: Path, text: str) -> None:
    """Write an owner-only file (mode 600), replacing any previous one."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)
    path.chmod(0o600)


def _quote(value: str | Path) -> str:
    return shlex.quote(str(value))


def _toml(table: str, values: dict[str, str]) -> str:
    lines = "".join(f"{key} = {json.dumps(value)}\n" for key, value in values.items())
    return f"[{table}]\n{lines}"


def _database(admin: dict[str, str], name: str, owner: str, clean: bool) -> dict[str, bool]:
    with psycopg.connect(**admin, autocommit=True, connect_timeout=5) as conn:
        exists = bool(
            conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone()
        )
        dropped = False
        if exists and clean:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=%s AND pid<>pg_backend_pid()",
                (name,),
            )
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            exists, dropped = False, True
        if not exists:
            conn.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(name), sql.Identifier(owner)
                )
            )
    return {"created": not exists, "dropped": dropped}


def init(
    config: str | Path | None = None,
    *,
    secrets_dir: str | Path = ".secrets",
    clean: bool = False,
    orchestrator_url: str = "http://127.0.0.1:8080",
) -> dict[str, Any]:
    """Create or refresh the staging database and print-ready summary (see the module doc)."""
    profile = load_profile(config or STAGING_FILE)
    secrets = Path(secrets_dir).resolve()
    app = read_credentials(secrets / "database.toml", "postgres")
    admin = read_credentials(secrets / "postgres-admin.toml", "postgres")
    name = profile["name"]
    if name in RESERVED or name == app.get("dbname") or name == admin.get("dbname"):
        raise ValueError(
            f"Refusing to use {name!r} as the staging database: it is the production or a "
            "maintenance database"
        )
    if not app.get("user"):
        raise ValueError("database.toml needs a user to own the staging database")
    database = _database(admin, name, app["user"], clean)

    staging_file = secrets / "staging-database.toml"
    _write_private(staging_file, _toml("postgres", app | {"dbname": name}))

    from ..calculated.store import initialize as initialize_calculated

    schema = storage.initialize(staging_file) | initialize_calculated(staging_file)
    seeded = storage.seed(staging_file)
    reports_dir = (
        str(Path(profile["reports_dir"]).expanduser().resolve()) if profile["reports_dir"] else None
    )
    methodology = (
        str(Path(profile["methodology"]).expanduser().resolve()) if profile["methodology"] else None
    )
    with storage.connect(staging_file) as conn:
        known = {
            row[0]
            for row in conn.execute(
                "SELECT country_code FROM countries WHERE country_code=ANY(%s)",
                (profile["countries"],),
            )
        }
        missing = sorted(set(profile["countries"]) - known)
        if missing:
            raise ValueError(f"Unknown scope countries: {', '.join(missing)}")
        conn.execute(
            "UPDATE countries SET enabled = country_code = ANY(%s)", (profile["countries"],)
        )
        census_targets = [
            row[0]
            for row in conn.execute(
                "UPDATE targets SET census_concurrency=%s WHERE census RETURNING target_id",
                (profile["census_concurrency"],),
            )
        ]
        pulse_targets = [
            row[0]
            for row in conn.execute(
                "UPDATE targets SET pulse_interval_minutes=%s,pulse_requests=%s "
                "WHERE pulse_enabled RETURNING target_id",
                (profile["pulse_interval_minutes"], profile["pulse_requests"]),
            )
        ]
        write_settings(
            conn,
            {
                "census.every_minutes": profile["census_every_minutes"],
                "census.min_sample": profile["census_min_sample"],
                "scheduler.census_enabled": profile["scheduler"]["census"],
                "scheduler.pulse_enabled": profile["scheduler"]["pulse"],
                "load.reference_defaults": DEFAULTS["load.reference_defaults"]
                | profile["reference_defaults"],
                "reports.dir": reports_dir,
            },
        )

    tokens = []
    token_dir = secrets / "staging-tokens"
    for node in profile["nodes"]:
        issued = issue_token(
            str(staging_file),
            provider=node["provider"],
            product=node["product"],
            region=node["region"],
        )
        path = token_dir / f"{node['provider']}-{node['product']}-{issued['region']}.token"
        _write_private(path, issued["token"] + "\n")
        provider_config = CONFIGS / f"{node['provider']}.toml"
        tokens.append(
            {
                "provider": node["provider"],
                "product": node["product"],
                "region": issued["region"],
                "node_id": issued["node_id"],
                "rotated": issued["rotated"],
                "token_file": str(path),
                "command": " ".join(
                    [
                        "proxy-index node",
                        f"--orchestrator {orchestrator_url}",
                        f"--token-file {_quote(path)}",
                        f"--provider {node['provider']}",
                        f"--product {node['product']}",
                        f"--region {issued['region']}",
                        f"--provider-config {_quote(provider_config)}",
                        f"--credentials-file {_quote(secrets / 'credentials.toml')}",
                    ]
                ),
            }
        )
    return {
        "database": name,
        "database_file": str(staging_file),
        **database,
        "schema": schema,
        "seeded": {"countries": seeded["countries"], "targets": seeded["targets"]},
        "countries": profile["countries"],
        "census": {
            "every_minutes": profile["census_every_minutes"],
            "min_sample": profile["census_min_sample"],
            "concurrency": profile["census_concurrency"],
            "targets": census_targets,
        },
        "pulse": {
            "interval_minutes": profile["pulse_interval_minutes"],
            "requests": profile["pulse_requests"],
            "targets": pulse_targets,
        },
        "scheduler": profile["scheduler"],
        "load": {
            "reference_defaults": profile["reference_defaults"],
            "concurrency": profile["census_concurrency"],
            "targets": census_targets,
        },
        "methodology": methodology,
        "reports_dir": reports_dir,
        "nodes": tokens,
        "commands": {
            "orchestrator": (
                f"proxy-index orchestrator --dev --database-file {_quote(staging_file)}"
                + (f" --methodology {_quote(methodology)}" if methodology else "")
            ),
            "calculate": (
                f"proxy-index calculate --database-file {_quote(staging_file)}"
                + (f" --methodology {_quote(methodology)}" if methodology else "")
            ),
            "nodes": [token["command"] for token in tokens],
            "report": (
                f"proxy-index report html --database-file {_quote(staging_file)} "
                f"--out {_quote(reports_dir or 'reports/staging')}"
            ),
        },
    }
