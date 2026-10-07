"""Private, resumable regional backup with whole-tree and observation verification."""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path

from .export import export_campaign, verify_chunk
from .files import atomic_json, digest, fingerprint, lock, private_dir, read_json
from .manifest import load, regional_window
from .scheduler import job_directory, validate_state


def tree_inventory(directory: Path) -> dict:
    result = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError('Backup may not contain symlinks')
        if path.is_file():
            result[str(path.relative_to(directory))] = {
                'size': path.stat().st_size, 'sha256': digest(path),
            }
    return result


def verify_region(manifest: dict, region: str, directory: Path, expected: dict) -> dict:
    if tree_inventory(directory) != expected:
        raise ValueError('Local files differ from the remote original tree')
    state = read_json(directory / 'state.json')
    validate_state(state, manifest, region)
    if state['status'] not in ('closed', 'completed'):
        raise ValueError('Regional scheduler has not closed naturally')
    rows = chunks = 0
    incomplete = []
    end = regional_window(manifest, region)['ends_at']
    for job in state['jobs']:
        if job['status'] != 'completed':
            incomplete.append({'round': job['round'], 'country': job['country'],
                               'status': job['status']})
        folder = job_directory(directory, job)
        summary_path = folder / 'summary.json'
        summary = read_json(summary_path) if summary_path.exists() else None
        if job['status'] == 'completed':
            if summary is None or any(summary.get(k) != job.get(k) for k in (
                'country', 'round', 'scheduled_at', 'started_at', 'finished_at',
                'status', 'observations', 'requests', 'errors',
            )):
                raise ValueError('Country summary differs from checkpoint')
            measurement = summary.get('measurement')
            if (measurement is None or measurement['successes'] != job['observations']
                    or measurement['completed'] != job['requests']):
                raise ValueError('Measurement counters differ from checkpoint')
        count = 0
        committed = set()
        for sidecar in sorted(folder.glob('observations-*.jsonl.meta.json')):
            metadata = read_json(sidecar)
            filename = metadata['file']
            if Path(filename).name != filename:
                raise ValueError('Invalid observation filename')
            chunk = folder / filename
            if chunk.with_suffix('.meta.json') != sidecar:
                raise ValueError('Invalid observation sidecar')
            count += verify_chunk(chunk, metadata, job, end)
            committed.add(filename)
            chunks += 1
        if count != job.get('observations', 0):
            raise ValueError('Observation rows differ from checkpoint')
        if any(p.name.endswith('.partial') or (p.name.endswith('.gz') and p.name not in committed)
               for p in folder.glob('observations-*')):
            raise ValueError('Uncommitted observations present')
        rows += count
    return {'region': region, 'verified_backup': True, 'complete_measurement': not incomplete,
            'jobs': len(state['jobs']), 'completed_jobs': len(state['jobs']) - len(incomplete),
            'incomplete_jobs': incomplete, 'observations': rows, 'chunks': chunks,
            'requests': sum(j.get('requests', 0) for j in state['jobs']),
            'files': len(expected), 'tree_sha256': fingerprint(expected)}


REMOTE_TREE = '''
import json,pathlib,sys,subprocess
from milf_framework.onboarding.backup import tree_inventory
from milf_framework.onboarding.files import read_json
root=pathlib.Path(sys.argv[1]);state=read_json(root/'state.json')
assert state['status'] in ('closed','completed'), 'Region has not closed naturally'
unit='milf-onboarding-'+state['campaign_id']+'-'+state['region']+'.service'
assert subprocess.check_output(['systemctl','show',unit,'--property=MainPID','--value'],
                               text=True).strip() in ('','0'), 'Region process is still alive'
print(json.dumps(tree_inventory(root)))
'''


def collect_region(manifest_path: Path, inventory_path: Path, region: str) -> dict:
    manifest = load(manifest_path)
    if region not in manifest['regions']:
        raise ValueError('Unknown campaign region')
    server = read_json(inventory_path)['servers'][region]
    root = manifest_path.parent
    backup = root / 'backup'
    private_dir(backup)
    options = ['-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'ConnectTimeout=15',
               '-i', os.path.expanduser(server['identity_file'])]
    target = server.get('user', 'root') + '@' + server['host']
    remote = (Path(server['root']) / 'onboarding-networks' / manifest['provider'] /
              manifest['product'] / manifest['campaign_id'] / 'regions' / region)
    with lock(backup / (region + '.lock')):
        command = shlex.join([server['root'] + '/venv/bin/python', '-c', REMOTE_TREE, str(remote)])
        with (backup / (region + '-remote.log')).open('w') as log:
            tree = subprocess.run(['ssh', *options, target, command], capture_output=True,
                                  text=True, timeout=1800)
            log.write(tree.stderr)
        if tree.returncode:
            raise RuntimeError('Remote tree check failed; inspect private backup log')
        expected = json.loads(tree.stdout)
        atomic_json(backup / (region + '-remote-tree.json'), expected)
        destination = root / 'regions' / region
        staging = backup / (region + '.staging')
        if not destination.exists():
            private_dir(staging)
            with (backup / (region + '-transfer.log')).open('a') as log:
                command = [
                    'rsync', '-rlt', '--checksum', '--partial', '--partial-dir=.rsync-partial',
                    '--bwlimit=1024', '--timeout=180', '-e', shlex.join(['ssh', *options]),
                    target + ':' + shlex.quote(str(remote) + '/'), str(staging) + '/',
                ]
                # A timeout must not leave an SSH child writing to staging.
                with subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                      start_new_session=True) as transfer:
                    try:
                        transfer.wait(timeout=6 * 3600)
                    except subprocess.TimeoutExpired:
                        os.killpg(transfer.pid, signal.SIGTERM)
                        try:
                            transfer.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(transfer.pid, signal.SIGKILL)
                            transfer.wait()
                        raise RuntimeError('Transfer timed out; staging preserved') from None
            if transfer.returncode:
                raise RuntimeError('Transfer incomplete; staging preserved for resumable retry')
            receipt = verify_region(manifest, region, staging, expected)
            private_dir(destination.parent)
            staging.rename(destination)
        else:
            receipt = verify_region(manifest, region, destination, expected)
        receipt.update(campaign_id=manifest['campaign_id'],
                       campaign_sha256=fingerprint(manifest), directory=str(destination),
                       remote_originals_preserved=True)
        atomic_json(backup / (region + '.json'), receipt)
    return receipt


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--region', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    receipt = collect_region(args.manifest, args.inventory, args.region)
    print(json.dumps(receipt))
    manifest = load(args.manifest)
    root = args.manifest.parent
    receipts = [root / 'backup' / (r + '.json') for r in manifest['regions']]
    if all(p.exists() and read_json(p).get('complete_measurement') for p in receipts):
        exported = export_campaign(args.manifest)
        if (not exported['complete']
                or exported['sha256'] != digest(root / 'export' / exported['file'])):
            raise ValueError('Final campaign export did not verify')
        atomic_json(root / 'backup' / 'complete.json', {
            'verified': True, 'regions': len(receipts), 'observations': exported['rows'],
            'chunks': exported['chunks'], 'export_sha256': exported['sha256'],
            'jobs': sum(read_json(p)['completed_jobs'] for p in receipts),
            'report': str(root / 'export' / 'report.html'),
        })
    return 0


if __name__ == '__main__':
    sys.exit(main())
