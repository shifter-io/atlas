"""CONC-1: `campaign probe` and the per-network concurrency cap.

The probe runs against the in-process fake DigitalOcean and a fake remote that answers each
step with a run summary; no request or SSH connection leaves the machine.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import tarfile
import tomllib
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from aiohttp import web
from campaign_fakes import CAMPAIGN_PROJECT, FakeDigitalOcean, Fault, no_sleep, running
from test_campaign_hour import (
    DROPLET,
    NETWORKS,
    REGIONS,
    FakeRunner,
    launched,
    make_root,
    make_spec,
)

from proxy_index import cli as main_cli
from proxy_index.campaign import cli as campaign_cli
from proxy_index.campaign import fanout
from proxy_index.campaign import probe as probing
from proxy_index.campaign.contracts import WORKER_TAG, audit_path, spec_path
from proxy_index.campaign.deploy import toml_table
from proxy_index.campaign.fanout import RemoteError
from proxy_index.campaign.probe import ProbeError, ProbeInputs, reasons, step_result
from proxy_index.providers.grammar import Grammar, load_grammar

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.fromisoformat("2027-01-01T22:00:00+00:00")
IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def summary(completed=25_000, successes=None, statuses=None, errors=None, rps=900.0):
    statuses, errors = statuses or {}, errors or {}
    failed = sum(statuses.get(s, 0) for s in ("429", "469", "502", "503", "504")) + sum(
        errors.values())
    return {"completed": completed, "successes": completed - failed if successes is None
            else successes, "total_errors": failed, "status_codes": {"200": 1, **statuses},
            "errors": {f"http_{s}": n for s, n in statuses.items()} | errors,
            "requests_per_second": rps, "latency_ms": {"p50": 820, "p95": 2400},
            "stop_reason": None, "unique_ips": 24_000, "config": {"gateway": "203.0.113.9"}}


GOOD = summary()


class Recording(FakeDigitalOcean):
    """Also keeps every create body."""

    def __init__(self):
        super().__init__()
        self.creates = []

    async def _create(self, request):
        self.creates.append(await request.json())
        return await super()._create(request)


class FakeProbeRemote:
    """Answers each (network, concurrency) step; records every command and the setup tar."""

    def __init__(self, answers, setup=b"installing\nready\n", ready=True):
        self.answers, self.setup, self.ready = answers, setup, ready
        self.calls, self.folders = [], []

    async def wait_ready(self, droplet, limit_seconds):
        return self.ready

    async def run(self, droplet, command, what, *, stdin=None, seconds=120):
        self.calls.append((command, stdin, what))
        if command[0] == "python3":
            if isinstance(self.setup, Exception):
                raise self.setup
            return self.setup
        key = (command[command.index("--provider") + 1],
               int(command[command.index("--concurrency") + 1]))
        answer = self.answers.get(key, GOOD)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, bytes):
            return answer
        return ("Results: /opt/x\n" + json.dumps(answer) + "\n").encode()

    def factory(self, folder):
        self.folders.append(folder)
        return self


PROBED = ("soax", "webshare", "iproyal")


def world(tmp_path, networks=PROBED[:2]):
    """A manager with a planned campaign: real census configs and 0600 credentials."""
    spec = make_spec(networks, ("lon1", "nyc3"))
    root = make_root(tmp_path, spec)
    spec_path(root).write_text(spec.to_json())
    for network in networks:
        config = ROOT / "configs" / f"{network}.toml"
        (root / spec.manifests[network]).with_name("provider.toml").write_bytes(
            config.read_bytes())
        names = tomllib.loads(config.read_text())["credentials"]
        credentials = tmp_path / "manager" / ".secrets" / "credentials" / f"{network}.toml"
        credentials.write_bytes(toml_table(network, {
            key.removesuffix("_env"): "acct9" if key == "account_env" else f"secret-{network}"
            for key in names}))
        credentials.chmod(0o600)
    return ProbeInputs(root, tmp_path / "manager" / "probes"), spec


async def probed(fake, remote, inputs):
    async with running(fake) as cloud:
        return await probing.probe(cloud, remote.factory, inputs, now=NOW, sleep=no_sleep)


def posts(fake, path):
    return [r for r in fake.requests if r[0] == "POST" and r[1] == path]


def test_step_result_classifies_every_failure_as_a_share_of_completed_requests():
    found = step_result(2000, 25_000, summary(
        20_000, statuses={"429": 400, "469": 100, "502": 50, "503": 30, "504": 20},
        errors={"proxy_connect": 300, "connection": 100, "timeout": 200, "tls": 40}))
    assert found == {
        "concurrency": 2000, "requested": 25_000, "completed": 20_000, "success_pct": 93.8,
        "rate_limited_pct": 2.5, "gateway_5xx_pct": 0.5, "proxy_connect_pct": 2.0,
        "timeout_pct": 1.0, "other_pct": 0.2, "rps": 900.0, "latency_p50_ms": 820,
        "latency_p95_ms": 2400, "stop_reason": None}
    assert not IP.search(json.dumps(found))
    empty = step_result(500, 25_000, summary(0, successes=0) | {"latency_ms": None})
    assert empty["success_pct"] == 0.0 and empty["latency_p50_ms"] is None


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
    assert reasons(step_result(2000, 25_000, summary(
        statuses={"504": 100}, errors={"timeout": 250})), base) == []
    high = step_result(500, 25_000, summary(errors={"timeout": 2500}))  # 10% at the floor
    assert reasons(step_result(2000, 25_000, summary(errors={"timeout": 3250})), high) == []
    assert reasons(step_result(2000, 25_000, summary(errors={"timeout": 5001})), high) == [
        "timeouts_5xx", "success_drop"]


def test_s2_every_failure_class_and_a_success_drop_count():
    """S2: 407s, DNS, TLS, protocol errors, other statuses and blocks can no longer pass."""
    base = step_result(500, 25_000, summary(errors={"tls": 50}))  # 0.2% other at the floor
    cases = {
        # The reviewer's case: 30% 407, 4% DNS, 4% protocol errors.
        ("proxy_connect", "other_errors", "success_drop"): summary(
            errors={"proxy_auth": 7500, "dns": 1000, "protocol": 1000}),
        ("proxy_connect",): summary(errors={"proxy_auth": 501}),  # a 407 storm
        ("other_errors",): summary(errors={"protocol": 150, "http_status": 151}),  # 1.204%
        ("other_errors", "success_drop"): summary(errors={"blocked": 450, "challenge": 450}),
        # Every class under its own limit, yet 5.5 points less success than the baseline.
        ("success_drop",): summary(statuses={"429": 475}, errors={
            "proxy_connect": 475, "timeout": 240, "tls": 225}),
    }
    for expected, run in cases.items():
        assert reasons(step_result(2000, 25_000, run), base) == list(expected), expected
    assert reasons(step_result(2000, 25_000, summary(errors={"tls": 299})), base) == []
    assert step_result(2000, 25_000, summary(errors={"proxy_auth": 5, "dns": 5}))[
        "proxy_connect_pct"] == 0.04


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
    remote = FakeProbeRemote({("soax", 2000): summary(statuses={"429": 1250}),
                              ("soax", 1500): summary(errors={"proxy_connect": 600})})
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
        [], ["rate_limited", "success_drop"], ["proxy_connect"], []]
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


async def test_the_worker_gets_inputs_on_stdin_only_and_fixed_commands(tmp_path):
    fake = FakeDigitalOcean()
    inputs, _ = world(tmp_path, PROBED[:1])
    remote = FakeProbeRemote({})
    await probed(fake, remote, replace(inputs, start=1000, country="DE"))
    (setup, payload, _), *steps = remote.calls
    assert setup[:2] == ["python3", "-c"] and setup[-2:] == [
        "/opt/proxy-index-worker", "/run/proxy-index-worker"]
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        names = archive.getnames()
        assert archive.extractfile("secrets/soax.toml").read() == (
            inputs.root.parents[1] / ".secrets" / "credentials" / "soax.toml").read_bytes()
        assert {m.mode for m in archive.getmembers()} == {0o600}
    assert names == ["wheel/proxy_index-1.0-py3-none-any.whl", "probe/soax.toml",
                     "secrets/soax.toml"]
    commands = [command for command, stdin, _ in steps]
    assert all(stdin is None for _, stdin, _ in steps) and len(commands) == 2
    argv = commands[1]
    assert argv[:3] == ["sh", "-c", 'ulimit -n 65536 && exec "$0" "$@"']
    assert argv[argv.index("--concurrency") + 1] == "1000"
    assert argv[argv.index("--requests") + 1] == "25000"
    assert argv[argv.index("--param") + 1] == "country=DE"
    assert argv[argv.index("--target") + 1] == "generic_http"
    assert argv[argv.index("--credentials-file") + 1] == "/run/proxy-index-worker/probe/soax.toml"
    assert "secret" not in " ".join(argv)


async def test_unusable_floor_run_failures_and_a_failing_floor(tmp_path):
    fake = FakeDigitalOcean()
    inputs, _ = world(tmp_path, PROBED)
    remote = FakeProbeRemote({
        ("soax", 500): summary(errors={"proxy_auth": 20_000}),
        ("webshare", 2000): RemoteError("probe webshare failed (exit 255)"),
        ("webshare", 1500): summary(statuses={"429": 2000}),
        ("webshare", 1000): summary(statuses={"429": 2000}),
        ("iproyal", 500): summary(statuses={"429": 1000}),
        **{("iproyal", c): summary(statuses={"429": 1000}) for c in (2000, 1500, 1000)},
    })
    result = await probed(fake, remote, inputs)
    soax, webshare, iproyal = (result["networks"][n] for n in PROBED)
    assert soax == {"steps": [soax["steps"][0]], "max_concurrency": None,
                     "verdict": "unusable at the floor"}
    assert webshare["steps"][1] == {"concurrency": 2000, "requested": 25_000,
                                 "run_failed": "RemoteError", "step_down": ["run_failed"]}
    assert webshare["max_concurrency"] == 500 and "verdict" not in webshare
    assert iproyal["max_concurrency"] == 500
    assert iproyal["verdict"] == "floor also fails: rate_limited"
    table = probing.markdown(result)
    assert "| soax | - | unusable at the floor |" in table
    assert result["worker_status"] == "destroyed"
    for reply, error in ((b"not json\n", "JSONDecodeError"), (b"", "IndexError"),
                         (b'{"completed": 1}', "KeyError")):
        bad = await probed(FakeDigitalOcean(), FakeProbeRemote({("soax", 500): reply}),
                           replace(inputs, networks=("soax",)))
        assert bad["networks"]["soax"]["steps"][0]["run_failed"] == error


async def test_a_setup_or_ssh_failure_still_removes_the_worker(tmp_path):
    for index, (remote, error) in enumerate((
                          (FakeProbeRemote({}, setup=RemoteError("probe setup failed (exit 1)")),
                           "RemoteError: probe setup failed (exit 1)"),
                          (FakeProbeRemote({}, setup=b"oops\n"),
                           "ProbeError: probe setup did not finish"),
                          (FakeProbeRemote({}, ready=False),
                           "ProbeError: worker SSH never became ready"))):
        fake = FakeDigitalOcean()
        inputs, _ = world(tmp_path / str(index))
        result = await probed(fake, remote, inputs)
        assert result["error"] == error and result["networks"] == {}
        assert result["worker_status"] == "destroyed" and fake.deleted == [
            ("droplet", result["worker"]["droplet_id"])]


async def test_an_unconfirmed_worker_is_removed_on_the_own_unassigned_path(tmp_path):
    fake = FakeDigitalOcean()
    fake.faults.append(Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 400, times=3))
    inputs, _ = world(tmp_path)
    remote = FakeProbeRemote({})
    result = await probed(fake, remote, inputs)
    assert result["error"].startswith("ProbeError: worker not confirmed in the project")
    assert remote.calls == [] and result["worker_status"] == "destroyed"
    audit = [json.loads(line) for line in audit_path(
        inputs.output / result["probe_id"]).read_text().splitlines()]
    assert audit[-1]["path"] == "own_unassigned" and audit[-1]["result"] == "deleted"


async def test_an_assignment_error_is_retried_by_the_removal_then_recovered(tmp_path,
                                                                           monkeypatch):
    """S1: an assignment that raised leaves no mark; the removal confirms it first."""
    real, calls = probing.scope.assign_and_verify_project, []

    async def once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("assignment crashed")
        return await real(*args, **kwargs)

    monkeypatch.setattr(probing.scope, "assign_and_verify_project", once)
    fake = FakeDigitalOcean()
    inputs, _ = world(tmp_path / "a")
    result = await probed(fake, FakeProbeRemote({}), inputs)
    assert result["error"] == "RuntimeError: assignment crashed" and len(calls) == 2
    assert result["worker_status"] == "destroyed" and len(fake.deleted) == 1
    assert "recover" not in result

    async def broken(*args, **kwargs):
        raise RuntimeError("assignment crashed")

    monkeypatch.setattr(probing.scope, "assign_and_verify_project", broken)
    fake = FakeDigitalOcean()
    inputs, _ = world(tmp_path / "b")
    result = await probed(fake, FakeProbeRemote({}), inputs)
    folder = inputs.output / result["probe_id"]
    assert result["worker_status"] == "destroy_failed" and fake.deleted == []
    assert result["recover"] == f"campaign probe --remove {folder}"
    monkeypatch.setattr(probing.scope, "assign_and_verify_project", real)
    async with running(fake) as cloud:  # a later process: a new client, nothing in memory
        assert await probing.recover(cloud, folder, sleep=no_sleep) == {
            "probe_id": result["probe_id"], "worker_status": "destroyed"}
    saved = json.loads((folder / "probe-result.json").read_text())
    assert saved["worker_status"] == "destroyed" and "recover" not in saved
    assert fake.deleted == [("droplet", result["worker"]["droplet_id"])]


async def test_a_lost_create_answer_is_adopted_and_a_failed_create_reported(tmp_path):
    fake = FakeDigitalOcean()
    fake.create_then_fail = 1
    inputs, _ = world(tmp_path / "a")
    result = await probed(fake, FakeProbeRemote({}), inputs)
    assert "error" not in result and result["worker_status"] == "destroyed"
    assert len(posts(fake, "/v2/droplets")) == 1
    fake = FakeDigitalOcean()
    fake.faults.append(Fault("POST", "/v2/droplets", 422))
    inputs, _ = world(tmp_path / "b")
    result = await probed(fake, FakeProbeRemote({}), inputs)
    assert result["error"].startswith("CreateError") and result["worker_status"] == (
        "create_failed") and fake.deleted == []


async def test_a_guard_refusal_or_error_is_reported_never_bypassed(tmp_path, monkeypatch):
    for failure, status in ((probing.scope.OwnershipError("droplet", "1", ["tags"]),
                             "destroy_refused"), (OSError("down"), "destroy_failed")):
        async def refuse(*args, failure=failure, **kwargs):
            raise failure

        monkeypatch.setattr(probing.scope, "guarded_delete", refuse)
        fake = FakeDigitalOcean()
        inputs, _ = world(tmp_path / status)
        result = await probed(fake, FakeProbeRemote({}), inputs)
        assert result["worker_status"] == status and fake.deleted == []


@pytest.mark.parametrize(("change", "message"), [
    ({"region": "syd1"}, "region syd1 is not planned"),
    ({"networks": ("other",)}, "networks not planned"),
    ({"start": 5001}, "start or floor"),
    ({"floor": 0}, "start or floor"),
    ({"step": 0}, "step or requests"),
])
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
    assert main_cli.main(["campaign", "probe", "--root", str(tmp_path), "--networks",
                          "soax,webshare", "--output", str(tmp_path / "out")]) == 0
    assert json.loads(capsys.readouterr().out)["worker_status"] == "destroyed"
    inputs = seen["inputs"]
    assert (inputs.networks, inputs.requests, inputs.start, inputs.step, inputs.floor) == (
        ("soax", "webshare"), 25_000, 2000, 500, 500)
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


async def test_the_ssh_remote_runs_one_fixed_command_with_stdin(tmp_path):
    runner = FakeRunner((0, b"ready\n"), (3, b"203.0.113.7 noise"))
    remote = fanout.OpenSSHRemote(tmp_path / "key", tmp_path / "rt", 0, runner=runner)
    assert await remote.run(DROPLET, ["python3", "-c", "x"], "probe setup",
                            stdin=b"tar") == b"ready\n"
    assert runner.calls[0][1] == b"tar" and runner.calls[0][0][-1] == "python3 -c x"
    with pytest.raises(RemoteError, match=r"^probe soax failed \(exit 3\)$"):
        await remote.run(DROPLET, ["true"], "probe soax")


# The grammar field, the plan cap and the launch.

def grammar(**testing):
    data = tomllib.loads((Path(probing.__file__).parents[1] / "providers" / "grammars"
                          / "soax.toml").read_text())
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
    assert planspec.network_concurrency("soax", 2500) == 2500  # nothing recorded yet


async def test_a_capped_manifest_launches_at_its_own_concurrency(tmp_path):
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:2], REGIONS[:1], concurrency=2500)
    root = make_root(tmp_path, spec)
    manifest = root / spec.manifests["net00"]
    manifest.write_text(json.dumps({"regions": {"lon1": ["US"]},
                                    "settings": {"concurrency": 1500}}))
    record, *_ = await launched(tmp_path, fake, spec)
    assert {d.network: d.concurrency for d in record.droplets} == {"net00": 1500, "net01": 2500}
    for bad in (0, 2501, "1500"):
        manifest.write_text(json.dumps({"regions": {"lon1": ["US"]},
                                        "settings": {"concurrency": bad}}))
        with pytest.raises(ValueError, match="exceeds the campaign spec"):
            await launched(tmp_path, FakeDigitalOcean(), spec)


def test_the_setup_script_writes_private_credentials_and_installs_like_a_round(tmp_path):
    """m5: the probe and the round share one wheel install block."""
    import os
    import stat
    import subprocess
    import sys

    assert fanout.INSTALL_WHEEL in probing.SETUP_SCRIPT
    assert fanout.INSTALL_WHEEL in fanout.START_SCRIPT
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "uv").write_text("#!/bin/sh\necho \"$@\" >> \"$(dirname \"$0\")/calls\"\n")
    (stub / "uv").chmod(0o755)
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for name, data in (("wheel/w-1-py3-none-any.whl", b"wheel"),
                           ("probe/soax.toml", b"provider = 'soax'\n"),
                           ("secrets/soax.toml", b"password = 'x'\n")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    root, secrets = tmp_path / "worker", tmp_path / "run"
    env = os.environ | {"PATH": f"{stub}:{os.environ['PATH']}"}
    for _ in range(2):  # the second push finds the same wheel installed
        reply = subprocess.run([sys.executable, "-c", probing.SETUP_SCRIPT, str(root),
                                str(secrets)], input=payload.getvalue(), capture_output=True,
                               env=env, check=True)
        assert reply.stdout.decode().splitlines()[-1] == "ready"
    credentials = secrets / "probe" / "soax.toml"
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    assert (root / "probe" / "soax.toml").exists() and not (root / "secrets").exists()
    assert len((stub / "calls").read_text().splitlines()) == 1


class Hidden(FakeDigitalOcean):
    """The create answer is lost and the first `hide` tag listings do not show it yet."""

    def __init__(self, hide):
        super().__init__()
        self.hide, self.create_then_fail = hide, 1

    async def _list(self, request):
        if self.hide and request.query.get("tag_name"):
            self.hide -= 1
            return web.json_response({"droplets": [], "meta": {"total": 0}})
        return await super()._list(request)


def sleeps_into(seen):
    async def sleep(seconds):
        seen.append(seconds)

    return sleep


async def test_s1_a_lost_create_is_looked_up_again_after_the_launch_delay(tmp_path):
    fake, seen = Hidden(1), []
    inputs, _ = world(tmp_path)
    async with running(fake) as cloud:
        result = await probing.probe(cloud, FakeProbeRemote({}).factory, inputs, now=NOW,
                                     sleep=sleeps_into(seen))
    assert probing.CREATE_RETRY_SECONDS in seen and "error" not in result
    assert len(posts(fake, "/v2/droplets")) == 1 and result["worker_status"] == "destroyed"


async def test_s1_a_create_still_unlisted_is_reported_and_recovered_later(tmp_path):
    fake = Hidden(3)  # the first look, the look 15 s later, the last look in the cleanup
    fake.seed_production(40)
    production = set(fake.droplets)
    inputs, _ = world(tmp_path)
    result = await probed(fake, FakeProbeRemote({}), inputs)
    folder = inputs.output / result["probe_id"]
    assert result["worker"]["error_class"] == "create_uncertain"
    assert result["worker_status"] == "create_failed" and result["worker"]["droplet_id"] is None
    assert result["recover"] == f"campaign probe --remove {folder}"
    [left] = set(fake.droplets) - production  # the droplet the lost answer created
    async with running(fake) as cloud:
        assert (await probing.recover(cloud, folder, sleep=no_sleep))["worker_status"] == (
            "destroyed")
    assert fake.deleted == [("droplet", left)] and set(fake.droplets) == production
    async with running(fake) as cloud:  # a rerun finds it removed and does nothing
        assert (await probing.recover(cloud, folder))["worker_status"] == "destroyed"
    assert len(fake.deleted) == 1


async def test_s1_an_unreadable_listing_or_a_create_that_never_landed(tmp_path):
    fake = FakeDigitalOcean()
    fake.create_then_fail = 1
    fake.faults.append(Fault("GET", "/v2/droplets", 400, times=10))
    inputs, _ = world(tmp_path / "a")
    result = await probed(fake, FakeProbeRemote({}), inputs)
    assert result["worker"]["error_class"] == "create_uncertain" and "recover" in result
    fake = FakeDigitalOcean()
    fake.faults.append(Fault("POST", "/v2/droplets", 500, times=10))  # nothing was created
    inputs, _ = world(tmp_path / "b")
    result = await probed(fake, FakeProbeRemote({}), inputs)
    async with running(fake) as cloud:
        found = await probing.recover(cloud, inputs.output / result["probe_id"])
    assert found["worker_status"] == "absent" and fake.deleted == []


async def died(fake, inputs, monkeypatch, stage):
    """A probe process that stops for good (SIGKILL, OOM, reboot) at `stage`: no cleanup."""
    if stage == "before_assignment":
        async def gone(*args, **kwargs):
            raise RuntimeError("killed")

        monkeypatch.setattr(probing.scope, "assign_and_verify_project", gone)
    async with running(fake) as cloud:
        run = probing.Probe(cloud, FakeProbeRemote({}).factory, inputs, NOW, no_sleep)
        with pytest.raises(RuntimeError if stage == "before_assignment" else ProbeError):
            await run.create()
            raise ProbeError("killed after the assignment")
    monkeypatch.undo()
    return inputs.output / run.probe_id


@pytest.mark.parametrize("stage", ["before_assignment", "after_assignment"])
async def test_s1_a_dead_probe_is_recovered_through_the_guard(tmp_path, monkeypatch, stage):
    fake = FakeDigitalOcean()
    fake.seed_production(40)
    production = set(fake.droplets)
    inputs, _ = world(tmp_path)
    folder = await died(fake, inputs, monkeypatch, stage)
    worker = json.loads((folder / "probe-result.json").read_text())["worker"]
    assert worker["droplet_id"] and bool(worker["assigned_at"]) == (stage == "after_assignment")
    async with running(fake) as cloud:  # nothing in memory: the record is all it has
        assert (await probing.recover(cloud, folder, sleep=no_sleep))["worker_status"] == (
            "destroyed")
    assert fake.deleted == [("droplet", worker["droplet_id"])] and set(fake.droplets) == (
        production)
    audit = [json.loads(line) for line in audit_path(folder).read_text().splitlines()]
    assert audit[-1]["result"] == "deleted" and audit[-1]["path"] == "normal"


async def test_s1_a_marked_worker_whose_removal_failed_takes_the_own_unassigned_path(tmp_path):
    fake = FakeDigitalOcean()
    fake.faults += [Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 400, times=3),
                    Fault("DELETE", "", 500)]  # path set below, once the id is known
    inputs, _ = world(tmp_path)
    async with running(fake) as cloud:
        run = probing.Probe(cloud, FakeProbeRemote({}).factory, inputs, NOW, no_sleep)
        original = cloud.create

        async def create(*args, **kwargs):
            made = await original(*args, **kwargs)
            fake.faults[-1] = Fault("DELETE", f"/v2/droplets/{made[0].id}", 500, times=10)
            return made

        cloud.create = create
        result = await run.run()
    assert result["worker"]["assign_failed_at"] and result["worker_status"] == "destroy_failed"
    fake.faults.clear()
    async with running(fake) as cloud:
        assert (await probing.recover(cloud, inputs.output / run.probe_id))[
            "worker_status"] == "destroyed"
    audit = [json.loads(line) for line in audit_path(
        inputs.output / run.probe_id).read_text().splitlines()]
    assert audit[-1]["path"] == "own_unassigned" and audit[-1]["result"] == "deleted"


async def test_s1_recovery_never_touches_a_droplet_the_record_does_not_own(tmp_path):
    fake = FakeDigitalOcean()
    fake.seed_production(20)
    inputs, _ = world(tmp_path)
    result = await probed(fake, FakeProbeRemote({}, ready=False), inputs)
    folder = inputs.output / result["probe_id"]
    record = json.loads((folder / "probe-result.json").read_text())
    victim = next(iter(fake.droplets))
    record["worker"].update(droplet_id=victim, status="destroy_failed", assigned_at=None)
    (folder / "probe-result.json").write_text(json.dumps(record))
    async with running(fake) as cloud:
        assert (await probing.recover(cloud, folder, sleep=no_sleep))["worker_status"] == (
            "destroy_refused")
    assert victim in fake.droplets and fake.deleted[-1] != ("droplet", victim)
    with pytest.raises(ProbeError, match="no probe record"):
        async with running(fake) as cloud:
            await probing.recover(cloud, tmp_path)


async def test_s1_a_signal_cancels_the_probe_and_the_worker_is_still_removed(tmp_path,
                                                                            monkeypatch):
    class Cancelled(FakeProbeRemote):
        async def run(self, droplet, command, what, *, stdin=None, seconds=120):
            if what == "probe webshare":
                asyncio.current_task().cancel()
                await asyncio.sleep(0)
            return await super().run(droplet, command, what, stdin=stdin, seconds=seconds)

    real = probing.release

    async def interrupted(*args, **kwargs):  # a second signal during the removal
        outer.cancel()
        await asyncio.sleep(0)
        return await real(*args, **kwargs)

    monkeypatch.setattr(probing, "release", interrupted)
    fake = FakeDigitalOcean()
    inputs, _ = world(tmp_path)
    outer = asyncio.current_task()
    result = await probed(fake, Cancelled({}), inputs)
    assert result["error"] == "cancelled by a signal" and result["worker_status"] == "destroyed"
    assert list(result["networks"]) == ["soax", "webshare"] and len(fake.deleted) == 1
    assert json.loads((inputs.output / result["probe_id"] / "probe-result.json").read_text())[
        "worker_status"] == "destroyed"


def test_s1_the_cli_cancels_on_sigterm_and_wires_remove(tmp_path, monkeypatch, capsys):
    import os
    import signal

    async def stopped(cloud, remotes, inputs):
        os.kill(os.getpid(), signal.SIGTERM)
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            os.kill(os.getpid(), signal.SIGTERM)  # ignored while the removal runs
            await asyncio.sleep(0)
            return {"worker_status": "destroyed", "error": "cancelled by a signal"}
        return {"worker_status": "destroyed"}

    monkeypatch.setattr(probing, "probe", stopped)
    monkeypatch.setattr("proxy_index.campaign.do_api.file_token", lambda path: lambda: "t")
    assert main_cli.main(["campaign", "probe", "--root", str(tmp_path)]) == 3
    assert json.loads(capsys.readouterr().out)["error"] == "cancelled by a signal"

    async def removed(cloud, folder):
        return {"probe_id": "p", "worker_status": "absent"}

    monkeypatch.setattr(probing, "recover", removed)
    assert main_cli.main(["campaign", "probe", "--remove", str(tmp_path)]) == 0

    async def interrupted(cloud, folder):
        raise asyncio.CancelledError

    monkeypatch.setattr(probing, "recover", interrupted)
    assert main_cli.main(["campaign", "probe", "--remove", str(tmp_path)]) == 3
    assert "Interrupted" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main_cli.main(["campaign", "probe", "--root", "a", "--remove", "b"])


async def test_s1_a_recovered_worker_the_project_never_confirms_is_removed_fail_closed(
        tmp_path, monkeypatch):
    fake = FakeDigitalOcean()
    inputs, _ = world(tmp_path)
    folder = await died(fake, inputs, monkeypatch, "before_assignment")
    fake.faults.append(Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 400, times=3))
    async with running(fake) as cloud:
        assert (await probing.recover(cloud, folder, sleep=no_sleep))["worker_status"] == (
            "destroyed")
    audit = [json.loads(line) for line in audit_path(folder).read_text().splitlines()]
    assert audit[-1]["path"] == "own_unassigned" and len(fake.deleted) == 1


async def test_m1_proxy_seller_gets_no_country_and_bad_configs_are_refused_early(tmp_path):
    fake = FakeDigitalOcean()
    inputs, spec = world(tmp_path / "a", ("proxy-seller", "soax"))
    remote = FakeProbeRemote({})
    result = await probed(fake, remote, replace(inputs, start=1000))
    commands = {c[c.index("--provider") + 1]: c for c, _, what in remote.calls
                if what.startswith("probe ") and what != "probe setup"}
    assert "--param" not in commands["proxy-seller"]
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
    remote = FakeProbeRemote({
        ("soax", 500): RemoteError("probe soax failed (exit 2)"),
        **{("webshare", c): summary(statuses={"429": 1000}) for c in (500, 2000, 1500, 1000)}})
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
        run_id="r", schema_version="1", framework_version="x", target={}, config={},
        started_at="", finished_at="", duration_seconds=10.0, status="completed",
        requested=1000, started=1000, completed=1000, cancelled=0, not_started=0,
        successes=900, total_errors=100,
        errors={"http_status": 40, "proxy_connect": 25, "timeout": 20, "tls": 15},
        status_codes={"200": 900, "429": 30, "503": 10}, success_rate=0.9,
        requests_per_second=100.0, unique_ips=850, pool_eligible=True,
        latency_ms={"p50": 700, "p90": 1500, "p95": 1900, "p99": 3000},
        latency_histogram={}, response_body_bytes=0, buckets=[]).to_dict()
    found = step_result(1000, 1000, real)
    assert (found["success_pct"], found["rate_limited_pct"], found["gateway_5xx_pct"],
            found["proxy_connect_pct"], found["timeout_pct"], found["other_pct"]) == (
        90.0, 3.0, 1.0, 2.5, 2.0, 1.5)
    assert (found["rps"], found["latency_p50_ms"], found["latency_p95_ms"]) == (100.0, 700, 1900)
