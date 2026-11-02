"""The shared place and ASN name lists, and the one routine that fills them.

place_names and asn_names hold every place and ASN name once; enrichment_locations (a place at a
coordinate) and enrichment_networks (an ASN name with per-IP network attributes) refer to them,
and so does the calculated layer. Every list is filled the same way: the distinct rows of a source
query are added with ON CONFLICT DO NOTHING, and each source row then finds its id through a
NULL-safe, hashable tuple join. The all-NULL row of each list is its unknown member.
"""

from __future__ import annotations

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
    """Add each distinct row of `values` (SQL expressions over `source`) to the list `table`."""
    columns = ",".join(LISTS[table][1])
    conn.execute(
        f"INSERT INTO {table} ({columns}) SELECT DISTINCT {','.join(values)} FROM {source} "
        "ON CONFLICT DO NOTHING",
        params,
    )


def lookup(table, values):
    """A join that names `table`'s id, as its identity column, for the row equal to `values`."""
    identity, fields = LISTS[table]
    stored = ",".join(f"d.{field}" for field in fields)
    return (
        f"JOIN LATERAL (SELECT {identity} FROM {table} d WHERE jsonb_build_array({stored})="
        f"jsonb_build_array({','.join(values)})) {identity} ON true"
    )


def resolve(conn, source, value):
    """Fill every list from the enrichment fields of `source`; return the joins naming its ids.

    `value(field)` is the SQL expression of one enrichment field (stats.LOCATION_FIELDS and
    NETWORK_FIELDS) over `source`. The returned joins give each source row location_id.location_id
    and network_id.network_id.
    """
    place = [value(field) for field in PLACE]
    asn = [value("asn"), value("asn_organization")]
    fill(conn, "place_names", place, source)
    fill(conn, "asn_names", asn, source)
    places, asns = lookup("place_names", place), lookup("asn_names", asn)
    location = ["place_id.place_id", value("latitude"), value("longitude")]
    attributes = ("isp", "organization", "connection_type", "usage_type")
    network = ["asn_name_id.asn_name_id", *(value(field) for field in attributes)]
    fill(conn, "enrichment_locations", location, f"{source} {places}")
    fill(conn, "enrichment_networks", network, f"{source} {asns}")
    locations = lookup("enrichment_locations", location)
    return " ".join((places, asns, locations, lookup("enrichment_networks", network)))


def labels(conn, places, networks):
    """Shared ids of calculated breakdown labels, filled like the enrichment lists.

    `places` holds PLACE tuples and `networks` (asn, name, usage_type) tuples; returns
    {place: place_names.place_id} and {network: asn_labels.asn_label_id}.
    """
    return (
        _ids(conn, places, PLACE, ("text",) * 5, _place),
        _ids(conn, networks, NETWORK, ("bigint", "text", "text"), _network),
    )


def _place(conn, source, params):
    values = [f"s.{field}" for field in PLACE]
    fill(conn, "place_names", values, source, params)
    return values, lookup("place_names", values), "place_id"


def _network(conn, source, params):
    asn = ["s.asn", "s.name"]
    fill(conn, "asn_names", asn, source, params)
    asns = lookup("asn_names", asn)
    label = ["asn_name_id.asn_name_id", "s.usage_type"]
    fill(conn, "asn_labels", label, f"{source} {asns}", params)
    return [*asn, "s.usage_type"], f"{asns} {lookup('asn_labels', label)}", "asn_label_id"


def _ids(conn, wanted, fields, types, add):
    """Fill the lists of `wanted` (tuples of `fields`) with `add`; map each tuple to its id."""
    rows = sorted(wanted, key=str)
    params = [list(column) for column in zip(*rows, strict=True)] or [[] for _ in fields]
    unnest = ",".join(f"%s::{kind}[]" for kind in types)
    source = f"unnest({unnest}) s({','.join(fields)})"
    values, joins, identity = add(conn, source, params)
    query = f"SELECT {','.join(values)},{identity}.{identity} FROM {source} {joins}"
    found = conn.cursor(row_factory=tuple_row).execute(query, params)
    return {tuple(row[:-1]): row[-1] for row in found}
