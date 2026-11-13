"""Shared helpers of the staging manager tests: tags, scopes and the staging deploy job.

Staging has its own tags, firewalls and keys (`pi-camp-stg-`); production's names are
unchanged. Fakes only.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from campaign_deploy_fakes import WORKER_PUBLIC, Shell, fast_assign
from campaign_fakes import CAMPAIGN_PROJECT, CID, MANAGER_ID, SID, SPEC_TIME

from atlas.campaign.contracts import WORKER_TAG, Scope

STAGING_WORKER_TAG, STAGING_MANAGER_TAG = "pi-camp-stg-worker", "pi-camp-stg-manager"
STAGING_MAC = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIStagingMacKeyForTests pi-camp-stg-mac"
STAGING_WORKER = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIStagingWorkerKeyForTests"
NAME = f"pic-{SID}-h03-lon1-shifter"
STAGING_OURS = [STAGING_WORKER_TAG, f"pi-camp-{SID}", f"pi-camp-{SID}-h03"]
PRODUCTION_OURS = [WORKER_TAG, f"pi-camp-{SID}", f"pi-camp-{SID}-h03"]
FIREWALL = "33333333-3333-4333-8333-333333333333"
DAY = datetime(2027, 1, 1, 21, 55, tzinfo=UTC)


@pytest.fixture(autouse=True)
def quick_assign(monkeypatch):
    fast_assign(monkeypatch)


def scope_of(environment: str) -> Scope:
    return Scope(
        CID, SID, CAMPAIGN_PROJECT, MANAGER_ID, datetime.fromisoformat(SPEC_TIME), environment
    )


class StagingShell(Shell):
    """The staging manager generates its own worker key on its own disk."""

    async def __call__(self, argv, stdin):
        code, out = await super().__call__(argv, stdin)
        return code, out.replace(WORKER_PUBLIC, STAGING_WORKER)


def staging_job(production):
    """The staging deploy: its own inventory beside production's and its own Mac key."""
    key = production.mac_key.with_name("proxy-index-campaign-staging")
    key.write_text("private part, never read by deploy\n")
    key.with_name(key.name + ".pub").write_text(STAGING_MAC + "\n")
    staging = production.inventory.with_name("campaign-manager-staging.json")
    return replace(production, inventory=staging, mac_key=key, environment="staging")
