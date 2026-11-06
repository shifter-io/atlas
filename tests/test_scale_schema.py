"""SCALE-SCHEMA-1: the scale scripts in the schema proxy_scale of a database holding production.

`public` stands in for production: the generator's miniature with one run it did not write, the
calculated layer and a table of its own. The scripts then run with
PGOPTIONS='-c search_path=proxy_scale' and must leave every `public` row as it was.
"""

import importlib
import json
from argparse import Namespace
from pathlib import Path

import pytest
import test_scale_cycle as scale

from proxy_index import storage
from proxy_index.storage import connect

generate, cycle, measure = scale.generate, scale.cycle, scale.measure

pytest_plugins = ["test_storage", "calculated_template"]

SCHEMA_ONLY = "-c search_path=proxy_scale"
TABLES = (
    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
    "WHERE n.nspname=current_schema() AND c.relkind IN ('r','p') AND NOT c.relispartition "
    "ORDER BY 1"
)
ANY_TABLE = (
    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
    "WHERE n.nspname IN ('public','proxy_scale') AND c.relkind IN ('r','p')"
)


@pytest.fixture
def pgoptions(monkeypatch):
    """Set (a value) or clear (None) libpq's PGOPTIONS for every later connection."""
    monkeypatch.delenv("PGOPTIONS", raising=False)

    def use(value):
        if value is None:
            monkeypatch.delenv("PGOPTIONS", raising=False)
        else:
            monkeypatch.setenv("PGOPTIONS", value)

    return use


def fingerprint(config) -> dict:
    """Row count and digest of every table of the session's schema."""
    with connect(config) as conn:
        return {
            table: conn.execute(
                f"SELECT count(*),md5(coalesce(string_agg(t::text,'|' ORDER BY t::text),'')) "
                f"FROM {table} t"
            ).fetchone()
            for (table,) in conn.execute(TABLES).fetchall()
        }


def schema_args(config, days="1"):
    """generate.py's arguments for the miniature; the target is the default (proxy_scale)."""
    values = scale.MINIATURE | {"database": config, "days": generate.parse_days(days)}
    return Namespace(**values)


def scratch_schema(config):
    with connect(config) as conn:
        conn.execute("CREATE SCHEMA proxy_scale")


def test_initialize_and_migrate_use_the_session_schema(database, pgoptions):
    config = database  # public.schema_versions, as in production, from the template
    scratch_schema(config)
    pgoptions(SCHEMA_ONLY)
    with pytest.raises(ValueError, match="No schema to migrate"):
        storage.migrate(config)
    storage.initialize(config)
    with connect(config) as conn:
        assert conn.execute("SELECT current_schema()").fetchone()[0] == "proxy_scale"
        found = conn.execute(
            "SELECT to_regclass('proxy_scale.schema_versions') IS NOT NULL,"
            "to_regclass('public.schema_versions') IS NOT NULL"
        ).fetchone()
        assert found == (True, True)
        assert conn.execute("SELECT count(*) FROM schema_versions").fetchone()[0] == 1
    assert storage.migrate(config)["migrated"] is False


def test_generate_cycle_and_measure_write_only_the_scratch_schema(
    calculated_database, tmp_path, pgoptions
):
    config = calculated_database  # the calculated layer, from its template
    scale.dataset(config, "1")  # the production stand-in, in public (old mode, by name)
    with connect(config) as conn:
        conn.execute("UPDATE test_runs SET summary=summary-'scale_generator' WHERE run_key=1")
        conn.execute("CREATE TABLE production_only(note text)")
        conn.execute("INSERT INTO production_only VALUES ('kept')")
    production = fingerprint(config)
    scratch_schema(config)

    pgoptions(SCHEMA_ONLY)
    result = generate.generate(schema_args(config))
    assert result["generated"] == [1]
    with connect(config) as conn:
        assert conn.execute("SELECT to_regclass('proxy_scale.schema_versions')").fetchone()[0]
        assert conn.execute("SELECT count(*) FROM test_runs").fetchone()[0] > 0
    finished = cycle.run(scale.options(config, tmp_path))
    assert finished["hours_done"] == 1
    report = json.loads(Path(finished["report"]).read_text())
    assert (report["database"], report["schema"]) == (scale.name(config), "proxy_scale")
    with connect(config) as conn:
        assert measure.has_statements(conn) is False
        sizes = measure.sizes(conn)
        built = conn.execute("SELECT build_id FROM current_calculation").fetchone()
    assert "production_only" not in sizes["tables"] and sizes["tables"]["ip_observations"] > 0
    assert sizes["tables_bytes"] == sum(sizes["tables"].values()) and built is not None
    assert set(sizes["tables"]) == set(fingerprint(config))

    pgoptions(None)
    assert fingerprint(config) == production


def test_other_targets_and_a_wider_search_path_are_refused(databases, pgoptions, capsys):
    config = databases()
    with pytest.raises(generate.Refused, match="writes only to 'proxy_scale'"):
        generate.generate(schema_args(config))
    scratch_schema(config)
    wider = ("-c search_path=proxy_scale,public", '-c search_path="$user",proxy_scale')
    for options in (None, *wider):
        pgoptions(options)
        with pytest.raises(generate.Refused, match="with search_path"):
            generate.generate(schema_args(config))
        assert generate.main(["--database", str(config), "--days", "1"]) == 2
        assert "search_path exactly 'proxy_scale'" in capsys.readouterr().err
        with connect(config) as conn, pytest.raises(cycle.Refused, match="uses only"):
            cycle.guard(conn)
    with connect(config) as conn:
        conn.execute("DROP SCHEMA proxy_scale")
    pgoptions(SCHEMA_ONLY)  # the schema is gone: no current schema
    with pytest.raises(generate.Refused, match="search_path 'proxy_scale'"):
        generate.generate(schema_args(config))
    pgoptions(None)
    with connect(config) as conn:
        assert conn.execute(ANY_TABLE).fetchone()[0] == 0


def test_the_operator_sql_and_the_read_only_check(databases, pgoptions, capsys):
    target = importlib.import_module("target")
    assert target.main(["sql"]) == 0
    printed = capsys.readouterr().out
    assert "CREATE SCHEMA proxy_scale AUTHORIZATION proxy_admin;" in printed
    assert "DROP SCHEMA proxy_scale CASCADE;" in printed
    assert "SELECT count(*) FROM pg_namespace WHERE nspname='proxy_scale';" in printed
    config = databases()
    assert target.main(["check", "--database", str(config)]) == 2
    assert "refused: the database is" in capsys.readouterr().out
    scratch_schema(config)
    pgoptions(SCHEMA_ONLY)
    assert target.main(["check", "--database", str(config)]) == 0
    assert capsys.readouterr().out.endswith("allowed\n")
    with pytest.raises(SystemExit):
        target.main(["check"])
