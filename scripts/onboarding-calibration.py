#!/usr/bin/env python3
"""Run only the first eligible regional round through the campaign scheduler.

The normal 24-round checkpoint is retained. After the final country, an operator
stop leaves the later rounds pending; they are deliberately outside this calibration.
Use a separate manifest/directory for the subsequent full campaign.
"""
import argparse
import asyncio
import json
import signal
from pathlib import Path

from milf_framework.onboarding.files import digest
from milf_framework.onboarding.manifest import load
from milf_framework.onboarding.scheduler import execute
from milf_framework.providers import load_provider
from milf_framework.runner import run


async def calibrate(manifest_path, region, provider_config, credentials_file):
    manifest = load(manifest_path)
    if digest(provider_config) != manifest["provider_config_sha256"]:
        raise ValueError("Provider configuration does not match manifest")
    countries = manifest["regions"][region]
    proxies = {country: load_provider(provider_config,
                                      parameters={"country": country.lower()},
                                      credentials_file=credentials_file) for country in countries}
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    completed = 0

    async def measure(*args, **kwargs):
        nonlocal completed
        summary = await run(*args, **kwargs)
        completed += 1
        if completed == len(countries):
            # Set this before returning to the scheduler: no second-round request
            # can slip through, even if the first round passed an hourly boundary.
            stop.set()
        return summary

    return await execute(manifest_path, region, proxies.__getitem__,
                         stop=stop, run_country=measure)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("manifest", "provider-config", "credentials-file"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(calibrate(args.manifest, args.region,
                                          args.provider_config, args.credentials_file))))
