import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest

from native_swarm import app_updates as updates


def release():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(
            "fnv_native_swarm-0.6.0.dist-info/METADATA",
            "Name: fnv-native-swarm\nVersion: 0.6.0\n",
        )
    wheel = data.getvalue()
    filename = "fnv_native_swarm-0.6.0-py3-none-any.whl"
    manifest = {
        "package": updates.PACKAGE,
        "repository": updates.REPOSITORY,
        "version": "0.6.0",
        "wheel": filename,
        "sha256": hashlib.sha256(wheel).hexdigest(),
    }
    raw = json.dumps(manifest).encode()
    info = {
        "draft": False,
        "prerelease": False,
        "assets": [
            {"id": 1, "name": "release.json", "size": len(raw)},
            {"id": 2, "name": filename, "size": len(wheel)},
        ],
    }

    def fetch(route, **kwargs):
        if route.endswith("/latest"):
            return json.dumps(info).encode()
        return raw if route.endswith("/1") else wheel

    return manifest, fetch


def test_release_is_staged_and_pointer_published_only_after_health(tmp_path):
    manifest, fetch = release()
    calls = []

    def installer(wheel, target):
        assert not (tmp_path / "application-runtime/current.json").exists()
        updates.verify_wheel(wheel, "0.6.0")
        (target / "bin").mkdir(parents=True)
        exe = target / "bin" / updates.ENTRYPOINT
        exe.write_text("#!" + str(target / "bin/python") + "\npass\n")
        calls.append(target)

    result = updates.check(
        tmp_path, fetch=fetch, installer=installer, now=10000, current="0.5.0"
    )
    assert result["state"] == "ready_on_next_launch"
    pointer = updates.read(tmp_path / "application-runtime/current.json")
    exe = Path(pointer["executable"])
    assert exe.exists()
    assert str(calls[0]) in exe.read_text()
    assert ".stage-" not in exe.read_text()
    assert updates.read(exe.parent.parent / "release.json") == manifest
    assert (
        updates.check(
            tmp_path, fetch=lambda *_: pytest.fail("TTL"), now=10001, current="0.5.0"
        )
        == result
    )


def test_failed_install_or_hash_keeps_last_good_pointer(tmp_path):
    manifest, fetch = release()
    original = {"version": "0.5.0", "executable": "old"}
    updates.atomic(tmp_path / "application-runtime/current.json", original)

    def broken(*args):
        raise ValueError("health failed")

    result = updates.check(
        tmp_path, fetch=fetch, installer=broken, now=10000, current="0.5.0"
    )
    assert result["state"] == "update_failed"
    assert updates.read(tmp_path / "application-runtime/current.json") == original

    def corrupt(route, **kwargs):
        raw = fetch(route, **kwargs)
        return b"x" * len(raw) if route.endswith("/2") else raw

    assert (
        updates.check(
            tmp_path, fetch=corrupt, installer=broken, now=20000, current="0.5.0"
        )["state"]
        == "update_failed"
    )
    assert updates.read(tmp_path / "application-runtime/current.json") == original


def test_disabled_updates_and_activation_escape_are_rejected(tmp_path, monkeypatch):
    updates.atomic(tmp_path / "application-updates.json", {"enabled": False})
    assert (
        updates.check(tmp_path, fetch=lambda *_: pytest.fail("disabled"))["state"]
        == "disabled"
    )
    updates.atomic(tmp_path / "application-updates.json", {"enabled": True})
    monkeypatch.setattr(updates, "installed_version", lambda _: "0.5.0")
    updates.atomic(
        tmp_path / "application-runtime/current.json",
        {
            "version": "0.6.0",
            "package": updates.PACKAGE,
            "repository": updates.REPOSITORY,
            "executable": "/bin/sh",
        },
    )
    monkeypatch.setattr(updates.os, "execv", lambda *_: pytest.fail("escape"))
    assert updates.activate(tmp_path, []) is False


def test_version_downgrades_never_install(tmp_path):
    _, fetch = release()
    result = updates.check(
        tmp_path,
        fetch=fetch,
        installer=lambda *_: pytest.fail("downgrade"),
        now=10000,
        current="0.7.0",
    )
    assert result["state"] == "current"
    assert not (tmp_path / "application-runtime/current.json").exists()


def test_default_home_and_relative_state_activate_same_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    state = tmp_path / ".local/share/app"
    _, fetch = release()

    def installer(wheel, target):
        (target / "bin").mkdir(parents=True)
        (target / "bin" / updates.ENTRYPOINT).write_text("#!/bin/sh\nexit 0\n")

    assert (
        updates.check(
            "~/.local/share/app",
            fetch=fetch,
            installer=installer,
            now=10000,
            current="0.5.0",
        )["state"]
        == "ready_on_next_launch"
    )
    monkeypatch.setattr(updates, "installed_version", lambda _: "0.5.0")
    calls = []
    monkeypatch.setattr(updates.os, "execv", lambda *args: calls.append(args))
    assert updates.activate("~/.local/share/app", [])
    monkeypatch.chdir(tmp_path)
    assert updates.activate(".local/share/app", [])
    assert calls[0] == calls[1]
    assert Path(calls[0][0]).is_absolute()


def test_installer_uses_final_path_with_spaces_and_no_rename(tmp_path):
    _, fetch = release()
    state = tmp_path / ("long path " * 15)

    def installer(wheel, target):
        assert ".stage-" not in str(target)
        (target / "bin").mkdir(parents=True)
        (target / "bin" / updates.ENTRYPOINT).write_text(
            "#!/bin/sh\nexec '" + str(target / "bin/python") + '\' "$@"\n'
        )

    assert (
        updates.check(
            state, fetch=fetch, installer=installer, now=10000, current="0.5.0"
        )["state"]
        == "ready_on_next_launch"
    )
    pointer = updates.read(state / "application-runtime/current.json")
    assert (
        str(Path(pointer["executable"]).parent / "python")
        in Path(pointer["executable"]).read_text()
    )


def test_interrupted_unpublished_install_recovers(tmp_path):
    _, fetch = release()

    def interrupted(wheel, target):
        target.mkdir()
        (target / "partial").write_text("interrupted")
        raise SystemExit("simulated process interruption")

    with pytest.raises(SystemExit):
        updates.check(
            tmp_path, fetch=fetch, installer=interrupted, now=10000, current="0.5.0"
        )

    def recovered(wheel, target):
        assert not target.exists()
        (target / "bin").mkdir(parents=True)
        (target / "bin" / updates.ENTRYPOINT).write_text("#!/bin/sh\n")

    assert (
        updates.check(
            tmp_path, fetch=fetch, installer=recovered, now=20000, current="0.5.0"
        )["state"]
        == "ready_on_next_launch"
    )


def test_incomplete_published_target_is_never_removed(tmp_path):
    manifest, fetch = release()
    target = tmp_path / "application-runtime" / ("0.6.0-" + manifest["sha256"][:12])
    target.mkdir(parents=True)
    (target / "keep").write_text("preserved")
    updates.atomic(target.parent / (target.name + ".install-intent.json"), manifest)
    updates.atomic(
        target.parent / "current.json",
        {"executable": str(target / "bin" / updates.ENTRYPOINT)},
    )
    assert (
        updates.check(
            tmp_path,
            fetch=fetch,
            installer=lambda *_: pytest.fail("published"),
            now=20000,
            current="0.5.0",
        )["state"]
        == "update_failed"
    )
    assert (target / "keep").read_text() == "preserved"
