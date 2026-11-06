"""Small explicit local-only live validation; credentials stay in a private file."""

import argparse
import asyncio
import gzip
import hashlib
import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from proxy_index import JsonSink, RunConfig, RunOptions, load_provider, run
from proxy_index.enrichment import LocalEnricher


async def check(args):
    os.umask(0o077)
    output = Path(args.output)
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    results = []
    with LocalEnricher(args.mmdb) as enricher:
        for target in ("generic_http", "generic_https"):
            for country, count in args.country:
                provider = load_provider(
                    args.config, credentials_file=args.credentials, parameters={"country": country}
                )
                observations = []

                async def observe(ip, timestamp, observations=observations, country=country):
                    observations.append(
                        {
                            "ip": ip,
                            "requested_country": country,
                            "observed_at": datetime.fromtimestamp(timestamp, UTC).isoformat(),
                            "geo": enricher.lookup(ip),
                        }
                    )

                sink = JsonSink(output)
                result = await run(
                    RunConfig(
                        target,
                        provider,
                        requests=int(count),
                        concurrency=10,
                        processes=4,
                        timeout_seconds=15,
                    ),
                    sink,
                    RunOptions(enricher=enricher, observation_consumer=observe),
                )
                path = sink.directory / "observations.jsonl.gz"
                with gzip.open(path, "wt") as stream:
                    for row in observations:
                        stream.write(json.dumps(row, separators=(",", ":")) + "\n")
                summary = result.to_dict()
                for artifact in summary["artifacts"].values():
                    raw = (sink.directory / artifact["file"]).read_bytes()
                    assert hashlib.sha256(raw).hexdigest() == artifact["sha256"]
                    assert len(gzip.decompress(raw).splitlines()) == artifact["rows"]
                assert len(observations) == result.successes
                countries = Counter(row["geo"]["country_code"] or "unknown" for row in observations)
                row = {
                    "run_id": result.run_id,
                    "target": target,
                    "requested_country": country,
                    "port": provider.port,
                    "requests": summary["completed"],
                    "successes": result.successes,
                    "unique_ips": result.unique_ips,
                    "geo_countries": dict(countries),
                    "geo_matches": countries[country.upper()],
                    "errors": result.errors,
                    "status_codes": result.status_codes,
                    "directory": str(sink.directory),
                    "observations_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                results.append(row)
                (output / "validation.json").write_text(
                    json.dumps(
                        {
                            "checked_at": datetime.now(UTC).isoformat(),
                            "local_only": True,
                            "config": args.config,
                            "geolocation_release": enricher.release,
                            "results": results,
                        },
                        indent=2,
                    )
                    + "\n"
                )
                print(json.dumps(row), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--credentials", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mmdb", default=None)
    parser.add_argument(
        "--country",
        nargs=2,
        action="append",
        metavar=("CODE", "COUNT"),
        required=True,
        help="Repeat to test multiple country filters per protocol",
    )
    asyncio.run(check(parser.parse_args()))
