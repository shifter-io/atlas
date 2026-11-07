from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from contextlib import nullcontext

from .errors import UserError
from .models import NETWORK_PROTOCOLS, ProxyConfig, ProxySource, RunConfig
from .providers import load_provider
from .providers import registry as providers
from .providers.grammar import answer_classes, grammar_ids_by_protocol
from .runner import RunOptions, run
from .sinks import JsonSink
from .targets import registry


async def _run(args: argparse.Namespace) -> int:
    if args.answer_classes and not args.provider_config:
        print("Run refused: --answer-classes needs --provider-config.", file=sys.stderr)
        return 2
    config = _run_config(args, _proxy(args))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = _stop_on_signals(loop, stop)
    sink = JsonSink(args.output)
    try:
        return await _record(args, config, sink, stop)
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


def _parameters(items: list[str]) -> dict:
    """The --param overrides by name; a value that is not JSON stays a string."""
    params = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or not key or key in params:
            raise ValueError("Each provider parameter must appear once as name=value")
        try:
            params[key] = json.loads(value)
        except json.JSONDecodeError:
            params[key] = value
    return params


def _proxy(args: argparse.Namespace) -> ProxySource:
    """The run's proxy: the provider config's, or the URL in the proxy environment variable."""
    if args.provider_config:
        params = _parameters(args.param)
        return load_provider(
            args.provider_config,
            parameters=params,
            session_policy=args.session_policy,
            credentials_file=args.credentials_file,
        )
    if args.param or args.session_policy or args.credentials_file:
        raise ValueError("Provider parameters require --provider-config")
    raw_proxy = os.environ.get(args.proxy_env or "PROXY_URL")
    if not raw_proxy:
        raise ValueError("Proxy environment variable is missing")
    return ProxyConfig.from_url(raw_proxy)


def _run_config(args: argparse.Namespace, proxy: ProxySource) -> RunConfig:
    return RunConfig(
        target=args.target,
        proxy=proxy,
        requests=args.requests,
        concurrency=args.concurrency,
        processes=args.processes,
        timeout_seconds=args.timeout,
        requests_per_second=args.rate,
        provider=args.provider,
        product=args.product,
        requested_country=args.country,
        runner_location=args.location,
        target_url=args.url,
        # The network's answer classes come from the grammar of the configured provider.
        answer_classes=answer_classes(proxy.public()["provider"]) if args.answer_classes else None,
    )


def _stop_on_signals(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> list:
    """Set stop on SIGINT and SIGTERM where the loop supports it; returns the signals installed."""
    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        except NotImplementedError:
            pass
    return installed


async def _record(args: argparse.Namespace, config: RunConfig, sink: JsonSink, stop) -> int:
    """Run into sink, print where the results are and their summary; import them when asked."""
    from .enrichment import LocalEnricher

    with LocalEnricher(args.mmdb) if args.mmdb else nullcontext() as enricher:
        summary = await run(config, sink, RunOptions(stop=stop, enricher=enricher))
    print(f"Results: {sink.directory}", file=sys.stderr)
    if args.database_file:
        from .storage import ingest

        imported = await asyncio.to_thread(ingest, sink.directory, args.database_file)
        print(json.dumps({"database_import": imported}), file=sys.stderr)
    print(json.dumps(summary.to_dict(), allow_nan=False))
    return 130 if summary.status == "cancelled" else 0


def _operator_groups(commands) -> dict:
    """Register the operator command groups; returns each group's dispatcher by command."""
    from . import dbip
    from .archive import cli as archive
    from .campaign import cli as campaign
    from .daily import cli as daily
    from .dbserver import cli as dbserver
    from .onboarding import cli as onboarding

    groups = {
        "onboarding": onboarding,
        "campaign": campaign,
        "daily": daily,
        "archive": archive,
        "dbip": dbip,
    }
    groups["db-server"] = dbserver
    for group in groups.values():
        group.register(commands)
    return {name: group.dispatch for name, group in groups.items()}


def _database_option(parser, required: bool = False, help_text: str | None = None) -> None:
    """Add the shared --database-file option."""
    parser.add_argument("--database-file", required=required, help=help_text)


def _add_run_parser(commands) -> None:
    measure = commands.add_parser("run", help="Run a target through a supplied proxy")
    measure.add_argument("--target", choices=[t.id for t in registry.all()], required=True)
    measure.add_argument(
        "--url", help="Echo-IP URL override; must match the target's HTTP/HTTPS scheme"
    )
    measure.add_argument("--requests", type=int, default=100)
    measure.add_argument("--concurrency", type=int, default=10)
    measure.add_argument(
        "--processes", type=int, default=0, help="HTTP worker processes; 0 = CPU count"
    )
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
    _database_option(measure, help_text="Import saved results using private PostgreSQL config")
    measure.add_argument("--provider")
    measure.add_argument("--product")
    measure.add_argument("--country", help="Requested geography label; does not change credentials")
    measure.add_argument("--location", help="Runner location label")
    measure.add_argument(
        "--answer-classes",
        action="store_true",
        help="Replace rate-limited answers (needs --provider-config)",
    )
    measure.add_argument("--mmdb", help="Local MMDB: geo rows and the country check")


def _add_data_parsers(commands) -> None:
    """Register enrich, ingest and the db-* commands."""
    enrich = commands.add_parser("enrich", help="Enrich an existing HTTP result using local MMDB")
    enrich.add_argument("directory")
    enrich.add_argument("--mmdb")
    _database_option(enrich, help_text="Import measurements and new enrichment after saving")
    ingest_parser = commands.add_parser("ingest", help="Import a saved run into PostgreSQL")
    ingest_parser.add_argument("directory")
    _database_option(ingest_parser, required=True)
    for name, help_text in (
        ("db-init", "Apply the PostgreSQL schema"),
        ("db-migrate", "Upgrade a v4 to v7 database in place to the current schema"),
        ("db-status", "Inspect stored runs and cached totals"),
    ):
        _database_option(commands.add_parser(name, help=help_text), required=True)
    seed = commands.add_parser(
        "db-seed", help="Upsert runner regions, countries and targets from configs/"
    )
    _database_option(seed, required=True)
    seed.add_argument("--regions", help="Region patterns YAML (default configs/regions.yaml)")
    seed.add_argument("--targets", help="Targets TOML (default configs/targets.toml)")
    seed.add_argument("--countries", help="Country names JSON (default configs/countries.json)")
    seed.add_argument(
        "--reset-times",
        action="store_true",
        help="Put every census time back on the staggered default slots",
    )
    seed.add_argument(
        "--only",
        choices=("targets",),
        help="Upsert only the targets (keeps their schedules; regions and countries untouched)",
    )
    database = commands.add_parser("db-info", help="Identify the local MMDB and show its checksum")
    database.add_argument("--mmdb")


def _measure(args: argparse.Namespace) -> int:
    return asyncio.run(_run(args))


def _targets(args: argparse.Namespace) -> int:
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
    return 0


def _providers(args: argparse.Namespace) -> int:
    if args.protocol is None:
        print(json.dumps(providers.all()))
    else:
        print(json.dumps(grammar_ids_by_protocol(args.protocol)))
    return 0


def _enrich(args: argparse.Namespace) -> int:
    from .enrichment import enrich_run

    print(json.dumps(enrich_run(args.directory, args.mmdb), indent=2))
    if args.database_file:
        from .storage import ingest

        print(json.dumps({"database_import": ingest(args.directory, args.database_file)}))
    return 0


def _db_seed(args: argparse.Namespace) -> int:
    from .storage import seed, seed_targets

    if args.only and args.reset_times:
        raise UserError("--reset-times changes countries; it cannot go with --only targets")
    if args.only:
        result = seed_targets(args.database_file, args.targets)
    else:
        result = seed(
            args.database_file,
            args.regions,
            args.targets,
            countries_path=args.countries,
            reset_times=args.reset_times,
        )
    print(json.dumps(result, indent=2))
    return 0


def _database(args: argparse.Namespace) -> int:
    """ingest, db-init, db-migrate and db-status."""
    from .calculated.store import initialize as initialize_all
    from .storage import ingest, initialize, migrate, status

    if args.command == "db-init":
        # The orchestrator calculates on the same database: create both layers.
        result = initialize(args.database_file) | initialize_all(args.database_file)
    else:
        result = (
            ingest(args.directory, args.database_file)
            if args.command == "ingest"
            else {"db-migrate": migrate, "db-status": status}[args.command](args.database_file)
        )
    print(json.dumps(result, indent=2))
    return 0


def _db_info(args: argparse.Namespace) -> int:
    from .enrichment import LocalEnricher

    with LocalEnricher(args.mmdb) as enricher:
        print(json.dumps(enricher.release, indent=2))
    return 0


# Each command of this module, by name, with its handler(args) -> exit code.
_HANDLERS = {
    "run": _measure,
    "targets": _targets,
    "providers": _providers,
    "enrich": _enrich,
    "db-seed": _db_seed,
    "ingest": _database,
    "db-init": _database,
    "db-migrate": _database,
    "db-status": _database,
    "db-info": _db_info,
}
_CALCULATED = (
    "calculate-init",
    "calculate",
    "snapshot-publish",
    "snapshot-export",
    "calculate-expire",
    "run-role",
    "metric-evidence",
    "serve",
    "report",
)


def _build_parser() -> tuple[argparse.ArgumentParser, dict]:
    """The parser and each command's handler, in the order commands appear in --help."""
    parser = argparse.ArgumentParser(prog="proxy-index")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("targets", help="List available target definitions")
    provider_list = commands.add_parser("providers", help="List available provider adapters")
    provider_list.add_argument(
        "--protocol",
        choices=NETWORK_PROTOCOLS,
        help="Only networks whose echo test uses this protocol (e.g. https-only: no HTTP test)",
    )
    _add_run_parser(commands)
    _add_data_parsers(commands)
    from .calculated import cli as calculated

    calculated.register(commands)
    from .orchestrator import cli as fleet

    fleet.register(commands)
    from .browser import release as browser_release

    browser_release.register(commands)
    from .browser import node as browser_node

    browser_node.register(commands)
    handlers = _HANDLERS | dict.fromkeys(_CALCULATED, calculated.dispatch)
    handlers |= {"browser": browser_release.dispatch, "browser-node": browser_node.dispatch}
    handlers |= dict.fromkeys(fleet.COMMANDS, fleet.dispatch)
    return parser, handlers | _operator_groups(commands)


def main(argv: list[str] | None = None) -> int:
    parser, handlers = _build_parser()
    args = parser.parse_args(argv)
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        return 130
    except UserError as exc:
        # Written for the user with public values only (errors.py).
        print(f"Command failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # boundary: the CLI's last handler turns any error into exit 2
        # Libraries can put proxy URLs into exception messages; expose only types.
        print(
            f"Command failed ({type(exc).__name__}). Check configuration and file paths.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
