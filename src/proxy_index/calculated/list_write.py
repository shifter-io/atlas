"""Writing a product group's lists (contract K2): what a list holds and the three statements that
store a group's new lists.

A target-free window's list is its full breakdowns (prepare()): its key (product, cohort, window
hours and signature), content_hash, entry counts, figures and rows. write_lists() takes the lists
of one product group's windows. A signed list whose stored twin exists is reused after its
content_hash is verified (STORED, one read per signed list). The others are written together:
one INSERT … RETURNING for their calculated_lists rows, one names.labels() call for the labels of
all their rows and one COPY for the rows, never a round trip per window or per row.

calculated_list_rows.list_id has no foreign key (calculated v13): a per-row check on
calculated_lists cost 1.76 ms a row in SCALE-RUN-2. A list's rows are written in the transaction
that inserts the list, and store.collect() deletes the rows of a list before the list.
"""

from __future__ import annotations

from collections import namedtuple

from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from ..names import labels
from .aggregate import BREAKDOWNS, NETWORK, PLACE
from .policy import digest
from .slim import POOL_LISTS

# Label fields of each breakdown: its place columns and whether it names an ASN.
FIELDS = {
    "country": (PLACE[:2], False),
    "region": (PLACE[:4], False),
    "city": (PLACE, False),
    "asn": ((), True),
    "country_asn": (PLACE[:2], True),
    "city_asn": (PLACE, True),
}
COUNTERS = ("ips", "observations", "completed", "successes", "success_rate", "challenges", "blocks")
LATENCY = ("p50", "p90", "p99")
# A list's figures: the window's pool figures that do not depend on its start (new_ips and
# churn_rate do; the daily averages depend on the cutoff), plus its pool lists' entry counts.
# list_write.py is part of IMPLEMENTATION_ID, so a change to FIGURES or content_hash changes
# every signature. A list the incremental calculation counted in SQL also stores "first_seen",
# its IPs per 5-minute bin of their product's first sighting from the window's start on
# (CALC-INC-2's new IPs), outside content_hash: a late run moves product_ips.first_seen, which no
# signature names (UNHASHED).
FIGURES = (
    "unique_ips",
    "observations",
    "recurring_share",
    "countries",
    "regions",
    "cities",
    "asns",
    "datacenter_asn_share",
    "classification_coverage",
    "geo_accuracy",
    "enrichment_coverage",
)
UNHASHED = ("first_seen",)
LIST_ROW_COLUMNS = (
    "list_id,dimension,position,place_id,asn_label_id,ips,observations,completed,successes,"
    "challenges,blocks,p50,p90,p99"
)
STORED = (
    "SELECT list_id,content_hash FROM calculated_lists "
    "WHERE product_id=%s AND cohort=%s AND window_hours=%s AND signature=%s"
)
# A group's new lists, in their order (so their ids ascend in it).
INSERT = (
    "INSERT INTO calculated_lists(product_id,cohort,window_hours,signature,content_hash,entries,"
    "figures,created_build) SELECT product_id,cohort,window_hours,signature,content_hash,entries,"
    "figures,%s FROM unnest(%s::integer[],%s::text[],%s::integer[],%s::text[],%s::text[],"
    "%s::jsonb[],%s::jsonb[]) WITH ORDINALITY AS l(product_id,cohort,window_hours,signature,"
    "content_hash,entries,figures,n) ORDER BY n RETURNING list_id,product_id,cohort,window_hours"
)
# A list ready to write: key (product_id, cohort, window_hours, signature), content_hash, the
# entries and figures to store, and its rows (dimension, position, place, network, breakdown row).
Prepared = namedtuple("Prepared", "key content_hash entries figures rows")


def figures(pool):
    """The list figures of a window's pool (see FIGURES)."""
    return {k: pool[k] for k in FIGURES if k in pool} | {
        "list_entries": {name: len(pool[name]) for name in POOL_LISTS if name in pool}
    }


def prepare(product, window, signature=None):
    """The list of a window of product (its product_id and cohort). content_hash is the digest of
    the full breakdowns and the figures; window["first_seen"], when present, is stored with the
    figures (see FIGURES)."""
    breakdowns, numbers = window["breakdowns"], figures(window["pool"])
    key = (product["product_id"], product["cohort"], window["period"]["hours"], signature)
    rows = []
    for name, code in BREAKDOWNS.items():
        places, named = FIELDS[name]
        for position, row in enumerate(breakdowns[name]):
            label = set(row) - set(COUNTERS) - set(LATENCY)
            if label != set(places) | (set(NETWORK) if named else set()):
                raise ValueError("Unexpected breakdown label")
            place = tuple(row.get(f) for f in PLACE) if places else None
            network = tuple(row[f] for f in NETWORK) if named else None
            rows.append((code, position, place, network, row))
    return Prepared(
        key,
        digest({"breakdowns": breakdowns, "figures": numbers}),
        {name: len(breakdowns[name]) for name in BREAKDOWNS},
        numbers | {k: window[k] for k in UNHASHED if k in window},
        rows,
    )


def write_lists(conn, build_id, prepared):
    """Store the prepared lists of one product group's windows; return their list ids in order.

    A signed list reuses the stored list with its signature, which must have its content_hash;
    the others are inserted with their rows."""
    cur = conn.cursor(row_factory=tuple_row)
    ids, new = [None] * len(prepared), []
    for n, found in enumerate(prepared):
        stored = cur.execute(STORED, found.key).fetchone() if found.key[3] is not None else None
        if stored and stored[1] != found.content_hash:
            raise ValueError("list signature is incomplete")
        if stored:
            ids[n] = stored[0]
        else:
            new.append(n)
    if not new:
        return ids
    keys = [prepared[n].key for n in new]
    if len(set(k[:3] for k in keys)) < len(keys):
        raise ValueError("A group's new lists must differ in their window")
    columns = [list(c) for c in zip(*keys, strict=True)]
    columns += [[prepared[n].content_hash for n in new]]
    columns += [[Jsonb(getattr(prepared[n], k)) for n in new] for k in ("entries", "figures")]
    written = {tuple(r[1:]): r[0] for r in cur.execute(INSERT, (build_id, *columns))}
    for n in new:
        ids[n] = written[prepared[n].key[:3]]
    _copy(conn, [(ids[n], prepared[n].rows) for n in new])
    return ids


def _copy(conn, lists):
    """COPY the rows of lists ((list_id, rows) pairs), their labels resolved to shared ids."""
    rows = [(list_id, *row) for list_id, found in lists for row in found]
    places, networks = labels(conn, *({r[i] for r in rows if r[i]} for i in (3, 4)))
    with conn.cursor().copy(f"COPY calculated_list_rows({LIST_ROW_COLUMNS}) FROM STDIN") as copy:
        for list_id, code, position, place, network, row in rows:
            copy.write_row(
                (
                    list_id,
                    code,
                    position,
                    places[place] if place else None,
                    networks[network] if network else None,
                    *[row[k] for k in ("ips", "observations", "completed", "successes")],
                    *[row[k] for k in ("challenges", "blocks", *LATENCY)],
                )
            )
