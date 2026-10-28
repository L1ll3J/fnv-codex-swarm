from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from native_swarm.board import Board
from native_swarm.group import audit_group, score
from native_swarm.group_check import check, digest
from native_swarm.state import atomic, read


def fixture(tmp_path):
    claim = tmp_path / "claims/claim"
    member = claim / "collaboration/peer-1"
    member.mkdir(parents=True)
    source = member / "candidate.cpp"
    source.write_text("int f(){return 1;}")
    driver = tmp_path / "recorded_batch.py"
    driver.write_text("unchanged original driver")
    assignment = claim / "assignment.json"
    atomic(
        assignment,
        {
            "claim_dir": str(claim),
            "claim_id": "claim",
            "max_batch_rounds": 20,
            "checker_workflow": {
                "driver_path": str(driver),
                "driver_sha256": digest(driver),
            },
        },
    )
    member_assignment = member / "assignment.json"
    atomic(
        member_assignment,
        {
            "work_dir": str(member),
            "collaboration": {
                "member": "peer-1",
                "board": str(claim / "collaboration/board.sqlite3"),
                "canonical_assignment": str(assignment),
            },
        },
    )
    return claim, member_assignment, source


def test_board_reservations_do_not_allow_two_members_same_experiment(tmp_path):
    board = Board(tmp_path / "board.sqlite3")
    assert board.reserve("peer-1", "same-hash")
    assert not board.reserve("peer-2", "same-hash")
    board.post(
        "peer-1", "hypothesis", {"source_sha256": "same-hash", "note": "untested"}
    )
    assert board.entries()[0]["kind"] == "hypothesis"


def test_group_checks_use_canonical_assignment_and_shared_budget(tmp_path, monkeypatch):
    claim, member, source = fixture(tmp_path)
    original = Mock(
        return_value={"batch_id": "peer-1-round-1", "match": False, "results": []}
    )
    monkeypatch.setattr(
        "native_swarm.group_check.module", lambda p: SimpleNamespace(run_batch=original)
    )
    check(member, "round-1", [source])
    assignment, batch_id, candidates = original.call_args.args
    assert assignment["assignment_path"] == str(claim / "assignment.json")
    assert assignment["max_batch_rounds"] == 20
    assert batch_id == "peer-1-round-1"
    assert candidates == [source]
    assert read(claim / "work/result.json") == {}


def test_group_reuses_real_duplicate_receipt_without_new_check(tmp_path, monkeypatch):
    claim, member, source = fixture(tmp_path)
    log = claim / "check.log"
    log.write_text("actual saved output")
    record = claim / "work/recorded-checks/check-001.json"
    atomic(
        record,
        {
            "source_path": str(source),
            "source_sha256": digest(source),
            "log_path": str(log),
            "log_sha256": digest(log),
        },
    )
    unexpected = Mock(side_effect=AssertionError("Must not compile duplicate"))
    monkeypatch.setattr("native_swarm.group_check.module", unexpected)
    result = check(member, "another-round", [source])
    assert result["reused_receipts"][digest(source)] == str(record)
    unexpected.assert_not_called()


def test_seal_blocks_new_checks_and_peers_cannot_submit_each_others_files(tmp_path):
    claim, member, source = fixture(tmp_path)
    external = tmp_path / "unassigned.cpp"
    external.write_text("int other;")
    with pytest.raises(ValueError, match="own member"):
        check(member, "r1", [external])
    atomic(claim / "collaboration/sealed.json", {})
    result = check(member, "r1", [source])
    assert result["checking_closed"] and result["status"]["checking_closed"]
    assert result["batch"] is None


def test_group_audit_refuses_incomplete_or_rejected_supervision(tmp_path):
    claim, _, _ = fixture(tmp_path)
    atomic(claim / "collaboration/group.json", {"state": "running"})
    with pytest.raises(ValueError, match="safely sealed"):
        audit_group(claim)
    atomic(
        claim / "collaboration/group.json",
        {"state": "sealed", "streams_closed": True, "approval_rejection": True},
    )
    with pytest.raises(ValueError):
        audit_group(claim)


def test_complete_match_always_ranks_before_partial_scores(tmp_path):
    match = tmp_path / "match.json"
    partial = tmp_path / "partial.json"
    atomic(match, {"checker_exit_code": 0, "checker_result": {"status": "match"}})
    atomic(
        partial,
        {"checker_exit_code": 1, "checker_result": {"status": "wip", "ndiff_insn": 0}},
    )
    assert score(match) < score(partial)


def test_all_members_share_native_round_limit(tmp_path):
    template = Path(
        str(
            Path.home()
            / "fnvdecomp/work/swarm-native-random-v2-20261026/recorded_batch.py"
        )
    )
    if not template.exists():
        pytest.skip("Local recorded batch fixture is unavailable")
    claim, member, source = fixture(tmp_path)
    driver = tmp_path / "recorded_batch.py"
    driver.write_bytes(template.read_bytes())
    single = tmp_path / "single.py"
    single.write_text("unchanged pinned recorder fixture")
    assignment = read(claim / "assignment.json")
    assignment["max_batch_rounds"] = 2
    assignment["checker_workflow"].update(
        driver_sha256=digest(driver),
        single_check_driver=str(single),
        single_check_driver_sha256=digest(single),
    )
    atomic(claim / "assignment.json", assignment)
    member2 = claim / "collaboration/peer-2"
    member2.mkdir()
    a2 = read(member)
    a2.update(
        work_dir=str(member2), collaboration={**a2["collaboration"], "member": "peer-2"}
    )
    atomic(member2 / "assignment.json", a2)
    source2 = member2 / "candidate.cpp"
    source2.write_text("int f(){return 2;}")
    calls = []

    def invoke(command):
        candidate = Path(command[-1])
        records = claim / "work/recorded-checks"
        log = candidate.with_suffix(".log")
        log.write_text("actual fixture output")
        atomic(
            records / f"check-{len(calls) + 1:03d}.json",
            {
                "claim_id": "claim",
                "source_path": str(candidate),
                "source_sha256": digest(candidate),
                "log_path": str(log),
                "log_sha256": digest(log),
                "checker_unchanged": True,
                "source_unchanged": True,
                "checker_exit_code": 1,
                "checker_result": {"status": "wip", "ndiff_insn": 10 - len(calls)},
            },
        )
        calls.append(command)
        return SimpleNamespace(stdout=b"fixture", stderr=b"", returncode=1)

    check(member, "first", [source], invoke)
    check(member2 / "assignment.json", "second", [source2], invoke)
    source.write_text("int f(){return 3;}")
    with pytest.raises(AssertionError, match="budget exhausted"):
        check(member, "third", [source], invoke)
    assert len(calls) == 2
    assert len(list((claim / "work/batch-rounds").glob("*.json"))) == 2


def test_unknown_group_terminality_blocks_expiry_cleanup(tmp_path, monkeypatch):
    import sys
    import types
    from native_swarm.group import due_expired_claims, group_terminal

    claim, _, _ = fixture(tmp_path)
    atomic(claim / "collaboration/group.json", {"state": "running", "members": []})
    fake = types.ModuleType("expiry_cleanup")
    fake.due_expired_claims = lambda *args, **kw: [{"claim_id": "claim"}]
    monkeypatch.setitem(sys.modules, "expiry_cleanup", fake)
    assert not group_terminal(claim)
    assert due_expired_claims(tmp_path) == []


def test_only_complete_zero_work_capacity_groups_are_retryable(tmp_path):
    import json
    from native_swarm.group import capacity_events

    members = []
    for index in range(2):
        directory = tmp_path / f"peer-{index}"
        directory.mkdir()
        events = [
            {"type": "thread.started", "thread_id": f"real-event-{index}"},
            {"type": "error", "message": "429 Too Many Requests"},
            {"type": "turn.failed"},
        ]
        (directory / "stdout.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events)
        )
        members.append({"workspace": str(directory), "exit_code": 1})
    assert len(capacity_events(members)) == 4
    with (tmp_path / "peer-1/stdout.jsonl").open("a") as output:
        output.write(
            "\n"
            + json.dumps(
                {"type": "item.completed", "item": {"type": "command_execution"}}
            )
        )
    assert capacity_events(members) == []


def test_ordinary_peer_crash_is_not_an_intentional_group_stop():
    from native_swarm.group import ordinary_peer_failure

    assert ordinary_peer_failure(
        {"exit_code": 1, "exit_before_shutdown": 1, "termination_requested": False}
    )
    assert ordinary_peer_failure(
        {"exit_code": 1, "exit_before_shutdown": None, "termination_requested": False}
    )
    assert not ordinary_peer_failure(
        {"exit_code": -15, "exit_before_shutdown": None, "termination_requested": True}
    )
    assert not ordinary_peer_failure(
        {"exit_code": 0, "exit_before_shutdown": 0, "termination_requested": False}
    )


@pytest.mark.parametrize(
    "event",
    [
        {"type": "approval.rejected"},
        {"event": "approval.denied"},
        {"type": "error", "code": "PERMISSION_DENIED"},
        {"status": "approval_rejected"},
    ],
)
def test_group_uses_native_rejection_event_normalization(event):
    from native_swarm.group import rejection_event

    assert rejection_event(event)
    assert not rejection_event({"type": "thread.started", "thread_id": "real-id"})


def test_rejection_retains_claim_and_stops_admissions(tmp_path, monkeypatch):
    import sys
    import types
    from native_swarm.group import due_expired_claims, retain_rejection

    claim, _, _ = fixture(tmp_path)
    atomic(
        claim / "collaboration/group.json",
        {
            "state": "sealed",
            "streams_closed": True,
            "approval_rejection": True,
            "members": [
                {"workspace": str(claim / "collaboration/peer-1"), "exit_code": 0}
            ],
        },
    )
    fake = types.ModuleType("expiry_cleanup")
    fake.due_expired_claims = lambda *args, **kw: [{"claim_id": "claim"}]
    monkeypatch.setitem(sys.modules, "expiry_cleanup", fake)
    # Manifest evidence alone blocks expiry, even if the current marker was lost.
    assert due_expired_claims(tmp_path) == []
    retain_rejection(tmp_path, "claim", "peer-1", {"type": "approval.rejected"})
    assert read(tmp_path / "approval-blocker-current.json")["claim_id"] == "claim"
    assert (tmp_path / "STOP_NEW_CLAIMS").is_file()
    assert due_expired_claims(tmp_path) == []


def test_member_reported_approval_rejection_is_not_dropped(tmp_path):
    from native_swarm.group import group_rejected, member_rejection

    claim, member, _ = fixture(tmp_path)
    atomic(member.parent / "result.json", {"approval_rejection": True})
    atomic(
        claim / "collaboration/group.json",
        {"members": [{"workspace": str(member.parent)}]},
    )
    assert member_rejection(member.parent)
    assert group_rejected(claim)


def test_large_native_diagnostics_leave_compact_auditable_board_receipts(
    tmp_path, monkeypatch
):
    claim, member, source = fixture(tmp_path)
    record = claim / "work/recorded-checks/check-001.json"
    source_hash = digest(source)
    native = {
        "batch_id": "peer-1-large",
        "match": False,
        "results": [
            {
                "manifest": str(record),
                "source_sha256": source_hash,
                "match": False,
                "exit_code": 1,
                "checker": {
                    "status": "wip",
                    "ndiff_insn": 70,
                    "diff": "large disassembly\n" * 30000,
                },
            }
        ],
    }
    monkeypatch.setattr(
        "native_swarm.group_check.module",
        lambda _: SimpleNamespace(run_batch=lambda *args, **kw: native),
    )
    result = check(member, "large", [source])
    assert len(result["batch"]["results"][0]["checker"]["diff"]) > 16384
    entries = Board(claim / "collaboration/board.sqlite3").entries()
    saved = next(e for e in entries if e["kind"] == "result")
    assert len(saved["body"].encode()) < 4096
    import json

    references = json.loads(saved["body"])["batch"]["results"]
    assert references[0]["manifest"] == "work/recorded-checks/check-001.json"


@pytest.mark.parametrize("relative", [True, False])
def test_group_audit_accepts_compact_receipt_references(tmp_path, relative):
    claim, member, source = fixture(tmp_path)
    prompt = member.parent / "cli-prompt.txt"
    prompt.write_text("actual launched prompt fixture")
    record = claim / "work/recorded-checks/check-001.json"
    atomic(record, {"fixture": True})
    atomic(
        claim / "collaboration/group.json",
        {
            "state": "sealed",
            "streams_closed": True,
            "canonical_assignment_sha256": digest(claim / "assignment.json"),
            "model": "gpt-6.1-sol",
            "members": [
                {
                    "workspace": str(member.parent),
                    "exit_code": 0,
                    "thread_id": "real-fixture-id",
                    "prompt_sha256": digest(prompt),
                    "assignment_sha256": digest(member),
                }
            ],
        },
    )
    reference = str(record.relative_to(claim)) if relative else str(record)
    Board(claim / "collaboration/board.sqlite3").post(
        "peer-1", "result", {"batch": {"results": [{"manifest": reference}]}}
    )
    atomic(
        tmp_path / "usage-current.json",
        {
            "threads": [
                {
                    "id": "real-fixture-id",
                    "model": "gpt-6.1-sol",
                    "usage": {"total_tokens": 10},
                }
            ]
        },
    )
    assert audit_group(claim) is True


def test_exhausted_member_cannot_reserve_another_members_candidate(tmp_path):
    claim, member, source = fixture(tmp_path)
    canonical = read(claim / "assignment.json")
    canonical.update(budget_mode="per_member", rounds_per_member=1, max_batch_rounds=2)
    atomic(claim / "assignment.json", canonical)
    atomic(claim / "work/batch-rounds/peer-1-used.json", {"state": "complete"})
    with pytest.raises(ValueError, match="Member round allowance"):
        check(member, "new", [source])
    board = Board(claim / "collaboration/board.sqlite3")
    assert board.reserve("peer-2", digest(source))


def stopped_group_fixture(tmp_path):
    from native_swarm.commands import operator_terminal

    claim, member, _ = fixture(tmp_path)
    prompt = member.parent / "cli-prompt.txt"
    prompt.write_text("immutable launched prompt")
    record = claim / "work/recorded-checks/check-001.json"
    atomic(record, {"fixture": True})
    atomic(member.parent / "thread.json", {"thread_id": "real-thread"})
    atomic(
        claim / "collaboration/group.json",
        {
            "state": "running",
            "streams_closed": False,
            "ordinary_failure": True,
            "model": "gpt-6.1-sol",
            "canonical_assignment_sha256": digest(claim / "assignment.json"),
            "members": [
                {
                    "workspace": str(member.parent),
                    "exit_code": None,
                    "prompt_sha256": digest(prompt),
                    "assignment_sha256": digest(member),
                }
            ],
        },
    )
    Board(claim / "collaboration/board.sqlite3").post(
        "peer-1", "result", {"batch": {"results": [{"manifest": str(record)}]}}
    )
    atomic(
        tmp_path / "usage-current.json",
        {
            "threads": [
                {
                    "id": "real-thread",
                    "model": "gpt-6.1-sol",
                    "usage": {"total_tokens": 10},
                }
            ]
        },
    )
    atomic(
        claim / "operator-stop-terminal.json",
        {
            "verified_stopped": True,
            "assignment_sha256": digest(claim / "assignment.json"),
            "pids": [],
        },
    )
    assert operator_terminal(claim)
    return claim, member, prompt, record


def test_verified_operator_stop_retains_group_integrity_audit(tmp_path):
    claim, _, _, _ = stopped_group_fixture(tmp_path)
    with pytest.raises(ValueError, match="safely sealed"):
        audit_group(claim)
    assert audit_group(claim, allow_operator_stop=True)


@pytest.mark.parametrize(
    "artifact",
    ["prompt", "member_assignment", "canonical_assignment", "receipt_provenance"],
)
def test_operator_match_cannot_bypass_group_integrity(tmp_path, artifact):
    claim, member, prompt, record = stopped_group_fixture(tmp_path)
    if artifact == "prompt":
        prompt.write_text("changed prompt")
    elif artifact == "member_assignment":
        atomic(member, {"changed": True})
    elif artifact == "canonical_assignment":
        atomic(claim / "assignment.json", {"changed": True})
    else:
        atomic(record.with_name("check-002.json"), {"unattributed": True})
    with pytest.raises(ValueError):
        audit_group(claim, allow_operator_stop=True)
