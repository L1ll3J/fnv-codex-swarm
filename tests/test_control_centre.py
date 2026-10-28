from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from native_swarm.backend import Backend
from native_swarm.flags import draft, submit
from native_swarm.operator_store import OperatorStore
from native_swarm.profiles import default_profile, validate_profiles
from native_swarm.runtime import configure
from native_swarm.selection import ordered_functions
from native_swarm.state import atomic, read


def test_profile_allocation_counts_all_collaborators():
    a = {**default_profile(2), "group_size": 3}
    b = {
        **default_profile(4),
        "id": "other",
        "model": "gpt-6.1-terra",
        "effort": "high",
    }
    assert validate_profiles([a, b]) == 10
    b["instances"] = 5
    with pytest.raises(ValueError, match="maximum is 10"):
        validate_profiles([a, b])


def test_priority_reads_every_catalog_page_before_ranking():
    call = Mock(
        side_effect=[
            {"functions": [{"address": "0x1", "size": 700}], "next": "more"},
            {"functions": [{"address": "0x2", "size": 999}], "next": None},
        ]
    )
    rows = ordered_functions(call, ["512-1K"], "largest", lambda _: "512-1K")
    assert rows[0]["address"] == "0x2"
    assert call.call_args_list[1].args[1]["cursor"] == "more"
    assert all(c.args[0] == "list_functions" for c in call.call_args_list)


def test_catalog_cursor_loop_fails_without_any_claim():
    call = Mock(return_value={"functions": [], "next": "same"})
    with pytest.raises(ValueError, match="repeated"):
        ordered_functions(call, ["<=255"], "smallest", lambda _: "<=255")


def test_recently_returned_priority_combines_brackets_before_ranking():
    call = Mock(
        side_effect=[
            {
                "functions": [
                    {"address": "0x1", "size": 100},
                    {
                        "address": "0x2",
                        "size": 200,
                        "back_in_pool_at": "2026-10-27T01:00:00Z",
                    },
                ]
            },
            {
                "functions": [
                    {
                        "address": "0x3",
                        "size": 700,
                        "back_in_pool_at": "2026-10-28T01:00:00Z",
                    }
                ]
            },
        ]
    )
    rows = ordered_functions(
        call,
        ["<=255", "512-1K"],
        "recently returned",
        lambda n: "<=255" if n <= 255 else "512-1K",
    )
    assert [r["address"] for r in rows] == ["0x3", "0x2", "0x1"]
    assert all(c.args[1]["order"] == "returned" for c in call.call_args_list)


def test_queue_is_targeted_reserved_once_and_preserves_uncertainty(tmp_path):
    store = OperatorStore(tmp_path)
    atomic(
        tmp_path / "queue.json",
        [
            {
                "id": "one",
                "profile_id": "terra",
                "state": "queued",
                "target": {"address": "0x1"},
            }
        ],
    )
    assert store.reserve("sol", "worker") is None
    assert store.reserve("terra", "worker")["id"] == "one"
    assert store.reserve("terra", "other") is None
    with pytest.raises(ValueError):
        store.remove("one")
    store.update("one", "review_required")
    assert store.reserve("terra", "other") is None


def test_operator_prompt_versions_are_exact_and_do_not_modify_default(tmp_path):
    default = tmp_path / "default.xml"
    default.write_text("<original/>")
    store = OperatorStore(tmp_path / "state")
    version = store.save_prompt("<operator>My wording</operator>")
    assert store.prompt(default) == "<operator>My wording</operator>"
    assert default.read_text() == "<original/>"
    assert (
        store.state / "prompt-history" / (version + ".xml")
    ).read_text() == store.prompt(default)


def test_flag_requires_exact_payload_and_never_retries_unknown_write(tmp_path):
    claim = tmp_path / "run/claims/claim"
    atomic(claim / "claim.json", {"address": "0x1234"})
    report = {"id": "proposal", "address": "0x1234", "evidence_path": str(claim)}
    payload = draft(report, "checker_problem", "Checked evidence")
    external = Mock(side_effect=TimeoutError("unknown write"))
    with pytest.raises(TimeoutError):
        submit(tmp_path / "state", report, payload, external)
    with pytest.raises(ValueError, match="uncertain"):
        submit(tmp_path / "state", report, payload, external)
    assert external.call_count == 1
    receipt = read(next((tmp_path / "state/flag-receipts").glob("*.json")))
    assert receipt["state"] == "write_uncertain"
    assert "claim_id" not in receipt["payload"]


def test_flag_stores_real_service_receipt(tmp_path):
    claim = tmp_path / "run/claims/claim"
    atomic(claim / "claim.json", {})
    report = {"id": "proposal", "address": "0x1234", "evidence_path": str(claim)}
    external = Mock(
        return_value=SimpleNamespace(returncode=0, stdout='{"flag_id":"real-receipt"}')
    )
    path = submit(
        tmp_path / "state",
        report,
        draft(report, "pack_problem", "Reproduced pack error"),
        external,
    )
    assert read(path)["response"]["flag_id"] == "real-receipt"


def test_approved_generalist_text_is_scoped_to_groups():
    prompts = Path(__file__).parents[1] / "src/native_swarm/prompts"
    independent = (prompts / "independent.xml").read_text()
    group = (prompts / "generalists.xml").read_text()
    assert "<collaboration>" not in independent
    assert "There are no fixed lead or specialist roles." in group
    assert (
        "No secrets, network, service/API operations or other workers' candidates."
        in independent
    )
    assert (
        "Read other workers' candidates only when the assignment identifies them as members of the same collaboration group"
        in group
    )
    assert "allocated per member; all members use one controller-owned lease" in group


@pytest.mark.skipif(
    not (Path.home() / "fnvdecomp/work/swarm-native-random-v2-20261026").exists(),
    reason="Local native template is not installed",
)
def test_runtime_configuration_binds_actual_model_and_effort(tmp_path):
    # Use the pinned template sites, without running native code or services.
    import shutil

    template = Path(str(Path.home() / "fnvdecomp/work/swarm-native-random-v2-20261026"))
    for name in (
        "run-config.json",
        "prepare_assignment.py",
        "rolling_executor.py",
        "resolve_ready.py",
    ):
        shutil.copy2(template / name, tmp_path / name)
    configure(
        tmp_path, {**default_profile(2), "model": "gpt-6.1-terra", "effort": "high"}
    )
    assert read(tmp_path / "run-config.json")["model"] == "gpt-6.1-terra"
    assert (
        'assignment["reasoning_effort"]'
        in (tmp_path / "rolling_executor.py").read_text()
    )
    assert "['reasoning_effort']" in (tmp_path / "prepare_assignment.py").read_text()


def test_legacy_worker_setting_cannot_resize_group_profiles(tmp_path):
    state = tmp_path / "state"
    runs = [tmp_path / "a", tmp_path / "b"]
    profiles = [
        {**default_profile(1), "id": "a", "group_size": 4},
        {**default_profile(2), "id": "b", "group_size": 3},
    ]
    for run, profile in zip(runs, profiles):
        atomic(run / "native-app-managed.json", {})
        atomic(run / "native-app-profile.json", profile)
        atomic(run / "operator-control.json", {"target_pool": profile["instances"]})
    atomic(
        state / "config.json",
        {
            "profiles": profiles,
            "managed_runs": list(map(str, runs)),
            "current_run": str(runs[-1]),
        },
    )
    backend = Backend(state)
    backend.settings(10, ["<=255"])
    assert [read(r / "operator-control.json")["target_pool"] for r in runs] == [1, 2]
    assert all(
        "native_app_bands" not in read(r / "operator-control.json") for r in runs
    )
    assert backend.config["workers"] == 10


def test_queue_closed_receipt_allows_remove_and_requeue(tmp_path):
    store = OperatorStore(tmp_path / "state")
    run = tmp_path / "run"
    atomic(
        store.state / "queue.json",
        [
            {
                "id": "one",
                "state": "assigned",
                "run": str(run),
                "claim_id": "claim",
                "target": {"address": "0x1"},
            }
        ],
    )
    assert store.queue()[0]["state"] == "assigned"
    atomic(run / "claims/claim/giveup-response.json", {"released": True})
    assert store.queue()[0]["state"] == "completed"
    store.remove("one")
    assert store.queue() == []


def test_profile_cannot_be_deleted_with_pending_targeted_queue(tmp_path):
    backend = Backend(tmp_path)
    atomic(
        tmp_path / "queue.json",
        [
            {
                "id": "one",
                "profile_id": "old",
                "state": "queued",
                "target": {"address": "0x1"},
            }
        ],
    )
    with pytest.raises(RuntimeError, match="queued or unresolved"):
        backend.save_profiles([default_profile(2)])


def test_targeted_enqueue_revalidates_profile_under_shared_control_lock(tmp_path):
    claim = tmp_path / "claim"
    source = claim / "work/source.cpp"
    source.parent.mkdir(parents=True)
    source.write_text("int f(){}")
    log = claim / "work/source.log"
    log.write_text("checked output")
    import hashlib

    atomic(claim / "claim.json", {"address": "0x1", "size": 100})
    atomic(
        claim / "work/recorded-checks/check-001.json",
        {
            "source_path": str(source),
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "log_path": str(log),
            "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
            "checker_result": {"status": "wip", "ndiff_insn": 2},
        },
    )
    store = OperatorStore(tmp_path / "state")
    atomic(store.state / "config.json", {"profiles": [{"id": "current"}]})
    with pytest.raises(ValueError, match="no longer exists"):
        store.enqueue(claim, "deleted")
    assert store.queue() == []
    assert store.enqueue(claim, "current")["profile_id"] == "current"


def test_concurrent_prompt_save_cannot_publish_another_writers_text(
    tmp_path, monkeypatch
):
    import os
    import threading

    original_fsync = os.fsync
    inside = threading.Event()
    release = threading.Event()
    output = []
    store = OperatorStore(tmp_path)

    def slow_fsync(fd):
        original_fsync(fd)
        if threading.current_thread().name == "first-writer" and not inside.is_set():
            inside.set()
            assert release.wait(5)

    monkeypatch.setattr(os, "fsync", slow_fsync)
    thread = threading.Thread(
        target=lambda: output.append(store.save_prompt("first operator text")),
        name="first-writer",
    )
    thread.start()
    try:
        assert inside.wait(5)
        with pytest.raises(ValueError, match="Another app"):
            OperatorStore(tmp_path).save_prompt("second operator text")
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert (tmp_path / "prompt.xml").read_text() == "first operator text"
    assert read(tmp_path / "prompt.xml.metadata.json")["sha256"] == output[0]
    assert (
        tmp_path / "prompt-history" / (output[0] + ".xml")
    ).read_text() == "first operator text"


def test_queue_uses_lowest_recorded_diff_even_when_final_handoff_chose_worse(tmp_path):
    import hashlib

    claim = tmp_path / "claim"
    atomic(claim / "claim.json", {"address": "0x1", "size": 100})
    records = []
    for index, differences in enumerate((2, 9), 1):
        source = claim / f"work/source-{index}.cpp"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"int f(){{return {index};}}")
        log = source.with_suffix(".log")
        log.write_text("checked")
        record = claim / f"work/recorded-checks/check-{index:03d}.json"
        atomic(
            record,
            {
                "source_path": str(source),
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "log_path": str(log),
                "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                "checker_result": {"status": "wip", "ndiff_insn": differences},
            },
        )
        records.append(record)
    atomic(claim / "work/result.json", {"recorded_check_manifest": str(records[1])})
    item = OperatorStore(tmp_path / "state").enqueue(claim)
    assert item["target"]["instruction_differences"] == 2
    assert item["target"]["record_path"] == str(records[0])


def test_stale_profile_save_cannot_delete_another_apps_changes(tmp_path):
    from native_swarm.backend import ControlError

    backend = Backend(tmp_path)
    original = backend.profiles()
    newer = original + [{**default_profile(1), "id": "new", "name": "New"}]
    newer[0] = {**newer[0], "instances": 1}
    backend.save_profiles(newer, expected=original)
    with pytest.raises(ControlError, match="another app"):
        backend.save_profiles(
            [{**original[0], "priority": "largest"}], expected=original
        )
    assert backend.profiles() == newer
