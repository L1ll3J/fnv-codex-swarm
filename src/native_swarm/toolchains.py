"""Bind supported official compiler builds to an assignment generation."""

import hashlib
from pathlib import Path
import struct

from .state import read

VERSIONS = {
    "sp1": (15, 0, 30729, 1),
    "rtm": (15, 0, 21022, 8),
    "vc8": (14, 0, 50727, 762),
}
COMPONENTS = ("cl.exe", "c1.dll", "c1xx.dll", "c2.dll")


def capture(root, name):
    root = Path(root).expanduser().resolve(strict=True)
    files = {}
    for filename in COMPONENTS:
        path = root / "VC/bin" / filename
        if path.is_symlink() or not path.is_file():
            raise ValueError("Missing regular compiler component: " + filename)
        data = path.read_bytes()
        if filename == "cl.exe":
            marker = data.find(struct.pack("<I", 0xFEEF04BD))
            if marker < 0 or marker + 16 > len(data):
                raise ValueError("Compiler executable has no fixed version information")
            ms, ls = struct.unpack_from("<II", data, marker + 8)
            actual = (ms >> 16, ms & 65535, ls >> 16, ls & 65535)
            if actual != VERSIONS[name]:
                raise ValueError("Wrong compiler version for " + name)
        files["VC/bin/" + filename] = hashlib.sha256(data).hexdigest()
    return {"root": str(root), "version": list(VERSIONS[name]), "files": files}


def discover(state):
    configured = read(Path(state) / "compiler-toolchains.json")
    base = Path.home() / "fnvdecomp/tools"
    roots = {
        "sp1": configured.get("sp1", str(base / "msvc9")),
        "rtm": configured.get("rtm", str(base / "msvc9rtm")),
        "vc8": configured.get("vc8", str(base / "msvc8")),
    }
    result = {"sp1": capture(roots["sp1"], "sp1")}
    for name in ("rtm", "vc8"):
        if Path(roots[name]).expanduser().exists():
            result[name] = capture(roots[name], name)
    return result


def validate(toolchains):
    if toolchains and "sp1" not in toolchains:
        raise ValueError("Compiler generation has no primary SP1 identity")
    for name, saved in toolchains.items():
        if name not in VERSIONS or capture(saved["root"], name) != saved:
            raise ValueError("Recorded compiler generation changed")


def files(toolchains):
    return {
        str(Path(record["root"]) / relative): digest
        for record in toolchains.values()
        for relative, digest in record["files"].items()
    }
