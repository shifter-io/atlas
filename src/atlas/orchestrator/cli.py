"""CLI commands: orchestrator, node, node-token, scheduler and staging (registered into
atlas)."""

from __future__ import annotations

import json
import os


def _orchestrator_parser(commands, name):
    serve = commands.add_parser(name, help="Run the census/pulse orchestrator")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--dev", action="store_true", help="Verbose logs; loopback only")
    serve.add_argument(
        "--database-file",
        default=os.environ.get("ATLAS_DATABASE_FILE"),
        help="Private PostgreSQL config (default $ATLAS_DATABASE_FILE)",
    )
    serve.add_argument(
        "--ingest-database-file",
        default=os.environ.get("ATLAS_INGEST_DATABASE_FILE"),
        help="The uploads' PostgreSQL config, a login that may only add run data (default "
        "$ATLAS_INGEST_DATABASE_FILE; unset: --database-file)",
    )
    serve.add_argument("--methodology", help="Scoring methodology for the calculate job")
    serve.add_argument("--tls-cert", help="PEM certificate: answer HTTPS only (with --tls-key)")
    serve.add_argument("--tls-key", help="Owner-only PEM private key of --tls-cert")
    serve.add_argument(
        "--browser-port", type=int, help="Also serve the node API here to browser hosts (mTLS)"
    )
    serve.add_argument("--client-ca", help="PEM CA the browser hosts' certificates are signed by")
    serve.add_argument(
        "--admin-host", default="127.0.0.1", help="The admin API's listener (default loopback)"
    )
    serve.add_argument(
        "--admin-port",
        type=int,
        help="The admin API's port (default: --port + 3, so 8446 for 8443)",
    )


def _node_parser(commands, name):
    node = commands.add_parser(name, help="Run a node agent for one provider product")
    node.add_argument("--orchestrator", required=True, help="Orchestrator base URL")
    node.add_argument("--token-file", required=True, help="Owner-only file holding the token")
    node.add_argument(
        "--ca-file", help="PEM certificate to trust alone for an https orchestrator (the manager's)"
    )
    node.add_argument("--provider", required=True)
    node.add_argument("--product", required=True)
    where = node.add_mutually_exclusive_group(required=True)
    where.add_argument("--region", help="Runner region slug, e.g. fra1")
    where.add_argument("--host-name", help="Host name matched by configs/regions.yaml")
    node.add_argument("--provider-config", required=True, help="Provider TOML")
    node.add_argument(
        "--credentials-file", help="Private credential TOML (owner-only); overrides env"
    )
    node.add_argument("--mmdb", help="Local DB-IP MMDB (default $DBIP_MMDB or db-ip/)")
    node.add_argument("--work-dir", default="node-runs")
    node.add_argument(
        "--max-connections",
        type=int,
        help="The most connections open at once over all runs (a budgeted pulse droplet's share)",
    )
    node.add_argument("--verbose", action="store_true")


def _token_parser(commands, name):
    token = commands.add_parser(
        name, help="Create a node or rotate its token; prints the token once"
    )
    token.add_argument("--database-file", required=True)
    token.add_argument("--provider", required=True)
    token.add_argument("--product", required=True)
    token.add_argument("--region", required=True, help="Region slug or host name")
    token.add_argument("--host-name", "--host", help="Required for --region browser")


def _scheduler_parser(commands, name):
    switch = commands.add_parser(
        name, help="Switch the census and/or pulse planner on or off, or show them"
    )
    switch.add_argument("action", choices=["on", "off", "status"])
    which = switch.add_mutually_exclusive_group()
    which.add_argument("--census", action="store_true", help="Only the census planner")
    which.add_argument("--pulse", action="store_true", help="Only the pulse planner")
    switch.add_argument("--database-file", required=True)


def _staging_parser(commands, name):
    staging = commands.add_parser(name, help="A staging database for the census/pulse loop")
    steps = staging.add_subparsers(dest="staging", required=True)
    init = steps.add_parser(
        "init", help="Create (or with --clean recreate) the staging database from a profile"
    )
    init.add_argument("--config", help="Staging profile TOML (default configs/staging.toml)")
    init.add_argument("--clean", action="store_true", help="Drop and recreate the database")
    init.add_argument(
        "--secrets-dir",
        default=".secrets",
        help="Holds database.toml and postgres-admin.toml; receives staging-database.toml "
        "and staging-tokens/ (default .secrets)",
    )
    init.add_argument(
        "--orchestrator-url",
        default="http://127.0.0.1:8080",
        help="Orchestrator URL used in the printed node commands",
    )


def _orchestrator(args) -> int:
    if not args.database_file:
        raise ValueError("--database-file or ATLAS_DATABASE_FILE is required")
    from .app import Listeners, serve

    serve(
        args.database_file,
        Listeners(
            args.host,
            args.port,
            browser=(args.browser_port, args.client_ca),
            admin=(args.admin_host, args.admin_port),
        ),
        dev=args.dev,
        methodology=args.methodology,
        tls=(args.tls_cert, args.tls_key),
        ingest_file=args.ingest_database_file,
    )
    return 0


def _scheduler(args) -> int:
    from .control import scheduler

    kinds = ["census"] if args.census else ["pulse"] if args.pulse else None
    print(json.dumps(scheduler(args.database_file, args.action, kinds), indent=2))
    return 0


def _staging(args) -> int:
    from .staging import init

    result = init(
        args.config,
        secrets_dir=args.secrets_dir,
        clean=args.clean,
        orchestrator_url=args.orchestrator_url,
    )
    print(json.dumps(result, indent=2))
    return 0


def _token(args) -> int:
    from .nodes import issue_token

    result = issue_token(
        args.database_file,
        provider=args.provider,
        product=args.product,
        region=args.region,
        host=args.host_name,
    )
    print(json.dumps(result, indent=2))
    return 0


def _node(args) -> int:
    import asyncio
    import logging

    from ..enrichment import LocalEnricher
    from ..node import NodeAgent, NodeSettings, provider_factory, read_token, serve
    from ..node_tls import client_tls

    tls = client_tls(args.orchestrator, args.ca_file)  # refuses a URL the node must not use

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Request URLs and proxy handshakes stay out of the log.
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    factory = provider_factory(args.provider_config, args.credentials_file)
    factory("us")  # Fail on a broken provider configuration before registering.
    with LocalEnricher(args.mmdb) as enricher:
        settings = NodeSettings(
            args.orchestrator,
            read_token(args.token_file),
            provider=args.provider,
            product=args.product,
            region=args.region,
            host=args.host_name,
            proxy_factory=factory,
            work_dir=args.work_dir,
            enricher=enricher,
            # The configured path as given: a link is followed again for every run.
            mmdb=enricher.source,
            max_connections=args.max_connections,
        )
        agent = NodeAgent(settings)
        agent.tls = tls
        asyncio.run(serve(agent))
    return 0


# Subcommand -> (parser builder, handler), in the order the commands appear in --help.
COMMANDS = {
    "orchestrator": (_orchestrator_parser, _orchestrator),
    "node": (_node_parser, _node),
    "node-token": (_token_parser, _token),
    "scheduler": (_scheduler_parser, _scheduler),
    "staging": (_staging_parser, _staging),
}


def register(commands) -> None:
    for name, (build, _) in COMMANDS.items():
        build(commands, name)


def dispatch(args) -> int:
    return COMMANDS[args.command][1](args)
