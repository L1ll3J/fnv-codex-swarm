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
