"""CONC-1: `campaign probe` runs on a probe worker, the command, and the per-network
concurrency cap (step results and the step-down rule are in test_campaign_probe_steps.py).

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
from campaign_probe_cases import IP, PROBED, FakeProbeRemote, Recording, probed, summary, world

from atlas import cli as main_cli
from atlas.campaign import cli as campaign_cli
from atlas.campaign import probe as probing
from atlas.campaign.contracts import WORKER_TAG, audit_path
from atlas.campaign.fanout import RemoteError
from atlas.campaign.probe import ProbeError
from atlas.providers.grammar import Grammar, load_grammar


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
    assert create["names"] == [f"atlas-{sid}-h00-nyc3-probe"] and create["size"] == "c-4"
    assert create["image"] == "snap-nyc3" and create["ssh_keys"] == ["4242"]
    assert create["tags"] == [WORKER_TAG, f"atlas-camp-{sid}", f"atlas-camp-{sid}-h00"]
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
    monkeypatch.setattr("atlas.campaign.do_api.file_token", lambda path: lambda: "t")
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
    from atlas.campaign import planspec

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
