from unittest.mock import Mock

import pytest

from native_swarm.state import read
from native_swarm.status_poll import StatusPollDeferred, configure, read_status


@pytest.mark.parametrize('code', [5, 6, 7, 18, 28, 35, 52, 55, 56])
def test_status_transport_retry_recovers_without_other_requests(tmp_path, code):
    account = {'strikes_last_7_days': 0, 'submissions_paused': False}
    call = Mock(side_effect=[RuntimeError(f'curl: ({code}) Connection unavailable\n'), account])
    sleep = Mock()
    assert read_status(call, tmp_path, sleep=sleep) is account
    assert call.call_args_list == [(('status', {}),), (('status', {}),)]
    sleep.assert_called_once_with(1)
    assert read(tmp_path / 'status-poll-health.json')['state'] == 'current'
    assert not (tmp_path / 'STOP_NEW_CLAIMS').exists()


def test_exhausted_status_transport_defers_then_recovers_next_poll(tmp_path):
    call = Mock(side_effect=RuntimeError('curl: (28) Operation timed out'))
    sleep = Mock()
    with pytest.raises(StatusPollDeferred):
        read_status(call, tmp_path, sleep=sleep)
    assert call.call_count == 3
    assert [c.args for c in sleep.call_args_list] == [(1,), (2,)]
    assert read(tmp_path / 'status-poll-health.json')['state'] == 'deferred'
    assert not (tmp_path / 'STOP_NEW_CLAIMS').exists()
    assert read_status(Mock(return_value={'ok': True}), tmp_path) == {'ok': True}
    assert read(tmp_path / 'status-poll-health.json')['state'] == 'current'


@pytest.mark.parametrize('error', [RuntimeError('status: HTTP 403: denied'), RuntimeError('status: HTTP 429: busy'), RuntimeError('curl: (3) Invalid URL'), RuntimeError('Account health stop'), ValueError('Invalid JSON')])
def test_other_errors_remain_genuine_failures(tmp_path, error):
    call = Mock(side_effect=error)
    sleep = Mock()
    with pytest.raises(type(error), match=str(error).replace('(', r'\(').replace(')', r'\)')):
        read_status(call, tmp_path, sleep=sleep)
    call.assert_called_once_with('status', {})
    sleep.assert_not_called()


def test_successful_status_preserves_account_health_evidence(tmp_path):
    account = {'strikes_last_7_days': 1, 'submissions_paused': True}
    assert read_status(Mock(return_value=account), tmp_path) is account


def test_generated_watcher_continues_typed_deferrals_and_keeps_real_stops(tmp_path):
    resolver = tmp_path / 'resolve_ready.py'
    watcher = tmp_path / 'campaign_watch.py'
    resolver.write_text("def once():\n status=call('status',{});save('account-current.json',status)\n")
    watcher.write_text("def failure(exc):\n try:\n  raise exc\n except Exception as exc:\n  deferred=any(reason in str(exc) for reason in ('HTTP 429:',))\n  if deferred:return 'wait'\n  stop_admission(str(exc));return 'stop'\n")
    configure(tmp_path)
    namespace = {'stop_admission': Mock()}
    exec(compile(watcher.read_text(), str(watcher), 'exec'), namespace)
    assert namespace['failure'](StatusPollDeferred('temporarily unavailable')) == 'wait'
    namespace['stop_admission'].assert_not_called()
    assert namespace['failure'](RuntimeError('validation failed')) == 'stop'
    namespace['stop_admission'].assert_called_once_with('validation failed')
    assert 'status=read_status(call,ROOT)' in resolver.read_text()


def test_template_validation_precedes_edits(tmp_path):
    resolver = tmp_path / 'resolve_ready.py'
    original = " status=call('status',{});save('account-current.json',status)"
    resolver.write_text(original)
    (tmp_path / 'campaign_watch.py').write_text('unsupported template')
    with pytest.raises(ValueError, match='Unsupported status-poll template'):
        configure(tmp_path)
    assert resolver.read_text() == original
