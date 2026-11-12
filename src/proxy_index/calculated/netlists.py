"""Network lists (contract K10): one place list per network window, merged in the database.

A network is one (provider, kind) pair; its id is "<provider>:<kind>:network". After store.write()
has persisted every product group, write_network_lists() writes one list per network window: one
calculated_lists row (cohort 'network', signature NULL, figures {}) and its calculated_list_rows,
merged from the rows of the network's group lists of that window by one INSERT … SELECT, so no
row passes through Python. The members are the network's groups with a list for that window
(daily country groups and the census-pulse group alike). The list's product_id is the headline
product: the network's product with the most 30d completed requests, on a tie the lowest id.

The rows are the member rows of dimensions c, r, g, a and y (not x) grouped by (dimension,
place_id, asn_label_id). ips, observations, completed, successes, challenges and blocks are
summed. A sum counts an IP that two groups of the network saw twice, so a merged row's ips (and a
group row's, as today) is an upper bound of its distinct IPs. p50/p90/p99 come from the member row
with the most successes; ties go to the lowest list id, then position. Positions follow completed
desc, ips desc, place_id, asn_label_id, NULLs last. content_hash is the sha256 of the rows (their
text, in dimension and position order) and entries counts them by dimension name. The same
statement counts the network's places from the merged rows (COUNTED): each dimension's distinct keys
of the rows with IPs, as poolfig.pool_figures() counts a group's, never its rows, which are labels
(an ASN once per usage type and name, a country once per name) and places of requests alone.

A network list is written for every build and is referenced by no window: collect() keeps it only
while its created_build is the current build or a draft whose payload prune() kept (KEPT), so each
publication frees the network lists of every other build, retained or not. The current build's
head names its lists, so when a publication makes current a build without them (an older retained
build), restore() derives them again from its group lists, under the same list ids.

Once a build's network lists are written, rank() ranks each of their rows with IPs among the rows
of the same dimension, place and ASN label in the build's network lists of the same kind and
window (calculated v15, COVERAGE-IPS-RANK-1): ips_rank is 1 + the number of those networks with
strictly more IPs (SQL rank(): ties share the better rank, as the API's rankOf), ips_peers the
number of networks with IPs there, this one included. A row without IPs and a group list's row
keep NULL. Neither is part of content_hash, which hashes the merged rows before they are ranked.
"""

from __future__ import annotations

from collections import defaultdict

from psycopg.rows import tuple_row

from . import parts
from .aggregate import BREAKDOWNS
from .list_write import LIST_ROW_COLUMNS

COHORT = "network"
DIMENSIONS = {name: code for name, code in BREAKDOWNS.items() if code != "x"}
SUMMED = ("ips", "observations", "completed", "successes", "challenges", "blocks")
LATENCY = ("p50", "p90", "p99")
# The member row a merged row takes its percentiles from.
TOP = "successes DESC NULLS LAST,list_id,position"
ORDER = "sum(completed) DESC NULLS LAST,sum(ips) DESC,place_id NULLS LAST,asn_label_id NULLS LAST"
# A network window's place counts (network.PLACES): (dimension, key, condition) of each, the
# distinct keys poolfig.pool_figures() counts with Python's truthiness: a country code, a region
# code (else name) in its country, a city in its country and region, an ASN number. Only rows
# with IPs count: a row of requests alone (a census location, a requested country's requests
# without an exit address) names no place of the pool.
REGION = "CASE WHEN p.region_code<>'' THEN p.region_code ELSE p.region END"
COUNTED = {
    "countries": ("c", "p.country_code", ""),
    "regions": ("r", f"jsonb_build_array(p.country_code,{REGION})", f" AND {REGION}<>''"),
    "cities": ("g", f"jsonb_build_array(p.country_code,{REGION},p.city)", " AND p.city<>''"),
    "asns": ("a", "n.asn", ""),
}
PLACE_COUNTS = (
    " d AS (SELECT jsonb_build_object("
    + ",".join(
        f"'{name}',count(DISTINCT {key}) FILTER (WHERE m.dimension='{code}' AND m.ips>0"
        + ("" if code == "a" else " AND p.country_code<>''")
        + f"{condition})"
        for name, (code, key, condition) in COUNTED.items()
    )
    + ") AS places FROM m LEFT JOIN place_names p USING(place_id) "
    "LEFT JOIN asn_labels USING(asn_label_id) LEFT JOIN asn_names n USING(asn_name_id))"
)
# One statement per network window: the merged rows (m), the list row (l), its rows and its place
# counts (d).
SHAPE = (
    "WITH m AS (SELECT dimension,(row_number() OVER (PARTITION BY dimension ORDER BY "
    f"{ORDER})-1)::integer AS position,place_id,asn_label_id,"
    + ",".join(f"sum({k})::bigint AS {k}" for k in SUMMED)
    + ","
    + ",".join(f"(array_agg({k} ORDER BY {TOP}))[1] AS {k}" for k in LATENCY)
    + " FROM calculated_list_rows WHERE list_id=ANY(%(members)s) AND dimension IN ("
    + ",".join(f"'{code}'" for code in DIMENSIONS.values())
    + ") GROUP BY dimension,place_id,asn_label_id),"
    " l AS (INSERT INTO calculated_lists(<id>product_id,cohort,window_hours,signature,content_hash,"
    "entries,figures,created_build) <select>%(product)s,%(cohort)s,%(hours)s,NULL,"
    "encode(sha256(convert_to(coalesce(string_agg(m::text,E'\\n' ORDER BY dimension,position),"
    "''),'UTF8')),'hex'),jsonb_build_object("
    + ",".join(f"'{k}',count(*) FILTER (WHERE dimension='{c}')" for k, c in DIMENSIONS.items())
    + "),'{}',%(build)s FROM m RETURNING list_id,entries),"
    f" r AS (INSERT INTO calculated_list_rows({LIST_ROW_COLUMNS}) SELECT l.list_id,m.* FROM m,l),"
    + PLACE_COUNTS
    + " SELECT list_id,entries,places FROM l,d"
)
# A new list takes the next id; a restored one the id its build's head names (%(list)s).
MERGE = SHAPE.replace("<id>", "").replace("<select>", "SELECT ")
RESTORE = SHAPE.replace("<id>", "list_id,").replace(
    "<select>", "OVERRIDING SYSTEM VALUE SELECT %(list)s,"
)
# The ranks of the rows with IPs of the network lists %(lists)s of kinds %(kinds)s (rank()).
PLACE = "PARTITION BY u.kind,l.window_hours,r.dimension,r.place_id,r.asn_label_id"
RANK = (
    "UPDATE calculated_list_rows t SET ips_rank=k.ips_rank,ips_peers=k.ips_peers FROM (SELECT "
    f"r.list_id,r.dimension,r.position,rank() OVER ({PLACE} ORDER BY r.ips DESC) AS ips_rank,"
    f"count(*) OVER ({PLACE}) AS ips_peers FROM calculated_list_rows r "
    "JOIN unnest(%(lists)s::bigint[],%(kinds)s::text[]) u(list_id,kind) USING(list_id) "
    "JOIN calculated_lists l USING(list_id) WHERE r.ips>0) k "
    "WHERE (t.list_id,t.dimension,t.position)=(k.list_id,k.dimension,k.position)"
)
# The builds that keep their network lists: the current build and the drafts with a payload.
KEPT = (
    "SELECT build_id FROM current_calculation UNION SELECT build_id FROM calculated_snapshots "
    "JOIN calculation_builds USING(build_id) WHERE NOT published"
)
# Lists collect() deletes: group lists no window references, network lists of no KEPT build.
ORPHANS = (
    "SELECT list_id FROM calculated_lists l WHERE NOT EXISTS "
    "(SELECT FROM calculated_windows w WHERE w.list_id=l.list_id) AND NOT (l.cohort='network' "
    f"AND l.created_build IN ({KEPT}))"
)


def network_id(group):
    return f"{group['provider']}:{group['kind']}:{COHORT}"


def headline(groups):
    """The product of groups with the most 30d completed requests; on a tie, the lowest id."""
    completed = defaultdict(int)
    for g in groups:
        window = g["windows"].get("30d")
        completed[g["product_id"]] += window["metrics"]["completed"] if window else 0
    return min(completed, key=lambda product: (-completed[product], product))


def write_network_lists(conn, build_id, payload, group_lists, ids=None):
    """Write one network list per network window of payload's product groups.

    group_lists maps (group id, window label) to the group's list_id (what lists.persist()
    returned for each group). ids, when given, maps (network_id, label) to the list_id to write
    again (restore()): only those windows are written. Returns {(network_id, label): (list_id,
    entries, places)}: places are the window's distinct place counts (COUNTED).
    """
    networks = defaultdict(list)
    for group in payload["products"]:
        networks[network_id(group)].append(group)
    cur = conn.cursor(row_factory=tuple_row)
    written = {}
    for network, groups in sorted(networks.items()):
        product = headline(groups)
        hours = {label: w["period"]["hours"] for g in groups for label, w in g["windows"].items()}
        for label in sorted(hours, key=hours.get):
            members = sorted(
                {group_lists[g["id"], label] for g in groups if (g["id"], label) in group_lists}
            )
            if members and (ids is None or (network, label) in ids):
                params = {"members": members, "product": product, "hours": hours[label]}
                params |= {"cohort": COHORT, "build": int(build_id)}
                params["list"] = None if ids is None else ids[network, label]
                query = MERGE if ids is None else RESTORE
                written[network, label] = cur.execute(query, params).fetchone()
    if ids is None:
        rank(conn, {key: found[0] for key, found in written.items()})
    return written


def rank(conn, lists):
    """Rank the rows of one build's network lists, {(network id, label): list_id}, among the
    lists of the same kind and window (RANK)."""
    keys = sorted(lists)
    kinds = [network.rsplit(":", 2)[1] for network, _ in keys]
    conn.execute(RANK, {"lists": [lists[key] for key in keys], "kinds": kinds})


def restore(conn):
    """Derive again the network lists the current build's head names and the database lacks,
    from the build's group lists (its stored parts name them), under the same list ids; return
    how many. The merge is deterministic, so each list is the one its build first wrote."""
    cur = conn.cursor(row_factory=tuple_row)
    found = cur.execute("SELECT build_id FROM current_calculation").fetchone()
    head = found and parts.head(conn, found[0])
    if not head or "parts" not in head:
        return 0
    ids = {
        (entry["id"], label): window["lists"]["id"]
        for entry in head.get("networks", ())
        for label, window in entry["windows"].items()
        if window.get("lists")
    }
    every = ids
    held = cur.execute(
        "SELECT list_id FROM calculated_lists WHERE list_id=ANY(%s)", (list(ids.values()),)
    )
    held = {row[0] for row in held}
    ids = {key: list_id for key, list_id in ids.items() if list_id not in held}
    if not ids:
        return 0
    groups = list(parts.stream(conn, found[0], "products"))
    members = {
        (g["id"], label): w["lists"]["id"]
        for g in groups
        for label, w in g["windows"].items()
        if w.get("lists")
    }
    restored = write_network_lists(conn, found[0], {"products": groups}, members, ids)
    rank(conn, every)
    return len(restored)


def collect(conn):
    """Delete the lists retention no longer keeps, their rows first; return how many lists."""
    conn.execute(f"DELETE FROM calculated_list_rows WHERE list_id IN ({ORPHANS})")
    return conn.execute(f"DELETE FROM calculated_lists WHERE list_id IN ({ORPHANS})").rowcount
