import runpy
from pathlib import Path

import pytest

from native_swarm.budgets import allocation, effective_limits, member_round_limit
from native_swarm.state import atomic


def fixture(tmp_path, monkeypatch, *, per_member=False):
    assignment = {
        "max_batch_rounds": 4,
        "max_plain_checks": 32,
        "max_compiler_invocations": 640,
        "official_internal_retry_bound": 20,
        "extend_while_improving": True,
        "improvement_window": 3,
    }
    if per_member:
        assignment.update(budget_mode="per_member", rounds_per_member=2)
    atomic(tmp_path / "assignment.json", assignment)
    events = []
    monkeypatch.setattr(
        "native_swarm.progress.checked_progress", lambda *a, **kw: {"events": events}
    )
    return assignment, events


def batch(
    claim,
    ordinal,
    *,
    improved=False,
    events=None,
    member=None,
    pending=False,
    reused=False,
):
    receipt = claim / f"work/recorded-checks/check-{ordinal:03d}.json"
    row = {
        "round_number": ordinal,
        "batch_id": f"{member}-round-{ordinal}" if member else f"round-{ordinal}",
        "results": [] if pending else [{"manifest": str(receipt), "reused": reused}],
    }
    if pending:
        row["pending"] = {"record": str(receipt)}
    atomic(claim / f"work/batch-rounds/{ordinal}.json", row)
    if improved:
        events.append({"manifest": str(receipt)})


def test_old_assignments_keep_hard_limits(tmp_path):
    assignment = {
        "max_batch_rounds": 2,
        "max_plain_checks": 16,
        "max_compiler_invocations": 320,
    }
    assert effective_limits(tmp_path, assignment) == assignment
    assert allocation({})["extend_while_improving"] is True


def test_recent_genuine_improvements_extend_all_ceilings(tmp_path, monkeypatch):
    assignment, events = fixture(tmp_path, monkeypatch)
    for number in range(1, 5):
        batch(tmp_path, number, improved=number == 4, events=events)
    limits = effective_limits(tmp_path)
    assert limits["max_batch_rounds"] == 7
    assert limits["max_plain_checks"] == 56
    assert limits["max_compiler_invocations"] == 1120
    batch(tmp_path, 5, improved=True, events=events)
    assert effective_limits(tmp_path)["max_batch_rounds"] == 8
    for number in (6, 7, 8):
        batch(tmp_path, number)
    assert effective_limits(tmp_path)["max_batch_rounds"] == 8
    assert assignment["max_batch_rounds"] == 4


def test_baseline_ties_and_reused_checks_do_not_extend(tmp_path, monkeypatch):
    _, events = fixture(tmp_path, monkeypatch)
    batch(tmp_path, 1, improved=True, events=events)
    batch(tmp_path, 4)
    assert effective_limits(tmp_path)["max_batch_rounds"] == 4
    batch(tmp_path, 4, improved=True, reused=True, events=events)
    assert effective_limits(tmp_path)["max_batch_rounds"] == 4


def test_pending_last_candidate_and_member_limits(tmp_path, monkeypatch):
    assignment, events = fixture(tmp_path, monkeypatch, per_member=True)
    batch(tmp_path, 1, member="peer-1")
    batch(tmp_path, 2, member="peer-2")
    batch(tmp_path, 3, member="peer-1", pending=True, improved=True, events=events)
    assert member_round_limit(tmp_path, assignment, "peer-1") == 5
    assert member_round_limit(tmp_path, assignment, "peer-2") == 2
    assert effective_limits(tmp_path)["max_batch_rounds"] == 7


def test_copied_batch_recorder_and_final_audit_guards_use_derived_limits(
    tmp_path, monkeypatch
):
    """Execute the adapted actual guard expressions without private fixtures."""
    from native_swarm.rolling_runtime import (
        configure_batch,
        configure_evidence,
        configure_recorder,
    )

    assignment, events = fixture(tmp_path, monkeypatch)
    batch(tmp_path, 4, improved=True, events=events)
    path = tmp_path / "batch.py"
    path.write_text(
        "assert 5<assignment['max_batch_rounds'],'Assigned batch-round budget exhausted'\nbatch={'identity':identity,'batch_id':batch_id,}\n"
    )
    configure_batch(path)
    scope = {
        "assignment": assignment,
        "claim": tmp_path,
        "identity": {},
        "batch_id": "new",
        "rounds": tmp_path / "work/batch-rounds",
    }
    scope.update(runpy.run_path(str(path), init_globals=scope))
    assert scope["batch"]["round_number"] == 2
    path.write_text(
        "def admission():\n  limit=assignment.get('max_compiler_invocations',assignment['max_plain_checks'])\n  assert len(prior)<assignment['max_plain_checks']\n  return limit\n"
    )
    configure_recorder(path)
    scope.update(prior=[None] * 40)
    scope.update(runpy.run_path(str(path), init_globals=scope))
    assert scope["admission"]() == 1120
    path.write_text(
        "assert count<=assignment['max_plain_checks']\nassert upper<=assignment['max_compiler_invocations']\n"
    )
    configure_evidence(path)
    scope.update(count=40, upper=1000)
    scope.update(runpy.run_path(str(path), init_globals=scope))
    scope["count"] = 57
    with pytest.raises(AssertionError):
        scope.update(runpy.run_path(str(path), init_globals=scope))


def test_real_durable_batch_driver_extends_then_stops_without_improvement(tmp_path):
    """Replay the actual batch admission state machine with CPU-free receipts."""
    from types import SimpleNamespace

    from native_swarm.group_check import digest, module
    from native_swarm.rolling_runtime import configure_batch
    from native_swarm.state import read

    driver = tmp_path / "batch.py"
    driver.write_bytes(
        (Path(__file__).parent / "fixtures/recorded_batch.py").read_bytes()
    )
    configure_batch(driver)
    single = tmp_path / "single.py"
    single.write_text("fixture recorder")
    assignment = {
        "claim_dir": str(tmp_path),
        "claim_id": "fixture",
        "assignment_path": str(tmp_path / "assignment.json"),
        "max_batch_rounds": 2,
        "max_plain_checks": 16,
        "max_compiler_invocations": 64,
        "extend_while_improving": True,
        "improvement_window": 2,
        "checker_workflow": {
            "single_check_driver": str(single),
            "single_check_driver_sha256": digest(single),
        },
    }
    atomic(tmp_path / "assignment.json", assignment)
    calls = []

    def invoke(command):
        source = Path(command[-1])
        number = len(calls) + 1
        calls.append(command)
        log = source.with_suffix(".log")
        log.write_text("actual fixture checker response")
        atomic(
            tmp_path / f"work/recorded-checks/check-{number:03d}.json",
            {
                "claim_id": "fixture",
                "source_sha256": digest(source),
                "source_path": str(source),
                "source_unchanged": True,
                "checker_unchanged": True,
                "log_path": str(log),
                "log_sha256": digest(log),
                "checker_exit_code": 1,
                "checker_result": {
                    "status": "wip",
                    "ndiff_insn": 10 if number == 1 else 9,
                },
            },
        )
        return SimpleNamespace(stdout=b"fixture", stderr=b"", returncode=1)

    run_batch = module(driver).run_batch
    source = tmp_path / "source.cpp"
    for number in range(1, 5):
        source.write_text(f"int candidate_{number};")
        run_batch(assignment, f"round-{number}", [source], invoke=invoke)
    assert effective_limits(tmp_path)["max_batch_rounds"] == 4
    assert read(tmp_path / "work/batch-rounds/round-4.json")["round_number"] == 4
    source.write_text("int forbidden_fifth;")
    with pytest.raises(AssertionError, match="budget exhausted"):
        run_batch(assignment, "round-5", [source], invoke=invoke)
    assert len(calls) == 4
