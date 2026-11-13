"""Raw-data archive: one compressed, re-importable file per UTC day (ARCHIVE-1).

Raw measurement rows stay `partitions.RAW_DAYS` (30) days in the database. Once everything
tied to a day is older than that, the day is exported (export.py) into `YYYY-MM-DD.tar.gz`:
a `manifest.json` and one COPY-format CSV per table (tables.py lists them, their roles and
which rows belong to the day); restore.py loads a file back into a database with the same
storage schema. job.py (the orchestrator's daily `archive` job) uploads each due day's file to
Spaces (spaces.py), verifies it, and only then purge.py deletes the day's rows, dropping its
daily partitions.
"""

FORMAT = "proxy-index-raw-day"
FORMAT_VERSION = 1


class ArchiveError(RuntimeError):
    """A day cannot be archived, deleted or restored as asked; nothing was changed."""
