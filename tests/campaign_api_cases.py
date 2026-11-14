"""Shared constants of the DigitalOcean client tests: our tags, names and firewall rules."""

from __future__ import annotations

import re

from campaign_fakes import SID

from atlas.campaign.contracts import WORKER_TAG

OURS = [WORKER_TAG, f"atlas-camp-{SID}", f"atlas-camp-{SID}-h03"]
IP = re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")
BUILDER = "atlas-camp-builder-20270101-ab12"
SNAPSHOT = "atlas-camp-worker-0.9.5-0123456789ab-20270101"
FIREWALL = "33333333-3333-4333-8333-000000000001"

RULES = {
    "name": "atlas-camp-worker-fw",
    "inbound_rules": [],
    "outbound_rules": [],
    "tags": [WORKER_TAG],
    "droplet_ids": [],
}
