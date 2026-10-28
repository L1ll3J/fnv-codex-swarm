import json
from pathlib import Path
from native_swarm.state import Projection, atomic


def fixture(tmp_path):
    r = tmp_path / "run"
    p = r / "claims/c"
    (p / "work/recorded-checks").mkdir(parents=True)
    atomic(
        p / "claim.json",
        {"address": "0x123", "size": 900, "expires": "2099-01-01T00:00:00Z"},
    )
    atomic(p / "assignment.json", {"max_batch_rounds": 20})
    atomic(
        r / "rolling-executor/journal.json",
        {
            "jobs": {
                "w": {
                    "worker": "w",
                    "claim_id": "c",
                    "state": "running",
                    "pid": 99999999,
                }
            }
        },
    )
    return r, p


def test_lease_does_not_imply_running_and_failed_checks_do_not_improve_best(tmp_path):
    r, p = fixture(tmp_path)
    for i, c in enumerate(
        (
            {"status": "wip", "ndiff_insn": 15},
            {"status": "rejected", "ndiff_insn": 0},
            {"status": "wip", "ndiff_insn": 7},
            {"status": "wip", "ndiff_insn": 9},
        ),
        1,
    ):
        atomic(
            p / f"work/recorded-checks/check-{i:03}.json",
            {"checker_result": c, "finished_at": "2026-10-20T00:00:00Z"},
        )
    s = Projection().snapshot([r], tmp_path / "quota")
    row = s["rows"][0]
    assert (
        s["running"] == 0
        and s["open"] == 1
        and row["state"] == "open claim / no worker"
    )
    assert row["best"] == 7 and row["checks_since_best"] == 1
    assert [x["to"] for x in row["best_history"]] == [7]


def test_local_match_pending_and_verified_are_different(tmp_path):
    r, p = fixture(tmp_path)
    projection = Projection()
    atomic(p / "result.json", {"match": True})
    assert (
        projection.snapshot([r], tmp_path / "q")["rows"][0]["state"]
        == "MATCH awaiting controller"
    )
    atomic(p / "submission-response.json", {"submission_id": "s"})
    assert (
        projection.snapshot([r], tmp_path / "q")["rows"][0]["state"]
        == "verification pending"
    )
    atomic(r / "observed-verdicts.json", {"s": {"verdict": "verified"}})
    assert projection.snapshot([r], tmp_path / "q")["verified"] == 1


def test_work_directory_defect_report_is_visible(tmp_path):
    r, p = fixture(tmp_path)
    atomic(
        p / "work/defect-proposal.json",
        {"category": "missing declaration", "note": "evidence"},
    )
    s = Projection().snapshot([r], tmp_path / "q")
    assert (
        len(s["blockers"]) == 1
        and s["blockers"][0]["proposal"]["category"] == "missing declaration"
    )


def test_expired_saved_result_is_history_not_an_open_claim(tmp_path):
    r, p = fixture(tmp_path)
    atomic(
        p / "claim.json",
        {"address": "0x123", "size": 900, "expires": "2000-01-01T00:00:00Z"},
    )
    atomic(p / "result.json", {"match": True})
    s = Projection().snapshot([r], tmp_path / "q")
    assert s["open"] == 0 and s["rows"][0]["closed"]
    assert s["rows"][0]["state"] == "expired lease / saved MATCH"
    assert not (p / "giveup-response.json").exists()


def test_preparation_failure_is_visible_even_before_any_claim(tmp_path):
    r = tmp_path / "run"
    atomic(r / "native-app-managed.json", {"state_dir": str(tmp_path)})
    atomic(r / ".rolling-executor-owner.json", {"pid": 99999999})
    (r / "STOP_NEW_CLAIMS").write_text("assignment preparation failed")
    s = Projection().snapshot([r], tmp_path / "q")
    assert not s["rows"]
    assert s["controllers"][0]["stop"] == "assignment preparation failed"
