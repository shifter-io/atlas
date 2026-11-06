"""CONC-1: `campaign probe` steps and verdicts, and the per-network concurrency cap.

The probe runs against the in-process fake DigitalOcean and a fake remote that answers each
step with a run summary; no request or SSH connection leaves the machine.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
from campaign_fakes import FakeDigitalOcean
from campaign_hour_cases import NETWORKS, REGIONS, launched, make_root, make_spec
from campaign_probe_cases import (
    IP,
    PROBED,
    FakeProbeRemote,
    Recording,
    classified,
    probed,
    summary,
    world,
)

from proxy_index import cli as main_cli
from proxy_index.campaign import cli as campaign_cli
from proxy_index.campaign import probe as probing
from proxy_index.campaign.contracts import WORKER_TAG, audit_path
from proxy_index.campaign.fanout import RemoteError
from proxy_index.campaign.probe import ProbeError, ProbeInputs, reasons, step_result
from proxy_index.providers.grammar import Grammar, load_grammar


def test_step_result_classifies_every_failure_as_a_share_of_completed_requests():
    found = step_result(
        2000,
        25_000,
        summary(
            20_000,
            statuses={"429": 400, "469": 100, "502": 50, "503": 30, "504": 20},
            errors={"proxy_connect": 300, "connection": 100, "timeout": 200, "tls": 40},
        ),
    )
    assert found == {
        "concurrency": 2000,
        "requested": 25_000,
        "completed": 20_000,
        "success_pct": 93.8,
        "rate_limited_pct": 2.5,
        "gateway_5xx_pct": 0.5,
        "proxy_connect_pct": 2.0,
        "timeout_pct": 1.0,
        "other_pct": 0.2,
        "rps": 900.0,
        "latency_p50_ms": 820,
        "latency_p95_ms": 2400,
        "unique_ips": 24_000,
        "stop_reason": None,
        # CTRY-1 keys; this summary has no answer classes, so nothing is answered away.
        "answered": 20_000,
        "no_inventory_pct": 0.0,
        "other_country_pct": None,
        "duplicates_pct": 0.0,
        "real_error_pct": 3.7,
    }
    assert not IP.search(json.dumps(found))
    empty = step_result(500, 25_000, summary(0, successes=0) | {"latency_ms": None})
    assert empty["success_pct"] == 0.0 and empty["latency_p50_ms"] is None


def test_step_result_answer_classes():
    """CTRY-1: rate limits come from the runner's signals (the CONNECT refusals among them
    leave proxy_connect), no-IP answers leave other, and real errors are of answered."""
    found = step_result(500, 1000, classified())
    assert {
        k: found[k]
        for k in (
            "completed",
            "answered",
            "success_pct",
            "rate_limited_pct",
            "gateway_5xx_pct",
            "proxy_connect_pct",
            "timeout_pct",
            "no_inventory_pct",
            "other_pct",
            "other_country_pct",
            "duplicates_pct",
            "real_error_pct",
        )
    } == {
        "completed": 1000,
        "answered": 950,
        "success_pct": 80.0,
        "rate_limited_pct": 5.0,
        "gateway_5xx_pct": 1.0,
        "proxy_connect_pct": 2.5,
        "timeout_pct": 2.0,
        "no_inventory_pct": 0.5,
        "other_pct": 9.0,
        "other_country_pct": 1.0,
        "duplicates_pct": 9.0,
        "real_error_pct": 15.263,
    }
    unchecked = step_result(500, 1000, classified(other_country=None, unique_ips=None))
    assert (unchecked["other_country_pct"], unchecked["duplicates_pct"]) == (None, None)
    assert step_result(500, 0, classified(completed=0, rate_limited={}))["real_error_pct"] == 0.0


def test_step_result_unchanged_for_old_summaries():
    """A CONC-1-era summary (no answer classes) keeps every share it had before CTRY-1."""
    from proxy_index.models import RunSummary

    old = RunSummary(
        run_id="r",
        schema_version="1",
        framework_version="x",
        target={},
        config={},
        started_at="",
        finished_at="",
        duration_seconds=10.0,
        status="completed",
        requested=1000,
        started=1000,
        completed=1000,
        cancelled=0,
        not_started=0,
        successes=900,
        total_errors=100,
        errors={"http_status": 40, "proxy_connect": 25, "timeout": 20, "tls": 15},
        status_codes={"200": 900, "429": 30, "503": 10},
        success_rate=0.9,
        requests_per_second=100.0,
        unique_ips=850,
        pool_eligible=True,
        latency_ms={"p50": 700, "p90": 1500, "p95": 1900, "p99": 3000},
        latency_histogram={},
        response_body_bytes=0,
        buckets=[],
    ).to_dict()
    assert step_result(1000, 1000, old) == {
        "concurrency": 1000,
        "requested": 1000,
        "completed": 1000,
        "success_pct": 90.0,
        "rate_limited_pct": 3.0,
        "gateway_5xx_pct": 1.0,
        "proxy_connect_pct": 2.5,
        "timeout_pct": 2.0,
        "other_pct": 1.5,
        "rps": 100.0,
        "latency_p50_ms": 700,
        "latency_p95_ms": 1900,
        "unique_ips": 850,
        "stop_reason": None,
        "answered": 1000,
        "no_inventory_pct": 0.0,
        "other_country_pct": None,
        "duplicates_pct": 5.0,
        "real_error_pct": 7.0,
    }


def test_incomplete_counts_answered_requests():
    """The goal is answered requests: 24,000 completed with 400 rate-limited is 23,600
    answered, below 95% of 25,000; without answer classes it is complete."""
    base = step_result(500, 25_000, summary())
    short = summary(24_000, statuses={"429": 400}) | {"rate_limited": {"429": 400}}
    assert reasons(step_result(2000, 25_000, short), base) == ["incomplete"]
    assert reasons(step_result(2000, 25_000, summary(24_000, statuses={"429": 400})), base) == []
    enough = summary(25_400, statuses={"429": 400}) | {"rate_limited": {"429": 400}}
    assert reasons(step_result(2000, 25_000, enough), base) == []


def test_run_command_counts_answered_requests():
    argv = probing.run_command("geonode", 500, 25_000, "US", "nyc3")
    timeout = argv.index("--timeout")
    assert argv[timeout + 1 : timeout + 3] == [str(probing.TIMEOUT_SECONDS), "--answer-classes"]
    assert argv[argv.index("--provider-config") + 1].endswith("/probe/geonode.toml")


@pytest.mark.parametrize("count", [0, 1, 2])
def test_a_step_adds_the_country_check_only_with_one_image_database(tmp_path, count):
    """CTRY-2: `--mmdb` is appended when the image holds exactly one MMDB."""
    import subprocess

    databases = tmp_path / "db-ip"
    databases.mkdir()
    for index in range(count):
        (databases / f"dbip-{index}.mmdb").write_bytes(b"x")
    reply = subprocess.run(
        ["sh", "-c", probing.launcher(str(databases)), "printf", "%s\n", "run", "--answer-classes"],
        capture_output=True,
        text=True,
        check=True,
    )
    extra = ["--mmdb", str(databases / "dbip-0.mmdb")] if count == 1 else []
    assert reply.stdout.splitlines() == ["run", "--answer-classes", *extra]
    assert probing.run_command("soax", 500, 25_000, "US", "nyc3")[2] == probing.launcher(
        "/opt/proxy-index-worker/db-ip"
    )


def test_the_step_down_rule():
    base = step_result(500, 25_000, summary(errors={"timeout": 100}))  # 0.4% timeouts
    assert reasons(base, base) == []
    assert reasons({"run_failed": "RemoteError"}, base) == ["run_failed"]
    cases = {
        "rate_limited": summary(statuses={"429": 501}),  # 2.004%
        "proxy_connect": summary(errors={"proxy_connect": 400, "connection": 101}),
        "incomplete": summary(23_000),
        # 0.4% at the floor: up to max(0.8, 1.4) = 1.4% is noise, above it a slowdown.
        "timeouts_5xx": summary(statuses={"504": 100}, errors={"timeout": 276}),
    }
    for reason, run in cases.items():
        assert reasons(step_result(2000, 25_000, run), base) == [reason]
    assert reasons(step_result(2000, 25_000, summary(statuses={"429": 500})), base) == []
    assert (
        reasons(
            step_result(2000, 25_000, summary(statuses={"504": 100}, errors={"timeout": 250})), base
        )
        == []
    )
    high = step_result(500, 25_000, summary(errors={"timeout": 2500}))  # 10% at the floor
    assert reasons(step_result(2000, 25_000, summary(errors={"timeout": 3250})), high) == []
    assert reasons(step_result(2000, 25_000, summary(errors={"timeout": 5001})), high) == [
        "timeouts_5xx",
        "success_drop",
    ]


def test_s2_every_failure_class_and_a_success_drop_count():
    """S2: 407s, DNS, TLS, protocol errors, other statuses and blocks can no longer pass."""
    base = step_result(500, 25_000, summary(errors={"tls": 50}))  # 0.2% other at the floor
    cases = {
        # The reviewer's case: 30% 407, 4% DNS, 4% protocol errors.
        ("proxy_connect", "other_errors", "success_drop"): summary(
            errors={"proxy_auth": 7500, "dns": 1000, "protocol": 1000}
        ),
        ("proxy_connect",): summary(errors={"proxy_auth": 501}),  # a 407 storm
        ("other_errors",): summary(errors={"protocol": 150, "http_status": 151}),  # 1.204%
        ("other_errors", "success_drop"): summary(errors={"blocked": 450, "challenge": 450}),
        # Every class under its own limit, yet 5.5 points less success than the baseline.
        ("success_drop",): summary(
            statuses={"429": 475}, errors={"proxy_connect": 475, "timeout": 240, "tls": 225}
        ),
    }
    for expected, run in cases.items():
        assert reasons(step_result(2000, 25_000, run), base) == list(expected), expected
    assert reasons(step_result(2000, 25_000, summary(errors={"tls": 299})), base) == []
    assert (
        step_result(2000, 25_000, summary(errors={"proxy_auth": 5, "dns": 5}))["proxy_connect_pct"]
        == 0.04
    )


def test_the_ladder_steps_down_to_just_above_the_floor(tmp_path):
    inputs = ProbeInputs(tmp_path, tmp_path)
    assert inputs.ladder() == [2000, 1500, 1000]
    assert replace(inputs, start=1200, step=300).ladder() == [1200, 900, 600]
    assert replace(inputs, start=500).ladder() == []


async def test_the_probe_finds_each_networks_max_and_removes_its_worker(tmp_path):
    fake = Recording()
    fake.seed_production(60)
    production = set(fake.droplets)
    inputs, spec = world(tmp_path)
    remote = FakeProbeRemote(
        {
            ("soax", 2000): summary(statuses={"429": 1250}),
            ("soax", 1500): summary(errors={"proxy_connect": 600}),
        }
    )
    result = await probed(fake, remote, inputs)

    [create] = fake.creates
    sid = result["probe_id"].split("-")[1][:8]
    assert create["names"] == [f"pic-{sid}-h00-nyc3-probe"] and create["size"] == "c-4"
    assert create["image"] == "snap-nyc3" and create["ssh_keys"] == ["4242"]
    assert create["tags"] == [WORKER_TAG, f"pi-camp-{sid}", f"pi-camp-{sid}-h00"]
    assert not (create["monitoring"] or create["with_droplet_agent"] or create["ipv6"])
    assert fake.deleted == [("droplet", result["worker"]["droplet_id"])]
    assert set(fake.droplets) == production and result["worker_status"] == "destroyed"
    assert result["worker"]["assigned_at"] and "error" not in result

    soax, webshare = result["networks"]["soax"], result["networks"]["webshare"]
    assert [s["concurrency"] for s in soax["steps"]] == [500, 2000, 1500, 1000]
    assert [s["step_down"] for s in soax["steps"]] == [
        [],
        ["rate_limited", "success_drop"],
        ["proxy_connect"],
        [],
    ]
    assert soax["max_concurrency"] == 1000 and webshare["max_concurrency"] == 2000
    assert [s["concurrency"] for s in webshare["steps"]] == [500, 2000]

    folder = inputs.output / result["probe_id"]
    assert remote.folders == [folder]
    assert json.loads((folder / "probe-result.json").read_text()) == result
    table = (folder / "probe-result.md").read_text()
    assert "| soax | 1000 | `[testing] max_concurrency = 1000` |" in table
    assert "| soax | 2000 | 25000 | 95.0 | 5.0 |" in table and "rate_limited" in table
    audit = [json.loads(line) for line in audit_path(folder).read_text().splitlines()]
    assert audit[-1]["result"] == "deleted" and audit[-1].get("path") != "own_unassigned"
    written = "".join(p.read_text() for p in folder.rglob("*") if p.is_file())
    assert not IP.search(written) and "secret-net" not in written


async def test_unusable_floor_run_failures_and_a_failing_floor(tmp_path):
    fake = FakeDigitalOcean()
    inputs, _ = world(tmp_path, PROBED)
    remote = FakeProbeRemote(
        {
            ("soax", 500): summary(errors={"proxy_auth": 20_000}),
            ("webshare", 2000): RemoteError("probe webshare failed (exit 255)"),
            ("webshare", 1500): summary(statuses={"429": 2000}),
            ("webshare", 1000): summary(statuses={"429": 2000}),
            ("iproyal", 500): summary(statuses={"429": 1000}),
            **{("iproyal", c): summary(statuses={"429": 1000}) for c in (2000, 1500, 1000)},
        }
    )
    result = await probed(fake, remote, inputs)
    soax, webshare, iproyal = (result["networks"][n] for n in PROBED)
    assert soax == {
        "steps": [soax["steps"][0]],
        "max_concurrency": None,
        "verdict": "unusable at the floor",
    }
    assert webshare["steps"][1] == {
        "concurrency": 2000,
        "requested": 25_000,
        "run_failed": "RemoteError",
        "step_down": ["run_failed"],
    }
    assert webshare["max_concurrency"] == 500 and "verdict" not in webshare
    assert iproyal["max_concurrency"] == 500
    assert iproyal["verdict"] == "floor also fails: rate_limited"
    table = probing.markdown(result)
    assert "| soax | - | unusable at the floor |" in table
    assert result["worker_status"] == "destroyed"
    for reply, error in (
        (b"not json\n", "JSONDecodeError"),
        (b"", "IndexError"),
        (b'{"completed": 1}', "KeyError"),
    ):
        bad = await probed(
            FakeDigitalOcean(),
            FakeProbeRemote({("soax", 500): reply}),
            replace(inputs, networks=("soax",)),
        )
        assert bad["networks"]["soax"]["steps"][0]["run_failed"] == error


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"region": "syd1"}, "region syd1 is not planned"),
        ({"networks": ("other",)}, "networks not planned"),
        ({"start": 5001}, "start or floor"),
        ({"floor": 0}, "start or floor"),
        ({"step": 0}, "step or requests"),
    ],
)
async def test_bad_inputs_are_refused_before_anything_is_created(tmp_path, change, message):
    fake = FakeDigitalOcean()
    inputs, _ = world(tmp_path)
    with pytest.raises(ProbeError, match=message):
        await probed(fake, FakeProbeRemote({}), replace(inputs, **change))
    (tmp_path / "manager" / ".secrets" / "credentials" / "webshare.toml").unlink()
    with pytest.raises(ProbeError, match="no credentials for webshare"):
        await probed(fake, FakeProbeRemote({}), inputs)
    assert fake.requests == [] and not inputs.output.exists()


def test_the_probe_command_is_wired(tmp_path, monkeypatch, capsys):
    seen = {}

    async def fake_probe(cloud, remotes, inputs):
        seen.update(inputs=inputs, remote=remotes(tmp_path / "p"))
        return {"worker_status": "destroyed", "networks": {}}

    monkeypatch.setattr(probing, "probe", fake_probe)
    monkeypatch.setattr("proxy_index.campaign.do_api.file_token", lambda path: lambda: "t")
    assert (
        main_cli.main(
            [
                "campaign",
                "probe",
                "--root",
                str(tmp_path),
                "--networks",
                "soax,webshare",
                "--output",
                str(tmp_path / "out"),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["worker_status"] == "destroyed"
    inputs = seen["inputs"]
    assert (inputs.networks, inputs.requests, inputs.start, inputs.step, inputs.floor) == (
        ("soax", "webshare"),
        25_000,
        2000,
        500,
        500,
    )
    assert (inputs.country, inputs.region, inputs.size) == ("US", "nyc3", "c-4")
    assert seen["remote"].runtime == tmp_path / "p" / ".runtime"

    async def failed(cloud, remotes, inputs):
        return {"worker_status": "destroy_refused"}

    monkeypatch.setattr(probing, "probe", failed)
    assert main_cli.main(["campaign", "probe", "--root", str(tmp_path)]) == 3

    async def refused(cloud, remotes, inputs):
        raise ProbeError("refused: step or requests")

    monkeypatch.setattr(probing, "probe", refused)
    assert main_cli.main(["campaign", "probe", "--root", str(tmp_path)]) == 3
    assert main_cli.main(["campaign", "probe", "--root", "r", "--networks", "a,a"]) == 3
    monkeypatch.undo()
    assert main_cli.main(["campaign", "probe", "--root", str(tmp_path / "none")]) == 3
    assert "Not probed" in capsys.readouterr().err
    assert "probe" in campaign_cli.HANDLERS


# The grammar field, the plan cap and the launch.


def grammar(**testing):
    data = tomllib.loads(
        (Path(probing.__file__).parents[1] / "providers" / "grammars" / "soax.toml").read_text()
    )
    return Grammar.parse(data | {"testing": testing})


def test_grammar_max_concurrency_is_validated():
    assert grammar().max_concurrency is None and grammar(max_concurrency=1).max_concurrency == 1
    assert grammar(max_concurrency=5000).max_concurrency == 5000
    for bad in (0, 5001, True, "2000", 1500.0):
        with pytest.raises(ValueError, match="max_concurrency"):
            grammar(max_concurrency=bad)
    assert load_grammar("shifter").max_concurrency == 2000


def test_the_plan_caps_each_network_at_its_grammar_max(monkeypatch):
    from proxy_index.campaign import planspec

    assert planspec.network_concurrency("shifter", 2500) == 2000
    assert planspec.network_concurrency("shifter", 1200) == 1200
    assert planspec.network_concurrency("soax", 2500) == 500
    assert planspec.network_concurrency("proxy-seller", 2500) == 2000


async def test_a_capped_manifest_launches_at_its_own_concurrency(tmp_path):
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:2], REGIONS[:1], concurrency=2500)
    root = make_root(tmp_path, spec)
    manifest = root / spec.manifests["net00"]
    manifest.write_text(
        json.dumps({"regions": {"lon1": ["US"]}, "settings": {"concurrency": 1500}})
    )
    record, *_ = await launched(tmp_path, fake, spec)
    assert {d.network: d.concurrency for d in record.droplets} == {"net00": 1500, "net01": 2500}
    for bad in (0, 2501, "1500"):
        manifest.write_text(
            json.dumps({"regions": {"lon1": ["US"]}, "settings": {"concurrency": bad}})
        )
        with pytest.raises(ValueError, match="exceeds the campaign spec"):
            await launched(tmp_path, FakeDigitalOcean(), spec)


async def test_m1_networks_get_the_probe_country_and_bad_configs_are_refused_early(tmp_path):
    fake = FakeDigitalOcean()
    inputs, spec = world(tmp_path / "a", ("proxy-seller", "soax"))
    remote = FakeProbeRemote({})
    result = await probed(fake, remote, replace(inputs, start=1000))
    commands = {
        c[c.index("--provider") + 1]: c
        for c, _, what in remote.calls
        if what.startswith("probe ") and what != "probe setup"
    }
    assert commands["proxy-seller"][commands["proxy-seller"].index("--param") + 1] == "country=US"
    assert commands["soax"][commands["soax"].index("--param") + 1] == "country=US"
    assert result["networks"]["proxy-seller"]["max_concurrency"] == 1000
    fake = FakeDigitalOcean()
    inputs, _ = world(tmp_path / "b")
    credentials = tmp_path / "b" / "manager" / ".secrets" / "credentials" / "webshare.toml"
    credentials.chmod(0o644)
    with pytest.raises(ProbeError, match="network webshare: ValueError: Credential file must"):
        await probed(fake, FakeProbeRemote({}), inputs)
    credentials.chmod(0o600)
    with pytest.raises(ProbeError, match="network soax: ValueError: .*country"):
        await probed(fake, FakeProbeRemote({}), replace(inputs, country="U1"))
    assert fake.requests == []


async def test_m1_m2_a_failed_floor_run_and_a_failing_floor_are_their_own_verdicts(tmp_path):
    inputs, _ = world(tmp_path)
    remote = FakeProbeRemote(
        {
            ("soax", 500): RemoteError("probe soax failed (exit 2)"),
            **{("webshare", c): summary(statuses={"429": 1000}) for c in (500, 2000, 1500, 1000)},
        }
    )
    result = await probed(FakeDigitalOcean(), remote, inputs)
    assert result["networks"]["soax"]["verdict"] == "run failed at the floor"
    table = probing.markdown(result)
    assert "| soax | - | run failed at the floor |" in table
    assert "| webshare | 500 | floor also fails: rate_limited |" in table
    assert "max_concurrency = 500" not in table


def test_m7_a_real_run_summary_is_classified():
    """m7: built by the runner's own RunSummary, so a schema change breaks this test."""
    from proxy_index.models import RunSummary

    real = RunSummary(
        run_id="r",
        schema_version="1",
        framework_version="x",
        target={},
        config={},
        started_at="",
        finished_at="",
        duration_seconds=10.0,
        status="completed",
        requested=1000,
        started=1000,
        completed=1000,
        cancelled=0,
        not_started=0,
        successes=900,
        total_errors=100,
        errors={"http_status": 40, "proxy_connect": 25, "timeout": 20, "tls": 15},
        status_codes={"200": 900, "429": 30, "503": 10},
        success_rate=0.9,
        requests_per_second=100.0,
        unique_ips=850,
        pool_eligible=True,
        latency_ms={"p50": 700, "p90": 1500, "p95": 1900, "p99": 3000},
        latency_histogram={},
        response_body_bytes=0,
        buckets=[],
    ).to_dict()
    found = step_result(1000, 1000, real)
    assert (
        found["success_pct"],
        found["rate_limited_pct"],
        found["gateway_5xx_pct"],
        found["proxy_connect_pct"],
        found["timeout_pct"],
        found["other_pct"],
    ) == (90.0, 3.0, 1.0, 2.5, 2.0, 1.5)
    assert (found["rps"], found["latency_p50_ms"], found["latency_p95_ms"]) == (100.0, 700, 1900)
