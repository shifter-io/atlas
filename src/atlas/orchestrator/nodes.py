"""Node identity: one bearer token per (region, provider, product), stored as its sha256.

A browser node (region `browser`, K-BP5) is bound to its host's logical name (`browser-<n>`),
kept in nodes.host; the browser region's runner_regions row is created with its first token.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any

from .. import storage
from ..browser.contracts import (
    BROWSER_REGION,
    BROWSER_REGION_CLOUD,
    BROWSER_REGION_ID,
    BROWSER_REGION_PATTERN,
    HOST_NAME_PATTERN,
)
from ..regions import region_id, resolve_region

# The browser region is not seeded from configs/regions.yaml; its row is inserted when missing.
BROWSER_REGION_SQL = (
    "INSERT INTO runner_regions(region_id,slug,cloud,pattern) VALUES (%s,%s,%s,%s) "
    "ON CONFLICT DO NOTHING"
)
BROWSER_REGION_ROW = (
    BROWSER_REGION_ID,
    BROWSER_REGION,
    BROWSER_REGION_CLOUD,
    BROWSER_REGION_PATTERN,
)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(
    credentials_file: str,
    *,
    provider: str,
    product: str,
    region: str,
    host: str | None = None,
) -> dict[str, Any]:
    """Create the node row, or rotate its token. The token is returned once and not stored.

    `region` is a slug or a host name matched by configs/regions.yaml, or `browser`; a browser
    node needs `host`, its logical name `browser-<n>`. The provider and product rows are
    created if this is the product's first node.
    """
    slug = resolve_region(region)
    if not provider or not product:
        raise ValueError("A node needs a provider and a product")
    browser = slug == BROWSER_REGION
    if browser and not re.fullmatch(HOST_NAME_PATTERN, host or ""):
        raise ValueError("A browser node needs its host's logical name, browser-<n>")
    token = secrets.token_urlsafe(32)
    with storage.connect(credentials_file) as conn:
        if browser:
            conn.execute(BROWSER_REGION_SQL, BROWSER_REGION_ROW)
        if not conn.execute(
            "SELECT 1 FROM runner_regions WHERE region_id=%s", (region_id(slug),)
        ).fetchone():
            raise ValueError("Runner regions are not seeded; run db-seed first")
        conn.execute("INSERT INTO providers VALUES (%s) ON CONFLICT DO NOTHING", (provider,))
        conn.execute(
            "INSERT INTO products(provider,name) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (provider, product),
        )
        node_id, rotated = conn.execute(
            "INSERT INTO nodes(region_id,provider,product,host,token_hash) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT(region_id,provider,product) DO UPDATE SET "
            "token_hash=excluded.token_hash,host=coalesce(excluded.host,nodes.host),"
            # A new token re-arms a retired node: offline until its first heartbeat.
            "status=CASE WHEN nodes.status='retired' THEN 'offline' ELSE nodes.status END "
            "RETURNING node_id,(xmax<>0)",
            (region_id(slug), provider, product, host, token_hash(token)),
        ).fetchone()
    return {
        "node_id": node_id,
        "region": slug,
        "provider": provider,
        "product": product,
        "rotated": rotated,
        "token": token,
    }
