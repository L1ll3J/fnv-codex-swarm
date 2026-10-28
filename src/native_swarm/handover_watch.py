"""Short-lived deterministic watcher for an explicitly edited active profile."""

import fcntl
import subprocess
import sys
import time
from pathlib import Path

from .allocation import selected_profile
from .handover import idle_reason, retire_idle
from .profiles import validate_profiles
from .state import atomic, process_alive, read


def request(root, state, *, control_locked=False):
    root, state = Path(root), Path(state)
    if not control_locked:
        # Use the same lock as advance() and Backend.start(). Checking a marker
        # before taking this lock could overwrite an ambiguous launch outcome.
        with (state / "control.lock").open("a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            return request(root, state, control_locked=True)
    marker = root / "operator-profile-handover-pending.json"
    if read(marker).get("state") in ("replacement_start_intent", "review_required"):
        return False
    atomic(marker, {"state": "pending"})
    with (root / "profile-handover.log").open("ab") as log:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "native_swarm.handover_watch",
                str(state),
                str(root),
            ],
            cwd=root,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def advance(root, backend):
    """Return False only while the old assignment still has normal work to finish."""
    root = Path(root)
    marker = root / "operator-profile-handover-pending.json"
    with backend.locked():
        if read(marker).get("state") != "pending":
            return True
        cfg = backend.config
        running = read(root / "native-app-profile.json")
        wanted = next(
            (p for p in backend.profiles() if p["id"] == running.get("id")), None
        )
        reason = None
        if (
            not wanted
            or wanted["instances"] == 0
            or selected_profile(root, cfg) is not None
        ):
            reason = "Profile removed, unallocated or restored to its existing identity"
        elif (root / "STOP_NEW_CLAIMS").exists() or (
            root / "operator-drain.json"
        ).exists():
            reason = "Protected controller stop or operator drain remains in force"
        elif (
            read(root / "operator-admissions-paused.json").get("source")
            != "profile-allocation"
        ):
            reason = "Operator admission pause remains in force"
        if reason:
            atomic(marker, {"state": "cancelled", "reason": reason})
            return True
        for value in cfg.get("managed_runs", []):
            other = Path(value)
            if other == root:
                continue
            if not process_alive(
                read(other / ".rolling-executor-owner.json").get("pid"), other
            ):
                continue
            if (other / "STOP_NEW_CLAIMS").exists() or (
                other / "operator-drain.json"
            ).exists():
                atomic(
                    marker,
                    {
                        "state": "blocked",
                        "reason": "Another managed controller has a protected stop or drain",
                    },
                )
                return True
            profile = read(other / "native-app-profile.json")
            if profile.get("id") == wanted["id"]:
                compatible = selected_profile(other, cfg) is not None
                atomic(
                    marker,
                    {
                        "state": "completed" if compatible else "blocked",
                        "replacement": value,
                        "reason": "Another controller already owns this profile",
                    },
                )
                return True
        if idle_reason(root):
            return False
        # Quota and service readiness are checked only once the old work is done.
        # No unrelated profile pause is removed, and no unrelated controller starts.
        try:
            validate_profiles(backend.profiles(), cfg.get("worker_capacity", 10))
            backend.quota(cfg)
            template = Path(cfg["template_run"])
            backend.verify_template(template, cfg)
            account = backend.preflight(root, require_idle=False)
            retire_idle(root, backend.state)
        except Exception as exc:
            atomic(marker, {"state": "blocked", "reason": str(exc)})
            return True
        atomic(marker, {"state": "replacement_start_intent", "profile": wanted})
        try:
            backend.create_run(cfg, template, account, wanted)
        except Exception as exc:
            # A controller-start side effect may have happened. Never retry blindly.
            atomic(marker, {"state": "review_required", "reason": str(exc)})
            return True
        atomic(marker, {"state": "completed", "replacement": str(backend.current)})
        return True


def main():
    from .backend import Backend, ControlError

    state, root = map(Path, sys.argv[1:])
    with (root / "profile-handover.lock").open("a") as guard:
        # A successor waits for a finishing watcher rather than losing a newly
        # published request in the interval between its final tick and exit.
        fcntl.flock(guard, fcntl.LOCK_EX)
        backend = Backend(state)
        while True:
            try:
                if advance(root, backend):
                    return
            except ControlError:
                pass  # A simultaneous operator action owns the control lock.
            time.sleep(5)


if __name__ == "__main__":
    main()
