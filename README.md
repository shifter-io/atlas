# MILF framework

Standalone Python 3.12+ library and CLI for HTTP/SOCKS5 proxy measurements.
MIT licensed. No database server or scheduler is required.

Open [the standalone HTML guide](docs/index.html) for setup, API examples,
measurement definitions, result contracts and the implemented PostgreSQL schema.

```sh
uv sync --locked --no-editable
uv run --no-editable milf-framework targets
# Set PROXY_URL privately in your environment first.
uv run --no-editable milf-framework run --target generic_http --requests 5000 --concurrency 100
uv run --no-editable milf-framework enrich results/RUN_ID
```

Shifter Residential: see [the adapter guide](docs/shifter-parameters.html).
Copy `credentials.toml.example` to `.secrets/credentials.toml`, replace the placeholders,
and set directory/file permissions to `700`/`600`. The private copy is excluded from
Git, packages and remote uploads. Then:

```sh
uv run --no-editable milf-framework run --provider-config configs/shifter.toml \
  --credentials-file .secrets/credentials.toml --target generic_http \
  --url http://checkip.amazonaws.com/ --requests 5000 --concurrency 100 --param country=us
```

Both targets support the same provider config; optional per-test parameters and
session policies are exposed through the CLI and `ShifterProxy`/`load_provider` Python API.
Target URLs can be supplied with `--url` or `RunConfig(target_url=...)`.
In v0.2 custom transports must accept `execute(target, *, context=None)`.

The locally supplied DB-IP MMDB belongs in `db-ip/` (or set `DBIP_MMDB`). It is
excluded from version control and distributions. It is separate from PostgreSQL:
MMDB provides optional offline enrichment; PostgreSQL persists history and cached lifetime metrics.

PostgreSQL: this project uses a dedicated local cluster on `127.0.0.1:55432`.
Run `uv run --no-editable python scripts/postgres_local.py start` after reboot.
Private database credentials are in `.secrets/database.toml`; the public template is
`database.toml.example`. Add `--database-file .secrets/database.toml` to `run` or
`enrich` for automatic ingestion, or use `milf-framework ingest results/RUN_ID`
with the same flag. Replaying an import does not double-count data. See the HTML
guide for initialization on a new machine and the schema.

Tests: `uv run --no-editable pytest`;
real database tests: `PG_TEST_ADMIN_FILE=.secrets/postgres-admin.toml uv run --no-editable pytest`;
scale benchmark: `uv run --no-editable python scripts/benchmark.py`.
All source changes are maintained locally; `scripts/test-remote.sh` uploads a
dedicated copy to the Mac Mini and downloads evidence. See the HTML guide.

Schema v4 stores each IP once and references it from product membership, history and
versioned enrichment. Lifetime provider overlap is cached and deduplicated across products.
See `docs/index.html#database` for keys, readable views and rebuild/backup details.

All 14 known enrichment fields use shared typed location/network records, including
connection/usage type, coordinates and organizations. `ip_enrichment_details` exposes them.
Only unknown future fields use `extra_attributes`. Numeric internal run keys reduce history
storage; external UUIDs and portable artifacts stay unchanged. `storage.read_summary()`
reconstructs full summaries without repeated bucket/histogram/configuration JSON.

`db-init` verifies v4 and refuses older schemas. The local `scripts/rebuild_database.py`
backs up, stages, verifies and swaps a rebuild from matching saved artifacts, retaining the
previous database. See the HTML guide for its scope and prerequisites. Pool membership is
shared across performance settings; product lifetime summaries and detailed history remain
separate. Measurements: `artifacts/storage-optimization.html`.

## Calculated website data (v0.8)

The existing HTML guide now includes the calculated schema, API and command workflow.
Collection still runs independently; calculation requires PostgreSQL and never sends proxy traffic.

```sh
milf-framework calculate-init --database-file .secrets/database.toml
milf-framework run-role RUN_UUID --role pool_sampling --database-file .secrets/database.toml
milf-framework calculate --methodology configs/methodology.toml --publish --database-file .secrets/database.toml
milf-framework snapshot-export ../src/data/dataset.json --database-file .secrets/database.toml
milf-framework serve --database-file .secrets/database.toml
```

From the website root, `npm run build` exports the measured dataset and builds the site;
`astro dev --background` serves it locally. `MEASUREMENTS_URL` defaults to `http://127.0.0.1:8081`.
Refreshes use the same-origin `/api/measurements/v1/snapshots/current` endpoint.
The development API helper is `python scripts/measurements_local.py start|status|stop`.
TOML is data-only and versioned by SHA-256. The balanced profile uses `[weights]`;
alternative profiles use their own weights. No overall score is emitted for missing required inputs.
The current run is explicitly a pool sample. Scoring thresholds and weights remain a draft.
