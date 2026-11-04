"""CI-PRIVACY-ALLOW-1: the privacy step's IP allowlist, run with the workflow's own grep patterns."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def _patterns() -> tuple[str, str]:
    """The match and allow patterns of the Privacy step, as CI runs them."""
    step = WORKFLOW.read_text().split("- name: Privacy", 1)[1].split("- name:", 1)[0]
    match = re.search(r"grep -nE '([^']*)'", step)
    allow = re.search(r"grep -vE '([^']*)'", step)
    assert match and allow
    return match.group(1), allow.group(1)


def _flagged(added: str) -> bool:
    """True when the step would fail on a diff adding this line."""
    match, allow = _patterns()
    found = subprocess.run(["grep", "-nE", match], input=f"+{added}\n", capture_output=True, text=True)
    kept = subprocess.run(["grep", "-vE", allow], input=found.stdout, capture_output=True, text=True)
    return bool(kept.stdout.strip())


def _ip(*octets: int) -> str:
    # Built from parts, so this file adds no literal public address.
    return ".".join(str(o) for o in octets)


@pytest.mark.parametrize(
    "ip",
    [
        _ip(10, 0, 0, 1), _ip(10, 255, 3, 4),
        _ip(172, 16, 0, 1), _ip(172, 31, 255, 254),
        _ip(192, 168, 1, 1), _ip(169, 254, 169, 254),
        _ip(100, 64, 0, 1), _ip(100, 127, 255, 255),
        _ip(224, 0, 0, 251), _ip(239, 255, 255, 250),
        _ip(8, 8, 8, 8), _ip(1, 1, 1, 1), _ip(9, 9, 9, 9),
        _ip(127, 0, 0, 1), _ip(192, 0, 2, 7),
    ],
)
def test_allowed_ranges_pass(ip: str) -> None:
    assert not _flagged(f'allow = "{ip}"')


@pytest.mark.parametrize(
    "ip",
    [
        _ip(45, 33, 12, 7), _ip(172, 32, 0, 1), _ip(172, 15, 0, 1),
        _ip(100, 128, 0, 1), _ip(100, 63, 0, 1), _ip(240, 0, 0, 1),
        _ip(192, 169, 1, 1), _ip(8, 8, 4, 4), _ip(1, 1, 1, 2), _ip(110, 1, 2, 3),
    ],
)
def test_other_public_ips_fail(ip: str) -> None:
    assert _flagged(f'peer = "{ip}"')
