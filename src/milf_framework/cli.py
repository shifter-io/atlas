from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys

from .models import ProxyConfig, RunConfig
from .providers import load_provider
from .providers import registry as providers
from .runner import run
from .sinks import JsonSink
from .targets import registry


async def _run(args: argparse.Namespace) -> int:
    if args.provider_config:
        params = {}
        for item in args.param:
            key, separator, value = item.partition("=")
            if not separator or not key or key in params:
                raise ValueError("Each provider parameter must appear once as name=value")
            try:
                params[key] = json.loads(value)
            except json.JSONDecodeError:
                params[key] = value
        proxy = load_provider(
            args.provider_config,
            parameters=params,
            session_policy=args.session_policy,
            credentials_file=args.credentials_file,
        )
    else:
        if args.param or args.session_policy or args.credentials_file:
            raise ValueError("Provider parameters require --provider-config")
        raw_proxy = os.environ.get(args.proxy_env or "PROXY_URL")
        if not raw_proxy:
            raise ValueError("Proxy environment variable is missing")
        proxy = ProxyConfig.from_url(raw_proxy)
    config = RunConfig(
        target=args.target,
        proxy=proxy,
        requests=args.requests,
        concurrency=args.concurrency,
        timeout_seconds=args.timeout,
        requests_per_second=args.rate,
        provider=args.provider,
        product=args.product,
        requested_country=args.country,
        runner_location=args.location,
        target_url=args.url,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        except NotImplementedError:
            pass
    sink = JsonSink(args.output)
    try:
        summary = await run(config, sink, stop=stop)
        print(f"Results: {sink.directory}", file=sys.stderr)
        if args.database_file:
            from .storage import ingest

            imported = await asyncio.to_thread(ingest, sink.directory, args.database_file)
            print(json.dumps({"database_import": imported}), file=sys.stderr)
        print(json.dumps(summary.to_dict(), allow_nan=False))
        return 130 if summary.status == "cancelled" else 0
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="milf-framework")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("targets", help="List available target definitions")
    commands.add_parser("providers", help="List available provider adapters")
    measure = commands.add_parser("run", help="Run a target through a supplied proxy")
    measure.add_argument("--target", choices=[t.id for t in registry.all()], required=True)
    measure.add_argument(
        "--url", help="Echo-IP URL override; must match the target's HTTP/HTTPS scheme"
    )
    measure.add_argument("--requests", type=int, default=100)
    measure.add_argument("--concurrency", type=int, default=10)
    measure.add_argument("--timeout", type=float, default=15)
    measure.add_argument("--rate", type=float, help="Maximum request starts per second")
    connection = measure.add_mutually_exclusive_group()
    connection.add_argument("--proxy-env", help="Proxy URL environment name (default PROXY_URL)")
    connection.add_argument(
        "--provider-config", help="Provider TOML file with credential env names"
    )
    measure.add_argument(
        "--credentials-file", help="Private credential TOML (owner-only permissions); overrides env"
    )
    measure.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Override provider parameter; null omits it; repeat for more",
    )
    measure.add_argument("--session-policy", help="Override provider session policy")
    measure.add_argument("--output", default="results")
    measure.add_argument(
        "--database-file", help="Import saved results using private PostgreSQL config"
    )
    measure.add_argument("--provider")
    measure.add_argument("--product")
    measure.add_argument("--country", help="Requested geography label; does not change credentials")
    measure.add_argument("--location", help="Runner location label")
    enrich = commands.add_parser("enrich", help="Enrich an existing HTTP result using local MMDB")
    enrich.add_argument("directory")
    enrich.add_argument("--mmdb")
    enrich.add_argument(
        "--database-file", help="Import measurements and new enrichment after saving"
    )
    ingest_parser = commands.add_parser("ingest", help="Import a saved run into PostgreSQL")
    ingest_parser.add_argument("directory")
    ingest_parser.add_argument("--database-file", required=True)
    for name, help_text in (
        ("db-init", "Apply the PostgreSQL schema"),
        ("db-migrate", "Upgrade a v4 to v7 database in place to the current schema"),
        ("db-status", "Inspect stored runs and cached totals"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--database-file", required=True)
    seed = commands.add_parser(
        "db-seed", help="Upsert runner regions, countries and targets from configs/"
    )
    seed.add_argument("--database-file", required=True)
    seed.add_argument("--regions", help="Region patterns YAML (default configs/regions.yaml)")
    seed.add_argument("--targets", help="Targets TOML (default configs/targets.toml)")
    seed.add_argument("--countries", help="Country names JSON (default configs/countries.json)")
    seed.add_argument(
        "--reset-times",
        action="store_true",
        help="Put every census time back on the staggered default slots",
    )
    database = commands.add_parser("db-info", help="Identify the local MMDB and show its checksum")
    database.add_argument("--mmdb")
    from .calculated.cli import register

    register(commands)
    from .orchestrator import cli as fleet

    fleet.register(commands)
    args = parser.parse_args(argv)
    try:
        if args.command in fleet.COMMANDS:
            return fleet.dispatch(args)
        if args.command in {
            "calculate-init",
            "calculate",
            "snapshot-publish",
            "snapshot-export",
            "run-role",
            "metric-evidence",
            "serve",
            "report",
        }:
            from .calculated.cli import dispatch

            return dispatch(args)
        if args.command == "run":
            return asyncio.run(_run(args))
        if args.command == "targets":
            print(
                json.dumps(
                    [
                        {
                            "id": t.id,
                            "version": t.version,
                            "url": t.build_request().url,
                            "engine": t.engine,
                            "pool_eligible": t.pool_eligible,
                        }
                        for t in registry.all()
                    ],
                    indent=2,
                )
            )
        elif args.command == "providers":
            print(json.dumps(providers.all()))
        elif args.command == "enrich":
            from .enrichment import enrich_run

            print(json.dumps(enrich_run(args.directory, args.mmdb), indent=2))
            if args.database_file:
                from .storage import ingest

                print(json.dumps({"database_import": ingest(args.directory, args.database_file)}))
        elif args.command == "db-seed":
            from .storage import seed

            result = seed(
                args.database_file,
                args.regions,
                args.targets,
                countries_path=args.countries,
                reset_times=args.reset_times,
            )
            print(json.dumps(result, indent=2))
        elif args.command in ("ingest", "db-init", "db-migrate", "db-status"):
            from .calculated.store import initialize as initialize_all
            from .storage import ingest, initialize, migrate, status

            if args.command == "db-init":
                # The orchestrator calculates on the same database: create both layers.
                result = initialize(args.database_file) | initialize_all(args.database_file)
            else:
                result = (
                    ingest(args.directory, args.database_file)
                    if args.command == "ingest"
                    else {"db-migrate": migrate, "db-status": status}[args.command](
                        args.database_file
                    )
                )
            print(json.dumps(result, indent=2))
        else:
            from .enrichment import LocalEnricher

            with LocalEnricher(args.mmdb) as enricher:
                print(json.dumps(enricher.release, indent=2))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Libraries can put proxy URLs into exception messages; expose only types.
        print(
            f"Command failed ({type(exc).__name__}). Check configuration and file paths.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
