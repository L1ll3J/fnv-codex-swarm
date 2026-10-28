"""Retire only positively idle controllers whose saved profile identity changed."""

import os
from pathlib import Path
import select
import signal

from .allocation import selected_profile, sync
from .group import group_terminal
from .group_check import locked
from .state import atomic, process_alive, read


TERMINAL_JOBS = {
    "awaiting_campaign_closure", "released_before_launch",
    "own_history_duplicate_resolved_no_launch", "continuation_unavailable_no_launch",
    "prepare_deferred_cooldown", "band_unavailable_cooldown_no_launch",
    "fresh_unavailable_cooldown_no_launch",
}


def idle_reason(root):
    """Return a reason to retain ownership; missing evidence is not idleness."""
    root = Path(root)
    if (root / "STOP_NEW_CLAIMS").exists() or (root / "operator-drain.json").exists():
        return "a protected stop or operator drain is present"
    journal = read(root / "rolling-executor/journal.json")
    if not isinstance(journal.get("jobs"), dict):
        return "controller journal is missing or unreadable"
    for job in journal["jobs"].values():
        if job.get("state") not in TERMINAL_JOBS:
            return "a worker, preparation or unresolved launch remains"
        if process_alive(job.get("pid"), root):
            return "a worker process is still alive"
    for claim in (root / "claims").glob("*/claim.json"):
        closed = any(read(claim.parent / name) for name in (
            "giveup-response.json", "submission-response.json"
        ))
        if not closed:
            return "a claim has no acknowledged closure"
        if not group_terminal(claim.parent):
            return "collaboration members are still finishing"
    return None


def terminate_controller(pid, root):
    """Signal the verified process through a stable kernel handle, never a PID race."""
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("safe controller retirement requires Linux pidfd support")
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        if not process_alive(pid, root):
            raise RuntimeError("controller process identity changed; ownership retained")
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        if not select.select([descriptor], [], [], 5)[0]:
            raise RuntimeError("idle controller has not exited; replacement deferred")
    finally:
        os.close(descriptor)


def retire_idle(root, state):
    """Close only a withdrawn controller, ordered against claim dispatch and launch."""
    root, state = Path(root), Path(state)
    with locked(state / "allocation.lock"), locked(state / "capacity.lock"):
        cfg = read(state / "config.json")
        if str(root) not in cfg.get("managed_runs", []):
            raise RuntimeError("controller is not managed by this application")
        if selected_profile(root, cfg) is not None:
            raise RuntimeError("saved profile identity no longer requires handover")
        sync(root, cfg)
        pause = read(root / "operator-admissions-paused.json")
        if pause.get("source") != "profile-allocation":
            raise RuntimeError("operator admission pause is preserved")
        reason = idle_reason(root)
        if reason:
            raise RuntimeError(reason)
        owner = read(root / ".rolling-executor-owner.json")
        pid = owner.get("pid")
        if type(pid) is not int:
            raise RuntimeError("controller ownership is missing")
        atomic(root / "operator-profile-handover.json", {
            "state": "retirement_requested", "owner": owner,
            "profile": read(root / "native-app-profile.json"),
        })
        if process_alive(pid, root):
            terminate_controller(pid, root)
        # Never remove the owner or original journals; they remain audit history.
        atomic(root / "operator-profile-handover.json", {
            "state": "retired", "owner": owner,
            "profile": read(root / "native-app-profile.json"),
        })
