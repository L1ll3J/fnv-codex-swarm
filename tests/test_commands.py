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
    monkeypatch.setattr("native_swarm.commands.operator_terminal", lambda claim: True)
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


def test_operator_stop_frees_capacity_despite_unsealed_metadata(tmp_path, monkeypatch):
    from native_swarm.assessment import occupied
    from native_swarm.group import group_terminal
    from native_swarm.commands import operator_terminal

    state, run, claim = fixture(tmp_path)
    atomic(
        run / "rolling-executor/journal.json",
        {"jobs": {"worker": {"claim_id": claim.name, "state": "running", "pid": 99999999}}},
    )
    atomic(claim / "session/state.json", {"streams_closed": False, "turns": []})
    atomic(claim / "collaboration/group.json", {"state": "running", "members": []})
    cfg = read(state / "config.json")
    assert occupied(cfg, state) == 1
    worker.stop_tree(claim, grace=0.01)
    atomic(claim / "giveup-response.json", {"released": True})
    assert operator_terminal(claim)
    assert occupied(cfg, state) == 0
    assert group_terminal(claim)
    assert read(claim / "session/state.json")["streams_closed"] is False
    assert read(claim / "collaboration/group.json")["state"] == "running"


@pytest.mark.parametrize(
    "location,field",
    [
        ("work/result.json", "permission_denied"),
        ("result.json", "approval_rejection"),
        ("session/state.json", "approval_rejection"),
        ("collaboration/peer-1/result.json", "permission_denied"),
    ],
)
def test_permission_rejection_blocks_handoff_before_model_launch(
    tmp_path, monkeypatch, location, field
):
    _, run, claim = fixture(tmp_path)
    atomic(claim / location, {field: True})
    if "peer-1" in location:
        atomic(
            claim / "collaboration/group.json",
            {"members": [{"workspace": str(claim / "collaboration/peer-1")}]},
        )
    launch = Mock()
    monkeypatch.setattr(worker.subprocess, "Popen", launch)
    worker.authored_handoff(claim, 15)
    launch.assert_not_called()
    assert (
        read(claim / "operator-authored-handoff.json")["status"] == "blocked_permission"
    )


def test_handoff_launch_reserves_physical_capacity_and_closes_reservation(
    tmp_path, monkeypatch
):
    from native_swarm.assessment import occupied

    state, run, claim = fixture(tmp_path)
    atomic(
        claim / "session/state.json", {"thread_id": "existing", "streams_closed": True}
    )
    (claim / "work").mkdir()
    monkeypatch.setattr("native_swarm.commands.operator_terminal", lambda _: True)
    monkeypatch.setattr(
        "native_swarm.session.command",
        lambda root, assignment, thread: [
            "codex",
            "exec",
            "-C",
            str(root),
            "resume",
            thread,
            "-",
        ],
    )
    cfg = read(state / "config.json")
    checked = []

    class Proc:
        pid = 99999999
        returncode = 0

        def communicate(self, value, timeout):
            checked.append(occupied(cfg, state))

        def poll(self):
            return 0

    def launch(*args, **kwargs):
        assert occupied(cfg, state) == 1  # Durable launch intent consumes the slot.
        return Proc()

    monkeypatch.setattr(worker.subprocess, "Popen", launch)
    monkeypatch.setattr(
        "native_swarm.commands.process_alive", lambda *a: False, raising=False
    )
    # A saved live PID counts even after the launch-intent window.
    monkeypatch.setattr(
        "native_swarm.state.process_alive", lambda pid, root: pid == 99999999
    )
    worker.authored_handoff(claim, 15)
    assert checked == [1]
    assert read(claim / "operator-handoff-process.json")["finished"] is True
    monkeypatch.setattr("native_swarm.state.process_alive", lambda *a: False)
    assert occupied(cfg, state) == 0


def test_full_capacity_skips_optional_handoff_without_extra_model(
    tmp_path, monkeypatch
):
    state, run, claim = fixture(tmp_path)
    atomic(claim / "session/state.json", {"thread_id": "existing"})
    (claim / "work").mkdir()
    monkeypatch.setattr("native_swarm.commands.operator_terminal", lambda _: True)
    monkeypatch.setattr(
        "native_swarm.session.command", lambda *a: ["codex", "-C", str(run)]
    )
    monkeypatch.setattr("native_swarm.assessment.occupied", lambda *a: 10)
    launch = Mock()
    monkeypatch.setattr(worker.subprocess, "Popen", launch)
    worker.authored_handoff(claim, 15)
    launch.assert_not_called()
    assert (
        read(claim / "operator-authored-handoff.json")["status"] == "skipped_capacity"
    )


def test_forced_kill_has_verified_terminal_capacity_evidence(tmp_path):
    import signal, subprocess, sys
    from native_swarm.assessment import occupied

    state, run, claim = fixture(tmp_path)
    code = 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(60)'
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(run), claim.name], stdout=subprocess.PIPE
    )
    try:
        assert proc.stdout.readline().strip() == b"ready"
        atomic(
            run / "rolling-executor/journal.json",
            {"jobs": {"worker": {"claim_id": claim.name, "pid": proc.pid}}},
        )
        atomic(
            claim / "session/state.json",
            {"streams_closed": False, "turns": [{"pid": proc.pid}]},
        )
        assert occupied(read(state / "config.json"), state) == 1
        worker.stop_tree(claim, grace=0.01)
        assert proc.wait(timeout=2) == -signal.SIGKILL
        assert occupied(read(state / "config.json"), state) == 0
        assert read(claim / "session/state.json")["streams_closed"] is False
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=3)
        proc.stdout.close()


def test_ambiguous_handoff_launch_keeps_capacity_until_observed(tmp_path):
    from native_swarm.assessment import occupied

    state, _, claim = fixture(tmp_path)
    atomic(
        claim / "operator-handoff-process.json",
        {
            "intent": True,
            "workspace": str(claim / "work"),
            "thread_id": "missing-thread",
        },
    )
    assert occupied(read(state / "config.json"), state) == 1
    with pytest.raises(RuntimeError, match="identity is incomplete"):
        worker.stop_tree(claim)
    assert occupied(read(state / "config.json"), state) == 1
    assert not read(claim / "operator-stop-terminal.json")


def test_new_handoff_rejection_latches_permission_stop(tmp_path, monkeypatch):
    import json

    state, run, claim = fixture(tmp_path)
    atomic(
        claim / "session/state.json", {"thread_id": "existing", "streams_closed": True}
    )
    (claim / "work").mkdir()
    monkeypatch.setattr("native_swarm.commands.operator_terminal", lambda _: True)
    monkeypatch.setattr(
        "native_swarm.session.command",
        lambda *a: ["codex", "exec", "-C", str(run), "resume", "existing", "-"],
    )

    class Proc:
        pid = 99999999
        returncode = 1

        def communicate(self, value, timeout):
            pass

        def poll(self):
            return 1

    def launch(argv, **kwargs):
        kwargs["stdout"].write(
            (json.dumps({"type": "error", "code": "approval_denied"}) + "\n").encode()
        )
        return Proc()

    monkeypatch.setattr(worker.subprocess, "Popen", launch)
    with pytest.raises(PermissionError, match="permission rejection"):
        worker.authored_handoff(claim, 15)
    assert read(run / "approval-blocker-current.json")["source"] == "operator-handoff"
    assert (run / "STOP_NEW_CLAIMS").exists()
    assert not operator_retiring(run)
    assert (
        read(claim / "operator-authored-handoff.json")["status"] == "blocked_permission"
    )


def test_rejected_handoff_does_not_release_or_resume_admissions(tmp_path, monkeypatch):
    state, _, claim = fixture(tmp_path)
    monkeypatch.setattr(
        "native_swarm.commands.subprocess.Popen",
        Mock(return_value=SimpleNamespace(pid=99999999)),
    )
    record = issue(state, [claim], "handoff", replace=True, handoff="always")[0]
    monkeypatch.setattr(worker, "stop_tree", Mock())
    monkeypatch.setattr(
        worker,
        "authored_handoff",
        Mock(side_effect=PermissionError("permission rejection")),
    )
    release = Mock()
    start = Mock()
    monkeypatch.setattr(worker, "release", release)
    monkeypatch.setattr(Backend, "start", start)
    worker.execute(state, record["id"])
    assert read(claim / "operator-command.json")["state"] == "review_required"
    release.assert_not_called()
    start.assert_not_called()


def test_cancelled_handoff_latches_rejection_before_propagating_stop(
    tmp_path, monkeypatch
):
    import json

    state, run, claim = fixture(tmp_path)
    atomic(
        claim / "session/state.json", {"thread_id": "existing", "streams_closed": True}
    )
    (claim / "work").mkdir()
    monkeypatch.setattr("native_swarm.commands.operator_terminal", lambda _: True)
    monkeypatch.setattr(
        "native_swarm.session.command",
        lambda *a: ["codex", "exec", "-C", str(run), "resume", "existing", "-"],
    )

    class Proc:
        pid = 99999999
        returncode = 1

        def communicate(self, *args, **kwargs):
            raise SystemExit(0)

        def poll(self):
            return 1

    def launch(argv, **kwargs):
        kwargs["stdout"].write(
            (json.dumps({"type": "error", "code": "approval_denied"}) + "\n").encode()
        )
        return Proc()

    monkeypatch.setattr(worker.subprocess, "Popen", launch)
    with pytest.raises(PermissionError, match="permission rejection"):
        worker.authored_handoff(claim, 15)
    assert read(claim / "operator-handoff-rejection.json")
    assert read(run / "approval-blocker-current.json")
    assert not operator_retiring(run)


def test_retirement_cannot_hide_sibling_failure_and_ack_is_snapshot_bound(tmp_path):
    state, run, claim = fixture(tmp_path)
    atomic(
        claim / "operator-command.json", {"action": "stop_now", "state": "completed"}
    )
    worker.stop_tree(claim)
    retire(run, claim.name)
    jobs = {"a": {"claim_id": claim.name, "state": "failed_review_required"}}
    atomic(run / "rolling-executor/journal.json", {"jobs": jobs})
    (run / "STOP_NEW_CLAIMS").write_text(
        "Rolling executor stopped: direct Codex process exited nonzero"
    )
    assert operator_retiring(
        run
    )  # The only failed job is explicitly and verifiably stopped.
    jobs["b"] = {"claim_id": "sibling", "state": "failed_review_required"}
    atomic(run / "rolling-executor/journal.json", {"jobs": jobs})
    assert not operator_retiring(run)
    retire(run, acknowledge=True)
    assert operator_retiring(run)
    jobs["c"] = {"claim_id": "another", "state": "failed_review_required"}
    atomic(run / "rolling-executor/journal.json", {"jobs": jobs})
    assert not operator_retiring(
        run
    )  # Same STOP text does not acknowledge a later failure.


def test_operator_drain_does_not_hide_failure_that_arrives_while_draining(tmp_path):
    _, run, _ = fixture(tmp_path)
    atomic(run / "operator-drain.json", {"source": "operator"})
    (run / "STOP_NEW_CLAIMS").write_text(
        "Rolling executor stopped: Operator requested a safe drain; no new or replacement workers"
    )
    atomic(
        run / "rolling-executor/journal.json",
        {"jobs": {"b": {"claim_id": "unrelated", "state": "failed_review_required"}}},
    )
    assert not operator_retiring(run)


def test_dead_controller_permission_stop_still_blocks_automatic_start(
    tmp_path, monkeypatch
):
    state, run, _ = fixture(tmp_path)
    atomic(run / "approval-blocker-current.json", {"source": "operator-handoff"})
    b = Backend(state)
    quota = Mock()
    monkeypatch.setattr(b, "quota", quota)
    with pytest.raises(ControlError, match="Permission stop retained"):
        b.start()
    quota.assert_not_called()
    with pytest.raises(ControlError, match="specific approval"):
        b.acknowledge_hold(run)


@pytest.mark.parametrize(
    "stream",
    [
        "session/turn-001.jsonl",
        "collaboration/peer-1/stdout.jsonl",
        "work/codex.stdout.jsonl",
    ],
)
def test_forced_stop_preserves_rejection_from_normal_worker_stream(tmp_path, stream):
    import json

    _, run, claim = fixture(tmp_path)
    path = claim / stream
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "error", "code": "approval_denied"}) + "\n")
    with pytest.raises(PermissionError, match="permission rejection"):
        worker.stop_tree(claim)
    assert read(run / "approval-blocker-current.json")["source"] == "operator-stop"
    assert not operator_retiring(run)
    assert read(claim / "operator-stop-terminal.json")["verified_stopped"] is True


def test_stop_orders_against_launch_and_refuses_unpublished_identity(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from native_swarm.group_check import locked

    state, run, claim = fixture(tmp_path)
    journal = run / "rolling-executor/journal.json"
    atomic(journal, {"jobs": {"worker": {"claim_id": claim.name, "state": "launch_intent"}}})
    with pytest.raises(RuntimeError, match="launch identity is incomplete"):
        worker.stop_tree(claim, grace=0.01)
    assert not (claim / "operator-stop-terminal.json").exists()
    entered = threading.Event()

    def stop():
        entered.set()
        worker.stop_tree(claim, grace=0.01)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with locked(state / "capacity.lock"):
            future = pool.submit(stop)
            assert entered.wait(2)
            assert not future.done()
            assert not (claim / "operator-stop-terminal.json").exists()
            atomic(journal, {"jobs": {"worker": {"claim_id": claim.name, "state": "running", "pid": 99999999}}})
        future.result(timeout=3)
    assert read(claim / "operator-stop-terminal.json")["verified_stopped"]


@pytest.mark.parametrize("marker", ["operator-drain.json", "claims/clm_test/operator-command.json"])
def test_controller_cannot_launch_after_operator_stop(tmp_path, monkeypatch, marker):
    import runpy
    import sys
    from types import ModuleType
    import native_swarm

    state, run, claim = fixture(tmp_path)
    random = ModuleType("random_executor")
    random.RandomExecutor = type("RandomExecutor", (), {})
    rolling = ModuleType("rolling_executor")
    rolling.read_json = read
    monkeypatch.setitem(sys.modules, "random_executor", random)
    monkeypatch.setitem(sys.modules, "rolling_executor", rolling)
    values = runpy.run_path(str(Path(native_swarm.__file__).with_name("controller.py")))
    controller = values["ManagedExecutor"]()
    controller.root = run
    controller.active = {}
    controller.usage_decision = lambda: {"target": 1}
    controller.journal = {"jobs": {"worker": {"state": "prepared"}}}
    controller.journal_save = Mock()
    controller.launch_supervised = Mock()
    atomic(run / marker, {"action": "stop_now"})
    assert controller.launch("worker", claim.name, {}) is False
    controller.launch_supervised.assert_not_called()
