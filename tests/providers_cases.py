"""Shared cases of the Shifter provider tests: the fixture credentials and the proxy helper."""

from pathlib import Path

from proxy_index import GrammarProxy, RequestContext

CONTEXT = RequestContext("run-one", 0, 0)
CONFIG = Path(__file__).parents[1] / "configs" / "shifter.toml"
ENV = {"SHIFTER_ACCOUNT": "fixture_33", "SHIFTER_PASSWORD": "private-password"}


def shifter(**kwargs):
    return GrammarProxy("shifter", "fixture_33", "private-password", **kwargs)
