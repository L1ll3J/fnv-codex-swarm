"""Controller handover never signals a worker or discards unfinished evidence."""

from unittest.mock import Mock

import pytest

from native_swarm.handover import idle_reason, retire_idle
from native_swarm.profiles import default_profile
from native_swarm.state import atomic, read


def setup_idle(tmp_path, monkeypatch):
    state, root = tmp_path / "state", tmp_path / "run"
    running = default_profile(1)
    atomic(root / "native-app-profile.json", running)
    atomic(
        root / "native-app-managed.json",
        {"state_dir": str(state), "allocation_version": 1},
    )
    atomic(root / ".rolling-executor-owner.json", {"pid": 123})
    atomic(root / "rolling-executor/journal.json", {"jobs": {}})
    atomic(
        state / "config.json",
        {"managed_runs": [str(root)], "profiles": [{**running, "group_size": 2}]},
    )
    monkeypatch.setattr(
        "native_swarm.handover.process_alive", lambda pid, root: pid == 123
    )
    terminate = Mock()
    monkeypatch.setattr("native_swarm.handover.terminate_controller", terminate)
    return state, root, terminate


def test_idle_withdrawn_controller_retires_only_its_owner(tmp_path, monkeypatch):
    state, root, terminate = setup_idle(tmp_path, monkeypatch)
    claim = root / "claims/closed"
    atomic(claim / "claim.json", {"claim_id": "closed"})
    atomic(claim / "giveup-response.json", {"released": True})
    atomic(
        root / "rolling-executor/journal.json",
        {"jobs": {"finished": {"state": "awaiting_campaign_closure", "pid": 456}}},
    )
    retire_idle(root, state)
    terminate.assert_called_once_with(123, root, {"pid": 123})
    assert read(root / "operator-profile-handover.json")["state"] == "retired"
    assert read(root / ".rolling-executor-owner.json")["pid"] == 123
    assert read(claim / "giveup-response.json")["released"] is True


@pytest.mark.parametrize(
    "state_name",
    [
        "preparing",
        "launch_intent",
        "running",
        "prepared",
        "prepared_waiting_transient_gate",
        "failed_review_required",
    ],
)
def test_unfinished_or_ambiguous_job_prevents_retirement(
    tmp_path, monkeypatch, state_name
):
    state, root, terminate = setup_idle(tmp_path, monkeypatch)
    atomic(
        root / "rolling-executor/journal.json", {"jobs": {"job": {"state": state_name}}}
    )
    with pytest.raises(RuntimeError, match="worker, preparation"):
        retire_idle(root, state)
    terminate.assert_not_called()


@pytest.mark.parametrize("marker", ["STOP_NEW_CLAIMS", "operator-drain.json"])
def test_stop_and_drain_are_preserved(tmp_path, monkeypatch, marker):
    state, root, terminate = setup_idle(tmp_path, monkeypatch)
    (root / marker).write_text("protected")
    with pytest.raises(RuntimeError, match="protected stop"):
        retire_idle(root, state)
    assert (root / marker).read_text() == "protected"
    terminate.assert_not_called()


def test_unclosed_claim_prevents_retirement_even_with_no_workers(tmp_path, monkeypatch):
    state, root, terminate = setup_idle(tmp_path, monkeypatch)
    atomic(root / "claims/open/claim.json", {"claim_id": "open"})
    with pytest.raises(RuntimeError, match="no acknowledged closure"):
        retire_idle(root, state)
    terminate.assert_not_called()


def test_live_worker_prevents_retirement_even_after_closure(tmp_path, monkeypatch):
    state, root, terminate = setup_idle(tmp_path, monkeypatch)
    atomic(
        root / "rolling-executor/journal.json",
        {"jobs": {"job": {"state": "awaiting_campaign_closure", "pid": 123}}},
    )
    with pytest.raises(RuntimeError, match="worker process"):
        retire_idle(root, state)
    terminate.assert_not_called()


def test_unsealed_group_prevents_retirement(tmp_path, monkeypatch):
    state, root, terminate = setup_idle(tmp_path, monkeypatch)
    claim = root / "claims/group"
    atomic(claim / "claim.json", {"claim_id": "group"})
    atomic(claim / "submission-response.json", {"submission_id": "sub"})
    atomic(claim / "collaboration/group.json", {"state": "running"})
    with pytest.raises(RuntimeError, match="collaboration members"):
        retire_idle(root, state)
    terminate.assert_not_called()


def test_manual_pause_and_compatible_profile_are_not_retired(tmp_path, monkeypatch):
    state, root, terminate = setup_idle(tmp_path, monkeypatch)
    atomic(root / "operator-admissions-paused.json", {"reason": "operator paused"})
    with pytest.raises(RuntimeError, match="operator admission pause"):
        retire_idle(root, state)
    cfg = read(state / "config.json")
    cfg["profiles"] = [read(root / "native-app-profile.json")]
    atomic(state / "config.json", cfg)
    with pytest.raises(RuntimeError, match="no longer requires"):
        retire_idle(root, state)
    terminate.assert_not_called()


def test_missing_journal_is_not_assumed_idle(tmp_path, monkeypatch):
    _, root, _ = setup_idle(tmp_path, monkeypatch)
    (root / "rolling-executor/journal.json").unlink()
    assert "journal" in idle_reason(root)


@pytest.mark.parametrize("wrong", ["script", "owner"])
def test_stable_process_handle_does_not_signal_changed_identity(
    tmp_path, monkeypatch, wrong
):
    from pathlib import Path
    from native_swarm.handover import terminate_controller

    root = tmp_path / "run"
    owner = {"pid": 123, "generation": "original"}
    atomic(root / ".rolling-executor-owner.json", owner)
    if wrong == "owner":
        atomic(
            root / ".rolling-executor-owner.json",
            {**owner, "generation": "replacement"},
        )
    script = "worker.py" if wrong == "script" else "app_controller.py"
    original = Path.read_bytes
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda path: (
            b"python\0-u\0" + str(root / script).encode() + b"\0"
            if str(path) == "/proc/123/cmdline"
            else original(path)
        ),
    )
    monkeypatch.setattr("native_swarm.handover.os.pidfd_open", lambda pid: 456)
    close = Mock()
    send = Mock()
    monkeypatch.setattr("native_swarm.handover.os.close", close)
    monkeypatch.setattr("native_swarm.handover.signal.pidfd_send_signal", send)
    with pytest.raises(RuntimeError, match="identity changed"):
        terminate_controller(123, root, owner)
    send.assert_not_called()
    close.assert_called_once_with(456)


def test_withdrawn_preparation_without_claim_allows_idle_retirement(
    tmp_path, monkeypatch
):
    state, root, terminate = setup_idle(tmp_path, monkeypatch)
    atomic(
        root / "rolling-executor/journal.json",
        {"jobs": {"withdrawn": {"state": "allocation_withdrawn_no_launch"}}},
    )
    retire_idle(root, state)
    terminate.assert_called_once_with(123, root, {"pid": 123})


@pytest.mark.parametrize("remaining", ["worker", "claim"])
def test_withdrawn_preparation_still_checks_workers_and_claims(
    tmp_path, monkeypatch, remaining
):
    state, root, terminate = setup_idle(tmp_path, monkeypatch)
    atomic(
        root / "rolling-executor/journal.json",
        {
            "jobs": {
                "withdrawn": {
                    "state": "allocation_withdrawn_no_launch",
                    "pid": 123 if remaining == "worker" else None,
                }
            }
        },
    )
    if remaining == "claim":
        atomic(root / "claims/open/claim.json", {"claim_id": "open"})
    with pytest.raises(RuntimeError, match="worker process|no acknowledged closure"):
        retire_idle(root, state)
    terminate.assert_not_called()
