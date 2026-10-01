"""CLI commands: orchestrator, node and node-token (registered into milf-framework)."""

from __future__ import annotations

import json
import os

COMMANDS = ("orchestrator", "node", "node-token")


def register(commands) -> None:
    serve = commands.add_parser("orchestrator", help="Run the census/pulse orchestrator")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--dev", action="store_true", help="Verbose logs; loopback only")
    serve.add_argument(
        "--database-file",
        default=os.environ.get("MILF_DATABASE_FILE"),
        help="Private PostgreSQL config (default $MILF_DATABASE_FILE)",
    )
    serve.add_argument("--methodology", help="Scoring methodology for the calculate job")

    node = commands.add_parser("node", help="Run a node agent for one provider product")
    node.add_argument("--orchestrator", required=True, help="Orchestrator base URL")
    node.add_argument("--token-file", required=True, help="Owner-only file holding the token")
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
    token.add_argument("--host-name")


def dispatch(args) -> int:
    if args.command == "orchestrator":
        if not args.database_file:
            raise ValueError("--database-file or MILF_DATABASE_FILE is required")
        from .app import serve

        serve(
            args.database_file,
            host=args.host,
            port=args.port,
            dev=args.dev,
            methodology=args.methodology,
        )
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
            mmdb=enricher.path,
        )
        asyncio.run(serve(agent))
    return 0
