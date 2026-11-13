"""COV-2: `manager_daily.verify` fails a manager step that exited non-zero or printed no
`daily-check` line, even when the formulas match."""

from __future__ import annotations

import json

import pytest

from atlas.campaign import deploy, manager_daily

SENT = {"a.json": "1" * 64}
FORMULA = f"formula {'1' * 64} a.json"


def check(**values):
    return "daily-check " + json.dumps({"ok": True, "issues": []} | values)


def test_a_passing_check_with_the_sent_formulas_is_returned():
    out = f"{FORMULA}\n{check(hours=24)}\n"
    assert manager_daily.verify(out, 0, SENT, "daily install") == {
        "ok": True,
        "issues": [],
        "hours": 24,
    }


def test_a_non_zero_exit_fails_even_after_a_passing_check():
    out = f"{FORMULA}\n{check()}\n"
    with pytest.raises(deploy.DeployError, match=r"manager step failed: daily install \(exit 2\)"):
        manager_daily.verify(out, 2, SENT, "daily install")


def test_no_daily_check_line_fails_the_step_even_with_exit_0():
    with pytest.raises(deploy.DeployError, match=r"manager step failed: daily status \(exit 0\)"):
        manager_daily.verify(f"{FORMULA}\nsomething else\n", 0, {}, "daily status")
