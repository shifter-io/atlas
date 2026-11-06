"""`campaign probe`: each network's highest safe concurrency, measured from one worker.

Runs on the manager, which holds the token, the worker key, the wheel and the credentials, and
is the only host the worker firewall admits. One worker (`pic-<sid>-h00-<region>-probe`, every
worker marker, its own probe id) is created, confirmed in project Proxy Index (fail closed),
used for every network in turn and removed through `scope.guarded_delete` whatever happens.

Per network: a baseline at the floor (500), then 2000, 1500, ... down to the floor; the first
step that passes the step-down rule (`reasons`) is the network's maximum. The results hold
counts, rates and latencies only, never an address.
"""

from __future__ import annotations

import asyncio
import io
import json
import secrets
import tarfile
from collections.abc import Callable, Collection
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from ..onboarding.files import atomic_bytes, atomic_json, private_dir
from ..onboarding.manifest import timestamp
from ..providers import load_provider
from ..providers.grammar import MAX_CONCURRENCY, load_grammar
from . import ops_api, scope
from .contracts import (
    PRODUCTION,
    WORKER_ROOT,
    WORKER_SECRETS,
    WORKER_TAG,
    CampaignSpec,
    Droplet,
    DropletRecord,
    Scope,
    audit_path,
    campaign_tag,
    droplet_name,
    hour_tag,
    short_id,
    spec_path,
)
from .do_api import CreateError, DigitalOcean
from .fanout import INSTALL_WHEEL, MMDB_DIR, RemoteError
from .launch import CREATE_RETRY_SECONDS, READY_LIMIT_SECONDS, WorkerInputs, manifest_path

REQUESTS, START, STEP, FLOOR = 25_000, 2000, 500, 500
COUNTRY, REGION, SIZE = "US", "nyc3", "c-4"
STEP_SECONDS = 1800.0  # one 25,000-request run; a timeout of 10 s bounds it near 500 s at 500
TIMEOUT_SECONDS = 10  # per request, as the census rounds (onboarding manifest default)
# The step-down rule (docs/campaign.html#probe). A step fails for concurrency when:
RATE_LIMIT_PCT = 2.0  # more than 2% of completed requests are HTTP 429 (or Geonode's 469);
CONNECT_PCT = 2.0  # more than 2% are refused by or cannot reach the gateway (incl. 407, DNS);
SLOWDOWN_FACTOR, SLOWDOWN_POINTS = 2.0, 1.0  # timeouts + 502/503/504, or every other failure
# (TLS, protocol, other statuses, challenges, blocks), exceed twice the baseline rate and the
# baseline plus 1 point;
SUCCESS_DROP_POINTS = 3.0  # success falls more than 3 points below the baseline's;
COMPLETE_SHARE = 0.95  # fewer than 95% of the requests completed, or the run failed.
USABLE_PCT = 50.0  # below this success rate at the floor the network is not probed further
RATE_STATUSES, GATEWAY_STATUSES = ("429", "469"), ("502", "503", "504")
# The gateway refused or could not be reached: CONNECT failures, resets, a 407 (some gateways
# answer a connection or thread limit with it) and an unresolvable gateway.
CONNECT_ERRORS = ("proxy_connect", "connection", "proxy_auth", "dns")

# Runs with the worker's system Python: argv = worker root, secrets dir. Credentials go to a
# 0600 file under the secrets dir; the wheel is installed as for a round (`INSTALL_WHEEL`).
SETUP_SCRIPT = (
    r"""
import hashlib, io, os, pathlib, subprocess, sys, tarfile
root, secrets = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]) / "probe"
archive = tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()), mode="r:")
secrets.mkdir(mode=0o700, parents=True, exist_ok=True)
for member in archive.getmembers():
    if member.name.startswith("secrets/"):
        path = secrets / pathlib.PurePosixPath(member.name).name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, archive.extractfile(member).read()); os.close(fd)
members = [m for m in archive.getmembers() if m.name.startswith(("probe/", "wheel/"))]
archive.extractall(root, members=members, filter="data")
wheel = next(root / m.name for m in members if m.name.endswith(".whl"))
"""
    + INSTALL_WHEEL
    + r"""print("ready")
"""
)


class ProbeError(RuntimeError):
    """A probe input or step that failed; fixed text, never a host or a secret."""


class ProbeRemote(Protocol):
    """What the probe needs from `fanout.OpenSSHRemote`."""

    async def wait_ready(self, droplet: Droplet, limit_seconds: float) -> bool: ...
    async def run(
        self,
        droplet: Droplet,
        command: list[str],
        what: str,
        *,
        stdin: bytes | None = None,
        seconds: float = 120,
    ) -> bytes: ...


@dataclass(frozen=True)
class ProbeInputs:
    root: Path  # a planned campaign uploaded to this manager: its spec and network configs
    output: Path  # the probe writes `<output>/<probe id>/`
    networks: tuple[str, ...] = ()  # default: the campaign's networks
    requests: int = REQUESTS
    start: int = START
    step: int = STEP
    floor: int = FLOOR
    country: str = COUNTRY
    region: str = REGION
    size: str = SIZE

    def ladder(self) -> list[int]:
        """The concurrencies tried after the baseline at the floor: start, start - step, ..."""
        return list(range(self.start, self.floor, -self.step))


def step_result(concurrency: int, requested: int, summary: dict[str, Any]) -> dict[str, Any]:
    """One run's counts as shares of its completed requests (a CONNECT answered 429 or 5xx is
    counted under its status and under connect errors), throughput and success latency.

    With the runner's answer classes (`--answer-classes`) the rate limits are the summary's
    `rate_limited` signals (CONNECT refusals among them leave `proxy_connect`) and its no-IP
    answers leave `other`; `answered` excludes the rate limits, and the real-error share is of
    answered requests. A summary without those fields gives the shares it always gave."""
    done, statuses, errors = summary["completed"], summary["status_codes"], summary["errors"]
    limited = summary.get("rate_limited")
    classes = {
        "rate_limited": sum(limited.values())
        if limited is not None
        else sum(statuses.get(s, 0) for s in RATE_STATUSES),
        "gateway_5xx": sum(statuses.get(s, 0) for s in GATEWAY_STATUSES),
        "proxy_connect": sum(errors.get(e, 0) for e in CONNECT_ERRORS)
        - (limited or {}).get("proxy_connect", 0),
        "timeout": errors.get("timeout", 0),
        "no_inventory": summary.get("no_inventory") or 0,
    }
    classes["other"] = max(0, summary["total_errors"] - sum(classes.values()))
    share = (lambda n: round(100.0 * n / done, 3)) if done else (lambda n: 0.0)
    answered = done - classes["rate_limited"] if limited is not None else done
    latency = summary.get("latency_ms") or {}
    return {
        "concurrency": concurrency,
        "requested": requested,
        "completed": done,
        "success_pct": share(summary["successes"]),
        **{f"{name}_pct": share(count) for name, count in classes.items()},
        "rps": round(summary["requests_per_second"], 1),
        "latency_p50_ms": latency.get("p50"),
        "latency_p95_ms": latency.get("p95"),
        "unique_ips": summary.get("unique_ips"),
        "stop_reason": summary.get("stop_reason"),
        **answer_shares(summary, classes, answered, share),
    }


def answer_shares(
    summary: dict[str, Any], classes: dict[str, int], answered: int, share: Callable[[int], float]
) -> dict[str, Any]:
    """The answer-class keys of `step_result`; None where the summary lacks their counts."""
    other, unique = summary.get("other_country"), summary.get("unique_ips")
    real = summary["total_errors"] - classes["rate_limited"] - classes["no_inventory"]
    return {
        "answered": answered,
        "other_country_pct": None if other is None else share(other),
        "duplicates_pct": None
        if unique is None
        else share(max(0, summary["successes"] - (other or 0) - unique)),
        "real_error_pct": round(100.0 * max(0, real) / answered, 3) if answered else 0.0,
    }


def reasons(step: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """Why a step fails for concurrency (empty: it passes). See the constants above."""
    if step.get("run_failed"):
        return ["run_failed"]
    found = []
    if step.get("answered", step["completed"]) < COMPLETE_SHARE * step["requested"]:
        found.append("incomplete")
    if step["rate_limited_pct"] > RATE_LIMIT_PCT:
        found.append("rate_limited")
    if step["proxy_connect_pct"] > CONNECT_PCT:
        found.append("proxy_connect")
    for reason, keys in (
        ("timeouts_5xx", ("timeout_pct", "gateway_5xx_pct")),
        ("other_errors", ("other_pct",)),
    ):
        rate, base = (sum(s[k] for k in keys) for s in (step, baseline))
        if rate > max(SLOWDOWN_FACTOR * base, base + SLOWDOWN_POINTS):
            found.append(reason)
    if step["success_pct"] < baseline["success_pct"] - SUCCESS_DROP_POINTS:
        found.append("success_drop")
    return found


def targeting(network: str, country: str) -> dict[str, str]:
    """The country parameter, for a network whose grammar has a country flag."""
    return {"country": country} if "country" in load_grammar(network).flags else {}


def launcher(databases: str) -> str:
    """The `sh -c` script of a step: raise the file limit, then run `"$0" "$@"`, adding
    `--mmdb` when `databases` holds exactly one MMDB, so the run applies the country check."""
    return (
        "ulimit -n 65536 || exit 1; n=0; for m in " + databases + "/*.mmdb; do "
        '[ -f "$m" ] && n=$((n + 1)) && f="$m"; done; '
        'if [ "$n" = 1 ]; then set -- "$@" --mmdb "$f"; fi; exec "$0" "$@"'
    )


def run_command(
    network: str, concurrency: int, requests: int, country: str, region: str
) -> list[str]:
    """The fixed worker command for one step; every argument is public."""
    probe, secret = f"{WORKER_ROOT}/probe", f"{WORKER_SECRETS}/probe"
    parameters = [
        arg
        for value in targeting(network, country).values()
        for arg in ("--param", f"country={value}")
    ]
    return [
        "sh",
        "-c",
        launcher(MMDB_DIR),
        f"{WORKER_ROOT}/venv/bin/proxy-index",
        "run",
        "--target",
        "generic_http",
        "--requests",
        str(requests),
        "--concurrency",
        str(concurrency),
        "--timeout",
        str(TIMEOUT_SECONDS),
        "--answer-classes",
        "--provider-config",
        f"{probe}/{network}.toml",
        "--credentials-file",
        f"{secret}/{network}.toml",
        *parameters,
        "--provider",
        network,
        "--country",
        country.lower(),
        "--location",
        region,
        "--output",
        f"{probe}/results",
    ]


def network_problems(
    root: Path, spec: CampaignSpec, files: WorkerInputs, network: str, countries: Collection[str]
) -> list[str]:
    """Why a network cannot be measured: no credentials, or its census config, credentials and
    country parameter do not load (no network I/O)."""
    credentials = files.credentials_dir / f"{network}.toml"
    if not credentials.is_file():
        return [f"no credentials for {network}"]
    config = manifest_path(root, spec, network).parent / "provider.toml"
    try:
        for country in sorted(set(countries)):
            load_provider(
                config, parameters=targeting(network, country), credentials_file=credentials
            )
    except (OSError, ValueError) as error:
        return [f"network {network}: {type(error).__name__}: {error}"]
    return []


def payload(
    root: Path, spec: CampaignSpec, files: WorkerInputs, networks: Collection[str]
) -> bytes:
    """One tar for `SETUP_SCRIPT`: the wheel, each network's census config and credentials."""
    entries = [(f"wheel/{files.wheel.name}", files.wheel.read_bytes())]
    for network in networks:
        config = manifest_path(root, spec, network).parent / "provider.toml"
        entries += [
            (f"probe/{network}.toml", config.read_bytes()),
            (f"secrets/{network}.toml", (files.credentials_dir / f"{network}.toml").read_bytes()),
        ]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o600
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


async def set_up(remote: ProbeRemote, droplet: Droplet, data: bytes, seconds: float = 600) -> None:
    """Install the wheel and write the configs and credentials on one worker."""
    reply = await remote.run(
        droplet,
        ["python3", "-c", SETUP_SCRIPT, WORKER_ROOT, WORKER_SECRETS],
        "probe setup",
        stdin=data,
        seconds=seconds,
    )
    if reply.decode(errors="replace").strip().splitlines()[-1:] != ["ready"]:
        raise ProbeError("probe setup did not finish")


# The answer-class columns of the probe and shot tables (from `step_result`).
CLASS_COLUMNS = {
    "No-IP %": "no_inventory_pct",
    "Other-country %": "other_country_pct",
    "Duplicates %": "duplicates_pct",
    "Real errors % (answered)": "real_error_pct",
}
CLASS_HEADER = " | ".join(CLASS_COLUMNS)


def class_cells(step: dict[str, Any]) -> list[Any]:
    """A step's answer-class cells; "-" where it lacks one (a result written before them)."""
    return ["-" if step.get(key) is None else step[key] for key in CLASS_COLUMNS.values()]


STEP_COLUMNS = (
    "concurrency",
    "completed",
    "success_pct",
    "rate_limited_pct",
    "gateway_5xx_pct",
    "proxy_connect_pct",
    "timeout_pct",
    "other_pct",
    "rps",
    "latency_p50_ms",
    "latency_p95_ms",
)


def step_row(network: str, s: dict[str, Any]) -> str:
    """One step's row of the steps table; "-" where the step lacks a value."""
    cells = [s.get(k, "-") for k in STEP_COLUMNS]
    row = [network, *cells, *class_cells(s), ", ".join(s.get("step_down", [])) or "-"]
    return "| " + " | ".join(map(str, row)) + " |"


def markdown(result: dict[str, Any]) -> str:
    """The owner's table: one row per step, the maximum and the grammar line per network."""
    lines = [
        f"# Concurrency probe {result['probe_id']}",
        "",
        f"{result['requests']} requests per step, country {result['country']}, one "
        f"{result['size']} worker in {result['region']}.",
        "",
        "| Network | Max | Grammar |",
        "|---|---|---|",
    ]
    for network, found in result["networks"].items():
        best, verdict = found.get("max_concurrency"), found.get("verdict")
        # A verdict (the floor also fails, or no maximum) replaces the grammar line.
        line = verdict or f"`[testing] max_concurrency = {best}`"
        lines.append(f"| {network} | {best or '-'} | {line} |")
    lines += [
        "",
        "| Network | Concurrency | Completed | Success % | Rate-limited % | 5xx % "
        f"| Connect % | Timeout % | Other % | RPS | p50 ms | p95 ms | {CLASS_HEADER} "
        "| Step down |",
        "|" + "---|" * 17,
    ]
    for network, found in result["networks"].items():
        lines += [step_row(network, s) for s in found["steps"]]
    return "\n".join(lines) + "\n"


def planned_campaign(root: Path) -> tuple[CampaignSpec, WorkerInputs]:
    """The planned campaign at `root` and its worker inputs; a missing or bad one refuses."""
    try:
        spec = CampaignSpec.from_json(spec_path(root).read_text())
        return spec, WorkerInputs.for_root(root, spec.wheel_sha256)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ProbeError(
            f"no planned campaign with its wheel at --root ({type(error).__name__})"
        ) from None


class Probe:
    """One probe: its worker record, results and files under `<output>/<probe id>/`."""

    def __init__(
        self,
        cloud: DigitalOcean,
        remotes: Callable[[Path], ProbeRemote],
        inputs: ProbeInputs,
        now: datetime,
        sleep: ops_api.Sleep,
    ):
        self.cloud, self.inputs, self.sleep = cloud, inputs, sleep
        self.spec, self.files = planned_campaign(inputs.root)
        self.networks = inputs.networks or self.spec.networks
        self.check()
        self.probe_id = f"{now:%Y%m%dt%H%M%S}z-{secrets.token_hex(6)}"
        self.scope = Scope(
            self.probe_id,
            short_id(self.probe_id),
            self.spec.project_id,
            self.spec.manager_droplet_id,
            now - ops_api.CLOCK_SKEW,
        )
        self.folder = inputs.output / self.probe_id
        self.remote = remotes(self.folder)  # its known-hosts file lives in this probe's folder
        self.worker = DropletRecord(
            "probe",
            inputs.region,
            0,
            droplet_name(self.probe_id, 0, inputs.region, "probe"),
            size=inputs.size,
            image_id=self.spec.image_by_region[inputs.region],
        )
        self.result = self.first_result()

    def first_result(self) -> dict[str, Any]:
        """The results before any network: what the report and `--remove` need."""
        inputs = self.inputs
        return {
            "probe_id": self.probe_id,
            "campaign_id": self.spec.campaign_id,
            # What `campaign probe --remove` needs to rebuild this scope in a later process.
            "scope": {
                "project_id": self.scope.project_id,
                "manager_droplet_id": self.scope.manager_droplet_id,
                "created_at": self.scope.created_at.isoformat(),
            },
            "requests": inputs.requests,
            "country": inputs.country,
            "region": inputs.region,
            "size": inputs.size,
            "ladder": [inputs.floor, *inputs.ladder()],
            "networks": {},
        }

    def check(self) -> None:
        """Every input before anything is created."""
        i = self.inputs
        planned = set(self.networks) <= set(self.spec.networks)
        problems = [
            (i.region not in self.spec.regions, f"region {i.region} is not planned"),
            (not planned, "networks not planned"),
            (not 1 <= i.floor <= i.start <= MAX_CONCURRENCY, "start or floor"),
            (i.step < 1 or i.requests < 1, "step or requests"),
            (self.spec.environment != PRODUCTION, "a probe runs on production's workers only"),
        ]
        bad = [text for failed, text in problems if failed]
        for network in self.networks if planned else ():
            bad += network_problems(i.root, self.spec, self.files, network, [i.country])
        if bad:
            raise ProbeError(f"refused: {', '.join(bad)}")

    def save(self) -> None:
        """The record and results, with the worker's name before its create and its id (never
        its address) right after."""
        self.result["worker"] = asdict(self.worker)
        private_dir(self.folder)
        atomic_json(self.folder / "probe-result.json", self.result)

    def write(self) -> None:
        self.save()
        atomic_bytes(self.folder / "probe-result.md", markdown(self.result).encode())

    async def run(self) -> dict[str, Any]:
        """Create, confirm, set up, measure every network; the worker is removed whatever
        happens, also when a signal cancels the probe (the CLI turns SIGTERM, SIGHUP and SIGINT
        into a cancel). A worker left behind is reported with its recovery command."""
        try:
            droplet = await self.create()
            await self.measure_all(droplet)
        except asyncio.CancelledError:
            self.result["error"] = "cancelled by a signal"
        except Exception as error:
            message = scope.SECRETS.sub("[removed]", str(error))
            self.result["error"] = f"{type(error).__name__}: {message}"[: scope.MESSAGE_LIMIT]
        finally:
            cleanup = asyncio.ensure_future(self.cleanup())
            while not cleanup.done():  # a signal during the removal never interrupts it
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    self.result["error"] = "cancelled by a signal"
        return self.result

    async def cleanup(self) -> None:
        worker = self.worker
        if worker.droplet_id is None and worker.error_class == "create_uncertain":
            await self.adopt()  # one last look before reporting it
        if worker.droplet_id is not None:
            await release(self.cloud, self.scope, worker, audit_path(self.folder), self.sleep)
        self.result["worker_status"] = worker.status
        if worker.status != "destroyed" and (
            worker.droplet_id or worker.error_class == "create_uncertain"
        ):
            self.result["recover"] = f"campaign probe --remove {self.folder}"
        self.write()

    async def adopt(self) -> Droplet | None:
        """Our worker by exact name under the probe's hour tag; an API error finds nothing."""
        found = await adopt(self.cloud, self.worker, self.probe_id)
        if found is not None:
            self.save()
        return found

    async def create(self) -> Droplet:
        """Name before the create, id right after; a lost answer is adopted by exact name."""
        worker, tag = self.worker, hour_tag(self.probe_id, 0)
        worker.attempts, worker.requested_at = 1, timestamp(datetime.now(UTC).timestamp())
        # Uncertain until an id is known or the create definitely failed, so a signal during
        # the POST or the retry wait still gets the cleanup's last look and the recover hint.
        worker.error_class = "create_uncertain"
        self.save()
        try:
            [droplet] = await self.cloud.create(
                [worker.name],
                region=worker.region,
                size=worker.size,
                image=worker.image_id,
                tags=[WORKER_TAG, campaign_tag(self.probe_id), tag],
                ssh_keys=[self.spec.ssh_key_id],
                monitoring=False,
                with_droplet_agent=False,
                ipv6=False,
            )
        except CreateError as error:
            found = await self.created_anyway(error)
            if found is None:
                worker.status = "create_failed"
                worker.error_class = "create_uncertain" if error.uncertain else "create_failed"
                raise
            droplet = found
        worker.droplet_id, worker.error_class = droplet.id, None
        self.save()
        return await self.arm(droplet)

    async def created_anyway(self, error: CreateError) -> Droplet | None:
        """The worker a failed create still made: in the error, or adopted when uncertain."""
        found = error.created[0] if error.created else None
        if found is None and error.uncertain:
            # The listing may lag a create that landed: look again later, as a launch does.
            found = await self.adopt()
            if found is None:
                await self.sleep(CREATE_RETRY_SECONDS)
                found = await self.adopt()
        return found

    async def arm(self, droplet: Droplet) -> Droplet:
        """Confirm the new worker in the project (fail closed), then wait for it and its SSH."""
        worker = self.worker
        _, failed = await scope.assign_and_verify_project(
            self.cloud,
            self.scope,
            [droplet.id],
            {droplet.id: worker},
            audit_path(self.folder),
            retry=scope.Retry(sleep=self.sleep, save=self.save),
        )
        self.save()
        if failed:
            raise ProbeError(f"worker not confirmed in the project ({worker.status})")
        ready = await ops_api.wait_droplet(self.cloud, droplet.id, "active", self.sleep)
        if not await self.remote.wait_ready(ready, READY_LIMIT_SECONDS):
            raise ProbeError("worker SSH never became ready")
        worker.status, worker.ssh_ready_at = "armed", timestamp(datetime.now(UTC).timestamp())
        return ready

    async def measure_all(self, droplet: Droplet) -> None:
        await set_up(
            self.remote, droplet, payload(self.inputs.root, self.spec, self.files, self.networks)
        )
        for network in self.networks:
            self.result["networks"][network] = found = {"steps": []}
            await self.measure(droplet, network, found)
            self.write()

    async def measure(self, droplet: Droplet, network: str, found: dict[str, Any]) -> None:
        """Baseline at the floor, then down from the start; the first passing step is the max."""
        base = await self.step(droplet, network, self.inputs.floor)
        base["step_down"] = reasons(base, base)
        found["steps"].append(base)
        if base.get("run_failed") or base["success_pct"] < USABLE_PCT:
            found.update(
                max_concurrency=None,
                verdict="run failed at the floor"
                if base.get("run_failed")
                else "unusable at the floor",
            )
            return
        for concurrency in self.inputs.ladder():
            current = await self.step(droplet, network, concurrency)
            current["step_down"] = reasons(current, base)
            found["steps"].append(current)
            if not current["step_down"]:
                found["max_concurrency"] = concurrency
                return
        found["max_concurrency"] = self.inputs.floor
        if base["step_down"]:
            found["verdict"] = "floor also fails: " + ", ".join(base["step_down"])

    async def step(self, droplet: Droplet, network: str, concurrency: int) -> dict[str, Any]:
        i = self.inputs
        command = run_command(network, concurrency, i.requests, i.country, i.region)
        try:
            reply = await self.remote.run(
                droplet, command, f"probe {network}", seconds=STEP_SECONDS
            )
            summary = json.loads(reply.decode().strip().splitlines()[-1])
            return step_result(concurrency, self.inputs.requests, summary)
        except (RemoteError, ValueError, IndexError, KeyError, TypeError) as error:
            return {
                "concurrency": concurrency,
                "requested": self.inputs.requests,
                "run_failed": type(error).__name__,
            }


async def adopt(
    cloud: DigitalOcean, worker: DropletRecord, probe_id: str, *, strict: bool = False
) -> Droplet | None:
    """The droplet with the worker's exact name under the probe's hour tag (`adopt_uncertain`,
    which also counts it as this client's own create); None when absent, or when unreadable
    unless `strict` (then the API error is raised)."""
    try:
        found = await cloud.adopt_uncertain([worker.name], hour_tag(probe_id, 0))
    except Exception:
        if strict:
            raise
        return None
    mine = [d for d in found if worker.droplet_id in (None, d.id)]
    if len(mine) != 1:
        return None
    worker.droplet_id, worker.error_class = mine[0].id, None
    return mine[0]


async def release(
    cloud: DigitalOcean,
    probe_scope: Scope,
    worker: DropletRecord,
    audit: Path,
    sleep: ops_api.Sleep,
) -> None:
    """Remove the probe worker through the guard and set its final status.

    Confirmed in the project: the plain `guarded_delete`. Marked by a failed assignment: the
    guard's own-unassigned path. Neither (the process stopped between the create and the
    assignment): it is first confirmed in the project, as a resumed launch does; one that is
    not confirmed is removed on the own-unassigned path inside `assign_and_verify_project`.
    A refusal is final (`destroy_refused`), an error leaves `destroy_failed` for a rerun."""
    if worker.status in ("destroyed", "destroy_refused"):  # done, or the owner's to decide
        return
    rid, records = str(worker.droplet_id), {str(worker.droplet_id): worker}
    try:
        if worker.assigned_at is None and worker.assign_failed_at is None:
            _, failed = await scope.assign_and_verify_project(
                cloud, probe_scope, [rid], records, audit, retry=scope.Retry(sleep=sleep)
            )
            if failed:
                return
        await scope.guarded_delete(
            cloud, "droplet", rid, probe_scope, scope.Ledger({"droplet": [rid]}, records), audit
        )
        worker.status, worker.destroyed_at = "destroyed", timestamp(datetime.now(UTC).timestamp())
    except scope.OwnershipError:
        worker.status = "destroy_refused"
    except Exception as error:  # audited by the guard; `campaign probe --remove` retries
        worker.status, worker.error_class = "destroy_failed", type(error).__name__


async def probe(
    cloud: DigitalOcean,
    remotes: Callable[[Path], ProbeRemote],
    inputs: ProbeInputs,
    *,
    now: datetime | None = None,
    sleep: ops_api.Sleep = asyncio.sleep,
) -> dict[str, Any]:
    """Run one probe; returns the result (also in `<output>/<probe id>/probe-result.*`).
    `remotes(folder)` gives the SSH remote whose runtime files live in the probe's folder."""
    return await Probe(cloud, remotes, inputs, now or datetime.now(UTC), sleep).run()
