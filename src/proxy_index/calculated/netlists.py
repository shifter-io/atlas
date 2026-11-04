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
text, in dimension and position order) and entries counts them by dimension name.

A network list is written for every build and is referenced by no window: collect() keeps it while
its created_build has calculated_windows rows, so discarding or expiring the build frees it.
"""

from __future__ import annotations

from collections import defaultdict

from psycopg.rows import tuple_row

from .aggregate import BREAKDOWNS
from .lists import LIST_ROW_COLUMNS

COHORT = "network"
DIMENSIONS = {name: code for name, code in BREAKDOWNS.items() if code != "x"}
SUMMED = ("ips", "observations", "completed", "successes", "challenges", "blocks")
LATENCY = ("p50", "p90", "p99")
# The member row a merged row takes its percentiles from.
TOP = "successes DESC NULLS LAST,list_id,position"
ORDER = "sum(completed) DESC NULLS LAST,sum(ips) DESC,place_id NULLS LAST,asn_label_id NULLS LAST"
# One statement per network window: the merged rows (m), the list row (l) and its rows.
MERGE = (
    "WITH m AS (SELECT dimension,(row_number() OVER (PARTITION BY dimension ORDER BY "
    f"{ORDER})-1)::integer AS position,place_id,asn_label_id,"
    + ",".join(f"sum({k})::bigint AS {k}" for k in SUMMED)
    + ","
    + ",".join(f"(array_agg({k} ORDER BY {TOP}))[1] AS {k}" for k in LATENCY)
    + " FROM calculated_list_rows WHERE list_id=ANY(%(members)s) AND dimension IN ("
    + ",".join(f"'{code}'" for code in DIMENSIONS.values())
    + ") GROUP BY dimension,place_id,asn_label_id),"
    " l AS (INSERT INTO calculated_lists(product_id,cohort,window_hours,signature,content_hash,"
    "entries,figures,created_build) SELECT %(product)s,%(cohort)s,%(hours)s,NULL,"
    "encode(sha256(convert_to(coalesce(string_agg(m::text,E'\\n' ORDER BY dimension,position),"
    "''),'UTF8')),'hex'),jsonb_build_object("
    + ",".join(f"'{k}',count(*) FILTER (WHERE dimension='{c}')" for k, c in DIMENSIONS.items())
    + "),'{}',%(build)s FROM m RETURNING list_id,entries),"
    f" r AS (INSERT INTO calculated_list_rows({LIST_ROW_COLUMNS}) SELECT l.list_id,m.* FROM m,l)"
    " SELECT list_id,entries FROM l"
)
# Lists collect() deletes: group lists no window references, network lists whose build has none.
ORPHANS = (
    "SELECT list_id FROM calculated_lists l WHERE NOT EXISTS "
    "(SELECT FROM calculated_windows w WHERE w.list_id=l.list_id) AND NOT (l.cohort='network' "
    "AND l.created_build IN (SELECT build_id FROM calculated_windows))"
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


def write_network_lists(conn, build_id, payload, group_lists):
    """Write one network list per network window of payload's product groups.

    group_lists maps (group id, window label) to the group's list_id (what lists.persist()
    returned for each group). Returns {(network_id, label): (list_id, entries)}.
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
            if members:
                params = {"members": members, "product": product, "hours": hours[label]}
                params |= {"cohort": COHORT, "build": int(build_id)}
                written[network, label] = cur.execute(MERGE, params).fetchone()
    return written


def collect(conn):
    """Delete the lists retention no longer keeps, their rows first; return how many lists."""
    conn.execute(f"DELETE FROM calculated_list_rows WHERE list_id IN ({ORPHANS})")
    return conn.execute(f"DELETE FROM calculated_lists WHERE list_id IN ({ORPHANS})").rowcount
