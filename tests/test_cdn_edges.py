"""scripts/server-setup/atlas-cdn-edges.py (SEC-RATE-1): reading the CDN's edge-server lists and
writing the files Caddy imports. Every address is from the documentation ranges; nothing reaches
the network but a local server, and a fake `caddy` and `systemctl` stand in for the real ones.
"""

import http.server
import ipaddress
import os
import threading
import types
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "server-setup" / "atlas-cdn-edges.py"
# Compiled by hand: an import would leave a __pycache__ among the server-setup templates.
edges = types.ModuleType("atlas_cdn_edges")
exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), edges.__dict__)

V4 = [f"192.0.2.{i}" for i in range(1, 13)]
V6 = ["2001:db8::1", "2001:db8:1::/48"]


def nets(*items: str) -> list:
    return [ipaddress.ip_network(i, strict=False) for i in items]


def test_parses_a_json_array_and_plain_lines():
    assert edges.parse_list('["192.0.2.1", "2001:db8::1", " 198.51.100.0/24 "]') == nets(
        "192.0.2.1", "2001:db8::1", "198.51.100.0/24"
    )
    assert edges.parse_list("192.0.2.1\r\n\r\n2001:db8::1\n198.51.100.7/32\n") == nets(
        "192.0.2.1", "2001:db8::1", "198.51.100.7"
    )
    assert edges.parse_list("") == []


@pytest.mark.parametrize(
    "text",
    [
        '["192.0.2.1", 7]',
        '["192.0.2.1",',
        "192.0.2.1\n<html>",
        "<ArrayOfstring><string>192.0.2.1</string></ArrayOfstring>",
        "192.0.2.1, 192.0.2.2",
        "192.0.2.300",
    ],
)
def test_anything_else_is_an_error(text):
    with pytest.raises(edges.EdgeListError):
        edges.parse_list(text)


def test_checked_sorts_and_drops_repeats():
    got = edges.checked(nets(*reversed(V4), *V6, V4[0]), 10)
    assert [str(n) for n in got] == [f"{a}/32" for a in V4] + ["2001:db8::1/128", "2001:db8:1::/48"]


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ("10.1.2.3", "no public address"),
        ("172.20.0.0/16", "no public address"),
        ("192.168.1.1", "no public address"),
        ("100.64.0.1", "no public address"),
        ("127.0.0.1", "no public address"),
        ("169.254.1.1", "no public address"),
        ("0.0.0.0/32", "no public address"),
        ("fd00::1", "no public address"),
        ("::ffff:192.0.2.1", "no public address"),
        ("::1", "no public address"),
        ("fe80::1", "no public address"),
        ("198.51.100.0/15", "wider than /16"),
        ("0.0.0.0/0", "no public address"),
        ("2001:db8::/31", "wider than /32"),
    ],
)
def test_checked_refuses_what_caddy_must_not_trust(extra, reason):
    with pytest.raises(edges.EdgeListError, match=reason):
        edges.checked(nets(*V4, extra), 10)


def test_checked_needs_enough_addresses_and_not_too_many():
    with pytest.raises(edges.EdgeListError, match="9 addresses, want 10"):
        edges.checked(nets(*V4[:9]), 10)
    assert len(edges.checked(nets(*V4[:9]), 9)) == 9
    many = [ipaddress.ip_network(f"2001:db8::{i:x}") for i in range(edges.MAX_RANGES + 1)]
    with pytest.raises(edges.EdgeListError, match="addresses, want"):
        edges.checked(many, 10)


def test_rendered_files():
    files = edges.rendered(nets("192.0.2.1", "2001:db8::/48"))
    trusted = files["cdn-edges.caddy"].splitlines()[1]
    assert trusted == "trusted_proxies static 192.0.2.1/32 2001:db8::/48"
    assert files["client-header.caddy"].splitlines()[1] == "header_up X-Atlas-Client-Ip {client_ip}"
    for text in files.values():
        assert text.startswith("# Written by atlas-cdn-edges")


def test_list_urls(tmp_path):
    lists = tmp_path / "lists"
    lists.write_text("# the lists\nhttps://lists.example/v4  # IPv4\n\nhttp://127.0.0.1:9/v6\n")
    assert edges.list_urls(lists) == ["https://lists.example/v4", "http://127.0.0.1:9/v6"]
    for text in ("# nothing\n", "http://lists.example/v4\n", "ftp://lists.example/v4\n"):
        lists.write_text(text)
        with pytest.raises(edges.EdgeListError):
            edges.list_urls(lists)
    with pytest.raises(edges.EdgeListError, match="cannot read"):
        edges.list_urls(tmp_path / "missing")


@pytest.fixture
def lists_server():
    """A local server answering each path with the body set in its `bodies`."""
    bodies: dict[str, bytes] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = bodies.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.bodies = bodies
    srv.url = f"http://127.0.0.1:{srv.server_port}"
    yield srv
    srv.shutdown()


def test_fetch(lists_server):
    lists_server.bodies["/ok"] = b"192.0.2.1\n"
    lists_server.bodies["/big"] = b"x" * (edges.MAX_BYTES + 1)
    assert edges.fetch(lists_server.url + "/ok") == "192.0.2.1\n"
    with pytest.raises(edges.EdgeListError, match="more than"):
        edges.fetch(lists_server.url + "/big")
    with pytest.raises(edges.EdgeListError, match="404"):
        edges.fetch(lists_server.url + "/missing")


def fake_tools(tmp_path: Path, validate: int = 0) -> Path:
    """Fake caddy and systemctl on PATH that log their arguments to calls.log."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(parents=True)
    log = tmp_path / "calls.log"
    for name, args, code in (("caddy", "$* $XDG_DATA_HOME", validate), ("systemctl", "$*", 0)):
        f = bin_dir / name
        f.write_text(f'#!/bin/sh\necho "{name} {args}" >> {log}\nexit {code}\n')
        f.chmod(0o755)
    return bin_dir


def calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def run_main(tmp_path, monkeypatch, lists_server, *extra, validate=0):
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PATH", f"{fake_tools(tmp_path, validate)}:{os.environ['PATH']}")
    lists = tmp_path / "cdn-edge-lists"
    lists.write_text(f"{lists_server.url}/v4\n{lists_server.url}/v6\n")
    caddy = tmp_path / "caddy"
    return edges.main(["--caddy", str(caddy), "--lists", str(lists), *extra]), caddy / "edges"


def test_first_run_writes_both_files_validates_and_reloads(
    tmp_path, monkeypatch, lists_server, capsys
):
    lists_server.bodies["/v4"] = "\n".join(V4).encode()
    lists_server.bodies["/v6"] = ('["' + '", "'.join(V6) + '"]').encode()
    code, out = run_main(tmp_path, monkeypatch, lists_server)
    assert code == 0, capsys.readouterr().err
    assert sorted(p.name for p in out.iterdir()) == ["cdn-edges.caddy", "client-header.caddy"]
    want = edges.rendered(edges.checked(nets(*V4, *V6), 10))["cdn-edges.caddy"]
    assert (out / "cdn-edges.caddy").read_text() == want
    assert oct(out.stat().st_mode & 0o777) == "0o700"
    caddy = tmp_path / "caddy"
    assert calls(tmp_path) == [
        f"caddy validate --config {caddy}/Caddyfile --adapter caddyfile {caddy}/data",
        "systemctl --user try-reload-or-restart atlas-caddy",
    ]
    assert "14 ranges, Caddy reloaded" in capsys.readouterr().out
    # The same lists again: nothing written, validated or reloaded.
    lists = tmp_path / "cdn-edge-lists"
    assert edges.main(["--caddy", str(caddy), "--lists", str(lists)]) == 0
    assert len(calls(tmp_path)) == 2
    assert "14 ranges, unchanged" in capsys.readouterr().out


def test_a_failed_validation_puts_the_previous_files_back(
    tmp_path, monkeypatch, lists_server, capsys
):
    lists_server.bodies["/v4"] = "\n".join(V4).encode()
    lists_server.bodies["/v6"] = b""
    out = tmp_path / "caddy" / "edges"
    out.mkdir(parents=True)
    (out / "cdn-edges.caddy").write_text("old\n")
    code, _ = run_main(tmp_path, monkeypatch, lists_server, validate=1)
    assert code == 1
    assert "caddy validate failed" in capsys.readouterr().err
    assert (out / "cdn-edges.caddy").read_text() == "old\n"
    assert not (out / "client-header.caddy").exists()
    assert not any(c.startswith("systemctl") for c in calls(tmp_path))


def test_a_bad_list_changes_nothing(tmp_path, monkeypatch, lists_server, capsys):
    lists_server.bodies["/v4"] = "\n".join(V4).encode()
    lists_server.bodies["/v6"] = b"10.0.0.1\n"
    code, out = run_main(tmp_path, monkeypatch, lists_server)
    assert code == 1 and "no public address" in capsys.readouterr().err
    assert not out.exists() and calls(tmp_path) == []
    del lists_server.bodies["/v6"]
    code, out = run_main(tmp_path / "missing", monkeypatch, lists_server)
    assert code == 1 and "404" in capsys.readouterr().err
    assert not out.exists()


def test_dry_run_prints_the_ranges_only(tmp_path, monkeypatch, lists_server, capsys):
    lists_server.bodies["/v4"] = "\n".join(V4).encode()
    lists_server.bodies["/v6"] = b""
    code, out = run_main(tmp_path, monkeypatch, lists_server, "--dry-run")
    assert code == 0 and not out.exists() and calls(tmp_path) == []
    assert "trusted_proxies static 192.0.2.1/32" in capsys.readouterr().out
