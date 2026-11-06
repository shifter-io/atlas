"""Shared helpers of the CAMP-1k unassigned worker tests: the hour's worker, specs and audits.

Offline: the fake DigitalOcean only.
"""

from __future__ import annotations

import json
from datetime import datetime

from campaign_fakes import CAMPAIGN_PROJECT, CID, MANAGER_ID, SID, SPEC_TIME
from campaign_hour_cases import NETWORKS, make_spec

from proxy_index.campaign import scope
from proxy_index.campaign.contracts import WORKER_TAG, DropletRecord, Scope

FIRST = "700001"  # the fake's first droplet id: the hour's only worker
LATE = datetime.fromisoformat("2027-01-01T22:16:00+00:00").timestamp()  # past HH:15
TRIES = 5  # one `request` call: the first try and four backoff retries
READS = 1 + len(scope.SETTLE_DELAYS)  # the fresh reads of our own new droplet (CAMP-1l)
NO_WAIT = scope.Retry(delay=0, settle=(0.0,) * len(scope.SETTLE_DELAYS))
SCOPE = Scope(CID, SID, CAMPAIGN_PROJECT, MANAGER_ID, datetime.fromisoformat(SPEC_TIME))
OURS = [WORKER_TAG, f"pi-camp-{SID}", f"pi-camp-{SID}-h03"]
NAME = f"pic-{SID}-h03-nyc3-shifter"


def audit_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def nyc3_spec(**changes):
    return make_spec(NETWORKS[:1], ("nyc3",), **changes)


def no_replacement_spec():
    """The replacement window counts from the launch (CAMP-1l): a zero window replaces none."""
    return nyc3_spec(replace_until_minutes=0)


async def created(cloud, name=NAME):
    [droplet] = await cloud.create(
        [name], region="nyc3", size="c-4", image="1", tags=OURS, ssh_keys=["9"]
    )
    return droplet, {droplet.id: DropletRecord("shifter", "nyc3", 3, name, droplet.id)}
