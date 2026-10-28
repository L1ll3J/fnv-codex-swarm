from pathlib import Path
import struct

import pytest

from native_swarm import toolchains
from native_swarm import official_tools as tools


def compiler(root, name):
    path = root / name
    (path / "VC/bin").mkdir(parents=True)
    major, minor, build, revision = toolchains.VERSIONS[name]
    version = struct.pack(
        "<IIII", 0xFEEF04BD, 0x10000, major << 16 | minor, build << 16 | revision
    )
    for filename in toolchains.COMPONENTS:
        (path / "VC/bin" / filename).write_bytes(
            b"MZ" + (version if filename == "cl.exe" else filename.encode())
        )
    return path


def test_compiler_identity_rejects_wrong_build_and_changed_codegen(tmp_path):
    sp1 = compiler(tmp_path, "sp1")
    rtm = compiler(tmp_path, "rtm")
    records = {
        "sp1": toolchains.capture(sp1, "sp1"),
        "rtm": toolchains.capture(rtm, "rtm"),
    }
    toolchains.validate(records)
    with pytest.raises(ValueError, match="Wrong compiler version"):
        toolchains.capture(sp1, "rtm")
    (rtm / "VC/bin/c2.dll").write_bytes(b"changed")
    with pytest.raises(ValueError, match="generation changed"):
        toolchains.validate(records)


def test_rtm_is_bound_in_generation_and_environment(tmp_path, monkeypatch):
    from test_official_tools import Downloads

    sp1 = compiler(tmp_path, "sp1")
    rtm = compiler(tmp_path, "rtm")
    records = {
        "sp1": toolchains.capture(sp1, "sp1"),
        "rtm": toolchains.capture(rtm, "rtm"),
    }
    monkeypatch.setattr(toolchains, "discover", lambda _: records)
    first = tools.current(tmp_path, Downloads(), now=100, compatibility=lambda _: 20)
    assert first["manifest"]["compiler_calls_per_flag_set"] == 20
    assert first["manifest"]["toolchains"] == records
    value = {
        "directory": Path(first["directory"]),
        "checker": Path(first["directory"]) / "fnvcheck",
        "toolchains": records,
    }
    env = dict(s.split("=", 1) for s in tools.environment(value, tmp_path / "claim"))
    assert env["FNV_MSVC9_ROOT"] == str(sp1)
    assert env["FNVCHECK_RTM_ROOT"] == str(rtm)
    second_records = {"sp1": records["sp1"]}
    monkeypatch.setattr(toolchains, "discover", lambda _: second_records)
    second = tools.current(tmp_path, Downloads(), now=500, compatibility=lambda _: 20)
    assert second["id"] != first["id"]
    assert "FNVCHECK_RTM_ROOT=" in tools.environment(
        {"directory": tmp_path, "checker": tmp_path}, tmp_path
    )


def test_discover_vc8_binds_identity_and_clears_ambient_compiler(tmp_path):
    from native_swarm.state import atomic

    roots = {name: str(compiler(tmp_path, name)) for name in toolchains.VERSIONS}
    atomic(tmp_path / "compiler-toolchains.json", roots)
    records = toolchains.discover(tmp_path)
    assert records["vc8"]["version"] == [14, 0, 50727, 762]
    toolchains.validate(records)
    env = dict(
        item.split("=", 1)
        for item in tools.environment(
            {"directory": tmp_path, "checker": tmp_path, "toolchains": records},
            tmp_path,
        )
    )
    assert env["FNVCHECK_VC8_ROOT"] == roots["vc8"]
    assert env["FNVCHECK_COMPILER"] == "sp1"
    legacy = tools.environment({"directory": tmp_path, "checker": tmp_path}, tmp_path)
    assert "FNVCHECK_VC8_ROOT=" in legacy
