import io
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace

import pytest

from native_swarm import flag_proof
from native_swarm.flags import draft
from native_swarm.state import atomic, read


def setup_claim(tmp_path, monkeypatch):
    claim = tmp_path / "run/claims/claim"
    pack = {"address": "0x00401000", "size": 10, "flags": ["/O2"]}
    atomic(claim / "pack/pack.json", pack)
    with tarfile.open(claim / "pack.tar.gz", "w:gz") as archive:
        archive.add(claim / "pack/pack.json", arcname="pack.json")
    atomic(
        claim / "assignment.json",
        {
            "official_tools": {"id": "generation"},
            "max_plain_checks": 10,
            "max_compiler_invocations": 100,
        },
    )
    atomic(claim / "claim.json", {"address": pack["address"]})
    (claim / "work").mkdir()
    source = claim / "work/source.cpp"
    source.write_text("void f(){}")
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "fnvcheck.py").write_text("pass")
    monkeypatch.setattr(
        flag_proof,
        "context",
        lambda _: {"checker": tools, "directory": tmp_path, "bound": 10},
    )
    monkeypatch.setattr(flag_proof, "environment", lambda *_: [])
    original = flag_proof.digest
    monkeypatch.setattr(
        flag_proof,
        "digest",
        lambda p: (
            "host-component"
            if str(p).startswith(str(Path.home() / "fnvdecomp") + "/")
            else original(p)
        ),
    )
    report = {"id": "r", "address": pack["address"], "evidence_path": str(claim)}
    calls = []

    def service(run, tool, args):
        calls.append((tool, args))
        if tool == "status":
            return {"open_claims": [{"claim_id": "claim", "this_key": True}]}
        assert tool == "checkpoint"
        return {"checkpoint_id": "checkpoint-proof"}

    line = "MATCH  (under your --flags, not the pack's /O2: the site does not accept this yet. To report it, save this code with checkpoint and flag the function with compile_flags and checkpoint_id)\n"
    invoke = lambda *_, **kw: SimpleNamespace(returncode=0, stdout=line, stderr="")
    return claim, source, report, calls, service, invoke


def test_proof_is_checkpointed_and_never_submitted_as_solution(tmp_path, monkeypatch):
    claim, source, report, calls, service, invoke = setup_claim(tmp_path, monkeypatch)
    proof = flag_proof.prepare(report, source, ["/Od"], call=service, invoke=invoke)
    assert [t for t, _ in calls] == ["status", "checkpoint"]
    assert proof["state"] == "checked_checkpoint_saved"
    assert not (claim / "result.json").exists()
    assert not list((claim / "work/recorded-checks").glob("*.json"))
    assert flag_proof.verify(report, proof["path"]) == proof
    report["compile_flag_proof"] = proof["path"]
    payload = draft(report, "pack_problem", "Reproduced alternate-flags mismatch")
    assert (
        payload["compile_flags"] == ["/Od"]
        and payload["checkpoint_id"] == "checkpoint-proof"
    )
    assert payload["claim_id"] == "claim"
    Path(proof["source_path"]).chmod(0o600)
    Path(proof["source_path"]).write_text("changed")
    with pytest.raises(ValueError, match="evidence changed"):
        draft(report, "pack_problem", "changed")


def test_no_match_no_checkpoint_and_uncertain_checkpoint_not_replayed(
    tmp_path, monkeypatch
):
    claim, source, report, calls, service, invoke = setup_claim(tmp_path, monkeypatch)
    bad = lambda *_, **kw: SimpleNamespace(returncode=1, stdout="WIP\n", stderr="")
    with pytest.raises(ValueError, match="proof MATCH"):
        flag_proof.prepare(report, source, ["/Od"], call=service, invoke=bad)
    assert [t for t, _ in calls] == ["status"]

    def uncertain(run, tool, args):
        if tool == "checkpoint":
            raise TimeoutError("unknown")
        return service(run, tool, args)

    with pytest.raises(TimeoutError):
        flag_proof.prepare(report, source, ["/Od"], call=uncertain, invoke=invoke)
    with pytest.raises(ValueError, match="uncertain proof checkpoint"):
        flag_proof.prepare(report, source, ["/Od"], call=service, invoke=invoke)


def test_already_accepted_flags_and_changed_pack_cannot_generate_proof(
    tmp_path, monkeypatch
):
    claim, source, report, calls, service, invoke = setup_claim(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="already accepted"):
        flag_proof.prepare(report, source, ["/O2"], call=service, invoke=invoke)
    assert calls == []
    atomic(
        claim / "pack/pack.json",
        {"address": report["address"], "size": 11, "flags": ["/O2"]},
    )
    with pytest.raises(ValueError, match="pack contents changed"):
        flag_proof.prepare(report, source, ["/Od"], call=service, invoke=invoke)
    assert not any(t == "checkpoint" for t, _ in calls)


def test_interrupted_checker_dispatch_blocks_proof(tmp_path, monkeypatch):
    claim, source, report, calls, service, invoke = setup_claim(tmp_path, monkeypatch)
    records = claim / "work/recorded-checks"
    records.mkdir()
    (records / "candidate-001.cpp").write_text("void f(){}")

    def forbidden(*args, **kwargs):
        raise AssertionError("No compiler may run with an unresolved dispatch")

    with pytest.raises(ValueError, match="unresolved checker dispatch"):
        flag_proof.prepare(report, source, ["/Od"], call=service, invoke=forbidden)
    assert not any(tool == "checkpoint" for tool, _ in calls)


def test_sealed_collaboration_can_create_operator_proof(tmp_path, monkeypatch):
    claim, source, report, calls, service, invoke = setup_claim(tmp_path, monkeypatch)
    atomic(
        claim / "collaboration/group.json",
        {
            "state": "sealed",
            "streams_closed": True,
            "members": [{"exit_code": 0, "pid": None}],
        },
    )
    result = flag_proof.prepare(report, source, ["/Od"], call=service, invoke=invoke)
    assert result["state"] == "checked_checkpoint_saved"


def test_proof_timeout_is_displayed_by_dialog_handler(monkeypatch):
    import asyncio
    import subprocess
    from native_swarm.dialogs import FlagEditor

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("checker", 1)

    monkeypatch.setattr(flag_proof, "prepare", timeout)
    messages = []
    fields = {
        "#flag-proof-approve": SimpleNamespace(value=True),
        "#flag-source": SimpleNamespace(value="source.cpp"),
        "#flag-flags": SimpleNamespace(value="/Od"),
        "#flag-error": SimpleNamespace(update=messages.append),
    }
    dialog = SimpleNamespace(report={}, query_one=lambda key, *_: fields[key])
    event = SimpleNamespace(stop=lambda: None, button=SimpleNamespace(id="flag-proof"))
    asyncio.run(FlagEditor.on_button_pressed(dialog, event))
    assert messages and "timed out" in messages[0]
