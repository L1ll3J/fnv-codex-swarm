from unittest.mock import Mock

import pytest

from native_swarm.backend import Backend, ControlError
from native_swarm.profiles import default_profile, validate_profiles
from native_swarm.state import atomic, read


def test_zero_slot_profile_can_be_created_when_default_uses_all_slots(tmp_path):
    backend = Backend(tmp_path)
    atomic(tmp_path / "config.json", {"workers": 10})
    original = backend.profiles()
    profiles = original + [{**default_profile(0), "id": "new", "name": "New"}]
    backend.save_profiles(profiles, expected=original)
    assert len(backend.profiles()) == 2
    assert validate_profiles(backend.profiles()) == 10


def test_increasing_capacity_allows_larger_profile_split_and_prevents_oversubscription(
    tmp_path,
):
    backend = Backend(tmp_path)
    atomic(tmp_path / "config.json", {"workers": 10})
    backend.settings(10, ["<=255"], capacity=30)
    profiles = [
        default_profile(12),
        {**default_profile(6), "id": "team", "group_size": 3},
    ]
    backend.save_profiles(profiles)
    assert backend.config["worker_capacity"] == 30
    assert backend.config["workers"] == 30
    with pytest.raises(ValueError, match="configured maximum is 30"):
        backend.save_profiles([default_profile(31)])
    with pytest.raises(ValueError, match="configured maximum is 20"):
        backend.settings(30, ["<=255"], capacity=20)
    assert backend.config["worker_capacity"] == 30
    assert backend.config["profiles"] == profiles


def test_total_capacity_can_exceed_native_per_profile_ceiling():
    profiles = [default_profile(60), {**default_profile(60), "id": "second"}]
    assert validate_profiles(profiles, 120) == 120
    with pytest.raises(ValueError, match="native controller limit"):
        validate_profiles([default_profile(76)], 120)


def test_all_unallocated_start_is_rejected_before_any_external_call(
    tmp_path, monkeypatch
):
    backend = Backend(tmp_path)
    atomic(tmp_path / "config.json", {"workers": 0, "profiles": [default_profile(0)]})
    quota = Mock(side_effect=AssertionError("No quota RPC"))
    monkeypatch.setattr(backend, "quota", quota)
    with pytest.raises(ControlError, match="No slots allocated"):
        backend.start()
    quota.assert_not_called()
    backend.settings(0, ["<=255"], capacity=20)
    assert backend.config["workers"] == 0


def test_start_skips_zero_slot_profiles(tmp_path, monkeypatch):
    backend = Backend(tmp_path)
    atomic(
        tmp_path / "config.json",
        {
            "workers": 3,
            "profiles": [default_profile(0), {**default_profile(3), "id": "active"}],
            "template_run": str(tmp_path / "template"),
        },
    )
    monkeypatch.setattr(backend, "quota", lambda cfg: None)
    monkeypatch.setattr(backend, "verify_template", lambda *args: None)
    monkeypatch.setattr(
        backend, "preflight", lambda *args, **kwargs: {"open_claims": []}
    )
    created = []
    monkeypatch.setattr(
        backend,
        "create_run",
        lambda cfg, template, account, profile: created.append(profile),
    )
    backend.start()
    assert [p["id"] for p in created] == ["active"]


def test_capacity_changes_do_not_resize_running_controllers(tmp_path):
    backend = Backend(tmp_path / "state")
    run = tmp_path / "run"
    atomic(
        backend.state / "config.json",
        {"workers": 10, "profiles": [default_profile(10)], "managed_runs": [str(run)]},
    )
    atomic(run / "native-app-managed.json", {})
    atomic(run / "native-app-profile.json", default_profile(10))
    atomic(run / "operator-control.json", {"target_pool": 10})
    backend.settings(10, ["<=255"], capacity=40)
    assert read(run / "operator-control.json")["target_pool"] == 10
