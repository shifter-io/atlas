"""Manual onboarding commands; intentionally no database or upload options."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
from pathlib import Path

from ..providers import load_provider
from .export import export_campaign
from .files import digest, read_json
from .manifest import create, load
from .scheduler import execute, status, validate_state


def register(commands):
    parser = commands.add_parser("onboarding", help="Private 24-hour network onboarding")
    actions = parser.add_subparsers(dest="onboarding_action", required=True)
    plan = actions.add_parser("plan", help="Create the shared 24-hour campaign manifest")
    plan.add_argument("--provider-config", type=Path, required=True)
    plan.add_argument("--product", required=True)
    plan.add_argument(
        "--start", required=True, help="Future whole UTC hour, e.g. 2027-01-01T22:00:00Z"
    )
    plan.add_argument("--output", type=Path, default=Path("onboarding-networks"))
    plan.add_argument("--execution-mode", choices=("parallel", "sequential"), default="parallel")
    plan.add_argument("--region-order", nargs="+", help="Each of the eight regions, in daily order")
    plan.add_argument("--retry-http-469", action="store_true")
    plan.add_argument("--target-url", help="HTTP exit-IP destination")
    plan.add_argument("--concurrency", type=int, default=1000)
    plan.add_argument("--processes", type=int, default=0)
    plan.add_argument("--timeout", type=float, default=10)
    run = actions.add_parser("run", help="Arm or explicitly resume one region")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--region", required=True)
    run.add_argument("--provider-config", type=Path, required=True)
    run.add_argument("--credentials-file", type=Path)
    run.add_argument(
        "--concurrency", type=int, help="Concurrent requests per country (default 1000)"
    )
    run.add_argument("--processes", type=int, help="Worker processes; default 0 = CPU count")
    run.add_argument("--timeout", type=float, help="Request timeout in seconds (default 10)")
    run.add_argument("--resume", action="store_true")
    check = actions.add_parser("status", help="Read local progress, queue and round durations")
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--region", required=True)
    export = actions.add_parser("export", help="Verify collected regions and export observations")
    export.add_argument("--manifest", type=Path, required=True)

    probe = actions.add_parser("probe", help="Single-country census calibration")
    probe.add_argument("--provider-config", type=Path, required=True)
    probe.add_argument("--credentials-file", type=Path)
    probe.add_argument("--country", required=True)
    probe.add_argument("--region", required=True)
    probe.add_argument("--output", type=Path, required=True, help="New private probe directory")
    probe.add_argument("--concurrency", type=int, default=1000)
    probe.add_argument("--processes", type=int, default=0, help="Worker processes; 0 = CPU count")
    probe.add_argument("--timeout", type=float, default=10)
    probe.add_argument(
        "--start", help="Optional future whole UTC hour; otherwise start immediately"
    )
    probe.add_argument("--max-seconds", type=float, default=86400)
    from . import fleet

    fleet.register(actions)


async def _run(args):
    manifest = load(args.manifest)
    if args.region not in manifest["regions"]:
        raise ValueError("Unknown campaign region")
    if digest(args.provider_config) != manifest["provider_config_sha256"]:
        raise ValueError("Provider configuration differs from the campaign manifest")
    # Validate every country before arming. Loading adapters performs no network I/O.
    proxies = {
        country: load_provider(
            args.provider_config,
            parameters={"country": country.lower()},
            credentials_file=args.credentials_file,
        )
        for country in manifest["regions"][args.region]
    }
    for country, proxy in proxies.items():
        public = proxy.public()
        if (
            public.get("provider") != manifest["provider"]
            or public.get("product") != manifest["product"]
            or public.get("requested_country", "").lower() != country.lower()
        ):
            raise ValueError("Provider/product/country targeting does not match the campaign")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
                installed.append(sig)
            except NotImplementedError:
                pass
        result = await execute(
            args.manifest,
            args.region,
            proxies.__getitem__,
            concurrency=args.concurrency,
            processes=args.processes,
            request_timeout=args.timeout,
            resume=args.resume,
            stop=stop,
        )
        print(json.dumps(result, allow_nan=False))
        return 130 if stop.is_set() else 0
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


async def _probe(args):
    from .probe import execute_probe

    proxy = load_provider(
        args.provider_config,
        parameters={"country": args.country.lower()},
        credentials_file=args.credentials_file,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        result = await execute_probe(
            args.output,
            proxy,
            args.region,
            args.country,
            concurrency=args.concurrency,
            processes=args.processes,
            request_timeout=args.timeout,
            start=args.start,
            max_seconds=args.max_seconds,
            stop=stop,
        )
        print(json.dumps(result, allow_nan=False))
        return 130 if stop.is_set() else 0
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


def dispatch(args: argparse.Namespace) -> int:
    if args.onboarding_action == "fleet-status":
        from . import fleet

        return fleet.dispatch(args)
    if args.onboarding_action == "probe":
        return asyncio.run(_probe(args))
    if args.onboarding_action == "plan":
        print(create(
            args.provider_config, args.product, args.start, args.output,
            execution_mode=args.execution_mode, region_order=args.region_order,
            retry_http_469=args.retry_http_469, target_url=args.target_url,
            run_settings={"concurrency": args.concurrency, "processes": args.processes,
                          "timeout_seconds": args.timeout},
        ))
    elif args.onboarding_action == "run":
        return asyncio.run(_run(args))
    elif args.onboarding_action == "status":
        manifest = load(args.manifest)
        if args.region not in manifest["regions"]:
            raise ValueError("Unknown campaign region")
        path = args.manifest.parent / "regions" / args.region / "state.json"
        state = read_json(path)
        validate_state(state, manifest, args.region)
        print(json.dumps(status(state), allow_nan=False))
    else:
        print(json.dumps(export_campaign(args.manifest), allow_nan=False))
    return 0
