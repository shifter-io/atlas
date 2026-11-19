"""scripts/site-check.sh (#1373) against a local fake API and website: every good case passes,
every bad one (a redirect loop, two hops, a redirect to itself, a 503, a body that is not JSON,
a page that says it is unavailable, no network list) fails the whole run."""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "site-check.sh"
COVERAGE = "/v1/networks/alpha/coverage?range=24h&page=1"
PAGES = ["/atlas", "/atlas/networks", "/atlas/networks/alpha", "/atlas/true-cost", "/de/atlas"]


class Fake(BaseHTTPRequestHandler):
    """Answers every path with 200; server.routes changes one path at a time:
    ("redirect", location), ("status", code), ("body", text) or ("self-once",), a redirect
    to the same URL on the first request and 200 after it."""

    def do_GET(self):
        route = self.server.routes.get(self.path)
        self.server.hits[self.path] = self.server.hits.get(self.path, 0) + 1
        if route and route[0] == "self-once" and self.server.hits[self.path] == 1:
            route = ("redirect", self.path)
        if route and route[0] == "redirect":
            return self.answer(301, "", location=route[1])
        if route and route[0] == "status":
            return self.answer(route[1], "<html>Service Unavailable</html>")
        if route and route[0] == "body":
            return self.answer(200, route[1])
        if self.path == "/v1/networks":
            slugs = self.server.slugs
            return self.answer(200, json.dumps({"networks": [{"slug": s} for s in slugs]}))
        if self.path.startswith("/v1/"):
            return self.answer(200, json.dumps({"path": self.path}))
        return self.answer(200, "<html><h1>Atlas</h1></html>")

    def answer(self, code, body, location=None):
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    srv.routes, srv.hits, srv.slugs = {}, {}, ["beta", "alpha"]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()
    srv.server_close()


def run(*args):
    proc = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout + proc.stderr


def test_every_url_good_passes(server):
    code, out = run(server.url, "24h 7d")
    assert code == 0, out
    # 9 fixed + 3 per range + per network (3 + 2 per range): 9 + 6 + 2 * 7.
    assert "2 networks, 29 URLs (29 API, 0 pages): 29 pass, 0 FAIL, 0 redirected" in out
    assert "| every URL: pass, no redirect | | | | |" in out


def test_one_redirect_hop_to_another_url_passes(server):
    server.routes["/v1/version"] = ("redirect", "/v1/version-now")
    code, out = run(server.url, "24h")
    assert code == 0, out
    assert "| pass, 1 redirect | 200 | 1 |" in out
    assert "0 FAIL, 1 redirected" in out


@pytest.mark.parametrize(
    ("routes", "row"),
    [
        ({COVERAGE: ("redirect", COVERAGE)}, "| FAIL redirect loop | 301 | 5 |"),
        (
            {COVERAGE: ("redirect", "/hop-1"), "/hop-1": ("redirect", "/v1/home")},
            "| FAIL 2 redirect hops | 200 | 2 |",
        ),
        ({COVERAGE: ("self-once",)}, "| FAIL redirect to itself | 200 | 1 |"),
        ({COVERAGE: ("status", 503)}, "| FAIL HTTP 503 | 503 | 0 |"),
        ({COVERAGE: ("body", "<html>Service Unavailable</html>")}, "| FAIL not JSON | 200 | 0 |"),
    ],
    ids=["redirect-loop", "two-hops", "redirect-to-itself", "http-503", "not-json"],
)
def test_one_bad_api_url_fails_the_run(server, routes, row):
    server.routes.update(routes)
    code, out = run(server.url, "24h")
    assert code == 1, out
    assert "1 FAIL" in out
    line = next(x for x in out.splitlines() if x.startswith("| FAIL"))
    assert line.startswith(row) and line.endswith(f"{server.url}{COVERAGE} |"), out


def test_site_pages_are_requested_with_the_first_network(server):
    code, out = run("--site", server.url, server.url, "24h")
    assert code == 0, out
    assert "+ " + server.url in out and "(22 API, 5 pages): 27 pass, 0 FAIL" in out
    assert all(server.hits.get(p) == 1 for p in PAGES), server.hits


@pytest.mark.parametrize(
    ("route", "row"),
    [
        (
            ("body", "<p>Atlas is Temporarily Unavailable</p>"),
            "| FAIL unavailable page | 200 | 0 |",
        ),
        (("status", 503), "| FAIL HTTP 503 | 503 | 0 |"),
        (("redirect", "/atlas/networks/alpha"), "| FAIL redirect loop | 301 | 5 |"),
    ],
    ids=["unavailable-text", "http-503", "redirect-loop"],
)
def test_one_bad_page_fails_the_run(server, route, row):
    server.routes["/atlas/networks/alpha"] = route
    code, out = run("--site", server.url, "--full", server.url, "24h")
    assert code == 1, out
    assert f"{row} " in out and "26 pass, 1 FAIL" in out
    assert out.count("| pass |") == 26


def test_no_network_list_fails(server):
    server.routes["/v1/networks"] = ("status", 503)
    code, out = run(server.url)
    assert code == 1
    assert f"FAIL: {server.url}/v1/networks gave no network list" in out


def test_list_expands_every_network_and_range(server):
    server.slugs = [f"net-{i:02d}" for i in range(18)]
    code, out = run("--list", server.url)
    assert code == 0, out
    urls = out.splitlines()
    # The operators' list before this script: 9 fixed, 3 per range, 9 per network (3 ranges).
    assert len(urls) == 180 and all(u.startswith(f"api {server.url}/v1/") for u in urls)
    assert f"api {server.url}/v1/networks/net-17/coverage?range=30d&page=1" in urls
    assert server.hits == {"/v1/networks": 1}


def test_usage_errors_exit_2():
    assert run("--bogus")[0] == 2
    assert run("--site")[0] == 2
    assert run("a", "b", "c")[0] == 2
