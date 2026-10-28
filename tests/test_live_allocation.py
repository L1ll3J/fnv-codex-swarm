"""Live profile allocation with fake process/allowance observations only."""

import datetime as dt
import time
from unittest.mock import Mock

from native_swarm.allocation import sync
from native_swarm.backend import Backend
from native_swarm.policy import decision
from native_swarm.profiles import default_profile
from native_swarm.state import atomic, read


def fixture(tmp_path, monkeypatch):
    state = tmp_path / "state"
    sol = tmp_path / "sol"
    terra = tmp_path / "terra"
    team = tmp_path / "team"
    profiles = [
        default_profile(2),
        {**default_profile(5), "id": "terra", "model": "gpt-6.1-terra"},
        {**default_profile(1), "id": "team", "group_size": 3},
    ]
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    atomic(
        state / "allowance",
        {
            "checked_at": stamp,
            "latest_counter": {"observed_at": stamp},
            "active_windows": [{"used_percent": 10, "resets_at": time.time() + 120}],
        },
    )
    for i, (root, p) in enumerate(zip((sol, terra, team), profiles), 1):
        atomic(root / "native-app-profile.json", p)
        atomic(root / "native-app-managed.json", {"state_dir": str(state)})
        atomic(root / "native-app-quota.json", {"path": str(state / "allowance")})
        atomic(root / ".rolling-executor-owner.json", {"pid": i})
        atomic(root / "operator-control.json", {"target_pool": p["instances"]})
    atomic(
        state / "config.json",
        {
            "profiles": profiles,
            "workers": 10,
            "worker_capacity": 10,
            "managed_runs": list(map(str, (sol, terra, team))),
        },
    )
    monkeypatch.setattr("native_swarm.policy.process_alive", lambda pid, root: True)
    monkeypatch.setattr("native_swarm.handover_watch.request", Mock())
    return Backend(state), sol, terra, team


def test_removed_team_zero_target_and_existing_solos_expand(tmp_path, monkeypatch):
    b, sol, terra, team = fixture(tmp_path, monkeypatch)
    before = read(team / "native-app-profile.json")
    selected = [{**b.profiles()[0], "instances": 5}, b.profiles()[1]]
    b.save_profiles(selected)
    assert decision(team)["target"] == 0
    assert decision(sol)["target"] == 5 and decision(terra)["target"] == 5
    assert read(sol / "operator-control.json")["target_pool"] == 5
    assert (
        read(team / "operator-control.json")["target_pool"] == 1
    )  # no native invalid-zero STOP
    assert (
        read(team / "operator-admissions-paused.json")["source"] == "profile-allocation"
    )
    assert read(team / "native-app-profile-at-creation.json") == before
    assert read(team / "native-app-profile.json")["group_size"] == 3


def test_reductions_do_not_change_live_assignment_or_kill_workers(
    tmp_path, monkeypatch
):
    b, sol, _, _ = fixture(tmp_path, monkeypatch)
    assignment = {"model": "gpt-6.1-sol", "max_batch_rounds": 20}
    atomic(sol / "claims/active/assignment.json", assignment)
    p = b.profiles()
    p[0]["instances"] = 1
    b.save_profiles(p)
    assert decision(sol)["target"] == 1
    assert read(sol / "claims/active/assignment.json") == assignment


def test_model_or_team_shape_change_disables_old_identity(tmp_path, monkeypatch):
    b, sol, _, _ = fixture(tmp_path, monkeypatch)
    p = b.profiles()
    p[0]["model"] = "gpt-6.1-terra"
    b.save_profiles(p)
    assert decision(sol)["target"] == 0
    assert read(sol / "native-app-profile.json")["model"] == "gpt-6.1-sol"


def test_reenable_clears_only_allocation_pause(tmp_path, monkeypatch):
    b, sol, _, _ = fixture(tmp_path, monkeypatch)
    cfg = b.config
    cfg["profiles"][0]["instances"] = 0
    sync(sol, cfg)
    assert (sol / "operator-admissions-paused.json").exists()
    cfg["profiles"][0]["instances"] = 2
    sync(sol, cfg)
    assert not (sol / "operator-admissions-paused.json").exists()
    atomic(sol / "operator-admissions-paused.json", {"reason": "manual pause"})
    sync(sol, cfg)
    assert read(sol / "operator-admissions-paused.json")["reason"] == "manual pause"


def test_start_resume_does_not_reenable_removed_controller(tmp_path, monkeypatch):
    b, sol, _, team = fixture(tmp_path, monkeypatch)
    selected = b.profiles()[:2]
    b.save_profiles(selected)
    monkeypatch.setattr("native_swarm.backend.process_alive", lambda pid, root: True)
    monkeypatch.setattr(b, "quota", Mock())
    monkeypatch.setattr(b, "preflight", Mock())
    atomic(sol / "operator-admissions-paused.json", {"reason": "manual pause"})
    b.start()
    assert not (sol / "operator-admissions-paused.json").exists()
    assert (team / "operator-admissions-paused.json").exists()


def test_new_controller_identity_snapshot_is_not_rewritten(tmp_path, monkeypatch):
    b, sol, _, _ = fixture(tmp_path, monkeypatch)
    marker = read(sol / "native-app-managed.json")
    marker["allocation_version"] = 1
    atomic(sol / "native-app-managed.json", marker)
    before = read(sol / "native-app-profile.json")
    p = b.profiles()[:2]
    p[0]["instances"] = 3
    b.save_profiles(p)
    assert read(sol / "native-app-profile.json") == before
    assert decision(sol)["target"] == 3


def test_claim_request_rechecks_saved_allocation_before_external_dispatch(
    tmp_path, monkeypatch
):
    from native_swarm.allocation import claim_request, WITHDRAWN
    import pytest

    b, sol, _, _ = fixture(tmp_path, monkeypatch)
    (sol / "claims").mkdir()
    atomic(sol / "run-config.json", {"max_workers": 2, "authorized_worker_ceiling": 75})
    dispatch = Mock(return_value={"claim_id": "new"})
    assert claim_request(sol, dispatch) == {"claim_id": "new"}
    selected = b.profiles()[1:]
    b.save_profiles(selected)
    with pytest.raises(RuntimeError, match=WITHDRAWN):
        claim_request(sol, dispatch)
    assert dispatch.call_count == 1


def test_claim_dispatch_and_profile_save_are_ordered(tmp_path, monkeypatch):
    import threading
    from native_swarm.allocation import claim_request, WITHDRAWN

    b, sol, _, _ = fixture(tmp_path, monkeypatch)
    (sol / "claims").mkdir()
    atomic(sol / "run-config.json", {"authorized_worker_ceiling": 75})
    entered = threading.Event()
    complete = threading.Event()
    saved = threading.Event()
    errors = []

    def dispatch():
        entered.set()
        assert complete.wait(3)
        return {"claim_id": "before-save"}

    def request():
        try:
            claim_request(sol, dispatch)
        except Exception as e:
            errors.append(e)

    def save():
        try:
            b.save_profiles(b.profiles()[1:])
            saved.set()
        except Exception as e:
            errors.append(e)

    first = threading.Thread(target=request)
    first.start()
    assert entered.wait(3)
    second = threading.Thread(target=save)
    second.start()
    assert not saved.wait(0.05)
    complete.set()
    first.join(3)
    second.join(3)
    assert saved.is_set() and not errors
    import pytest

    with pytest.raises(RuntimeError, match=WITHDRAWN):
        claim_request(sol, Mock(side_effect=AssertionError("No new dispatch")))


def test_legacy_acquire_ceiling_tracks_count_without_changing_model(
    tmp_path, monkeypatch
):
    b, sol, _, _ = fixture(tmp_path, monkeypatch)
    original = {
        "max_workers": 2,
        "model": "gpt-6.1-sol",
        "authorized_worker_ceiling": 150,
    }
    atomic(sol / "run-config.json", original)
    b.save_profiles([{**b.profiles()[0], "instances": 5}, b.profiles()[1]])
    assert read(sol / "run-config.json") == {**original, "max_workers": 5}
    assert read(sol / "run-config-at-creation.json") == original


def resume_fixture(tmp_path, monkeypatch):
    b, sol, terra, team = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "native_swarm.backend.process_alive", lambda pid, root: bool(pid)
    )
    monkeypatch.setattr(b, "quota", Mock())
    monkeypatch.setattr(b, "preflight", Mock(return_value={"open_claims": []}))
    monkeypatch.setattr(b, "verify_template", Mock())
    cfg = b.config
    cfg["template_run"] = str(tmp_path / "template")
    atomic(b.state / "config.json", cfg)
    return b, sol, terra, team


def test_resume_adds_only_missing_allocated_profile_without_restarting_existing(
    tmp_path, monkeypatch
):
    b, sol, terra, team = resume_fixture(tmp_path, monkeypatch)
    fresh = {**default_profile(1), "id": "new-team", "group_size": 2}
    b.save_profiles([{**b.profiles()[0], "instances": 8}, fresh])
    original = read(sol / "native-app-profile.json")
    created = []

    def create(cfg, template, account, profile):
        root = tmp_path / profile["id"]
        created.append(profile)
        atomic(root / "native-app-profile.json", profile)
        atomic(root / ".rolling-executor-owner.json", {"pid": 12})
        cfg["managed_runs"].append(str(root))
        cfg["current_run"] = str(root)
        atomic(b.state / "config.json", cfg)

    monkeypatch.setattr(b, "create_run", create)
    assert "started 1 new profile" in b.start()
    assert created == [fresh]
    assert read(sol / "native-app-profile.json")["model"] == original["model"]
    assert read(sol / "operator-control.json")["target_pool"] == 8
    assert (terra / "operator-admissions-paused.json").exists()
    assert (team / "operator-admissions-paused.json").exists()
    b.preflight.assert_called_once_with(sol, require_idle=False)
    assert "started" not in b.start()
    assert created == [fresh]  # repeated Resume does not create a duplicate


def test_resume_queues_changed_identity_until_original_work_finishes(
    tmp_path, monkeypatch
):
    b, sol, _, _ = resume_fixture(tmp_path, monkeypatch)
    selected = b.profiles()
    selected[0]["model"] = "gpt-6.1-terra"
    b.save_profiles(selected)
    create = Mock()
    monkeypatch.setattr(b, "create_run", create)
    assert "handover(s)" in b.start()
    assert read(sol / "native-app-profile.json")["model"] == "gpt-6.1-sol"
    create.assert_not_called()


def test_resume_keeps_protected_stop_and_reports_actual_reason(tmp_path, monkeypatch):
    import pytest
    from native_swarm.backend import ControlError

    b, sol, _, _ = resume_fixture(tmp_path, monkeypatch)
    (sol / "STOP_NEW_CLAIMS").write_text("Submission integrity audit failed")
    create = Mock()
    monkeypatch.setattr(b, "create_run", create)
    with pytest.raises(ControlError, match="Submission integrity audit failed"):
        b.start()
    assert (sol / "STOP_NEW_CLAIMS").read_text() == "Submission integrity audit failed"
    create.assert_not_called()


def test_resume_keeps_operator_drain_and_does_not_start_new_profiles(
    tmp_path, monkeypatch
):
    import pytest
    from native_swarm.backend import ControlError

    b, sol, _, _ = resume_fixture(tmp_path, monkeypatch)
    atomic(sol / "operator-drain.json", {"reason": "Finish the current run"})
    create = Mock()
    monkeypatch.setattr(b, "create_run", create)
    with pytest.raises(ControlError, match="Finish the current run"):
        b.start()
    assert read(sol / "operator-drain.json")["reason"] == "Finish the current run"
    create.assert_not_called()


def test_resume_checks_capacity_before_creating_missing_profile(tmp_path, monkeypatch):
    import pytest

    b, _, _, _ = resume_fixture(tmp_path, monkeypatch)
    cfg = b.config
    cfg["profiles"].append({**default_profile(1), "id": "over-capacity"})
    atomic(b.state / "config.json", cfg)
    create = Mock()
    monkeypatch.setattr(b, "create_run", create)
    with pytest.raises(ValueError, match="capacity"):
        b.start()
    create.assert_not_called()


def test_resume_does_not_start_missing_profile_with_orphaned_workers(
    tmp_path, monkeypatch
):
    import pytest
    from native_swarm.backend import ControlError

    b, _, _, team = resume_fixture(tmp_path, monkeypatch)
    (team / ".rolling-executor-owner.json").unlink()
    atomic(team / "rolling-executor/journal.json", {"jobs": {"old": {"pid": 42}}})
    create = Mock()
    monkeypatch.setattr(b, "create_run", create)
    with pytest.raises(ControlError, match="Workers without an active controller"):
        b.start()
    create.assert_not_called()


def test_resume_missing_profile_preflight_failure_preserves_existing_pause(
    tmp_path, monkeypatch
):
    import pytest
    from native_swarm.backend import ControlError

    b, sol, _, team = resume_fixture(tmp_path, monkeypatch)
    (team / ".rolling-executor-owner.json").unlink()
    atomic(sol / "operator-admissions-paused.json", {"reason": "manual pause"})
    b.preflight.side_effect = ControlError("Readiness unavailable")
    create = Mock()
    monkeypatch.setattr(b, "create_run", create)
    with pytest.raises(ControlError, match="Readiness unavailable"):
        b.start()
    assert read(sol / "operator-admissions-paused.json")["reason"] == "manual pause"
    create.assert_not_called()


def test_resume_rejects_duplicate_live_profile_owners(tmp_path, monkeypatch):
    import pytest
    from native_swarm.backend import ControlError

    b, sol, _, team = resume_fixture(tmp_path, monkeypatch)
    atomic(team / "native-app-profile.json", read(sol / "native-app-profile.json"))
    create = Mock()
    monkeypatch.setattr(b, "create_run", create)
    with pytest.raises(ControlError, match="Multiple active controllers own profile"):
        b.start()
    create.assert_not_called()


def test_resume_failed_multi_profile_start_pauses_only_new_controllers(
    tmp_path, monkeypatch
):
    import pytest
    from native_swarm.backend import ControlError

    b, sol, terra, team = resume_fixture(tmp_path, monkeypatch)
    for root in (terra, team):
        (root / ".rolling-executor-owner.json").unlink()
    atomic(sol / "operator-admissions-paused.json", {"reason": "manual pause"})
    new_run = tmp_path / "new-terra"

    def create(cfg, template, account, profile):
        if profile["id"] == "team":
            raise ControlError("Preparation unavailable")
        cfg["managed_runs"].append(str(new_run))
        cfg["current_run"] = str(new_run)
        atomic(b.state / "config.json", cfg)

    monkeypatch.setattr(b, "create_run", create)
    with pytest.raises(ControlError, match="Preparation unavailable"):
        b.start()
    assert read(sol / "operator-admissions-paused.json")["reason"] == "manual pause"
    assert (
        read(new_run / "operator-admissions-paused.json")["reason"]
        == "profile start incomplete"
    )


def test_resume_retires_idle_changed_profile_and_starts_replacement(
    tmp_path, monkeypatch
):
    b, sol, terra, team = resume_fixture(tmp_path, monkeypatch)
    selected = b.profiles()
    selected[0]["model"] = "gpt-6.1-terra"
    b.save_profiles(selected)
    atomic(sol / "rolling-executor/journal.json", {"jobs": {}})
    live = {1, 2, 3}
    monkeypatch.setattr(
        "native_swarm.backend.process_alive", lambda pid, root: pid in live
    )
    monkeypatch.setattr(
        "native_swarm.handover.process_alive", lambda pid, root: pid in live
    )
    stopped = []

    def terminate(pid, root, owner):
        live.remove(pid)
        stopped.append(root)

    monkeypatch.setattr("native_swarm.handover.terminate_controller", terminate)
    create = Mock()
    monkeypatch.setattr(b, "create_run", create)
    assert "started 1 new profile" in b.start()
    assert stopped == [sol]
    assert live == {2, 3}
    assert create.call_args.args[3]["id"] == "default"
    assert create.call_args.args[3]["model"] == "gpt-6.1-terra"
    assert read(sol / "operator-profile-handover.json")["state"] == "retired"
    b.preflight.assert_called_once_with(terra, require_idle=False)


def test_editing_live_identity_requests_automatic_handover(tmp_path, monkeypatch):
    b, sol, terra, team = resume_fixture(tmp_path, monkeypatch)
    from native_swarm.handover_watch import request

    selected = b.profiles()
    selected[0]["model"] = "gpt-6.1-terra"
    b.save_profiles(selected)
    request.assert_called_once_with(sol, b.state)
    assert (
        read(sol / "operator-admissions-paused.json")["source"] == "profile-allocation"
    )


def test_edit_does_not_schedule_handover_under_manual_pause(tmp_path, monkeypatch):
    b, sol, _, _ = resume_fixture(tmp_path, monkeypatch)
    from native_swarm.handover_watch import request

    atomic(sol / "operator-admissions-paused.json", {"reason": "operator paused"})
    selected = b.profiles()
    selected[0]["group_size"] = 2
    selected[0]["instances"] = 1
    b.save_profiles(selected)
    request.assert_not_called()
