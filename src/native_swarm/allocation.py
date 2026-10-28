"""Saved allocation governs admissions; active assignment identity stays fixed."""

from pathlib import Path

from .profiles import default_profile
from .state import atomic, read

IDENTITY = ("model", "effort", "group_size", "team_mode")


def selected_profile(root, cfg):
    running = read(Path(root) / "native-app-profile.json")
    configured = cfg.get("profiles")
    if not configured:
        if "workers" not in cfg:
            return running  # Compatibility with standalone policy snapshots.
        configured = [default_profile(cfg["workers"], cfg.get("bands"))]
    wanted = next((p for p in configured if p["id"] == running.get("id")), None)
    if wanted is None or any(
        wanted.get(k, "collaborative" if k == "team_mode" else None)
        != running.get(k, "collaborative" if k == "team_mode" else None)
        for k in IDENTITY
    ):
        return None
    return wanted


def sync(root, cfg):
    """Publish future admission changes without removing unrelated pause/stops."""
    root = Path(root)
    wanted = selected_profile(root, cfg)
    count = wanted["instances"] if wanted else 0
    running = read(root / "native-app-profile.json")
    control = read(root / "operator-control.json")
    # The native loop only accepts 1..75; a zero allocation is enforced by the
    # profile gate/pause, rather than generating a genuine native STOP.
    control["target_pool"] = max(1, count)
    atomic(root / "operator-control.json", control)
    pause = root / "operator-admissions-paused.json"
    previous = read(pause)
    if count == 0:
        if not pause.exists() or previous.get("source") == "profile-allocation":
            atomic(
                pause,
                {
                    "source": "profile-allocation",
                    "reason": "Profile removed, unallocated or awaiting a compatible controller",
                },
            )
    elif previous.get("source") == "profile-allocation":
        pause.unlink()
    atomic(
        root / "operator-profile-allocation.json",
        {"instances": count, "enabled": count > 0},
    )
    # Pre-live-allocation controllers read their target from this snapshot on
    # every tick. Mirror only the count for those runtimes; never change model,
    # reasoning, group size, assignment files or prompts.
    if read(root / "native-app-managed.json").get("allocation_version") != 1:
        original = root / "native-app-profile-at-creation.json"
        if not original.exists():
            atomic(original, running)
        if running.get("instances") != count:
            atomic(root / "native-app-profile.json", {**running, "instances": count})
        config_path = root / "run-config.json"
        if config_path.exists():
            config = read(config_path)
            backup = root / "run-config-at-creation.json"
            if not backup.exists():
                atomic(backup, config)
            if config.get("max_workers") != count:
                atomic(config_path, {**config, "max_workers": count})


WITHDRAWN = "PROFILE_ALLOCATION_WITHDRAWN_BEFORE_CLAIM"


def claim_request(root, dispatch, address=None):
    """Order claim dispatch against allocation publication; no network on refusal."""
    from .group_check import locked

    root = Path(root)
    state = Path(read(root / "native-app-managed.json")["state_dir"])
    with locked(state / "allocation.lock"):
        cfg = read(state / "config.json")
        wanted = selected_profile(root, cfg)
        local = [
            p
            for p in (root / "claims").iterdir()
            if p.is_dir()
            and not any(
                (p / n).exists()
                for n in ("giveup-response.json", "submission-response.json")
            )
        ]
        ceiling = read(root / "run-config.json").get("authorized_worker_ceiling", 75)
        if (
            not wanted
            or wanted["instances"] <= len(local)
            or len(local) >= ceiling
            or (root / "operator-admissions-paused.json").exists()
            or (root / "operator-drain.json").exists()
        ):
            raise RuntimeError(WITHDRAWN)
        from .routing import allowed

        rules = read(state / "routes.json")
        if (
            address is None
            and any(r.get("mode") == "exclusive" for r in rules.values())
        ) or (address is not None and not allowed(state, address, wanted["id"])):
            raise RuntimeError(WITHDRAWN)
        return dispatch()
