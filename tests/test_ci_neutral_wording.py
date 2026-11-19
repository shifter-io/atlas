"""NEUTRAL-WORDING-1: public text never singles out one network by a ranking effect."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "neutral_wording.py"
_spec = importlib.util.spec_from_file_location("neutral_wording", SCRIPT)
nw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nw)

NAMES = {
    "alpha-net": "alpha-net",
    "alphanet": "alpha-net",
    "alpha net": "alpha-net",
    "massive": "massive",
    "beta": "beta",
}


def git(repo: Path, *args: str) -> str:
    env_args = ["-c", "user.name=t", "-c", "user.email=t@users.noreply.github.com"]
    done = subprocess.run(
        ["git", "-C", str(repo), *env_args, *args], check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def repo_with_formulas(tmp_path: Path) -> Path:
    (tmp_path / "formulas").mkdir()
    for net in ("alpha-net", "beta"):
        formula = tmp_path / "formulas" / f"{net}.json"
        formula.write_text(json.dumps({"network": net}), encoding="utf-8")
    (tmp_path / "formulas" / "README.md").write_text("not a formula", encoding="utf-8")
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    git(tmp_path, "tag", "base")
    return tmp_path


def test_network_spellings_come_from_the_formulas(tmp_path):
    names = nw.networks(repo_with_formulas(tmp_path))
    assert names == {
        "alpha-net": "alpha-net",
        "alphanet": "alpha-net",
        "alpha net": "alpha-net",
        "beta": "beta",
    }


def test_a_network_next_to_a_ranking_effect_is_flagged_in_every_spelling():
    for text in (
        "Alpha-Net rises to the top of the 24h table.",
        "After this change Alpha Net moves up two places.",
        "The new weights mean AlphaNet overtakes the others.",
        "| Beta | gains 0.4 |",
    ):
        assert nw.findings(text, NAMES), text
    assert nw.findings("Beta ranks first now.", NAMES) == [("beta", "ranks")]


def test_neutral_method_text_passes():
    for text in (
        "The success score now weights every country the same for every network (fairness).",
        "Beta was measured in 52 countries.",  # a neutral fact
        "Ranking change: the latency component uses the median for all networks (accuracy).",
        "beta rises",  # not capitalised: a common word, not a name
        "Massive data volumes. The coverage gains are spread over all networks.",
    ):
        assert nw.findings(text, NAMES) == [], text


def test_the_window_and_the_sentence_bound_the_match():
    far = "Beta " + "x " * (nw.WINDOW + 1) + "rises"
    assert nw.findings(far, NAMES) == []
    assert nw.findings("Beta was tested. Scores rise for every network.", NAMES) == []
    assert nw.findings("- Beta was tested\n- gains everywhere", NAMES) == []


def test_main_flags_commits_and_the_pr_body_without_printing_them(tmp_path, capsys):
    repo = repo_with_formulas(tmp_path)
    (repo / "a.txt").write_text("a", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "SCORE-9: the method change for every network")
    assert nw.main([str(repo), "base", "HEAD"]) == 0
    body = repo / "body.md"
    body.write_text("Closes o/r#9\n\nWith this, Beta beats the field.", encoding="utf-8")
    assert nw.main([str(repo), "base", "HEAD", "--body-file", str(body)]) == 1
    out = capsys.readouterr().out
    assert "PR body: the network 'beta' next to 'beats'" in out and "the field" not in out
    git(repo, "commit", "-q", "--allow-empty", "-m", "Alpha-Net gains from the new weights")
    assert nw.main([str(repo), "base", "HEAD"]) == 1
    assert "commit " in capsys.readouterr().out


def test_usage_errors_and_the_script_entry_point(tmp_path):
    assert nw.main(["only-one"]) == 2
    assert nw.main([".", "a", "b", "--other", "x"]) == 2
    repo = repo_with_formulas(tmp_path)
    cmd = [sys.executable, str(SCRIPT), str(repo), "base", "HEAD"]
    done = subprocess.run(cmd, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
