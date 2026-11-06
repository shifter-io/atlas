"""Shared helpers of the one-round tests: manifests, a fake census runner and one round."""

from datetime import timedelta
from types import SimpleNamespace

from test_onboarding import ROOT, START, Clock, Proxy

from proxy_index.onboarding import manifest as campaign
from proxy_index.onboarding.rounds import execute_round

TOR1 = ["CA", "GL", "PM"]


def make(tmp_path, **kwargs):
    kwargs.setdefault("now", START - timedelta(hours=1))
    return campaign.create(
        ROOT / "configs/shifter.toml",
        "residential",
        campaign.timestamp(kwargs.pop("start", START)),
        tmp_path / "networks",
        execution_mode=kwargs.pop("execution_mode", "ephemeral"),
        **kwargs,
    )


def measurement(rows, **extra):
    values = (
        dict(
            status="completed",
            stop_reason="unique_ratio",
            completed=rows + 1,
            successes=rows,
            total_errors=1,
            unique_ips=rows,
            cancelled=0,
            bytes_up=100 * rows,
            bytes_down=300 * rows,
        )
        | extra
    )
    return SimpleNamespace(**values, to_dict=lambda: dict(values))


def census(clock, calls, *, rows=2, seconds=30.0, special=None):
    """A fake runner that writes ``rows`` observations and takes ``seconds`` per country."""

    async def run_country(config, sink, options):
        country = config.proxy.public()["requested_country"].upper()
        calls.append(
            SimpleNamespace(
                country=country,
                at=clock.now(),
                deadline=options.deadline,
                concurrency=config.concurrency,
                requests=config.requests,
            )
        )
        if special and country in special:
            return await special[country](options.stop)
        for index in range(rows):
            await options.observation_consumer(f"203.0.113.{index + 1}", options.wall_clock())
        clock.value += seconds
        return measurement(rows)

    return run_country


async def run_round(manifest, index=3, *, clock=None, calls=None, **kwargs):
    scheduled = campaign.moment(campaign.load(manifest)["rounds"][index]).timestamp()
    clock = clock or Clock(scheduled)
    calls = [] if calls is None else calls
    stop = kwargs.pop("stop", None)
    result = await execute_round(
        manifest,
        "tor1",
        index,
        Proxy,
        clock=clock,
        stop=stop,
        run_country=census(clock, calls, **kwargs),
    )
    return result, calls
