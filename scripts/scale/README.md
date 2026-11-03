# Full-size synthetic data (SCALE-GEN-1a)

`generate.py` fills an empty scratch PostgreSQL 17 database named `proxy_scale` with up to 31 days
of synthetic data for every network that has a formula (`formulas/*.json`, 18 today) and every
country in `configs/countries.json` (249). It exists to measure the hourly calculation, the
database and the API at full size before the pulse fleet starts (SCALE-RUN-1 and SCALE-RUN-2).

No real IP address, real proxy traffic or credential is used: every address, place, network and
counter is generated from a seed.

## Usage

```sh
# Once, as the admin role: an empty database for the test.
createdb proxy_scale
# Then in steps of a few days (each day commits on its own; a step can be rerun).
uv run python scripts/scale/generate.py --database proxy_scale.toml --days 1..4
uv run python scripts/scale/generate.py --database proxy_scale.toml --days 5..8
# ...
uv run python scripts/scale/generate.py --database proxy_scale.toml --days 29..31
# Afterwards, as the admin role:
dropdb proxy_scale
```

`proxy_scale.toml` is a database credentials file (`[postgres]`, as `database.toml.example`)
whose `dbname` is `proxy_scale`.

Options (all recorded by the first call in the settings row `scale.generator`; a later call that
names a different value is refused):

| Option | Default | Meaning |
|---|---|---|
| `--seed` | 1 | every generated value follows from it |
| `--networks` | all | `all`, the first N networks (by name) or a comma list |
| `--countries` | all | `all`, the first N countries (by code) or a comma list |
| `--total-days` | 31 | days in the data set |
| `--end-date` | today (UTC) | the last day ends at 00:00 of this date |
| `--scale` | 1.0 | multiplies every daily test's unique IPs (for smaller trials) |
| `--formulas` | `formulas/` | the hour formulas |

The first call creates the storage schema (`db-init`'s storage half) and seeds regions,
countries and targets (`db-seed`). The calculated schema is created by `proxy-index db-init`, as
SCALE-RUN-1 does. Days must be generated in order; a day already done is skipped.

## Guards

- It writes only to a database named `proxy_scale`.
- It refuses a database holding any `test_runs` row it did not write (its runs carry the summary
  key `scale_generator`), and a storage schema other than this framework's.
- A day is one transaction: an interrupted step leaves whole days only.

## What one day holds

Per network and country:

- **One daily test** at the formula's test hour (`schedule`), stored as `daily ingest` stores one:
  `test_runs` (summary `test_kind` `daily`), 2-4 five-minute `run_buckets`, one `ip_observations`
  row per IP and bucket (every third IP in two buckets), `run_enrichments`, and the 24
  `estimated_hours` from `estimates.compute.hours`. Its unique IPs are the formula's figure at the
  test hour (±10% a day), and its requests the formula's requests per IP. A country the formula
  lacks (12 countries have no formula at all) uses the network's 10th-percentile country and the
  network curve. Hours are released once they have ended within the generated days.
- **24 pulses**, one an hour on `generic_http`: a `test_batches` row per country and hour, and per
  network a `test_runs`, `batch_runs` and `run_buckets` row with 100 requests and 47-92
  `run_geo_buckets` rows (location × network). Pulses store no per-IP rows, as on the nodes.
- **IPs.** Each network × country has its own block of synthetic addresses: IPv4 in
  the reserved IPv4 block 240/4 (class E) and IPv6 in `2001:db8::/32`. 30-60% of a test's IPs (one share per network) were
  also in the day before's test, and about 2% more come from another country of the same network.
  `ip_addresses`, `product_ips`, `pool_ips` and `pool_stats.unique_ips` are kept as ingest keeps
  them.
- **DB-IP.** One synthetic release per day; every IP of the day is enriched with it (about 0.5%
  unmatched). Places and ASNs are synthetic lists sized like DB-IP: up to 5,000 cities, 60
  regions and 900 ASNs in the largest countries; every 33rd ASN is a hosting network.

At full size a day is about 23 M `ip_observations` rows, 15-17 M `ip_enrichment` rows, 107,568
pulse runs with about 7.6 M `run_geo_buckets` rows and 4,482 daily tests, matching the DB-AUDIT-2
scale model.

## Not generated

Census and load runs (off in production), `test_events`, outages, nodes, `load_references`,
`provider_overlap` (no IP is shared between networks) and per-scope lifetime `metrics` (left
`{}`). SCALE-GEN-1b drives the hourly cycle on top of this data through the real ingest, release,
calculate and API paths.

## Tests

`tests/test_scale_gen_model.py` checks the model without a database;
`tests/test_scale_gen.py` generates a 2 × 3 × 2-day miniature in PostgreSQL and checks that every
table has the rows `synthetic.plan` counts, that steps give the same rows as one run, that the
guards refuse, and that the calculator reads the result.
