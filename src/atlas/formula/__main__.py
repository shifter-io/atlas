"""Command line: python -m atlas.formula {aggregate,build,best-hours,spread}.

aggregate reads the private hour enrichment (one directory per network) and writes one counts
file per network (no IPs); build turns those counts into the public formulas/<network>.json files
and formulas/SUMMARY.md; `--borrow <network>` gives that network the other networks' average curve
(formula/borrow.py); best-hours writes each formula's best hour per country to
formulas/best-hours/<network>.json (formula/best_hours.py); `spread --network <network>` gives that
network an even 24-hour schedule with the other networks' curves (formula/spread.py) and rewrites
SUMMARY.md. Each written file replaces the old one atomically.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from atlas.formula import best_hours, borrow, spread
from atlas.formula.aggregate import FormulaError, aggregate_network
from atlas.formula.build import (
    DEFAULT_MIN_HOURS,
    DEFAULT_MIN_PEAK_IPS,
    SCHEMAS,
    build_formula,
    dumps,
    summary_markdown,
)


def _write(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.partial")
    temporary.write_text(text)
    os.replace(temporary, path)


def _networks(directory: Path, names: list[str], marker: str) -> list[Path]:
    if names:
        return [directory / name for name in names]
    return sorted(path.parent for path in directory.glob(f"*/{marker}"))


def run_aggregate(args: argparse.Namespace) -> None:
    args.out.mkdir(parents=True, exist_ok=True)
    for network_dir in _networks(args.enrich_dir, args.network, "manifest.json"):
        target = args.out / f"{network_dir.name}.json"
        if target.exists() and not args.force:
            print(f"{network_dir.name}: kept {target.name}", flush=True)
            continue
        counts = aggregate_network(network_dir)
        _write(target, json.dumps(counts, sort_keys=True) + "\n")
        print(
            f"{network_dir.name}: {counts['day_unique_ips']} day IPs, "
            f"{len(counts['countries'])} countries",
            flush=True,
        )


def run_build(args: argparse.Namespace) -> None:
    args.out.mkdir(parents=True, exist_ok=True)
    documents = []
    for path in sorted(args.aggregates.glob("*.json")):
        counts = json.loads(path.read_text())
        documents.append(build_formula(counts, args.version, args.min_peak_ips, args.min_hours))
    if not documents:
        raise FormulaError(f"no counts files in {args.aggregates}")
    unknown = set(args.borrow) - {document["network"] for document in documents}
    if unknown:
        raise FormulaError(f"--borrow names networks without counts: {sorted(unknown)}")
    donors = [document for document in documents if document["network"] not in args.borrow]
    for document in documents:
        if document["network"] in args.borrow:
            borrow.apply(document, donors)
        _write(args.out / f"{document['network']}.json", dumps(document))
    _write(args.out / "SUMMARY.md", summary_markdown(documents))
    print(f"built {len(documents)} formulas", flush=True)


def run_best_hours(args: argparse.Namespace) -> None:
    documents = [json.loads(path.read_text()) for path in sorted(args.formulas.glob("*.json"))]
    documents = [document for document in documents if document.get("schema") in SCHEMAS]
    if not documents:
        raise FormulaError(f"no formula files in {args.formulas}")
    args.out.mkdir(parents=True, exist_ok=True)
    for result in best_hours.build_all(documents):
        _write(args.out / f"{result['network']}.json", dumps(result))
    print(f"wrote {len(documents)} best-hour files", flush=True)


def run_spread(args: argparse.Namespace) -> None:
    documents = [json.loads(path.read_text()) for path in sorted(args.formulas.glob("*.json"))]
    documents = [document for document in documents if document.get("schema") in SCHEMAS]
    found = [document for document in documents if document["network"] == args.network]
    if not found:
        raise FormulaError(f"no formula of {args.network} in {args.formulas}")
    others = [d for d in documents if d["network"] != args.network]
    donors = [d for d in others if "borrowed" not in d and "spread" not in d]
    document = spread.spread(found[0], donors)
    _write(args.formulas / f"{args.network}.json", dumps(document))
    _write(args.formulas / "SUMMARY.md", summary_markdown(documents))
    print("\n".join(spread.report(document)), flush=True)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m atlas.formula")
    commands = root.add_subparsers(dest="command", required=True)
    aggregate = commands.add_parser("aggregate", help="count hour files per country (no IPs)")
    aggregate.add_argument("--enrich-dir", type=Path, required=True)
    aggregate.add_argument("--out", type=Path, required=True)
    aggregate.add_argument("--network", action="append", default=[])
    aggregate.add_argument("--force", action="store_true", help="recount existing counts files")
    aggregate.set_defaults(run=run_aggregate)
    build = commands.add_parser("build", help="write the public formula files")
    build.add_argument("--aggregates", type=Path, required=True)
    build.add_argument("--out", type=Path, required=True)
    build.add_argument("--version", type=int, required=True)
    build.add_argument("--min-peak-ips", type=int, default=DEFAULT_MIN_PEAK_IPS)
    build.add_argument("--min-hours", type=int, default=DEFAULT_MIN_HOURS)
    build.add_argument(
        "--borrow",
        action="append",
        default=[],
        metavar="NETWORK",
        help="temporary: give NETWORK the average curve of the other networks (see borrow.py)",
    )
    build.set_defaults(run=run_build)
    best = commands.add_parser("best-hours", help="write each formula's best hour per country")
    best.add_argument("--formulas", type=Path, required=True)
    best.add_argument("--out", type=Path, required=True)
    best.set_defaults(run=run_best_hours)
    even = commands.add_parser("spread", help="give a network an even 24-hour schedule")
    even.add_argument("--formulas", type=Path, required=True)
    even.add_argument("--network", required=True)
    even.set_defaults(run=run_spread)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        args.run(args)
    except FormulaError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
