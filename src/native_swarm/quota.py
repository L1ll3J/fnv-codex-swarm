"""Read account limits through Codex's documented local RPC; never start a model turn."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from .state import atomic
from .executables import executable as check_executable, resolve_codex


def limits(timeout=20, executable=None):
    codex = check_executable(executable) if executable else resolve_codex()
    process = subprocess.Popen(
        [str(codex), "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    events = queue.Queue()

    def pump():
        for line in process.stdout:
            try:
                events.put(json.loads(line))
            except ValueError:
                continue
        events.put(None)

    threading.Thread(target=pump, daemon=True).start()

    def send(value):
        process.stdin.write(json.dumps(value) + "\n")
        process.stdin.flush()

    def receive(identifier, operation):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            try:
                event = events.get(timeout=max(0.01, until - time.monotonic()))
            except queue.Empty:
                raise TimeoutError(
                    f"Codex {operation} timed out; no allowance was read"
                ) from None
            if event is None:
                raise RuntimeError(
                    f"Codex account client exited before {operation} replied. Check compatibility of {codex}."
                )
            if event.get("id") == identifier:
                if event.get("error"):
                    raise RuntimeError(
                        f"Codex {operation} RPC failed; check Codex account access."
                    )
                return event.get("result", {})
        raise TimeoutError("Codex account RPC timed out")

    try:
        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "fnv_native_swarm",
                        "title": "Native Swarm",
                        "version": "0.1.0",
                    }
                },
            }
        )
        receive(1, "initialize")
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read", "params": {}})
        return normalize(receive(2, "account/rateLimits/read"))
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def normalize(response):
    bucket = (
        (response.get("rateLimitsByLimitId") or {}).get("codex")
        or response.get("rateLimits")
        or {}
    )
    active = []
    now = time.time()
    for key in ("primary", "secondary"):
        value = bucket.get(key)
        if not isinstance(value, dict):
            continue
        used, reset = value.get("usedPercent"), value.get("resetsAt")
        if (
            type(used) not in (int, float)
            or not 0 <= used <= 100
            or type(reset) not in (int, float)
            or reset <= now
        ):
            continue
        active.append(
            {
                "window": key,
                "used_percent": used,
                "resets_at": reset,
                "window_minutes": value.get("windowDurationMins"),
            }
        )
    if not active:
        raise RuntimeError(
            "Codex returned no current allowance window; no new work admitted"
        )
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    return {
        "checked_at": stamp,
        "latest_counter": {
            "observed_at": stamp,
            "source": "Codex account/rateLimits/read",
        },
        "active_windows": active,
        "reserve_reached": any(x["used_percent"] >= 50 for x in active),
        "threshold_used_percent": 50,
        "keep_remaining_percent": 50,
        "model_calls": 0,
    }


def refresh(path, executable=None):
    value = limits(executable=executable)
    atomic(Path(path), value)
    return value


def monitor(path, parent_pid, executable=None):
    while Path(f"/proc/{parent_pid}/cmdline").exists():
        try:
            refresh(path, executable)
        except Exception:
            # Retain the timestamp of the last actual observation; existing gates
            # fail closed when it becomes stale. Never refresh stale evidence.
            pass
        time.sleep(15)


if __name__ == "__main__":
    monitor(
        Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else None
    )
