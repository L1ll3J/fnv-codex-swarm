import json
import sys
import threading
import time
from pathlib import Path

import pytest

from native_swarm import session
from native_swarm.group_check import digest
from native_swarm.state import atomic, read


def events(capacity=True):
    return [
        {"type": "thread.started", "thread_id": "actual-thread"},
        {"type": "item.started", "item": {"type": "command_execution", "id": "tool"}},
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "id": "tool", "exit_code": 1},
        },
        {
            "type": "error",
            "message": "Selected model is at capacity"
            if capacity
            else "Unknown failure",
        },
        {
            "type": "turn.failed",
            "error": {
                "message": "Selected model is at capacity"
                if capacity
                else "Unknown failure"
            },
        },
    ]


@pytest.mark.parametrize("failures,last_error", [(1, False), (5, True)])
def test_real_supervisor_resumes_actual_thread_after_toolwork_and_capacity(
    tmp_path, monkeypatch, failures, last_error
):
    claim = tmp_path / "claims/claim"
    claim.mkdir(parents=True)
    driver = tmp_path / "finish.py"
    driver.write_text("# CPU-free finish fixture\n")
    assignment = {
        "claim_dir": str(claim),
        "model": "gpt-6.1-sol",
        "reasoning_effort": "medium",
        "max_batch_rounds": 2,
        "checker_workflow": {
            "single_check_driver": str(driver),
            "single_check_driver_sha256": digest(driver),
        },
    }
    atomic(claim / "assignment.json", assignment)
    atomic(claim / "claim.json", {})
    calls, delays = [], []
    actual_event = threading.Event

    class FastEvent(actual_event):
        def wait(self, delay=None):
            if delay is not None and delay >= 1:
                delays.append(delay)
            return super().wait(min(delay or 0, 0.001))

    monkeypatch.setattr(session.threading, "Event", FastEvent)
    monkeypatch.setattr("native_swarm.policy.decision", lambda root: {"reached": False})

    def command(root, supplied, thread):
        calls.append(thread)
        number = len(calls)
        rows = (
            events()
            if number <= failures or last_error
            else [
                {"type": "thread.started", "thread_id": "actual-thread"},
                {"type": "turn.completed"},
            ]
        )
        receipt = {
            "checker_exit_code": 1 if number <= failures else 0,
            "checker_result": {"status": "wip", "ndiff_insn": 3}
            if number <= failures
            else {"status": "match"},
        }
        path = claim / f"work/recorded-checks/check-{number:03d}.json"
        code = f"import json,pathlib,sys;sys.stdin.read();p=pathlib.Path({str(path)!r});p.parent.mkdir(parents=True,exist_ok=True);p.write_text({json.dumps(receipt)!r});[print(json.dumps(row),flush=True) for row in {rows!r}];sys.exit({1 if number <= failures or last_error else 0})"
        return [sys.executable, "-c", code]

    monkeypatch.setattr(session, "command", command)
    assert session.run(tmp_path, "claim", b"initial prompt") == 0
    assert calls == [None] + ["actual-thread"] * failures
    assert 15 in delays
    state = read(claim / "session/state.json")
    assert state["state"] == "complete" and state["capacity_retries"] == failures
    assert len(state["turns"]) == failures + 1
    assert len(list((claim / "work/recorded-checks").glob("*.json"))) == failures + 1


def saved_failure(tmp_path):
    claim = tmp_path / "claims/claim"
    directory = claim / "session"
    atomic(
        directory / "state.json",
        {
            "state": "review_required",
            "streams_closed": True,
            "thread_id": None,
            "deadline": time.time() + 60,
            "turns": [{"number": 1, "pid": 999999998, "exit_code": 1}],
        },
    )
    atomic(directory / "launch-intent.json", {"pid": 999999999})
    (directory / "turn-001.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events())
    )
    return claim, directory


def test_recovery_reconstructs_same_thread_preserving_deadline(tmp_path, monkeypatch):
    claim, directory = saved_failure(tmp_path)
    monkeypatch.setattr(
        session.os, "kill", lambda *args: (_ for _ in ()).throw(ProcessLookupError())
    )
    before = read(directory / "state.json")
    restored = session.recovery_state(tmp_path, claim)
    assert restored["thread_id"] == "actual-thread"
    assert restored["deadline"] == before["deadline"]
    assert read(directory / "state.json") == before


@pytest.mark.parametrize(
    "failure",
    [
        "live",
        "deadline",
        "ambiguous",
        "other_error",
        "safety",
        "unfinished_tool",
        "retry_limit",
    ],
)
def test_recovery_preserves_unsafe_or_noncapacity_failures(
    tmp_path, monkeypatch, failure
):
    claim, directory = saved_failure(tmp_path)
    monkeypatch.setattr(
        session.os, "kill", lambda *args: (_ for _ in ()).throw(ProcessLookupError())
    )
    state = read(directory / "state.json")
    if failure == "live":
        monkeypatch.setattr(session.os, "kill", lambda *args: None)
    elif failure == "deadline":
        state["deadline"] = time.time() - 1
    elif failure == "retry_limit":
        state["capacity_retries"] = 5
    elif failure == "ambiguous":
        path = claim / "work/recorded-checks/candidate-001.cpp"
        path.parent.mkdir(parents=True)
        path.write_text("uncertain compilation")
    else:
        rows = events(capacity=failure != "other_error")
        if failure == "safety":
            rows.append({"type": "approval.rejected"})
        if failure == "unfinished_tool":
            rows.pop(2)
        (directory / "turn-001.jsonl").write_text(
            "\n".join(json.dumps(e) for e in rows)
        )
    atomic(directory / "state.json", state)
    with pytest.raises(ValueError):
        session.recovery_state(tmp_path, claim)
