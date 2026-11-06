"""CLI commands: orchestrator, node, node-token, scheduler and staging (registered into
proxy-index)."""

from __future__ import annotations

import json
import os

COMMANDS = ("orchestrator", "node", "node-token", "scheduler", "staging")


def register(commands) -> None:
    serve = commands.add_parser("orchestrator", help="Run the census/pulse orchestrator")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--dev", action="store_true", help="Verbose logs; loopback only")
    serve.add_argument(
        "--database-file",
        default=os.environ.get("PROXY_INDEX_DATABASE_FILE"),
        help="Private PostgreSQL config (default $PROXY_INDEX_DATABASE_FILE)",
    )
    serve.add_argument("--methodology", help="Scoring methodology for the calculate job")
    serve.add_argument("--tls-cert", help="PEM certificate: answer HTTPS only (with --tls-key)")
    serve.add_argument("--tls-key", help="Owner-only PEM private key of --tls-cert")
    serve.add_argument(
        "--browser-port", type=int, help="Also serve the node API here to browser hosts (mTLS)"
    )
    serve.add_argument("--client-ca", help="PEM CA the browser hosts' certificates are signed by")

    node = commands.add_parser("node", help="Run a node agent for one provider product")
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
    node.add_argument("--verbose", action="store_true")

    token = commands.add_parser(
        "node-token", help="Create a node or rotate its token; prints the token once"
    )
    token.add_argument("--database-file", required=True)
    token.add_argument("--provider", required=True)
    token.add_argument("--product", required=True)
    token.add_argument("--region", required=True, help="Region slug or host name")
    token.add_argument("--host-name", "--host", help="Required for --region browser")

    switch = commands.add_parser(
        "scheduler", help="Switch the census and/or pulse planner on or off, or show them"
    )
    switch.add_argument("action", choices=["on", "off", "status"])
    which = switch.add_mutually_exclusive_group()
    which.add_argument("--census", action="store_true", help="Only the census planner")
    which.add_argument("--pulse", action="store_true", help="Only the pulse planner")
    switch.add_argument("--database-file", required=True)

    staging = commands.add_parser("staging", help="A staging database for the census/pulse loop")
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


def dispatch(args) -> int:
    if args.command == "orchestrator":
        if not args.database_file:
            raise ValueError("--database-file or PROXY_INDEX_DATABASE_FILE is required")
        from .app import serve

        serve(
            args.database_file,
            host=args.host,
            port=args.port,
            dev=args.dev,
            methodology=args.methodology,
            tls=(args.tls_cert, args.tls_key),
            browser=(args.browser_port, args.client_ca),
        )
        return 0
    if args.command == "scheduler":
        from .control import scheduler

        kinds = ["census"] if args.census else ["pulse"] if args.pulse else None
        print(json.dumps(scheduler(args.database_file, args.action, kinds), indent=2))
        return 0
    if args.command == "staging":
        from .staging import init

        result = init(
            args.config,
            secrets_dir=args.secrets_dir,
            clean=args.clean,
            orchestrator_url=args.orchestrator_url,
        )
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "node-token":
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
    import asyncio
    import logging

    from ..enrichment import LocalEnricher
    from ..node import NodeAgent, provider_factory, read_token, serve
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
        agent = NodeAgent(
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
        )
        agent.tls = tls
        asyncio.run(serve(agent))
    return 0
