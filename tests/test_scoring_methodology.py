"""Methodology files (split from test_scoring.py): staging extends production, the latency
target must be a required target, and removed settings are refused."""

from pathlib import Path

import pytest

from atlas.calculated.policy import events, load, slot_settings


def test_staging_methodology_extends_production(tmp_path):
    production, production_id = load("configs/methodology.toml")
    staging, staging_id = load("configs/staging-methodology.toml")
    assert (production["minimum_requests"], staging["minimum_requests"]) == (4000, 1000)
    assert {k: v for k, v in staging.items() if k not in ("minimum_requests", "name")} == {
        k: v for k, v in production.items() if k not in ("minimum_requests", "name")
    }
    assert "extends" not in staging and staging_id != production_id
    assert slot_settings(staging) == {"min_latency_successes": 30}
    assert events("configs/staging-methodology.toml") == events("configs/methodology.toml")
    # Tables merge key by key; a cycle is refused.
    child = tmp_path / "child.toml"
    child.write_text(
        f'extends = "{Path("configs/methodology.toml").resolve()}"\n'
        '[products."shifter/residential"]\ndisplay_name = "Other"\n'
    )
    merged, _ = load(child)
    assert merged["products"]["shifter/residential"] == {
        "kind": "residential",
        "display_name": "Other",
    }
    (tmp_path / "a.toml").write_text('extends = "b.toml"\n')
    (tmp_path / "b.toml").write_text('extends = "a.toml"\n')
    with pytest.raises(ValueError, match="cycle"):
        load(tmp_path / "a.toml")


def test_latency_target_is_a_required_target(tmp_path):
    config, _ = load("configs/methodology.toml")
    assert config["latency_target"] == "generic_http"
    text = Path("configs/methodology.toml").read_text()
    policy = tmp_path / "methodology.toml"
    policy.write_text(text.replace('latency_target = "generic_http"', 'latency_target = "other"'))
    with pytest.raises(ValueError, match="latency target"):
        load(policy)


def test_removed_settings_are_refused(tmp_path):
    text = Path("configs/methodology.toml").read_text()
    for extra in ('scored_roles = ["benchmark"]\n', "[authenticity]\nblocklist_penalty = 120\n"):
        policy = tmp_path / "methodology.toml"
        policy.write_text(extra + text if extra.startswith("scored") else text + extra)
        with pytest.raises(ValueError, match="Unknown methodology setting"):
            load(policy)
