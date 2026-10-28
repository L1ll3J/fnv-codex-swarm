"""Execute one operator request with durable release reconciliation."""

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

from .commands import claim_path, retire, update
from .group_check import locked
from .progress import checked_progress, receipt_paths
from .state import atomic, process_alive, read


def process_snapshot():
    result = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdecimal():
            continue
        try:
            stat = (path / "stat").read_text().rsplit(")", 1)[1].split()
            result[int(path.name)] = (int(stat[1]), stat[19])
        except (OSError, ValueError, IndexError):
            pass
    return result


def stop_tree(claim, grace=10):
    run = claim.parent.parent
    jobs = read(run / "rolling-executor/journal.json").get("jobs", {})
    roots = {j.get("pid") for j in jobs.values() if j.get("claim_id") == claim.name}
    roots.discard(None)
    handoff = read(claim / "operator-handoff-process.json")
    handoff_pids = set()
    if handoff.get("intent") and not handoff.get("finished"):
        for path in Path("/proc").iterdir():
            if not path.name.isdecimal():
                continue
            try:
                argv = (path / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if (
                handoff.get("workspace")
                and handoff.get("thread_id")
                and handoff["workspace"].encode() in argv
                and handoff["thread_id"].encode() in argv
                and b"resume" in argv
                and b"exec" in argv
            ):
                handoff_pids.add(int(path.name))
        if handoff.get("pid"):
            handoff_pids.add(handoff["pid"])
    if (
        handoff.get("intent")
        and not handoff.get("finished")
        and not handoff.get("pid")
        and not handoff_pids
    ):
        raise RuntimeError(
            "Handoff launch identity is incomplete; no process is assumed dead. Retry reconciliation once its process identity becomes observable."
        )
    roots |= handoff_pids
    snapshot = process_snapshot()
    known = set()
    for pid in roots:
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        handoff_identity = (
            pid in handoff_pids
            and handoff.get("workspace", "").encode() in argv
            and handoff.get("thread_id", "").encode() in argv
            and b"resume" in argv
            and b"exec" in argv
        )
        if not handoff_identity and (
            claim.name.encode() not in argv or str(run).encode() not in argv
        ):
            if process_alive(pid, run):
                raise RuntimeError(
                    "Worker process identity needs reconciliation before signalling."
                )
            continue
        known.add(pid)
    while True:
        children = {pid for pid, (ppid, _) in snapshot.items() if ppid in known}
        if children <= known:
            break
        known |= children
    handles = []
    try:
        for pid in known:
            try:
                fd = os.pidfd_open(pid)
                if process_snapshot().get(pid) != snapshot.get(pid):
                    os.close(fd)
                    raise RuntimeError("Worker process identity changed during stop.")
                handles.append(fd)
            except ProcessLookupError:
                pass
        for fd in handles:
            try:
                signal.pidfd_send_signal(fd, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + grace
        for fd in handles:
            if not select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]:
                try:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if not select.select([fd], [], [], 5)[0]:
                    raise RuntimeError(
                        "A worker process has not exited; release withheld."
                    )
    finally:
        for fd in handles:
            os.close(fd)
    # Orphaned member processes must not be guessed dead.
    session = read(claim / "session/state.json")
    group = read(claim / "collaboration/group.json")
    for pid in [t.get("pid") for t in session.get("turns", [])] + [
        m.get("pid") for m in group.get("members", [])
    ]:
        if process_alive(pid, run):
            raise RuntimeError(
                "An orphaned worker remains; inspect its process before release."
            )
    if handoff.get("intent") and not handoff.get("finished"):
        atomic(
            claim / "operator-handoff-process.json",
            {**handoff, "finished": True, "stopped_by_operator": True},
        )
    assignment = claim / "assignment.json"
    if assignment.is_file():
        atomic(
            claim / "operator-stop-terminal.json",
            {
                "verified_stopped": True,
                "assignment_sha256": hashlib.sha256(
                    assignment.read_bytes()
                ).hexdigest(),
                "pids": sorted(known),
                "stopped_at": time.time(),
                "reason": "Operator stop; no successful model completion is implied.",
            },
        )

    streams = [(claim / "work/codex.stdout.jsonl", 0)]
    streams.extend((path, 0) for path in (claim / "session").glob("turn-*.jsonl"))
    streams.extend(
        (path, 0) for path in (claim / "collaboration").glob("peer-*/stdout.jsonl")
    )
    workspaces = [claim / "work"] + [
        path for path in (claim / "collaboration").glob("peer-*") if path.is_dir()
    ]
    rejected = latch_permission_rejection(claim, streams, workspaces, "operator-stop")
    if handoff.get("workspace"):
        rejected = (
            latch_handoff_rejection(
                claim, Path(handoff["workspace"]), handoff.get("log_offset", 0)
            )
            or rejected
        )
    if rejected:
        raise PermissionError(
            "Retained permission rejection; mechanical evidence preserved and admissions remain stopped."
        )


def is_match(best):
    return bool(
        best
        and best[2].get("checker_exit_code") == 0
        and best[2].get("checker_result", {}).get("status") == "match"
    )


def preserve(claim):
    progress = checked_progress(claim)
    records = []
    for path in receipt_paths(claim):
        row = read(path)
        source, log = (
            Path(row.get("source_path", "/missing")),
            Path(row.get("log_path", "/missing")),
        )
        if not source.is_file() or not log.is_file():
            continue
        if hashlib.sha256(source.read_bytes()).hexdigest() != row.get(
            "source_sha256"
        ) or hashlib.sha256(log.read_bytes()).hexdigest() != row.get("log_sha256"):
            continue
        check = row.get("checker_result", {})
        if check.get("status") == "match" and row.get("checker_exit_code") == 0:
            records.append((0, path, row))
        elif check.get("status") == "wip" and type(check.get("ndiff_insn")) is int:
            records.append((check["ndiff_insn"], path, row))
    best = min(records, key=lambda r: (not is_match(r), r[0])) if records else None
    handoff = {
        "claim_id": claim.name,
        "best_receipt": str(best[1]) if best else None,
        "best_score": best[0] if best else None,
        "source_sha256": best[2]["source_sha256"] if best else None,
        "improvements": progress["events"],
        "authored_result": read(claim / "result.json")
        or read(claim / "work/result.json"),
        "operator_stop": True,
    }
    atomic(claim / "operator-handoff.json", handoff)
    return best


def restore_completion(run):
    """Recover a stopped CPU watcher after an interrupted operator command."""
    marker = read(run / "operator-completion-window.json")
    if marker.get("state") != "paused":
        return
    pid = marker["pid"]
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        atomic(
            run / "operator-completion-window.json",
            {**marker, "state": "watcher_exited"},
        )
        return
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if (
            str(run / "campaign_watch.py").encode() not in argv
            or stat[19] != marker["start_ticks"]
        ):
            raise RuntimeError(
                "Completion watcher identity changed; retained paused-watcher evidence needs review."
            )
        signal.pidfd_send_signal(fd, signal.SIGCONT)
        atomic(run / "operator-completion-window.json", {**marker, "state": "resumed"})
    finally:
        os.close(fd)


def recover_completion(run):
    """Start recovers only an abandoned window; a live command retains its lock."""
    import fcntl

    with (run / ".operator-resolution.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        restore_completion(run)


@contextmanager
def completion_window(run):
    """Order manual RPCs against an older completion watch without killing it."""
    with locked(run / ".operator-resolution.lock"):
        restore_completion(run)
        pid = read(run / "launch.json").get("completion_watch_pid")
        fd = None
        paused = False
        try:
            if pid and process_alive(pid, run):
                fd = os.pidfd_open(pid)
                argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                if str(run / "campaign_watch.py").encode() not in argv:
                    raise RuntimeError(
                        "Completion watcher identity changed; manual release withheld."
                    )
                start_ticks = (
                    Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
                )
                deadline = time.monotonic() + 60
                while True:
                    try:
                        atomic(
                            run / "operator-completion-window.json",
                            {
                                "state": "paused",
                                "pid": pid,
                                "start_ticks": start_ticks,
                                "operator_pid": os.getpid(),
                            },
                        )
                        signal.pidfd_send_signal(fd, signal.SIGSTOP)
                        paused = True
                    except ProcessLookupError:
                        break
                    # Confirm STOP before taking the child snapshot; an in-flight
                    # resolver/transport must finish before the manual write.
                    for _ in range(100):
                        try:
                            phase = (
                                Path(f"/proc/{pid}/stat")
                                .read_text()
                                .rsplit(")", 1)[1]
                                .split()[0]
                            )
                        except FileNotFoundError:
                            phase = "Z"
                        if phase in ("T", "t", "Z"):
                            break
                        time.sleep(0.01)
                    else:
                        raise RuntimeError(
                            "Completion watcher did not enter a stopped state."
                        )
                    if not any(ppid == pid for ppid, _ in process_snapshot().values()):
                        break
                    signal.pidfd_send_signal(fd, signal.SIGCONT)
                    paused = False
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "Completion processing is busy; worker stopped, release deferred for explicit retry."
                        )
                    time.sleep(0.25)
            yield
        finally:
            if fd is not None:
                if paused:
                    try:
                        signal.pidfd_send_signal(fd, signal.SIGCONT)
                    except ProcessLookupError:
                        pass
                os.close(fd)
                restore_completion(run)


def latch_permission_rejection(claim, streams, workspaces, source):
    """Preserve structured permission failures from any stopped model stream."""
    from .group import rejection_event, member_rejection

    rejected = any(member_rejection(workspace) for workspace in workspaces)
    reference = None
    for log, offset in streams:
        if not log.exists():
            continue
        if not log.resolve().is_relative_to(claim.resolve()):
            raise RuntimeError(
                "Permission evidence stream escaped the claim; retained for review."
            )
        with log.open("rb") as output:
            output.seek(offset)
            for line in output:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and rejection_event(event):
                    rejected, reference = True, str(log)
                    break
        if reference:
            break
    if not rejected:
        return False
    run = claim.parent.parent
    evidence = {
        "claim_id": claim.name,
        "source": source,
        "report_path": reference,
        "at": time.time(),
    }
    atomic(claim / (source + "-rejection.json"), evidence)
    if not (run / "approval-blocker-current.json").exists():
        atomic(run / "approval-blocker-current.json", evidence)
    try:
        with (run / "STOP_NEW_CLAIMS").open("x") as stop:
            stop.write(
                "Approval rejection during " + source + "; operator review required."
            )
    except FileExistsError:
        pass
    return True


def latch_handoff_rejection(claim, workspace, offset=0):
    return latch_permission_rejection(
        claim,
        [(claim / "operator-authored-handoff.jsonl", offset)],
        [workspace],
        "operator-handoff",
    )


def authored_handoff(claim, timeout):
    """One permission-guarded, capacity-accounted continuation of an existing thread."""
    from .session import command
    from .group import group_rejected
    from .commands import operator_terminal
    from .assessment import occupied

    run = claim.parent.parent
    assignment = read(claim / "assignment.json")
    if (
        (run / "approval-blocker-current.json").exists()
        or group_rejected(claim)
        or any(
            read(claim / name).get(key)
            for name in ("result.json", "work/result.json", "session/state.json")
            for key in ("approval_rejection", "permission_denied")
        )
    ):
        atomic(
            claim / "operator-authored-handoff.json",
            {
                "status": "blocked_permission",
                "reason": "Retained permission rejection; mechanical evidence preserved without model continuation.",
            },
        )
        return
    thread = read(claim / "session/state.json").get("thread_id")
    workspace = claim / "work"
    if not thread:
        for member in read(claim / "collaboration/group.json").get("members", []):
            candidate = read(Path(member["workspace"]) / "thread.json").get("thread_id")
            if candidate:
                thread, workspace = candidate, Path(member["workspace"])
                break
    if not thread:
        atomic(
            claim / "operator-authored-handoff.json",
            {"status": "unavailable", "reason": "No recorded thread to resume."},
        )
        return
    argv = command(run, assignment, thread)
    argv[argv.index("-C") + 1] = str(workspace)
    state = Path(read(run / "native-app-managed.json")["state_dir"])
    with (
        (claim / "operator-authored-handoff.jsonl").open("ab") as out,
        (claim / "operator-authored-handoff.stderr").open("ab") as err,
    ):
        out.seek(0, 2)
        offset = out.tell()
        proc = None
        status = "interrupted"
        launch = {
            "intent": True,
            "workspace": str(workspace),
            "thread_id": thread,
            "log_offset": offset,
            "started_at": time.time(),
        }
        try:
            with locked(state / "capacity.lock"):
                cfg = read(state / "config.json")
                if not operator_terminal(claim) or occupied(cfg, state) >= cfg.get(
                    "worker_capacity", 10
                ):
                    atomic(
                        claim / "operator-authored-handoff.json",
                        {
                            "status": "skipped_capacity",
                            "reason": "No free verified slot; mechanical handoff preserved.",
                        },
                    )
                    return
                atomic(claim / "operator-handoff-process.json", launch)
                try:
                    proc = subprocess.Popen(
                        argv,
                        cwd=workspace,
                        stdin=subprocess.PIPE,
                        stdout=out,
                        stderr=err,
                        start_new_session=True,
                    )
                except OSError:
                    atomic(
                        claim / "operator-handoff-process.json",
                        {**launch, "finished": True, "launch_failed": True},
                    )
                    raise
                atomic(
                    claim / "operator-handoff-process.json", {**launch, "pid": proc.pid}
                )
            proc.communicate(
                (Path(__file__).parent / "prompts/operator_handoff.xml").read_bytes(),
                timeout=timeout,
            )
            status = "finished" if proc.returncode == 0 else "failed"
        except subprocess.TimeoutExpired:
            status = "timed_out"
        finally:
            if proc is not None:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                atomic(
                    claim / "operator-handoff-process.json",
                    {
                        **launch,
                        "pid": proc.pid,
                        "finished": True,
                        "exit_code": proc.returncode,
                    },
                )
                out.flush()
                rejected = latch_handoff_rejection(claim, workspace, offset)
                atomic(
                    claim / "operator-authored-handoff.json",
                    {
                        "status": "blocked_permission" if rejected else status,
                        "thread_id": thread,
                        "exit_code": proc.returncode,
                    },
                )
                if rejected:
                    raise PermissionError(
                        "Handoff reported a permission rejection; evidence and claim retained, admissions not resumed."
                    )


def submit_match(claim, record, best):
    """Recover an operator-stopped MATCH through original evidence and validation."""
    from .group_check import digest
    from .group import group_rejected

    run = claim.parent.parent
    assignment = read(claim / "assignment.json")
    if read(claim / "submission-response.json") or read(claim / "giveup-response.json"):
        return
    if read(claim / "operator-submit-intent.json"):
        raise RuntimeError(
            "An earlier MATCH submission is uncertain; reconcile ownership before retry."
        )
    if (run / "approval-blocker-current.json").exists() or group_rejected(claim):
        raise RuntimeError(
            "Approval rejection remains protected; MATCH evidence preserved for review."
        )
    if any(
        read(claim / name).get(key)
        for name in ("result.json", "work/result.json")
        for key in ("approval_rejection", "permission_denied")
    ):
        raise RuntimeError("Worker approval rejection remains protected.")
    for script in ("usage.py", "register_usage.py"):
        value = subprocess.run(
            [sys.executable, str(run / script)],
            cwd=run,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if value.returncode:
            raise RuntimeError("MATCH usage verification failed in " + script)
    threads = {t["id"]: t for t in read(run / "usage-current.json").get("threads", [])}
    group = read(claim / "collaboration/group.json")
    ids = [
        m.get("thread_id")
        or read(Path(m["workspace"]) / "thread.json").get("thread_id")
        for m in group.get("members", [])
    ]
    if not ids:
        ids = [
            read(claim / "worker-assignment.json").get("thread_id")
            or read(claim / "session/state.json").get("thread_id")
        ]
    if not ids or any(
        not tid
        or not threads.get(tid, {}).get("usage")
        or threads[tid].get("model") != assignment["model"]
        for tid in ids
    ):
        raise RuntimeError(
            "Actual worker model/usage is not yet verified; MATCH retained."
        )
    driver = Path(assignment["checker_workflow"]["single_check_driver"])
    if digest(driver) != assignment["checker_workflow"]["single_check_driver_sha256"]:
        raise RuntimeError("Recorded finish driver changed; MATCH retained.")
    output = claim / ("operator-match-" + record["id"] + ".json")
    if not output.exists():
        result = subprocess.run(
            [
                sys.executable,
                str(driver),
                "--assignment",
                str(claim / "assignment.json"),
                "--finish",
                str(best[1]),
                "--result-output",
                str(output),
                "--residual",
                "Operator stopped reconstruction; original checked MATCH preserved.",
            ],
            cwd=run,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode:
            raise RuntimeError(
                "Could not finalize original MATCH receipt: " + result.stderr[-1000:]
            )
    value = read(output)
    if value.get("match") is not True:
        raise RuntimeError("Original recorded finish did not produce a MATCH result.")
    audit = subprocess.run(
        [
            sys.executable,
            "-c",
            "from evidence import audit_worker; from native_swarm.group import audit_group; import json,pathlib,sys; p=pathlib.Path(sys.argv[1]); assert not (p/'collaboration').exists() or audit_group(p,allow_operator_stop=True), 'Group integrity/attribution audit incomplete'; audit_worker(p,json.loads(pathlib.Path(sys.argv[2]).read_text()))",
            str(claim),
            str(output),
        ],
        cwd=run,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if audit.returncode:
        raise RuntimeError(
            "Original worker evidence audit rejected preserved MATCH: "
            + audit.stderr[-1000:]
        )
    existing = claim / "result.json"
    if existing.exists():
        history = claim / "operator-result-history"
        history.mkdir(exist_ok=True)
        if not (history / (record["id"] + ".json")).exists():
            atomic(history / (record["id"] + ".json"), read(existing))
    atomic(existing, value)
    atomic(
        claim / "operator-submit-intent.json",
        {"command": record["id"], "state": "dispatch_started", "receipt": str(best[1])},
    )
    result = subprocess.run(
        [sys.executable, str(run / "validate_submit.py"), claim.name],
        cwd=run,
        capture_output=True,
        text=True,
        timeout=300,
    )
    (claim / "operator-submit.log").write_text(result.stdout + result.stderr)
    if result.returncode or not read(claim / "submission-response.json"):
        raise RuntimeError(
            "Independent MATCH validation/submission needs review; inspect operator-submit.log. No automatic submission retry."
        )


def release(claim, record, best=None):
    run = claim.parent.parent
    code = "from service import call,guard; import json,sys; s=call('status',{}); guard(s); cid=sys.argv[1]; owned=any(c.get('this_key') and c.get('claim_id',c.get('id'))==cid for c in s['open_claims']); print(json.dumps({'owned':owned,'status':s}))"
    status = subprocess.run(
        [sys.executable, "-c", code, claim.name],
        cwd=run,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    value = json.loads(status.stdout)
    if not value["owned"]:
        atomic(
            claim / "giveup-response.json",
            {"observed_closed": True, "status_evidence": value["status"]},
        )
        return
    if read(claim / "operator-release-intent.json"):
        raise RuntimeError(
            "A previous release is uncertain; reconcile ownership and explicitly retry."
        )
    atomic(
        claim / "operator-release-intent.json",
        {"command": record["id"], "state": "dispatch_started"},
    )
    payload = {
        "claim_id": claim.name,
        "reason": "Operator ended this assignment early; checked sources, diagnostics and handoff are preserved.",
    }
    if best:
        payload["best_code"] = Path(best[2]["source_path"]).read_text()
        payload["model"] = read(claim / "assignment.json")["model"]
    rpc = "from service import call,save; import json,sys; result=call('giveup',json.load(sys.stdin)); save('claims/'+sys.argv[1]+'/giveup-response.json',result); print(json.dumps(result))"
    result = subprocess.run(
        [sys.executable, "-c", rpc, claim.name],
        cwd=run,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=90,
    )
    (claim / "operator-release.log").write_text(result.stdout + result.stderr)
    if result.returncode or not read(claim / "giveup-response.json"):
        raise RuntimeError(
            "Release has no acknowledged result; inspect/reconcile the saved operation."
        )


def settle_preparation(state, record, claim):
    """Let acknowledged in-flight acquisition finish under the controller's drain."""
    deadline = time.monotonic() + 120
    while True:
        if read(claim / "giveup-response.json") or read(
            claim / "submission-response.json"
        ):
            return
        jobs = (
            read(claim.parent.parent / "rolling-executor/journal.json")
            .get("jobs", {})
            .values()
        )
        prepared = any(
            j.get("claim_id") == claim.name
            and j.get("state")
            in ("preparing", "prepared", "prepared_waiting_transient_gate")
            for j in jobs
        )
        if read(claim / "assignment.json") and not prepared:
            return
        update(
            state,
            record,
            "waiting_for_preparation",
            reason="Admission is closed; waiting for the in-flight preparation/controller release to settle before manual release.",
        )
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Preparation has not settled; its claim and request evidence are retained. Reconcile this command after checking the preparation log."
            )
        time.sleep(0.5)


def execute(state, identifier):
    state = Path(state)
    record = read(state / "commands" / (identifier + ".json"))
    claim = claim_path(state, record["claim"])
    with locked(claim / ".operator-command.lock"):
        record = read(state / "commands" / (identifier + ".json"))
        if record.get("state") != "requested":
            return
        try:
            from .backend import Backend

            with locked(state / "control.lock"):
                if read(claim / "operator-command.json").get("id") != identifier:
                    return
                retire(claim.parent.parent, claim.name)
            settle_preparation(state, record, claim)
            if record["action"] == "finish_round":
                update(state, record, "waiting_for_round")
                rounds = claim / "work/batch-rounds"
                rounds.mkdir(parents=True, exist_ok=True)
                with locked(rounds / ".lock"):
                    update(state, record, "stopping")
                    stop_tree(claim)
            else:
                update(state, record, "stopping")
                stop_tree(claim)
            best = preserve(claim)
            if (
                record["action"] == "handoff"
                and not is_match(best)
                and (
                    record["handoff"] == "always"
                    or record["handoff"] == "improved"
                    and checked_progress(claim)["events"]
                )
            ):
                update(state, record, "writing_handoff")
                authored_handoff(claim, record["timeout"])
                best = preserve(claim)
            update(state, record, "evidence_preserved")
            if is_match(best):
                update(state, record, "validating_match")
                with completion_window(claim.parent.parent):
                    submit_match(claim, record, best)
                update(
                    state,
                    record,
                    "completed",
                    reason="MATCH passed original evidence and independent validation; submission response saved.",
                )
            else:
                update(state, record, "releasing")
                with completion_window(claim.parent.parent):
                    release(claim, record, best)
                update(state, record, "completed")
            if record.get("resume_admissions", True):
                from .backend import Backend

                profile = read(claim.parent.parent / "native-app-profile.json").get(
                    "id"
                )
                if (claim.parent.parent / "approval-blocker-current.json").exists():
                    update(
                        state,
                        record,
                        "completed",
                        refill_status="Permission stop retained; admissions were not resumed.",
                    )
                    return
                if profile not in read(state / "config.json").get(
                    "paused_profiles", []
                ):
                    try:
                        message = Backend(state).start([profile], restore_slots=False)
                    except Exception as exc:
                        message = "Claim released; refill needs attention: " + str(exc)
                    update(state, record, "completed", refill_status=message)

        except Exception as exc:
            update(state, record, "review_required", reason=str(exc))


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    execute(sys.argv[1], sys.argv[2])
