"""Close positively unlaunched assignments during an explicit operator drain."""

import hashlib
import json

from .state import atomic, read


def release_unlaunched(
    executor,
    worker,
    *,
    reason="Operator requested drain before any worker launch; local assignment preserved.",
):
    root = executor.root
    job = executor.journal["jobs"][worker]
    if job.get("prepared_gate_version") != 1 or job.get("state") not in (
        "prepared",
        "prepared_waiting_transient_gate",
    ):
        return
    cid = job["claim_id"]
    claim = root / "claims" / cid
    intent = claim / "drain-release-intent.json"
    # A durable launch intent is an absolute boundary, even if no PID was saved.
    if worker in executor.active or any(
        job.get(key)
        for key in ("launch_intent", "launch_intent_at", "pid", "process_started_at")
    ):
        raise RuntimeError("Drain cannot release an ambiguous worker launch")
    if any(
        (claim / name).exists()
        for name in (
            "result.json",
            "submission-response.json",
            "giveup-response.json",
            "job-quarantine.json",
            "collaboration",
        )
    ) or list((claim / "work/recorded-checks").glob("check-*.json")):
        raise RuntimeError("Drain found execution evidence; claim retained")
    if intent.exists() or (root / "approval-blocker-current.json").exists():
        raise RuntimeError("Drain release requires review; no automatic retry")
    raw = (root / "assignments" / (worker + ".json")).read_bytes()
    assignment = json.loads(raw)
    digest = job.get("prepared_assignment_sha256")
    config = read(root / "run-config.json")
    metadata = read(claim / "worker-assignment.json")
    if not (
        digest
        and hashlib.sha256(raw).hexdigest() == digest
        and hashlib.sha256((claim / "assignment.json").read_bytes()).hexdigest()
        == digest
        and assignment.get("worker") == worker
        and assignment.get("claim_id") == cid
        and assignment.get("model") == config["model"]
        and cid not in config.get("protected_claim_ids", [])
        and read(claim / "claim.json").get("claim_id", cid) == cid
        and metadata.get("worker") == worker
        and metadata.get("claim_id") == cid
        and metadata.get("thread_source") == "codex_exec_direct"
        and metadata.get("thread_id") is None
    ):
        raise RuntimeError("Drain assignment identity failed; claim retained")
    from service import call, guard

    status = call("status", {})
    guard(status)
    owned = [
        row
        for row in status["open_claims"]
        if row.get("this_key") is True and row.get("claim_id", row.get("id")) == cid
    ]
    if len(owned) != 1:
        raise RuntimeError("Drain could not confirm claim ownership; claim retained")
    atomic(
        intent, {"claim_id": cid, "worker": worker, "state": "request_attempt_started"}
    )
    receipt = call(
        "giveup",
        {
            "claim_id": cid,
            "reason": reason,
        },
    )
    if not isinstance(receipt, dict) or not receipt:
        raise RuntimeError("Drain release response is ambiguous; retained for review")
    atomic(claim / "giveup-response.json", receipt)
    job["state"] = "released_before_launch"
    executor.journal_save()
