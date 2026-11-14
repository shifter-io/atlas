"""ARCHIVE-RAW-1: size and time of one network-hour of raw files under each zstd setting.

Generates one network's hour of raw files in the shapes the spool holds (a daily run per
country and some census runs, each a .tar.gz of summary.json and ips.jsonl.gz rows as the
runner's sink and `daily ingest` write them), then packs them as the archive does under each
choice and prints bytes, ratio and seconds. Usage: `uv run python scripts/archive/raw_zstd_bench.py
[--countries 40] [--census 12] [--seed 1]`. Generated data only; nothing is uploaded.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import random
import tarfile
import time
import uuid
from datetime import UTC, datetime, timedelta

import zstandard

HOUR = datetime(2026, 11, 14, 10, tzinfo=UTC)


def _ips(rng: random.Random, count: int) -> list[str]:
    prefixes = [(rng.randrange(1, 224), rng.randrange(256)) for _ in range(max(4, count // 40))]
    found = set()
    while len(found) < count:
        a, b = rng.choice(prefixes)
        found.add(f"{a}.{b}.{rng.randrange(256)}.{rng.randrange(1, 255)}")
    return sorted(found)


def _rows(rng: random.Random, run_id: str, unique: int) -> bytes:
    lines = []
    for bucket in range(3):
        start = HOUR + timedelta(minutes=5 * bucket)
        for ip in _ips(rng, unique // 2 + rng.randrange(unique // 2 + 1)):
            first = start + timedelta(seconds=rng.random() * 290)
            row = {
                "schema_version": "1",
                "run_id": run_id,
                "ip": ip,
                "bucket_start": start.isoformat(),
                "count": rng.randrange(1, 40),
                "first_seen": first.isoformat(),
                "last_seen": (first + timedelta(seconds=rng.random() * 9)).isoformat(),
            }
            lines.append(json.dumps(row, separators=(",", ":")))
    return ("\n".join(lines) + "\n").encode()


def _summary(rng: random.Random, run_id: str, country: str) -> bytes:
    stats = {f"p{p}": round(rng.uniform(100, 3000), 3) for p in (50, 75, 90, 95, 99)} | {
        "count": rng.randrange(1000, 90000)
    }
    summary = {
        "schema_version": "1",
        "run_id": run_id,
        "config": {"provider": "decodo", "product": "residential", "concurrency": 2000},
        "target": {"id": "generic_http", "url": "https://example.test/ip"},
        "pool_stats": {"requested_country": country, "unique_ips": rng.randrange(500, 20000)},
        "latency_ms": stats,
        "statuses": {str(s): rng.randrange(0, 5000) for s in (200, 403, 407, 429, 502, 503)},
        "errors": {f"error_{i}": rng.randrange(0, 50) for i in range(30)},
    }
    return json.dumps(summary, indent=2).encode()


def _tar(members: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, data in sorted(members.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), int(HOUR.timestamp()), 0o600
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


def network_hour(countries: int, census: int, seed: int) -> dict[str, bytes]:
    """{file name: .tar.gz bytes}: one network's spooled hour."""
    rng, files = random.Random(seed), {}
    for index in range(countries + census):
        run_id, kind = str(uuid.UUID(int=rng.getrandbits(128))), "daily"
        unique = rng.randrange(500, 20000)
        if index >= countries:
            kind, unique = "census-b7", rng.randrange(50, 2000)
        ips = gzip.compress(_rows(rng, run_id, unique), 9, mtime=0)
        summary = _summary(rng, run_id, f"C{index:02d}")
        files[f"{kind}-{run_id}.tar.gz"] = gzip.compress(
            _tar({"summary.json": summary, "ips.jsonl.gz": ips}), 6, mtime=0
        )
    return files


def _plain_members(data: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data))) as archive:
        members = {m.name: archive.extractfile(m).read() for m in archive.getmembers()}
    return {
        k.removesuffix(".gz"): gzip.decompress(v) if k.endswith(".gz") else v
        for k, v in members.items()
    }


def _measure(label: str, body: bytes, params: zstandard.ZstdCompressionParameters, base: int):
    started = time.perf_counter()
    packed = zstandard.ZstdCompressor(compression_params=params).compress(body)
    took = time.perf_counter() - started
    started = time.perf_counter()
    assert zstandard.ZstdDecompressor(max_window_size=1 << 31).decompress(packed) == body
    back = time.perf_counter() - started
    print(f"| {label} | {len(packed):,} | {len(packed) / base:.3f} | {took:.2f} | {back:.2f} |")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--countries", type=int, default=40)
    parser.add_argument("--census", type=int, default=12)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    files = network_hour(args.countries, args.census, args.seed)
    base = sum(len(v) for v in files.values())
    shapes = {
        "the .tar.gz files as spooled": _tar(files),
        "each run's tar, gzip removed": _tar(
            {k.removesuffix(".gz"): gzip.decompress(v) for k, v in files.items()}
        ),
        "every file decompressed": _tar(
            {f"{k}/{m}": v for k, d in files.items() for m, v in _plain_members(d).items()}
        ),
    }
    print(f"{len(files)} files, {base:,} bytes as .tar.gz (the ratio's base)\n")
    print("| shape, setting | bytes | ratio | compress s | decompress s |")
    print("|---|---|---|---|---|")
    for shape, body in shapes.items():
        for level, long in ((3, 0), (19, 0), (19, 27), (22, 27)):
            params = zstandard.ZstdCompressionParameters.from_level(
                level, source_size=len(body), enable_ldm=bool(long), window_log=long or 0
            )
            _measure(f"{shape}, {level}{' --long' if long else ''}", body, params, base)


if __name__ == "__main__":
    main()
