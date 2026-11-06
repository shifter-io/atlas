"""Local onboarding simulations of the campaign manifest, its export and the CLI: no provider
traffic, database, orchestrator or website."""

import gzip
import json
import stat
import subprocess
from datetime import timedelta
from types import SimpleNamespace

import pytest
from onboarding_cases import ROOT, START, Clock, Proxy

from proxy_index.cli import main
from proxy_index.onboarding import cli as onboarding_cli
from proxy_index.onboarding import manifest as campaign
from proxy_index.onboarding.export import export_campaign
from proxy_index.onboarding.files import atomic_json
from proxy_index.onboarding.scheduler import RegionOptions, execute, new_state
from proxy_index.onboarding.writer import ObservationWriter, WriterBounds

pytest_plugins = ["onboarding_cases"]


def test_manifest_freezes_all_countries_in_existing_order(manifest):
    data = campaign.load(manifest)
    assert len(data["rounds"]) == 24
    assert data["rounds"][0] == "2027-01-01T22:00:00.000Z"
    assert data["rounds"][-1] == "2027-01-02T21:00:00.000Z"
    assert data["ends_at"] == "2027-01-02T22:00:00.000Z"
    mapping = campaign.load_regions()
    assert data["regions"] == {r.slug: list(r.countries) for r in mapping.regions}
    assert sum(map(len, data["regions"].values())) == 249
    assert data["settings"]["timeout_seconds"] == 10
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "start",
    [
        "2027-01-01T22:00:00",
        "2027-01-01T22:01:00Z",
        "2027-01-01T21:00:00Z",
        "2027-01-01T22:00:00+04:00",
    ],
)
def test_reject_invalid_start(tmp_path, start):
    with pytest.raises(ValueError):
        campaign.create(
            ROOT / "configs/shifter.toml",
            "residential",
            start,
            tmp_path,
            now=START - timedelta(minutes=30),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("concurrency", 0),
        ("concurrency", True),
        ("concurrency", 1.5),
        ("timeout_seconds", 0),
        ("timeout_seconds", float("nan")),
        ("timeout_seconds", float("inf")),
    ],
)
def test_invalid_settings(field, value):
    with pytest.raises(ValueError):
        campaign.settings(campaign.DEFAULTS | {field: value})


async def test_export_verifies_chunks_and_reports_missing_regions(manifest):
    data = campaign.load(manifest)
    state = new_state(data, "tor1", data["settings"])
    job = state["jobs"][0]
    job.update(status="completed", started_at=data["starts_at"], observations=2)
    directory = manifest.parent / "regions/tor1"
    atomic_json(directory / "state.json", state)
    folder = directory / "rounds/00/ca"
    writer = ObservationWriter(
        folder,
        country="CA",
        scheduled=data["starts_at"],
        started=data["starts_at"],
        deadline=START.timestamp() + 86400,
        bounds=WriterBounds(chunk_rows=1),
    )
    for _ in range(2):
        await writer.observe("203.0.113.1", START.timestamp() + 1)
    await writer.close()
    (folder / "observations-000003.jsonl.gz.partial").write_bytes(b"truncated")
    result = export_campaign(manifest)
    assert result["rows"] == 2 and result["chunks"] == 2 and not result["complete"]
    assert any("uncommitted" in issue for issue in result["issues"])
    assert any("fra1: missing" in issue for issue in result["issues"])
    assert len(list(gzip.open(manifest.parent / "export/observations.jsonl.gz", "rt"))) == 2
    assert (manifest.parent / "export/report.html").read_text().startswith("<!doctype html>")
    original_meta = next(folder.glob("*.meta.json"))
    duplicate_meta = folder / "observations-999999.jsonl.meta.json"
    duplicate_meta.write_bytes(original_meta.read_bytes())
    with pytest.raises(ValueError, match="sidecar"):
        export_campaign(manifest)
    duplicate_meta.unlink()
    chunk = next(folder.glob("*.gz"))
    chunk.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        export_campaign(manifest)


def test_cli_options_and_no_database_flags(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["onboarding", "run", "--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "--concurrency" in text and "--timeout" in text and "--resume" in text
    assert "--database" not in text


def test_runtime_git_and_package_exclusion():
    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=ROOT,
        input="onboarding-networks/shifter/residential/test/campaign.json\n",
        text=True,
        capture_output=True,
        check=True,
    )
    assert "onboarding-networks/" in result.stdout
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert (
        "onboarding-networks/**" in config["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    )


async def test_partial_country_is_exportable_after_crash_without_replay(manifest):
    data = campaign.load(manifest)
    state = new_state(data, "tor1", data["settings"])
    state["status"] = "running"
    state["jobs"][0].update(status="running", started_at=data["starts_at"])
    directory = manifest.parent / "regions/tor1"
    atomic_json(directory / "state.json", state)
    writer = ObservationWriter(
        directory / "rounds/00/ca",
        country="CA",
        scheduled=data["starts_at"],
        started=data["starts_at"],
        deadline=START.timestamp() + 86400,
    )
    await writer.observe("203.0.113.1", START.timestamp() + 1)
    await writer.close()
    await execute(
        manifest, "tor1", Proxy, RegionOptions(resume=True), clock=Clock(START.timestamp() + 86400)
    )
    result = export_campaign(manifest)
    assert result["rows"] == 1 and not result["complete"]
    assert result["regions"][0]["observations"] == 1


def test_export_rejects_swapped_regional_state(manifest):
    data = campaign.load(manifest)
    wrong = new_state(data, "fra1", data["settings"])
    atomic_json(manifest.parent / "regions/tor1/state.json", wrong)
    with pytest.raises(ValueError, match="Checkpoint"):
        export_campaign(manifest)


def test_cli_status_reads_one_regions_checked_state(manifest, capsys):
    data = campaign.load(manifest)
    state = new_state(data, "tor1", data["settings"])
    state["jobs"][0].update(status="completed", observations=3)
    atomic_json(manifest.parent / "regions/tor1/state.json", state)
    assert main(["onboarding", "status", "--manifest", str(manifest), "--region", "tor1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["region"] == "tor1" and report["campaign_id"] == data["campaign_id"]
    assert report["observations"] == 3 and report["completed_countries"] == 1
    assert main(["onboarding", "status", "--manifest", str(manifest), "--region", "nowhere"]) == 2
    status = SimpleNamespace(onboarding_action="status", manifest=manifest, region="nowhere")
    with pytest.raises(ValueError, match="Unknown campaign region"):
        onboarding_cli.dispatch(status)
    swapped = new_state(data, "fra1", data["settings"])
    atomic_json(manifest.parent / "regions/tor1/state.json", swapped)
    status.region = "tor1"
    with pytest.raises(ValueError, match="Checkpoint"):
        onboarding_cli.dispatch(status)


def test_ephemeral_manifest_records_rule_4(tmp_path):
    def plan(name, **options):
        return campaign.load(
            campaign.create(
                ROOT / "configs/shifter.toml",
                "residential",
                campaign.timestamp(START),
                tmp_path / name,
                now=START - timedelta(hours=1),
                **options,
            )
        )

    data = plan("rule-4", execution_mode="ephemeral")
    assert data["settings"]["answer_classes"] is True and data["census_rule_version"] == 4
    assert data["census_ratio_basis"] == "completed_requests_excluding_rate_limits"
    off = plan("off", execution_mode="ephemeral", answer_classes=False)
    assert "answer_classes" not in off["settings"] and off["census_rule_version"] == 2
    retry = plan("retry", execution_mode="ephemeral", retry_http_469=True)
    assert "answer_classes" not in retry["settings"] and retry["census_rule_version"] == 3
    parallel = plan("parallel")
    assert "answer_classes" not in parallel["settings"] and parallel["census_rule_version"] == 2
    # A version 3 manifest written before rule 4 still validates under rule 2.
    old = dict(
        data,
        settings={k: v for k, v in data["settings"].items() if k != "answer_classes"},
        census_rule_version=2,
        census_ratio_basis="completed_requests",
    )
    assert campaign.validate(old) is old
    with pytest.raises(ValueError, match="census rule"):
        campaign.validate(dict(data, census_rule_version=2))


def test_answer_classes_refused_outside_ephemeral_or_with_469(tmp_path):
    def plan(**options):
        return campaign.create(
            ROOT / "configs/shifter.toml",
            "residential",
            campaign.timestamp(START),
            tmp_path,
            now=START - timedelta(hours=1),
            **options,
        )

    for options, message in (
        ({"answer_classes": True}, "ephemeral"),
        (
            {
                "answer_classes": True,
                "execution_mode": "sequential",
                "region_order": ["lon1", "fra1", "ams3", "nyc3", "tor1", "sgp1", "blr1", "syd1"],
            },
            "ephemeral",
        ),
        (
            {"answer_classes": True, "retry_http_469": True, "execution_mode": "ephemeral"},
            "retry_http_469",
        ),
        ({"answer_classes": "yes", "execution_mode": "ephemeral"}, "boolean"),
        ({"run_settings": {"answer_classes": True}, "execution_mode": "ephemeral"}, "boolean"),
    ):
        with pytest.raises(ValueError, match=message):
            plan(**options)
    assert not list(tmp_path.iterdir())
    with pytest.raises(ValueError, match="retry_http_469"):
        campaign.settings(campaign.DEFAULTS | {"answer_classes": True, "retry_http_469": True})
    with pytest.raises(ValueError, match="boolean"):
        campaign.settings(campaign.DEFAULTS | {"answer_classes": 1})
    data = campaign.load(plan())
    data["settings"]["answer_classes"] = True
    data.update(
        census_rule_version=4, census_ratio_basis="completed_requests_excluding_rate_limits"
    )
    with pytest.raises(ValueError, match="ephemeral"):
        campaign.validate(data)
