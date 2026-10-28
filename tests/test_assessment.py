"""Assessor scope, evidence freshness and queue classification, without model calls."""

from pathlib import Path

import pytest

from native_swarm.assessment import annotated, evidence, validate_result
from native_swarm.state import atomic


def fixture(tmp_path):
    claim = tmp_path / "claim"
    report = {
        "id": str(claim / "work/defect-proposal.json"),
        "evidence_path": str(claim),
        "address": "0x123",
        "proposal": {"note": "claim"},
    }
    atomic(Path(report["id"]), report["proposal"])
    atomic(claim / "assignment.json", {"model": "gpt-6.1-sol"})
    (claim / "pack.tar.gz").write_bytes(b"pack")
    return claim, report, [{"id": str(claim), "address": "0x123"}]


def test_unchanged_report_not_reassessed_for_each_failed_check(tmp_path):
    claim, report, rows = fixture(tmp_path)
    before = evidence(report, rows)[0]
    atomic(
        claim / "work/recorded-checks/check-001.json",
        {"checker_exit_code": 1, "checker_result": {"status": "wip", "ndiff_insn": 10}},
    )
    assert evidence(report, rows)[0] == before
    atomic(
        tmp_path / "state/assessments/a/decision.json",
        {"fingerprint": before, "decision": "observation"},
    )
    assert (
        annotated(tmp_path / "state", [report], rows)[0]["assessment"]["decision"]
        == "observation"
    )
    atomic(
        claim / "work/recorded-checks/check-002.json",
        {"checker_exit_code": 0, "checker_result": {"status": "match"}},
    )
    assert evidence(report, rows)[0] != before
    assert not annotated(tmp_path / "state", [report], rows)[0]["assessment"]


def test_changed_pack_or_report_invalidates_decision(tmp_path):
    claim, report, rows = fixture(tmp_path)
    before = evidence(report, rows)[0]
    (claim / "pack.tar.gz").write_bytes(b"new pack")
    assert evidence(report, rows)[0] != before
    report["proposal"]["note"] = "new evidence"
    assert evidence(report, rows)[0] != before


def test_assessor_evidence_must_stay_inside_supplied_directories(tmp_path):
    claim, report, _ = fixture(tmp_path)
    value = {
        "evidence_refs": [{"artifact": report["id"], "observation": "checked fact"}],
        "explanation": "fact",
        "decision": "observation",
        "missing_evidence": [],
        "suggested_report": "fact",
    }
    assert validate_result(value, [claim]) == value
    external = tmp_path / "secret"
    external.write_text("not supplied")
    value["evidence_refs"][0]["artifact"] = str(external)
    with pytest.raises(ValueError):
        validate_result(value, [claim])
    value["evidence_refs"] = []
    with pytest.raises(ValueError):
        validate_result(value, [claim])
    value["decision"] = "inconclusive"
    assert validate_result(value, [claim])


def test_assessor_is_fresh_read_only_and_persists_actual_thread(tmp_path, monkeypatch):
    import io
    import json
    import time
    import datetime as dt
    from native_swarm import assessment
    from native_swarm.state import read

    claim, report, rows = fixture(tmp_path)
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    state = tmp_path / "state"
    state.mkdir()
    atomic(
        state / "config.json",
        {"worker_capacity": 1, "quota_path": str(state / "quota.json")},
    )
    atomic(
        state / "quota.json",
        {
            "checked_at": stamp,
            "latest_counter": {"observed_at": stamp},
            "active_windows": [{"used_percent": 10, "resets_at": time.time() + 100}],
        },
    )
    monkeypatch.setattr(assessment, "resolve_codex", lambda: Path("/fake/codex"))
    monkeypatch.setattr(
        assessment,
        "observed_session",
        lambda thread, cwd: {"model": assessment.MODEL, "usage": {"total_tokens": 123}},
    )
    calls = []
    value = {
        "evidence_refs": [{"artifact": report["id"], "observation": "observed"}],
        "explanation": "supported",
        "decision": "observation",
        "missing_evidence": [],
        "suggested_report": "supported",
    }

    class Proc:
        pid = 123
        returncode = 0
        stdin = io.BytesIO()

        def poll(self):
            return 0

    def popen(argv, **kw):
        calls.append(argv)
        kw["stdout"].write(
            (
                json.dumps({"type": "thread.started", "thread_id": "real"})
                + "\n"
                + json.dumps({"type": "turn.completed"})
                + "\n"
            ).encode()
        )
        atomic(Path(argv[argv.index("-o") + 1]), value)
        return Proc()

    key, payload, roots = assessment.evidence(report, rows)
    assert assessment.assess(state, report, rows, key, payload, roots, proc_start=popen)
    assert len(calls) == 1 and "--approve-for-me" not in calls[0]
    assert calls[0][calls[0].index("--sandbox") + 1] == "read-only"
    assert 'model_reasoning_effort="medium"' in calls[0]
    decisions = list((state / "assessments").glob("*/decision.json"))
    assert read(decisions[0])["thread_id"] == "real"
    assert read(decisions[0])["usage"]["total_tokens"] == 123


def test_dead_supervisor_does_not_free_live_or_ambiguous_children(
    tmp_path, monkeypatch
):
    from native_swarm.assessment import occupied

    run = tmp_path / "run"
    atomic(run / "native-app-profile.json", {"group_size": 3})
    atomic(
        run / "rolling-executor/journal.json",
        {"jobs": {"worker": {"claim_id": "claim", "pid": 10, "state": "running"}}},
    )
    group = run / "claims/claim/collaboration/group.json"
    atomic(
        group, {"state": "running", "streams_closed": False, "members": [{"pid": 20}]}
    )
    monkeypatch.setattr(
        "native_swarm.assessment.process_alive", lambda pid, root: pid == 20
    )
    cfg = {"managed_runs": [str(run)]}
    assert occupied(cfg) == 3
    monkeypatch.setattr(
        "native_swarm.assessment.process_alive", lambda pid, root: False
    )
    assert occupied(cfg) == 3  # Unknown shutdown still reserves the full allocation.
    atomic(group, {"state": "sealed", "streams_closed": True, "members": [{"pid": 20}]})
    assert occupied(cfg) == 0


def test_dead_independent_supervisor_retains_unfinished_turn(tmp_path, monkeypatch):
    from native_swarm.assessment import occupied

    run = tmp_path / "run"
    atomic(run / "native-app-profile.json", {"group_size": 1})
    atomic(
        run / "rolling-executor/journal.json",
        {"jobs": {"worker": {"claim_id": "claim", "pid": 10, "state": "running"}}},
    )
    session = run / "claims/claim/session/state.json"
    atomic(session, {"streams_closed": False, "turns": [{"number": 1}]})
    monkeypatch.setattr(
        "native_swarm.assessment.process_alive", lambda pid, root: False
    )
    assert occupied({"managed_runs": [str(run)]}) == 1
    atomic(session, {"streams_closed": True, "turns": [{"number": 1, "pid": 20}]})
    monkeypatch.setattr(
        "native_swarm.assessment.process_alive", lambda pid, root: pid == 20
    )
    assert occupied({"managed_runs": [str(run)]}) == 1


def test_abandoned_assessor_retains_slot_and_prevents_second_assessor(
    tmp_path, monkeypatch
):
    from native_swarm.assessment import assessor_running, occupied

    state = tmp_path / "state"
    atomic(state / "assessments/first/launch.json", {"intent": True})
    assert assessor_running(state)
    assert occupied({}, state) == 0  # Assessor capacity is independent of reconstruction slots.
