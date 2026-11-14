"""CI-PRIVACY-ALLOW-1, SEC-EVENTS-SAFE-1: the privacy step's matches and allowlist, run with the workflow's own greps."""

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


# SEC-EVENTS-SAFE-1: global IPv6 addresses, /home/<name>/ paths and e-mail addresses of
# any domain. Built from parts so this file adds none of them literally.
AT = "@"
FLAGGED = [
    ":".join(["2a01", "4f8", "c17", "1a2b", "", "1"]),
    ":".join(["2606", "4700", "", "1111"]),
    "/ho" + "me/" + "ops/notes.txt",
    "ops" + AT + "corp.de",
    "first.last" + AT + "mail.co.uk",
    "a" + AT + "b.io",
]
CLEAN = [
    ":".join(["2001", "db8", "", "1"]),
    ":".join(["2001", "0db8", "85a3", "", "7"]),
    "::1",
    "fe80::1",
    "fd12:3456::1",
    "2026-11-14T03:00:00Z",
    "ports 3000:3000",
    "/home/",
    "ops" + AT + "example.com",
    "ops" + AT + "mail.example.org",
    "12345+user" + AT + "users.noreply.github.com",
    "noreply" + AT + "corp.de",
    "uses: actions/checkout" + AT + "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "golang.org/x/sync" + AT + "v0.22.0",
]


@pytest.mark.parametrize("text", FLAGGED)
def test_private_data_fails(text: str) -> None:
    assert _flagged(f'value = "{text}"')


@pytest.mark.parametrize("text", CLEAN)
def test_documentation_and_noreply_pass(text: str) -> None:
    assert not _flagged(f'value = "{text}"')
