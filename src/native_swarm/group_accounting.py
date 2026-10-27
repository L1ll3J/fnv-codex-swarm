"""Attribute real peer threads to their claim without synthesizing model usage."""

import json
from pathlib import Path

from .state import read


def totals(claim, threads):
    claim = Path(claim)
    group = read(claim / "collaboration/group.json")
    if not group:
        return None
    ids = set()
    receipts = claim.parent.parent / "rolling-executor/direct-cli-threads.jsonl"
    if receipts.exists():
        for line in receipts.read_text().splitlines():
            try:
                receipt = json.loads(line)
            except ValueError:
                continue
            if (
                receipt.get("claim_id") == claim.name
                and receipt.get("source") == "codex_exec_direct"
                and isinstance(receipt.get("thread_id"), str)
            ):
                ids.add(receipt["thread_id"])
    pending_members = 0
    for member in group.get("members", []):
        identifier = member.get("thread_id") or read(
            Path(member["workspace"]) / "thread.json"
        ).get("thread_id")
        if identifier:
            ids.add(identifier)
        else:
            pending_members += 1
    observed = [threads.get(identifier, {}) for identifier in sorted(ids)]
    known_tokens = {}
    for thread in observed:
        for key, count in (thread.get("usage") or {}).items():
            known_tokens[key] = known_tokens.get(key, 0) + count
    costs = [thread.get("cost_usd") for thread in observed]
    unknown_cost = sum(cost is None for cost in costs) + pending_members
    known_cost = sum(cost for cost in costs if cost is not None)
    models = {thread.get("model") for thread in observed}
    return {
        "thread_ids": sorted(ids),
        "known_tokens": known_tokens,
        "known_cost_usd": known_cost,
        "total_cost_usd": known_cost if not unknown_cost and observed else None,
        "unknown_cost_threads": unknown_cost,
        "unknown_usage_threads": sum(thread.get("usage") is None for thread in observed)
        + pending_members,
        "model": next(iter(models))
        if len(models) == 1 and None not in models and not pending_members
        else None,
    }
