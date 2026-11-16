"""SEC-MINI-KEYS-1: a browser host makes its own key and request (`atlas browser keygen`), the
operator's CA signs and revokes on their Mac (`campaign manager browser-ca|browser-sign|
browser-revoke`), all with the real system openssl, and the orchestrator's browser port refuses
a revoked certificate once it loads the CA's revocation list (`--client-crl`)."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from campaign_deploy_fakes import parse
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from helpers import client_ca, client_certificate, self_signed

from atlas.browser import keys
from atlas.campaign import browser_ca
from atlas.campaign import cli as campaign_cli
from atlas.cli import main
from atlas.errors import UserError
from atlas.node_tls import client_tls, openssl
from atlas.orchestrator.app import browser_tls, server_tls


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def pki(tmp_path):
    """A CA made on "the Mac" and browser-1's key and request made on "the host"."""
    ca_dir = tmp_path / "ca"
    made = browser_ca.make_ca(ca_dir, datetime(2026, 11, 16, tzinfo=UTC))
    host = keys.keygen("browser-1", tmp_path / "host")
    return ca_dir, made, host


def test_the_host_makes_its_key_and_request(tmp_path, pki):
    _, _, host = pki
    folder = tmp_path / "host"
    assert sorted(p.name for p in folder.iterdir()) == ["browser-1.csr", "browser-1.key"]
    assert mode(folder) == 0o700 and mode(folder / "browser-1.key") == 0o600
    request = x509.load_pem_x509_csr((folder / "browser-1.csr").read_bytes())
    assert request.is_signature_valid
    assert request.subject.rfc4514_string() == "CN=browser-1"
    assert "PRIVATE KEY" not in json.dumps(host) and host["csr"].endswith("browser-1.csr")
    with pytest.raises(UserError, match="make the new key in a new folder"):
        keys.keygen("browser-1", folder)
    with pytest.raises(UserError, match="browser-<n>"):
        keys.keygen("../x", tmp_path / "other")


def test_the_ca_stays_in_its_owner_only_folder(tmp_path, pki):
    ca_dir, made, _ = pki
    assert mode(ca_dir) == 0o700 and mode(ca_dir / "ca.key") == 0o600
    ca = x509.load_pem_x509_certificate((ca_dir / "ca.crt").read_bytes())
    assert ca.subject.rfc4514_string() == "CN=atlas-browser-ca-20261116"
    usage = ca.extensions.get_extension_for_class(x509.KeyUsage).value
    assert usage.key_cert_sign and usage.crl_sign
    crl = x509.load_pem_x509_crl((ca_dir / "ca.crl").read_bytes())
    assert len(crl) == 0 and crl.is_signature_valid(ca.public_key())
    assert crl.next_update_utc - crl.last_update_utc >= timedelta(days=3650)
    assert "PRIVATE KEY" not in json.dumps(made)
    with pytest.raises(UserError, match="already holds a browser CA"):
        browser_ca.make_ca(ca_dir)
    os.chmod(ca_dir, 0o755)
    with pytest.raises(UserError, match="owner-only"):
        browser_ca.revoke(ca_dir, "browser-1")
    with pytest.raises(UserError, match="no usable browser CA"):
        browser_ca.sign(tmp_path / "none", "browser-1", tmp_path / "x.csr", tmp_path)


def test_signing_names_the_host_whatever_the_request_says(tmp_path, pki):
    ca_dir, _, _ = pki
    other = Path(keys.keygen("browser-9", tmp_path / "other")["csr"])
    signed = browser_ca.sign(ca_dir, "browser-2", other, tmp_path / "out")
    cert = tmp_path / "out" / "browser-2.crt"
    assert signed["cert"] == str(cert) and mode(cert) == 0o600
    issued = x509.load_pem_x509_certificate(cert.read_bytes())
    assert issued.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "browser-2"
    usage = issued.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(usage) == [ExtendedKeyUsageOID.CLIENT_AUTH]
    assert issued.not_valid_after_utc - issued.not_valid_before_utc == timedelta(days=90)
    with pytest.raises(UserError, match="--csr is not a readable file"):
        browser_ca.sign(ca_dir, "browser-2", tmp_path / "missing.csr", tmp_path / "out")
    (tmp_path / "bad.csr").write_text("not a request\n")
    with pytest.raises(UserError, match="openssl could not sign the request"):
        browser_ca.sign(ca_dir, "browser-2", tmp_path / "bad.csr", tmp_path / "out")
    assert not (ca_dir / "signed.partial").exists()


async def handshake(url: str, folder, host: str, manager) -> int:
    tls = client_tls(url, manager, (folder / f"{host}.crt", folder / f"{host}.key"))
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{url}/v1/work", ssl=tls) as response:
            return response.status


async def work(_request):
    return web.json_response({"ok": True})


async def serving(tmp_path, ca_dir, crl):
    """The browser port's TLS (`browser_tls`) in front of a one-route app."""
    cert, key = self_signed(tmp_path, "manager")
    context = browser_tls((str(cert), str(key)), (0, str(ca_dir / "ca.crt")), crl)
    app = web.Application()
    app.router.add_get("/v1/work", work)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context).start()
    return runner, f"https://127.0.0.1:{runner.addresses[0][1]}", cert


async def test_a_revoked_host_is_refused_and_the_others_still_pass(tmp_path, pki):
    ca_dir, _, _ = pki
    host_dir = tmp_path / "host"
    browser_ca.sign(ca_dir, "browser-1", host_dir / "browser-1.csr", host_dir)
    keys.keygen("browser-2", host_dir)
    browser_ca.sign(ca_dir, "browser-2", host_dir / "browser-2.csr", host_dir)
    runner, url, manager = await serving(tmp_path, ca_dir, str(ca_dir / "ca.crl"))
    try:
        assert await handshake(url, host_dir, "browser-1", manager) == 200
    finally:
        await runner.cleanup()

    revoked = browser_ca.revoke(ca_dir, "browser-1")
    assert len(revoked["revoked"]) == 1
    crl = x509.load_pem_x509_crl((ca_dir / "ca.crl").read_bytes())
    assert [hex(entry.serial_number)[2:].upper() for entry in crl] == revoked["revoked"]
    (ca_dir / "ca.crl").unlink()  # a run that stopped after the revocation, before its list
    again = browser_ca.revoke(ca_dir, "browser-1")
    assert again["revoked"] == [] and again["crl_sha256"] != revoked["crl_sha256"]
    with pytest.raises(UserError, match="no certificate of browser-7 to revoke"):
        browser_ca.revoke(ca_dir, "browser-7")

    runner, url, manager = await serving(tmp_path, ca_dir, str(ca_dir / "ca.crl"))
    try:
        with pytest.raises(aiohttp.ClientError):
            await handshake(url, host_dir, "browser-1", manager)
        assert await handshake(url, host_dir, "browser-2", manager) == 200
    finally:
        await runner.cleanup()


async def test_with_a_list_a_ca_without_its_own_list_is_refused(tmp_path, pki):
    """The bundle holds a second CA (a retired one) but the list file only the new CA's: the
    retired CA's hosts are refused, so the switch keeps an empty list file until they moved."""
    ca_dir, _, _ = pki
    old_path, old = client_ca(tmp_path, "old")
    cert, key = client_certificate(tmp_path, old, "browser-3")
    bundle = tmp_path / "bundle.crt"
    bundle.write_bytes((ca_dir / "ca.crt").read_bytes() + old_path.read_bytes())
    for crl, expected in ((ca_dir / "ca.crl", False), (tmp_path / "empty.crl", True)):
        (tmp_path / "empty.crl").write_text("")
        manager_cert, manager_key = self_signed(tmp_path, "manager")
        context = server_tls(str(manager_cert), str(manager_key), str(bundle), str(crl))
        app = web.Application()
        app.router.add_get("/v1/work", work)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context).start()
        url = f"https://127.0.0.1:{runner.addresses[0][1]}"
        try:
            tls = client_tls(url, manager_cert, (cert, key))
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.get(f"{url}/v1/work", ssl=tls) as response:
                        passed = response.status == 200
                except aiohttp.ClientError:
                    passed = False
            assert passed is expected, crl.name
        finally:
            await runner.cleanup()


def test_the_list_file_must_be_readable_and_go_with_a_client_ca(tmp_path):
    cert, key = self_signed(tmp_path, "manager")
    ca, _ = client_ca(tmp_path, "browser")
    with pytest.raises(ValueError, match="--client-crl cannot be read"):
        server_tls(str(cert), str(key), str(ca), str(tmp_path / "missing.crl"))
    with pytest.raises(ValueError, match="--client-crl needs --client-ca"):
        server_tls(str(cert), str(key), None, str(tmp_path / "missing.crl"))
    with pytest.raises(ValueError, match="--client-crl needs --browser-port"):
        browser_tls((str(cert), str(key)), None, str(tmp_path / "x.crl"))
    with pytest.raises(ValueError, match="holds no revocation list"):
        server_tls(str(cert), str(key), str(ca), str(ca))  # the CA given for the list
    (tmp_path / "blank.crl").write_text("\n")
    assert server_tls(str(cert), str(key), str(ca), str(tmp_path / "blank.crl")) is not None


def test_openssl_failures_name_the_step_alone(tmp_path, monkeypatch):
    with pytest.raises(UserError, match="^openssl could not read$"):
        openssl(["x509", "-in", "missing.pem"], "read", tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(UserError, match="openssl command cannot be run"):
        openssl(["version"], "run", tmp_path)


def test_the_commands_print_paths_and_digests_only(tmp_path, capsys):
    host = tmp_path / "host"
    assert main(["browser", "keygen", "--host", "browser-4", "--out", str(host)]) == 0
    assert json.loads(capsys.readouterr().out)["key"] == str(host / "browser-4.key")
    ca_dir = tmp_path / "ca"
    steps = [
        ["browser-ca", "--ca-dir", str(ca_dir)],
        ["browser-sign", "--ca-dir", str(ca_dir), "--host", "browser-4"]
        + ["--csr", str(host / "browser-4.csr"), "--out", str(host)],
        ["browser-revoke", "--ca-dir", str(ca_dir), "--host", "browser-4"],
    ]
    for argv in steps:
        assert campaign_cli.dispatch(parse(["campaign", "manager", *argv])) == 0
        printed = capsys.readouterr().out
        assert "PRIVATE KEY" not in printed and "BEGIN" not in printed
        assert json.loads(printed)
    assert (host / "browser-4.crt").exists()
