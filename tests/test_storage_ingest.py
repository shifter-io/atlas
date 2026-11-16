"""The ingest steps' rejections: each rolls the whole import back."""

import gzip
import hashlib
import json

import pytest
from test_storage import enrichment, measurement

from atlas.storage import ingest, status

pytest_plugins = ["test_storage"]


def rewrite_ips(directory, change, *, extra=0):
    """Apply change to every IP row (`extra` copies of the first one added) and record the
    artifact's new checksum in the summary."""
    source = directory / "ips.jsonl.gz"
    with gzip.open(source, "rt") as stream:
        rows = [json.loads(line) for line in stream]
    rows += rows[:1] * extra
    with gzip.open(source, "wt") as stream:
        stream.writelines(json.dumps(change(row)) + "\n" for row in rows)
    summary_path = directory / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary["artifacts"]["ips"]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    summary_path.write_text(json.dumps(summary))


def rewrite_enrichment(directory, change, release="a" * 64):
    path = directory / f"enriched-{release}.jsonl.gz"
    with gzip.open(path, "rt") as stream:
        rows = [json.loads(line) for line in stream]
    with gzip.open(path, "wt") as stream:
        stream.writelines(json.dumps(change(row)) + "\n" for row in rows)


async def test_an_ip_row_of_another_run_is_rejected(database, tmp_path):
    directory = await measurement(tmp_path)
    rewrite_ips(directory, lambda row: row | {"run_id": "another"})
    with pytest.raises(ValueError, match="Invalid IP observation"):
        ingest(directory, database)
    assert status(database)["counts"]["test_runs"] == 0


async def test_ip_rows_past_the_declared_count_are_rejected(database, tmp_path):
    """SEC-UPLOAD-1: reading stops at the first row past the summary's count."""
    directory = await measurement(tmp_path)
    rewrite_ips(directory, lambda row: row, extra=1)
    with pytest.raises(ValueError, match="exceed the declared count"):
        ingest(directory, database)
    assert status(database)["counts"]["test_runs"] == 0


async def test_a_timestamp_without_a_timezone_is_rejected(database, tmp_path):
    directory = await measurement(tmp_path)
    rewrite_ips(directory, lambda row: row | {"first_seen": row["first_seen"][:19]})
    with pytest.raises(ValueError, match="require a timezone"):
        ingest(directory, database)
    assert status(database)["counts"]["test_runs"] == 0


async def test_an_enrichment_manifest_with_other_rows_is_rejected(database, tmp_path):
    directory = await measurement(tmp_path)
    enrichment(directory)
    path = directory / f"enrichment-{'a' * 64}.json"
    path.write_text(json.dumps(json.loads(path.read_text()) | {"rows": 99}))
    with pytest.raises(ValueError, match="Invalid enrichment manifest"):
        ingest(directory, database)
    assert status(database)["counts"]["test_runs"] == 0


async def test_an_enrichment_row_of_another_release_is_rejected(database, tmp_path):
    directory = await measurement(tmp_path)
    enrichment(directory)
    rewrite_enrichment(directory, lambda row: row | {"dbip_release": "b" * 64})
    with pytest.raises(ValueError, match="another run or release"):
        ingest(directory, database)
    assert status(database)["counts"]["test_runs"] == 0


async def test_a_changed_enrichment_is_rejected_on_replay(database, tmp_path):
    directory = await measurement(tmp_path)
    enrichment(directory)
    assert ingest(directory, database)["enrichment_releases_added"] == 1
    rewrite_enrichment(directory, lambda row: row | {"enrichment": {"country_code": "DE"}})
    with pytest.raises(ValueError, match="Previously imported enrichment has changed"):
        ingest(directory, database)
    assert status(database)["counts"]["run_enrichments"] == 1
