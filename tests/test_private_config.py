import json
import os
from pathlib import Path

import pytest
from helpers import FakeTransport, Gateway, server

from proxy_index import JsonSink, ProxyConfig, RunConfig, RunOptions, load_provider, run
from proxy_index.cli import main
from proxy_index.providers.credentials import read_credentials
from proxy_index.targets import registry

CONFIG = Path(__file__).parents[1] / "configs/shifter.toml"


def private_file(tmp_path, text='[shifter]\naccount="fixture"\npassword="private-secret"\n'):
    path = tmp_path / "credentials.toml"
    path.write_text(text)
    path.chmod(0o600)
    return path


def test_file_credentials_override_environment_and_do_not_escape(tmp_path):
    path = private_file(tmp_path)
    proxy = load_provider(
        CONFIG,
        credentials_file=path,
        environ={"SHIFTER_ACCOUNT": "wrong", "SHIFTER_PASSWORD": "wrong"},
    )
    assert proxy.account == "fixture" and proxy.password == "private-secret"
    text = json.dumps(RunConfig("generic_http", proxy).public()) + repr(proxy)
    assert "private-secret" not in text and str(path) not in text


@pytest.mark.skipif(os.name != "posix", reason="POSIX file mode enforcement")
def test_credentials_refuse_shared_permissions_and_symlinks(tmp_path):
    path = private_file(tmp_path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="mode 600"):
        read_credentials(path, "shifter")
    path.chmod(0o600)
    link = tmp_path / "symlink.toml"
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_credentials(link, "shifter")


@pytest.mark.parametrize(
    "text",
    [
        '[other]\naccount="fixture"\npassword="secret"',
        '[shifter]\naccount="fixture"',
        '[shifter]\naccount="fixture"\npassword=""',
        '[shifter]\naccount="fixture"\npassword=123',
        '[shifter]\naccount="fixture"\npassword="secret"\nextra="secret"',
    ],
)
def test_private_credential_schema(tmp_path, text):
    with pytest.raises(ValueError):
        load_provider(CONFIG, credentials_file=private_file(tmp_path, text))


@pytest.mark.parametrize(
    "target,url",
    [
        ("generic_http", "https://example.test/"),
        ("generic_https", "http://example.test/"),
        ("generic_http", "http://user:secret@example.test/"),
        ("generic_http", "http://example.test/#fragment"),
        ("generic_http", "http://example.test:0/"),
        ("generic_http", "http://example.test:invalid/"),
        ("generic_http", "http://example.test/\r\n"),
        ("generic_http", "http:///"),
        ("generic_http", 123),
    ],
)
def test_url_override_validation(target, url):
    with pytest.raises(ValueError) as exc:
        RunConfig(target, ProxyConfig("http", "localhost", 8080), target_url=url)
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("target,scheme", [("generic_http", "http"), ("generic_https", "https")])
async def test_url_override_is_per_run_and_recorded(tmp_path, target, scheme):
    original = registry.get(target).url
    received = []

    class InspectTransport(FakeTransport):
        async def execute(self, target, *, context=None):
            received.append(target.build_request().url)
            return await super().execute(target, context=context)

    url = f"{scheme}://example.test/custom?format=plain"
    config = RunConfig(
        target,
        ProxyConfig("http", "localhost", 8080),
        requests=2,
        target_url=url.upper().replace(
            "EXAMPLE.TEST/CUSTOM?FORMAT=PLAIN", "example.test/custom?format=plain"
        ),
    )
    summary = await run(config, JsonSink(tmp_path), RunOptions(transport_factory=InspectTransport))
    assert received == [url, url]
    assert summary.target["url"] == summary.config["target_url"] == url
    assert registry.get(target).url == original


async def test_custom_http_url_reaches_gateway(tmp_path):
    gateway = Gateway()
    async with server(gateway) as port:
        config = RunConfig(
            "generic_http",
            ProxyConfig("http", "127.0.0.1", port),
            requests=1,
            target_url="http://example.test/echo?plain=1",
        )
        summary = await run(config, JsonSink(tmp_path))
    assert summary.successes == 1
    assert b"GET http://example.test/echo?plain=1 HTTP/1.1" in gateway.requests[0]


def test_cli_uses_private_file_and_url(tmp_path, monkeypatch, capsys):
    path = private_file(tmp_path)

    async def fake_run(config, *args, **kwargs):
        assert config.proxy.password == "private-secret"
        assert config.target_url == "http://example.test/ip"

        class Summary:
            status = "completed"

            def to_dict(self):
                return config.public()

        return Summary()

    monkeypatch.setattr("proxy_index.cli.run", fake_run)
    assert (
        main(
            [
                "run",
                "--target",
                "generic_http",
                "--provider-config",
                str(CONFIG),
                "--credentials-file",
                str(path),
                "--url",
                "http://example.test/ip",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    assert "private-secret" not in output.out + output.err
