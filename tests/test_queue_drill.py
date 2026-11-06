"""Merge-queue drill (2026-11-06): fails once the drill marker is on main. Safe to delete."""

from pathlib import Path


def test_the_drill_marker_is_absent():
    assert not (Path(__file__).parent / "queue_drill_marker.txt").exists()
