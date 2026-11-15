"""IMAGE-AUTO-1: droplets get the wheel of the code the manager runs, at launch.

`wheels/current.json` (written by scripts/server-setup.sh) names the current wheel; the daily
hour ships it instead of its template's and records it; the worker's install refuses a wheel
whose dependencies the image lacks. Offline: fake manager folders, a stub `uv`, no cloud.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from daily_cases import NOW, inputs, template

from atlas.campaign import fanout
from atlas.campaign.collector import Pacing
from atlas.campaign.contracts import spec_path
from atlas.campaign.current_wheel import CurrentWheelError, load
from atlas.campaign.launch_steps import WorkerInputs
from atlas.campaign.tick import save_hour
from atlas.daily.runner import run_hour

COMMIT = "c0ffee" + "0" * 34
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "server-setup.sh"


def current(wheels: Path, data: bytes = b"current wheel", **changes) -> tuple[str, str]:
    """Write a wheel under `<sha12>/` and the current.json naming it; returns (sha, name)."""
    sha = hashlib.sha256(data).hexdigest()
    name = f"{sha[:12]}/atlas-2.0-py3-none-any.whl"
    (wheels / sha[:12]).mkdir(parents=True, exist_ok=True)
    (wheels / name).write_bytes(data)
    raw = {"commit": COMMIT, "wheel": name, "sha256": sha, "requirements_sha256": "0" * 64}
    (wheels / "current.json").write_text(json.dumps(raw | changes))
    return sha, name


def test_no_file_means_no_current_wheel(tmp_path):
    assert load(tmp_path) is None


def test_the_current_wheel_is_named_checked_and_recorded(tmp_path):
    sha, name = current(tmp_path)
    found = load(tmp_path)
    assert found.wheel == tmp_path / name and found.version == "2.0"
    assert found.record() == {
        "commit": COMMIT,
        "wheel": "atlas-2.0-py3-none-any.whl",
        "wheel_sha256": sha,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"commit": "main"},
        {"wheel": "../atlas-2.0-py3-none-any.whl"},
        {"wheel": "0123456789ab/atlas-2.0-py3-none-any.whl"},  # not the sha's folder
        {"sha256": None},
    ],
)
def test_a_file_naming_no_valid_wheel_refuses(tmp_path, changes):
    current(tmp_path, **changes)
    with pytest.raises(CurrentWheelError, match="names no valid"):
        load(tmp_path)


def test_a_wheel_changed_after_the_setup_refuses(tmp_path):
    _, name = current(tmp_path)
    (tmp_path / name).write_bytes(b"other code")
    with pytest.raises(CurrentWheelError, match="SHA-256 differs"):
        load(tmp_path)


def test_an_unreadable_file_refuses_without_its_content(tmp_path):
    (tmp_path / "current.json").write_text("{not json")
    with pytest.raises(CurrentWheelError, match="unreadable") as caught:
        load(tmp_path)
    assert "not json" not in str(caught.value)


def test_a_launch_finds_the_wheel_in_its_sha_folder(tmp_path):
    sha, name = current(tmp_path / "wheels")
    root = tmp_path / "campaigns" / "c1"
    assert WorkerInputs.for_root(root, sha).wheel == tmp_path / "wheels" / name


async def test_the_daily_hour_ships_and_records_the_current_wheel(tmp_path):
    root = template(tmp_path)
    sha, name = current(root.parent.parent / "wheels")
    seen = {}

    async def finished(spec, record, hour_root):
        seen["spec"] = json.loads(spec_path(hour_root).read_text())
        seen["wheel"] = WorkerInputs.for_root(hour_root, spec.wheel_sha256).wheel
        record.state, record.finished_at = "done", NOW.isoformat()
        save_hour(hour_root, record)

    def services(hour_root, spec):
        return None, None, finished, None

    pacing = Pacing(interval=0, clock_synced=lambda: True)
    outcome = await run_hour(
        inputs(tmp_path, root, GB=22), services=services, now=NOW, pacing=pacing
    )
    assert outcome["state"] == "done"
    assert outcome["code"] == {
        "commit": COMMIT,
        "wheel": "atlas-2.0-py3-none-any.whl",
        "wheel_sha256": sha,
    }
    assert seen["spec"]["wheel_sha256"] == sha and seen["spec"]["framework_version"] == "2.0"
    assert seen["wheel"] == root.parent.parent / "wheels" / name


async def test_without_a_current_wheel_the_hour_keeps_the_template_wheel(tmp_path):
    root = template(tmp_path)

    async def finished(spec, record, hour_root):
        record.state, record.finished_at = "done", NOW.isoformat()
        save_hour(hour_root, record)

    pacing = Pacing(interval=0, clock_synced=lambda: True)
    outcome = await run_hour(
        inputs(tmp_path, root, GB=22),
        services=lambda hour_root, spec: (None, None, finished, None),
        now=NOW,
        pacing=pacing,
    )
    template_sha = json.loads(spec_path(root).read_text())["wheel_sha256"]
    assert outcome["code"] == {"wheel_sha256": template_sha}


def install(tmp_path: Path, check_exit: int) -> subprocess.CompletedProcess:
    """INSTALL_WHEEL with a stub `uv` whose `pip check` exits `check_exit`."""
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "uv").write_text(
        '#!/bin/sh\necho "$@" >> "$(dirname "$0")/calls"\n'
        f'[ "$2" = check ] && exit {check_exit}\nexit 0\n'
    )
    (stub / "uv").chmod(0o755)
    root = tmp_path / "worker"
    (root / "wheel").mkdir(parents=True)
    (root / "wheel" / "atlas-2.0-py3-none-any.whl").write_bytes(b"wheel")
    script = (
        "import hashlib, pathlib, subprocess, sys\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "wheel = root / 'wheel' / 'atlas-2.0-py3-none-any.whl'\n" + fanout.INSTALL_WHEEL
    )
    env = os.environ | {"PATH": f"{stub}:{os.environ['PATH']}"}
    return subprocess.run(
        [sys.executable, "-c", script, str(root)], capture_output=True, text=True, env=env
    )


def test_the_install_checks_the_image_dependencies(tmp_path):
    reply = install(tmp_path, 0)
    assert reply.returncode == 0
    calls = (tmp_path / "bin" / "calls").read_text().splitlines()
    assert [c.split()[:2] for c in calls] == [["pip", "install"], ["pip", "check"]]
    assert (tmp_path / "worker" / "installed-wheel.sha256").exists()


def test_a_wheel_the_image_dependencies_do_not_fit_never_starts(tmp_path):
    reply = install(tmp_path, 1)
    assert reply.returncode == 1
    assert "rebuild the image" in reply.stderr
    assert not (tmp_path / "worker" / "installed-wheel.sha256").exists()  # retried next push


def function(name: str) -> str:
    """One shell function of server-setup.sh, by name."""
    return re.search(rf"^{name}\(\) {{\n.*?^}}\n", SCRIPT.read_text(), re.M | re.S).group(0)


def worker_wheel(tmp_path: Path) -> subprocess.CompletedProcess:
    """server-setup.sh's worker_wheel step for real, with a stub uv that writes a wheel."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    if not (src / ".git").exists():
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(src)]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "c"], check=True)
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    (stub / "uv").write_text(
        '#!/bin/sh\necho "$1" >> "$(dirname "$0")/calls"\n'
        'if [ "$1" = build ]; then for a; do [ -n "$o" ] && d=$a && o=; '
        '[ "$a" = --out-dir ] && o=1; done; echo wheel > "$d/atlas-2.0-py3-none-any.whl"; '
        "else echo 'pinned==1.0'; fi\n"
    )
    (stub / "uv").chmod(0o755)
    (tmp_path / "work").mkdir(exist_ok=True)
    text = SCRIPT.read_text()
    helpers = re.findall(r"^(?:die|say|plan)\(\) \{.*\}$", text, re.M)
    setup = (
        "set -euo pipefail\n"
        + "\n".join(helpers)
        + "\n"
        + function("put")
        + function("worker_wheel")
        + f"DRY=false ROOT={tmp_path} ENV=production COMMIT={COMMIT} SRC={src}"
        + f" WORK={tmp_path / 'work'} PYTHON_VERSION=3.12\nworker_wheel\n"
    )
    env = os.environ | {"PATH": f"{stub}:{os.environ['PATH']}"}
    return subprocess.run(["bash", "-c", setup], capture_output=True, text=True, env=env)


def test_the_server_setup_builds_the_commit_wheel_once(tmp_path):
    first = worker_wheel(tmp_path)
    assert first.returncode == 0, first.stderr
    found = load(tmp_path / "production" / "wheels")
    assert found.commit == COMMIT and found.wheel.read_bytes() == b"wheel\n"
    raw = json.loads((tmp_path / "production" / "wheels" / "current.json").read_text())
    assert raw["requirements_sha256"] == hashlib.sha256(b"pinned==1.0\n").hexdigest()
    # IMAGE-DAILY-1: the requirements beside the wheel, for the daily image rebuild.
    assert (found.wheel.parent / "requirements.txt").read_text() == "pinned==1.0\n"
    again = worker_wheel(tmp_path)
    assert again.returncode == 0, again.stderr
    assert (tmp_path / "bin" / "calls").read_text().split() == ["build", "export"]
