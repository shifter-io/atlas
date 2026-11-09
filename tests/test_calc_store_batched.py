"""CALC-WRITES-1: the calculation's typed rows go in a batch at a time (batch.py), and they are
the rows the former writer stored with one INSERT per row.

OldWriter below is that writer as it was (lists.py before CALC-WRITES-1), kept as the oracle. A
real build's persist() calls are recorded and replayed, its product groups copied under new
cohorts until the build has more than batch.CHUNK series points, once through the oracle and
once through lists.persist() with one Batch, as store.write() does. Both builds' rows are equal,
ids and timestamps aside, and the batched one costs a fixed number of statements per flush.
"""

import copy
import importlib
import math
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from calculated_cases import benchmark_scenario, payload_mismatches

from proxy_index.calculated import batch, lists
from proxy_index.calculated.calculator import calculate
from proxy_index.calculated.list_write import prepare, write_lists
from proxy_index.storage import column, connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scale"))
measure = importlib.import_module("measure")

pytest_plugins = ["test_storage"]

BATCHED = (
    "cohorts",
    "cohort_criteria",
    "score_reasons",
    "calculated_windows",
    "calculated_scores",
    "calculated_score_components",
    "calculated_score_reasons",
    "calculated_series",
    "calculated_overlaps",
)


class OldWriter:
    """lists.persist() before CALC-WRITES-1: an INSERT per window, score, series point and
    overlap, two statements per cohort and a reason lookup per product group."""

    def __init__(self, conn):
        self.conn = conn

    def persist(self, payload):
        build_id = int(payload["snapshot_id"])
        for p in payload["products"]:
            lists.store_cohort(self.conn, p["cohort"], p["criteria"])
        reason_ids = self.reasons(payload["products"])
        for p in payload["products"]:
            new = [(label, w) for label, w in p["windows"].items() if "lists" not in w]
            prepared = [prepare(p, w) for _, w in new]
            written = write_lists(self.conn, build_id, prepared)
            ids = dict(zip([label for label, _ in new], written, strict=True))
            for label, w in p["windows"].items():
                list_id = w["lists"]["id"] if "lists" in w else ids[label]
                self.results((build_id, list_id), p, w, reason_ids)
        for o in payload["overlap"]:
            a, b, na, nb = o["product_a"], o["product_b"], o["ips_a"], o["ips_b"]
            if a > b:
                a, b, na, nb = b, a, nb, na
            hours = payload["periods"][o["window"]]["hours"]
            self.conn.execute(
                "INSERT INTO calculated_overlaps VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (build_id, o["cohort"], hours, a, b, o["shared_ips"], na, nb),
            )

    def reasons(self, products):
        texts = set()
        for p in products:
            for w in p["windows"].values():
                for target, score in [("", s) for s in w["scores"].values()] + [
                    (t, v["score"]) for t, v in w["targets"].items()
                ]:
                    texts.update(c["reason"] for c in score["components"].values() if c["reason"])
                    texts.update(r for _, r in lists.split_reasons(score, not target))
        found = batch.labels(self.conn, "score_reasons", {(t,) for t in texts})
        return {k[0]: v for k, v in found.items()}

    def results(self, ids, p, w, reason_ids):
        build_id, list_id = ids
        for target, scores in [
            ("", w["scores"]),
            *[(t, {"balanced": v["score"]}) for t, v in w["targets"].items()],
        ]:
            row = lists._window((build_id, None if target else list_id), p, w, target)
            result = self.conn.execute(
                f"INSERT INTO calculated_windows({batch.WINDOW_COLUMNS}) VALUES ("
                + ",".join(["%s"] * 31)
                + ") RETURNING result_id",
                row,
            ).fetchone()[0]
            for profile, s in scores.items():
                self.score(result, profile, s, not target, reason_ids)
            if not target:
                for point in w["series"]:
                    self.conn.execute(
                        "INSERT INTO calculated_series VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            result,
                            point["at"],
                            point["completed"],
                            point["successes"],
                            *[point["latency_ms"][k] for k in ("p50", "p90", "p99")],
                            point["unique_ips"],
                            point["uptime"],
                        ),
                    )

    def score(self, result, profile, s, product, reason_ids):
        fields = ("score", "grade", "rank", "previous_rank", "state", "scored_at", "valid_until")
        self.conn.execute(
            "INSERT INTO calculated_scores(result_id,profile,score,grade,rank,"
            "previous_rank,state,scored_at,valid_until) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (result, profile, *[s[k] for k in fields]),
        )
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO calculated_score_components(result_id,profile,component,"
                "position,state,weight,value,inputs,reason_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [
                    (
                        result,
                        profile,
                        k,
                        i,
                        c["state"],
                        column("number", s["weights"][k]),
                        column("number", c["value"]),
                        column("json", c["inputs"]),
                        reason_ids.get(c["reason"]),
                    )
                    for i, (k, c) in enumerate(s["components"].items())
                ],
            )
            cur.executemany(
                "INSERT INTO calculated_score_reasons VALUES (%s,%s,%s,%s)",
                [(result, profile, i, reason_ids[r]) for i, r in lists.split_reasons(s, product)],
            )


def copies(calls, build_id, tag, count):
    """calls (recorded persist() payloads) for build_id, every product group and overlap repeated
    count times under the cohort "<cohort>#<tag>-<n>"."""
    out = []
    for payload in calls:
        for n in range(count):
            cohort = lambda c: f"{c}#{tag}-{n}"  # noqa: B023, E731
            groups = [
                g | {"id": f"{g['id']}#{n}", "cohort": cohort(g["cohort"])}
                for g in payload["products"]
            ]
            overlap = [o | {"cohort": cohort(o["cohort"])} for o in payload["overlap"]]
            out.append(payload | {"snapshot_id": str(build_id), "products": groups})
            out[-1]["overlap"] = overlap
    return out


# Each build's rows: windows by their natural key (list by its content), and their children.
SAME = "regexp_replace({}cohort,'#(old|new)-','#-')"
WINDOW_KEY = f"w.product_id,{SAME.format('w.')},w.target,w.window_hours"
DUMPS = {
    "windows": "SELECT {key},to_jsonb(w)-'result_id'-'build_id'-'list_id'-'cohort',"
    "l.content_hash,l.entries,l.figures,(SELECT md5(string_agg((to_jsonb(r)-'list_id')::text,',' "
    "ORDER BY dimension,position)) FROM calculated_list_rows r WHERE r.list_id=l.list_id) "
    "FROM calculated_windows w LEFT JOIN calculated_lists l USING(list_id) WHERE w.build_id=%s",
    "scores": "SELECT {key},to_jsonb(s)-'result_id' FROM calculated_scores s "
    "JOIN calculated_windows w USING(result_id) WHERE w.build_id=%s",
    "components": "SELECT {key},to_jsonb(c)-'result_id'-'reason_id',r.reason "
    "FROM calculated_score_components c JOIN calculated_windows w USING(result_id) "
    "LEFT JOIN score_reasons r USING(reason_id) WHERE w.build_id=%s",
    "reasons": "SELECT {key},to_jsonb(c)-'result_id'-'reason_id',r.reason "
    "FROM calculated_score_reasons c JOIN calculated_windows w USING(result_id) "
    "JOIN score_reasons r USING(reason_id) WHERE w.build_id=%s",
    "series": "SELECT {key},to_jsonb(s)-'result_id' FROM calculated_series s "
    "JOIN calculated_windows w USING(result_id) WHERE w.build_id=%s",
    "overlaps": f"SELECT {SAME.format('')},to_jsonb(o)-'build_id'-'cohort' "
    "FROM calculated_overlaps o WHERE build_id=%s",
    "cohorts": f"SELECT {SAME.format('')},criteria FROM cohort_criteria WHERE cohort LIKE %s",
}


def dump(conn, build_id, tag):
    found = {}
    for name, query in DUMPS.items():
        param = f"%#{tag}%" if name == "cohorts" else build_id
        rows = conn.execute(query.format(key=WINDOW_KEY), (param,)).fetchall()
        found[name] = sorted(str(r) for r in rows)
    return found


def new_build(conn, build_id, tag):
    return conn.execute(
        "INSERT INTO calculation_builds(methodology_id,as_of,input_hash) SELECT methodology_id,"
        "as_of,input_hash||%s FROM calculation_builds WHERE build_id=%s RETURNING build_id",
        (tag, build_id),
    ).fetchone()[0]


def touched(tracer):
    """Calls of each statement shape on the batched tables."""
    return {
        row["statement"]: row["calls"]
        for row in tracer.summary()["shapes"]
        if any(table in row["statement"] for table in BATCHED)
    }


@pytest.mark.asyncio
async def test_batched_rows_equal_the_rows_of_one_insert_per_row(database, tmp_path, monkeypatch):
    policy, last, _ = await benchmark_scenario(database, tmp_path)
    calls, original = [], lists.persist

    def record(conn, payload, **options):
        calls.append(copy.deepcopy(payload))
        return original(conn, payload, **options)

    monkeypatch.setattr(lists, "persist", record)
    second = calculate(database, policy, as_of=last + timedelta(minutes=10), publish=True)
    monkeypatch.undo()
    built = int(second["snapshot_id"])
    assert payload_mismatches(database, built) == []
    with connect(database) as conn:
        points = conn.execute(
            "SELECT count(*) FROM calculated_series JOIN calculated_windows USING(result_id) "
            "WHERE build_id=%s",
            (built,),
        ).fetchone()[0]
        count = batch.CHUNK // points + 1  # copies: more than CHUNK series points
        old, new = new_build(conn, built, "-old"), new_build(conn, built, "-new")
        writer = OldWriter(conn)
        for payload in copies(calls, old, "old", count):
            writer.persist(payload)
        flushes, flush = [], batch.Batch.flush
        monkeypatch.setattr(batch.Batch, "flush", lambda self: flushes.append(1) or flush(self))
        rows = batch.Batch(conn)
        with measure.Tracer() as tracer:
            for payload in copies(calls, new, "new", count):
                lists.persist(conn, payload, batch=rows)
            rows.flush()
        before, after = dump(conn, old, "old"), dump(conn, new, "new")
        solo = new_build(conn, built, "-solo")  # without a batch: written before it returns
        lists.persist(conn, copies(calls[:1], solo, "solo", 1)[0])
        query = "SELECT count(*) FROM calculated_windows WHERE build_id=%s"
        assert conn.execute(query, (solo,)).fetchone()[0] > 0
    assert len(after["series"]) == points * count > batch.CHUNK
    assert {k: len(v) for k, v in after.items()} == {k: len(v) for k, v in before.items()}
    assert after == before
    # A fixed number of statements per flush, however many rows: no INSERT per row.
    chunks = math.ceil(points * count / batch.CHUNK)
    assert 2 <= len(flushes) <= chunks + 1
    for statement, calls_of in touched(tracer).items():
        limit = len(flushes) + chunks if "calculated_series" in statement else 3 * len(flushes)
        assert calls_of <= limit, statement
