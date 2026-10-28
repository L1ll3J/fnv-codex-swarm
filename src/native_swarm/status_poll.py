"""Retry read-only status transport failures without replaying resolutions."""

import re
import time
from pathlib import Path

from .state import atomic


class StatusPollDeferred(RuntimeError):
    """Status is unavailable; completion processing must wait, not stop."""


def read_status(call, root, *, sleep=time.sleep):
    health = Path(root) / "status-poll-health.json"
    for attempt in range(1, 4):
        try:
            value = call("status", {})
        except RuntimeError as exc:
            # Classify only the direct read-only call's transport error. HTTP,
            # account health, parsing and validation failures keep their gates.
            if not re.match(r"\Acurl: \((?:5|6|7|18|28|35|52|55|56)\) ", str(exc)):
                raise
            atomic(
                health,
                {
                    "state": "retrying" if attempt < 3 else "deferred",
                    "attempt": attempt,
                    "error": str(exc),
                    "checked_at": time.time(),
                },
            )
            if attempt == 3:
                raise StatusPollDeferred(
                    "Read-only status transport unavailable; completion polling will retry."
                ) from exc
            sleep(attempt)
        else:
            atomic(health, {"state": "current", "checked_at": time.time()})
            return value


def configure(root):
    """Wire only the completion status poll and its typed deferral handler."""
    root = Path(root)
    changes = [
        (
            root / "resolve_ready.py",
            " status=call('status',{});save('account-current.json',status)",
            " from native_swarm.status_poll import read_status\n status=read_status(call,ROOT);save('account-current.json',status)",
        ),
        (
            root / "campaign_watch.py",
            "  deferred=any(",
            "  from native_swarm.status_poll import StatusPollDeferred\n  deferred=isinstance(exc,StatusPollDeferred) or any(",
        ),
    ]
    for path, old, _ in changes:
        if path.read_text().count(old) != 1:
            raise ValueError(f"Unsupported status-poll template in {path.name}; no launch permitted.")
    for path, old, new in changes:
        path.write_text(path.read_text().replace(old, new))
