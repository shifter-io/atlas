"""The engine of a browser run (browser/engines.py): the manifest's engine block (K-BP4 v2),
manifests of version 1 and 2, the try record's engine, a Chrome run's seal and the run's
samples; the pool's engines share one budget of browsers."""

from __future__ import annotations

import asyncio
import json
import tarfile

import pytest
from browser_contracts_cases import try_fields
from browser_rawfiles_cases import CAMOUFOX, MEMBERS, make_run, members, seal_dir
from test_browser_pool import Launcher

from proxy_index.browser import chrome, engines, rawfiles
from proxy_index.browser import contracts as bc
from proxy_index.browser.pool import BrowserPool

CHROME = {
    "name": "chrome",
    "version": "151.0.7",
    "user_agent": chrome.user_agent("151.0.7", "macos"),
}


def manifest_of(tmp_path, **summary):
    """The manifest a sealed run of make_run() carries (seal_dir: Camoufox)."""
    run_dir = make_run(tmp_path, **summary)
    batch, sha = rawfiles.run_facts(run_dir)
    summary = json.loads((run_dir / "summary.json").read_text())
    return rawfiles.build_manifest(run_dir, batch, summary, sha, CAMOUFOX)


# --- the engine block and the manifest versions -----------------------------------------------


def test_a_camoufox_manifest_is_version_2_with_both_blocks(tmp_path):
    manifest = manifest_of(tmp_path)
    assert manifest["version"] == bc.MANIFEST_VERSION == 2
    assert manifest["camoufox"] == CAMOUFOX
    assert manifest["engine"] == {
        "name": "camoufox",
        "version": CAMOUFOX["engine_version"],
        "user_agent": CAMOUFOX["user_agent"],
    }
    assert bc.manifest_engine(manifest) == "camoufox"


def test_a_version_1_manifest_still_validates_as_camoufox(tmp_path):
    manifest = manifest_of(tmp_path)
    v1 = {key: value for key, value in manifest.items() if key != "engine"} | {"version": 1}
    assert bc.check_manifest(v1) == v1 and bc.manifest_engine(v1) == "camoufox"
    with pytest.raises(ValueError, match=r"unknown fields \['engine'\]"):
        bc.check_manifest(v1 | {"engine": manifest["engine"]})
    with pytest.raises(ValueError, match=r"missing fields \['camoufox'\]"):
        bc.check_manifest({k: v for k, v in v1.items() if k != "camoufox"})


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda m: m.update(version=3), "not a version 1 or 2"),
        (lambda m: m.update(version=True), "not a version 1 or 2"),
        (lambda m: m.pop("engine"), r"missing fields \['engine'\]"),
        (lambda m: m["engine"].update(name="firefox"), "engine.name: must be one of"),
        (lambda m: m["engine"].update(version="x"), "engine.version: must be like"),
        (lambda m: m["engine"].update(user_agent="Mozilla/5.0"), "must claim the engine's major"),
        (lambda m: m["engine"].update(version="159.0"), "must claim the engine's major"),
        (lambda m: m["engine"].pop("user_agent"), r"engine: missing fields"),
        (lambda m: m.pop("camoufox"), "a Camoufox bundle has the block"),
        (lambda m: m["camoufox"].update(release="other"), None),
        (lambda m: m["camoufox"].update(engine_version="158.0.2"), "disagrees with the camoufox"),
        (lambda m: m.update(engine=dict(CHROME)), "a Chrome one has none"),
    ],
)
def test_an_engine_block_that_does_not_hold_is_refused(tmp_path, change, message):
    manifest = manifest_of(tmp_path)
    change(manifest)
    if message is None:  # the release is Camoufox's own: no engine block field
        assert bc.check_manifest(manifest)["camoufox"]["release"] == "other"
        return
    with pytest.raises(ValueError, match=message):
        bc.check_manifest(manifest)


def test_a_chrome_manifest_has_no_camoufox_block(tmp_path):
    manifest = manifest_of(tmp_path)
    del manifest["camoufox"]
    manifest["engine"] = dict(CHROME)
    assert bc.check_manifest(manifest)["engine"] == CHROME
    assert bc.manifest_engine(manifest) == "chrome"
    manifest["engine"]["user_agent"] = chrome.user_agent("150.0.1", "macos")
    with pytest.raises(ValueError, match="must claim the engine's major"):
        bc.check_manifest(manifest)


def test_a_try_record_may_name_its_engine():
    assert "engine" not in bc.try_record(**try_fields())
    assert bc.try_record(**try_fields(), engine="chrome")["engine"] == "chrome"
    with pytest.raises(ValueError, match="engine: must be one of"):
        bc.try_record(**try_fields(), engine="firefox")


# --- the seal of a Chrome run and the run's samples ------------------------------------------


def test_a_chrome_run_seals_with_its_engine_block_and_its_samples(tmp_path):
    run_dir = make_run(tmp_path)
    summary = json.loads((run_dir / "summary.json").read_text())
    summary["config"]["browser_engine"] = "chrome"
    (run_dir / "summary.json").write_text(json.dumps(summary))
    recorder = rawfiles.FileRecorder(*rawfiles.run_facts(run_dir))
    recorder.directory = run_dir / "browser"
    lines = [
        {"at": "t0", "processes": 2, "rss_bytes": 100, "cpu_seconds": 1.0, "cpu_percent": None},
        {"at": "t1", "processes": 3, "rss_bytes": 300, "cpu_seconds": 1.5, "cpu_percent": 50.0},
    ]
    recorder.samples(lines)
    batch, sha = rawfiles.run_facts(run_dir)
    # The run's engine must be the block's.
    with pytest.raises(ValueError, match="not the engine of the run's config"):
        rawfiles.seal(run_dir, batch, summary, sha, CAMOUFOX, outbox=tmp_path / "outbox")
    bundle = rawfiles.seal(
        run_dir, batch, summary, sha, None, outbox=tmp_path / "outbox", engine=dict(CHROME)
    )
    names = [member.name for member in members(bundle.archive)]
    assert MEMBERS.samples in names
    with tarfile.open(bundle.archive, "r:gz") as tar:
        manifest = json.load(tar.extractfile(MEMBERS.manifest))
        rows = [json.loads(line) for line in tar.extractfile(MEMBERS.samples).read().splitlines()]
    assert manifest["engine"] == CHROME and "camoufox" not in manifest
    assert MEMBERS.samples in {entry["name"] for entry in manifest["files"]}
    assert rows[:2] == lines
    assert rows[2]["summary"]["samples"] == 2
    assert rows[2]["summary"]["rss_bytes"] == {"average": 200, "peak": 300}
    assert rows[2]["summary"]["cpu_percent"]["peak"] == 50.0


def test_samples_before_bind_write_nothing_and_a_failed_write_loses_only_them(
    tmp_path, monkeypatch, caplog
):
    run_dir = make_run(tmp_path)
    unbound = rawfiles.FileRecorder(*rawfiles.run_facts(run_dir))
    unbound.samples([])  # no directory yet: nothing, no error
    recorder = rawfiles.FileRecorder(*rawfiles.run_facts(run_dir))
    recorder.directory = run_dir / "browser"
    recorder.samples([])
    assert json.loads((run_dir / MEMBERS.samples).read_text())["summary"]["samples"] == 0

    def full(path, data, mode="wb", sync=True):
        path.write_bytes(data[:3])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(rawfiles, "_write", full)
    recorder.samples([])  # a full disk: logged, the half file gone, the run goes on
    assert "samples not written (OSError)" in caplog.text
    assert not (run_dir / MEMBERS.samples).exists()


def test_a_camoufox_seal_is_unchanged_but_for_the_engine_block(tmp_path):
    run_dir = make_run(tmp_path)
    bundle = seal_dir(run_dir, tmp_path / "outbox")
    with tarfile.open(bundle.archive, "r:gz") as tar:
        manifest = json.load(tar.extractfile(MEMBERS.manifest))
    assert manifest["camoufox"] == CAMOUFOX and manifest["engine"]["name"] == "camoufox"
    assert engines.from_camoufox(CAMOUFOX) == manifest["engine"]


# --- one budget of browsers for every engine --------------------------------------------------


async def test_engines_share_the_pools_size_and_lease_first_in_first_out():
    camoufox, chrome_launch = Launcher(), Launcher()
    pool = BrowserPool(
        1, {"camoufox": camoufox, "chrome": chrome_launch}, recycle_after=100, memory=lambda: 1
    )
    fox, chromium = pool.engine("camoufox"), pool.engine("chrome")
    assert fox.refused_networks is pool.refused_networks
    order: list[str] = []

    async def run(engine_pool, name):
        for _ in range(2):
            async with engine_pool.browser() as browser:
                order.append(f"{name}{browser.number}")
                await asyncio.sleep(0)

    await asyncio.gather(run(fox, "f"), run(chromium, "c"))
    # One slot: each lease waits its turn whatever its engine, and each switch replaces the
    # other engine's process.
    assert order == ["f0", "c0", "f1", "c1"]
    stats = pool.stats()
    assert stats["launches"] == 4 and stats["switches"] == 3 and stats["leases"] == 4
    await pool.close()
    ends = ("recycles", "crashes", "switches", "closes")
    assert stats["launches"] == sum(pool.stats()[key] for key in ends)
    with pytest.raises(ValueError, match="no launch"):
        pool.engine("other")


async def test_a_lease_prefers_its_engines_process_then_an_empty_slot():
    camoufox, chrome_launch = Launcher(), Launcher()
    pool = BrowserPool(3, {"camoufox": camoufox, "chrome": chrome_launch}, recycle_after=100)
    async with pool.browser("camoufox"), pool.browser("chrome"):
        pass
    # Idle: a Camoufox, a Chrome and an empty slot.
    async with pool.browser("chrome") as browser:
        assert browser is chrome_launch.browsers[0]
    async with pool.browser("camoufox") as first, pool.browser("camoufox") as second:
        assert first is camoufox.browsers[0] and second is camoufox.browsers[1]
    assert pool.stats()["switches"] == 0
    await pool.close()
