"""Shared helpers of the runner tests: the run configuration every test starts from."""

from proxy_index import ProxyConfig, RunConfig


def config(**kwargs):
    return RunConfig("generic_http", ProxyConfig("http", "localhost", 8080), **kwargs)
