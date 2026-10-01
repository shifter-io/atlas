"""Node identity: one bearer token per (region, provider, product), stored as its sha256."""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

from .. import storage
from ..regions import REGION_IDS, resolve_region


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

    `region` is a slug or a host name matched by configs/regions.yaml. The provider and product
    rows are created if this is the product's first node.
    """
    slug = resolve_region(region)
    if not provider or not product:
        raise ValueError("A node needs a provider and a product")
    token = secrets.token_urlsafe(32)
    with storage.connect(credentials_file) as conn:
        if not conn.execute(
            "SELECT 1 FROM runner_regions WHERE region_id=%s", (REGION_IDS[slug],)
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
            "token_hash=excluded.token_hash,host=coalesce(excluded.host,nodes.host) "
            "RETURNING node_id,(xmax<>0)",
            (REGION_IDS[slug], provider, product, host, token_hash(token)),
        ).fetchone()
    return {
        "node_id": node_id,
        "region": slug,
        "provider": provider,
        "product": product,
        "rotated": rotated,
        "token": token,
    }
