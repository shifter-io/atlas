"""CTRY-1: the answer-class columns of the `campaign shot` and `campaign probe` tables."""

from __future__ import annotations

from campaign_probe_cases import classified

from proxy_index.campaign import probe as probing
from proxy_index.campaign import shot as shooting


def test_markdown_answer_class_columns():
    """CTRY-1: the shot and probe tables show rate limits in their own column and the answer
    classes beside them; a result written before them renders "-"."""
    new = probing.step_result(500, 1000, classified())
    old = {
        k: v
        for k, v in new.items()
        if not k.endswith(
            ("no_inventory_pct", "other_country_pct", "duplicates_pct", "real_error_pct")
        )
    }
    cell = {
        "country": "US",
        "size": "c-4",
        "concurrency": 500,
        "outcome": "measured",
        "start_lag_seconds": 1.5,
        "step_down": [],
    }
    shot = shooting.markdown(
        {
            "shot_id": "s",
            "requests": 1000,
            "start_at": "t",
            "cells": {"geonode/nyc3": cell | {"step": new}, "soax/lon1": cell | {"step": old}},
        }
    )
    header, rule, *rows = shot.splitlines()[4:]
    columns = [c.strip() for c in header.strip("|").split("|")]
    assert columns[8] == "Rate-limited %" and "429 %" not in shot
    assert columns[17:21] == [
        "No-IP %",
        "Other-country %",
        "Duplicates %",
        "Real errors % (answered)",
    ]
    assert rule.count("---") == len(columns) == 23
    cells = [[c.strip() for c in row.strip("|").split("|")] for row in rows]
    assert cells[0][8] == "5.0" and cells[0][17:21] == ["0.5", "1.0", "9.0", "15.263"]
    assert cells[1][17:21] == ["-"] * 4 and all(len(c) == 23 for c in cells)
    probe = probing.markdown(
        {
            "probe_id": "p",
            "requests": 1000,
            "country": "US",
            "size": "c-4",
            "region": "nyc3",
            "networks": {"geonode": {"max_concurrency": 500, "steps": [new, old]}},
        }
    )
    header, rule, *rows = probe.split("\n\n")[-1].splitlines()
    columns = [c.strip() for c in header.strip("|").split("|")]
    assert columns[4] == "Rate-limited %" and (
        columns[12:16] == ["No-IP %", "Other-country %", "Duplicates %", "Real errors % (answered)"]
    )
    assert rule.count("---") == len(columns) == 17
    cells = [[c.strip() for c in row.strip("|").split("|")] for row in rows]
    assert cells[0][12:16] == ["0.5", "1.0", "9.0", "15.263"] and cells[1][12:16] == ["-"] * 4
