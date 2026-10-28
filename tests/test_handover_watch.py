"""Pending replacement changes only the requested profile after authoritative closure."""

from unittest.mock import Mock

import pytest

from native_swarm.backend import Backend
from native_swarm.handover_watch import advance
from native_swarm.profiles import default_profile
from native_swarm.state import atomic, read


def fixture(tmp_path, monkeypatch):
    state, root = tmp_path / "state", tmp_path / "old"
    current = default_profile(1)
    wanted = {**current, "model": "gpt-6.1-terra"}
    atomic(root / "native-app-profile.json", current)
    atomic(
        root / "native-app-managed.json",
        {"state_dir": str(state), "allocation_version": 1},
    )
    atomic(root / "operator-admissions-paused.json", {"source": "profile-allocation"})
    atomic(root / "operator-profile-handover-pending.json", {"state": "pending"})
    atomic(root / ".rolling-executor-owner.json", {"pid": 1})
    atomic(root / "rolling-executor/journal.json", {"jobs": {}})
    atomic(
        state / "config.json",
        {
            "managed_runs": [str(root)],
            "profiles": [wanted],
            "worker_capacity": 10,
            "template_run": str(tmp_path / "template"),
        },
    )
    backend = Backend(state)
    backend.quota = Mock()
    backend.verify_template = Mock()
    backend.preflight = Mock(return_value={"open_claims": []})
    backend.create_run = Mock()
    monkeypatch.setattr("native_swarm.handover_watch.retire_idle", Mock())
    monkeypatch.setattr(
        "native_swarm.handover_watch.process_alive", lambda pid, root: bool(pid)
    )
    return backend, root, wanted


def test_waits_until_original_claim_is_closed_then_starts_requested_profile(
    tmp_path, monkeypatch
):
    backend, root, wanted = fixture(tmp_path, monkeypatch)
    claim = root / "claims/claim"
    atomic(claim / "claim.json", {"claim_id": "claim"})
    assert advance(root, backend) is False
    backend.quota.assert_not_called()
    backend.create_run.assert_not_called()
    atomic(claim / "giveup-response.json", {"released": True})
    assert advance(root, backend) is True
    backend.create_run.assert_called_once()
    assert backend.create_run.call_args.args[3] == wanted
    assert read(root / "operator-profile-handover-pending.json")["state"] == "completed"
    assert advance(root, backend) is True
    backend.create_run.assert_called_once()  # no duplicate admission


@pytest.mark.parametrize(
    "stop",
    [
        "manual_pause",
        "STOP_NEW_CLAIMS",
        "operator-drain.json",
        "zero_slots",
        "restored_identity",
    ],
)
def test_explicit_controls_cancel_pending_admission(tmp_path, monkeypatch, stop):
    backend, root, wanted = fixture(tmp_path, monkeypatch)
    if stop == "manual_pause":
        atomic(root / "operator-admissions-paused.json", {"requested_at": "now"})
    elif stop in ("zero_slots", "restored_identity"):
        cfg = backend.config
        cfg["profiles"][0] = (
            {**wanted, "instances": 0} if stop == "zero_slots" else default_profile(1)
        )
        atomic(backend.state / "config.json", cfg)
    else:
        (root / stop).touch()
    assert advance(root, backend) is True
    assert read(root / "operator-profile-handover-pending.json")["state"] == "cancelled"
    backend.quota.assert_not_called()
    backend.create_run.assert_not_called()


def test_usage_cutoff_defers_without_retiring_or_starting(tmp_path, monkeypatch):
    backend, root, _ = fixture(tmp_path, monkeypatch)
    retire = Mock()
    monkeypatch.setattr("native_swarm.handover_watch.retire_idle", retire)
    backend.quota.side_effect = RuntimeError("usage cutoff")
    assert advance(root, backend) is True
    assert read(root / "operator-profile-handover-pending.json")["state"] == "blocked"
    retire.assert_not_called()
    backend.create_run.assert_not_called()


def test_start_failure_is_retained_without_automatic_retry(tmp_path, monkeypatch):
    backend, root, _ = fixture(tmp_path, monkeypatch)
    backend.create_run.side_effect = RuntimeError("ambiguous launch")
    assert advance(root, backend) is True
    assert (
        read(root / "operator-profile-handover-pending.json")["state"]
        == "review_required"
    )
    assert advance(root, backend) is True
    backend.create_run.assert_called_once()


def test_other_profiles_are_never_resumed(tmp_path, monkeypatch):
    backend, root, _ = fixture(tmp_path, monkeypatch)
    other = tmp_path / "other"
    atomic(other / "native-app-profile.json", {**default_profile(2), "id": "other"})
    atomic(other / ".rolling-executor-owner.json", {"pid": 2})
    atomic(other / "operator-admissions-paused.json", {"reason": "operator paused"})
    cfg = backend.config
    cfg["managed_runs"].append(str(other))
    atomic(backend.state / "config.json", cfg)
    assert advance(root, backend) is True
    assert read(other / "operator-admissions-paused.json") == {
        "reason": "operator paused"
    }
    backend.create_run.assert_called_once()


def test_other_controller_protected_stop_blocks_replacement(tmp_path, monkeypatch):
    backend, root, _ = fixture(tmp_path, monkeypatch)
    other = tmp_path / "other"
    atomic(other / ".rolling-executor-owner.json", {"pid": 2})
    (other / "STOP_NEW_CLAIMS").write_text("integrity")
    cfg = backend.config
    cfg["managed_runs"].append(str(other))
    atomic(backend.state / "config.json", cfg)
    assert advance(root, backend) is True
    assert read(root / "operator-profile-handover-pending.json")["state"] == "blocked"
    backend.create_run.assert_not_called()


@pytest.mark.parametrize("protected", ["replacement_start_intent", "review_required"])
def test_concurrent_request_cannot_overwrite_controller_launch_state(
    tmp_path, monkeypatch, protected
):
    import threading
    from native_swarm.handover_watch import request

    backend, root, _ = fixture(tmp_path, monkeypatch)
    marker = root / "operator-profile-handover-pending.json"
    attempted = threading.Event()
    finished = threading.Event()
    spawn = Mock()
    monkeypatch.setattr("native_swarm.handover_watch.subprocess.Popen", spawn)
    errors, outcomes = [], []

    def publish_request():
        attempted.set()
        try:
            outcomes.append(request(root, backend.state))
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    # advance() owns this exact lock throughout retirement/start and publication
    # of the final outcome. A simultaneous save must observe the final state.
    with backend.locked():
        thread = threading.Thread(target=publish_request)
        thread.start()
        assert attempted.wait(2)
        assert not finished.wait(0.1)
        atomic(marker, {"state": "replacement_start_intent"})
        atomic(marker, {"state": protected})
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert not errors
    assert outcomes == [False]
    assert read(marker)["state"] == protected
    spawn.assert_not_called()


def test_request_with_existing_control_lock_does_not_reacquire_it(
    tmp_path, monkeypatch
):
    from native_swarm.handover_watch import request

    backend, root, _ = fixture(tmp_path, monkeypatch)
    spawn = Mock()
    monkeypatch.setattr("native_swarm.handover_watch.subprocess.Popen", spawn)
    with backend.locked():
        request(root, backend.state, control_locked=True)
    spawn.assert_called_once()
    assert read(root / "operator-profile-handover-pending.json")["state"] == "pending"
