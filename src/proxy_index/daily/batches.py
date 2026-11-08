"""An hour's daily tests stored in batches (INGEST-BATCH-2): the converted countries of a batch
go into the database through one `storage.ingest_many` and their estimated hours through one
`compute.estimate_runs`, a fixed number of round trips whatever the batch's size.

A batch that fails is retried one country at a time (`storage.ingest`, `compute.estimate_run`),
so each failing country gets the issue it gets when imported alone, and only it fails.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from .. import storage
from ..archive import rawfiles
from ..estimates import compute

BATCH = 20  # daily tests per transaction (`daily ingest --batch`)
IMPORT_ERRORS = (OSError, ValueError, KeyError, psycopg.Error)
ESTIMATE_ERRORS = (KeyError, ValueError, ZeroDivisionError, psycopg.Error)


@dataclass
class Country:
    """A due country of a cell: its converted and enriched run directory, then what storing it
    gave; `issue` (not imported) is set as soon as a step fails."""

    code: str
    entry: dict
    formula: dict | None = None
    directory: Path | None = None
    issue: str | None = None
    spool_issue: str | None = None
    estimate_issue: str | None = None
    estimated: bool = False

    def fail(self, exc: Exception) -> None:
        self.issue = f"{self.code}: not imported ({type(exc).__name__})"


def store(countries: list[Country], credentials: Path, spool: Path | None) -> None:
    """Import the countries not failed yet as one batch, keep each imported run's raw files when
    `spool` is set, then store the estimated hours of those with a formula."""
    ready = [country for country in countries if country.issue is None]
    _import(ready, credentials)
    imported = [country for country in ready if country.issue is None]
    for country in imported if spool else ():  # ARCHIVE-1: 30 days here, then Spaces
        name = f"daily-{country.directory.name}.tar.gz"
        moment = datetime.now(UTC)
        country.spool_issue = rawfiles.try_spool(spool, moment, name, country.directory, move=False)
    _estimate([country for country in imported if country.formula is not None], credentials)


def _import(countries: list[Country], credentials: Path) -> None:
    if not countries:
        return
    try:
        storage.ingest_many([country.directory for country in countries], credentials)
        return
    except (storage.IngestBatchError, *IMPORT_ERRORS):
        pass  # retried one at a time below: each country raises its own error
    for country in countries:
        try:
            storage.ingest(country.directory, credentials)
        except IMPORT_ERRORS as exc:
            country.fail(exc)


def _estimate(countries: list[Country], credentials: Path) -> None:
    if not countries:
        return
    runs = [(country.directory.name, country.code, country.formula) for country in countries]
    try:
        compute.estimate_runs(credentials, runs)
    except ESTIMATE_ERRORS:  # nothing was stored: find the countries that fail
        for country, run in zip(countries, runs, strict=True):
            try:
                compute.estimate_run(credentials, *run)
            except ESTIMATE_ERRORS as exc:
                country.estimate_issue = f"{country.code}: no estimates ({type(exc).__name__})"
            else:
                country.estimated = True
        return
    for country in countries:
        country.estimated = True
