"""The calculated layer's per-worker template: a copy of `database_template` (test_storage) with
the calculated schema initialised once, and `calculated_database`, a fresh copy of it per test.

A test module that needs the current calculated schema lists this module in its
`pytest_plugins`, after "test_storage", whose fixtures these build on.
"""

import pytest
from helpers import drop_databases, scratch_database
from test_storage import admin_credentials, credentials_file

from proxy_index.calculated import store


@pytest.fixture(scope="session")
def calculated_template(database_template, tmp_path_factory):
    """This worker's initialised history and calculated schemas; each `calculated_database` is a
    copy of it."""
    admin = admin_credentials()
    name = scratch_database(admin, template=database_template)
    try:
        store.initialize(credentials_file(admin, name, tmp_path_factory.mktemp("calculated")))
        yield name
    finally:
        drop_databases(admin, [name])


@pytest.fixture
def calculated_database(databases, calculated_template):
    """A fresh database with the current history and calculated schemas, from the template."""
    return databases(template=calculated_template)
