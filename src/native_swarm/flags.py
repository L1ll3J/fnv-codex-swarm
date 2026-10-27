"""Human-reviewed flags with durable uncertainty, separate from automated validation."""

import json
from pathlib import Path
import subprocess
import sys
import uuid

from .state import atomic, read
from .operator_store import now

CATEGORIES = ("index_problem", "pack_problem", "symbol_problem", "checker_problem")


def draft(report, category, note):
    if category not in CATEGORIES or not note.strip():
        raise ValueError("Choose an official category and supply a factual note.")
    claim = Path(report["evidence_path"])
    if not (claim / "claim.json").is_file():
        raise ValueError("Original claim evidence is missing.")
    # Historical address reporting does not release or change an active claim.
    payload = {
        "category": category,
        "note": note.strip(),
        "addresses": [report["address"]],
    }
    if report.get("compile_flag_proof"):
        from .flag_proof import verify
        proof = verify(report, report["compile_flag_proof"])
        if category != "pack_problem":
            raise ValueError("Compiler-flag proof uses the pack_problem category")
        payload.update(claim_id=claim.name, compile_flags=proof["compile_flags"], checkpoint_id=proof["checkpoint_id"])
    return payload


def submit(state, report, payload, invoke=subprocess.run):
    expected = draft(report, payload.get("category"), payload.get("note", ""))
    if payload != expected:
        raise ValueError("Flag payload differs from the reviewed draft.")
    directory = Path(state) / "flag-receipts"
    directory.mkdir(parents=True, exist_ok=True)
    previous = [read(p) for p in directory.glob("*.json")]
    if any(
        x.get("report_id") == report["id"]
        and x.get("state") in ("write_uncertain", "submitted")
        for x in previous
    ):
        raise ValueError(
            "This report already has a submitted or uncertain flag; no automatic retry."
        )
    path = directory / (uuid.uuid4().hex + ".json")
    receipt = {
        "report_id": report["id"],
        "payload": payload,
        "state": "write_uncertain",
        "at": now(),
    }
    atomic(path, receipt)
    run = Path(report["evidence_path"]).parent.parent
    code = "import json,sys; from service import call,guard; guard(call('status',{})); print(json.dumps(call('flag',json.load(sys.stdin))))"
    try:
        result = invoke(
            [sys.executable, "-c", code],
            cwd=run,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=120,
        )
        if result.returncode:
            raise ValueError(
                "Flag dispatch did not return a receipt. Inspect the saved uncertain operation; do not resubmit."
            )
        response = json.loads(result.stdout)
        receipt.update(state="submitted", response=response, finished_at=now())
        atomic(path, receipt)
    except Exception:
        # Once dispatch is possible, uncertainty is retained, never guessed away.
        raise
    return path
