"""Operator-selected routing preferences; open routing imposes no profile rule."""

import re
from pathlib import Path

from .state import atomic, read

MODES = ("open", "preferred", "exclusive")


def canonical(address):
    value = int(str(address), 0) if isinstance(address, str) else int(address)
    if not 0 < value <= 0xFFFFFFFF:
        raise ValueError("Choose a valid 32-bit function address.")
    return f"0x{value:08x}"


def retire_legacy(state):
    # Called while control and allocation publication are serialized. Older
    # copied controllers cannot honor new routing rules; preserve their workers.
    from .commands import retire

    cfg = read(Path(state) / "config.json")
    for root in cfg.get("managed_runs", []):
        run = Path(root)
        if read(run / "native-app-managed.json").get("command_center_version") != 1:
            retire(run)


def allowed(state, address, profile):
    rule = read(Path(state) / "routes.json").get(canonical(address), {})
    return rule.get("mode", "open") != "exclusive" or profile in rule.get(
        "profiles", []
    )


def set_route(state, address, mode, profiles):
    from .backend import Backend

    if not re.fullmatch(r"0x[0-9a-fA-F]{1,8}", address) or mode not in MODES:
        raise ValueError("Choose a valid address and routing mode.")
    address = canonical(address)
    state = Path(state)
    from .group_check import locked

    with Backend(state).locked(), locked(state / "allocation.lock"):
        known = {
            p["id"]
            for p in read(state / "config.json").get("profiles", [{"id": "default"}])
        }
        if set(profiles) - known or (mode != "open" and not profiles):
            raise ValueError("Select existing destination profiles.")
        rules = read(state / "routes.json")
        rules[address.lower()] = {
            "mode": mode,
            "profiles": list(dict.fromkeys(profiles)),
        }
        retire_legacy(state)
        atomic(state / "routes.json", rules)


def enqueue_target(state, address, size, profile=None, *, mode="open", immediate=True):
    from .operator_store import OperatorStore, now
    import uuid

    if type(size) is not int or size <= 0:
        raise ValueError("Function size must be a positive whole number.")
    if not re.fullmatch(r"0x[0-9a-fA-F]{1,8}", address) or mode not in MODES:
        raise ValueError("Choose a valid address and routing mode.")
    address = canonical(address)
    store = OperatorStore(state)
    from .group_check import locked

    with store.controlled(), locked(Path(state) / "allocation.lock"):
        known = {
            p["id"]
            for p in read(Path(state) / "config.json").get(
                "profiles", [{"id": "default"}]
            )
        }
        if (
            profile is not None
            and profile not in known
            or mode != "open"
            and not profile
        ):
            raise ValueError("Select an existing destination profile.")
        queue = store.queue()
        for item in queue:
            if canonical(item["target"]["address"]) == address and item["state"] in (
                "queued",
                "reserved",
                "assigned",
                "review_required",
            ):
                if item["state"] != "queued":
                    raise ValueError(
                        "This function already has an active reservation. Stop or release its current assignment before rerouting it."
                    )
                item.update(profile_id=profile, immediate=immediate)
                retire_legacy(state)
                rules = read(Path(state) / "routes.json")
                rules[address.lower()] = {
                    "mode": mode,
                    "profiles": [profile] if profile else [],
                }
                atomic(Path(state) / "routes.json", rules)
                atomic(Path(state) / "queue.json", queue)
                return item
        item = {
            "id": uuid.uuid4().hex,
            "target": {
                "address": address.lower(),
                "bytes": size,
                "source": "operator target",
                "instruction_differences": None,
            },
            "state": "queued",
            "profile_id": profile,
            "immediate": immediate,
            "at": now(),
        }
        rules = read(Path(state) / "routes.json")
        rules[address.lower()] = {
            "mode": mode,
            "profiles": [profile] if profile else [],
        }
        atomic(Path(state) / "routes.json", rules)
        retire_legacy(state)
        queue.insert(0, item)
        atomic(Path(state) / "queue.json", queue)
        return item
