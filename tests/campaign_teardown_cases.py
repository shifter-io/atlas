"""Shared fixtures of the manager teardown tests (fakes only).

The test modules load it with `pytest_plugins` and apply `quick_assign` with `usefixtures`, so
it holds no autouse fixture: a plugin's autouse fixture would apply to every test module.
"""

import pytest
from campaign_deploy_fakes import fast_assign, make_job


@pytest.fixture
def quick_assign(monkeypatch):
    fast_assign(monkeypatch)


@pytest.fixture
def job(tmp_path):
    return make_job(tmp_path)
