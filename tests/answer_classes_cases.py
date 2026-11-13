"""Shared helpers of the answer-class tests (test_answer_classes.py): run configs, scripted
transports and outcomes, a fake country lookup and a dropping loopback gateway."""

from helpers import FakeTransport

from atlas import ProxyConfig, RunConfig
from atlas.models import AnswerClasses, Outcome

GEONODE = AnswerClasses(("429", "469"), ("465",))
CONNECT = AnswerClasses(("429", "proxy_connect"), ())
PLAIN = AnswerClasses(("429",), ())


def config(**overrides):
    return RunConfig(
        "generic_http",
        ProxyConfig("http", "127.0.0.1", 8080),
        **({"concurrency": 1, "answer_classes": PLAIN} | overrides),
    )


def census(**overrides):
    return config(**({"requests": None, "stop_rule": "unique_ratio", "min_sample": 10} | overrides))


def scripted(outcomes):
    """A transport answering `outcomes` in order (a callable gets the 1-based request number)."""

    class Scripted(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            if callable(outcomes):
                return outcomes(self.count)
            return outcomes[self.count - 1]

    return Scripted


class Countries:
    """A fake MMDB: 192.0.2.x is in the US, 198.51.100.x in DE, anything else unmatched."""

    release = {"sha256": "0" * 64}

    def lookup(self, address):
        code = (
            "US"
            if address.startswith("192.0.2.")
            else "DE"
            if address.startswith("198.51.100.")
            else None
        )
        return {"matched": code is not None, "country_code": code}


def ok(ip, at=None):
    return Outcome(1, ip=ip, status=200, completed_at=at)


def limited(status=429, at=None):
    return Outcome(1, error="http_status", status=status, bytes_up=5, completed_at=at)


def refused(at):
    return Outcome(1, error="proxy_connect", completed_at=at)


def timeout(at=None):
    return Outcome(1, error="timeout", completed_at=at)


def dropping_gateway(served, *, after=0):
    """A loopback HTTP gateway that drops every third connection after the first `after`, as
    DataImpulse's gateway does under its account connection limit (plain HTTP has no CONNECT)."""

    async def gateway(reader, writer):
        served["connections"] += 1
        if served["connections"] > after and served["connections"] % 3 == 0:
            return  # accepted, then closed before any answer
        await reader.readuntil(b"\r\n\r\n")
        body = f"192.0.2.{served['connections'] % 250 + 1}\n".encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
        await writer.drain()

    return gateway
