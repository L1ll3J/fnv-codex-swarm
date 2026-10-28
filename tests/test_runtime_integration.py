"""Validate generated runtime code against the actual pinned local template.

These tests compile and inspect Python only. They never execute the native
controller, account RPC, compiler or FNV client.
"""

from pathlib import Path
import shutil

import pytest

from native_swarm.profiles import default_profile
from native_swarm.runtime import configure
from native_swarm.state import read

TEMPLATE = Path(str(Path.home() / "fnvdecomp/work/swarm-native-random-v2-20261026"))


@pytest.mark.skipif(
    not TEMPLATE.exists(), reason="Local native template is not installed"
)
@pytest.mark.parametrize("group_size", [1, 3])
def test_real_template_generation_preserves_original_checker(tmp_path, group_size):
    names = [
        "run-config.json",
        "prepare_assignment.py",
        "acquire.py",
        "rolling_executor.py",
        "resolve_ready.py",
        "campaign_watch.py",
        "native_fresh_restart.py",
        "recorded_batch.py",
        "ingest.py",
        "recorded_fnv_check_v2.py",
        "validate_submit.py",
        "evidence.py",
        "attempt_history.py",
        "recorded_check_with_history.py",
    ]
    for name in names:
        shutil.copy2(TEMPLATE / name, tmp_path / name)
    pinned = {n: (TEMPLATE / n).read_bytes() for n in names}
    profile = {
        **default_profile(2),
        "model": "gpt-6.1-terra",
        "effort": "high",
        "group_size": group_size,
    }
    configure(tmp_path, profile)
    helper = (tmp_path / "prepare_assignment.py").read_text()
    assert "acquire_args=[sys.executable," in helper
    assert "['python3'" not in helper
    assert "f'python3 " not in helper
    assert "f'{sys.executable} " in helper
    for path in tmp_path.glob("*.py"):
        compile(path.read_text(), str(path), "exec")
    assert read(tmp_path / "run-config.json")["model"] == "gpt-6.1-terra"
    assert all((TEMPLATE / n).read_bytes() == text for n, text in pinned.items())
    assert (
        "tool_context=context(assignment)"
        in (tmp_path / "recorded_fnv_check_v2.py").read_text()
    )
    assert "accepted_match" in (tmp_path / "validate_submit.py").read_text()
    assert (
        "if not resolution_ready(p):return False"
        in (tmp_path / "ingest.py").read_text()
    )
    assert "native-app-quota.json" in (tmp_path / "campaign_watch.py").read_text()
    assert ("audit_group" in (tmp_path / "resolve_ready.py").read_text()) == (
        group_size > 1
    )


@pytest.mark.skipif(
    not TEMPLATE.exists(), reason="Local native template is not installed"
)
@pytest.mark.parametrize(
    "second_cost, expected_total, known, lower",
    [(2, 3, 3, False), (None, None, 1, True)],
)
def test_persisted_native_accounting_counts_every_group_thread(
    tmp_path, monkeypatch, second_cost, expected_total, known, lower
):
    import runpy
    import sys
    import types
    from native_swarm.state import atomic

    names = [
        "run-config.json",
        "prepare_assignment.py",
        "rolling_executor.py",
        "resolve_ready.py",
        "accounting.py",
    ]
    for name in names:
        shutil.copy2(TEMPLATE / name, tmp_path / name)
    configure(tmp_path, {**default_profile(1), "group_size": 2})
    claim = tmp_path / "claims/claim"
    atomic(claim / "claim.json", {"size": 900})
    atomic(claim / "assignment.json", {"model": "gpt-6.1-sol"})
    atomic(
        claim / "worker-assignment.json",
        {
            "thread_id": "a",
            "thread_source": "codex_exec_direct",
            "usage_baseline": {"total_tokens": 0},
            "cost_baseline_usd": 0,
        },
    )
    members = [
        {
            "workspace": str(claim / "collaboration" / identifier),
            "thread_id": identifier,
        }
        for identifier in ("a", "b")
    ]
    atomic(claim / "collaboration/group.json", {"members": members})
    atomic(
        tmp_path / "usage-current.json",
        {
            "threads": [
                {
                    "id": "a",
                    "model": "gpt-6.1-sol",
                    "usage": {"total_tokens": 100},
                    "cost_usd": 1,
                },
                {
                    "id": "b",
                    "model": "gpt-6.1-sol",
                    "usage": {"total_tokens": 200},
                    "cost_usd": second_cost,
                },
            ],
            "known_cost_usd": known,
            "known_token_totals": {"total_tokens": 300},
        },
    )
    (tmp_path / "submission-ledger.jsonl").touch()
    service = types.ModuleType("service")
    service.ROOT = tmp_path
    service.CONFIG = {}
    service.save = lambda path, value: atomic(tmp_path / path, value)
    monkeypatch.setitem(sys.modules, "service", service)
    runpy.run_path(str(tmp_path / "accounting.py"))
    result = read(claim / "cost.json")
    assert result["worker_tokens"]["total_tokens"] == 300
    assert result["worker_cost_usd"] == expected_total
    assert result["collaboration"]["thread_ids"] == ["a", "b"]
    bucket = read(tmp_path / "cost-accounting.json")["by_model_actual_size"][
        "gpt-6.1-sol:512-1K"
    ]
    assert bucket["known_worker_cost_usd"] == known
    assert bucket["priced_cost_is_lower_bound"] == lower


@pytest.mark.skipif(
    not TEMPLATE.exists(), reason="Local native template is not installed"
)
def test_generated_fresh_parent_does_not_override_operator_cutoff(tmp_path):
    import ast
    from types import SimpleNamespace
    from unittest.mock import Mock

    for name in (
        "run-config.json",
        "prepare_assignment.py",
        "rolling_executor.py",
        "resolve_ready.py",
        "native_fresh_restart.py",
    ):
        shutil.copy2(TEMPLATE / name, tmp_path / name)
    configure(tmp_path, default_profile(1))
    parsed = ast.parse((tmp_path / "native_fresh_restart.py").read_text())
    cls = next(node for node in parsed.body if isinstance(node, ast.ClassDef))
    parent = type("Parent", (), {"maybe_acquire": lambda self, target: target})
    scope = {"base": SimpleNamespace(RollingExecutor=parent)}
    exec(
        compile(
            ast.Module(body=[cls], type_ignores=[]), "<generated-native-parent>", "exec"
        ),
        scope,
    )
    executor = scope["FreshExecutor"]()
    executor.stop = Mock(side_effect=AssertionError("Hidden fixed cutoff"))
    assert executor.maybe_acquire(4) == 4


@pytest.mark.skipif(
    not TEMPLATE.exists(), reason="Local native template is not installed"
)
@pytest.mark.parametrize("cutoff,admitted", [(80, True), (50, False)])
def test_actual_managed_inheritance_honors_cutoff_above_fifty(
    tmp_path, monkeypatch, cutoff, admitted
):
    import importlib
    import importlib.util
    import sys
    import datetime as dt
    import time
    from unittest.mock import Mock
    from native_swarm.policy import gate
    from native_swarm.state import atomic

    for name in (
        "run-config.json",
        "prepare_assignment.py",
        "rolling_executor.py",
        "resolve_ready.py",
        "native_fresh_restart.py",
        "random_executor.py",
        "capacity_executor.py",
        "selected_executor.py",
    ):
        shutil.copy2(TEMPLATE / name, tmp_path / name)
    profile = default_profile(2)
    configure(tmp_path, profile)
    state = tmp_path / "state"
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    atomic(state / "config.json", {"usage_policy": {"cutoff_used_percent": cutoff}})
    atomic(
        state / "quota.json",
        {
            "checked_at": stamp,
            "latest_counter": {"observed_at": stamp},
            "active_windows": [{"used_percent": 60, "resets_at": time.time() + 120}],
            "reserve_reached": True,
        },
    )
    atomic(tmp_path / "native-app-managed.json", {"state_dir": str(state)})
    atomic(tmp_path / "native-app-quota.json", {"path": str(state / "quota.json")})
    atomic(tmp_path / "native-app-profile.json", profile)
    monkeypatch.syspath_prepend(str(tmp_path))
    modules = [
        "rolling_executor",
        "native_fresh_restart",
        "selected_executor",
        "capacity_executor",
        "random_executor",
    ]
    for name in modules:
        monkeypatch.delitem(sys.modules, name, raising=False)
    try:
        base = importlib.import_module("rolling_executor")
        monkeypatch.setattr(base, "RESERVE", state / "quota.json")
        monkeypatch.setattr(base, "quota_state", lambda now=None: gate(tmp_path, now))
        monkeypatch.setattr(base, "lock_held", lambda path: True)
        path = Path(__file__).parents[1] / "src/native_swarm/controller.py"
        spec = importlib.util.spec_from_file_location("real_policy_controller", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        executor = module.ManagedExecutor(tmp_path)
        executor.acquire_one = Mock()
        executor.available_mix = lambda mix, target: mix
        assert executor.maybe_acquire(2) is admitted
        assert executor.acquire_one.call_count == int(admitted)
        assert not (tmp_path / "STOP_NEW_CLAIMS").exists()
    finally:
        for name in modules:
            sys.modules.pop(name, None)


@pytest.mark.skipif(
    not TEMPLATE.exists(), reason="Local native template is not installed"
)
def test_intermediate_results_are_held_before_ingestion_and_resolver_reads(
    tmp_path, monkeypatch
):
    import ast
    import json
    import sys
    import types
    from native_swarm.state import atomic

    for name in (
        "run-config.json",
        "rolling_executor.py",
        "prepare_assignment.py",
        "resolve_ready.py",
        "ingest.py",
    ):
        shutil.copy2(TEMPLATE / name, tmp_path / name)
    configure(tmp_path, default_profile(1))
    atomic(tmp_path / "native-app-profile.json", default_profile(1))
    claim = tmp_path / "claims/claim"
    atomic(claim / "session/state.json", {"state": "running", "streams_closed": True})
    atomic(claim / "result.json", {"match": False})
    atomic(claim / "work/result.json", {"match": False})
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        if path in (claim / "result.json", claim / "work/result.json"):
            raise AssertionError("Nonterminal result must not be inspected/ingested")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    parsed = ast.parse((tmp_path / "ingest.py").read_text())
    function = next(
        n
        for n in parsed.body
        if isinstance(n, ast.FunctionDef) and n.name == "_ingest_claim"
    )
    scope = {}
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), "<ingest>", "exec"), scope
    )
    assert scope["_ingest_claim"](claim, {}) is False
    parsed = ast.parse((tmp_path / "resolve_ready.py").read_text())
    function = next(
        n for n in parsed.body if isinstance(n, ast.FunctionDef) and n.name == "once"
    )
    monkeypatch.setitem(
        sys.modules, "ingest", types.SimpleNamespace(ingest=lambda: None)
    )
    scope = {
        "ROOT": tmp_path,
        "json": json,
        "call": lambda *args: {"strikes_last_7_days": 0, "submissions_paused": False},
        "save": lambda *args: None,
        "due_expired_claims": lambda *args: [],
        "CONFIG": {},
    }
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), "<resolver>", "exec"),
        scope,
    )
    assert scope["once"]() == []


@pytest.mark.skipif(
    not TEMPLATE.exists(), reason="Local native template is not installed"
)
def test_new_recorder_never_reports_a_repaired_rejection_as_free(tmp_path):
    import ast
    from native_swarm.tool_runtime import configure as configure_tools

    for name in (
        "prepare_assignment.py",
        "recorded_fnv_check_v2.py",
        "recorded_batch.py",
        "recorded_check_with_history.py",
        "attempt_history.py",
        "evidence.py",
        "validate_submit.py",
    ):
        shutil.copy2(TEMPLATE / name, tmp_path / name)
    configure_tools(tmp_path)
    validator = (tmp_path / "validate_submit.py").read_text()
    assert "pathlib.Path(tool_context['toolchains']['sp1']['root'])" in validator
    assert "compiler_files(tool_context.get('toolchains',{}))" in validator
    fn = next(
        n
        for n in ast.parse((tmp_path / "recorded_fnv_check_v2.py").read_text()).body
        if isinstance(n, ast.FunctionDef) and n.name == "compiler_bounds"
    )
    scope = {"MAX_COMPILERS_PER_CHECK": 20}
    exec(
        compile(ast.Module(body=[fn], type_ignores=[]), "<generated bounds>", "exec"),
        scope,
    )
    assert scope["compiler_bounds"](
        {"status": "rejected", "errors": ["AUTO-REPAIR: removed duplicate function"]}
    ) == (0, 20)
