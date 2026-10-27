import importlib.util
from pathlib import Path
import sys
import types
from native_swarm.state import read, atomic


def test_operator_pause_and_drain_block_prepared_and_new_workers(tmp_path, monkeypatch):
    class Parent:
        def prepared_gate(self):
            return "ready", "okay", {}

        def maybe_acquire(self, target):
            raise AssertionError("No admissions or capacity retries allowed")

        def stop(self, reason):
            self.reason = reason

    parent = types.ModuleType("random_executor")
    parent.RandomExecutor = Parent
    base = types.ModuleType("rolling_executor")
    base.read_json = read
    monkeypatch.setitem(sys.modules, "random_executor", parent)
    monkeypatch.setitem(sys.modules, "rolling_executor", base)
    source = Path(__file__).parents[1] / "src/native_swarm/controller.py"
    spec = importlib.util.spec_from_file_location("isolated_controller", source)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    x = m.ManagedExecutor()
    x.root = tmp_path
    x.active = {"in-flight": {}}
    atomic(tmp_path / "operator-admissions-paused.json", {})
    assert x.maybe_acquire(10) is False and x.prepared_gate()[0] == "wait"
    atomic(tmp_path / "operator-drain.json", {})
    assert x.maybe_acquire(10) is False and x.prepared_gate()[0] == "stop"


def test_priority_address_race_does_not_become_an_uncertain_retry(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    parent = types.ModuleType("random_executor")
    parent.RandomExecutor = type("Parent", (), {})
    base = types.ModuleType("rolling_executor")
    base.read_json = read
    base.MODEL = "gpt-6.1-sol"
    base.band = lambda _: "<=255"
    base.live_claims = lambda _: {}
    service = types.ModuleType("service")
    calls = []

    def call(kind, args):
        calls.append(kind)
        return (
            {
                "functions": [
                    {"address": "0x1234", "size": 101},
                    {"address": "0x5678", "size": 100},
                ]
            }
            if kind == "list_functions"
            else {"address": "0x1234"}
        )

    service.call = call
    monkeypatch.setitem(sys.modules, "random_executor", parent)
    monkeypatch.setitem(sys.modules, "rolling_executor", base)
    monkeypatch.setitem(sys.modules, "service", service)
    source = Path(__file__).parents[1] / "src/native_swarm/controller.py"
    spec = importlib.util.spec_from_file_location("priority_controller", source)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    atomic(
        tmp_path / "native-app-profile.json",
        {"id": "p", "queue_first": False, "priority": "largest"},
    )
    atomic(tmp_path / "native-app-managed.json", {"state_dir": str(tmp_path / "state")})
    atomic(tmp_path / "operator-control.json", {"native_app_bands": ["<=255"]})

    commands = []

    def refused(*args, **kwargs):
        commands.append(args[0])
        kwargs["stdout"].write(b"CONTINUATION_ADDRESS_UNAVAILABLE\n")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(m.subprocess, "run", refused)
    monkeypatch.setattr("native_swarm.official_tools.current", lambda *_args: {})
    executor = m.ManagedExecutor()
    executor.root = tmp_path
    import pytest

    with pytest.raises(RuntimeError):
        executor.prepare_assignment("w", "<=255", "nonce", tmp_path / "prepare.log")
    assert (
        "BOUNDED_CONTINUATION_UNAVAILABLE_NO_FALLBACK"
        in (tmp_path / "prepare.log").read_text()
    )
    assert calls == ["list_functions", "function_info"]

    assert commands[-1][commands[-1].index("--address") + 1] == "0x1234"
    owned = tmp_path / "claims/existing"
    atomic(owned / "claim.json", {"address": "0x1234", "size": 101})
    # This is the actual pinned live_claims shape: address lives in claim.json.
    base.live_claims = lambda _: {
        "existing": {"claim_id": "existing", "size": 101, "path": owned}
    }
    with pytest.raises(RuntimeError):
        executor.prepare_assignment("next", "<=255", "nonce", tmp_path / "next.log")
    assert commands[-1][commands[-1].index("--address") + 1] == "0x5678"
