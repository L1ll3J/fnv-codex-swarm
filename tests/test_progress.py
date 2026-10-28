from native_swarm.progress import checked_progress
from native_swarm.state import Projection, atomic


def receipt(claim, number, score, source=None, code=1, status="wip"):
    path = claim / f"work/recorded-checks/check-{number:03d}.json"
    atomic(
        path,
        {
            "checker_result": {"status": status, "ndiff_insn": score},
            "checker_exit_code": code,
            "source_sha256": source,
            "finished_at": "2026-10-28T12:00:00Z",
        },
    )
    return path


def test_supplied_seed_checks_establish_baseline_not_progress(tmp_path):
    claim = tmp_path / "claims/c"
    atomic(claim / "claim.json", {"address": "0x123", "size": 900})
    atomic(
        claim / "assignment.json",
        {"server_attempt_seed": {"source_sha256": "supplied"}},
    )
    receipt(claim, 1, 15, "supplied")
    receipt(claim, 2, 15, "unchanged-equivalent")
    row = Projection().snapshot([tmp_path], tmp_path / "quota")["rows"][0]
    assert row["best"] == 15
    assert row["checks_since_best"] is None
    assert row["seconds_since_best"] is None
    assert row["best_history"] == []
    receipt(claim, 3, 10, "improved")
    receipt(claim, 4, 15, "supplied")
    row = Projection().snapshot([tmp_path], tmp_path / "quota")["rows"][0]
    assert row["best"] == 10
    assert row["checks_since_best"] == 1
    assert row["seconds_since_best"] is not None
    assert [(r["from"], r["to"]) for r in row["best_history"]] == [(15, 10)]


def test_unchecked_packet_score_cannot_create_progress(tmp_path):
    atomic(
        tmp_path / "assignment.json",
        {"server_attempt_seed": {"source_sha256": "seed", "packet_score": 50}},
    )
    receipt(tmp_path, 1, 20, "first")
    receipt(tmp_path, 2, 22, "second")
    p = checked_progress(tmp_path)
    assert p["best"] == 20
    assert p["events"] == []


def test_future_seed_cannot_retroactively_invent_progress(tmp_path):
    atomic(
        tmp_path / "assignment.json", {"server_attempt_seed": {"source_sha256": "seed"}}
    )
    receipt(tmp_path, 1, 10, "new")
    receipt(tmp_path, 2, 15, "seed")
    p = checked_progress(tmp_path)
    assert p["best"] == 10
    assert p["events"] == []


def test_saved_work_and_failed_checks_do_not_reset_progress(tmp_path):
    atomic(
        tmp_path / "work/attempt-history.json", {"entries": [{"source_sha256": "old"}]}
    )
    receipt(tmp_path, 1, 30, "initial")
    receipt(tmp_path, 2, 20, "old")
    receipt(tmp_path, 3, 10, "actual-improvement")
    receipt(tmp_path, 4, 0, "failed", code=2)
    receipt(tmp_path, 5, 0, "rejected", code=0, status="rejected")
    p = checked_progress(tmp_path)
    assert p["best"] == 10
    assert p["best_check"] == 3
    assert len(p["events"]) == 1


def test_equal_scores_and_rechecked_sources_never_extend_a_window(tmp_path):
    receipt(tmp_path, 1, 15, "initial")
    receipt(tmp_path, 2, 10, "improvement")
    receipt(tmp_path, 3, 10, "improvement")
    receipt(tmp_path, 4, 11, "worse")
    assert [r["check"] for r in checked_progress(tmp_path)["events"]] == [2]


def test_later_better_seed_does_not_revoke_admitted_rounds(tmp_path):
    from native_swarm.budgets import effective_limits

    atomic(
        tmp_path / "assignment.json",
        {
            "max_batch_rounds": 2,
            "max_plain_checks": 16,
            "max_compiler_invocations": 64,
            "extend_while_improving": True,
            "improvement_window": 3,
            "server_attempt_seed": {"source_sha256": "seed"},
        },
    )
    receipt(tmp_path, 1, 15, "initial")
    improved = receipt(tmp_path, 2, 10, "improved")
    atomic(
        tmp_path / "work/batch-rounds/2.json",
        {
            "round_number": 2,
            "batch_id": "second",
            "results": [{"manifest": str(improved)}],
        },
    )
    limits = effective_limits(tmp_path)
    assert limits["max_batch_rounds"] == 5
    receipt(tmp_path, 3, 5, "seed")
    assert effective_limits(tmp_path) == limits
    assert [e["check"] for e in checked_progress(tmp_path)["events"]] == [2]


def test_numeric_receipt_order_after_999_preserves_window_and_display(tmp_path):
    from native_swarm.budgets import effective_limits

    claim = tmp_path / "claims/c"
    atomic(claim / "claim.json", {"address": "0x123", "size": 900})
    atomic(
        claim / "assignment.json",
        {
            "max_batch_rounds": 1000,
            "max_plain_checks": 8000,
            "max_compiler_invocations": 32000,
            "extend_while_improving": True,
            "improvement_window": 3,
        },
    )
    receipt(claim, 1, 100, "baseline")
    improved = receipt(claim, 999, 10, "improved")
    worse = receipt(claim, 1000, 50, "worse")
    for number, path in ((999, improved), (1000, worse)):
        atomic(
            claim / f"work/batch-rounds/{number}.json",
            {
                "round_number": number,
                "batch_id": str(number),
                "results": [{"manifest": str(path)}],
            },
        )
    p = checked_progress(claim)
    assert [e["manifest"] for e in p["events"]] == [str(improved)]
    assert effective_limits(claim)["max_batch_rounds"] == 1002
    row = Projection().snapshot([tmp_path], tmp_path / "quota")["rows"][0]
    assert row["best"] == 10
    assert row["checks_since_best"] == 1
    assert [e["manifest"] for e in row["best_history"]] == [str(improved)]
