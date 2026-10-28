"""Profile attempt allowances, copied into immutable new assignments."""


def validate(rounds, mode):
    if type(rounds) is not int or not 1 <= rounds <= 1000:
        raise ValueError("Batch rounds must be a whole number from 1 to 1000.")
    if mode not in ("shared", "per_member"):
        raise ValueError("Choose a shared team or per-member allowance.")


def allocation(profile):
    rounds, mode = profile.get("rounds", 20), profile.get("budget_mode", "shared")
    validate(rounds, mode)
    size = profile.get("group_size", 1)
    total = rounds * size if mode == "per_member" else rounds
    return {
        "max_batch_rounds": total,
        "max_plain_checks": total * 8,
        "max_compiler_invocations": total * 8 * 4,
        "extend_while_improving": profile.get("extend_while_improving", True),
        "improvement_window": profile.get("improvement_window", 3),
        "budget_mode": mode,
        "rounds_per_member": rounds if mode == "per_member" else None,
    }


def for_run(root):
    from .state import read

    profile = read(root / "native-app-profile.json")
    profile.update(read(root / "operator-control.json").get("native_app_selection", {}))
    return allocation(profile)


def effective_limits(claim, assignment=None):
    """Derive admission ceilings from immutable settings and real checker receipts.

    Only newly configured assignments opt in. Durable batch ordinals allow this
    calculation during the last candidate of a still-running batch as well.
    """
    from pathlib import Path

    from .progress import checked_progress
    from .state import read

    claim = Path(claim)
    assignment = (
        assignment if assignment is not None else read(claim / "assignment.json")
    )
    keys = ("max_batch_rounds", "max_plain_checks", "max_compiler_invocations")
    base = dict(
        zip(
            keys,
            (
                assignment.get(keys[0], 20),
                assignment.get(keys[1], 160),
                assignment.get(keys[2], 640),
            ),
        )
    )
    if not assignment.get("extend_while_improving", False):
        return base
    window = assignment.get("improvement_window", 3)
    if type(window) is not int or not 1 <= window <= 1000:
        raise ValueError("Improvement window must be a whole number from 1 to 1000.")
    rounds = [read(p) for p in (claim / "work/batch-rounds").glob("*.json")]
    rounds.sort(key=lambda row: row.get("round_number", 0))
    events = checked_progress(claim, assignment=assignment)["events"]
    improved = {Path(e["manifest"]).name for e in events if e.get("manifest")}
    member_limits = {}
    own_counts = {}
    last = 0
    for batch in rounds:
        ordinal = batch.get("round_number")
        if type(ordinal) is not int or ordinal < 1:
            # No new-generation ordinal means no evidence for an extension.
            continue
        member = batch.get("batch_id", "").split("-", 2)
        member = (
            "-".join(member[:2]) if len(member) > 2 and member[0] == "peer" else None
        )
        if member:
            own_counts[member] = own_counts.get(member, 0) + 1
        receipts = [
            r["manifest"] for r in batch.get("results", []) if not r.get("reused")
        ]
        if batch.get("pending"):
            receipts.append(batch["pending"]["record"])
        if not any(Path(path).name in improved for path in receipts):
            continue
        last = max(last, ordinal)
        if member:
            member_limits[member] = max(
                member_limits.get(member, 0), own_counts[member] + window
            )
    if assignment.get("budget_mode") == "per_member":
        own_base = assignment["rounds_per_member"]
        total = base["max_batch_rounds"] + sum(
            max(0, value - own_base) for value in member_limits.values()
        )
    else:
        total = (
            max(base["max_batch_rounds"], last + window)
            if last
            else base["max_batch_rounds"]
        )
    extra = total - base["max_batch_rounds"]
    return {
        "max_batch_rounds": total,
        "max_plain_checks": base["max_plain_checks"] + extra * 8,
        "max_compiler_invocations": base["max_compiler_invocations"]
        + extra * 8 * assignment.get("official_internal_retry_bound", 4),
        "member_round_limits": {
            key: max(assignment.get("rounds_per_member", 0), value)
            for key, value in member_limits.items()
        },
    }


def member_round_limit(claim, assignment, member):
    return (
        effective_limits(claim, assignment)
        .get("member_round_limits", {})
        .get(member, assignment["rounds_per_member"])
    )
