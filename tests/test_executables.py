import io
import json
from types import SimpleNamespace
from unittest.mock import Mock
import time

import pytest

from native_swarm.executables import CodexNotFound, resolve_codex, run_codex
from native_swarm.state import atomic


def binary(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o700)
    return path


def isolate(monkeypatch, home):
    monkeypatch.delenv("NATIVE_SWARM_CODEX", raising=False)
    monkeypatch.setattr("native_swarm.executables.shutil.which", lambda name: None)
    monkeypatch.setattr("native_swarm.executables.Path.home", lambda: home)
    monkeypatch.setattr("native_swarm.executables.platform.system", lambda: "Linux")
    monkeypatch.setattr("native_swarm.executables.platform.machine", lambda: "x86_64")


def test_missing_path_discovers_newest_compatible_user_extension(tmp_path, monkeypatch):
    isolate(monkeypatch, tmp_path)
    root = tmp_path / ".vscode/extensions"
    binary(root / "openai.chatgpt-9.0/bin/linux-x86_64/codex")
    latest = binary(root / "openai.chatgpt-10.1/bin/linux-x86_64/codex")
    binary(root / "openai.chatgpt-11.0/bin/windows-x86_64/codex")
    assert resolve_codex() == latest


def test_explicit_path_wins_and_invalid_override_never_falls_back(
    tmp_path, monkeypatch
):
    isolate(monkeypatch, tmp_path)
    chosen = binary(tmp_path / "chosen codex")
    monkeypatch.setenv("NATIVE_SWARM_CODEX", str(chosen))
    assert resolve_codex() == chosen
    chosen.unlink()
    monkeypatch.setattr(
        "native_swarm.executables.shutil.which", lambda name: "/usr/bin/other"
    )
    with pytest.raises(CodexNotFound, match="unavailable"):
        resolve_codex()


def test_path_installation_preferred_over_extension(tmp_path, monkeypatch):
    isolate(monkeypatch, tmp_path)
    chosen = binary(tmp_path / "codex")
    monkeypatch.setattr(
        "native_swarm.executables.shutil.which", lambda name: str(chosen)
    )
    assert resolve_codex() == chosen


def test_pinned_run_executable_is_not_reselected(tmp_path, monkeypatch):
    isolate(monkeypatch, tmp_path)
    chosen = binary(tmp_path / "original")
    atomic(tmp_path / "native-app-codex.json", {"executable": str(chosen)})
    monkeypatch.setenv("NATIVE_SWARM_CODEX", str(tmp_path / "other"))
    assert run_codex(tmp_path) == chosen
    chosen.unlink()
    with pytest.raises(CodexNotFound):
        run_codex(tmp_path)


def test_no_executable_error_does_not_claim_sign_in_is_broken(tmp_path, monkeypatch):
    isolate(monkeypatch, tmp_path)
    with pytest.raises(CodexNotFound, match="executable not found"):
        resolve_codex()


def test_allowance_rpc_uses_explicit_binary_without_any_model_turn(
    tmp_path, monkeypatch
):
    from native_swarm.quota import limits

    chosen = binary(tmp_path / "codex")
    reply = {
        "rateLimits": {"primary": {"usedPercent": 23, "resetsAt": time.time() + 100}}
    }
    proc = SimpleNamespace(
        stdin=io.StringIO(),
        stdout=io.StringIO(
            json.dumps({"id": 1, "result": {}})
            + "\n"
            + json.dumps({"id": 2, "result": reply})
            + "\n"
        ),
        terminate=Mock(),
        wait=Mock(),
        kill=Mock(),
    )
    calls = []

    def start(argv, **kwargs):
        calls.append(argv)
        return proc

    monkeypatch.setattr("native_swarm.quota.subprocess.Popen", start)
    result = limits(executable=chosen)
    assert calls == [[str(chosen), "app-server", "--listen", "stdio://"]]
    assert result["active_windows"][0]["used_percent"] == 23
    assert result["model_calls"] == 0
    proc.terminate.assert_called_once()


def test_backend_preserves_discovery_error(tmp_path, monkeypatch):
    from native_swarm.backend import Backend, ControlError

    monkeypatch.setattr(
        "native_swarm.backend.resolve_codex",
        Mock(side_effect=CodexNotFound("Codex executable not found")),
    )
    with pytest.raises(ControlError, match="Codex executable not found"):
        Backend(tmp_path).quota({"quota_path": str(tmp_path / "quota")})
    assert not (tmp_path / "quota").exists()
