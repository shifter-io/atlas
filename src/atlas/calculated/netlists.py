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

Each row with IPs of a build's network lists is ranked among the build's network lists of the
same kind and window at the same place, as the API keys a place (ips_rank.sql; calculated v15,
COVERAGE-IPS-RANK-1): ips_rank is 1 + the number of those networks with strictly more IPs there
(SQL rank(): ties share the better rank, as the API's rankOf), summed over the network's rows of
the place, and ips_peers the number of networks with IPs there, this one included. A row without
IPs or a place key and a group list's row keep NULL. Neither is part of content_hash, which hashes
the merged rows before they are ranked. A build merges all its network windows at once into a
temporary table (MERGE_ALL), hashes and counts them there (HEADS), and writes each row once,
ranked (MERGED_RANKED; CALC-SCALE-1: writing them, then rank()'s UPDATE of every row, took 345 s
of a production build at 8 M rows). restore() writes a list at a time and ranks after (rank()).
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from . import parts
from .aggregate import BREAKDOWNS
from .list_write import INSERT, LIST_ROW_COLUMNS

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
PLACES = (
    "jsonb_build_object("
    + ",".join(
        f"'{name}',count(DISTINCT {key}) FILTER (WHERE m.dimension='{code}' AND m.ips>0"
        + ("" if code == "a" else " AND p.country_code<>''")
        + f"{condition})"
        for name, (code, key, condition) in COUNTED.items()
    )
    + ")"
)
NAMED = (
    "LEFT JOIN place_names p USING(place_id) LEFT JOIN asn_labels USING(asn_label_id) "
    "LEFT JOIN asn_names n USING(asn_name_id)"
)
PLACE_COUNTS = f" d AS (SELECT {PLACES} AS places FROM m {NAMED})"
# A list's rows by dimension name.
ENTRIES = (
    "jsonb_build_object("
    + ",".join(f"'{k}',count(*) FILTER (WHERE dimension='{c}')" for k, c in DIMENSIONS.items())
    + ")"
)
CODES = ",".join(f"'{code}'" for code in DIMENSIONS.values())
# A network list written again (restore()), one statement per window under the id its build's
# head names (%(list)s): the merged rows (m), the list row (l), its rows and its place counts (d).
RESTORE = (
    "WITH m AS (SELECT dimension,(row_number() OVER (PARTITION BY dimension ORDER BY "
    f"{ORDER})-1)::integer AS position,place_id,asn_label_id,"
    + ",".join(f"sum({k})::bigint AS {k}" for k in SUMMED)
    + ","
    + ",".join(f"(array_agg({k} ORDER BY {TOP}))[1] AS {k}" for k in LATENCY)
    + f" FROM calculated_list_rows WHERE list_id=ANY(%(members)s) AND dimension IN ({CODES})"
    + " GROUP BY dimension,place_id,asn_label_id),"
    " l AS (INSERT INTO calculated_lists(list_id,product_id,cohort,window_hours,signature,"
    "content_hash,entries,figures,created_build) OVERRIDING SYSTEM VALUE SELECT %(list)s,"
    "%(product)s,%(cohort)s,%(hours)s,NULL,"
    "encode(sha256(convert_to(coalesce(string_agg(m::text,E'\\n' ORDER BY dimension,position),"
    f"''),'UTF8')),'hex'),{ENTRIES},'{{}}',%(build)s FROM m RETURNING list_id,entries),"
    f" r AS (INSERT INTO calculated_list_rows({LIST_ROW_COLUMNS}) SELECT l.list_id,m.* FROM m,l),"
    + PLACE_COUNTS
    + " SELECT list_id,entries,places FROM l,d"
)
# The ranks (r) of the rows with IPs of the given network lists (ips_rank.sql's {rows}).
RANKS = (Path(__file__).parent / "ips_rank.sql").read_text()
# rank(): the stored rows of the lists %(lists)s of kinds %(kinds)s, ranked in place.
RANK = RANKS.format(
    rows="calculated_list_rows r JOIN (SELECT u.list_id,u.kind,l.window_hours "
    "FROM unnest(%(lists)s::bigint[],%(kinds)s::text[]) u(list_id,kind) "
    "JOIN calculated_lists l USING(list_id)) u USING(list_id)"
) + (
    " UPDATE calculated_list_rows t SET ips_rank=r.ips_rank,ips_peers=r.ips_peers FROM r "
    "WHERE t.list_id=r.list_id AND t.dimension=r.dimension AND t.position=r.position"
)
# A build's merged network rows (_write_merged()): RESTORE's m of each window w (its place in the
# written order), with m's column types.
MERGED = (
    "DROP TABLE IF EXISTS pg_temp.network_rows;CREATE TEMP TABLE network_rows(w integer,"
    'dimension "char",position integer,place_id integer,asn_label_id integer,'
    + ",".join(f"{k} bigint" for k in SUMMED)
    + ","
    + ",".join(f"{k} integer" for k in LATENCY)
    + ")"
)
MERGE_ALL = (
    "INSERT INTO network_rows SELECT k.w,dimension,(row_number() OVER (PARTITION BY k.w,dimension "
    f"ORDER BY {ORDER})-1)::integer,place_id,asn_label_id,"
    + ",".join(f"sum({k})::bigint" for k in SUMMED)
    + ","
    + ",".join(f"(array_agg({k} ORDER BY {TOP}))[1]" for k in LATENCY)
    + " FROM unnest(%s::integer[],%s::bigint[]) k(w,list_id) JOIN calculated_list_rows "
    f"USING(list_id) WHERE dimension IN ({CODES}) GROUP BY k.w,dimension,place_id,asn_label_id"
)
# Each window's content_hash, entries and place counts, as RESTORE's l and d (an empty window too).
COLUMNS = ("dimension", "position", "place_id", "asn_label_id", *SUMMED, *LATENCY)
HEADS = (
    "SELECT x.w,encode(sha256(convert_to(coalesce(string_agg(ROW("
    + ",".join(f"m.{c}" for c in COLUMNS)
    + ")::text,E'\\n' ORDER BY m.dimension,m.position) FILTER (WHERE m.w IS NOT NULL),''),"
    f"'UTF8')),'hex'),{ENTRIES},{PLACES} FROM unnest(%s::integer[]) x(w) "
    f"LEFT JOIN network_rows m USING(w) {NAMED} GROUP BY x.w ORDER BY x.w"
)
# Every merged row written once with its rank, the windows %(windows)s being the lists %(ids)s.
MERGED_RANKED = RANKS.format(
    rows="(SELECT w AS list_id,dimension,position,place_id,asn_label_id,ips FROM network_rows) r "
    "JOIN unnest(%(windows)s::integer[],%(kinds)s::text[],%(hours)s::integer[]) "
    "u(list_id,kind,window_hours) USING(list_id)"
) + (
    f" INSERT INTO calculated_list_rows({LIST_ROW_COLUMNS},ips_rank,ips_peers) SELECT i.list_id,"
    + ",".join(f"m.{c}" for c in COLUMNS)
    + ",r.ips_rank,r.ips_peers FROM network_rows m "
    "JOIN unnest(%(windows)s::integer[],%(ids)s::bigint[]) i(w,list_id) USING(w) "
    "LEFT JOIN r ON r.list_id=m.w AND r.dimension=m.dimension AND r.position=m.position"
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
    again (restore()): only those windows are written, and rank() ranks them after. Returns
    {(network_id, label): (list_id, entries, places)}: places are the window's distinct place
    counts (COUNTED).
    """
    networks = defaultdict(list)
    for group in payload["products"]:
        networks[network_id(group)].append(group)
    windows = []  # ((network, label), headline product, hours, member lists), in id order
    for network, groups in sorted(networks.items()):
        product = headline(groups)
        hours = {label: w["period"]["hours"] for g in groups for label, w in g["windows"].items()}
        for label in sorted(hours, key=hours.get):
            members = sorted(
                {group_lists[g["id"], label] for g in groups if (g["id"], label) in group_lists}
            )
            if members and (ids is None or (network, label) in ids):
                windows.append(((network, label), product, hours[label], members))
    if ids is None:
        return _write_merged(conn, int(build_id), windows)
    cur, written = conn.cursor(row_factory=tuple_row), {}
    for key, product, hours, members in windows:
        params = {"members": members, "product": product, "hours": hours, "list": ids[key]}
        params |= {"cohort": COHORT, "build": int(build_id)}
        written[key] = cur.execute(RESTORE, params).fetchone()
    return written


def _write_merged(conn, build_id, windows):
    """Write a new build's network windows (write_network_lists()) together: their merged rows
    into a temporary table (MERGE_ALL), their lists from its hashes and counts (HEADS) with ids
    in the windows' order, then every row once with its rank (MERGED_RANKED)."""
    if not windows:
        return {}
    cur = conn.cursor(row_factory=tuple_row)
    cur.execute(MERGED)
    pairs = [(w, list_id) for w, (*_, members) in enumerate(windows) for list_id in members]
    cur.execute(MERGE_ALL, [list(column) for column in zip(*pairs, strict=True)])
    cur.execute("ANALYZE network_rows")
    order = list(range(len(windows)))
    heads = cur.execute(HEADS, (order,)).fetchall()
    n, hours = len(windows), [w[2] for w in windows]
    columns = [[w[1] for w in windows], [COHORT] * n, hours, [None] * n, [h[1] for h in heads]]
    columns += [[Jsonb(h[2]) for h in heads], [Jsonb({})] * n]
    ids = sorted(row[0] for row in cur.execute(INSERT, (build_id, *columns)))
    kinds = [network.rsplit(":", 2)[1] for (network, _), *_ in windows]
    cur.execute(MERGED_RANKED, {"windows": order, "kinds": kinds, "hours": hours, "ids": ids})
    cur.execute("DROP TABLE network_rows")
    return {
        key: (list_id, entries, places)
        for (key, *_), list_id, (_, _, entries, places) in zip(windows, ids, heads, strict=True)
    }


def rank(conn, lists):
    """Rank the rows of one build's network lists, {(network id, label): list_id}, among the
    lists of the same kind and window (RANK)."""
    keys = sorted(lists)
    kinds = [network.rsplit(":", 2)[1] for network, _ in keys]
    conn.execute(RANK, {"lists": [lists[key] for key in keys], "kinds": kinds})


def restore(conn):
    """Derive again the network lists the current build's head names and the database lacks,
    from the build's group lists (its stored parts name them), under the same list ids, and rank
    the build's network lists again; return how many. The merge is deterministic, so each list is
    the one its build first wrote."""
    cur = conn.cursor(row_factory=tuple_row)
    found = cur.execute("SELECT build_id FROM current_calculation").fetchone()
    head = found and parts.head(conn, found[0])
    if not head or "parts" not in head:
        return 0
    named = {
        (entry["id"], label): window["lists"]["id"]
        for entry in head.get("networks", ())
        for label, window in entry["windows"].items()
        if window.get("lists")
    }
    held = cur.execute(
        "SELECT list_id FROM calculated_lists WHERE list_id=ANY(%s)", (list(named.values()),)
    )
    held = {row[0] for row in held}
    ids = {key: list_id for key, list_id in named.items() if list_id not in held}
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
    rank(conn, named)  # its lists ranked again with the restored ones
    return len(restored)


def collect(conn):
    """Delete the lists retention no longer keeps, their rows first; return how many lists."""
    conn.execute(f"DELETE FROM calculated_list_rows WHERE list_id IN ({ORPHANS})")
    return conn.execute(f"DELETE FROM calculated_lists WHERE list_id IN ({ORPHANS})").rowcount
