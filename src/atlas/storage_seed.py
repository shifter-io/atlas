"""storage.seed: upsert the runner regions, countries and targets from configs/."""

from __future__ import annotations


def _seed_regions(cursor, regions, times, reset_times):
    """Upsert the runner regions and their countries; reset_times resets each census_time."""
    cursor.executemany(
        "INSERT INTO runner_regions(region_id,slug,cloud,pattern) VALUES (%s,%s,%s,%s) "
        "ON CONFLICT(region_id) DO UPDATE SET slug=excluded.slug,cloud=excluded.cloud,"
        "pattern=excluded.pattern",
        [(r.region_id, r.slug, "digitalocean", r.pattern) for r in regions.regions],
    )
    cursor.executemany(
        "INSERT INTO countries(country_code,name,region_id,census_time) "
        "VALUES (%s,%s,%s,%s) ON CONFLICT(country_code) DO UPDATE SET "
        "name=excluded.name,region_id=excluded.region_id,census_time=CASE WHEN %s "
        "THEN excluded.census_time ELSE countries.census_time END",
        [
            (code, regions.names[code], region.region_id, times[code], reset_times)
            for region in regions.regions
            for code in region.countries
        ],
    )


def _seed_targets(cursor, targets):
    """Upsert the target definitions; a seeded target keeps the schedule operators left."""
    definition = ("label", "url", "engine", "pass_condition", "timeout_ms", "sort_order")
    schedule = (
        "census",
        "census_concurrency",
        "pulse_enabled",
        "pulse_interval_minutes",
        "pulse_requests",
        "pulse_concurrency",
    )
    columns = ("target_id", *definition, *schedule)
    # Ingest-added targets have no label; their schedule comes from the seed once.
    cursor.executemany(
        "INSERT INTO targets("
        + ",".join(columns)
        + ") VALUES ("
        + ",".join(["%s"] * len(columns))
        + ") ON CONFLICT(target_id) DO UPDATE SET "
        + ",".join(f"{c}=excluded.{c}" for c in definition)
        + ","
        + ",".join(
            f"{c}=CASE WHEN targets.label IS NULL THEN excluded.{c} ELSE targets.{c} END"
            for c in schedule
        ),
        [tuple(row[c] for c in columns) for row in targets],
    )


def seed(
    credentials_file,
    regions_path=None,
    targets_path=None,
    *,
    countries_path=None,
    reset_times=False,
):
    """Upsert runner_regions, countries and targets from configs/ (atlas db-seed).

    Re-seeding moves countries between regions and refreshes names, patterns and target
    definitions, but keeps each country's census_time and enabled flag and each target's schedule
    as operators left them. reset_times puts every census_time back on the staggered default.
    """
    # Late: storage imports this module; its connect and _lock are read at call time.
    from . import storage, storage_catalog
    from .regions import load_regions, load_targets

    regions = load_regions(regions_path, countries_path)
    targets = load_targets(targets_path)
    config = _methodology(None)
    times = regions.census_times()
    with storage.connect(credentials_file) as conn:
        storage._lock(conn, "seeding")
        with conn.cursor() as cursor:
            _seed_regions(cursor, regions, times, reset_times)
            _seed_targets(cursor, targets)
            catalog = storage_catalog.seed(cursor, targets, config)
    return {
        "regions": len(regions.regions),
        "countries": len(times),
        "targets": [row["target_id"] for row in targets],
        "census_times_reset": reset_times,
        "site_targets": catalog,
    }


def _methodology(path):
    """The methodology the catalog's difficulties come from (default the packaged one)."""
    from .calculated.policy import load
    from .regions import CONFIGS

    return load(path or CONFIGS / "methodology.toml")[0]


def seed_targets(credentials_file, targets_path=None, *, methodology=None, require=False):
    """Upsert only the targets, as seed does (atlas db-seed --only targets): a database
    seeded before a target was added gets it; regions and countries are never read or touched.
    With schema site, the catalog's target rows too (storage_catalog); `require` stops, after the
    commit, when one the methodology needs is still missing or differs."""
    from . import storage, storage_catalog
    from .regions import load_targets

    targets = load_targets(targets_path)
    config = _methodology(methodology)
    with storage.connect(credentials_file) as conn:
        storage._lock(conn, "seeding")
        with conn.cursor() as cursor:
            _seed_targets(cursor, targets)
            catalog = storage_catalog.seed(cursor, targets, config)
    if require:
        storage_catalog.require(catalog, config)
    return {"targets": [row["target_id"] for row in targets], "site_targets": catalog}
