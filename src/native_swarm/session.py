"""Same-thread continuation beneath the native scheduler's original lease/budget."""

import datetime as dt
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .executables import run_codex
from .group import member_rejection, rejection_event, score, stop_peers
from .group_check import digest
from .shared_status import status
from .state import atomic, read

FOLLOWUP = "keep going o7 let's get this solved"


def resolution_ready(claim):
    path = Path(claim) / "session/state.json"
    if not path.exists():
        # Managed independent claims are held even during supervisor startup.
        profile = read(Path(claim).parent.parent / "native-app-profile.json")
        return profile.get("group_size", 1) != 1 or not profile
    state = read(path)
    return state.get("state") == "complete" and state.get("streams_closed") is True


def due_expired_claims(root, *args, **kwargs):
    from expiry_cleanup import due_expired_claims as original

    return [
        r
        for r in original(root, *args, **kwargs)
        if resolution_ready(Path(root) / "claims" / r["claim_id"])
    ]


def remaining(claim, assignment):
    value = status(claim)
    return (
        value["rounds_remaining"] > 0
        and value["checks"] < assignment.get("max_plain_checks", 160)
        and value["compiler_invocations_upper_bound"] + assignment.get("official_internal_retry_bound", 4)
        <= assignment.get("max_compiler_invocations", 640)
    )


def archive_results(claim, directory):
    directory.mkdir(parents=True, exist_ok=False)
    for index, path in enumerate((claim / "result.json", claim / "work/result.json")):
        if path.exists():
            path.replace(directory / f"result-{index}.json")


def command(root, assignment, thread=None):
    argv = [
        str(run_codex(root)),
        "exec",
        "--ignore-user-config",
        "--approve-for-me",
        "--skip-git-repo-check",
        "--model",
        assignment["model"],
        "-c",
        "model_reasoning_effort=" + assignment["reasoning_effort"],
        "-c",
        "agents.enabled=false",
        "-C",
        str(root),
        "--json",
    ]
    return argv + (["resume", thread, "-"] if thread else ["-"])


def run_turn(root, claim, assignment, directory, state, turn, prompt, stop, deadline):
    events, ids, stream_errors = [], [], []
    proc = None
    reader = None
    with (
        (directory / "stderr.log").open("ab") as err,
        (directory / f"turn-{turn['number']:03d}.jsonl").open("ab") as out,
    ):
        try:
            proc = subprocess.Popen(
                command(root, assignment, state["thread_id"]),
                cwd=root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=err,
                start_new_session=True,
            )
            turn["pid"] = proc.pid
            atomic(directory / "state.json", state)

            def pump():
                try:
                    for line in iter(proc.stdout.readline, b""):
                        out.write(line)
                        out.flush()
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        events.append(event)
                        if event.get("type") == "thread.started":
                            tid = event.get("thread_id") or event.get("thread", {}).get(
                                "id"
                            )
                            if tid:
                                ids.append(tid)
                        print(line.decode(errors="replace"), end="", flush=True)
                except Exception as exc:
                    stream_errors.append(str(exc))

            reader = threading.Thread(target=pump, daemon=True)
            reader.start()
            proc.stdin.write(prompt if not state["thread_id"] else FOLLOWUP.encode())
            proc.stdin.close()
            while proc.poll() is None and not stop.is_set() and time.time() < deadline:
                stop.wait(0.25)
        finally:
            if proc is not None:
                if proc.poll() is None:
                    stop_peers([proc])
                proc.wait()
            if reader:
                reader.join(timeout=15)
            state["streams_closed"] = reader is not None and not reader.is_alive()
            turn.update(
                exit_code=proc.returncode if proc else None,
                thread_ids=ids,
                finished_at=time.time(),
            )
            atomic(directory / "state.json", state)
        rejected = any(rejection_event(e) for e in events) or member_rejection(
            claim / "work"
        )
        errors = any(e.get("type") in ("error", "turn.failed") for e in events)
        normal = (
            state["streams_closed"]
            and not stream_errors
            and not rejected
            and not errors
            and proc.returncode == 0
            and any(e.get("type") == "turn.completed" for e in events)
            and len(set(ids)) == 1
        )
        capacity = (
            state["streams_closed"]
            and not rejected
            and not stream_errors
            and proc.returncode == 1
            and not list((claim / "work/recorded-checks").glob("check-*.json"))
            and bool(events)
            and all(
                e.get("type")
                in ("thread.started", "turn.started", "error", "turn.failed")
                for e in events
            )
            and any(
                "429 Too Many Requests" in e.get("message", "")
                or "Selected model is at capacity" in e.get("message", "")
                for e in events
            )
        )
        return ids, proc.returncode, normal, capacity


def run(root, claim_id, prompt):
    root = Path(root).resolve()
    claim = root / "claims" / claim_id
    assignment = read(claim / "assignment.json")
    directory = claim / "session"
    directory.mkdir(exist_ok=True)
    # Exclusive creation prevents replay after an ambiguous supervisor launch.
    with (directory / "launch-intent.json").open("x") as output:
        json.dump({"pid": os.getpid(), "at": time.time()}, output)
        output.flush()
        os.fsync(output.fileno())
    state = {
        "state": "running",
        "turns": [],
        "thread_id": None,
        "streams_closed": False,
    }
    atomic(directory / "state.json", state)
    stop = threading.Event()
    previous_handlers = {
        sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    for sig in previous_handlers:
        signal.signal(sig, lambda *_: stop.set())
    deadline = time.time() + assignment.get("timeout_minutes", 20) * 60
    expires = read(claim / "claim.json").get("expires") or read(
        claim / "claim.json"
    ).get("expires_at")
    if expires:
        deadline = min(
            deadline,
            dt.datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp() - 305,
        )
    state["deadline"] = deadline
    try:
        while time.time() < deadline and not stop.is_set():
            if state["turns"]:
                from .policy import decision

                try:
                    policy = decision(root)
                except (OSError, ValueError, KeyError, TypeError):
                    stop.wait(1)
                    continue
                if policy["reached"]:
                    if policy["mode"] == "stop_immediately":
                        raise RuntimeError("Usage policy stopped active work")
                    # Stop-queuing/taper do not interrupt an admitted assignment.
                if (root / "approval-blocker-current.json").exists():
                    raise RuntimeError("Approval blocker retained")
            turn = {"number": len(state["turns"]) + 1, "launch_intent_at": time.time()}
            state["turns"].append(turn)
            state["streams_closed"] = False
            atomic(directory / "state.json", state)
            ids, exit_code, normal, capacity = run_turn(
                root, claim, assignment, directory, state, turn, prompt, stop, deadline
            )
            if capacity and len(state["turns"]) == 1:
                state.update(state="review_required", capacity_retryable=True)
                return 1
            if not normal or (state["thread_id"] and ids[0] != state["thread_id"]):
                raise RuntimeError(
                    "Worker did not complete an ordinary successful turn; review retained"
                )
            state["thread_id"] = ids[0]
            atomic(directory / "state.json", state)
            snapshots = list((claim / "work/recorded-checks").glob("candidate-*.cpp"))
            receipts = list((claim / "work/recorded-checks").glob("check-*.json"))
            if len(snapshots) > len(receipts):
                raise RuntimeError(
                    "Uncertain compiler dispatch retained; no continuation"
                )
            if stop.is_set():
                raise RuntimeError("Supervisor stopped; evidence retained")
            records = list((claim / "work/recorded-checks").glob("check-*.json"))
            if any(score(p)[0] == 0 for p in records) or not remaining(
                claim, assignment
            ):
                break
            archive_results(claim, directory / f"early-{turn['number']:03d}")
            # Avoid a tight loop if a model immediately returns without work.
            stop.wait(min(1, max(0, deadline - time.time())))
        if stop.is_set():
            raise RuntimeError("Supervisor stopped; evidence retained")
        records = list((claim / "work/recorded-checks").glob("check-*.json"))
        if not records:
            raise RuntimeError("No checked evidence to finish")
        archive_results(claim, directory / "final-authored")
        driver = Path(assignment["checker_workflow"]["single_check_driver"])
        if (
            digest(driver)
            != assignment["checker_workflow"]["single_check_driver_sha256"]
        ):
            raise ValueError("Recorder changed; result withheld")
        # --finish only records a result from a real receipt; no compilation.
        done = subprocess.run(
            [
                sys.executable,
                str(driver),
                "--assignment",
                str(claim / "assignment.json"),
                "--finish",
                str(min(records, key=score)),
                "--residual",
                "Session budget/deadline closed; checked evidence and authored handoffs preserved.",
            ],
            cwd=root,
        )
        if done.returncode:
            raise RuntimeError("Original recorder could not finish")
        state["state"] = "complete"
        return 0
    except Exception as exc:
        state.update(state="review_required", error=str(exc))
        return 2
    finally:
        atomic(directory / "state.json", state)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1], sys.argv[2], sys.stdin.buffer.read()))
