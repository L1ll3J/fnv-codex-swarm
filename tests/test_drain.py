import hashlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from native_swarm.drain import release_unlaunched
from native_swarm.state import atomic, read


def fixture(tmp_path):
    claim = tmp_path / 'claims/c'
    assignment = {'claim_id': 'c', 'worker': 'w', 'model': 'model'}
    atomic(tmp_path / 'assignments/w.json', assignment)
    atomic(claim / 'assignment.json', assignment)
    atomic(claim / 'claim.json', {'claim_id': 'c'})
    atomic(claim / 'worker-assignment.json', {
        'worker': 'w', 'claim_id': 'c', 'thread_source': 'codex_exec_direct', 'thread_id': None})
    atomic(tmp_path / 'run-config.json', {'model': 'model'})
    job = {'claim_id': 'c', 'prepared_gate_version': 1, 'state': 'prepared_waiting_transient_gate',
           'prepared_assignment_sha256': hashlib.sha256((claim / 'assignment.json').read_bytes()).hexdigest()}
    return SimpleNamespace(root=tmp_path, journal={'jobs': {'w': job}}, active={}, journal_save=Mock())


def test_drain_releases_only_proven_unlaunched_claim_once(tmp_path, monkeypatch):
    executor = fixture(tmp_path)
    service = types.ModuleType('service')
    service.call = Mock(side_effect=[{'open_claims': [{'claim_id': 'c', 'this_key': True}]}, {'released': True}])
    service.guard = Mock()
    monkeypatch.setitem(sys.modules, 'service', service)
    release_unlaunched(executor, 'w')
    assert read(tmp_path / 'claims/c/giveup-response.json') == {'released': True}
    assert executor.journal['jobs']['w']['state'] == 'released_before_launch'
    assert service.call.call_count == 2
    release_unlaunched(executor, 'w')
    assert service.call.call_count == 2


@pytest.mark.parametrize('boundary', ['launch_intent_at', 'thread', 'changed', 'uncertain', 'approval'])
def test_drain_retains_ambiguous_or_executed_claims(tmp_path, monkeypatch, boundary):
    executor = fixture(tmp_path)
    service = types.ModuleType('service')
    service.call = Mock(side_effect=AssertionError('No service write allowed'))
    service.guard = Mock()
    monkeypatch.setitem(sys.modules, 'service', service)
    if boundary == 'launch_intent_at':
        executor.journal['jobs']['w']['launch_intent_at'] = 'started'
    elif boundary == 'thread':
        path = tmp_path / 'claims/c/worker-assignment.json'
        atomic(path, {**read(path), 'thread_id': 'real-thread'})
    elif boundary == 'changed':
        atomic(tmp_path / 'assignments/w.json', {'changed': True})
    elif boundary == 'approval':
        atomic(tmp_path / 'approval-blocker-current.json', {'rejected': True})
    else:
        atomic(tmp_path / 'claims/c/drain-release-intent.json', {'state': 'request_attempt_started'})
    with pytest.raises(RuntimeError):
        release_unlaunched(executor, 'w')
    assert service.call.call_count == 0


def test_drain_uncertain_release_never_retries(tmp_path, monkeypatch):
    executor = fixture(tmp_path)
    service = types.ModuleType('service')
    service.call = Mock(side_effect=[{'open_claims': [{'claim_id': 'c', 'this_key': True}]}, OSError('transport')])
    service.guard = Mock()
    monkeypatch.setitem(sys.modules, 'service', service)
    with pytest.raises(OSError):
        release_unlaunched(executor, 'w')
    with pytest.raises(RuntimeError, match='no automatic retry'):
        release_unlaunched(executor, 'w')
    assert service.call.call_count == 2
