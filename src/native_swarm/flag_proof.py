"""Operator-invoked flag proof; never a normal submission or autonomous flag."""

import json
from pathlib import Path
import subprocess
import sys
import uuid

from .group_check import locked
from .official_tools import context, digest, environment, pack_files
from .state import atomic, read


def service(run, tool, args):
    code = "import json,sys; from service import call; print(json.dumps(call(sys.argv[1],json.load(sys.stdin))))"
    result = subprocess.run(
        [sys.executable, "-c", code, tool],
        cwd=run,
        input=json.dumps(args),
        capture_output=True,
        text=True,
        timeout=90,
    )
    if result.returncode:
        raise ValueError(
            "FNV request failed; retained intent requires reconciliation before any retry"
        )
    return json.loads(result.stdout)


def prepare(report, source_path, flags, *, call=service, invoke=subprocess.run):
    claim = Path(report["evidence_path"]).resolve()
    run = claim.parent.parent
    assignment = read(claim / "assignment.json")
    selected = context(assignment)
    if not selected:
        raise ValueError(
            "This historical assignment has no versioned tools. Create proof from a current assignment."
        )
    pack = read(claim / "pack/pack.json")
    if (
        not isinstance(flags, list)
        or not flags
        or any(
            not isinstance(f, str)
            or not f.startswith("/")
            or any(c in f for c in "\r\n,")
            for f in flags
        )
    ):
        raise ValueError("Supply the complete comma-separated compiler flag set")
    if flags in [pack["flags"], *(pack.get("accept_also") or [])]:
        raise ValueError(
            "Those flags are already accepted by this pack; no compiler-flag report is needed"
        )
    source = Path(source_path).expanduser().resolve()
    if (
        not source.is_relative_to(claim)
        or not source.is_file()
        or not 0 < source.stat().st_size <= 65536
    ):
        raise ValueError("Choose an existing candidate source inside this claim")
    with locked(claim / "proof.lock"):
        # Never run a diagnostic proof concurrently with an active model/checker.
        session = read(claim / "session/state.json")
        group = read(claim / "collaboration/group.json")
        from .group import group_terminal

        if session.get("state") == "running" or (group and not group_terminal(claim)):
            raise ValueError(
                "Wait for this assignment's workers to finish before preparing operator proof"
            )
        s = call(run, "status", {})
        if s.get("strikes_last_7_days") or s.get("submissions_paused"):
            raise ValueError("Account health blocks proof publication")
        if not any(
            c.get("claim_id", c.get("id")) == claim.name and c.get("this_key")
            for c in s.get("open_claims", [])
        ):
            raise ValueError(
                "Proof needs the current owned claim; no claim was acquired"
            )
        # One recorded, budgeted diagnostic operation, separate from MATCH receipts.
        records = list((claim / "work/recorded-checks").glob("check-*.json"))
        snapshots = list((claim / "work/recorded-checks").glob("candidate-*.cpp"))
        if len(snapshots) != len(records):
            raise ValueError(
                "An unresolved checker dispatch needs reconciliation before proof"
            )
        if any(
            not (p.parent / "checkpoint-response.json").exists()
            for p in (claim / "flag-proofs").glob("*/checkpoint-intent.json")
        ):
            raise ValueError(
                "An uncertain proof checkpoint needs reconciliation; no automatic replay"
            )
        previous = [read(p) for p in (claim / "flag-proofs").glob("*/intent.json")]
        bound = selected["bound"]
        used = sum(
            read(p).get("compiler_invocations_upper_bound", bound) for p in records
        )
        used += sum(p["compiler_invocations_upper_bound"] for p in previous)
        if len(records) + len(previous) >= assignment["max_plain_checks"]:
            raise ValueError("The check allowance cannot cover another proof check")
        if used + bound > assignment["max_compiler_invocations"]:
            raise ValueError("The compiler allowance cannot cover another proof check")
        directory = claim / "flag-proofs" / uuid.uuid4().hex
        directory.mkdir(parents=True)
        snapshot = directory / "source.cpp"
        snapshot.write_bytes(source.read_bytes())
        snapshot.chmod(0o400)
        intent = {
            "claim_id": claim.name,
            "address": pack["address"],
            "source_sha256": digest(snapshot),
            "pack_sha256": digest(claim / "pack.tar.gz"),
            "compile_flags": flags,
            "official_tools_id": assignment["official_tools"]["id"],
            "compiler_invocations_upper_bound": bound,
            "state": "check_intent",
        }
        atomic(directory / "intent.json", intent)
        command = [
            "bash",
            "-lc",
            'source "$1"; shift; exec env "$@"',
            "proof-check",
            assignment.get("resource_paths", {}).get(
                "environment_script", str(Path.home() / "fnvdecomp/env.sh")
            ),
            *environment(selected, claim),
            "python",
            str(selected["checker"] / "fnvcheck.py"),
            "--pack",
            str(claim / "pack"),
            str(snapshot),
            "--flags",
            ",".join(flags),
        ]
        versions = {str(p): digest(p) for p in (selected["checker"]).glob("*.py")}
        versions.update(pack_files(claim))
        from .toolchains import files as compiler_files

        versions.update(compiler_files(selected.get("toolchains", {})))
        for path in [
            Path(selected["toolchains"]["sp1"]["root"]) / "VC/bin/cl.exe"
            if selected.get("toolchains", {}).get("sp1")
            else Path.home() / "fnvdecomp/tools/msvc9/VC/bin/cl.exe",
            Path(
                assignment.get("resource_paths", {}).get(
                    "game_image", str(Path.home() / "fnvdecomp/game/FalloutNV.exe")
                )
            ),
        ]:
            versions[str(path)] = digest(path)
        result = invoke(command, capture_output=True, text=True, timeout=210)
        (directory / "checker.log").write_text(result.stdout + result.stderr)
        expected = (
            "MATCH  (under your --flags, not the pack's %s: the site does not accept this yet. To report it, save this code with checkpoint and flag the function with compile_flags and checkpoint_id)"
            % " ".join(pack["flags"])
        )
        from .checker_result import RTM_SUFFIX, VC8_SUFFIX

        lines = result.stdout.splitlines()
        first = lines[0] if lines else ""
        rtm_expected = "MATCH" + RTM_SUFFIX + expected[len("MATCH") :]
        vc8_expected = "MATCH" + VC8_SUFFIX + expected[len("MATCH") :]
        allowed = (
            (vc8_expected,)
            if pack.get("compiler") == "vc8"
            else (expected, rtm_expected)
        )
        if result.returncode != 0 or first not in allowed:
            raise ValueError(
                "Alternate flags did not establish an official proof MATCH; diagnostics retained"
            )
        if (
            digest(snapshot) != intent["source_sha256"]
            or digest(claim / "pack.tar.gz") != intent["pack_sha256"]
            or any(digest(p) != h for p, h in versions.items())
        ):
            raise ValueError("Proof inputs changed; no checkpoint saved")
        request = {
            "claim_id": claim.name,
            "request_id": str(uuid.uuid4()),
            "source": snapshot.read_text(),
            "note": "Operator compiler-flag proof: exact local MATCH under "
            + ",".join(flags),
        }
        atomic(directory / "checkpoint-intent.json", request)
        response = call(run, "checkpoint", request)
        atomic(directory / "checkpoint-response.json", response)
        if not isinstance(response.get("checkpoint_id"), str):
            raise ValueError(
                "Checkpoint response lacks a checkpoint ID; preserve response for review"
            )
        proof = {
            **intent,
            "state": "checked_checkpoint_saved",
            "checkpoint_id": response["checkpoint_id"],
            "source_path": str(snapshot),
            "log_path": str(directory / "checker.log"),
            "log_sha256": digest(directory / "checker.log"),
            "checked_versions": versions,
            "path": str(directory / "proof.json"),
        }
        atomic(directory / "proof.json", proof)
        return proof


def verify(report, path):
    claim = Path(report["evidence_path"]).resolve()
    path = Path(path).resolve()
    if not path.is_relative_to(claim / "flag-proofs"):
        raise ValueError("Proof is outside this claim")
    proof = read(path)
    if (
        proof.get("state") != "checked_checkpoint_saved"
        or proof["claim_id"] != claim.name
        or proof["address"] != report["address"]
    ):
        raise ValueError("Proof does not bind this report and claim")
    if (
        digest(proof["source_path"]) != proof["source_sha256"]
        or digest(proof["log_path"]) != proof["log_sha256"]
        or digest(claim / "pack.tar.gz") != proof["pack_sha256"]
    ):
        raise ValueError("Proof evidence changed")
    assignment = read(claim / "assignment.json")
    if assignment.get("official_tools", {}).get("id") != proof["official_tools_id"]:
        raise ValueError("Proof belongs to another tool generation")
    context(assignment)
    pack_files(claim)
    if any(digest(p) != h for p, h in proof.get("checked_versions", {}).items()):
        raise ValueError("Proof checked components changed")
    if (
        read(path.parent / "checkpoint-response.json").get("checkpoint_id")
        != proof["checkpoint_id"]
    ):
        raise ValueError("Checkpoint receipt differs")
    return proof
