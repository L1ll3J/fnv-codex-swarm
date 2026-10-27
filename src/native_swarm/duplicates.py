"""Recognize a proven zero-compile duplicate without inventing a new check."""

import json
from pathlib import Path

from .group_check import digest
from .state import read


def duplicate_result(assignment, output, source_hash, exit_code):
    if exit_code != 2:
        return None
    try:
        cue = json.loads(output)
    except (ValueError, TypeError):
        return None
    if (
        cue.get("status") != "historical_failed_source_duplicate_no_compile"
        or cue.get("compiler_calls") != 0
        or cue.get("source_sha256") != source_hash
    ):
        return None
    claim = Path(assignment["claim_dir"]).resolve()
    history = read(claim / "work/attempt-history.json")
    if history.get("current_pack_sha256") != digest(claim / "pack.tar.gz"):
        raise ValueError("Duplicate history pack differs")
    for prior in history.get("entries", []):
        if (
            prior.get("manifest_path") != cue.get("prior_manifest")
            or not prior.get("same_current_pack")
            or prior.get("source_sha256") != source_hash
        ):
            continue
        row = read(Path(prior["manifest_path"]))
        if (
            row.get("checker_result", {}).get("status")
            not in ("wip", "rejected", "literal_address", "literal_mismatch")
            or row.get("source_sha256") != source_hash
            or row.get("pack_archive_sha256") != history["current_pack_sha256"]
            or not row.get("source_unchanged")
            or not row.get("checker_unchanged")
            or digest(row["source_path"]) != source_hash
            or digest(row["log_path"]) != row["log_sha256"]
            or row.get("checker_component_sha256") != prior.get("checker_components")
        ):
            raise ValueError("Duplicate evidence failed integrity checks")
        from .official_tools import context
        selected = context(assignment)
        if selected and (row.get("checker_component_sha256") != selected["components"] or row.get("official_tools_id") != assignment["official_tools"]["id"]):
            raise ValueError("Duplicate evidence belongs to another checker generation")
        # The pinned history wrapper has already verified the current components.
        return {
            "source_sha256": source_hash,
            "reused": True,
            "scope": "historical_checked_evidence",
            "manifest": prior["manifest_path"],
            "checker": row["checker_result"],
            "exit_code": row["checker_exit_code"],
            "match": False,
            "compiler_calls": 0,
        }
    raise ValueError("Duplicate refusal has no matching verified history entry")
