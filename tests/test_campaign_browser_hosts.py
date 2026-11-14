"""BROWSER-MTLS-1: `campaign manager browser-cert` issues a browser host's client certificate on
the manager (fake SSH), and the real openssl commands of `orchestrator-enable` and
`browser-cert` make a CA and a certificate that pass the orchestrator's browser-port handshake."""

from __future__ import annotations

import asyncio
import json
import stat
from datetime import timedelta

import aiohttp
import pytest
from aiohttp import web
from campaign_deploy_fakes import MANAGER_IP, parse
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from helpers import client_ca, client_certificate, self_signed

from atlas.campaign import browser_hosts, deploy, manager_orch
from atlas.campaign import cli as campaign_cli
from atlas.node_tls import client_tls
from atlas.orchestrator.app import server_tls


def recorded(tmp_path, **values):
    """An atlas manager inventory with the browser port open (as orchestrator-enable)."""
    path = tmp_path / "campaign-manager.json"
    inventory = {
        "account": "atlas",
        "manager_id": 1,
        "host": MANAGER_IP,
        "identity": str(tmp_path / "mac-key"),
        "known_hosts": "campaign-manager.known_hosts",
        "browser_port": 8444,
    }
    path.write_text(json.dumps(inventory | values))
    return path


def pem_pair(tmp_path, common_name="browser-1"):
    _, ca = client_ca(tmp_path, f"ca-{common_name}")
    cert, key = client_certificate(tmp_path, ca, common_name)
    return key.read_text(), cert.read_text()


class Shell:
    """Fake SSH: records argv and answers with `out` and `code`."""

    def __init__(self, out="", code=0):
        self.calls, self.out, self.code = [], out, code

    async def __call__(self, argv, stdin):
        self.calls.append(argv)
        return self.code, self.out


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


async def test_browser_cert_writes_an_owner_only_key_and_certificate(tmp_path):
    key, cert = pem_pair(tmp_path)
    shell, out = Shell(key + cert), tmp_path / "certs"
    result = await browser_hosts.issue(recorded(tmp_path), "browser-1", out, shell=shell)
    (argv,) = shell.calls
    assert argv[-1] == browser_hosts.issue_script("browser-1") and f"root@{MANAGER_IP}" in argv
    assert (out / "browser-1.key").read_text() == key and (
        out / "browser-1.crt"
    ).read_text() == cert
    assert (
        mode(out) == 0o700 and mode(out / "browser-1.key") == mode(out / "browser-1.crt") == 0o600
    )
    assert result["valid_days"] == 90 and result["orchestrator_url"] == f"https://{MANAGER_IP}:8444"
    assert result["key"] == str(out / "browser-1.key") and len(result["cert_sha256"]) == 64
    assert key not in json.dumps(result) and "PRIVATE" not in json.dumps(result)
    # Rotation is issuing again: both files are replaced, still owner-only.
    again_key, again_cert = pem_pair(tmp_path, "browser-1")
    (out / "browser-1.key").chmod(0o644)
    shell.out = again_key + again_cert
    await browser_hosts.issue(recorded(tmp_path), "browser-1", out, shell=shell)
    assert (out / "browser-1.key").read_text() == again_key and mode(out / "browser-1.key") == 0o600
    assert sorted(p.name for p in out.iterdir()) == ["browser-1.crt", "browser-1.key"]


def test_the_issue_script_signs_a_client_certificate_for_the_host():
    lines = browser_hosts.issue_script("browser-7").splitlines()
    assert lines[:3] == [
        "set -eu",
        f"test -s {manager_orch.BROWSER_CA}",
        f"test -s {manager_orch.BROWSER_CA_KEY}",
    ]
    script = "\n".join(lines)
    assert "-subj /CN=browser-7 " in script and "-days 90 " in script
    assert "extendedKeyUsage=clientAuth" in script and "CA:FALSE" in script
    assert "trap 'rm -rf \"$work\"' EXIT" in script and "umask 077" in script
    assert lines[-1] == 'cat "$work"/key "$work"/crt'


@pytest.mark.parametrize("host", ["mini-one", "Browser-1", "browser-1; rm -rf /", "browser-"])
async def test_a_host_that_is_not_a_logical_name_is_refused_before_ssh(tmp_path, host):
    shell = Shell()
    with pytest.raises(deploy.DeployError, match="browser-<n>"):
        await browser_hosts.issue(recorded(tmp_path), host, tmp_path / "certs", shell=shell)
    assert shell.calls == [] and not (tmp_path / "certs").exists()


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"browser_port": None}, "orchestrator-enable first"),
        ({"account": "leftclick"}, "only on the atlas account"),
    ],
)
async def test_a_manager_without_the_browser_port_is_refused(tmp_path, values, message):
    shell = Shell()
    with pytest.raises(deploy.DeployError, match=message):
        await browser_hosts.issue(recorded(tmp_path, **values), "browser-1", tmp_path, shell=shell)
    assert shell.calls == []


async def test_a_failed_or_odd_answer_writes_nothing(tmp_path):
    key, cert = pem_pair(tmp_path)
    out = tmp_path / "certs"
    for answer, code, message in [
        (key + cert, 1, "manager step failed: browser-cert"),
        (cert, 0, "one key and one certificate"),
        (key + cert + cert, 0, "one key and one certificate"),
    ]:
        with pytest.raises(deploy.DeployError, match=message) as refused:
            await browser_hosts.issue(
                recorded(tmp_path), "browser-1", out, shell=Shell(answer, code)
            )
        assert "PRIVATE" not in str(refused.value)
    assert not out.exists()


def test_the_cli_routes_browser_cert(monkeypatch, capsys, tmp_path):
    seen = []

    async def issue(inventory, host, out):
        seen.append((inventory, host, out))
        return {"host": host}

    monkeypatch.setattr(browser_hosts, "issue", issue)
    inventory, out = recorded(tmp_path), tmp_path / "certs"
    argv = ["--host", "browser-2", "--out", str(out), "--inventory", str(inventory)]
    assert campaign_cli.dispatch(parse(["campaign", "manager", "browser-cert", *argv])) == 0
    assert seen == [(inventory, "browser-2", out)]
    assert json.loads(capsys.readouterr().out) == {"host": "browser-2"}
    with pytest.raises(SystemExit):
        parse(["campaign", "manager", "browser-cert", "--out", str(out), "--inventory", "x"])


async def test_the_openssl_ca_and_certificate_pass_the_browser_port(tmp_path, monkeypatch):
    """The real openssl commands (on this machine instead of the manager) make a CA and a host
    certificate that the browser port's TLS context accepts."""
    monkeypatch.setattr(manager_orch, "BROWSER_CA", str(tmp_path / "browser-ca.crt"))
    monkeypatch.setattr(manager_orch, "BROWSER_CA_KEY", str(tmp_path / "browser-ca.key"))
    monkeypatch.setattr(browser_hosts, "WORK", str(tmp_path / "work-XXXXXX"))

    async def local(argv, stdin):
        process = await asyncio.create_subprocess_exec(
            "sh", "-c", argv[-1], stdout=asyncio.subprocess.PIPE
        )
        out, _ = await process.communicate()
        return process.returncode, out.decode()

    code, _ = await local(
        ["sh", "\n".join(["set -eu", "umask 077", *manager_orch.browser_ca_lines()])], b""
    )
    assert code == 0 and mode(tmp_path / "browser-ca.key") == 0o600
    out = tmp_path / "certs"
    await browser_hosts.issue(recorded(tmp_path), "browser-3", out, shell=local)
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("work-")) == []
    issued = x509.load_pem_x509_certificate((out / "browser-3.crt").read_bytes())
    name = issued.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    usage = issued.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert name == "browser-3" and list(usage) == [ExtendedKeyUsageOID.CLIENT_AUTH]
    lifetime = issued.not_valid_after_utc - issued.not_valid_before_utc
    assert lifetime == timedelta(days=90)

    cert, key = self_signed(tmp_path, "manager")
    app = web.Application()

    async def work(_request):
        return web.json_response({"ok": True})

    app.router.add_get("/v1/work", work)
    runner = web.AppRunner(app)
    await runner.setup()
    context = server_tls(str(cert), str(key), client_ca=manager_orch.BROWSER_CA)
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
    await site.start()
    url = f"https://127.0.0.1:{runner.addresses[0][1]}"
    try:
        tls = client_tls(url, cert, (out / "browser-3.crt", out / "browser-3.key"))
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{url}/v1/work", ssl=tls) as response:
                assert response.status == 200
    finally:
        await runner.cleanup()
