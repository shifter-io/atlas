"""COV-2: `manager enable` passes on a firewall refusal other than DigitalOcean's 422 (a missing
pulse tag) unchanged, and records nothing (fakes only, no live calls)."""

from __future__ import annotations

import pytest
from campaign_deploy_fakes import fast_assign, inventory, make_job
from campaign_fakes import Fault, running
from manager_orch_cases import Shell, deployed, go_inputs, manager_firewall, recorded_size

from proxy_index.campaign import manager_orch
from proxy_index.campaign.do_api import ApiError


@pytest.fixture(autouse=True)
def quick_assign(monkeypatch):
    fast_assign(monkeypatch)


async def test_a_forbidden_firewall_update_is_raised_as_is_and_nothing_is_recorded(tmp_path):
    job = make_job(tmp_path, account="proxy-index")
    fake = await deployed(job)
    await recorded_size(fake, job)
    firewall_id = inventory(job)["manager_firewall_id"]
    fake.faults.append(Fault("PUT", f"/v2/firewalls/{firewall_id}", 403))
    before, shell = inventory(job), Shell()
    async with running(fake) as cloud:
        with pytest.raises(ApiError) as raised:
            await manager_orch.enable(
                cloud, job.inventory, go_inputs(tmp_path), apply=True, shell=shell
            )
    assert type(raised.value) is ApiError  # never turned into the missing-tag DeployError
    assert (raised.value.method, raised.value.status) == ("PUT", 403)
    assert inventory(job) == before and shell.calls == []
    assert "node_api_port" not in inventory(job) and "browser_port" not in inventory(job)
    assert len(manager_firewall(fake, job)["inbound_rules"]) == 1
