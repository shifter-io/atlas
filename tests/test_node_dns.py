"""PULSE-NODE-DNS-1: a node's own DNS failures keep their texts and resolver, and a pulse run they
dominate is retried once on the node, then reported failed instead of uploaded."""

import json
import socket
from types import SimpleNamespace

from helpers import FakeTransport
from test_cov_core_node import assignment, make_agent

from atlas import JsonSink, ProxyConfig, RunConfig, RunOptions, run
from atlas import node_dns as node_dns_module
from atlas.models import Outcome
from atlas.node import Active
from atlas.node_dns import DnsFailures, failure_text, local_fault, resolver

TEXT = "Cannot connect to host gateway.test:8080 ssl:default [Name or service not known]"


def fake_lookup(monkeypatch):
    """getaddrinfo fails for gateway.test only, as a droplet's broken resolver does."""
    real = socket.getaddrinfo

    def lookup(host, *args, **kwargs):
        if host == "gateway.test":
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", lookup)


async def test_a_dns_failure_keeps_its_exact_text_and_the_resolver(tmp_path, monkeypatch):
    fake_lookup(monkeypatch)
    proxy = ProxyConfig("http", "gateway.test", 8080, username="user", password="hunter2")
    summary = await run(RunConfig("generic_http", proxy, requests=3), JsonSink(tmp_path))
    assert summary.errors == {"dns": 3} and summary.proxy_errors == 3
    failures = summary.to_dict()["dns_failures"]
    [(text, count)] = failures["texts"].items()
    assert count == 3 and "gateway.test:8080" in text and f"errno {socket.EAI_NONAME}" in text
    assert "hunter2" not in json.dumps(failures) and "user" not in text
    assert failures["other"] == 0 and failures["resolver"]["resolver"]
    stored = json.loads(next(tmp_path.glob("*/summary.json")).read_text())
    assert stored["dns_failures"]["texts"] == {text: 3}


async def test_a_run_without_dns_failures_has_no_dns_keys(tmp_path):
    proxy = ProxyConfig("http", "127.0.0.1", 9)
    options = RunOptions(transport_factory=FakeTransport)
    summary = await run(RunConfig("generic_http", proxy, requests=2), JsonSink(tmp_path), options)
    assert summary.dns_failures is None
    assert "dns_failures" not in summary.to_dict() and "dns_retry" not in summary.to_dict()


def test_the_resolver_settings_are_read_from_resolv_conf(tmp_path):
    target = tmp_path / "stub-resolv.conf"
    target.write_text(
        "# comment\nnameserver 10.0.0.2\nnameserver\noptions edns0 trust-ad\nsearch .\n"
    )
    link = tmp_path / "resolv.conf"
    link.symlink_to(target)
    found = resolver(link)
    assert found["nameservers"] == ["10.0.0.2"] and found["options"] == ["edns0", "trust-ad"]
    assert found["resolv_conf"] == str(target) and found["resolver"]
    assert resolver(tmp_path / "missing")["resolv_conf_error"] == "FileNotFoundError"


def test_only_the_first_distinct_texts_are_kept():
    failures = DnsFailures()
    for index in range(node_dns_module.MAX_TEXTS + 2):
        failures.add(f"text {index}")
    failures.add("text 0")
    failures.add(None)
    report = failures.report()
    assert len(report["texts"]) == node_dns_module.MAX_TEXTS and report["other"] == 2
    assert report["texts"]["text 0"] == 2 and DnsFailures().report() is None


def test_a_run_is_the_nodes_fault_from_a_fifth_of_its_requests():
    def summary(dns, completed=100):
        return SimpleNamespace(completed=completed, errors={"dns": dns} if dns else {})

    assert local_fault(summary(20)) and local_fault(summary(50))
    assert not local_fault(summary(19)) and not local_fault(summary(0))
    assert not local_fault(summary(0, completed=0))


def test_the_failure_text_fits_the_orchestrators_error_column():
    texts = {f"{TEXT} {index}" * 2: 10 for index in range(5)}
    resolver_found = {"resolver": "ThreadedResolver", "nameservers": ["10.0.0.2"]}
    summary = SimpleNamespace(
        completed=100,
        errors={"dns": 50, "timeout": 50},
        dns_failures={"texts": texts, "other": 0, "resolver": resolver_found},
    )
    text = failure_text(summary, {"requests": 100, "dns": 25})
    assert text.startswith("node_dns {") and len(text) <= node_dns_module.ERROR_CHARS
    reported = json.loads(text.removeprefix("node_dns "))
    assert reported["dns"] == 50 and reported["first"] == {"requests": 100, "dns": 25}
    assert reported["resolver"] == resolver_found and 0 < len(reported["texts"]) < 5
    huge = SimpleNamespace(completed=1, errors={}, dns_failures={"resolver": {"x": "y" * 600}})
    assert len(failure_text(huge)) == node_dns_module.ERROR_CHARS


def dns_transport(failing_runs):
    """A transport whose first `failing_runs` runs fail every request on the node's DNS."""
    runs = []

    class Transport(FakeTransport):
        def __init__(self, config):
            super().__init__(config)
            runs.append(self)

        async def execute(self, target, *, context=None):
            if len(runs) <= failing_runs:
                self.count += 1
                return Outcome(3.0, error="dns", detail=TEXT)
            return await super().execute(target, context=context)

    return Transport, runs


def node_resolver(monkeypatch, works):
    """getaddrinfo as a node whose resolver works (every name resolves) or not (none does);
    returns the names looked up."""
    looked = []

    def lookup(host, *args, **kwargs):
        looked.append(host)
        if not works:
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    return looked


async def test_a_dns_failed_pulse_is_retried_then_reported_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(node_dns_module, "RETRY_SECONDS", 0)
    looked = node_resolver(monkeypatch, works=False)
    transport, runs = dns_transport(failing_runs=2)
    agent = make_agent(tmp_path, transport_factory=transport)
    uploaded = []

    async def upload(batch_id, directory):
        uploaded.append(batch_id)

    agent.upload = upload
    await agent.execute(Active(assignment()))
    assert len(runs) == 2 and uploaded == []
    [(_, _, body)] = [call for call in agent._call.calls if call[1] == "/v1/failed"]
    assert body["batch_id"] == 7 and body["error"].startswith("node_dns ")
    reported = json.loads(body["error"].removeprefix("node_dns "))
    assert reported["dns"] == 3 and reported["first"] == {"requests": 3, "dns": 3}
    assert reported["texts"] == {TEXT: 3} and "resolver" in reported["resolver"]
    # The node's resolver failed the control names too: the target's host, the orchestrator's.
    assert looked[-1] == "orchestrator" and len(looked) == 2
    assert reported["control"] == {name: f"gaierror {socket.EAI_AGAIN}" for name in looked}
    assert list(tmp_path.iterdir()) == []  # neither attempt is kept or uploaded


async def test_a_gateway_name_failing_alone_is_the_networks_data(tmp_path, monkeypatch, caplog):
    """Every node fails the gateway's name (its zone or domain broke) while the node's resolver
    answers the control names: the retry is uploaded with its dns errors, never node_dns."""
    monkeypatch.setattr(node_dns_module, "RETRY_SECONDS", 0)
    looked = node_resolver(monkeypatch, works=True)
    transport, runs = dns_transport(failing_runs=2)
    agent = make_agent(tmp_path, transport_factory=transport)
    uploaded = []

    async def upload(batch_id, directory):
        uploaded.append(json.loads((directory / "summary.json").read_text()))

    agent.upload = upload
    await agent.execute(Active(assignment()))
    assert len(runs) == 2 and "/v1/failed" not in agent._call.paths()
    [summary] = uploaded
    assert summary["errors"] == {"dns": 3} and summary["proxy_errors"] == 3
    assert summary["dns_failures"]["texts"] == {TEXT: 3} and summary["dns_retry"]["dns"] == 3
    assert len(looked) == 1  # the first control name answered
    assert "only the gateway's name fails" in caplog.text


def test_the_control_names_are_the_urls_host_names():
    urls = ("https://echo.test/ip", "http://orchestrator:8080", "https://echo.test/x")
    assert node_dns_module.control_names(*urls) == ["echo.test", "orchestrator"]
    ip_only = ("https://192.0.2.7:8443", "http://[2001:db8::1]/", None, "not a url")
    assert node_dns_module.control_names(*ip_only) == []


async def test_a_control_lookup_that_hangs_has_failed(monkeypatch):
    import time

    monkeypatch.setattr(node_dns_module, "CONTROL_SECONDS", 0.05)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: time.sleep(0.5))
    assert await node_dns_module.control(["echo.test"]) == {"echo.test": "TimeoutError None"}
    assert await node_dns_module.control([]) == {}  # nothing to look up: the node's fault


async def test_a_retry_that_gets_through_is_uploaded_with_the_first_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(node_dns_module, "RETRY_SECONDS", 0)
    transport, runs = dns_transport(failing_runs=1)
    agent = make_agent(tmp_path, transport_factory=transport)
    uploaded = []

    async def upload(batch_id, directory):
        uploaded.append(json.loads((directory / "summary.json").read_text()))

    agent.upload = upload
    deadlines, run_options = [], agent.run_options

    def counted(given):
        deadlines.append(run_options(given)["deadline"])
        return {"deadline": deadlines[-1]}

    agent.run_options = counted
    await agent.execute(Active(assignment()))
    # The retry's pulse deadline counts from its own start, not the first attempt's.
    assert len(deadlines) == 2 and deadlines[1] >= deadlines[0]
    assert len(runs) == 2 and "/v1/failed" not in agent._call.paths()
    [summary] = uploaded
    assert summary["successes"] == 3 and "dns_failures" not in summary
    retry = summary["dns_retry"]
    assert retry["requests"] == 3 and retry["dns"] == 3
    assert retry["dns_failures"]["texts"] == {TEXT: 3} and retry["run_id"] != summary["run_id"]
    assert len(list(tmp_path.iterdir())) == 1  # the first attempt's directory is gone


async def test_only_pulse_runs_are_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(node_dns_module, "RETRY_SECONDS", 0)
    transport, runs = dns_transport(failing_runs=1)
    agent = make_agent(tmp_path, transport_factory=transport)
    uploaded = []

    async def upload(batch_id, directory):
        uploaded.append(json.loads((directory / "summary.json").read_text()))

    agent.upload = upload
    await agent.execute(Active(assignment(kind="census")))
    assert len(runs) == 1 and uploaded[0]["errors"] == {"dns": 3}
    assert uploaded[0]["dns_failures"]["texts"] == {TEXT: 3}


def test_the_detail_is_the_errors_text_and_number():
    import aiohttp

    key = SimpleNamespace(host="gateway.test", port=8080, ssl=True)
    lookup = OSError(-3, "Temporary failure in name resolution")
    error = aiohttp.ClientConnectorDNSError(key, lookup)
    text = node_dns_module.detail(error)
    assert "gateway.test:8080" in text and "Temporary failure" in text
    assert text.endswith("(errno -3)")
    assert node_dns_module.detail(ValueError("x" * 300)) == "x" * node_dns_module.TEXT_CHARS
