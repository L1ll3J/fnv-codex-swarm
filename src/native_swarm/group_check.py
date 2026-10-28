"""Group adapter: the original recorded batch driver remains the checker authority."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
from pathlib import Path

from .board import Board
from .budgets import member_round_limit
from .state import atomic, read


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextmanager
def locked(path):
    with Path(path).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def module(path):
    spec = importlib.util.spec_from_file_location("original_recorded_batch", path)
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def check(member_path, batch_id, sources, invoke=None):
    member = read(member_path)
    group = member["collaboration"]
    canonical = Path(group["canonical_assignment"])
    assignment = read(canonical)
    assignment["assignment_path"] = str(canonical)
    claim = Path(assignment["claim_dir"])
    directory = claim / "collaboration"
    board = Board(group["board"])
    driver = Path(assignment["checker_workflow"]["driver_path"])
    if digest(driver) != assignment["checker_workflow"]["driver_sha256"]:
        raise ValueError("Original recorded batch driver changed.")
    for source in sources:
        if (
            not Path(source)
            .resolve()
            .is_relative_to(Path(member["work_dir"]).resolve())
        ):
            raise ValueError("Submit sources from your own member workspace.")
    if not 1 <= len(sources) <= 8:
        raise ValueError("A round requires 1–8 candidates.")
    with locked(directory / "checker.lock"):
        if (directory / "sealed.json").exists():
            from .shared_status import status

            return {"checking_closed": True, "status": status(claim), "batch": None}
        hashes = [digest(p) for p in sources]
        if len(set(hashes)) != len(hashes):
            raise ValueError("Duplicate source within round.")
        # A repeated completed experiment returns its actual receipt; it does not
        # consume another round or invent a fresh checker observation.
        recorded = {}
        for path in (claim / "work/recorded-checks").glob("check-*.json"):
            row = read(path)
            if (
                digest(row["source_path"]) == row["source_sha256"]
                and digest(row["log_path"]) == row["log_sha256"]
            ):
                recorded[row["source_sha256"]] = str(path)
        duplicates = {h: recorded[h] for h in hashes if h in recorded}
        pending = [(p, h) for p, h in zip(sources, hashes) if h not in duplicates]
        if pending and assignment.get("budget_mode") == "per_member":
            own_rounds = list(
                (claim / "work/batch-rounds").glob(group["member"] + "-*.json")
            )
            existing = (
                claim
                / "work/batch-rounds"
                / (group["member"] + "-" + batch_id + ".json")
            )
            if (
                len(own_rounds)
                >= member_round_limit(claim, assignment, group["member"])
                and not existing.exists()
            ):
                raise ValueError(
                    "Member round allowance exhausted; preserve handoff and discuss remaining team work."
                )
        for path, source_hash in pending:
            if not board.reserve(group["member"], source_hash):
                raise ValueError(
                    "Another member reserved this source; inspect the shared board."
                )
            board.post(
                group["member"],
                "reservation",
                {"source_sha256": source_hash, "path": str(path)},
            )
        result = {"reused_receipts": duplicates}
        if pending:
            result["batch"] = module(driver).run_batch(
                assignment,
                group["member"] + "-" + batch_id,
                [p for p, _ in pending],
                invoke=invoke,
            )
        # The complete diagnostics stay in native artifacts and the CLI reply.
        # Board provenance uses bounded, claim-relative receipt references.
        compact = {
            "reused_receipts": {
                source: str(Path(path).relative_to(claim))
                for source, path in duplicates.items()
            }
        }
        if "batch" in result:
            batch = result["batch"]
            compact["batch"] = {
                "batch_id": batch["batch_id"],
                "match": batch["match"],
                "results": [
                    {
                        "manifest": str(Path(row["manifest"]).relative_to(claim))
                        if not row.get("reused")
                        else row["manifest"],
                        "reused": row.get("reused", False),
                        "source_sha256": row["source_sha256"],
                        "match": row["match"],
                        "exit_code": row["exit_code"],
                        "ndiff_insn": row["checker"].get("ndiff_insn"),
                    }
                    for row in batch["results"]
                ],
            }
        board.post(group["member"], "result", compact)
        from .shared_status import status

        result["status"] = status(claim)
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assignment", type=Path, required=True)
    parser.add_argument("--batch-id")
    parser.add_argument("--finish", type=Path)
    parser.add_argument("--residual", default="")
    parser.add_argument("candidates", type=Path, nargs="*")
    args = parser.parse_args()
    member = read(args.assignment)
    group = member["collaboration"]
    if args.finish:
        claim = Path(read(group["canonical_assignment"])["claim_dir"])
        path = args.finish.resolve()
        if (
            path.parent != (claim / "work/recorded-checks").resolve()
            or not path.is_file()
        ):
            raise ValueError("Handoff must nominate an actual shared checker receipt.")
        atomic(
            Path(member["work_dir"]) / "nomination.json",
            {"manifest": str(path), "residual": args.residual},
        )
        Board(group["board"]).post(
            group["member"],
            "handoff",
            {"manifest": str(path), "residual": args.residual},
        )
        return
    print(json.dumps(check(args.assignment, args.batch_id, args.candidates), indent=2))


if __name__ == "__main__":
    main()
