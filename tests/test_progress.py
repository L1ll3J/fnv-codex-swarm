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


def test_current_checked_seed_can_establish_baseline_after_candidate(tmp_path):
    atomic(
        tmp_path / "assignment.json", {"server_attempt_seed": {"source_sha256": "seed"}}
    )
    path = receipt(tmp_path, 1, 10, "new")
    receipt(tmp_path, 2, 15, "seed")
    p = checked_progress(tmp_path)
    assert p["best"] == 10
    assert [(r["from"], r["to"]) for r in p["events"]] == [(15, 10)]
    assert p["events"][0]["manifest"] == str(path)


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
