"""REFUSAL-RECORD-1: redacted samples of refused answers, kept beside a run's counters, never in
them (atlas/refusals.py)."""

import base64
import gzip
import json
import uuid
from datetime import UTC, date, datetime

import pytest
from helpers import Gateway, server

from atlas import JsonSink, ProxyConfig, RunConfig, RunOptions, refusals, run
from atlas.archive.rawfiles import spool
from atlas.cli import main
from atlas.daily import ingest as daily_ingest
from atlas.models import Outcome
from atlas.onboarding.writer import SummarySink
from atlas.targets import EchoIpTarget, RequestSpec, TargetRegistry
from atlas.transport import HttpTransport, _leg

USER, PASSWORD = "user-zone-a-session-42", "hunter2-secret"
TOKEN = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
HOUR = datetime(2026, 11, 20, 10, tzinfo=UTC)


def read_rows(path):
    with gzip.open(path, "rt", encoding="utf-8") as lines:
        return [json.loads(line) for line in lines]


def test_the_quota_takes_a_few_of_each_kind_and_a_few_kinds():
    quota = refusals.Quota(per_kind=2, kinds=2)
    kinds = [("plain", 429)] * 3 + [("connect", 407), ("tunnel", 403)]
    taken = [quota.take(kind) for kind in kinds]
    assert taken == [True, True, False, True, False]
    assert refusals.Quota().per_kind == refusals.SAMPLES_PER_KIND == 3


def test_a_sample_keeps_identifying_headers_and_redacts_credentials():
    headers = [
        (b"Server", b"edge-proxy/1.2"),
        (b"Via", b"1.1 gateway"),
        (b"CF-RAY", b"8a1b2c3d4e5f-AMS"),
        (b"Retry-After", b"30"),
        (b"X-RateLimit-Remaining", b"0"),
        (b"Proxy-Authenticate", b'Basic realm="proxy"'),
        (b"Proxy-Authorization", f"Basic {TOKEN}".encode()),
        (b"Authorization", b"Bearer abcdefghijklmnop"),
        (b"Cookie", b"sid=abc"),
        (b"Set-Cookie", b"__edge_bm=value123; Path=/; HttpOnly"),
        (b"X-Session-Id", b"s-1"),
        (b"X-Upstream-Token", b"t-1"),
        (b"X-Proxy-User", USER.encode()),
    ]
    body = f"denied for {USER} ({PASSWORD}) Authorization: Basic {TOKEN} Bearer abcdefghijklmnop"
    secrets = (f"Basic {TOKEN}", TOKEN, USER, PASSWORD)
    sample = refusals.capture("plain", 429, headers, body.encode(), secrets)
    kept = dict(sample["headers"])
    assert kept["Server"] == "edge-proxy/1.2" and kept["CF-RAY"] == "8a1b2c3d4e5f-AMS"
    assert kept["Via"] == "1.1 gateway" and kept["Retry-After"] == "30"
    assert kept["X-RateLimit-Remaining"] == "0"
    assert kept["Proxy-Authenticate"] == 'Basic realm="proxy"'  # a challenge, not a credential
    for name in ("Proxy-Authorization", "Authorization", "Cookie", "X-Session-Id"):
        assert kept[name] == refusals.REDACTED
    assert kept["X-Upstream-Token"] == refusals.REDACTED
    assert kept["Set-Cookie"] == "__edge_bm=[redacted]; Path=/; HttpOnly"
    assert kept["X-Proxy-User"] == refusals.REDACTED
    text = json.dumps(sample)
    for secret in (USER, PASSWORD, TOKEN, "abcdefghijklmnop", "value123", "sid=abc"):
        assert secret not in text
    assert sample["body"].startswith("denied for [redacted] ([redacted])")
    assert (sample["leg"], sample["status"], sample["body_bytes"]) == ("plain", 429, len(body))
    assert datetime.fromisoformat(sample["captured_at"]).tzinfo is not None


def test_a_sample_is_cut_to_size_and_printable():
    many = [(f"X-H{n}", "v" * 5000) for n in range(100)]
    # A password across the 2 KB cut: scrubbed over the whole body first, so no half is kept.
    body = b"a" * (refusals.BODY_BYTES - 5) + PASSWORD.encode() + b"\x00\xff" + b"z" * 5000
    sample = refusals.capture("tunnel", 403, many, body, [PASSWORD])
    assert len(sample["headers"]) == refusals.MAX_HEADERS
    assert all(len(value) == refusals.HEADER_CHARS for _, value in sample["headers"])
    assert len(sample["body"].encode()) <= refusals.BODY_BYTES
    assert "hunt" not in sample["body"] and "\x00" not in sample["body"]
    assert sample["body_bytes"] == len(body)
    mapping = refusals.capture("connect", 407, {"Server": "gw"}, b"", [])
    assert mapping["headers"] == [["Server", "gw"]] and mapping["body"] == ""
    assert refusals.capture("connect", 502, None, b"", [])["headers"] == []
    odd = refusals.capture("plain", 500, [("X-A", "\udcff\x00b")], b"\xff\xfe", [])
    json.dumps(odd, ensure_ascii=False).encode("utf-8")  # no lone surrogate survives
    assert odd["headers"] == [["X-A", "?b"]]


def test_a_run_keeps_its_own_quota_and_the_run_identifiers():
    sample = refusals.capture("plain", 429, [], b"x", [])
    collected = refusals.Refusals()
    for _ in range(5):
        collected.add(Outcome(1.0, error="http_status", status=429, refusal=sample))
    collected.add(Outcome(1.0, ip="203.0.113.8"))  # a success: nothing to keep
    assert len(collected.samples) == refusals.SAMPLES_PER_KIND
    config = RunConfig("generic_http", ProxyConfig("http", "127.0.0.1", 1), provider="net-a")
    rows = collected.rows("run-1", config)
    assert rows[0]["run_id"] == "run-1" and rows[0]["network"] == "net-a"
    assert rows[0]["error"] == "http_status" and rows[0]["exit_ip"] is None
    assert rows[0]["target"] == "generic_http" and rows[0]["schema_version"] == "1"


def test_no_file_without_samples(tmp_path):
    assert refusals.write(tmp_path, []) is None
    assert not list(tmp_path.iterdir())


def _spooled_run(tmp_path, name, rows, network="net-a"):
    run_dir = tmp_path / name
    run_dir.mkdir()
    (run_dir / "summary.json").write_text("{}")
    refusals.write(run_dir, rows)
    spool(tmp_path / "spool", HOUR, network, f"pulse-b1-{uuid.uuid4()}.tar.gz", run_dir, move=False)


def test_the_operator_reads_the_spooled_samples(tmp_path, capsys):
    _spooled_run(tmp_path, "a", [{"status": 429, "network": "net-a"}, {"status": 403}])
    _spooled_run(tmp_path, "b", [{"status": 429, "network": "net-b"}], network="net-b")
    _spooled_run(tmp_path, "c", [])  # a run without refusals has no file
    root, day = tmp_path / "spool", HOUR.date()
    found = list(refusals.spooled(root, day, day))
    assert sorted(row["status"] for row in found) == [403, 429, 429]
    assert all(row["file"].startswith("pulse-b1-") for row in found)
    assert [r["network"] for r in refusals.spooled(root, day, day, "net-b")] == ["net-b"]
    assert len(list(refusals.spooled(root, day, day, status=429))) == 2
    assert list(refusals.spooled(root, date(2026, 11, 21), date(2026, 11, 22))) == []
    args = ["archive", "refusals", "--spool", str(root), "--from", "2026-11-20"]
    assert main([*args, "--to", "2026-11-20", "--status", "403"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["status"] for line in lines] == [403]


def refusing_gateway(seen):
    """A plain-HTTP gateway: every even connection 429 with identifying headers (the body echoes
    the request's login), every odd one an exit IP."""

    async def handler(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        seen.append(request)
        if len(seen) % 2:
            body = b"203.0.113.8\n"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\nConnection: close\r\n\r\n")
        else:
            body = b"slow down " + request[:300]
            writer.write(
                b"HTTP/1.1 429 Too Many\r\nServer: edge/9\r\nRetry-After: 7\r\n"
                b"Set-Cookie: bm=opaque; Path=/\r\nConnection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
            )
        writer.write(body)
        await writer.drain()

    return handler


def _counters(summary):
    keys = ("completed", "successes", "errors", "status_codes", "unique_ips", "blocks")
    found = {key: getattr(summary, key) for key in keys}
    found["bytes"] = (summary.bytes_up, summary.bytes_down, summary.response_body_bytes)
    return found


async def _plain_run(tmp_path, name):
    seen = []
    async with server(refusing_gateway(seen)) as port:
        proxy = ProxyConfig("http", "127.0.0.1", port, USER, PASSWORD)
        config = RunConfig("generic_http", proxy, requests=10, concurrency=1, provider="net-a")
        return await run(config, JsonSink(tmp_path / name))


async def test_a_run_keeps_redacted_samples_and_the_same_counts(tmp_path, monkeypatch):
    summary = await _plain_run(tmp_path, "kept")
    rows = read_rows(tmp_path / "kept" / summary.run_id / refusals.FILE)
    assert len(rows) == refusals.SAMPLES_PER_KIND
    row = rows[0]
    found = (row["leg"], row["status"], row["error"], row["network"])
    assert found == ("plain", 429, "http_status", "net-a")
    headers = dict(row["headers"])
    assert headers["Server"] == "edge/9" and headers["Retry-After"] == "7"
    assert headers["Set-Cookie"] == "bm=[redacted]; Path=/"
    assert row["body"].startswith("slow down GET http://") and row["run_id"] == summary.run_id
    text = json.dumps(rows)
    assert USER not in text and PASSWORD not in text and TOKEN not in text
    # The summary is the run's own: no new key, the file is not an artifact.
    assert set(summary.artifacts) == {"ips"} and "refusals" not in summary.to_dict()
    # The same run without capturing counts exactly the same.
    monkeypatch.setattr(HttpTransport, "_refusal", lambda *args: None)
    bare = await _plain_run(tmp_path, "bare")
    assert not (tmp_path / "bare" / bare.run_id / refusals.FILE).exists()
    assert _counters(bare) == _counters(summary)
    assert summary.successes == 5 and summary.status_codes == {"200": 5, "429": 5}


async def test_a_gateway_refusing_the_connect_is_sampled_as_connect(tmp_path):
    targets = TargetRegistry()
    targets.register(EchoIpTarget("generic_https", "https://localhost:9/"))
    async with server(Gateway(auth=True)) as port:  # our login is not the gateway's: 407
        proxy = ProxyConfig("http", "127.0.0.1", port, USER, PASSWORD)
        config = RunConfig("generic_https", proxy, requests=4, concurrency=1)
        summary = await run(config, JsonSink(tmp_path), RunOptions(targets=targets))
    assert summary.errors == {"proxy_auth": 4}
    rows = read_rows(tmp_path / summary.run_id / refusals.FILE)
    kinds = [(row["leg"], row["status"], row["error"]) for row in rows]
    assert kinds == [("connect", 407, "proxy_auth")] * refusals.SAMPLES_PER_KIND
    assert rows[0]["body"] == "" and ["Content-Length", "0"] in rows[0]["headers"]


async def test_a_sink_without_the_hook_keeps_no_samples(tmp_path):
    sink = SummarySink()
    sink.start("r")
    assert sink.refused == []
    sink.refusals([{"status": 429}])  # the daily scheduler keeps them in its summary.json
    assert sink.refused == [{"status": 429}]
    seen = []

    class Plain:  # the ResultSink protocol only
        def __init__(self):
            self.inner = JsonSink(tmp_path)

        def __getattr__(self, name):
            if name == "refusals":
                raise AttributeError(name)
            return getattr(self.inner, name)

    async with server(refusing_gateway(seen)) as port:
        config = RunConfig("generic_http", ProxyConfig("http", "127.0.0.1", port), requests=4)
        summary = await run(config, Plain())
    assert summary.status_codes["429"] == 2
    assert not (tmp_path / summary.run_id / refusals.FILE).exists()


def test_a_daily_country_carries_its_samples_into_its_run(tmp_path):
    folder = tmp_path / "ca"
    folder.mkdir()
    measured = {"run_id": "r1", "schema_version": "1"}
    rows = [{"run_id": "r1", "status": 429, "leg": "tunnel"}]
    (folder / "summary.json").write_text(json.dumps({"measurement": measured, "refusals": rows}))
    directory = daily_ingest.convert_country(folder, tmp_path / "runs")
    assert read_rows(directory / refusals.FILE) == rows
    assert "refusals" not in json.loads((directory / "summary.json").read_text())
    other = tmp_path / "pm"
    other.mkdir()
    (other / "summary.json").write_text(json.dumps({"measurement": measured | {"run_id": "r2"}}))
    assert not (daily_ingest.convert_country(other, tmp_path / "runs") / refusals.FILE).exists()


@pytest.mark.parametrize(
    "scheme,url,leg",
    [
        ("http", "http://x/", "plain"),
        ("http", "https://x/", "tunnel"),
        ("socks5", "http://x/", "tunnel"),
    ],
)
def test_how_an_answer_arrived(scheme, url, leg):
    assert _leg(RequestSpec(url), ProxyConfig(scheme, "127.0.0.1", 1)) == leg
