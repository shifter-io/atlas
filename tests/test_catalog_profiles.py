"""The site catalog's ranking profiles (api/scripts/catalog-profiles.sql,
CATALOG-TARGETS-SCOPES-1): the five website profiles over the 8 score components, balanced the
methodology's [weights] and latency-first its [profiles.latency_first], so a client re-ranking
with a profile agrees with the calculator."""

import json
import re
import tomllib
from pathlib import Path

CATALOG = Path("api/scripts/catalog-profiles.sql")
# A row: id, label, description, then the weights object as JSONB.
ROW = re.compile(r"^\s*\('([\w-]+)',[^\n]*\n[^\n]*\n\s*'(\{[^']+\})'::jsonb", re.MULTILINE)
COMPONENTS = "reliability speed uptime coverage diversity dcAsns authenticity value".split()


def profiles():
    """The catalog's {profile: weights}, in file order."""
    return {p: json.loads(w) for p, w in ROW.findall(CATALOG.read_text())}


def test_catalog_has_the_five_profiles_over_every_component():
    rows = profiles()
    assert list(rows) == ["balanced", "hard-targets", "cost-first", "latency-first", "compliance"]
    assert all(list(w) == COMPONENTS and sum(w.values()) == 100 for w in rows.values())


def test_balanced_and_latency_first_are_the_methodology_weights():
    config = tomllib.loads(Path("configs/methodology.toml").read_text())
    rows = profiles()
    assert rows["balanced"] == config["weights"]
    assert rows["latency-first"] == config["profiles"]["latency_first"]
