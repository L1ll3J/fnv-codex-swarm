"""Durable operator commands, separate from worker-produced results."""

import datetime as dt
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys

from .state import atomic, read

ACTIONS = ("stop_now", "finish_round", "handoff")
TERMINAL = ("completed", "cancelled", "review_required")


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def claim_path(state, value):
    path = Path(value).resolve()
    cfg = read(Path(state) / "config.json")
    runs = {Path(r).resolve() for r in cfg.get("managed_runs", [])}
    if path.parent.name != "claims" or path.parent.parent not in runs:
        raise ValueError("Select a claim managed by this application.")
    if not read(path / "claim.json"):
        raise ValueError("Claim identity is missing.")
    return path


def cancel_executor(state, record):
    """Supersede a command daemon through its exact, stable process identity."""
    import os
    import signal
    import select

    pid = read(Path(state) / "commands" / (record["id"] + ".launch.json")).get("pid")
    if not pid:
        return  # The claim lock and current command ID gate any late launch.
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if (
            b"native_swarm.command_worker" not in argv
            or record["id"].encode() not in argv
            or str(state).encode() not in argv
        ):
            # A recycled PID is not the command daemon and must not be signalled.
            return
        signal.pidfd_send_signal(fd, signal.SIGTERM)
        if not select.select([fd], [], [], 15)[0]:
            raise ValueError(
                "Previous command is still finalizing; inspect it before superseding."
            )
    finally:
        os.close(fd)


def issue(
    state,
    claims,
    action,
    *,
    replace=False,
    handoff="improved",
    timeout=45,
    resume_admissions=True,
):
    from .backend import Backend

    if action not in ACTIONS or handoff not in ("always", "improved", "never"):
        raise ValueError("Choose a valid worker action and handoff policy.")
    if type(timeout) is not int or not 1 <= timeout <= 600:
        raise ValueError("Handoff timeout must be 1–600 seconds.")
    state = Path(state)
    requests = []
    with Backend(state).locked():
        paths = list(dict.fromkeys(claim_path(state, c) for c in claims))
        if not paths:
            raise ValueError("Select at least one worker or open claim.")
        for claim in paths:
            if not read(claim.parent.parent / "native-app-profile.json").get("id"):
                raise ValueError("Managed profile identity is missing.")
            current = read(claim / "operator-command.json")
            if (
                current
                and current.get("state") not in TERMINAL
                and action != "stop_now"
            ):
                raise ValueError(
                    "A worker command is already in progress; Stop now can supersede it."
                )
            if action == "finish_round":
                driver = (
                    read(claim / "assignment.json")
                    .get("checker_workflow", {})
                    .get("driver_path")
                )
                if not driver or "block_new_batch" not in Path(driver).read_text():
                    raise ValueError(
                        "This older assignment lacks the round-boundary stop gate. Use Stop now or handoff; fresh assignments support Finish current round."
                    )
        reservations = read(state / "idle-reservations.json")
        for claim in paths:
            if read(claim / "giveup-response.json") or read(
                claim / "submission-response.json"
            ):
                continue
            current = read(claim / "operator-command.json")
            if current and current.get("state") not in TERMINAL:
                cancel_executor(state, current)
                update(
                    state,
                    current,
                    "cancelled",
                    reason="Superseded by explicit Stop now.",
                )
            identifier = secrets.token_hex(12)
            record = {
                "id": identifier,
                "claim": str(claim),
                "action": action,
                "replace": bool(replace),
                "handoff": handoff,
                "timeout": timeout,
                "resume_admissions": resume_admissions,
                "state": "requested",
                "requested_at": now(),
            }
            retire(claim.parent.parent, claim.name)
            profile = read(claim.parent.parent / "native-app-profile.json")["id"]
            if replace:
                reservations.pop(str(claim), None)
            else:
                reservations[str(claim)] = profile
                record["idle_slot_reserved"] = True
            from collections import Counter

            atomic(state / "idle-reservations.json", reservations)
            atomic(state / "idle-slots.json", dict(Counter(reservations.values())))
            atomic(claim / "operator-command.json", record)
            atomic(state / "commands" / (identifier + ".json"), record)
            requests.append(record)
        for record in requests:
            with (state / "command-center.log").open("ab") as log:
                try:
                    proc = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "native_swarm.command_worker",
                            str(state),
                            record["id"],
                        ],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    atomic(
                        state / "commands" / (record["id"] + ".launch.json"),
                        {"pid": proc.pid},
                    )
                except OSError as exc:
                    update(
                        state,
                        record,
                        "review_required",
                        reason="Command launch failed: " + str(exc),
                    )
    return requests


def update(state, record, phase, **details):
    if phase != "review_required" and "reason" not in details:
        record.pop("reason", None)
    record.update(state=phase, updated_at=now(), **details)
    atomic(Path(record["claim"]) / "operator-command.json", record)
    atomic(Path(state) / "commands" / (record["id"] + ".json"), record)


def block_new_batch(claim):
    command = read(Path(claim) / "operator-command.json")
    if command and command.get("state") != "cancelled":
        raise RuntimeError(
            "Operator stopped new candidate batches; preserve checked work."
        )


def block_new_check(claim):
    command = read(Path(claim) / "operator-command.json")
    if command.get("action") != "finish_round":
        block_new_batch(claim)


def operator_terminal(claim):
    """Verified operator termination is distinct from a successful model turn."""
    from .state import process_alive

    claim = Path(claim)
    marker = read(claim / "operator-stop-terminal.json")
    assignment = claim / "assignment.json"
    if (
        not marker.get("verified_stopped")
        or not assignment.is_file()
        or marker.get("assignment_sha256")
        != hashlib.sha256(assignment.read_bytes()).hexdigest()
    ):
        return False
    root = claim.parent.parent
    pids = list(marker.get("pids", []))
    pids.extend(
        t.get("pid") for t in read(claim / "session/state.json").get("turns", [])
    )
    pids.extend(
        m.get("pid")
        for m in read(claim / "collaboration/group.json").get("members", [])
    )
    return not any(process_alive(pid, root) for pid in pids)


def handoff_reserved(claim):
    from .state import process_alive

    claim = Path(claim)
    value = read(claim / "operator-handoff-process.json")
    return process_alive(value.get("pid"), claim.parent.parent) or bool(
        value.get("intent") and not value.get("pid") and not value.get("finished")
    )


def failure_rows(run):
    return {
        worker: job
        for worker, job in read(Path(run) / "rolling-executor/journal.json")
        .get("jobs", {})
        .items()
        if "review" in str(job.get("state", ""))
    }


def failure_digest(run):
    return hashlib.sha256(
        json.dumps(failure_rows(run), sort_keys=True).encode()
    ).hexdigest()


def operator_retiring(run):
    """A drain cannot conceal an unrelated unresolved job failure."""
    run = Path(run)
    if (run / "approval-blocker-current.json").exists():
        return False
    stop = (
        (run / "STOP_NEW_CLAIMS").read_text().strip()
        if (run / "STOP_NEW_CLAIMS").exists()
        else ""
    )
    marker = read(run / "operator-retiring.json")
    if marker.get("acknowledged_stop_sha256") == hashlib.sha256(
        stop.encode()
    ).hexdigest() and marker.get("acknowledged_failures_sha256") == failure_digest(run):
        return True
    failures = failure_rows(run)
    for job in failures.values():
        cid = job.get("claim_id")
        if not cid:
            return False
        claim = run / "claims" / cid
        closed = read(claim / "giveup-response.json") or read(
            claim / "submission-response.json"
        )
        stopped = (
            cid in marker.get("claims", [])
            and operator_terminal(claim)
            and read(claim / "operator-command.json").get("action") in ACTIONS
        )
        if not closed and not stopped:
            return False
    drain_stop = "Rolling executor stopped: Operator requested a safe drain; no new or replacement workers"
    if not marker:
        return (run / "operator-drain.json").exists() and stop in ("", drain_stop)
    if stop in ("", drain_stop):
        return True
    return (
        stop == "Rolling executor stopped: direct Codex process exited nonzero"
        and bool(failures)
    )


def retire(run, claim=None, *, acknowledge=False):
    run = Path(run)
    record = read(run / "operator-retiring.json")
    claims = record.get("claims", [])
    if claim and claim not in claims:
        claims.append(claim)
    record.update(claims=claims, requested_at=now())
    if acknowledge:
        record["acknowledged_failures_sha256"] = failure_digest(run)
    if acknowledge:
        stop = (
            (run / "STOP_NEW_CLAIMS").read_text().strip()
            if (run / "STOP_NEW_CLAIMS").exists()
            else ""
        )
        record["acknowledged_stop_sha256"] = hashlib.sha256(stop.encode()).hexdigest()
    atomic(run / "operator-retiring.json", record)
    atomic(run / "operator-drain.json", {"source": "operator-retirement", "at": now()})
    atomic(
        run / "operator-admissions-paused.json",
        {"source": "operator-retirement", "at": now()},
    )


def retry(state, value):
    """Explicit operator retry: archive the uncertain intent, then recheck ownership."""
    from .backend import Backend

    state = Path(state)
    claim = claim_path(state, value)
    with Backend(state).locked():
        record = read(claim / "operator-command.json")
        if not record or record.get("state") in ("completed", "cancelled"):
            raise ValueError("Select an unfinished command to reconcile.")
        cancel_executor(state, record)
        # The daemon holds this lock throughout dispatch. Never retry a live request.
        import fcntl

        with (claim / ".operator-command.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError(
                    "The command is still running; wait for it to finish."
                ) from exc
            pending = any(
                (claim / name).exists()
                for name in ("operator-release-intent.json", "operator-submit-intent.json")
            )
            if pending and not read(claim / "operator-resolution-process.json").get("quiesced"):
                raise ValueError(
                    "The previous resolution process is not confirmed quiescent; retain its intent and reconcile before retrying."
                )
            for name in ("operator-release-intent.json", "operator-submit-intent.json"):
                intent = claim / name
                if intent.exists():
                    archive = (
                        claim
                        / "operator-release-history"
                        / (
                            name
                            + "-"
                            + record["id"]
                            + "-"
                            + secrets.token_hex(6)
                            + ".json"
                        )
                    )
                    archive.parent.mkdir(parents=True, exist_ok=True)
                    intent.replace(archive)
            record["state"] = "requested"
            update(state, record, "requested", retry_authorized_at=now())
            with (state / "command-center.log").open("ab") as log:
                try:
                    proc = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "native_swarm.command_worker",
                            str(state),
                            record["id"],
                        ],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    atomic(
                        state / "commands" / (record["id"] + ".launch.json"),
                        {"pid": proc.pid},
                    )
                except OSError as exc:
                    update(state, record, "review_required", reason=str(exc))
                    raise
    return "Explicit reconciliation queued; ownership will be checked before any release retry."
