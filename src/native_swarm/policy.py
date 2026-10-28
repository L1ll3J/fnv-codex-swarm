"""Operator usage policy over fresh account observations; no model/service calls."""

import datetime as dt
import math
import time
from pathlib import Path

from .state import process_alive, read

DEFAULT = {
    "mode": "stop_queuing",
    "cutoff_used_percent": 50,
    "taper_start_used_percent": 35,
}
MODES = {
    "stop_queuing": "Stop queuing; finish active work",
    "taper": "Taper admissions; finish active work",
    "stop_immediately": "Stop active workers at cutoff",
}


def validate(value):
    policy = {**DEFAULT, **(value or {})}
    cutoff, start = policy["cutoff_used_percent"], policy["taper_start_used_percent"]
    if policy["mode"] not in MODES:
        raise ValueError("Choose a usage policy.")
    if (
        type(cutoff) not in (int, float)
        or not math.isfinite(cutoff)
        or not 1 <= cutoff <= 100
    ):
        raise ValueError("Usage cutoff must be 1–100% used.")
    if (
        type(start) not in (int, float)
        or not math.isfinite(start)
        or not 0 <= start <= 100
        or (policy["mode"] == "taper" and start >= cutoff)
    ):
        raise ValueError("Taper must start at 0% or above and below the cutoff.")
    return policy


def used_percent(observation, now=None):
    now = time.time() if now is None else now
    for value in (
        observation["checked_at"],
        observation["latest_counter"]["observed_at"],
    ):
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or not -60 <= now - stamp.timestamp() <= 90:
            raise ValueError("Allowance observation is stale")
    windows = observation["active_windows"]
    if not isinstance(windows, list) or not windows:
        raise ValueError("No allowance windows")
    values = []
    for window in windows:
        used, reset = window["used_percent"], window["resets_at"]
        if (
            type(used) not in (int, float)
            or not math.isfinite(used)
            or not 0 <= used <= 100
        ):
            raise ValueError("Invalid allowance percentage")
        if type(reset) not in (int, float) or not math.isfinite(reset):
            raise ValueError("Invalid allowance reset")
        if reset > now:
            values.append(used)
    if not values:
        raise ValueError("Allowance windows expired")
    return max(values)


def evaluate(observation, policy, now=None):
    policy = validate(policy)
    used = used_percent(observation, now)
    cutoff = policy["cutoff_used_percent"]
    fraction = 0 if used >= cutoff else 1
    if policy["mode"] == "taper" and policy["taper_start_used_percent"] < used < cutoff:
        fraction = (cutoff - used) / (cutoff - policy["taper_start_used_percent"])
    return {
        **policy,
        "used_percent": used,
        "fraction": fraction,
        "reached": used >= cutoff,
    }


def targets(profiles, fraction):
    """Largest remainders preserve a single total function/group admission target."""
    exact = {p["id"]: p["instances"] * fraction for p in profiles}
    out = {key: math.floor(value) for key, value in exact.items()}
    remaining = math.floor(
        sum(p["instances"] for p in profiles) * fraction + 1e-9
    ) - sum(out.values())
    order = sorted(exact, key=lambda key: (-(exact[key] - out[key]), key))
    for key in order[:remaining]:
        out[key] += 1
    return out


def decision(root, now=None):
    root = Path(root)
    state = Path(read(root / "native-app-managed.json")["state_dir"])
    cfg = read(state / "config.json")
    observation = read(Path(read(root / "native-app-quota.json")["path"]))
    result = evaluate(observation, cfg.get("usage_policy"), now)
    from .allocation import selected_profile

    profile = read(root / "native-app-profile.json")
    owners = {}
    for value in cfg.get("managed_runs", []):
        run = Path(value)
        wanted = selected_profile(run, cfg)
        if wanted and process_alive(
            read(run / ".rolling-executor-owner.json").get("pid"), run
        ):
            owners[wanted["id"]] = (run, wanted)
    wanted = selected_profile(root, cfg)
    if wanted and wanted["id"] not in owners:
        owners[wanted["id"]] = (root, wanted)
    profiles = [value[1] for value in owners.values()]
    allocation = targets(profiles, result["fraction"])
    idle = read(state / "idle-slots.json")
    for identifier in allocation:
        allocation[identifier] = max(
            0, allocation[identifier] - idle.get(identifier, 0)
        )
        if identifier in cfg.get("paused_profiles", []):
            allocation[identifier] = 0
    owner = owners.get(profile["id"])
    result["target"] = (
        allocation.get(profile["id"], 0) if owner and owner[0] == root else 0
    )
    return result


def gate(root, now=None):
    try:
        value = decision(root, now)
    except (OSError, ValueError, KeyError, TypeError):
        return "Usage policy or allowance observation unavailable; admissions paused"
    if value["reached"] or (Path(root) / "operator-usage-paused.json").exists():
        return "Operator usage cutoff reached; admissions paused"
    return None
