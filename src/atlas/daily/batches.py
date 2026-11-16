"""An hour's daily tests stored in batches (INGEST-BATCH-2): the converted countries of a batch
go into the database through one `storage.ingest_many` and their estimated hours through one
`compute.estimate_runs`, a fixed number of round trips whatever the batch's size. The batches of
an hour go over several connections at once (storage_parallel, INGEST-PARALLEL-1).

A batch that fails is retried one country at a time (`storage.ingest`, `compute.estimate_run`),
so each failing country gets the issue it gets when imported alone, and only it fails.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from .. import storage, storage_parallel
from ..archive import rawfiles
from ..estimates import compute

IMPORT_ERRORS = (OSError, ValueError, KeyError, psycopg.Error)
ESTIMATE_ERRORS = (KeyError, ValueError, ZeroDivisionError, psycopg.Error)


@dataclass
class Country:
    """A due country of a cell: its converted and enriched run directory, then what storing it
    gave; `issue` (not imported) is set as soon as a step fails. `scheduled` is the hour its
    test was due (the campaign's slot), the hour its estimate counts for."""

    code: str
    entry: dict
    formula: dict | None = None
    scheduled: datetime | None = None
    directory: Path | None = None
    issue: str | None = None
    spool_issue: str | None = None
    estimate_issue: str | None = None
    estimated: bool = False
    stopped: bool = False  # stopped by the connection budget: stored with stop_reason budget

    def fail(self, exc: Exception) -> None:
        self.issue = f"{self.code}: not imported ({type(exc).__name__})"


def store(
    groups: list[tuple[list[Country], frozenset]],
    credentials: Path,
    options: storage_parallel.Options | None = None,
    on_commit=None,
) -> None:
    """Import each group's countries not failed yet as one batch, the batches over
    options.connections connections at once over options.processes worker processes, or with
    its open options.pool (storage_parallel.store, which passes on_commit),
    with the estimated hours of those with a formula; a group is (countries, their networks)."""
    ready, jobs = groups_of(groups)
    options = options or storage_parallel.Options()
    found = storage_parallel.store(jobs, credentials, **vars(options), on_commit=on_commit)
    for (countries, _), outcome in zip(ready, found, strict=True):
        if outcome.error is not None:  # retried one at a time: each raises its own error
            _one_at_a_time(countries, credentials)
        imported = [country for country in countries if country.issue is None]
        estimated = [country for country in imported if country.formula is not None]
        if outcome.error is None and outcome.estimate_error is None:
            for country in estimated:
                country.estimated = True
        else:
            _estimate(estimated, credentials)


def groups_of(groups: list[tuple[list[Country], frozenset]]):
    """Each group's countries not failed yet (groups with none left out), with its networks, and
    its storage_parallel.Group."""
    ready = [([c for c in own if c.issue is None], networks) for own, networks in groups]
    ready = [(countries, networks) for countries, networks in ready if countries]
    jobs = [storage_parallel.Group([c.directory for c in own], _runs(own), n) for own, n in ready]
    return ready, jobs


def _runs(countries: list[Country]) -> list[tuple]:
    """The estimated hours to store: (run_id, country, formula, scheduled) of those with a
    formula."""
    return [_run(c) for c in countries if c.formula is not None]


def _run(country: Country) -> tuple:
    return (country.directory.name, country.code, country.formula, country.scheduled)


def spool(country: Country, root: Path | None, network: str) -> None:
    """Keep the country's converted run directory as its raw file (ARCHIVE-RAW-1: before its
    enrichment; 30 days under root, then Spaces); a failure is the country's spool_issue."""
    if root is not None:
        name, moment = f"daily-{country.directory.name}.tar.gz", datetime.now(UTC)
        found = rawfiles.try_spool(root, moment, network, name, country.directory, move=False)
        country.spool_issue = found


def _one_at_a_time(countries: list[Country], credentials: Path) -> None:
    for country in countries:
        try:
            storage.ingest(country.directory, credentials)
        except IMPORT_ERRORS as exc:
            country.fail(exc)


def _estimate(countries: list[Country], credentials: Path) -> None:
    if not countries:
        return
    runs = [_run(country) for country in countries]
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
