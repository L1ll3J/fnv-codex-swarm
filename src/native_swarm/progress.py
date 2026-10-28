"""Checked progress relative to supplied work, rather than baseline validation."""

from pathlib import Path

from .state import read


def checked_score(record):
    result = record.get("checker_result", {})
    code = record.get("checker_exit_code", 0)
    if result.get("status") == "match":
        return 0 if code == 0 else None
    if code not in (0, 1):
        return None
    score = result.get("ndiff_insn") if result.get("status") == "wip" else None
    return score if type(score) is int and score >= 0 else None


def supplied_hashes(claim, assignment):
    hashes = set()
    seed = assignment.get("server_attempt_seed", {}).get("source_sha256")
    if isinstance(seed, str) and seed:
        hashes.add(seed)
    # Use the claim-local snapshot of earlier attempts, never current-run receipts
    # or an arbitrary path supplied by report text.
    history = read(claim / "work/attempt-history.json")
    for entry in history.get("entries", []):
        source = entry.get("source_sha256")
        if isinstance(source, str) and source:
            hashes.add(source)
    return hashes


def checked_progress(claim, records=None, assignment=None):
    claim = Path(claim)
    assignment = read(claim / "assignment.json") if assignment is None else assignment
    paths = sorted((claim / "work/recorded-checks").glob("check-*.json"))
    if records is None:
        records = [read(path) for path in paths]
    supplied = supplied_hashes(claim, assignment)
    scored = [(index, row, checked_score(row)) for index, row in enumerate(records, 1)]
    baseline = [
        score
        for _, row, score in scored
        if score is not None and row.get("source_sha256") in supplied
    ]
    if not baseline:
        baseline = [score for _, _, score in scored if score is not None][:1]
    best = min(baseline) if baseline else None
    best_check, best_time = None, None
    events = []
    for index, row, score in scored:
        if score is None or (best is not None and score >= best):
            continue
        previous, best = best, score
        if row.get("source_sha256") in supplied:
            continue
        best_check = index
        best_time = row.get("finished_at") or row.get("started_at")
        events.append(
            {
                "check": index,
                "from": previous,
                "to": score,
                "at": best_time,
                "source_sha256": row.get("source_sha256"),
                "manifest": str(paths[index - 1]) if index <= len(paths) else None,
            }
        )
    return {
        "best": best,
        "best_check": best_check,
        "best_time": best_time,
        "events": events,
    }
