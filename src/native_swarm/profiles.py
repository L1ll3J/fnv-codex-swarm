"""Validated operator-owned allocation; one controller run has one model identity."""

import re

MODELS = {
    "gpt-6.1-sol": {
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        "rates": (2, 0.1, 2.5, 10),
    },
    "gpt-6.1-terra": {
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        "rates": (1, 0.05, 1.25, 5),
    },
    "gpt-6-luna": {
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        "rates": (0.1, 0.01, 0.125, 0.5),
    },
    "gpt-6-astra": {
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        "rates": (10, 1, 12.5, 50),
    },
}
PRIORITIES = ("random", "smallest", "largest")
BANDS = ("<=255", "256-511", "512-1K", "1K-4K", "4K+")


def default_profile(workers=10, bands=None):
    return {
        "id": "default",
        "name": "Default",
        "instances": workers,
        "model": "gpt-6.1-sol",
        "effort": "medium",
        "priority": "random",
        "queue_first": True,
        "group_size": 1,
        "bands": list(BANDS if bands is None else bands),
        "rounds": 20,
        "budget_mode": "shared",
    }


def validate_capacity(capacity):
    if type(capacity) is not int or capacity < 1:
        raise ValueError("Total worker capacity must be a positive whole number.")
    return capacity


def validate_profiles(profiles, capacity=10):
    validate_capacity(capacity)
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("Add at least one worker profile.")
    ids = set()
    total = 0
    for p in profiles:
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", str(p.get("id", "")))
            or p["id"] in ids
        ):
            raise ValueError("Profile IDs must be unique.")
        ids.add(p["id"])
        if not str(p.get("name", "")).strip():
            raise ValueError("Name each worker profile.")
        if (
            p.get("model") not in MODELS
            or p.get("effort") not in MODELS[p["model"]]["efforts"]
        ):
            raise ValueError("Choose a supported model and reasoning level.")
        if p.get("priority") not in PRIORITIES:
            raise ValueError("Choose random, smallest or largest.")
        bands = p.get("bands", list(BANDS))
        if (
            not isinstance(bands, list)
            or not bands
            or any(b not in BANDS for b in bands)
        ):
            raise ValueError("Select at least one valid size bracket per profile.")
        if type(p.get("instances")) is not int or not 0 <= p["instances"] <= 75:
            raise ValueError(
                "Concurrent functions must be 0–75 per profile (native controller limit); use multiple profiles for more. Zero leaves the profile unallocated."
            )
        if type(p.get("group_size")) is not int or not 1 <= p["group_size"] <= 4:
            raise ValueError("Agents per function must be 1–4.")
        if type(p.get("queue_first")) is not bool:
            raise ValueError("Queue preference must be on or off.")
        from .budgets import validate

        validate(p.get("rounds", 20), p.get("budget_mode", "shared"))
        total += p["instances"] * p["group_size"]
    if total > capacity:
        raise ValueError(
            f"This allocation uses {total} agents; the configured maximum is {capacity}. Increase Total worker capacity in Settings or reduce an allocation."
        )
    return total
