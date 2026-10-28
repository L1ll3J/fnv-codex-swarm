"""Operator lifecycle proof using local artifacts and CPU-only process doubles."""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from native_swarm.state import atomic, read
from native_swarm.backend import Backend, ControlError
from native_swarm.commands import (
    issue,
    operator_retiring,
    retire,
    retry,
    block_new_batch,
)
from native_swarm import command_worker as worker
from native_swarm.profiles import default_profile, validate_profiles
from native_swarm.routing import allowed, enqueue_target, set_route


def fixture(tmp_path):
    state, run = tmp_path / "state", tmp_path / "run"
    claim = run / "claims/clm_test"
    atomic(
        state / "config.json",
        {
            "managed_runs": [str(run)],
            "profiles": [default_profile(1)],
            "quota_path": str(state / "quota.json"),
        },
    )
    atomic(run / "native-app-profile.json", default_profile(1))
    atomic(
        run / "native-app-managed.json",
        {"state_dir": str(state), "command_center_version": 1},
    )
    atomic(claim / "claim.json", {"claim_id": claim.name, "address": "0x00ea3d60"})
    atomic(claim / "assignment.json", {"model": "gpt-6.1-sol", "claim_dir": str(claim)})
    return state, run, claim


def receipt(claim, number=1, score=20, match=False):
    source = claim / f"work/recorded-checks/candidate-{number:03d}.cpp"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("int f(){return 0;}")
    log = source.with_suffix(".log")
    log.write_text("actual compiler diagnostic")
    path = source.parent / f"check-{number:03d}.json"
    atomic(
        path,
        {
            "source_path": str(source),
            "log_path": str(log),
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
            "checker_exit_code": 0 if match else 1,
            "checker_result": {
                "status": "match" if match else "wip",
                "ndiff_insn": score,
            },
        },
    )
    return path


def test_issue_prevalidates_entire_scope_without_partial_commands(
    tmp_path, monkeypatch
):
    state, run, claim = fixture(tmp_path)
    other = run / "claims/second"
    atomic(other / "claim.json", {"id": "second"})
    atomic(other / "assignment.json", {"model": "gpt-6.1-sol"})
    atomic(other / "operator-command.json", {"state": "releasing"})
    launch = Mock(return_value=SimpleNamespace(pid=99999999))
    monkeypatch.setattr("native_swarm.commands.subprocess.Popen", launch)
    with pytest.raises(ValueError, match="already in progress"):
        issue(state, [claim, other], "handoff")
    assert not (claim / "operator-command.json").exists()
    launch.assert_not_called()


def test_stop_request_is_durable_and_reserves_idle_slot_before_dispatch(
    tmp_path, monkeypatch
):
    state, run, claim = fixture(tmp_path)
    launch = Mock(return_value=SimpleNamespace(pid=99999999))
    monkeypatch.setattr("native_swarm.commands.subprocess.Popen", launch)
    records = issue(state, [claim], "stop_now")
    assert records[0]["state"] == "requested"
    assert read(state / "idle-slots.json") == {"default": 1}
    assert operator_retiring(run)
    assert read(claim / "operator-command.json") == records[0]
    assert launch.call_args.kwargs["start_new_session"]
    with pytest.raises(RuntimeError, match="stopped new"):
        block_new_batch(claim)


def test_legacy_round_boundary_refuses_false_guarantee(tmp_path):
    state, _, claim = fixture(tmp_path)
    with pytest.raises(ValueError, match="older assignment"):
        issue(state, [claim], "finish_round")
    assert not (claim / "operator-command.json").exists()


def test_operator_retirement_never_masks_unrelated_failure(tmp_path):
    state, run, claim = fixture(tmp_path)
    retire(run, claim.name)
    (run / "STOP_NEW_CLAIMS").write_text("source integrity changed")
    assert not operator_retiring(run)
    retire(run, acknowledge=True)
    assert operator_retiring(run)
    (run / "STOP_NEW_CLAIMS").write_text("new service failure")
    assert not operator_retiring(run)


def test_stop_executor_releases_once_and_keeps_completed_state_on_refill_error(
    tmp_path, monkeypatch
):
    state, run, claim = fixture(tmp_path)
    monkeypatch.setattr(
        "native_swarm.commands.subprocess.Popen",
        Mock(return_value=SimpleNamespace(pid=99999999)),
    )
    record = issue(state, [claim], "stop_now", replace=True)[0]
    stop, release = Mock(), Mock()
    monkeypatch.setattr(worker, "stop_tree", stop)
    monkeypatch.setattr(worker, "release", release)
    monkeypatch.setattr(
        Backend, "start", Mock(side_effect=RuntimeError("quota unavailable"))
    )
    worker.execute(state, record["id"])
    result = read(claim / "operator-command.json")
    assert result["state"] == "completed"
    assert "refill needs attention" in result["refill_status"]
    worker.execute(state, record["id"])
    stop.assert_called_once()
    release.assert_called_once()


def test_uncertain_release_is_visible_and_never_automatically_retried(
    tmp_path, monkeypatch
):
    state, _, claim = fixture(tmp_path)
    monkeypatch.setattr(
        "native_swarm.commands.subprocess.Popen",
        Mock(return_value=SimpleNamespace(pid=99999999)),
    )
    record = issue(state, [claim], "stop_now")[0]
    monkeypatch.setattr(worker, "stop_tree", Mock())
    release = Mock(side_effect=RuntimeError("Release has no acknowledged result"))
    monkeypatch.setattr(worker, "release", release)
    worker.execute(state, record["id"])
    assert read(claim / "operator-command.json")["state"] == "review_required"
    assert not read(claim / "giveup-response.json")
    worker.execute(state, record["id"])
    release.assert_called_once()


def test_match_is_preserved_and_never_released_as_failure(tmp_path, monkeypatch):
    state, _, claim = fixture(tmp_path)
    path = receipt(claim, score=0, match=True)
    monkeypatch.setattr(
        "native_swarm.commands.subprocess.Popen",
        Mock(return_value=SimpleNamespace(pid=99999999)),
    )
    record = issue(state, [claim], "stop_now")[0]
    monkeypatch.setattr(worker, "stop_tree", Mock())
    release = Mock()
    monkeypatch.setattr(worker, "release", release)
    submit = Mock()
    monkeypatch.setattr(worker, "submit_match", submit)
    monkeypatch.setattr(Backend, "start", Mock(return_value="resumed"))
    worker.execute(state, record["id"])
    release.assert_not_called()
    submit.assert_called_once()
    assert read(claim / "operator-handoff.json")["best_receipt"] == str(path)
    assert read(claim / "operator-command.json")["state"] == "completed"


def test_preserve_ignores_tampered_source_or_log(tmp_path):
    _, _, claim = fixture(tmp_path)
    path = receipt(claim)
    source = Path(read(path)["source_path"])
    source.write_text("modified")
    assert worker.preserve(claim) is None


def test_explicit_retry_archives_intent_and_rechecks_ownership(tmp_path, monkeypatch):
    state, _, claim = fixture(tmp_path)
    monkeypatch.setattr(
        "native_swarm.commands.subprocess.Popen",
        Mock(return_value=SimpleNamespace(pid=99999999)),
    )
    record = issue(state, [claim], "stop_now")[0]
    atomic(claim / "operator-command.json", {**record, "state": "review_required"})
    atomic(claim / "operator-release-intent.json", {"command": record["id"]})
    retry(state, claim)
    assert not (claim / "operator-release-intent.json").exists()
    assert list((claim / "operator-release-history").glob("*.json"))
    assert read(claim / "operator-command.json")["state"] == "requested"


def test_routing_modes_and_address_normalization(tmp_path):
    state, _, _ = fixture(tmp_path)
    set_route(state, "0xea3d60", "exclusive", ["default"])
    assert not allowed(state, "0x00EA3D60", "another")
    assert allowed(state, "0x00ea3d60", "default")
    set_route(state, "0x00ea3d60", "preferred", ["default"])
    assert allowed(state, "0x00ea3d60", "another")
    set_route(state, "0xea3d60", "open", [])
    assert allowed(state, "0x00ea3d60", "another")


def test_failed_reroute_does_not_mutate_existing_route(tmp_path):
    state, _, _ = fixture(tmp_path)
    item = enqueue_target(state, "0xea3d60", 14124, "default", mode="exclusive")
    item["state"] = "assigned"
    atomic(state / "queue.json", [item])
    before = read(state / "routes.json")
    with pytest.raises(ValueError, match="active reservation"):
        enqueue_target(state, "0x00ea3d60", 14124, mode="open")
    assert read(state / "routes.json") == before


def test_direct_target_overrides_queue_first_without_enabling_other_history(tmp_path):
    from native_swarm.operator_store import OperatorStore

    state, _, _ = fixture(tmp_path)
    enqueue_target(state, "0xea3d60", 14124, "default", mode="exclusive")
    item = OperatorStore(state).reserve("default", "slot-1", queue_enabled=False)
    assert item["target"]["address"] == "0x00ea3d60"
    assert item["state"] == "reserved"


def test_ten_independent_agents_are_configurable_within_capacity():
    profile = {**default_profile(1), "group_size": 10, "team_mode": "isolated"}
    assert validate_profiles([profile], 10) == 10
    with pytest.raises(ValueError, match="configured maximum"):
        validate_profiles([profile], 9)


def test_assessor_capacity_is_independent_and_validated(tmp_path, monkeypatch):
    state, _, _ = fixture(tmp_path)
    monkeypatch.setattr("native_swarm.assessment.start", lambda _: "enabled")
    b = Backend(state)
    assert (
        b.assessor_settings(
            {
                "enabled": True,
                "capacity": 3,
                "model": "gpt-6.1-sol",
                "effort": "medium",
                "usage_policy": {"mode": "stop_queuing", "cutoff_used_percent": 100},
            }
        )
        == "enabled"
    )
    assert b.config["assessor"]["capacity"] == 3
    assert b.config.get("worker_capacity", 10) == 10
    with pytest.raises(ControlError):
        b.assessor_settings(
            {"enabled": True, "capacity": 0, "model": "gpt-6.1-sol", "effort": "medium"}
        )


def test_immediate_stop_supersedes_round_wait_without_double_idling(
    tmp_path, monkeypatch
):
    state, _, claim = fixture(tmp_path)
    driver = claim / "driver.py"
    driver.write_text("block_new_batch")
    a = read(claim / "assignment.json")
    a["checker_workflow"] = {"driver_path": str(driver)}
    atomic(claim / "assignment.json", a)
    monkeypatch.setattr(
        "native_swarm.commands.subprocess.Popen",
        Mock(return_value=SimpleNamespace(pid=99999999)),
    )
    first = issue(state, [claim], "finish_round")[0]
    stop = Mock()
    monkeypatch.setattr("native_swarm.commands.cancel_executor", stop)
    latest = issue(state, [claim], "stop_now")[0]
    stop.assert_called_once()
    assert read(state / "commands" / (first["id"] + ".json"))["state"] == "cancelled"
    assert read(claim / "operator-command.json")["id"] == latest["id"]
    assert read(state / "idle-slots.json") == {"default": 1}


def test_finish_round_allows_its_current_checks_but_closes_new_batches(
    tmp_path, monkeypatch
):
    from native_swarm.commands import block_new_check

    state, _, claim = fixture(tmp_path)
    atomic(
        claim / "operator-command.json",
        {"action": "finish_round", "state": "waiting_for_round"},
    )
    block_new_check(claim)
    with pytest.raises(RuntimeError):
        block_new_batch(claim)
    atomic(claim / "operator-command.json", {"action": "handoff", "state": "stopping"})
    with pytest.raises(RuntimeError):
        block_new_check(claim)


def test_stop_tree_signals_only_the_recorded_worker(tmp_path):
    import subprocess
    import sys

    state, run, claim = fixture(tmp_path)
    selected = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", str(run), claim.name]
    )
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        atomic(
            run / "rolling-executor/journal.json",
            {"jobs": {"worker": {"claim_id": claim.name, "pid": selected.pid}}},
        )
        worker.stop_tree(claim, grace=0.1)
        assert selected.wait(timeout=2) != 0
        assert other.poll() is None
    finally:
        for proc in (selected, other):
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=3)


def test_authored_handoff_resumes_existing_thread_with_approved_message(
    tmp_path, monkeypatch
):
    _, run, claim = fixture(tmp_path)
    (claim / "work").mkdir()
    atomic(claim / "session/state.json", {"thread_id": "original-thread"})
    seen = {}

    class Proc:
        pid = 99999999
        returncode = 0

        def communicate(self, value, timeout):
            seen["prompt"] = value
            seen["timeout"] = timeout

        def poll(self):
            return 0

    def launch(argv, **kwargs):
        seen["argv"] = argv
        return Proc()

    monkeypatch.setattr(
        "native_swarm.session.command",
        lambda root, assignment, thread: [
            "codex",
            "-C",
            str(root),
            "resume",
            thread,
            "-",
        ],
    )
    monkeypatch.setattr(worker.subprocess, "Popen", launch)
    worker.authored_handoff(claim, 15)
    assert seen["argv"][-2] == "original-thread"
    assert (
        seen["prompt"]
        == (Path(worker.__file__).parent / "prompts/operator_handoff.xml").read_bytes()
    )
    assert read(claim / "operator-authored-handoff.json")["status"] == "finished"


def test_zero_instruction_difference_without_match_is_not_submitted(
    tmp_path, monkeypatch
):
    state, _, claim = fixture(tmp_path)
    path = receipt(claim, score=0, match=False)
    assert not worker.is_match(worker.preserve(claim))
    monkeypatch.setattr(
        "native_swarm.commands.subprocess.Popen",
        Mock(return_value=SimpleNamespace(pid=99999999)),
    )
    record = issue(state, [claim], "stop_now")[0]
    monkeypatch.setattr(worker, "stop_tree", Mock())
    monkeypatch.setattr(Backend, "start", Mock(return_value="resumed"))
    submit = Mock()
    release = Mock()
    monkeypatch.setattr(worker, "submit_match", submit)
    monkeypatch.setattr(worker, "release", release)
    worker.execute(state, record["id"])
    submit.assert_not_called()
    release.assert_called_once()
    receipt(claim, number=2, score=0, match=True)
    assert worker.is_match(worker.preserve(claim))
    assert worker.preserve(claim)[1] != path


@pytest.mark.parametrize("field", ["approval_rejection", "permission_denied"])
def test_preserved_match_does_not_bypass_permission_stop(tmp_path, monkeypatch, field):
    _, _, claim = fixture(tmp_path)
    receipt(claim, match=True)
    atomic(claim / "work/result.json", {field: True})
    rpc = Mock()
    monkeypatch.setattr(worker.subprocess, "run", rpc)
    with pytest.raises(RuntimeError, match="rejection"):
        worker.submit_match(claim, {"id": "command"}, worker.preserve(claim))
    rpc.assert_not_called()


def match_environment(tmp_path):
    state, run, claim = fixture(tmp_path)
    receipt(claim, match=True)
    driver = run / "original-recorder.py"
    driver.write_text("original checked finish adapter")
    a = read(claim / "assignment.json")
    a["checker_workflow"] = {
        "single_check_driver": str(driver),
        "single_check_driver_sha256": hashlib.sha256(driver.read_bytes()).hexdigest(),
    }
    atomic(claim / "assignment.json", a)
    atomic(claim / "worker-assignment.json", {"thread_id": "real-thread"})
    atomic(
        run / "usage-current.json",
        {
            "threads": [
                {
                    "id": "real-thread",
                    "model": "gpt-6.1-sol",
                    "usage": {"total_tokens": 100},
                }
            ]
        },
    )
    return state, run, claim, driver


def test_recovered_match_uses_finish_evidence_audit_and_independent_validation(
    tmp_path, monkeypatch
):
    _, run, claim, driver = match_environment(tmp_path)
    calls = []

    def execute(argv, **kwargs):
        calls.append(argv)
        if str(driver) in argv:
            atomic(
                Path(argv[argv.index("--result-output") + 1]),
                {"match": True, "model": "gpt-6.1-sol"},
            )
        if str(run / "validate_submit.py") in argv:
            atomic(
                claim / "submission-response.json", {"submission_id": "actual-response"}
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", execute)
    worker.submit_match(claim, {"id": "operator"}, worker.preserve(claim))
    assert [Path(a[1]).name for a in calls[:2]] == ["usage.py", "register_usage.py"]
    assert calls[2][1] == str(driver) and "--finish" in calls[2]
    assert "audit_worker" in calls[3][2]
    assert calls[4][1] == str(run / "validate_submit.py")
    assert (
        read(claim / "submission-response.json")["submission_id"] == "actual-response"
    )
    worker.submit_match(claim, {"id": "operator"}, worker.preserve(claim))
    assert len(calls) == 5


def test_missing_model_evidence_stops_recovery_before_finish_or_submission(
    tmp_path, monkeypatch
):
    _, run, claim, _ = match_environment(tmp_path)
    atomic(
        run / "usage-current.json",
        {
            "threads": [
                {
                    "id": "real-thread",
                    "model": "different",
                    "usage": {"total_tokens": 100},
                }
            ]
        },
    )
    rpc = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(worker.subprocess, "run", rpc)
    with pytest.raises(RuntimeError, match="model/usage"):
        worker.submit_match(claim, {"id": "operator"}, worker.preserve(claim))
    assert rpc.call_count == 2
    assert not (claim / "operator-submit-intent.json").exists()


def test_failed_independent_gate_keeps_intent_without_retry(tmp_path, monkeypatch):
    _, run, claim, driver = match_environment(tmp_path)

    def execute(argv, **kwargs):
        if str(driver) in argv:
            atomic(Path(argv[argv.index("--result-output") + 1]), {"match": True})
        return SimpleNamespace(
            returncode=1 if str(run / "validate_submit.py") in argv else 0,
            stdout="",
            stderr="actual validation failed",
        )

    monkeypatch.setattr(worker.subprocess, "run", execute)
    with pytest.raises(RuntimeError, match="Independent MATCH"):
        worker.submit_match(claim, {"id": "operator"}, worker.preserve(claim))
    assert read(claim / "operator-submit-intent.json")["state"] == "dispatch_started"
    blocked = Mock()
    monkeypatch.setattr(worker.subprocess, "run", blocked)
    with pytest.raises(RuntimeError, match="uncertain"):
        worker.submit_match(claim, {"id": "operator"}, worker.preserve(claim))
    blocked.assert_not_called()


def test_superseding_with_refill_restores_its_reserved_slot(tmp_path, monkeypatch):
    state, _, claim = fixture(tmp_path)
    monkeypatch.setattr(
        "native_swarm.commands.subprocess.Popen",
        Mock(return_value=SimpleNamespace(pid=99999999)),
    )
    monkeypatch.setattr("native_swarm.commands.cancel_executor", Mock())
    issue(state, [claim], "stop_now", replace=False)
    assert read(state / "idle-slots.json") == {"default": 1}
    issue(state, [claim], "stop_now", replace=True)
    assert read(state / "idle-slots.json") == {}
    assert read(state / "idle-reservations.json") == {}


def test_completion_window_resumes_watcher_even_after_error(tmp_path):
    import subprocess
    import sys

    _, run, _ = fixture(tmp_path)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            str(run / "campaign_watch.py"),
        ]
    )
    atomic(run / "launch.json", {"completion_watch_pid": proc.pid})
    try:
        with pytest.raises(RuntimeError, match="test failure"):
            with worker.completion_window(run):
                assert (
                    read(run / "operator-completion-window.json")["state"] == "paused"
                )
                raise RuntimeError("test failure")
        assert read(run / "operator-completion-window.json")["state"] == "resumed"
        assert Path(f"/proc/{proc.pid}/stat").read_text().rsplit(")", 1)[1].split()[
            0
        ] not in ("T", "t")
    finally:
        proc.terminate()
        proc.wait(timeout=3)
