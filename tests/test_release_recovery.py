"""Verify actual native release payload generation without making service calls."""

import runpy
import shutil
import sys
import types
from pathlib import Path

import pytest

from native_swarm.app import report_visible
from native_swarm.backend import Backend
from native_swarm.profiles import default_profile
from native_swarm.reasons import one_line_reason
from native_swarm.runtime import configure
from native_swarm.state import atomic, read

TEMPLATE = Path(str(Path.home() / "fnvdecomp/work/swarm-native-random-v2-20261026"))


@pytest.mark.parametrize(
    "reason", [None, "\n\n", "line one\nline two\r\nline three", " x " * 1000]
)
def test_release_reason_is_nonempty_bounded_and_one_line(reason):
    value = one_line_reason(reason)
    assert value and len(value) <= 900 and value == " ".join(value.split())


@pytest.mark.skipif(
    not TEMPLATE.exists(), reason="Local native template is not installed"
)
@pytest.mark.parametrize("reason", [None, "\n\n", "detailed\nteam handoff"])
def test_actual_release_keeps_artifacts_and_best_code_while_normalizing_payload(
    tmp_path, monkeypatch, reason
):
    for name in (
        "run-config.json",
        "rolling_executor.py",
        "prepare_assignment.py",
        "resolve_ready.py",
        "release.py",
    ):
        shutil.copy2(TEMPLATE / name, tmp_path / name)
    configure(tmp_path, default_profile(1))
    claim = tmp_path / "claims/test"
    atomic(claim / "claim.json", {})
    atomic(claim / "assignment.json", {})
    candidate = claim / "best.cpp"
    candidate.write_text("int f(){return 1;}")
    result = {
        "residual_issue": reason,
        "model": "gpt-6.1-sol",
        "final_candidate_path": str(candidate),
    }
    atomic(claim / "result.json", result)
    before = (claim / "result.json").read_bytes()
    service = types.ModuleType("service")
    service.ROOT = tmp_path
    service.CONFIG = {"model": "gpt-6.1-sol"}
    service.save = lambda p, v: atomic(tmp_path / p, v)
    calls = []
    service.call = lambda tool, args: calls.append((tool, args)) or {"released": True}
    monkeypatch.setitem(sys.modules, "service", service)
    monkeypatch.setitem(
        sys.modules, "expiry_cleanup", types.SimpleNamespace(read_json=read)
    )
    monkeypatch.setattr(sys, "argv", ["release.py", "test"])
    runpy.run_path(str(tmp_path / "release.py"))
    assert calls[0][0] == "giveup"
    assert calls[0][1]["reason"] == one_line_reason(reason)
    assert calls[0][1]["best_code"] == candidate.read_text()
    assert (claim / "result.json").read_bytes() == before


def test_operator_observations_leave_queue_but_remain_in_history():
    report = {"assessment": {"decision": "observation"}}
    assert not report_visible({}, {"status": "observation"})
    assert not report_visible({}, {"status": "resolved"})
    assert report_visible(report, {"status": "established"})
    assert report_visible(report, {"status": "observation"}, history=True)
    assert report_visible({"assessment": {"decision": "inconclusive"}}, {})


def test_local_review_keeps_provenance_and_reason(tmp_path):
    backend = Backend(tmp_path)
    backend.review(
        "report",
        "observation",
        note="A valid workaround exists",
        evidence_refs=["check-014.json"],
    )
    review = read(tmp_path / "reviews.json")["report"]
    assert (
        review["source"] == "operator" and review["note"] == "A valid workaround exists"
    )
    assert review["evidence_refs"] == ["check-014.json"]


def test_status_only_review_preserves_existing_notes_and_evidence(tmp_path):
    backend = Backend(tmp_path)
    backend.review(
        "report",
        "observation",
        note="Checked source workaround",
        evidence_refs=["check-014.json"],
    )
    backend.review("report", "resolved")
    saved = read(tmp_path / "reviews.json")["report"]
    assert saved["status"] == "resolved"
    assert saved["note"] == "Checked source workaround"
    assert saved["evidence_refs"] == ["check-014.json"]
