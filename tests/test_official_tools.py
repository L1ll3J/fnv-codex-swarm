import io
import json
import tarfile
from pathlib import Path

import pytest

from native_swarm import official_tools as tools
from native_swarm.state import atomic, read


class Downloads:
    def __init__(self):
        self.tip = "guidance v1"
        self.fail = None
        self.calls = []

    def __call__(self, url, path):
        self.calls.append(url)
        if path.name == self.fail:
            raise RuntimeError("interrupted download")
        if path.name == "fnvcheck.tar.gz":
            with tarfile.open(path, "w:gz") as archive:
                for name in ["fnvcheck.py", "fc_check.py"]:
                    raw = b"pass\n"
                    member = tarfile.TarInfo("fnvcheck/" + name)
                    member.size = len(raw)
                    archive.addfile(member, io.BytesIO(raw))
        elif path.name == "function_index.json":
            path.write_text('[{"address":"00401000"}]')
        elif path.name == "fnvdecomp_symbols.json":
            path.write_text('{"symbols":{"foo":"0x00401000"}}')
        else:
            path.write_text(self.tip)


def generation(root, download, now=100):
    return tools.current(root, download, now=now, compatibility=lambda _: 10)


def test_refresh_ttl_pinning_and_failed_update(tmp_path):
    download = Downloads()
    first = generation(tmp_path, download)
    assert len(download.calls) == 5
    assert generation(tmp_path, download, 101) == first
    download.tip = "guidance v2"
    second = generation(tmp_path, download, 401)
    assert second["id"] != first["id"]
    assert (tools.validate(first) / "compiler_tips.md").read_text() == "guidance v1"
    download.fail = "fnvdecomp_symbols.json"
    with pytest.raises(RuntimeError):
        generation(tmp_path, download, 800)
    assert read(tmp_path / "official-tools-current.json") == second
    assert read(tmp_path / "official-tools-health.json")["state"] == "update_failed"
    tools.validate(first)
    assert not list((tmp_path / "official-tools").glob(".download-*"))


def test_snapshot_tampering_and_incompatible_checker_fail_closed(tmp_path):
    download = Downloads()
    snapshot = generation(tmp_path, download)
    (Path(snapshot["directory"]) / "function_index.json").write_text("[]")
    with pytest.raises(ValueError, match="generation changed"):
        tools.current(tmp_path, download, now=101)
    with pytest.raises(ValueError):
        tools.current(tmp_path / "unsupported", download, now=100)
    assert not (tmp_path / "unsupported/official-tools-current.json").exists()


def test_assignment_uses_all_accepted_flag_sets_for_budget_and_is_pinned(
    tmp_path, monkeypatch
):
    download = Downloads()
    snapshot = generation(tmp_path, download)
    claim = tmp_path / "claims/claim"
    atomic(
        claim / "pack/pack.json",
        {"address": "0x00401000", "flags": ["/O2"], "accept_also": [["/Od", "/Ob1"]]},
    )
    monkeypatch.setattr(tools, "current", lambda *_args, **_kw: snapshot)
    result = tools.attach(tmp_path, "claim", {"max_plain_checks": 16})
    assert result["official_internal_retry_bound"] == 20
    assert result["max_compiler_invocations"] == 320
    assert result["official_tools"]["id"] == snapshot["id"]
    assert result["resource_paths"]["game_image"] == str(
        Path.home() / "fnvdecomp/game/FalloutNV.exe"
    )
    assert result["resource_paths"]["environment_script"] == str(
        Path.home() / "fnvdecomp/env.sh"
    )
    assert tools.context(result)["checker"] == Path(snapshot["directory"]) / "fnvcheck"
    assert all(
        "official-tools" in item
        for item in tools.environment(tools.context(result), claim)[1:4]
    )


def test_official_import_does_not_mutate_generation(tmp_path):
    import os
    import subprocess
    import sys

    download = Downloads()
    snapshot = generation(tmp_path, download)
    directory = Path(snapshot["directory"])
    value = {"directory": directory, "checker": directory / "fnvcheck"}
    env = dict(os.environ)
    env.update(
        item.split("=", 1) for item in tools.environment(value, tmp_path / "claim")
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0,sys.argv[1]); import fc_check",
            str(value["checker"]),
        ],
        env=env,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert not list(value["checker"].rglob("__pycache__"))
    tools.validate(snapshot)
