from copy import deepcopy
from datetime import timedelta

import pytest
from test_onboarding import START
from test_onboarding_sequential import ORDER, make

from proxy_index.onboarding import manifest as campaign
from proxy_index.onboarding.backup import tree_inventory, verify_region
from proxy_index.onboarding.files import atomic_json
from proxy_index.onboarding.health import audit
from proxy_index.onboarding.scheduler import job_directory, new_state


def snapshot(m, now):
    servers = []
    for region in ORDER:
        s = new_state(m, region, m['settings'])
        s['updated_at'] = campaign.timestamp(now)
        servers.append({'region': region, 'state': s, 'service': {'MainPID': '123'},
                        'server_time': now})
    return {'servers': servers}


def test_audit_armed_days_and_overlap(tmp_path):
    m = campaign.load(make(tmp_path, execution_mode='sequential', region_order=ORDER))
    now = START.timestamp() - 60
    snap = snapshot(m, now)
    assert not audit(m, snap, now)['alerts']
    snap['servers'][1]['state']['active'] = {'country': 'ca'}
    snap['servers'][0]['state']['active'] = {'country': 'us'}
    issues = {a['issue'] for a in audit(m, snap, now)['alerts']}
    assert {'overlapping_regions', 'traffic_outside_regional_window'} <= issues


def test_audit_missed_jobs_and_dead_heartbeat(tmp_path):
    m = campaign.load(make(tmp_path, execution_mode='sequential', region_order=ORDER))
    now = (START + timedelta(days=1, minutes=1)).timestamp()
    snap = snapshot(m, now)
    snap['servers'][0]['state']['status'] = 'closed'
    snap['servers'][1]['state']['updated_at'] = campaign.timestamp(now - 60)
    snap['servers'][1]['service']['MainPID'] = '0'
    issues = {a['issue'] for a in audit(m, snap, now)['alerts']}
    assert {'region_not_complete_at_deadline', 'stale_heartbeat', 'scheduler_not_running'} <= issues
    assert not audit(m, snap, now)['measurement_complete']


def closed_empty_region(tmp_path):
    path = make(tmp_path, execution_mode='sequential', region_order=ORDER)
    m = campaign.load(path)
    s = new_state(m, 'tor1', m['settings'])
    directory = path.parent / 'regions' / 'tor1'
    s['status'] = 'closed'
    for job in s['jobs']:
        job.update(status='completed', started_at=job['scheduled_at'],
                   finished_at=job['scheduled_at'], observations=0, requests=1000, errors=1000)
        atomic_json(job_directory(directory, job) / 'summary.json', job | {
            'measurement': {'successes': 0, 'completed': 1000}})
    atomic_json(directory / 'state.json', s)
    return m, s, directory


def test_backup_accepts_completed_no_success_but_detects_summary_and_tree_damage(tmp_path):
    m, state, directory = closed_empty_region(tmp_path)
    expected = tree_inventory(directory)
    receipt = verify_region(m, 'tor1', directory, expected)
    assert receipt['verified_backup'] and receipt['complete_measurement']
    assert receipt['completed_jobs'] == 72 and receipt['observations'] == 0
    summary = job_directory(directory, state['jobs'][0]) / 'summary.json'
    summary.write_text('{}')
    with pytest.raises(ValueError, match='remote original'):
        verify_region(m, 'tor1', directory, expected)
    with pytest.raises(ValueError, match='summary'):
        verify_region(m, 'tor1', directory, tree_inventory(directory))


def test_backup_never_treats_terminal_failure_as_complete(tmp_path):
    m, state, directory = closed_empty_region(tmp_path)
    state = deepcopy(state)
    state['jobs'][0]['status'] = 'not_started_deadline'
    atomic_json(directory / 'state.json', state)
    receipt = verify_region(m, 'tor1', directory, tree_inventory(directory))
    assert receipt['verified_backup'] and not receipt['complete_measurement']
    assert receipt['completed_jobs'] == 71


def test_remote_tree_checks_only_the_current_unit_name():
    from proxy_index.onboarding.backup import REMOTE_TREE
    compile(REMOTE_TREE, 'remote_tree', 'exec')
    assert ("unit='proxy-index-onboarding-'+state['campaign_id']+'-'+state['region']+'.service'"
            in REMOTE_TREE)
    assert REMOTE_TREE.count('.service') == 1
