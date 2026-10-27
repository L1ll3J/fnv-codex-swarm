"""Deterministic shared budget and best receipt, supplied on every board read."""

from pathlib import Path
from .state import read


def status(claim):
    claim = Path(claim)
    assignment = read(claim / "assignment.json")
    records = [
        (p, read(p)) for p in (claim / "work/recorded-checks").glob("check-*.json")
    ]
    rounds = list((claim / "work/batch-rounds").glob("*.json"))
    ranked = []
    for path, row in records:
        result = row.get("checker_result", {})
        score = (
            0
            if result.get("status") == "match" and row.get("checker_exit_code") == 0
            else result.get("ndiff_insn")
            if result.get("status") == "wip"
            else None
        )
        if type(score) is int:
            ranked.append((score, str(path)))
    best = min(ranked) if ranked else None
    nominated = read(Path(best[1])) if best else {}
    return {
        "claim_id": claim.name,
        "rounds_used": len(rounds),
        "rounds_limit": assignment.get("max_batch_rounds", 20),
        "rounds_remaining": max(
            0, assignment.get("max_batch_rounds", 20) - len(rounds)
        ),
        "checks": len(records),
        "compiler_invocations_upper_bound": sum(
            r.get("compiler_invocations_upper_bound", assignment.get("official_internal_retry_bound", 4)) for _, r in records
        )
        + max(
            0,
            len(list((claim / "work/recorded-checks").glob("candidate-*.cpp")))
            - len(records),
        )
        * assignment.get("official_internal_retry_bound", 4),
        "budget_mode": assignment.get("budget_mode", "shared"),
        "rounds_per_member": assignment.get("rounds_per_member"),
        "member_rounds_used": {
            p.name: len(list((claim / "work/batch-rounds").glob(p.name + "-*.json")))
            for p in (claim / "collaboration").glob("peer-*")
            if p.is_dir()
        },
        "best": {
            "score": best[0],
            "manifest": best[1],
            "source_sha256": nominated.get("source_sha256"),
        }
        if best
        else None,
        "diagnostic_scope": "candidate instruction diff may be truncated at 4096 bytes; plain MATCH remains authoritative",
        "checking_closed": (claim / "collaboration/sealed.json").exists(),
        "handoff": read(claim / "collaboration/handoff-phase.json"),
    }
