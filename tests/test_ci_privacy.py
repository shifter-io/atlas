"""CI-PRIVACY-ALLOW-1: the privacy step's IP allowlist, run with the workflow's own greps."""

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
    found = _grep("-nE", match, f"+{added}\n")
    return bool(_grep("-vE", allow, found).strip())


def _grep(flag: str, pattern: str, text: str) -> str:
    done = subprocess.run(["grep", flag, pattern], input=text, capture_output=True, text=True)
    return done.stdout


# Octets joined by "/" so this file adds no literal public address.
ALLOWED = (
    "10/0/0/1 10/255/3/4 172/16/0/1 172/31/255/254 192/168/1/1 169/254/169/254 100/64/0/1 "
    "100/127/255/255 224/0/0/251 239/255/255/250 8/8/8/8 1/1/1/1 9/9/9/9 127/0/0/1 192/0/2/7"
).split()
PUBLIC = (
    "45/33/12/7 172/32/0/1 172/15/0/1 100/128/0/1 100/63/0/1 240/0/0/1 192/169/1/1 "
    "8/8/4/4 1/1/1/2 110/1/2/3"
).split()


@pytest.mark.parametrize("ip", ALLOWED)
def test_allowed_ranges_pass(ip: str) -> None:
    assert not _flagged(f'allow = "{ip.replace("/", ".")}"')


@pytest.mark.parametrize("ip", PUBLIC)
def test_other_public_ips_fail(ip: str) -> None:
    assert _flagged(f'peer = "{ip.replace("/", ".")}"')
