"""Resolve a user-owned Codex CLI without depending on IDE-injected PATH."""

import os
from pathlib import Path
import platform
import re
import shutil

from .state import read


class CodexNotFound(RuntimeError):
    pass


def executable(path):
    path = Path(path).expanduser().absolute()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise CodexNotFound(
            f"Codex executable is unavailable: {path}. Set NATIVE_SWARM_CODEX to an executable Codex CLI path."
        )
    return path


def extension_roots(home):
    homes = [home]
    # On WSL, the Windows VS Code extension can contain the Linux CLI.
    windows_users = Path("/mnt/c/Users")
    if platform.system() == "Linux" and windows_users.is_dir():
        homes.extend(
            p
            for p in windows_users.iterdir()
            if p.is_dir() and p.name.casefold() == home.name.casefold()
        )
    for user_home in homes:
        for directory in (
            ".vscode",
            ".vscode-insiders",
            ".vscode-server",
            ".vscode-server-insiders",
        ):
            root = user_home / directory / "extensions"
            if root.is_dir():
                yield root


def resolve_codex():
    override = os.environ.get("NATIVE_SWARM_CODEX")
    if override:
        return executable(override)
    found = shutil.which("codex")
    if found:
        return executable(found)
    machine = platform.machine().lower()
    arch = (
        "aarch64"
        if machine in ("aarch64", "arm64")
        else "x86_64"
        if machine in ("x86_64", "amd64")
        else None
    )
    system = {"Linux": "linux", "Darwin": "macos"}.get(platform.system())
    candidates = []
    if arch and system:
        for root in extension_roots(Path.home()):
            for package in root.glob("openai.chatgpt-*"):
                match = re.match(r"openai\.chatgpt-(\d+(?:\.\d+)*)", package.name)
                path = package / "bin" / f"{system}-{arch}" / "codex"
                if match and path.is_file() and os.access(path, os.X_OK):
                    candidates.append(
                        (tuple(int(n) for n in match[1].split(".")), str(path), path)
                    )
    if candidates:
        return max(candidates)[2].absolute()
    raise CodexNotFound(
        "Codex executable not found. Install the Codex CLI or set NATIVE_SWARM_CODEX to its executable path. A compatible VS Code Codex extension is also supported. No claim was made."
    )


def run_codex(root):
    saved = read(Path(root) / "native-app-codex.json").get("executable")
    return executable(saved) if saved else resolve_codex()
