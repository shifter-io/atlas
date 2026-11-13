"""RENAME-ATLAS-1: the old product name stays out of the repository outside the reviewed keep-list
(scripts/rename_to_atlas.keep, explained in docs/plans/atlas-rename.md), and
scripts/rename_to_atlas.sh brings a branch's own files over the same way, idempotently."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "rename_to_atlas", ROOT / "scripts/rename_to_atlas.py"
)
rename = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rename)

OLD_MODULE = "proxy_index"  # the package name an open branch may still carry


def test_the_old_name_is_only_where_the_keep_list_allows_it():
    found = rename.check(ROOT, rename.load_keep())
    assert not found, (
        "the old product name is back outside scripts/rename_to_atlas.keep; run "
        "scripts/rename_to_atlas.sh (or keep it there with its reason):\n" + "\n".join(found[:40])
    )


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def branch(tmp_path: Path) -> Path:
    """A checkout as an open branch has it: old paths and names, one file already renamed, one
    file git does not track yet, an applied schema file and a lock file."""
    repo = tmp_path / "repo"
    files = {
        f"src/{OLD_MODULE}/new_part.py": (
            f"from {OLD_MODULE}.storage import connect\n"
            'TITLE = "Proxy Index campaign"\n'
            'BASE = "/opt/proxy-index-campaign/venv/bin/proxy-index"\n'
            'PROJECT_NAME = "Proxy Index"\n'
        ),
        f"src/{OLD_MODULE}/schema.sql": "-- seeded by proxy-index db-seed\n",
        "src/atlas/kept_already.py": "VALUE = 1\n",
        "docs/page.md": (
            "A Proxy Index page links /proxy-index/networks and reads "
            "PROXY_INDEX_DATABASE_FILE, the bucket proxy-index-archive.\n"
        ),
        "uv.lock": (
            'version = 1\n\n[[package]]\nname = "attrs"\nversion = "1"\n\n'
            '[[package]]\nname = "proxy-index"\nversion = "2"\n\n'
            '[[package]]\nname = "zipp"\nversion = "3"\n'
        ),
    }
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git(repo.parent, "init", "-q", str(repo))
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "branch")
    (repo / f"tests/test_{OLD_MODULE}_new.py").parent.mkdir(parents=True)
    (repo / f"tests/test_{OLD_MODULE}_new.py").write_text(f"import {OLD_MODULE}\n")  # untracked
    return repo


def test_a_branch_is_renamed_like_main_and_a_second_run_changes_nothing(tmp_path, capsys):
    repo = branch(tmp_path)
    assert rename.main(["--root", str(repo)]) == 0
    assert "change(s)" in capsys.readouterr().out
    assert not (repo / "src" / OLD_MODULE).exists()
    part = (repo / "src/atlas/new_part.py").read_text()
    assert part == (
        "from atlas.storage import connect\n"
        'TITLE = "Atlas campaign"\n'
        'BASE = "/opt/proxy-index-campaign/venv/bin/atlas"\n'  # the old server's path stays
        'PROJECT_NAME = "Proxy Index"\n'  # the DigitalOcean project keeps its name
    )
    assert (repo / "src/atlas/schema.sql").read_text() == "-- seeded by proxy-index db-seed\n"
    assert (repo / "src/atlas/kept_already.py").read_text() == "VALUE = 1\n"
    assert (repo / "docs/page.md").read_text() == (
        "An Atlas page links /proxy-index/networks and reads ATLAS_DATABASE_FILE, the bucket "
        "proxy-index-archive.\n"
    )
    assert (repo / "tests/test_atlas_new.py").read_text() == "import atlas\n"
    names = [
        line for line in (repo / "uv.lock").read_text().splitlines() if line.startswith("name")
    ]
    assert names == ['name = "atlas"', 'name = "attrs"', 'name = "zipp"']
    staged = git(repo, "diff", "--cached", "--name-status", "-M").split("\n")
    assert f"R100\tsrc/{OLD_MODULE}/schema.sql\tsrc/atlas/schema.sql" in staged  # by git mv
    assert rename.main(["--root", str(repo), "--check"]) == 0
    capsys.readouterr()
    assert rename.main(["--root", str(repo)]) == 0
    assert capsys.readouterr().out.startswith("0 change(s)")


def test_the_check_reports_each_old_name_and_its_line(tmp_path, capsys):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs/a.md").write_text("fine\nrun proxy-index calculate\n/opt/proxy-index-worker\n")
    git(tmp_path, "init", "-q", str(repo))
    assert rename.main(["--root", str(repo), "--check"]) == 1
    assert capsys.readouterr().out == "docs/a.md:2: run proxy-index calculate\n"
    assert rename.main(["--root", str(repo), "--dry-run"]) == 0
    assert "edit docs/a.md" in capsys.readouterr().out
    assert (repo / "docs/a.md").read_text().startswith("fine\nrun proxy-index")  # unchanged
