"""Read-only campaign checks; no traffic, state repair, or automatic restarts."""
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from .files import atomic_json, fingerprint, lock
from .fleet import collect
from .manifest import load, moment, regional_window, timestamp
from .scheduler import validate_state


def audit(manifest: dict, snapshot: dict, now: float) -> dict:
    alerts = []
    regions = []
    by_region = {s['region']: s for s in snapshot['servers']}
    measuring = []
    for region in manifest['regions']:
        server = by_region.get(region, {})
        window = regional_window(manifest, region)
        start, end = (moment(window[k]).timestamp() for k in ('starts_at', 'ends_at'))
        state = server.get('state', {})
        if server.get('error') or not state:
            alerts.append({'region': region, 'issue': 'unreachable_or_missing_state'})
            continue
        try:
            validate_state(state, manifest, region)
        except (ValueError, KeyError):
            alerts.append({'region': region, 'issue': 'campaign_checkpoint_mismatch'})
            continue
        if state['status'] in ('failed', 'stopped'):
            alerts.append({'region': region, 'issue': 'scheduler_' + state['status']})
        if now < end and now - moment(state['updated_at']).timestamp() > 30:
            alerts.append({'region': region, 'issue': 'stale_heartbeat'})
        if now < end and server.get('service', {}).get('MainPID', '0') == '0':
            alerts.append({'region': region, 'issue': 'scheduler_not_running'})
        if abs(server.get('server_time', now) - now) > 10:
            alerts.append({'region': region, 'issue': 'clock_offset'})
        if server.get('disk_free_gb', 100) < 5:
            alerts.append({'region': region, 'issue': 'low_disk'})
        if server.get('memory_used_pct', 0) > 90:
            alerts.append({'region': region, 'issue': 'high_memory'})
        active = state.get('active')
        workers = [p for p in server.get('processes', []) if p.get('role') == 'http_worker']
        if active or workers:
            measuring.append(region)
            if not start <= now < end + 5:
                alerts.append({'region': region, 'issue': 'traffic_outside_regional_window'})
        jobs = state['jobs']
        bad = [j for j in jobs if j['status'] in
               ('failed', 'interrupted', 'deadline', 'not_started_deadline')]
        if bad:
            alerts.append({'region': region, 'issue': 'incomplete_jobs', 'count': len(bad)})
        late = [j for j in jobs if j['status'] == 'pending'
                and moment(j['scheduled_at']).timestamp() + 3600 < now < end]
        if late:
            alerts.append({'region': region, 'issue': 'round_backlog', 'count': len(late)})
        complete = sum(j['status'] in ('completed', 'no_list') for j in jobs)
        if now > end + 30 and (state['status'] not in ('closed', 'completed')
                              or complete != len(jobs)):
            alerts.append({'region': region, 'issue': 'region_not_complete_at_deadline'})
        progress = state.get('progress') or {}
        attempts = progress.get('requests', 0)
        rate = progress.get('http_469_responses', 0)
        if active and rate:
            alerts.append({'region': region, 'issue': 'http_469_observed', 'count': rate,
                           'completed_attempts': attempts})
        other = attempts - rate - progress.get('successes', 0)
        if active and attempts - rate >= 1000 and other / (attempts - rate) > 0.10:
            alerts.append({'region': region, 'issue': 'high_other_error_rate',
                           'other_errors': other, 'non_469_attempts': attempts - rate})
        regions.append({'region': region, **window, 'status': state['status'],
                        'completed_jobs': complete, 'expected_jobs': len(jobs),
                        'requests': server.get('requests', 0),
                        'observations': sum(j.get('observations', 0) for j in jobs)
                        + (progress.get('observations_saved', 0) if active else 0),
                        'active': active, 'worker_count': len(workers)})
    if manifest.get('execution_mode') == 'sequential' and len(measuring) > 1:
        alerts.append({'issue': 'overlapping_regions', 'regions': measuring})
    return {'checked_at': timestamp(now), 'campaign_id': manifest['campaign_id'],
            'campaign_sha256': fingerprint(manifest), 'alerts': alerts, 'regions': regions,
            'measurement_complete': len(regions) == 8 and all(
                r['completed_jobs'] == r['expected_jobs']
                and r['status'] in ('closed', 'completed') for r in regions)}


async def watch(args):
    manifest = load(args.manifest)
    with lock(args.output / '.health.lock'):
        last_alert = 0.0
        while True:
            snapshot = await collect(args.inventory, args.output)
            result = audit(manifest, snapshot, time.time())
            atomic_json(args.output / 'campaign-audit.json', result)
            if result['alerts'] and time.time() - last_alert >= 60:
                last_alert = time.time()
                name = str(int(time.time())) + '.json'
                atomic_json(args.output / 'alerts' / name, result)
            if not args.watch:
                return result
            await asyncio.sleep(10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    asyncio.run(watch(args))


if __name__ == '__main__':
    main()
