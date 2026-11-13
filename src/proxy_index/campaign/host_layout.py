"""MANAGER-HOST-1: one environment's folders, unit names and ports on an existing server.

A host inventory (`manager_kind: host`) runs the manager on a server we did not create, as its
normal user with user units (`systemctl --user`), production and staging side by side. This
module is the shared Phase 5 layout table: production keeps the normal ports, staging adds 1000
and the `-staging` infix; folders are `~/atlas/<env>/`, private files `~/.config/atlas/<env>/`.

The droplet's scripts are reused as they are: `Layout.translate` maps their fixed folders, files
and unit names onto this layout, and `unit_files` renders the `atlas-*` user-unit assets.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from typing import Any

HOST = "host"  # the inventory's manager_kind
OFFSETS = {"production": 0, "staging": 1000}
ASSETS = (
    "orchestrator.service",
    "analyze.service",
    "analyze.timer",
    "hourly.service",
    "hourly.timer",
    "manager.target",
)


@dataclass(frozen=True)
class Layout:
    environment: str

    def __post_init__(self) -> None:
        if self.environment not in OFFSETS:
            raise ValueError(f"unknown environment {self.environment!r}")

    @property
    def prefix(self) -> str:
        return "atlas" if self.environment == "production" else "atlas-staging"

    def port(self, production: int) -> int:
        return production + OFFSETS[self.environment]

    @property
    def node_port(self) -> int:
        return self.port(8443)

    @property
    def browser_port(self) -> int:
        return self.port(8444)

    def unit(self, name: str) -> str:
        return f"{self.prefix}-{name}"

    @property
    def root(self) -> str:
        return f"$HOME/atlas/{self.environment}"

    @property
    def private(self) -> str:
        return f"$HOME/.config/atlas/{self.environment}"

    @property
    def manager_ref(self) -> str:
        """What a campaign spec records as its manager (there is no droplet id)."""
        return f"{HOST}:{self.environment}"

    @property
    def started(self) -> tuple[str, ...]:
        """The manager on/off target and the two units it holds (never the hourly timer)."""
        return tuple(self.unit(n) for n in ("manager.target", *ASSETS[:1], ASSETS[2]))

    def names(self) -> dict[str, Any]:
        """Every folder, unit, scope, port and private file this environment owns."""
        return {
            "folders": [self.root, f"$HOME/atlas/{self.environment}/campaigns"],
            "private": [self.private],
            "units": [self.unit(n) for n in ASSETS] + [self.unit("api@.service")],
            "scope": self.unit("calculate.scope"),
            "hourly_runs": self.unit("hourly-"),
            "ports": [self.port(p) for p in (8443, 8444, 8445, 8090, 8091)],
        }

    def _pairs(self, home: str) -> list[tuple[str, str]]:
        """(droplet text, host text), longest first where one contains another."""
        from . import deploy, manager_daily, manager_orch

        root, private = (
            f"{home}/atlas/{self.environment}",
            f"{home}/.config/atlas/{self.environment}",
        )
        return [
            (" ".join(manager_orch.STARTED), " ".join(self.started)),
            (manager_daily.DAILY_RUNS, self.unit("hourly-*")),
            (manager_orch.ORCHESTRATOR_UNIT, self.unit(ASSETS[0])),
            (manager_orch.ANALYZE_SERVICE, self.unit(ASSETS[1])),
            (manager_orch.ANALYZE_TIMER, self.unit(ASSETS[2])),
            (manager_daily.DAILY_SERVICE, self.unit(ASSETS[3])),
            (manager_daily.DAILY_TIMER, self.unit(ASSETS[4])),
            ("/etc/systemd/system", f"{home}/.config/systemd/user"),
            (deploy.DATABASE_FILE, f"{private}/database.toml"),
            (deploy.SECRETS, private),
            (deploy.BASE, root),
            ("/root/", f"{home}/"),
            ("systemctl ", "systemctl --user "),
        ]

    def translate(self, script: str, home: str = "$HOME") -> str:
        """A droplet script or path for this layout (`home` is `%h` inside a unit file)."""
        for old, new in self._pairs(home):
            script = script.replace(old, new)
        return script

    def unit_files(self) -> list[tuple[str, bytes, int]]:
        """The rendered user units (`~/.config/systemd/user`), named for this environment."""
        assets = files("proxy_index.campaign").joinpath("assets")
        values = self._values()
        rendered = []
        for name in ASSETS:
            text = assets.joinpath(f"atlas-{name}").read_text()
            for key, value in values.items():
                text = text.replace(f"@{key}@", value)
            rendered.append((self.unit(name), text.encode(), 0o644))
        return rendered

    def _values(self) -> dict[str, str]:
        from ..enrichment import CURRENT, MMDB_ENV
        from ..orchestrator.jobs import MEMORY_ENV, SCOPE_ENV
        from . import manager_daily, manager_orch
        from .collector import ROOT_ENV

        def here(path: str) -> str:
            return self.translate(path, "%h")

        return {
            "PREFIX": self.prefix,
            "ENV": self.environment,
            "ROOT": here(manager_orch.deploy.BASE),
            "CLI": here(manager_daily.PROXY_INDEX),
            "DATABASE": here(manager_orch.DATABASE_FILE),
            "ENV_FILE": here(manager_orch.ENV_FILE),
            "CERT": here(manager_orch.CERT),
            "KEY": here(manager_orch.KEY),
            "CLIENT_CA": here(manager_orch.BROWSER_CA),
            "MMDB": here(f"{manager_orch.DBIP_DIR}/{CURRENT}"),
            "FORMULAS": here(manager_daily.FORMULAS),
            "TEMPLATE": here(manager_daily.TEMPLATE),
            "NODE_PORT": str(self.node_port),
            "BROWSER_PORT": str(self.browser_port),
            "MEMORY_ENV": MEMORY_ENV,
            "SCOPE_ENV": SCOPE_ENV,
            "MMDB_ENV": MMDB_ENV,
            "ROOT_ENV": ROOT_ENV,
        }


def of(inventory: dict[str, Any]) -> Layout | None:
    """The layout of a host inventory; None for a droplet manager's."""
    if inventory.get("manager_kind") != HOST:
        return None
    return Layout(inventory.get("environment", "production"))


def manager_ref(inventory: dict[str, Any]) -> str | None:
    """The recorded manager: a droplet's id, or `host:<environment>` for a host inventory."""
    layout = of(inventory)
    if layout is not None:
        return layout.manager_ref if inventory.get("host") else None
    found = inventory.get("manager_id")
    return str(found) if found else None


def script(inventory: dict[str, Any], text: str) -> str:
    """`text` as the inventory's manager runs it: unchanged on a droplet, translated on a host."""
    layout = of(inventory)
    return layout.translate(text) if layout else text
