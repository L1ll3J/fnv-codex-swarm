"""One lease and checker budget, several fresh generalists, deterministic supervision."""

import datetime as dt
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .board import Board
from .budgets import member_round_limit
from .group_check import digest, locked
from .state import atomic, read, process_alive
from .shared_status import status as shared_status


def append(path, value):
    with path.open("a") as output:
        output.write(json.dumps(value) + "\n")
        output.flush()
        os.fsync(output.fileno())


def close_exhausted_checking(claim, deadline):
    """Recheck the final batch and its improvement under the checker lock."""
    directory = claim / "collaboration"
    with locked(directory / "checker.lock"):
        current = shared_status(claim)
        if current["rounds_remaining"] > 0 or any(
            read(path).get("state") == "running"
            for path in (claim / "work/batch-rounds").glob("*.json")
        ):
            return None
        atomic(
            directory / "sealed.json",
            {"at": dt.datetime.now(dt.timezone.utc).isoformat()},
        )
        until = min(deadline, time.monotonic() + 45)
        atomic(
            directory / "handoff-phase.json",
            {
                "checking_closed": True,
                "seconds_allowed": max(0, until - time.monotonic()),
                "best": current["best"],
            },
        )
        return until


def score(path):
    row = read(path)
    check = row.get("checker_result", {})
    if row.get("checker_exit_code") == 0 and check.get("status") == "match":
        return (0, 0)
    if check.get("status") == "wip" and type(check.get("ndiff_insn")) is int:
        return (1, check["ndiff_insn"])
    return (2, 0)


def group_terminal(claim):
    claim = Path(claim)
    from .commands import operator_terminal

    if operator_terminal(claim):
        return True
    directory = claim / "collaboration"
    if not directory.exists():
        return True
    state = read(directory / "group.json")
    return (
        state.get("state") == "sealed"
        and state.get("streams_closed") is True
        and bool(state.get("members"))
        and all(
            member.get("exit_code") is not None
            and not process_alive(member.get("pid"), claim.parent.parent)
            for member in state["members"]
        )
    )


def due_expired_claims(root, *args, **kwargs):
    from expiry_cleanup import due_expired_claims as original

    if (Path(root) / "approval-blocker-current.json").exists():
        return []
    return [
        row
        for row in original(root, *args, **kwargs)
        if group_terminal(Path(root) / "claims" / row["claim_id"])
        and not group_rejected(Path(root) / "claims" / row["claim_id"])
    ]


def rejection_event(event):
    """Use the same event normalization as the pinned native scheduler."""
    kind = str(event.get("type") or event.get("event") or "").lower().replace(".", "_")
    code = str(event.get("code") or event.get("error_code") or "").lower()
    status = str(event.get("status") or "").lower()
    return any(
        tag in value
        for value in (kind, code, status)
        for tag in ("approval_reject", "approval_denied", "permission_denied")
    )


def member_rejection(workspace):
    for name in ("result.json", "nomination.json"):
        report = read(Path(workspace) / name)
        if report.get("approval_rejection") or report.get("permission_denied"):
            return {"report_path": str(Path(workspace) / name), "report": report}
    return None


def group_rejected(claim):
    claim = Path(claim)
    state = read(claim / "collaboration/group.json")
    return bool(
        state.get("approval_rejection")
        or (claim / "collaboration/approval-rejection.json").exists()
        or any(member_rejection(m["workspace"]) for m in state.get("members", []))
    )


def retain_rejection(root, claim_id, member, evidence):
    payload = {
        "claim_id": claim_id,
        "member": member,
        "evidence": evidence,
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "state": "approval_rejected_operator_review_required",
    }
    atomic(
        Path(root) / "claims" / claim_id / "collaboration/approval-rejection.json",
        payload,
    )
    current = Path(root) / "approval-blocker-current.json"
    if not current.exists():
        atomic(current, payload)
    try:
        with (Path(root) / "STOP_NEW_CLAIMS").open("x") as stop:
            stop.write(
                "Approval rejection in collaboration group "
                + claim_id
                + "; operator review required.\n"
            )
            stop.flush()
            os.fsync(stop.fileno())
    except FileExistsError:
        pass


def stop_peers(peers):
    shutdown = {}
    for proc in peers:
        prior = proc.poll()
        shutdown[proc.pid] = {
            "exit_before_shutdown": prior,
            "termination_requested": False,
        }
        if prior is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                shutdown[proc.pid]["termination_requested"] = True
            except ProcessLookupError:
                shutdown[proc.pid]["exit_before_shutdown"] = proc.poll()
    deadline = time.monotonic() + 10
    for proc in peers:
        try:
            proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    return shutdown


def capacity_events(members):
    """Recognize only complete, zero-work capacity failures from every peer."""
    retained = []
    for member in members:
        try:
            events = [
                json.loads(line)
                for line in (Path(member["workspace"]) / "stdout.jsonl")
                .read_text()
                .splitlines()
            ]
        except (OSError, ValueError):
            return []
        if (
            member.get("exit_code") != 1
            or not events
            or any(
                e.get("type")
                not in ("thread.started", "turn.started", "error", "turn.failed")
                for e in events
            )
        ):
            return []
        if not any(
            "429 Too Many Requests" in e.get("message", "")
            or "Selected model is at capacity" in e.get("message", "")
            for e in events
        ):
            return []
        retained.extend(e for e in events if e.get("type") in ("error", "turn.failed"))
    return retained


def ordinary_peer_failure(member):
    return member.get("exit_before_shutdown") not in (None, 0) or (
        member.get("exit_code") != 0 and not member.get("termination_requested")
    )


def run(root, claim_id):
    root = Path(root).resolve()
    sys.path.insert(0, str(root))
    claim = root / "claims" / claim_id
    assignment_path = claim / "assignment.json"
    assignment = read(assignment_path)
    profile = read(root / "native-app-profile.json")
    isolated = profile.get("team_mode", "collaborative") == "isolated"
    directory = claim / "collaboration"
    directory.mkdir(exist_ok=True)
    # No supervisor relaunch on an ambiguous process-start window.
    intent = directory / "launch-intent.json"
    with intent.open("x") as output:
        json.dump(
            {"at": dt.datetime.now(dt.timezone.utc).isoformat(), "pid": os.getpid()},
            output,
        )
    board = Board(directory / "board.sqlite3")
    manifest = {
        "claim_id": claim_id,
        "canonical_assignment_sha256": digest(assignment_path),
        "model": assignment["model"],
        "effort": assignment["reasoning_effort"],
        "members": [],
        "isolated": isolated,
        "state": "running",
    }
    atomic(directory / "group.json", manifest)
    signal_stop = threading.Event()
    rejected = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: signal_stop.set())
    peers, readers, files = [], [], []
    member_readers = {}
    primary_lock = threading.Lock()
    primary = []

    def pump(proc, member, out):
        try:
            for line in iter(proc.stdout.readline, b""):
                out.write(line)
                out.flush()
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") in ("error", "turn.failed"):
                    member["stream_error"] = True
                if event.get("type") == "turn.completed":
                    member["turn_completed"] = True
                if rejection_event(event):
                    rejected.set()
                    retain_rejection(root, claim_id, member["id"], event)
                    print(json.dumps(event), flush=True)
                if event.get("type") == "thread.started":
                    tid = event.get("thread_id") or event.get("thread", {}).get("id")
                    if tid:
                        previous_thread = read(
                            Path(member["workspace"]) / "thread.json"
                        ).get("thread_id")
                        if previous_thread and tid != previous_thread:
                            member["stream_error"] = True
                        atomic(
                            Path(member["workspace"]) / "thread.json",
                            {"thread_id": tid},
                        )
                        receipt = {
                            "thread_id": tid,
                            "worker": assignment["worker"] + "-" + member["id"],
                            "claim_id": claim_id,
                            "model_expected": assignment["model"],
                            "source": "codex_exec_direct",
                            "received_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        }
                        with primary_lock:
                            append(
                                root / "rolling-executor/direct-cli-threads.jsonl",
                                receipt,
                            )
                            if not primary:
                                primary.append(tid)
                                print(json.dumps(event), flush=True)
            out.flush()
            os.fsync(out.fileno())
        finally:
            out.close()

    deadline = time.monotonic() + assignment.get("timeout_minutes", 20) * 60

    def launch_peer(member, prompt, thread=None, index=None):
        from .session import command as peer_command

        workspace = Path(member["workspace"])
        member["turn_completed"] = False
        member.setdefault("turns", []).append(
            {"launch_intent_at": time.time(), "thread_id": thread}
        )
        atomic(directory / "group.json", manifest)
        err = (workspace / "stderr.log").open("ab")
        files.append(err)
        argv = peer_command(root, assignment, thread)
        if isolated:
            argv[argv.index("-C") + 1] = str(workspace)
        proc = subprocess.Popen(
            argv,
            cwd=workspace if isolated else root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=err,
            start_new_session=True,
        )
        # Popen duplicated stderr into the child; the supervisor needs no copy.
        err.close()
        if index is None:
            peers.append(proc)
        else:
            peers[index] = proc
        member["pid"] = proc.pid
        atomic(directory / "group.json", manifest)
        out = (workspace / "stdout.jsonl").open("ab")
        files.append(out)
        reader = threading.Thread(target=pump, args=(proc, member, out), daemon=True)
        reader.start()
        readers.append(reader)
        member_readers[member["id"]] = reader
        proc.stdin.write(prompt)
        proc.stdin.close()

    try:
        for index in range(profile["group_size"]):
            member_id = f"peer-{index + 1}"
            workspace = directory / member_id
            workspace.mkdir()
            member_path = workspace / "assignment.json"
            data = dict(assignment)
            data["work_dir"] = str(workspace)
            data["collaboration"] = {
                "group": claim_id,
                "member": member_id,
                "workspace": str(workspace),
                "shared_paths": [str(directory), str(claim / "work")],
                "board": str(board.path),
                "canonical_assignment": str(assignment_path),
                "board_command": f"{sys.executable} -m native_swarm.board --assignment {member_path}",
                "shared_checker": f"{sys.executable} -m native_swarm.group_check --assignment {member_path}",
            }
            if isolated:
                data["collaboration"]["isolated"] = True
                data["collaboration"].pop("shared_paths", None)
                data["collaboration"].pop("board", None)
                data["collaboration"].pop("board_command", None)
            data["checker_workflow"] = dict(assignment["checker_workflow"])
            command = data["collaboration"]["shared_checker"]
            from . import group_check

            adapter = Path(group_check.__file__).resolve()
            data["checker_workflow"].update(
                driver_path=str(adapter),
                driver_sha256=digest(adapter),
                check_command=command
                + " --batch-id UNIQUE_BATCH_ID CANDIDATE_CPP [OTHER_CANDIDATE_CPP ...]",
                finish_command=command
                + ' --finish CHECK_MANIFEST_JSON --residual "concise residual issue"',
            )
            atomic(member_path, data)
            visible = dict(data)
            if isolated:
                visible.pop("collaboration", None)
                visible["member_budget"] = {
                    "member": member_id,
                    "budget_mode": assignment.get("budget_mode", "shared"),
                    "members": profile["group_size"],
                }
            prompt = (
                (root / "worker-contract.xml").read_bytes()
                + b"\nASSIGNMENT JSON\n"
                + json.dumps(visible).encode()
            )
            prompt_path = workspace / "cli-prompt.txt"
            prompt_path.write_bytes(prompt)
            member = {
                "id": member_id,
                "workspace": str(workspace),
                "prompt_sha256": digest(prompt_path),
                "assignment_sha256": digest(member_path),
            }
            manifest["members"].append(member)
            # Persist the intent before Popen; a crash never permits blind replay.
            atomic(directory / "group.json", manifest)
            launch_peer(member, prompt)
        handoff_until = None
        while True:
            for member in manifest["members"]:
                evidence = member_rejection(member["workspace"])
                if evidence:
                    rejected.set()
                    retain_rejection(root, claim_id, member["id"], evidence)
            records = list((claim / "work/recorded-checks").glob("check-*.json"))
            matched = any(score(path)[0] == 0 for path in records)
            exhausted = len(
                list((claim / "work/batch-rounds").glob("*.json"))
            ) >= shared_status(claim)["rounds_limit"] and not any(
                read(path).get("state") == "running"
                for path in (claim / "work/batch-rounds").glob("*.json")
            )
            if (
                exhausted
                and not matched
                and not rejected.is_set()
                and not signal_stop.is_set()
                and handoff_until is None
            ):
                handoff_until = close_exhausted_checking(claim, deadline)
                exhausted = handoff_until is not None
            # Resume ordinary early completions in their existing conversations.
            if (
                not exhausted
                and not matched
                and not rejected.is_set()
                and not signal_stop.is_set()
                and time.monotonic() < deadline
            ):
                from .policy import decision
                from .session import FOLLOWUP

                try:
                    policy = decision(root)
                    may_continue = not (
                        policy["reached"] and policy["mode"] == "stop_immediately"
                    )
                except (OSError, ValueError, KeyError, TypeError):
                    may_continue = False
                for index, (member, proc) in enumerate(
                    zip(manifest["members"], list(peers))
                ):
                    used = len(
                        list(
                            (claim / "work/batch-rounds").glob(member["id"] + "-*.json")
                        )
                    )
                    own_exhausted = assignment.get(
                        "budget_mode"
                    ) == "per_member" and used >= member_round_limit(
                        claim, assignment, member["id"]
                    )
                    if (
                        proc.poll() == 0
                        and not member.get("stream_error")
                        and member.get("turn_completed")
                        and not own_exhausted
                        and may_continue
                    ):
                        proc.wait()
                        # The old stream must be closed before reading its actual thread ID.
                        member_readers[member["id"]].join(timeout=1)
                        thread = read(Path(member["workspace"]) / "thread.json").get(
                            "thread_id"
                        )
                        if (
                            thread
                            and not member_readers[member["id"]].is_alive()
                            and not member_rejection(member["workspace"])
                        ):
                            archive = (
                                Path(member["workspace"])
                                / "early-results"
                                / str(len(member["turns"]))
                            )
                            archive.mkdir(parents=True, exist_ok=False)
                            for name in ("result.json", "nomination.json"):
                                path = Path(member["workspace"]) / name
                                if path.exists():
                                    path.replace(archive / name)
                            # Re-read under the shared checker lock: a peer may
                            # have consumed the last round or matched meanwhile.
                            with locked(directory / "checker.lock"):
                                records_now = list(
                                    (claim / "work/recorded-checks").glob(
                                        "check-*.json"
                                    )
                                )
                                if len(
                                    list(
                                        (claim / "work/recorded-checks").glob(
                                            "candidate-*.cpp"
                                        )
                                    )
                                ) > len(records_now):
                                    member["stream_error"] = True
                                    continue
                                if shared_status(claim)["rounds_remaining"] <= 0 or any(
                                    score(p)[0] == 0 for p in records_now
                                ):
                                    continue
                                launch_peer(member, FOLLOWUP.encode(), thread, index)
            handoffs_complete = all(
                (Path(m["workspace"]) / "nomination.json").exists()
                for m in manifest["members"]
            )
            if (
                matched
                or (
                    exhausted
                    and (
                        handoffs_complete
                        or (
                            handoff_until is not None
                            and time.monotonic() >= handoff_until
                        )
                    )
                )
                or rejected.is_set()
                or signal_stop.is_set()
                or time.monotonic() >= deadline
                or all(p.poll() is not None for p in peers)
                and (
                    exhausted
                    or any(p.returncode != 0 for p in peers)
                    or any(
                        m.get("stream_error") or not m.get("turn_completed")
                        for m in manifest["members"]
                    )
                )
            ):
                break
            time.sleep(0.5)
    finally:
        # Shared checker lock proves no group compiler is in flight before peers
        # are stopped and the canonical result becomes visible to the resolver.
        with locked(directory / "checker.lock"):
            atomic(
                directory / "sealed.json",
                {"at": dt.datetime.now(dt.timezone.utc).isoformat()},
            )
            shutdown = stop_peers(peers)
        for reader in readers:
            reader.join(timeout=15)
        for member in manifest["members"]:
            evidence = member_rejection(member["workspace"])
            if evidence:
                rejected.set()
                retain_rejection(root, claim_id, member["id"], evidence)
        manifest["members"] = [
            {
                **member,
                "exit_code": peer.poll(),
                **shutdown[peer.pid],
                **read(Path(member["workspace"]) / "thread.json"),
            }
            for member, peer in zip(manifest["members"], peers)
        ]
        manifest.update(
            state="sealed",
            approval_rejection=rejected.is_set(),
            streams_closed=all(not t.is_alive() for t in readers),
        )
        atomic(directory / "group.json", manifest)
        for file in files:
            file.close()
    if signal_stop.is_set():
        manifest["ordinary_failure"] = True
        atomic(directory / "group.json", manifest)
        return 2
    if rejected.is_set() or not manifest["streams_closed"]:
        return 2
    # A nonzero peer exit before deliberate termination is retained as a blocker;
    # never synthesize a replacement session or a fictitious successful result.
    records = list((claim / "work/recorded-checks").glob("check-*.json"))
    if not records:
        events = capacity_events(manifest["members"])
        if events:
            manifest["capacity_retryable"] = True
            atomic(directory / "group.json", manifest)
            for event in events:
                print(json.dumps(event), flush=True)
            return 1
        return 2
    if any(
        ordinary_peer_failure(member)
        or member.get("stream_error")
        or (
            not member.get("turn_completed") and not member.get("termination_requested")
        )
        for member in manifest["members"]
    ):
        manifest["ordinary_failure"] = True
        atomic(directory / "group.json", manifest)
        return 2
    selected = min(records, key=score)
    residual = "\n".join(
        read(Path(m["workspace"]) / "nomination.json").get("residual", "")
        for m in manifest["members"]
    ).strip()
    driver = Path(assignment["checker_workflow"]["single_check_driver"])
    if digest(driver) != assignment["checker_workflow"]["single_check_driver_sha256"]:
        raise ValueError("Underlying recorder changed; canonical result withheld.")
    result = subprocess.run(
        [
            sys.executable,
            str(driver),
            "--assignment",
            str(assignment_path),
            "--finish",
            str(selected),
            "--residual",
            residual or "Group completed; shared checked evidence and board preserved.",
        ],
        cwd=root,
    )
    return result.returncode


def audit_group(claim):
    """Additional provenance gate; the original evidence audit still follows."""
    claim = Path(claim)
    directory = claim / "collaboration"
    if not directory.exists():
        return True
    state = read(directory / "group.json")
    if (
        state.get("state") != "sealed"
        or group_rejected(claim)
        or state.get("ordinary_failure")
        or not state.get("streams_closed")
    ):
        raise ValueError("Group is not safely sealed.")
    if digest(claim / "assignment.json") != state["canonical_assignment_sha256"]:
        raise ValueError("Canonical group assignment changed.")
    board = Board(directory / "board.sqlite3")
    attributed = set()

    def resolve_receipt(value):
        path = Path(value)
        return str((path if path.is_absolute() else claim / path).resolve())

    with board.connect() as database:
        for (body,) in database.execute("SELECT body FROM entries WHERE kind='result'"):
            result = json.loads(body)
            attributed.update(
                resolve_receipt(path)
                for path in result.get("reused_receipts", {}).values()
            )
            attributed.update(
                resolve_receipt(row["manifest"])
                for row in result.get("batch", {}).get("results", [])
                if not row.get("reused")
            )
    actual = {
        str(path.resolve())
        for path in (claim / "work/recorded-checks").glob("check-*.json")
    }
    if state.get("isolated"):
        for path in (directory / "private-receipts").glob("*.json"):
            value = read(path)
            attributed.update(
                resolve_receipt(p) for p in value.get("reused_receipts", {}).values()
            )
            attributed.update(
                resolve_receipt(row["manifest"])
                for row in value.get("batch", {}).get("results", [])
                if not row.get("reused")
            )
    if actual != attributed:
        raise ValueError("Group check receipts are missing shared-board provenance.")
    usage = {
        row["id"]: row
        for row in read(claim.parent.parent / "usage-current.json").get("threads", [])
    }
    for member in state["members"]:
        workspace = Path(member["workspace"])
        if (
            not workspace.resolve().is_relative_to(directory.resolve())
            or member.get("exit_code") is None
            or ordinary_peer_failure(member)
            or process_alive(member.get("pid"), claim.parent.parent)
        ):
            raise ValueError(
                "Group peer is not terminal or its workspace escaped the group."
            )
        if (
            digest(workspace / "cli-prompt.txt") != member["prompt_sha256"]
            or digest(workspace / "assignment.json") != member["assignment_sha256"]
        ):
            raise ValueError("Group prompt or assignment changed.")
        observed = usage.get(member.get("thread_id"))
        if not observed or observed.get("usage") is None:
            return False
        if observed.get("model") != state["model"]:
            raise ValueError("Actual peer model differs from the group assignment.")
    return True


if __name__ == "__main__":
    # The parent sends its canonical launch envelope through stdin. Each actual
    # model receives its own immutable prompt above, exposed in the UI.
    sys.stdin.buffer.read()
    raise SystemExit(run(sys.argv[1], sys.argv[2]))
