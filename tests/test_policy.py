import datetime as dt
import time

import pytest

from native_swarm.policy import DEFAULT, evaluate, targets, validate, decision
from native_swarm.profiles import default_profile, validate_profiles
from native_swarm.state import atomic, read
from native_swarm.backend import Backend
from native_swarm.operator_store import OperatorStore


def observation(used=45, age=0):
    now = time.time()
    stamp = dt.datetime.fromtimestamp(now - age, dt.timezone.utc).isoformat()
    return {
        "checked_at": stamp,
        "latest_counter": {"observed_at": stamp},
        "active_windows": [{"used_percent": used, "resets_at": now + 100}],
    }


@pytest.mark.parametrize(
    "policy,used,fraction",
    [
        ({}, 49, 1),
        ({}, 50, 0),
        ({"cutoff_used_percent": 80}, 70, 1),
        (
            {
                "mode": "taper",
                "cutoff_used_percent": 80,
                "taper_start_used_percent": 40,
            },
            60,
            0.5,
        ),
        ({"mode": "taper"}, 50, 0),
        ({"mode": "stop_immediately"}, 50, 0),
    ],
)
def test_usage_modes_and_configurable_cutoff(policy, used, fraction):
    assert evaluate(observation(used), policy)["fraction"] == fraction


@pytest.mark.parametrize(
    "policy",
    [
        {"cutoff_used_percent": 101},
        {"cutoff_used_percent": float("nan")},
        {"mode": "taper", "taper_start_used_percent": 50},
        {"mode": "unknown"},
        {"cutoff_used_percent": True},
        {"taper_start_used_percent": -1},
    ],
)
def test_invalid_usage_settings_rejected(policy):
    with pytest.raises(ValueError):
        validate(policy)


def test_stale_quota_blocks_instead_of_stopping_workers():
    with pytest.raises(ValueError):
        evaluate(observation(age=91), {"mode": "stop_immediately"})


def test_taper_total_is_shared_across_profiles():
    profiles = [{"id": "a", "instances": 3}, {"id": "b", "instances": 3}]
    assert sum(targets(profiles, 0.5).values()) == 3
    assert targets(profiles, 0) == {"a": 0, "b": 0}
    assert targets(profiles, 1) == {"a": 3, "b": 3}


def test_profile_bands_migrate_and_apply_without_changing_default(tmp_path):
    backend = Backend(tmp_path)
    atomic(tmp_path / "config.json", {"workers": 2, "bands": ["<=255"]})
    original = backend.profiles()
    assert original[0]["bands"] == ["<=255"]
    selected = [{**original[0], "bands": ["256-511", "1K-4K"]}]
    backend.save_profiles(selected, expected=original)
    assert backend.profiles()[0]["bands"] == ["256-511", "1K-4K"]
    assert backend.config["bands"] == ["<=255"]
    with pytest.raises(ValueError):
        validate_profiles([{**default_profile(1), "bands": []}])


def test_queue_skips_incompatible_size_without_reserving_it(tmp_path):
    atomic(
        tmp_path / "queue.json",
        [
            {
                "id": "big",
                "profile_id": None,
                "state": "queued",
                "target": {"bytes": 900, "address": "0x900"},
            },
            {
                "id": "small",
                "profile_id": None,
                "state": "queued",
                "target": {"bytes": 50, "address": "0x50"},
            },
        ],
    )
    store = OperatorStore(tmp_path)
    assert (
        store.reserve("a", "w", eligible=lambda t: t["bytes"] <= 255)["id"] == "small"
    )
    assert store.queue()[0]["state"] == "queued"


def test_policy_uses_current_settings_and_ignores_legacy_fixed_breach(tmp_path):
    state, run = tmp_path / "state", tmp_path / "run"
    atomic(
        state / "config.json", {"usage_policy": {**DEFAULT, "cutoff_used_percent": 80}}
    )
    atomic(run / "native-app-managed.json", {"state_dir": str(state)})
    atomic(run / "native-app-profile.json", default_profile(2))
    atomic(run / "native-app-quota.json", {"path": str(state / "quota")})
    atomic(state / "quota", {**observation(60), "reserve_reached": True})
    assert decision(run)["target"] == 2


def test_blocker_assessments_are_local(tmp_path):
    backend = Backend(tmp_path)
    for value in ("observation", "suspected", "established", "resolved"):
        backend.review("report", value)
        assert read(tmp_path / "reviews.json")["report"]["status"] == value


def executor(tmp_path, monkeypatch):
    import importlib.util
    import sys
    import types
    from pathlib import Path
    from unittest.mock import Mock

    parent = types.ModuleType("random_executor")
    parent.RandomExecutor = type(
        "Parent",
        (),
        {
            "resume_prepared": lambda self: None,
            "maybe_acquire": lambda self, target: (_ for _ in ()).throw(
                AssertionError("No acquisition")
            ),
        },
    )
    base = types.ModuleType("rolling_executor")
    base.read_json, base.atomic_json = read, atomic
    monkeypatch.setitem(sys.modules, "random_executor", parent)
    monkeypatch.setitem(sys.modules, "rolling_executor", base)
    source = Path(__file__).parents[1] / "src/native_swarm/controller.py"
    spec = importlib.util.spec_from_file_location("policy_controller", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value = module.ManagedExecutor()
    value.root, value.clock, value.journal_save = tmp_path, time.time, Mock()
    value.journal = {"jobs": {"w": {}}}
    value.active = {"w": {"proc": Mock()}}
    return value


def test_immediate_policy_signals_full_pool_once_without_releasing(
    tmp_path, monkeypatch
):
    value = executor(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "native_swarm.policy.decision",
        lambda *a: {"reached": True, "mode": "stop_immediately", "target": 0},
    )
    value.resume_prepared()
    value.resume_prepared()
    value.active["w"]["proc"].terminate.assert_called_once()
    assert read(tmp_path / "operator-usage-paused.json")["reached"]
    assert not list(tmp_path.glob("claims/*/giveup-response.json"))


@pytest.mark.parametrize(
    "mode,reached,target", [("stop_queuing", True, 0), ("taper", False, 0)]
)
def test_usage_modes_block_refill_without_stopping_active_work(
    tmp_path, monkeypatch, mode, reached, target
):
    value = executor(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "native_swarm.policy.decision",
        lambda *a: {"reached": reached, "mode": mode, "target": target},
    )
    value.resume_prepared()
    assert value.maybe_acquire(10) is False
    value.active["w"]["proc"].terminate.assert_not_called()


def test_non_taper_cutoff_does_not_require_editing_an_unused_taper_start():
    assert validate({"cutoff_used_percent": 20})["cutoff_used_percent"] == 20
