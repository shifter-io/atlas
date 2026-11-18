"""RENAME-ATLAS-2: the names from before the rename to Atlas that the code still reads for one
release, so nothing created before the rename is orphaned. Nothing new is ever created under them.

The only file (with its test) that may write them:
scripts/rename_to_atlas.keep keeps it whole, and tests/test_atlas_rename.py fails on an old name
anywhere else. Remove each entry once the operator's checklist has renamed or deleted what it
names; the checklist lists the same names.
"""

from __future__ import annotations

import re

# DigitalOcean resource-name prefixes: (old, new, what must follow). Tags, firewalls, keys,
# snapshots, manager and builder names used `pi-camp-` (staging `pi-camp-stg-`), pulse nodes
# `pi-pulse-`, worker names `pic-` before their 8-hex short id.
PREFIXES = (
    ("pi-camp-", "atlas-camp-", ""),
    ("pi-pulse-", "atlas-pulse-", ""),
    ("pic-", "atlas-", "(?=[0-9a-f]{8}-)"),
)
# The DigitalOcean project every account keeps campaign droplets in (renamed by the operator).
PROJECT_NAME = "Proxy Index"
# The DigitalOcean account key (`--account`, inventories' and specs' "account").
ACCOUNT = "proxy-index"
# Keychain entries on the owner's Mac: (service, account) of the DigitalOcean token, and the
# Spaces archive's service.
DO_KEYCHAIN = ("Proxy Index DigitalOcean", "team: Proxy Index")
SPACES_KEYCHAIN_SERVICE = "Proxy Index Spaces archive"
# The `schema` / `format` ids written into files kept outside the repository: formula copies on
# the managers, archive day files, browser manifests.
HOUR_FORMULA = "proxy-index-hour-formula"
BEST_HOURS = "proxy-index-best-hours"
RAW_DAY = "proxy-index-raw-day"
BROWSER_RAW = "proxy-index-browser-raw"
RELEASE_MARKER = ".proxy-index-release.json"
# The owner's Mac: the campaign SSH key's default path, the watch agent's launchd label.
MAC_KEY = "~/.ssh/proxy-index-campaign"
WATCH_LABEL = "com.proxy-index.campaign-watch"


def current(name: str) -> str:
    """A DigitalOcean resource name or tag with an old prefix as it is named now; any other name
    unchanged. The scope checks read names through it, so a pre-rename droplet, firewall, key or
    snapshot is still recognised as ours, and can be listed and removed."""
    for before, now, follows in PREFIXES:
        if re.match(re.escape(before) + follows, name):
            return now + name[len(before) :]
    return name


def old(name: str) -> str | None:
    """The pre-rename spelling of a current resource name or tag, or None if it had none."""
    for before, now, follows in PREFIXES:
        if re.match(re.escape(now) + follows, name):
            return before + name[len(now) :]
    return None


def account(key: str) -> str:
    """The account key as it is named now (an old inventory or spec records the old one)."""
    return "atlas" if key == ACCOUNT else key
