"""Both schedules share the same census, with disjoint regional deadlines."""

from copy import deepcopy
from datetime import timedelta

import pytest
from test_onboarding import ROOT, START, Clock, Proxy, fake_measurement

from proxy_index.onboarding import manifest as campaign
from proxy_index.onboarding.export import export_campaign
from proxy_index.onboarding.files import atomic_json, read_json
from proxy_index.onboarding.scheduler import execute, new_state, validate_state

ORDER = ["nyc3", "tor1", "lon1", "fra1", "ams3", "sgp1", "blr1", "syd1"]


def make(tmp_path, **kwargs):
    return campaign.create(
        ROOT / "configs/shifter.toml",
        "residential",
        campaign.timestamp(START),
        tmp_path,
        now=START - timedelta(hours=1),
        **kwargs,
    )


def test_parallel_remains_legacy_and_same_window(tmp_path):
    m = campaign.load(make(tmp_path))
    assert m["schema_version"] == 1 and m["census_rule_version"] == 2
    assert "retry_http_469" not in m["settings"]
    windows = [campaign.regional_window(m, r) for r in ORDER]
    assert all(w == windows[0] for w in windows)
    assert windows[0]["rounds"] == m["rounds"]
    assert windows[0]["ends_at"] == m["ends_at"]


async def test_all_eight_days_hourly_no_overlap_retry_and_complete_export(tmp_path):
    path = make(
        tmp_path,
        execution_mode="sequential",
        region_order=ORDER,
        retry_http_469=True,
        target_url="http://example.test/",
        run_settings={"concurrency": 1500, "processes": 4},
    )
    m = campaign.load(path)
    assert m["schema_version"] == 2 and m["census_rule_version"] == 3
    assert campaign.moment(m["ends_at"]) - START == timedelta(days=8)
    all_calls = []
    for day, region in enumerate(ORDER):
        # Every server can be armed together; each must wait until its own day.
        clock = Clock((START - timedelta(hours=1)).timestamp())
        window = campaign.regional_window(m, region)
        calls = []

        async def run(config, sink, clock=clock, window=window, calls=calls, **kw):
            now = clock.now()
            assert config.retry_http_469 and config.concurrency == 1500 and config.processes == 4
            assert config.target_url == "http://example.test/"
            assert campaign.moment(window["starts_at"]).timestamp() <= now < kw["deadline"]
            assert kw["deadline"] == campaign.moment(window["ends_at"]).timestamp()
            calls.append((config.proxy.country, now))
            return fake_measurement(completed=0)

        result = await execute(path, region, Proxy, clock=clock, run_country=run)
        assert result["status"] == "closed" and result["unfinished_countries"] == 0
        assert len(calls) == 24 * len(m["regions"][region])
        size = len(m["regions"][region])
        for hour in range(24):
            group = calls[hour * size : (hour + 1) * size]
            assert [c for c, _ in group] == [c.lower() for c in m["regions"][region]]
            assert all(t == (START + timedelta(days=day, hours=hour)).timestamp() for _, t in group)
        assert clock.now() == (START + timedelta(days=day + 1)).timestamp()
        all_calls.extend(calls)
    assert len(all_calls) == 5976
    report = export_campaign(path)
    assert report["complete"] and len(report["regions"]) == 8 and not report["issues"]


@pytest.mark.parametrize(
    "change",
    [
        "order_missing",
        "order_duplicate",
        "short_end",
        "legacy_schema",
        "wrong_rule",
        "wrong_basis",
        "bad_retry",
        "bad_target",
    ],
)
def test_reject_ambiguous_or_inconsistent_schedules(tmp_path, change):
    path = make(tmp_path, execution_mode="sequential", region_order=ORDER, retry_http_469=True)
    m = read_json(path)
    if change == "order_missing":
        del m["region_order"]
    elif change == "order_duplicate":
        m["region_order"][-1] = m["region_order"][0]
    elif change == "short_end":
        m["ends_at"] = campaign.timestamp(START + timedelta(days=1))
    elif change == "legacy_schema":
        m["schema_version"] = 1
    elif change == "wrong_rule":
        m["census_rule_version"] = 2
    elif change == "wrong_basis":
        m["census_ratio_basis"] = "completed_requests"
    elif change == "bad_retry":
        m["settings"]["retry_http_469"] = 1
    else:
        m["target_url"] = "https://example.test/"
    atomic_json(path, m)
    with pytest.raises(ValueError):
        campaign.load(path)


async def test_finished_day_never_spills_into_next_region(tmp_path):
    path = make(tmp_path, execution_mode="sequential", region_order=ORDER)
    clock = Clock((START + timedelta(days=1)).timestamp())

    async def must_not_run(*args, **kwargs):
        pytest.fail("An expired region issued requests")

    result = await execute(path, "nyc3", Proxy, clock=clock, run_country=must_not_run)
    assert result["status"] == "closed" and result["completed_countries"] == 0
    assert result["unfinished_countries"] == 24 * len(campaign.load(path)["regions"]["nyc3"])


def test_resume_rejects_wrong_regional_deadline(tmp_path):
    m = campaign.load(make(tmp_path, execution_mode="sequential", region_order=ORDER))
    s = new_state(m, "nyc3", m["settings"])
    validate_state(s, m, "nyc3")
    wrong = deepcopy(s)
    wrong["ends_at"] = m["ends_at"]
    with pytest.raises(ValueError):
        validate_state(wrong, m, "nyc3")


def test_cli_freezes_sequential_policy_and_settings(tmp_path, capsys):
    from proxy_index.cli import main

    assert (
        main(
            [
                "onboarding",
                "plan",
                "--provider-config",
                str(ROOT / "configs/shifter.toml"),
                "--product",
                "residential",
                "--start",
                campaign.timestamp(START),
                "--output",
                str(tmp_path),
                "--execution-mode",
                "sequential",
                "--region-order",
                *ORDER,
                "--retry-http-469",
                "--concurrency",
                "1500",
                "--processes",
                "4",
                "--target-url",
                "http://example.test/",
            ]
        )
        == 0
    )
    from pathlib import Path

    m = campaign.load(Path(capsys.readouterr().out.strip()))
    assert m["region_order"] == ORDER and m["settings"]["concurrency"] == 1500
    assert m["settings"]["retry_http_469"] and m["settings"]["processes"] == 4
