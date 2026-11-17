"""The census-pulse windows' geo breakdown rows summed in the database (CALC-SCALE-3 step 1, #1307).

A census-pulse group's window lists were made in Python from every geo row of the window, every
hour (they are counted again each hour): about 1 M rows for 30 days at the synthetic scale, most
of the hourly build's growth. A breakdown row's counters and light-load bins are sums of its geo
rows', so the database sums them per breakdown key (QUERY, over rollups.ProductGeo's union of
hours, days and raw edges, the census overlaps already subtracted) and aggregate.breakdowns()
merges the sums (summed=, entries()) with the window's IP combinations by its own rules: every
key and label is memberships()', a country (or country x ASN) row takes the smallest country name
of the rows that place it (COLLATE "C": Python's order), and a country that no row places takes
the name of the country its requests fell back to, as when breakdowns() reads the rows. The full
calculation (the oracle) still reads every row in Python, which the tests compare with.
"""

from __future__ import annotations

from ..names import NETWORK, PLACE

COUNTERS = ("completed", "successes", "challenges", "blocks")  # scheduled.COUNTERS
_KEYS = ("cc", "country", "region_code", "region", "city", "asn", "org", "usage")
_COLUMNS = (*_KEYS, "name", "placed")
_TEXT, _ASN = "NULL::text", "NULL::bigint"
# Per breakdown: its key columns (and name, placed) in _COLUMNS' order, and its rows' condition.
_MEMBERS = (
    ("country", ("cc", *[_TEXT] * 4, _ASN, _TEXT, _TEXT, "country", "true"), "cc IS NOT NULL"),
    (
        "country",
        ("fallback", *[_TEXT] * 4, _ASN, _TEXT, _TEXT, _TEXT, "false"),
        "cc IS NULL AND fallback IS NOT NULL",
    ),
    (
        "region",
        ("cc", "country", "region_code", "region", _TEXT, _ASN, _TEXT, _TEXT, _TEXT, "true"),
        "cc IS NOT NULL AND (region_code IS NOT NULL OR region IS NOT NULL)",
    ),
    ("city", (*_KEYS[:5], _ASN, _TEXT, _TEXT, _TEXT, "true"), "cc IS NOT NULL"),
    ("asn", (*[_TEXT] * 5, "asn", "org", "usage", _TEXT, "true"), "asn IS NOT NULL"),
    (
        "country_asn",
        ("cc", *[_TEXT] * 4, "asn", "org", "usage", "country", "true"),
        "asn IS NOT NULL AND cc IS NOT NULL",
    ),
    ("city_asn", (*_KEYS, _TEXT, "true"), "cc IS NOT NULL"),
)
_SUMS = ",".join(COUNTERS)
_MEMBERSHIPS = " UNION ALL ".join(
    f"SELECT '{dim}' AS dim,"
    + ",".join(f"{e} AS {c}" for e, c in zip(exprs, _COLUMNS, strict=True))
    + f",{_SUMS},ms,n FROM x WHERE {where}"
    for dim, exprs, where in _MEMBERS
)
_GROUP = "dim," + ",".join(_KEYS)
# {union} (filled by str.replace): rollups' geo union, its OUT keys then the counters, or a
# light-load ms and its count.
QUERY = (
    f"WITH u AS MATERIALIZED (SELECT u.location_id,u.network_id,u.fallback,{_SUMS},u.ms,u.n "
    "FROM ({union}) u WHERE u.scope_id IS NULL AND u.hours<=%(window)s), "
    "x AS (SELECT l.country_code AS cc,l.country,l.region_code,l.region,l.city,"
    "e.asn::bigint AS asn,e.asn_organization AS org,e.usage_type AS usage,"
    "CASE WHEN u.fallback ~ '^[A-Z]{2}$' THEN u.fallback END AS fallback,"
    f"{_SUMS},u.ms,u.n FROM u JOIN enrichment_location_fields l USING(location_id) "
    "JOIN enrichment_network_fields e USING(network_id)), "
    f"m AS ({_MEMBERSHIPS}) "
    f'SELECT {_GROUP},min(name COLLATE "C") FILTER (WHERE placed) AS name,'
    "coalesce(bool_or(placed),false) AS placed,"
    + ",".join(f"coalesce(sum({c}),0)::bigint" for c in COUNTERS)
    + ",coalesce(array_agg(ms ORDER BY ms) FILTER (WHERE ms IS NOT NULL),'{}'),"
    "coalesce(array_agg(n ORDER BY ms) FILTER (WHERE ms IS NOT NULL),'{}') FROM ("
    f"SELECT {_GROUP},name,placed,{_SUMS},NULL::bigint AS ms,NULL::bigint AS n FROM m "
    f"WHERE ms IS NULL UNION ALL SELECT {_GROUP},NULL,NULL,NULL,NULL,NULL,NULL,ms,"
    f"sum(n)::bigint FROM m WHERE ms IS NOT NULL GROUP BY {_GROUP},ms) z GROUP BY {_GROUP}"
)


def entries(rows, names):
    """aggregate.breakdowns()' summed entries of QUERY's rows: (breakdown, key, label, placed,
    counters, bins); names: every country's name (a fallback country's label)."""
    found = []
    for dim, cc, country, region_code, region, city, asn, org, usage, name, placed, *rest in rows:
        counters = dict(zip(COUNTERS, rest[:4], strict=True))
        bins = dict(zip(rest[4], rest[5], strict=True))
        if not placed:  # a country only requests that fell back to it reached
            found.append((dim, cc, {"country_code": cc, "country": names.get(cc)}, False))
        else:
            named = name if dim in ("country", "country_asn") else country
            place = None if cc is None else (cc, named, region_code, region, city)
            network = (asn, org, usage) if dim in ("asn", "country_asn", "city_asn") else None
            key, label = next((k, a) for n, k, a in memberships(place, network) if n == dim)
            found.append((dim, key, label, True))
        found[-1] += (counters, bins)
    return found


def memberships(place, network):
    """(breakdown, key, label) for every breakdown row a location and network fall in."""
    result = []
    located = place is not None and place[0] is not None
    if located:
        cc, country, region_code, region, city = place
        result.append(("country", cc, {"country_code": cc, "country": country}))
        if region_code is not None or region is not None:
            result.append(
                (
                    "region",
                    place[:4],
                    {
                        "country_code": cc,
                        "country": country,
                        "region": region,
                        "region_code": region_code,
                    },
                )
            )
        result.append(
            (
                "city",
                place,
                {
                    "country_code": cc,
                    "country": country,
                    "region": region,
                    "region_code": region_code,
                    "city": city,
                },
            )
        )
    if network is not None and network[0] is not None:
        asn = dict(zip(NETWORK, network, strict=True))
        result.append(("asn", network, asn))
        if located:
            result.append(
                ("country_asn", (place[0], network), {"country_code": cc, "country": country} | asn)
            )
    if located:
        # Coverage evidence needs the actual location/network intersection,
        # never a join of independent country or city totals. Unknown network
        # labels remain explicit nulls so no observed IPs disappear.
        label = dict(zip(PLACE, place, strict=True))
        identity = network if network is not None else (None, None, None)
        result.append(
            ("city_asn", (place, identity), label | dict(zip(NETWORK, identity, strict=True)))
        )
    return result
