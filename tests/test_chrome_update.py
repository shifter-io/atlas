"""scripts/chrome-update.sh (CHROME-CURRENT-1), the browser mini's Chrome update, against fake
curl, hdiutil, codesign, plutil, ditto, pgrep and sleep on PATH and a fake /Applications. It runs
under the Linux bash of CI; the script keeps to bash 3.2 (macOS)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "chrome-update.sh"
BASH = os.environ.get("MINI_WATCH_BASH", "bash")  # a bash 3.2 build checks the macOS shell
GOOGLE = "EQHXZ8M8AV"

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="GNU tools")

FAKE = r"""
import json, os, shutil, sys
from pathlib import Path
fake = Path(os.environ["FAKE"])
tool, args = Path(sys.argv[0]).name, sys.argv[1:]
with open(fake / "argv.jsonl", "a") as log:
    log.write(json.dumps([tool, *args]) + "\n")

def text(name, default=""):
    p = fake / name
    return p.read_text() if p.exists() else default

if tool == "curl":
    if (fake / "down").exists():
        sys.exit(22)
    Path(args[args.index("-o") + 1]).write_text("dmg")
elif tool == "hdiutil" and args[0] == "attach":
    info = Path(args[args.index("-mountpoint") + 1]) / "Google Chrome.app" / "Contents"
    info.mkdir(parents=True)
    (info / "Info.plist").write_text(text("offered", "158.0.8127.37"))
elif tool == "codesign":
    if args[0] == "--verify":
        sys.exit(1 if (fake / "badsig").exists() else 0)
    print(f"Identifier=com.google.Chrome\nTeamIdentifier={text('team', 'EQHXZ8M8AV')}",
          file=sys.stderr)
elif tool == "plutil":
    print(Path(args[-1]).read_text())
elif tool == "ditto":
    shutil.copytree(args[0], args[1])
elif tool == "pgrep":
    left = int(text("busy", "0"))
    if left:
        (fake / "busy").write_text(str(left - 1))
    sys.exit(0 if left else 1)
"""


class Mini:
    def __init__(self, tmp: Path) -> None:
        self.fake, self.bin, self.tmp = tmp / "fake", tmp / "bin", tmp / "tmp"
        self.apps = tmp / "Applications"
        for d in (self.fake, self.bin, self.tmp, self.apps):
            d.mkdir()
        for tool in ("curl", "hdiutil", "codesign", "plutil", "ditto", "pgrep", "sleep"):
            path = self.bin / tool
            path.write_text(f"#!{sys.executable}\n{FAKE}")
            path.chmod(0o755)
        self.app = self.apps / "Google Chrome.app"

    def install(self, version: str) -> None:
        (self.app / "Contents").mkdir(parents=True)
        (self.app / "Contents" / "Info.plist").write_text(version)
        (self.app / "Contents" / "old-only").write_text("x")

    def version(self) -> str | None:
        plist = self.app / "Contents" / "Info.plist"
        return plist.read_text() if plist.exists() else None

    def set(self, name: str, value: str = "") -> None:
        (self.fake / name).write_text(value)

    def run(self, *args: str) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "FAKE": str(self.fake),
            "TMPDIR": str(self.tmp),
        }
        return subprocess.run(
            [BASH, str(SCRIPT), "--app", str(self.app), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def calls(self, tool: str) -> list[list[str]]:
        log = self.fake / "argv.jsonl"
        lines = log.read_text().splitlines() if log.exists() else []
        return [call[1:] for call in map(json.loads, lines) if call[0] == tool]

    def leftovers(self) -> list[str]:
        """Anything but the app in /Applications or the temporary folder: nothing after a run."""
        names = [p.name for p in self.apps.iterdir() if p != self.app]
        return names + [p.name for p in self.tmp.iterdir()]


@pytest.fixture
def mini(tmp_path):
    return Mini(tmp_path)


def test_an_older_chrome_is_replaced_by_the_signed_current_stable(mini):
    mini.install("157.0.8103.79")
    result = mini.run()
    assert result.returncode == 0, result.stderr
    assert "updated Chrome 157.0.8103.79 to 158.0.8127.37" in result.stdout
    assert mini.version() == "158.0.8127.37"
    assert not (mini.app / "Contents" / "old-only").exists()  # the whole app is the new one
    assert mini.leftovers() == []
    assert mini.calls("hdiutil")[-1][:2] == ["detach", "-quiet"]
    assert mini.calls("curl")[0][-1].startswith("https://dl.google.com/chrome/mac/")


def test_a_missing_chrome_is_installed(mini):
    result = mini.run()
    assert result.returncode == 0, result.stderr
    assert "updated Chrome none to 158.0.8127.37" in result.stdout
    assert mini.version() == "158.0.8127.37"


@pytest.mark.parametrize("version", ["158.0.8127.37", "158.0.8127.40", "159.0.8154.1"])
def test_a_current_or_newer_chrome_is_left_alone(mini, version):
    mini.install(version)
    result = mini.run()
    assert result.returncode == 0, result.stderr
    assert f"current: Chrome {version} (Stable offers 158.0.8127.37)" in result.stdout
    assert mini.version() == version and (mini.app / "Contents" / "old-only").exists()
    assert mini.calls("ditto") == [] and mini.leftovers() == []


def test_a_dry_run_changes_nothing(mini):
    mini.install("157.0.8103.79")
    result = mini.run("--dry-run")
    assert result.returncode == 0, result.stderr
    assert "would update Chrome 157.0.8103.79 to 158.0.8127.37" in result.stdout
    assert mini.version() == "157.0.8103.79" and mini.calls("ditto") == []


@pytest.mark.parametrize(
    ("flag", "value", "error"),
    [
        ("badsig", "", "fails its signature check"),
        ("team", "ABCDE12345", "not signed by Google (team ABCDE12345)"),
        ("down", "", "download failed"),
        ("offered", "", "has no version"),
    ],
)
def test_an_installer_that_fails_a_check_changes_nothing(mini, flag, value, error):
    mini.install("157.0.8103.79")
    mini.set(flag, value)
    result = mini.run()
    assert result.returncode == 1
    assert error in result.stderr
    assert mini.version() == "157.0.8103.79" and mini.leftovers() == []


def test_it_waits_for_a_gap_between_chromes_and_gives_up_after_wait(mini):
    mini.install("157.0.8103.79")
    mini.set("busy", "2")  # two checks see a Chrome running, the third none
    result = mini.run("--wait", "10")
    assert result.returncode == 0, result.stderr
    assert mini.version() == "158.0.8127.37" and mini.calls("sleep") == [["2"], ["2"]]

    mini.set("busy", "99")
    mini.set("offered", "159.0.8154.1")
    result = mini.run("--wait", "4")
    assert result.returncode == 3
    assert "busy: Chrome ran for 4 s; Chrome 158.0.8127.37 kept, update later" in result.stdout
    assert mini.version() == "158.0.8127.37" and mini.leftovers() == []


@pytest.mark.parametrize("args", [("--wait", "soon"), ("--color",), ("--app",)])
def test_bad_arguments_are_refused(mini, args):
    result = mini.run(*args)
    assert result.returncode != 0 and mini.calls("curl") == []
