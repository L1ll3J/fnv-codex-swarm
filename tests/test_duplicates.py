import json
from pathlib import Path
from types import SimpleNamespace
import shutil

from native_swarm.duplicates import duplicate_result
from native_swarm.group_check import digest, module
from native_swarm.profiles import default_profile
from native_swarm.runtime import configure
from native_swarm.state import atomic

TEMPLATE = Path(str(Path.home() / "fnvdecomp/work/swarm-native-random-v2-20261026"))


def test_historical_duplicate_then_new_candidate_does_not_invent_a_receipt(tmp_path):
    for name in (
        "run-config.json",
        "rolling_executor.py",
        "prepare_assignment.py",
        "resolve_ready.py",
        "recorded_batch.py",
    ):
        shutil.copy2(TEMPLATE / name, tmp_path / name)
    configure(tmp_path, default_profile(1))
    claim = tmp_path / "claims/current"
    claim.mkdir(parents=True)
    (claim / "pack.tar.gz").write_bytes(b"pack")
    prior = tmp_path / "prior"
    prior.mkdir()
    oldsource = prior / "source.cpp"
    oldsource.write_text("int f(){return 1;}")
    oldlog = prior / "log"
    oldlog.write_text("checked difference=4")
    manifest = prior / "check.json"
    atomic(
        manifest,
        {
            "source_sha256": digest(oldsource),
            "pack_archive_sha256": digest(claim / "pack.tar.gz"),
            "source_unchanged": True,
            "checker_unchanged": True,
            "source_path": str(oldsource),
            "log_path": str(oldlog),
            "log_sha256": digest(oldlog),
            "checker_component_sha256": {"checker": "hash"},
            "checker_exit_code": 1,
            "checker_result": {"status": "wip", "ndiff_insn": 4},
        },
    )
    atomic(
        claim / "work/attempt-history.json",
        {
            "current_pack_sha256": digest(claim / "pack.tar.gz"),
            "entries": [
                {
                    "manifest_path": str(manifest),
                    "same_current_pack": True,
                    "source_sha256": digest(oldsource),
                    "checker_components": {"checker": "hash"},
                }
            ],
        },
    )
    candidate = claim / "a.cpp"
    candidate.write_bytes(oldsource.read_bytes())
    second = claim / "b.cpp"
    second.write_text("int f(){return 2;}")
    driver = tmp_path / "driver.py"
    driver.write_text("pinned driver")
    assignment = {
        "claim_id": "current",
        "claim_dir": str(claim),
        "assignment_path": str(claim / "assignment.json"),
        "max_batch_rounds": 20,
        "checker_workflow": {
            "single_check_driver": str(driver),
            "single_check_driver_sha256": digest(driver),
        },
    }
    calls = []

    def invoke(cmd):
        calls.append(cmd)
        snapshot = Path(cmd[-1])
        if len(calls) == 1:
            cue = {
                "status": "historical_failed_source_duplicate_no_compile",
                "compiler_calls": 0,
                "source_sha256": digest(snapshot),
                "prior_manifest": str(manifest),
            }
            return SimpleNamespace(
                stdout=json.dumps(cue).encode(), stderr=b"", returncode=2
            )
        atomic(
            claim / "work/recorded-checks/check-001.json",
            {
                "claim_id": "current",
                "source_sha256": digest(snapshot),
                "source_unchanged": True,
                "checker_unchanged": True,
                "source_path": str(snapshot),
                "log_path": str(oldlog),
                "log_sha256": digest(oldlog),
                "checker_exit_code": 1,
                "checker_result": {"status": "wip", "ndiff_insn": 3},
            },
        )
        return SimpleNamespace(stdout=b"checked", stderr=b"", returncode=1)

    result = module(tmp_path / "recorded_batch.py").run_batch(
        assignment, "round1", [candidate, second], invoke=invoke
    )
    assert result["state"] == "complete" and not result["match"]
    assert (
        result["results"][0]["reused"] and result["results"][0]["compiler_calls"] == 0
    )
    assert result["results"][1]["manifest"].endswith("check-001.json")
    assert len(list((claim / "work/recorded-checks").glob("check-*.json"))) == 1
    assert (
        module(tmp_path / "recorded_batch.py").run_batch(
            assignment, "round1", [candidate, second], invoke=invoke
        )
        == result
    )
    assert len(calls) == 2
    assert (
        duplicate_result(assignment, b'{"status":"unknown"}', digest(candidate), 2)
        is None
    )
