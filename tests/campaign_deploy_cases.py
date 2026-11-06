"""Shared helper for the campaign manager tests: a planned campaign root beside a spec.

`planned_root` writes the spec and one Shifter manifest that `upload-spec` sends; no request
leaves the machine.
"""

from __future__ import annotations

from campaign_deploy_fakes import CID, IMAGE, NOW
from campaign_fakes import CAMPAIGN_PROJECT

from proxy_index.campaign.contracts import CampaignSpec, spec_path


def planned_root(tmp_path, manager_id="500001", wheel="ab" * 32, **changes):
    from dataclasses import replace

    spec = replace(
        CampaignSpec(
            CID,
            NOW.isoformat(),
            "leftclick",
            CAMPAIGN_PROJECT,
            "2027-01-01T23:00:00+00:00",
            48,
            ("lon1",),
            ("shifter",),
            {"shifter": "networks/shifter/residential/m/campaign.json"},
            {"lon1": "c-2"},
            {"lon1": "9001"},
            IMAGE,
            wheel,
            "0.9.5",
            "77",
            manager_id,
        ),
        **changes,
    )
    root = tmp_path / "plan" / CID
    (root / "networks/shifter/residential/m").mkdir(parents=True)
    (root / "networks/shifter/residential/m/campaign.json").write_text("{}")
    (root / "networks/shifter/residential/m/provider.toml").write_text("provider = 1\n")
    spec_path(root).write_text(spec.to_json())
    return root
