"""The one pinned Camoufox browser release (D7), and its verified download.

The browser is not a Python package: `atlas browser fetch --dest D` downloads the build of
this release for the current platform, checks its size and SHA-256 before unpacking anything,
unpacks it into `D/<RELEASE>/` with no path traversal and moves it into place atomically. A
finished install carries a marker naming the release, platform and archive SHA-256; a second
fetch finds it and downloads nothing. Any mismatch is a refusal that leaves nothing behind.

Bumping the release is a reviewed PR that changes RELEASE, ENGINE_VERSION and every BUILDS row
(new URLs, sizes and SHA-256s); the camoufox package pin in pyproject.toml pairs with it.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path, PurePosixPath

from .. import legacy_names
from ..dbip.source import CHUNK, UpdateError, _open, private_file
from ..errors import UserError

RELEASE = "158.0.1-beta.41"
ENGINE = "Firefox"
ENGINE_VERSION = "158.0.1"
_BASE = f"https://github.com/daijro/camoufox/releases/download/v{RELEASE}/camoufox-{RELEASE}"
MARKER = ".atlas-release.json"
# The marker of a build installed before the rename (legacy_names), read when MARKER is absent.
MARKERS = (MARKER, legacy_names.RELEASE_MARKER)
MAX_UNPACKED_BYTES = 8 << 30
MAX_MEMBERS = 20_000


@dataclass(frozen=True, slots=True)
class Build:
    """One platform's archive of the release, and the browser binary inside it."""

    url: str
    sha256: str
    bytes: int
    executable: str


BUILDS = {
    "darwin-arm64": Build(
        f"{_BASE}-mac.arm64.zip",
        "9038f86d666c9780b6fff199d488737b8596c01f24e3e517845a0ecfb0cf8add",
        1_297_694_189,
        "Camoufox.app/Contents/MacOS/camoufox",
    ),
    "darwin-x86_64": Build(
        f"{_BASE}-mac.x86_64.zip",
        "1afd6caa1e82caa41a4f56d8519f673e36b0835700b1c9c904e78a84c5711f3a",
        1_305_712_546,
        "Camoufox.app/Contents/MacOS/camoufox",
    ),
    "linux-x86_64": Build(
        f"{_BASE}-lin.x86_64.zip",
        "3f2eb1d0451fa58e45e43e55f5e6181565069791ae60c0574f0ceb1f01341c33",
        1_303_577_033,
        "camoufox-bin",
    ),
}


class ReleaseError(UserError):
    """A refused fetch; the message names files and values, never a URL."""


def current_platform() -> str:
    """This machine's BUILDS key, such as `darwin-x86_64`."""
    machine = {"aarch64": "arm64", "amd64": "x86_64"}.get(platform.machine().lower())
    return f"{sys.platform}-{machine or platform.machine().lower()}"


def _build(name: str | None) -> tuple[str, Build]:
    name = name or current_platform()
    if name not in BUILDS:
        raise ReleaseError(f"no pinned Camoufox build for {name}; pinned: {', '.join(BUILDS)}")
    return name, BUILDS[name]


def _marker(name: str, build: Build) -> dict:
    return {"release": RELEASE, "platform": name, "sha256": build.sha256}


def _installed(target: Path, name: str, build: Build) -> bool:
    """True when `target` holds this release's build; a refusal when it holds anything else."""
    if not target.exists():
        return False
    found = None
    for marker in MARKERS:
        try:
            found = json.loads((target / marker).read_text())
            break
        except (OSError, ValueError):
            continue
    if found != _marker(name, build) or not (target / build.executable).is_file():
        raise ReleaseError(f"{target.name} exists but is not the verified {name} build")
    return True


def executable(dest: str | Path, platform_name: str | None = None) -> Path:
    """The browser binary of the verified install under `dest`."""
    name, build = _build(platform_name)
    target = Path(dest) / RELEASE
    if not _installed(target, name, build):
        raise ReleaseError(f"Camoufox {RELEASE} is not installed; run `atlas browser fetch`")
    return target / build.executable


def _copy(response, out, build: Build, step: str) -> tuple[int, str]:
    """Stream the body into `out`, stopping as soon as it passes the pinned size."""
    digest, size = hashlib.sha256(), 0
    while chunk := response.read(CHUNK):
        size += len(chunk)
        if size > build.bytes:
            raise ReleaseError(f"{step}: larger than the pinned {build.bytes} bytes")
        digest.update(chunk)
        out.write(chunk)
    return size, digest.hexdigest()


def _download(build: Build, partial: Path) -> None:
    step = f"download {build.url.rsplit('/', 1)[1]}"
    try:
        with _open(build.url, step) as response, private_file(partial) as out:
            size, sha256 = _copy(response, out, build, step)
    except UpdateError as exc:
        raise ReleaseError(str(exc)) from None
    except (OSError, HTTPException) as exc:
        raise ReleaseError(f"{step}: {type(exc).__name__}") from None
    if size != build.bytes:
        raise ReleaseError(f"{step}: {size} bytes, not the pinned {build.bytes}")
    if sha256 != build.sha256:
        raise ReleaseError(f"{step}: SHA-256 differs from the pinned one")


def _member_path(root: Path, info: zipfile.ZipInfo) -> Path:
    """Where a member goes: plain relative names of regular files or directories only."""
    path = PurePosixPath(info.filename)
    kind = stat.S_IFMT(info.external_attr >> 16)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in ("", ".", "..") for part in path.parts)
        or "\\" in info.filename
        or "\0" in info.filename
        or path.parts[0] in MARKERS
    ):
        raise ReleaseError(f"archive member {info.filename!r} is not a plain relative path")
    if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise ReleaseError(f"archive member {info.filename!r} is not a file or directory")
    return root.joinpath(*path.parts)


def _extract(bundle: zipfile.ZipFile, info: zipfile.ZipInfo, path: Path) -> None:
    """One member; a directory, or a new file with mode 0755 or 0644."""
    if info.is_dir():
        path.mkdir(parents=True, exist_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with bundle.open(info) as source, path.open("xb") as out:
        shutil.copyfileobj(source, out, CHUNK)
    path.chmod(0o755 if (info.external_attr >> 16) & 0o111 else 0o644)


def _extract_all(bundle: zipfile.ZipFile, root: Path) -> None:
    members = bundle.infolist()
    if len(members) > MAX_MEMBERS:
        raise ReleaseError("archive has too many members")
    total = 0
    for info in members:
        path = _member_path(root, info)
        total += 0 if info.is_dir() else info.file_size
        if total > MAX_UNPACKED_BYTES:
            raise ReleaseError("archive is larger than the unpack limit")
        _extract(bundle, info, path)


def _unpack(archive: Path, root: Path) -> None:
    try:
        with zipfile.ZipFile(archive) as bundle:
            _extract_all(bundle, root)
    except (zipfile.BadZipFile, EOFError, OSError, RuntimeError, NotImplementedError) as exc:
        raise ReleaseError(f"unreadable archive ({type(exc).__name__})") from None


def fetch(dest: str | Path, platform_name: str | None = None) -> dict:
    """Install the pinned build for this (or the given) platform under `dest`, once."""
    name, build = _build(platform_name)
    dest = Path(dest)
    target = dest / RELEASE
    result = {"release": RELEASE, "engine": ENGINE, "engine_version": ENGINE_VERSION}
    result |= {"platform": name, "executable": str(target / build.executable)}
    if _installed(target, name, build):
        return result | {"fetched": False}
    dest.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{RELEASE}-", dir=dest))
    partial = staging.parent / f"{staging.name}.zip"
    try:
        _download(build, partial)
        _unpack(partial, staging)
        if not (staging / build.executable).is_file():
            raise ReleaseError(f"archive has no {build.executable}")
        (staging / MARKER).write_text(json.dumps(_marker(name, build)))
        os.rename(staging, target)
    finally:
        partial.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
    return result | {"fetched": True}


def register(commands) -> None:
    parser = commands.add_parser("browser", help="Browser pulse tools (Camoufox)")
    actions = parser.add_subparsers(dest="browser_action", required=True)
    run = actions.add_parser("fetch", help=f"Download and verify the pinned Camoufox {RELEASE}")
    run.add_argument("--dest", type=Path, required=True, help=f"Installs into DEST/{RELEASE}/")
    run.add_argument("--platform", choices=sorted(BUILDS), help="Default: this machine")


def dispatch(args) -> int:
    print(json.dumps(fetch(args.dest.expanduser(), args.platform)))
    return 0
