"""The shared place and ASN name lists, and the one routine that fills them.

place_names and asn_names hold every place and ASN name once; enrichment_locations (a place at a
coordinate) and enrichment_networks (an ASN name with per-IP network attributes) refer to them,
and so does the calculated layer. Every list is filled the same way: the distinct rows of a source
query are added with ON CONFLICT DO NOTHING, and each source row then finds its id through the
list's unique key (lookup()), a NULL-safe match that reads one index entry, never the whole list.
The all-NULL row of each list is its unknown member. The calculated layer's labels (labels())
come from Python values and are matched through each list's unique key too (_matched()).
"""

from __future__ import annotations

from collections import defaultdict
from itertools import product

from psycopg.rows import tuple_row

PLACE = ("country_code", "country", "region_code", "region", "city")
# Each list's identity column and the fields that identify one of its rows.
LISTS = {
    "place_names": ("place_id", PLACE),
    "asn_names": ("asn_name_id", ("asn", "name")),
    "enrichment_locations": ("location_id", ("place_id", "latitude", "longitude")),
    "enrichment_networks": (
        "network_id",
        ("asn_name_id", "isp", "organization", "connection_type", "usage_type"),
    ),
    # Calculated layer: a breakdown's ASN label is an ASN name with a usage type.
    "asn_labels": ("asn_label_id", ("asn_name_id", "usage_type")),
}
# The fields of a list that are never NULL (the ids of the lists it refers to).
NOT_NULL = {"enrichment_locations": ("place_id",), "enrichment_networks": ("asn_name_id",)}
# Column types of the lists that labels() fills from Python values.
TYPES = {
    "place_names": ("text",) * 5,
    "asn_names": ("bigint", "text"),
    "asn_labels": ("integer", "text"),
}
# A calculated breakdown label's ASN fields, as calculated.aggregate and its persist use them.
NETWORK = ("asn", "name", "usage_type")
# v8 → v9 (migrate_v8_v9.sql keeps the v8 rows): each count is of rows whose v9 view output
# differs from the v8 row, so every location, network and per-IP enrichment row must be equal.
V8_CHECKS = {
    name: f"SELECT count(*) FROM v8_before_{before} o FULL JOIN {after} n USING({keys}) "
    "WHERE to_jsonb(o)::text IS DISTINCT FROM to_jsonb(n)::text"
    for name, before, after, keys in (
        ("enrichment locations", "locations", "enrichment_location_fields", "location_id"),
        ("enrichment networks", "networks", "enrichment_network_fields", "network_id"),
        ("ip enrichment", "ip_enrichment", "ip_enrichment_fields", "ip_id,release_id"),
    )
}


def fill(conn, table, values, source, params=()):
    """Add each distinct row of `values` (SQL expressions over `source`) to the list `table`, in
    the order of its key: imports filling the same list at once never wait on each other."""
    columns, order = ",".join(LISTS[table][1]), ",".join(map(str, range(1, len(values) + 1)))
    conn.execute(
        f"INSERT INTO {table} ({columns}) SELECT DISTINCT {','.join(values)} FROM {source} "
        f"ORDER BY {order} ON CONFLICT DO NOTHING",
        params,
    )


def lookup(table, values):
    """A join that names `table`'s id, as its identity column, for the row equal to `values`
    (NULL equal to NULL).

    A NULL-safe comparison of whole rows cannot use an index (a jsonb_build_array() equality read
    and hashed every row of the list, 2 s a pulse at 300,000 rows, CALC-SCALE-1), so it is one
    branch per pattern of NULL values, as _matched() splits rows: each runs only for a source row
    of its pattern (a gate on the row's values alone, checked before the list is read) and
    compares the known fields with = and the others with IS NULL, conditions the list's unique
    key answers with one index entry.
    """
    identity, fields = LISTS[table]
    nullable = [i for i, field in enumerate(fields) if field not in NOT_NULL.get(table, ())]
    branches = []
    for nulls in product((False, True), repeat=len(nullable)):
        null = dict(zip(nullable, nulls, strict=True))
        gate = [f"{values[i]} IS {'' if is_null else 'NOT '}NULL" for i, is_null in null.items()]
        match = [
            f"d.{field} IS NULL" if null.get(i) else f"d.{field}={values[i]}"
            for i, field in enumerate(fields)
        ]
        where = " AND ".join([*gate, *match])
        branches.append(f"SELECT d.{identity} FROM {table} d WHERE {where}")
    return f"JOIN LATERAL ({' UNION ALL '.join(branches)}) {identity} ON true"


def resolve(conn, source, value):
    """Fill every list from the enrichment fields of `source`; return the joins naming its ids.

    `value(field)` is the SQL expression of one enrichment field (stats.LOCATION_FIELDS and
    NETWORK_FIELDS) over `source`. The returned joins give each source row location_id.location_id
    and network_id.network_id. Each distinct location and network of the source is looked up
    once (lookup()), and the source rows find theirs on the NULL-safe tuple of the same fields, so
    the work follows the source's distinct labels, never the lists' size. Memoize is off for the
    rest of the transaction: over lookups of distinct labels it never hits, and its evictions cost
    0.7 ms a row at production's sizes (CALC-SCALE-1).
    """
    conn.execute("SET LOCAL enable_memoize=off")
    place = [value(field) for field in PLACE]
    asn = [value("asn"), value("asn_organization")]
    fill(conn, "place_names", place, source)
    fill(conn, "asn_names", asn, source)
    k = [f"k.{field}" for field in PLACE]
    locations, located = _distinct(source, value, (*PLACE, "latitude", "longitude"))
    located += " " + lookup("place_names", k)
    location = ["place_id.place_id", "k.latitude", "k.longitude"]
    attributes = ("isp", "organization", "connection_type", "usage_type")
    networks, networked = _distinct(source, value, ("asn", "asn_organization", *attributes))
    networked += " " + lookup("asn_names", ["k.asn", "k.asn_organization"])
    network = ["asn_name_id.asn_name_id", *(f"k.{field}" for field in attributes)]
    fill(conn, "enrichment_locations", location, located)
    fill(conn, "enrichment_networks", network, networked)
    return " ".join(
        (
            _label_ids("enrichment_locations", located, location, locations),
            _label_ids("enrichment_networks", networked, network, networks),
        )
    )


def _distinct(source, value, fields):
    """The fields of a label (location or network) and the distinct ones of `source`, as `k`."""
    columns = ",".join(f"{value(field)} AS {field}" for field in fields)
    return {field: value(field) for field in fields}, f"(SELECT DISTINCT {columns} FROM {source}) k"


def _label_ids(table, distinct, values, fields):
    """A join giving each source row the id in `table` of its label: the distinct labels with
    their ids (looked up once each), joined on the NULL-safe tuple of the label's fields."""
    identity = LISTS[table][0]
    found = f"(SELECT k.*,{identity}.{identity} FROM {distinct} {lookup(table, values)})"
    stored = ",".join(f"{identity}.{field}" for field in fields)
    return (
        f"JOIN {found} {identity} ON jsonb_build_array({stored})="
        f"jsonb_build_array({','.join(fields.values())})"
    )


def labels(conn, places, networks):
    """Shared ids of calculated breakdown labels, filled like the enrichment lists.

    `places` holds PLACE tuples and `networks` (asn, name, usage_type) tuples; returns
    {place: place_names.place_id} and {network: asn_labels.asn_label_id}.
    """
    by_place = _ids(conn, "place_names", places)
    asns = _ids(conn, "asn_names", {n[:2] for n in networks})
    found = _ids(conn, "asn_labels", {(asns[n[:2]], n[2]) for n in networks})
    return by_place, {n: found[asns[n[:2]], n[2]] for n in networks}


def _ids(conn, table, wanted):
    """Add the rows of `wanted` (tuples of `table`'s fields) to the list; map each to its id."""
    if not wanted:
        return {}
    fields, types = LISTS[table][1], TYPES[table]
    rows = sorted(wanted, key=str)
    params = [list(column) for column in zip(*rows, strict=True)]
    unnest = ",".join(f"%s::{kind}[]" for kind in types)
    source = f"unnest({unnest}) s({','.join(fields)})"
    fill(conn, table, [f"s.{field}" for field in fields], source, params)
    return _matched(conn, table, rows)


def _matched(conn, table, rows):
    """Each row's id in the list `table`, read through the list's unique key in one statement.

    A NULL-safe comparison of whole rows cannot use an index, so the rows are split by which of
    their fields are NULL: each such pattern is one branch that compares its known fields with =
    and the others with IS NULL, conditions the unique index answers.
    """
    identity, fields = LISTS[table]
    patterns = defaultdict(list)
    for row in rows:
        patterns[tuple(value is None for value in row)].append(row)
    branches, params = [], []
    for n, (nulls, members) in enumerate(patterns.items()):
        known = [i for i, null in enumerate(nulls) if not null]
        on = " AND ".join(
            f"d.{field} IS NULL" if null else f"d.{field}=s.{field}"
            for field, null in zip(fields, nulls, strict=True)
        )
        unnest = ",".join(f"%s::{TYPES[table][i]}[]" for i in known)
        named = ",".join([*(fields[i] for i in known), "n"])
        source = f"unnest({unnest}) WITH ORDINALITY s({named})" if known else "(SELECT 1) s(n)"
        select = f"SELECT {n},s.n::integer,d.{identity} FROM {source}"
        branches.append(f"{select} JOIN {table} d ON {on}")
        params += [[row[i] for row in members] for i in known]
    found = conn.cursor(row_factory=tuple_row).execute(" UNION ALL ".join(branches), params)
    grouped = list(patterns.values())
    return {grouped[branch][n - 1]: found_id for branch, n, found_id in found}
