"""Profile edits close proven unlaunched claims through their owning controller."""

import fcntl
import hashlib
import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import Mock

import pytest

from native_swarm.backend import Backend
from native_swarm.handover_watch import advance
from native_swarm.profiles import default_profile
from native_swarm.state import atomic, read


def setup(tmp_path, monkeypatch):
    class Parent:
        def try_launch_prepared(self, worker):
            return "base-gate"

    parent = types.ModuleType("random_executor")
    parent.RandomExecutor = Parent
    base = types.ModuleType("rolling_executor")
    base.read_json = read
    monkeypatch.setitem(sys.modules, "random_executor", parent)
    monkeypatch.setitem(sys.modules, "rolling_executor", base)
    source = Path(__file__).parents[1] / "src/native_swarm/controller.py"
    spec = importlib.util.spec_from_file_location("withdrawn_controller", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root, state = tmp_path / "run", tmp_path / "state"
    running = default_profile(1)
    wanted = {**running, "group_size": 2}
    assignment = {"claim_id": "c", "worker": "w", "model": running["model"]}
    claim = root / "claims/c"
    atomic(root / "assignments/w.json", assignment)
    atomic(claim / "assignment.json", assignment)
    atomic(claim / "claim.json", {"claim_id": "c"})
    atomic(
        claim / "worker-assignment.json",
        {
            "worker": "w",
            "claim_id": "c",
            "thread_source": "codex_exec_direct",
            "thread_id": None,
        },
    )
    atomic(root / "run-config.json", {"model": running["model"]})
    atomic(root / "native-app-profile.json", running)
    atomic(
        root / "native-app-managed.json",
        {"state_dir": str(state), "withdrawn_preparation_closure_version": 1},
    )
    atomic(root / "operator-admissions-paused.json", {"source": "profile-allocation"})
    atomic(root / "operator-profile-handover-pending.json", {"state": "pending"})
    atomic(
        state / "config.json",
        {
            "managed_runs": [str(root)],
            "profiles": [wanted],
            "worker_capacity": 10,
            "template_run": str(tmp_path / "template"),
        },
    )
    executor = module.ManagedExecutor()
    executor.root = root
    executor.active = {"unrelated-running-worker": {"untouched": True}}
    executor.journal = {
        "jobs": {
            "w": {
                "claim_id": "c",
                "prepared_gate_version": 1,
                "state": "prepared_waiting_transient_gate",
                "prepared_assignment_sha256": hashlib.sha256(
                    (claim / "assignment.json").read_bytes()
                ).hexdigest(),
            }
        }
    }
    executor.journal_save = lambda: atomic(
        root / "rolling-executor/journal.json", executor.journal
    )
    executor.journal_save()
    service = types.ModuleType("service")
    service.call = Mock(
        side_effect=[
            {"open_claims": [{"claim_id": "c", "this_key": True}]},
            {"released": True},
        ]
    )
    service.guard = Mock()
    monkeypatch.setitem(sys.modules, "service", service)
    return executor, state, service


def test_prepared_withdrawal_closes_claim_then_handover_proceeds(tmp_path, monkeypatch):
    executor, state, service = setup(tmp_path, monkeypatch)
    atomic(executor.root / "claims/other/claim.json", {"claim_id": "other"})
    executor.journal["jobs"]["unrelated-running-worker"] = {
        "state": "running",
        "claim_id": "other",
    }
    executor.journal_save()
    backend = Backend(state)
    backend.quota = Mock()
    backend.verify_template = Mock()
    backend.preflight = Mock(return_value={"open_claims": []})
    backend.create_run = Mock()
    retire = Mock()
    monkeypatch.setattr("native_swarm.handover_watch.retire_idle", retire)
    assert (
        advance(executor.root, backend) is False
    )  # supported controller finishes closure itself
    assert executor.try_launch_prepared("w") is False
    assert read(executor.root / "claims/c/giveup-response.json") == {"released": True}
    assert executor.journal["jobs"]["w"]["state"] == "released_before_launch"
    assert executor.active == {"unrelated-running-worker": {"untouched": True}}
    assert "Profile allocation changed" in service.call.call_args.args[1]["reason"]
    assert (
        advance(executor.root, backend) is False
    )  # existing work still owns its lease
    retire.assert_not_called()
    atomic(executor.root / "claims/other/giveup-response.json", {"released": True})
    executor.active.clear()
    executor.journal["jobs"]["unrelated-running-worker"][
        "state"
    ] = "awaiting_campaign_closure"
    executor.journal_save()
    assert advance(executor.root, backend) is True
    retire.assert_called_once_with(executor.root, state)
    backend.create_run.assert_called_once()
    executor.try_launch_prepared("w")
    assert service.call.call_count == 2  # release cannot be repeated


@pytest.mark.parametrize(
    "boundary",
    [
        "manual_pause",
        "STOP_NEW_CLAIMS",
        "live_worker",
        "launch_intent",
        "checked_source",
    ],
)
def test_withdrawal_preserves_operator_and_execution_boundaries(
    tmp_path, monkeypatch, boundary
):
    executor, state, service = setup(tmp_path, monkeypatch)
    if boundary == "manual_pause":
        atomic(
            executor.root / "operator-admissions-paused.json", {"requested_at": "now"}
        )
        assert executor.try_launch_prepared("w") == "base-gate"
    elif boundary == "STOP_NEW_CLAIMS":
        (executor.root / boundary).write_text("protected")
        assert executor.try_launch_prepared("w") is False
    else:
        if boundary == "live_worker":
            executor.active["w"] = {"alive": True}
        elif boundary == "launch_intent":
            executor.journal["jobs"]["w"]["launch_intent_at"] = "started"
        else:
            atomic(
                executor.root / "claims/c/work/recorded-checks/check-1.json",
                {"status": "wip"},
            )
        with pytest.raises(RuntimeError):
            executor.try_launch_prepared("w")
    service.call.assert_not_called()
    assert not (executor.root / "claims/c/giveup-response.json").exists()


def test_withdrawal_is_ordered_against_profile_edits_and_launches(
    tmp_path, monkeypatch
):
    executor, state, service = setup(tmp_path, monkeypatch)

    def call(kind, params):
        for name in ("allocation.lock", "capacity.lock"):
            with (state / name).open("a") as lock:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return (
            {"open_claims": [{"claim_id": "c", "this_key": True}]}
            if kind == "status"
            else {"released": True}
        )

    service.call.side_effect = call
    assert executor.try_launch_prepared("w") is False


def test_legacy_prepared_assignment_has_explicit_recovery_action(tmp_path, monkeypatch):
    executor, state, service = setup(tmp_path, monkeypatch)
    managed = read(executor.root / "native-app-managed.json")
    managed.pop("withdrawn_preparation_closure_version")
    atomic(executor.root / "native-app-managed.json", managed)
    backend = Backend(state)
    backend.create_run = Mock()
    assert advance(executor.root, backend) is True
    marker = read(executor.root / "operator-profile-handover-pending.json")
    assert marker["state"] == "blocked"
    assert "Drain safely" in marker["reason"]
    service.call.assert_not_called()
    backend.create_run.assert_not_called()
    assert executor.journal["jobs"]["w"]["state"] == "prepared_waiting_transient_gate"
