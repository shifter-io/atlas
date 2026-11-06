"""The cloud side of a campaign: droplets as the API reports them, a campaign's scope, and the
Cloud and Remote protocols. Re-exported by `campaign.contracts`, which callers import from."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

PRODUCTION = "production"  # the default environment; `contracts` adds staging


@dataclass(frozen=True)
class Droplet:
    """A droplet as the API reports it; `public_ipv4` stays in memory and `.runtime` only."""

    id: str
    name: str
    region: str
    size: str
    status: str
    created_at: datetime
    tags: tuple[str, ...]
    public_ipv4: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Scope:
    """What one campaign owns: its short id, project, recorded manager and environment."""

    campaign_id: str
    sid: str
    project_id: str
    manager_droplet_id: str
    created_at: datetime
    environment: str = PRODUCTION


class Cloud(Protocol):
    """Read and create operations only. Deletes exist solely in `scope.guarded_delete`."""

    async def create(
        self,
        names: list[str],
        *,
        region: str,
        size: str,
        image: str,
        tags: list[str],
        ssh_keys: list[str],
        **flags: bool,
    ) -> list[Droplet]: ...
    async def assign_project(
        self, project_id: str, droplet_ids: list[str], *, guard: object
    ) -> None: ...
    async def get(self, droplet_id: str) -> Droplet | None: ...
    async def list_tag(self, tag: str) -> list[Droplet]: ...
    async def project_droplet_ids(self, project_id: str) -> frozenset[str]: ...
    async def account(self) -> dict[str, Any]: ...


class Remote(Protocol):
    async def wait_ready(self, droplet: Droplet, limit_seconds: float) -> bool: ...
    async def push_start(self, droplet: Droplet, payload: bytes, command: list[str]) -> None: ...
    async def inspect(self, droplet: Droplet) -> dict[str, Any]: ...
    async def stop(self, droplet: Droplet) -> None: ...
    async def pull(self, droplet: Droplet, remote: str, local: Path) -> None: ...
    async def journal(self, droplet: Droplet, local: Path) -> None: ...
