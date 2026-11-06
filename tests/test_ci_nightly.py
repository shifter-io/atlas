"""NIGHTLY-1: the nightly dependency audit uses ci.yml's pinned uv, never the server's Python."""

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _runs(workflow: dict) -> str:
    return "\n".join(step.get("run", "") for step in workflow["jobs"]["audit"]["steps"])


@pytest.mark.parametrize("key", ["UV_VERSION", "UV_SHA256", "PYTHON_VERSION"])
def test_nightly_pins_equal_ci(key):
    assert _load("maestro-nightly.yml")["env"][key] == _load("ci.yml")["env"][key]


def test_nightly_audits_the_locked_set_with_uv_only():
    nightly = _load("maestro-nightly.yml")
    runs = _runs(nightly)
    assert nightly["env"]["PIP_AUDIT_VERSION"] == "2.9.0"
    assert "uv export --locked --all-extras --all-groups --no-emit-project" in runs
    assert "--require-hashes --no-deps --disable-pip" in runs
    assert "sha256sum -c --quiet -" in runs
    assert "python3" not in runs
