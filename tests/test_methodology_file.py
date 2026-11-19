"""The methodology files the site API serves (METHODOLOGY-FILE-1, #1179): GET
/v1/methodology/<id>.json is the rules a methodology id is the SHA-256 of. The API rebuilds
those bytes from public.methodologies in Go (api/internal/pyjson); these tests hold its
Python-written vectors to Python and the recompute recipe of api/README.md to policy.load()."""

import hashlib
import json
import tomllib
from pathlib import Path

from atlas.calculated.policy import digest, load, read

ROOT = Path(__file__).resolve().parents[1]
TESTDATA = ROOT / "api" / "internal" / "pyjson" / "testdata"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def test_the_readme_recipe_gives_the_methodology_id():
    # api/README.md, "Methodology files": a file without `extends`, read as TOML, [events] dropped.
    path = ROOT / "configs" / "methodology.toml"
    rules = tomllib.loads(path.read_text())
    rules.pop("events", None)
    assert hashlib.sha256(canonical(rules).encode()).hexdigest() == load(path)[1]
    # A file that extends another: its merged settings (policy.read), [events] dropped.
    staging = ROOT / "configs" / "staging-methodology.toml"
    merged = read(staging)
    merged.pop("events", None)
    assert hashlib.sha256(canonical(merged).encode()).hexdigest() == load(staging)[1]


def test_the_go_vectors_are_pythons():
    vectors = json.loads((TESTDATA / "vectors.json").read_text())
    for case in vectors["documents"]:
        assert canonical(json.loads(case["in"])) == case["out"]
    assert all(repr(float(text)) == text for text in vectors["floats"])
    body = (TESTDATA / "methodology.json").read_bytes()
    assert canonical(json.loads(body)).encode() == body
    assert digest(json.loads(body)) == hashlib.sha256(body).hexdigest()
