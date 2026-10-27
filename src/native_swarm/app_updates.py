"""Stage trusted repository releases into new runtimes; activate on next launch."""

import argparse
from email.parser import BytesParser
import fcntl
import hashlib
from importlib.metadata import version as installed_version
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen
import venv
import zipfile

PACKAGE = "fnv-native-swarm"
REPOSITORY = "L1ll3J/fnv-codex-swarm"
ENTRYPOINT = "native-swarm"
HEALTH_MODULE = "native_swarm.app"
CHECK_SECONDS = 3600


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(temporary, path)


def release_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("Only stable three-part release versions are supported")
    return tuple(int(part) for part in value.split("."))


def github(route, *, binary=False):
    if not route.startswith("repos/" + REPOSITORY + "/"):
        raise ValueError("Update route differs from configured repository")
    accept = "application/octet-stream" if binary else "application/vnd.github+json"
    if shutil.which("gh"):
        result = subprocess.run(
            ["gh", "api", route, "-H", "Accept: " + accept],
            capture_output=True,
            timeout=90,
            check=False,
        )
        if result.returncode:
            raise ValueError(
                "GitHub release check/download failed; current application retained"
            )
        return result.stdout
    request = Request(
        "https://api.github.com/" + route,
        headers={"Accept": accept, "User-Agent": PACKAGE + "-updater"},
    )
    with urlopen(request, timeout=60) as response:
        data = response.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024:
        raise ValueError("Application update asset exceeds size limit")
    return data


def verify_wheel(path, expected_version):
    with zipfile.ZipFile(path) as archive:
        names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(names) != 1:
            raise ValueError("Ambiguous application wheel metadata")
        info = archive.getinfo(names[0])
        if info.file_size > 1024 * 1024:
            raise ValueError("Application wheel metadata exceeds size limit")
        metadata = BytesParser().parsebytes(archive.read(names[0]))
    normalize = lambda text: re.sub("[-_.]+", "-", text).lower()
    if (
        normalize(metadata.get("Name", "")) != normalize(PACKAGE)
        or metadata["Version"] != expected_version
    ):
        raise ValueError("Application wheel identity differs from release manifest")


def install(wheel, target):
    # Copy only the existing runtime, never its account/state directories.
    if (Path(sys.prefix) / "pyvenv.cfg").is_file():
        shutil.copytree(sys.prefix, target, symlinks=True)
    else:
        venv.EnvBuilder(with_pip=True).create(target)
    python = target / "bin/python"
    result = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--only-binary=:all:",
            "--disable-pip-version-check",
            str(wheel),
        ],
        capture_output=True,
        timeout=600,
        check=False,
    )
    if result.returncode:
        raise ValueError(
            "Application release installation failed; current runtime retained"
        )
    result = subprocess.run(
        [str(python), "-m", "pip", "check"],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise ValueError("Application release dependencies failed validation")
    code = (
        "from importlib.metadata import version; import "
        + HEALTH_MODULE
        + "; assert version("
        + repr(PACKAGE)
        + ") == "
        + repr(wheel_version(wheel))
    )
    result = subprocess.run(
        [str(python), "-I", "-c", code], capture_output=True, timeout=60, check=False
    )
    if result.returncode or not (target / "bin" / ENTRYPOINT).is_file():
        raise ValueError("Application release health check failed")


def wheel_version(path):
    with zipfile.ZipFile(path) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
        return BytesParser().parsebytes(archive.read(name))["Version"]


def check(
    state, *, fetch=github, installer=install, now=None, force=False, current=None
):
    state = Path(state).expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True)
    settings = read(state / "application-updates.json")
    if not settings.get("enabled", True):
        return {"state": "disabled"}
    now = time.time() if now is None else now
    directory = state / "application-runtime"
    directory.mkdir(exist_ok=True)
    if (
        Path(sys.prefix).resolve() == directory.resolve()
        or Path(sys.prefix).resolve() in directory.resolve().parents
    ):
        raise ValueError("Application update directory overlaps the running runtime")
    with (directory / "update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "checking"}
        health = read(state / "application-update-status.json")
        if not force and now < health.get("checked_at", 0) + CHECK_SECONDS:
            return health
        try:
            release = json.loads(fetch("repos/" + REPOSITORY + "/releases/latest"))
            if release.get("draft") or release.get("prerelease"):
                raise ValueError("Only published stable releases are eligible")
            assets = {a["name"]: a for a in release["assets"]}

            def asset(name):
                row = assets[name]
                if (
                    type(row.get("id")) is not int
                    or type(row.get("size")) is not int
                    or not 0 < row["size"] <= 64 * 1024 * 1024
                ):
                    raise ValueError("Invalid application release asset")
                raw = fetch(
                    "repos/" + REPOSITORY + "/releases/assets/" + str(row["id"]),
                    binary=True,
                )
                if len(raw) != row["size"]:
                    raise ValueError("Application release asset size differs")
                return raw

            manifest = json.loads(asset("release.json"))
            if (
                manifest.get("package") != PACKAGE
                or manifest.get("repository") != REPOSITORY
            ):
                raise ValueError("Release manifest identity differs")
            latest = release_version(manifest["version"])
            running = release_version(current or installed_version(PACKAGE))
            if latest <= running:
                result = {
                    "state": "current",
                    "version": current or installed_version(PACKAGE),
                    "checked_at": now,
                }
                atomic(state / "application-update-status.json", result)
                return result
            filename = manifest["wheel"]
            if (
                Path(filename).name != filename
                or not filename.endswith(".whl")
                or not re.fullmatch("[0-9a-f]{64}", manifest["sha256"])
            ):
                raise ValueError("Invalid wheel manifest")
            raw = asset(filename)
            if hashlib.sha256(raw).hexdigest() != manifest["sha256"]:
                raise ValueError("Application release checksum differs")
            target = directory / (manifest["version"] + "-" + manifest["sha256"][:12])
            intent_path = directory / (target.name + ".install-intent.json")
            complete = (
                read(target / "release.json") == manifest
                and (target / "bin" / ENTRYPOINT).is_file()
            )
            if target.exists() and not complete:
                published = read(directory / "current.json").get("executable")
                if (
                    read(intent_path) != manifest
                    or target.is_symlink()
                    or published == str(target / "bin" / ENTRYPOINT)
                ):
                    raise ValueError("Incomplete runtime needs operator review")
                # A prior interrupted install has no published users. Its durable
                # intent identifies precisely the updater-owned directory.
                shutil.rmtree(target)
            with tempfile.TemporaryDirectory(prefix=".stage-", dir=directory) as temp:
                stage = Path(temp)
                wheel = stage / filename
                wheel.write_bytes(raw)
                verify_wheel(wheel, manifest["version"])
                if not target.exists():
                    atomic(intent_path, manifest)
                    try:
                        # The pointer remains unpublished while installation runs.
                        # Installing at the final path keeps pip's shell wrappers
                        # valid even for long prefixes and paths containing spaces.
                        installer(wheel, target)
                        atomic(target / "release.json", manifest)
                        intent_path.unlink(missing_ok=True)
                    except Exception:
                        if target.exists():
                            shutil.rmtree(target)
                        raise
            if (
                read(target / "release.json") != manifest
                or not (target / "bin" / ENTRYPOINT).is_file()
            ):
                raise ValueError(
                    "Existing application runtime failed release identity check"
                )
            pointer = {
                "version": manifest["version"],
                "repository": REPOSITORY,
                "package": PACKAGE,
                "executable": str(target / "bin" / ENTRYPOINT),
                "sha256": manifest["sha256"],
            }
            atomic(directory / "current.json", pointer)
            result = {
                "state": "ready_on_next_launch",
                "version": manifest["version"],
                "checked_at": now,
            }
            atomic(state / "application-update-status.json", result)
            return result
        except Exception as exc:
            result = {
                "state": "update_failed",
                "checked_at": now,
                "error": type(exc).__name__ + ": " + str(exc),
            }
            atomic(state / "application-update-status.json", result)
            return result


def enabled(state):
    return read(Path(state).expanduser().resolve() / "application-updates.json").get(
        "enabled", True
    )


def set_enabled(state, value):
    atomic(
        Path(state).expanduser().resolve() / "application-updates.json",
        {"enabled": bool(value)},
    )


def start(state):
    state = Path(state).expanduser().resolve()
    if not read(state / "application-updates.json").get("enabled", True):
        return
    state.mkdir(parents=True, exist_ok=True)
    with (state / "application-update.log").open("ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", __name__, "--state-dir", str(state)],
            stdout=log,
            stderr=log,
            start_new_session=True,
        )


def activate(state, arguments=None):
    """Dispatch a healthy staged release only at the operator app entrypoint."""
    directory = Path(state).expanduser().resolve() / "application-runtime"
    pointer = read(directory / "current.json")
    if not pointer or not read(
        Path(state).expanduser().resolve() / "application-updates.json"
    ).get("enabled", True):
        return False
    try:
        if pointer.get("package") != PACKAGE or pointer.get("repository") != REPOSITORY:
            return False
        if release_version(pointer["version"]) <= release_version(
            installed_version(PACKAGE)
        ):
            return False
        executable = Path(pointer["executable"])
        if (
            not executable.is_absolute()
            or executable.is_symlink()
            or not executable.is_file()
        ):
            return False
        if (
            not executable.resolve().is_relative_to(directory.resolve())
            or executable.parent.name != "bin"
            or executable.name != ENTRYPOINT
        ):
            return False
        manifest = read(executable.parent.parent / "release.json")
        if any(
            manifest.get(key) != pointer.get(key)
            for key in ["version", "repository", "package", "sha256"]
        ):
            return False
        os.execv(
            str(executable),
            [str(executable), *(sys.argv[1:] if arguments is None else arguments)],
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    check(args.state_dir)


if __name__ == "__main__":
    main()
