"""SEED-ON-DEPLOY-1 (#1284): the deploy's `atlas db-seed --only targets --require-catalog`.

On staging (16 Nov 2026) the social group's sources were served with `group = social_web` but
without a `social_web` row: the seed and the catalog SQL were manual steps, and the deploy shipped
only code. These tests use a real PostgreSQL with the site API's own schema
(api/internal/store/migrations/0001_site_schema.sql, its revision triggers included): a database
with the methodology's groups but a catalog without social_web fails loudly while the seed may not
write, and the seed fixes it once it may; a complete catalog is left exactly as it was.
"""

import json
import secrets
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from atlas import storage_catalog
from atlas.calculated.policy import load
from atlas.cli import main
from atlas.providers.credentials import read_credentials
from atlas.regions import load_targets
from atlas.storage import connect, seed

pytest_plugins = ["test_storage"]

ROOT = Path(__file__).resolve().parents[1]
SITE_SCHEMA = ROOT / "api" / "internal" / "store" / "migrations" / "0001_site_schema.sql"
CATALOG_SQL = ROOT / "api" / "scripts" / "catalog-targets.sql"
SOCIAL = ("social_web", "social", 3, 55, False)
ROWS = "SELECT target_id, category, difficulty, sort_order, is_demo FROM site.targets"
STAMP = "SELECT value, (SELECT max(updated_at) FROM site.targets) FROM site.revision"


def config():
    return load(ROOT / "configs" / "methodology.toml")[0]


@pytest.fixture
def site(database):
    """The framework database with schema site (`siteapi migrate`) and the catalog SQL loaded."""
    with connect(database) as conn:
        conn.execute(SITE_SCHEMA.read_text())
        conn.execute(CATALOG_SQL.read_text())
    return database


@pytest.fixture
def reader(site, tmp_path):
    """A login like the run-time role before the owner's grant: it writes public.targets but only
    reads site (dbserver/check.py `site_read`)."""
    credentials = read_credentials(site, "postgres") | {"sslmode": "disable"}
    role, password = f"seed_{uuid.uuid4().hex[:8]}", secrets.token_hex(16)
    name = sql.Identifier(role)
    with psycopg.connect(**credentials, autocommit=True) as conn:
        create = sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(name, sql.Literal(password))
        conn.execute(create)
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public, site TO {}").format(name))
        conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA site TO {}").format(name))
        conn.execute(sql.SQL("GRANT SELECT ON public.schema_versions TO {}").format(name))
        grant = "GRANT SELECT, INSERT, UPDATE ON public.targets TO {}"
        conn.execute(sql.SQL(grant).format(name))
    path = tmp_path / "reader.toml"
    login = credentials | {"user": role, "password": password}
    path.write_text("[postgres]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in login.items()))
    path.chmod(0o600)
    try:
        yield path
    finally:
        with psycopg.connect(**credentials, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(name))
            conn.execute(sql.SQL("DROP ROLE {}").format(name))


def deploy_seed(database):
    args = ["db-seed", "--only", "targets", "--require-catalog", "--database-file", str(database)]
    return main(args)


def query(database, statement):
    with connect(database) as conn:
        return conn.execute(statement).fetchall()


def drop_social(database):
    with connect(database) as conn:
        conn.execute("DELETE FROM site.targets WHERE target_id = 'social_web'")
        conn.execute("DELETE FROM targets WHERE target_id = 'social_web'")


def test_the_catalog_sql_and_the_seed_write_the_same_rows(site):
    """Every target's row the seed wants equals the operator's api/scripts/catalog-targets.sql."""
    wanted = storage_catalog.wanted(load_targets(), config())
    assert {r[0]: tuple(r[1:]) for r in query(site, ROWS)} == wanted
    assert wanted["social_web"] == SOCIAL[1:]


def test_a_complete_catalog_is_left_exactly_as_it_was(site, capsys):
    assert main(["db-seed", "--only", "targets", "--database-file", str(site)]) == 0
    capsys.readouterr()
    before = query(site, STAMP)
    assert deploy_seed(site) == 0
    assert json.loads(capsys.readouterr().out)["site_targets"] == {"written": [], "pending": []}
    assert query(site, STAMP) == before  # no revision bump: the API does not rebuild


def test_a_missing_group_fails_loudly_until_the_seed_may_write(site, reader, capsys):
    drop_social(site)
    before = query(site, STAMP)
    assert deploy_seed(reader) == 2
    err = capsys.readouterr().err
    assert "site.targets lacks or differs for social_web" in err
    assert storage_catalog.GRANT in err and "api/scripts/catalog-targets.sql" in err
    # The framework's row is seeded and kept, the catalog untouched.
    assert query(site, "SELECT label FROM targets WHERE target_id = 'social_web'") == [
        ("Social media web app",)
    ]
    assert query(site, STAMP) == before

    assert deploy_seed(site) == 0  # a login that may write site.targets: the seed fixes it
    assert SOCIAL in query(site, ROWS)
    # One upsert statement: its INSERT and UPDATE statement triggers bump the revision once each.
    assert query(site, "SELECT value FROM site.revision")[0][0] == before[0][0] + 2
    assert deploy_seed(reader) == 0  # complete now: the reader passes too


def test_a_changed_difficulty_is_written_in_one_statement(site):
    with connect(site) as conn:
        wrong = "UPDATE site.targets SET difficulty = 1 WHERE target_id IN ('x_web', 'ai_gemini')"
        conn.execute(wrong)
        conn.execute("UPDATE site.targets SET is_demo = true WHERE target_id = 'amazon_pdp'")
    revision = query(site, "SELECT value FROM site.revision")[0][0]
    with connect(site) as conn, conn.cursor() as cursor:
        result = storage_catalog.seed(cursor, load_targets(), config())
    assert result == {"written": ["amazon_pdp", "x_web", "ai_gemini"], "pending": []}
    assert query(site, "SELECT value FROM site.revision")[0][0] == revision + 2
    rows = {r[0]: r for r in query(site, ROWS)}
    assert rows["x_web"][2] == rows["ai_gemini"][2] - 2 == 3 and rows["amazon_pdp"][4] is False


def test_a_database_without_schema_site(database, capsys):
    """A framework-only database (a droplet manager, no API): the seed skips the catalog; the
    deploy's check refuses, because the API it switches to would have no catalog."""
    result = seed(database)
    assert result["site_targets"] is None
    assert deploy_seed(database) == 2
    assert "No schema site" in capsys.readouterr().err


def test_the_full_seed_writes_the_catalog_too(site):
    drop_social(site)
    assert seed(site)["site_targets"] == {"written": ["social_web"], "pending": []}
    assert SOCIAL in query(site, ROWS)


def test_require_catalog_goes_with_only_targets(site, capsys):
    assert main(["db-seed", "--require-catalog", "--database-file", str(site)]) == 2
    assert "--require-catalog goes with --only targets" in capsys.readouterr().err


def test_the_methodology_targets_need_a_category(tmp_path):
    targets = [t for t in load_targets() if t["target_id"] != "x_web"]
    with pytest.raises(ValueError, match=r"without a category in targets.toml: \['x_web'\]"):
        storage_catalog.wanted(targets, config())
    stray = [*load_targets(), {"target_id": "stray", "category": "social", "sort_order": 1}]
    with pytest.raises(ValueError, match="stray has a category but no difficulty"):
        storage_catalog.wanted(stray, config())
    required = storage_catalog.required(config())
    assert {"social_web", "instagram_web", "x_web", "ai_answers", "generic_http"} <= required
