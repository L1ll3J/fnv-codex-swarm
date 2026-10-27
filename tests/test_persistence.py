"""Deterministic fake CLI tests; no model, lease, compiler or service calls."""

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from native_swarm import session
from native_swarm.budgets import allocation
from native_swarm.group_check import digest
from native_swarm.state import atomic, read


class Input(io.BytesIO):
    def close(self):
        self.sent = self.getvalue()
        super().close()


class FakeProcess:
    def __init__(self, events, code=0):
        self.pid = 12345
        self.stdin = Input()
        self.stdout = io.BytesIO(
            b"".join(json.dumps(e).encode() + b"\n" for e in events)
        )
        self.returncode = code

    def poll(self):
        return self.returncode

    def wait(self, **kwargs):
        return self.returncode


def setup(tmp_path):
    claim = tmp_path / "claims/test"
    driver = tmp_path / "recorder.py"
    driver.write_text("original recorder")
    assignment = {
        "claim_id": "test",
        "claim_dir": str(claim),
        "model": "gpt-6.1-terra",
        "reasoning_effort": "medium",
        "max_batch_rounds": 2,
        "max_plain_checks": 16,
        "max_compiler_invocations": 64,
        "timeout_minutes": 1,
        "checker_workflow": {
            "single_check_driver": str(driver),
            "single_check_driver_sha256": digest(driver),
        },
    }
    atomic(claim / "assignment.json", assignment)
    atomic(claim / "claim.json", {})
    atomic(tmp_path / "native-app-profile.json", {"group_size": 1})
    return claim, assignment


def events(thread="real-thread", *extra):
    return [
        {"type": "thread.started", "thread_id": thread},
        *extra,
        {"type": "turn.completed"},
    ]


def test_normal_early_exit_resumes_exact_thread_and_keeps_budget(tmp_path, monkeypatch):
    claim, assignment = setup(tmp_path)
    procs, commands = [], []

    def popen(argv, **kwargs):
        commands.append(argv)
        n = len(commands)
        proc = FakeProcess(events())
        procs.append(proc)
        atomic(
            claim / f"work/recorded-checks/check-{n:03d}.json",
            {
                "checker_exit_code": 1,
                "checker_result": {"status": "wip", "ndiff_insn": 10 - n},
                "compiler_invocations_upper_bound": 1,
            },
        )
        atomic(claim / f"work/batch-rounds/r{n}.json", {"state": "complete"})
        atomic(
            claim / "work/result.json", {"match": False, "residual_issue": f"early {n}"}
        )
        return proc

    monkeypatch.setattr(session.subprocess, "Popen", popen)
    monkeypatch.setattr(session, "run_codex", lambda root: Path("/fake/codex"))
    monkeypatch.setattr(
        "native_swarm.policy.decision",
        lambda root: {"reached": False, "mode": "stop_queuing"},
    )
    finish = []
    monkeypatch.setattr(
        session.subprocess,
        "run",
        lambda cmd, **kw: finish.append(cmd) or SimpleNamespace(returncode=0),
    )
    assert session.run(tmp_path, "test", b"original prompt") == 0
    assert len(commands) == 2
    assert "resume" not in commands[0]
    assert commands[1][-3:] == ["resume", "real-thread", "-"]
    assert procs[0].stdin.sent == b"original prompt"
    assert procs[1].stdin.sent == session.FOLLOWUP.encode()
    assert read(claim / "assignment.json") == assignment
    assert (
        read(claim / "session/early-001/result-1.json")["residual_issue"] == "early 1"
    )
    assert len(finish) == 1 and "--finish" in finish[0]
    assert finish[0][finish[0].index("--finish") + 1].endswith("check-002.json")
    assert session.resolution_ready(claim)


@pytest.mark.parametrize(
    "kind,code", [("approval_denied", 0), ("turn.failed", 0), ("error", 1)]
)
def test_errors_do_not_resume_or_resolve(tmp_path, monkeypatch, kind, code):
    claim, _ = setup(tmp_path)
    calls = []
    monkeypatch.setattr(session, "run_codex", lambda root: Path("/fake/codex"))
    monkeypatch.setattr(
        session.subprocess,
        "Popen",
        lambda *a, **kw: (
            calls.append(a) or FakeProcess(events("real", {"type": kind}), code)
        ),
    )
    monkeypatch.setattr(
        session.subprocess,
        "run",
        lambda *a, **kw: pytest.fail("No finish on genuine error"),
    )
    assert session.run(tmp_path, "test", b"prompt") == 2
    assert len(calls) == 1 and not session.resolution_ready(claim)


def test_startup_and_review_states_cannot_release_early_result(tmp_path):
    claim, _ = setup(tmp_path)
    assert not session.resolution_ready(claim)
    atomic(claim / "session/state.json", {"state": "running", "streams_closed": True})
    assert not session.resolution_ready(claim)
    atomic(
        claim / "session/state.json",
        {"state": "review_required", "streams_closed": True},
    )
    assert not session.resolution_ready(claim)


def test_budget_allocation_is_per_profile_and_scales_only_in_member_mode():
    assert (
        allocation({"rounds": 7, "group_size": 3, "budget_mode": "shared"})[
            "max_batch_rounds"
        ]
        == 7
    )
    value = allocation({"rounds": 7, "group_size": 3, "budget_mode": "per_member"})
    assert (
        value["max_batch_rounds"] == 21
        and value["max_plain_checks"] == 168
        and value["max_compiler_invocations"] == 672
    )
    for rounds in (0, True, 1001, 1.5):
        with pytest.raises(ValueError):
            allocation({"rounds": rounds})


def test_partial_dispatch_consumes_conservative_compiler_allowance(tmp_path):
    claim, assignment = setup(tmp_path)
    (claim / "work/recorded-checks").mkdir(parents=True)
    (claim / "work/recorded-checks/candidate-001.cpp").write_text("uncompleted")
    assignment["max_compiler_invocations"] = 4
    assert not session.remaining(claim, assignment)
