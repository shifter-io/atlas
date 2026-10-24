"""CONC-1: `campaign probe` and the per-network concurrency cap.

The probe runs against the in-process fake DigitalOcean and a fake remote that answers each
step with a run summary; no request or SSH connection leaves the machine.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
import tomllib
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
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
from proxy_index.campaign.fanout import RemoteError
from proxy_index.campaign.probe import ProbeError, ProbeInputs, reasons, step_result
from proxy_index.providers.grammar import Grammar, load_grammar

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


def world(tmp_path, networks=NETWORKS[:2]):
    spec = make_spec(networks, ("lon1", "nyc3"))
    root = make_root(tmp_path, spec)
    spec_path(root).write_text(spec.to_json())
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
    assert reasons(step_result(2000, 25_000, summary(errors={"timeout": 5000})), high) == []
    assert reasons(step_result(2000, 25_000, summary(errors={"timeout": 5001})), high) == [
        "timeouts_5xx"]


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
    remote = FakeProbeRemote({("net00", 2000): summary(statuses={"429": 1250}),
                              ("net00", 1500): summary(errors={"proxy_connect": 600})})
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

    net00, net01 = result["networks"]["net00"], result["networks"]["net01"]
    assert [s["concurrency"] for s in net00["steps"]] == [500, 2000, 1500, 1000]
    assert [s["step_down"] for s in net00["steps"]] == [
        [], ["rate_limited"], ["proxy_connect"], []]
    assert net00["max_concurrency"] == 1000 and net01["max_concurrency"] == 2000
    assert [s["concurrency"] for s in net01["steps"]] == [500, 2000]

    folder = inputs.output / result["probe_id"]
    assert remote.folders == [folder]
    assert json.loads((folder / "probe-result.json").read_text()) == result
    table = (folder / "probe-result.md").read_text()
    assert "| net00 | 1000 | `[testing] max_concurrency = 1000` |" in table
    assert "| net00 | 2000 | 25000 | 95.0 | 5.0 |" in table and "rate_limited" in table
    audit = [json.loads(line) for line in audit_path(folder).read_text().splitlines()]
    assert audit[-1]["result"] == "deleted" and audit[-1].get("path") != "own_unassigned"
    written = "".join(p.read_text() for p in folder.rglob("*") if p.is_file())
    assert not IP.search(written) and "secret-net" not in written


async def test_the_worker_gets_inputs_on_stdin_only_and_fixed_commands(tmp_path):
    fake = FakeDigitalOcean()
    inputs, _ = world(tmp_path, NETWORKS[:1])
    remote = FakeProbeRemote({})
    await probed(fake, remote, replace(inputs, start=1000, country="DE"))
    (setup, payload, _), *steps = remote.calls
    assert setup[:2] == ["python3", "-c"] and setup[-2:] == [
        "/opt/proxy-index-worker", "/run/proxy-index-worker"]
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        names = archive.getnames()
        assert archive.extractfile("secrets/net00.toml").read() == b"password = 'secret-net00'\n"
        assert {m.mode for m in archive.getmembers()} == {0o600}
    assert names == ["wheel/proxy_index-1.0-py3-none-any.whl", "probe/net00.toml",
                     "secrets/net00.toml"]
    commands = [command for command, stdin, _ in steps]
    assert all(stdin is None for _, stdin, _ in steps) and len(commands) == 2
    argv = commands[1]
    assert argv[:3] == ["sh", "-c", 'ulimit -n 65536 && exec "$0" "$@"']
    assert argv[argv.index("--concurrency") + 1] == "1000"
    assert argv[argv.index("--requests") + 1] == "25000"
    assert argv[argv.index("--param") + 1] == "country=DE"
    assert argv[argv.index("--target") + 1] == "generic_http"
    assert argv[argv.index("--credentials-file") + 1] == "/run/proxy-index-worker/probe/net00.toml"
    assert "secret" not in " ".join(argv)


async def test_unusable_floor_run_failures_and_a_failing_floor(tmp_path):
    fake = FakeDigitalOcean()
    inputs, _ = world(tmp_path, NETWORKS[:3])
    remote = FakeProbeRemote({
        ("net00", 500): summary(errors={"proxy_auth": 20_000}),
        ("net01", 2000): RemoteError("probe net01 failed (exit 255)"),
        ("net01", 1500): summary(statuses={"429": 2000}),
        ("net01", 1000): summary(statuses={"429": 2000}),
        ("net02", 500): summary(statuses={"429": 1000}),
        **{("net02", c): summary(statuses={"429": 1000}) for c in (2000, 1500, 1000)},
    })
    result = await probed(fake, remote, inputs)
    net00, net01, net02 = (result["networks"][n] for n in NETWORKS[:3])
    assert net00 == {"steps": [net00["steps"][0]], "max_concurrency": None,
                     "verdict": "unusable at the floor"}
    assert net01["steps"][1] == {"concurrency": 2000, "requested": 25_000,
                                 "run_failed": "RemoteError", "step_down": ["run_failed"]}
    assert net01["max_concurrency"] == 500 and "verdict" not in net01
    assert net02["max_concurrency"] == 500
    assert net02["verdict"] == "floor also fails: rate_limited"
    table = probing.markdown(result)
    assert "| net00 | - | unusable at the floor |" in table
    assert result["worker_status"] == "destroyed"
    for reply, error in ((b"not json\n", "JSONDecodeError"), (b"", "IndexError"),
                         (b'{"completed": 1}', "KeyError")):
        bad = await probed(FakeDigitalOcean(), FakeProbeRemote({("net00", 500): reply}),
                           replace(inputs, networks=("net00",)))
        assert bad["networks"]["net00"]["steps"][0]["run_failed"] == error


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


async def test_a_worker_created_but_never_assigned_is_removed_on_the_own_path(tmp_path,
                                                                             monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("assignment crashed")

    monkeypatch.setattr(probing.scope, "assign_and_verify_project", broken)
    fake = FakeDigitalOcean()
    inputs, _ = world(tmp_path)
    result = await probed(fake, FakeProbeRemote({}), inputs)
    assert result["error"] == "RuntimeError: assignment crashed"
    assert result["worker_status"] == "destroyed" and len(fake.deleted) == 1


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
    (tmp_path / "manager" / ".secrets" / "credentials" / "net01.toml").unlink()
    with pytest.raises(ProbeError, match="no credentials for net01"):
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
    with pytest.raises(RemoteError, match=r"^probe net00 failed \(exit 3\)$"):
        await remote.run(DROPLET, ["true"], "probe net00")


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
