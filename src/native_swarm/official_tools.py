"""Content-addressed official tool generations; never replace active inputs."""

import ast
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import time

from .group_check import locked
from .state import atomic, read

BASE = "https://fnvdecomp.com/pack-data/"
FILES = (
    "fnvcheck.tar.gz",
    "function_index.json",
    "fnvdecomp_symbols.json",
    "compiler_tips.md",
    "large_functions.md",
)
MAX_AGE = 300
# Only compilation control-flow changes require an accounting compatibility audit.
# Data, diagnostics and other checker files remain independently content-bound.
AUDITS = {
    (
        "92090707f0b8803ced2818f35482ea315ab6af007f337b928e85d531f662b3d5",
        "a2245d11482bddfa65c7b2dfc40e57989172ac46fc795487b25cc8ed0a60732b",
    ): 4,
    (
        "f3fc79495063c77d40f06c1e89d99f062ff805576e658f13c7758c4be0f7368a",
        "0ff871a2d83ae63e323c730d582dc8157e7439682d27804fd053cdc448c57720",
    ): 10,
}
COMPILE_COMPONENTS = {
    "fc_common.py": "248d567380df6f5f4ad76dcc110734bb2dd6f6079965825b8fc41c0154a97b0f",
    "msvc9.py": "5a995286cea82819b335d84e8383b071f5836f9f13ffe9165b94cb4cc7909946",
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree(root):
    rows = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_symlink() or not (p.is_dir() or p.is_file()):
            raise ValueError("Non-regular official tool input")
        if p.is_file():
            rows[str(p.relative_to(root))] = digest(p)
    return rows


def audit(checker):
    names = ("fnvcheck.py", "fc_check.py", "fc_common.py", "msvc9.py")
    if any(not (checker / name).is_file() for name in names):
        raise ValueError("Missing official compiler adapter; no admission")
    key = tuple(digest(checker / name) for name in names)
    known = {
        (
            *pair,
            COMPILE_COMPONENTS["fc_common.py"],
            COMPILE_COMPONENTS["msvc9.py"],
        ): bound
        for pair, bound in AUDITS.items()
    }
    known[
        (
            "a4e945c44b49f02ab007a3e4ae4cafc5896a2ec2ca3147d048017bb578859cfb",
            "8a3e6353bc82ab3537a4967d5702e6139654eb2b6f75851fbf91a684cbd0c78b",
            "0e3628d9b9ad27fd4adf9c8218c0a569e0e27c294e11ffe6f377767ec9962db5",
            COMPILE_COMPONENTS["msvc9.py"],
        )
    ] = 20
    # 28 October: VC8 pack selection, all-code RTM fallback and whole-function
    # diagnostics. Each compiler retains the initial call plus three repair
    # iterations (at most three calls each); two compilers => at most 20.
    known[
        (
            "190fa74a97f3696f9fe7e338ad1db55512a053b10ed5e9b4b933bd3a2cc1acab",
            "855da0215bf0281d39da8adcb17c3cb880f3f0c8b1efc898ff3c2be047fde69b",
            "837ddc3e4545914ae15bc599ccf880ad06efd35f5135d80a14c09c1084796725",
            "6310083197c8fbd8236145f6a5ac6c50d1edbf1f8aa703b86abd9a94c3280692",
        )
    ] = 20
    if key not in known:
        raise ValueError(
            "New official checker downloaded; compilation accounting compatibility review required before admission"
        )
    return known[key]


def validate(snapshot):
    directory = Path(snapshot["directory"])
    manifest = snapshot["manifest"]
    identifier = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()
    ).hexdigest()
    if (
        snapshot["id"] != identifier
        or directory.name != identifier
        or directory.is_symlink()
    ):
        raise ValueError("Official tool generation identity differs")
    if tree(directory / "fnvcheck") != manifest["checker"]:
        raise ValueError("Official checker generation changed")
    if any(
        (directory / name).is_symlink() or digest(directory / name) != value
        for name, value in manifest["files"].items()
    ):
        raise ValueError("Official data generation changed")
    from .toolchains import validate as validate_toolchains

    validate_toolchains(manifest.get("toolchains", {}))
    return directory


def current(root, download=None, *, now=None, force=False, compatibility=audit):
    root = Path(root)
    state = Path(read(root / "native-app-managed.json").get("state_dir", str(root)))
    parent = state / "official-tools"
    parent.mkdir(parents=True, exist_ok=True)
    now = time.time() if now is None else now
    with locked(state / "official-tools.lock"):
        saved = read(state / "official-tools-current.json")
        health = read(state / "official-tools-health.json")
        if (
            health.get("state") == "update_failed"
            and now < health["checked_at"] + MAX_AGE
        ):
            raise ValueError(
                "Official tool refresh is cooling down after failure: "
                + health["error"]
            )
        if saved and not force and now < saved["verified_at"] + MAX_AGE:
            validate(saved)
            return saved
        if download is None:
            from service import download_pack

            download = download_pack
        try:
            with tempfile.TemporaryDirectory(prefix=".download-", dir=parent) as temp:
                stage = Path(temp)
                for name in FILES:
                    download(BASE + name, stage / name)
                # Guidance is reference data, never an automatic prompt replacement.
                download("https://fnvdecomp.com/agents.md", stage / "agents.md")
                index = json.loads((stage / "function_index.json").read_text())
                addresses = [int(x["address"], 16) for x in index]
                if not addresses or len(set(addresses)) != len(addresses):
                    raise ValueError("Invalid official function index")
                if not json.loads((stage / "fnvdecomp_symbols.json").read_text()):
                    raise ValueError("Empty official symbol data")
                for name in ("compiler_tips.md", "large_functions.md", "agents.md"):
                    if not (stage / name).read_text().strip():
                        raise ValueError("Empty official guidance")
                unpack = stage / ".unpack"
                with tarfile.open(stage / "fnvcheck.tar.gz") as archive:
                    if any(not (x.isfile() or x.isdir()) for x in archive.getmembers()):
                        raise ValueError("Non-regular checker archive member")
                    archive.extractall(unpack, filter="data")
                candidates = list(unpack.rglob("fnvcheck.py"))
                if len(candidates) != 1:
                    raise ValueError("Ambiguous official checker archive")
                shutil.move(str(candidates[0].parent), str(stage / "fnvcheck"))
                shutil.rmtree(unpack, ignore_errors=True)
                for path in (stage / "fnvcheck").glob("*.py"):
                    ast.parse(path.read_text())
                bound = compatibility(stage / "fnvcheck")
                manifest = {
                    "files": {n: digest(stage / n) for n in (*FILES, "agents.md")},
                    "checker": tree(stage / "fnvcheck"),
                    "compiler_calls_per_flag_set": bound,
                }
                if bound >= 20:
                    from .toolchains import discover

                    manifest["toolchains"] = discover(state)
                identifier = hashlib.sha256(
                    json.dumps(manifest, sort_keys=True).encode()
                ).hexdigest()
                target = parent / identifier
                snapshot = {
                    "id": identifier,
                    "directory": str(target),
                    "manifest": manifest,
                    "verified_at": now,
                }
                if not target.exists():
                    stage.rename(target)
                validate(snapshot)
                atomic(state / "official-tools-current.json", snapshot)
                atomic(
                    state / "official-tools-health.json",
                    {"state": "current", "id": identifier, "checked_at": now},
                )
                return snapshot
        except Exception as exc:
            atomic(
                state / "official-tools-health.json",
                {
                    "state": "update_failed",
                    "checked_at": now,
                    "error": str(exc),
                    "last_good_id": saved.get("id"),
                },
            )
            raise


def attach(root, claim_id, assignment):
    root = Path(root)
    claim = root / "claims" / claim_id
    snapshot = read(claim / "official-tools.json") or current(root)
    directory = validate(snapshot)
    pack = json.loads((claim / "pack/pack.json").read_text())
    compiler = pack.get("compiler", "sp1")
    if compiler not in ("sp1", "vc8"):
        raise ValueError("Unsupported official pack compiler: " + str(compiler))
    if compiler == "vc8" and "vc8" not in snapshot["manifest"].get("toolchains", {}):
        raise ValueError(
            "Install official Visual C++ 2005 SP1 before preparing this VC8 assignment"
        )
    target = int(pack["address"], 16)
    if target not in {
        int(r["address"], 16)
        for r in json.loads((directory / "function_index.json").read_text())
    }:
        snapshot = current(root, force=True)
        directory = validate(snapshot)
        if target not in {
            int(r["address"], 16)
            for r in json.loads((directory / "function_index.json").read_text())
        }:
            raise ValueError(
                "Current official index lacks assigned target; no checker run"
            )
    bound = snapshot["manifest"]["compiler_calls_per_flag_set"] * (
        1 + len(pack.get("accept_also") or [])
    )
    assignment.update(
        official_tools=snapshot,
        official_internal_retry_bound=bound,
        max_compiler_invocations=assignment["max_plain_checks"] * bound,
    )
    workflow = assignment.get("checker_workflow", {})
    if "budget_rule" in workflow:
        workflow["budget_rule"] = workflow["budget_rule"].replace(
            "up to4", f"up to{bound}"
        )
    if "budget_basis" in assignment:
        assignment["budget_basis"] = assignment["budget_basis"].replace(
            "maximum4", f"maximum{bound}"
        )
    assignment["resource_paths"] = {
        "checker": str(directory / "fnvcheck/fnvcheck.py"),
        "function_index": str(directory / "function_index.json"),
        "symbols": str(directory / "fnvdecomp_symbols.json"),
        "compiler_tips": str(directory / "compiler_tips.md"),
        "large_functions": str(directory / "large_functions.md")
        if (directory / "large_functions.md").is_file()
        else None,
        "official_guidance": str(directory / "agents.md"),
        "game_image": str(Path.home() / "fnvdecomp/game/FalloutNV.exe"),
        "environment_script": str(Path.home() / "fnvdecomp/env.sh"),
    }
    atomic(claim / "official-tools.json", snapshot)
    return assignment


def context(assignment):
    snapshot = assignment.get("official_tools")
    if not snapshot:
        return None
    directory = validate(snapshot)
    return {
        "directory": directory,
        "checker": directory / "fnvcheck",
        "components": {
            p.name: digest(p) for p in (directory / "fnvcheck").glob("*.py")
        },
        "bound": assignment["official_internal_retry_bound"],
        "toolchains": snapshot["manifest"].get("toolchains", {}),
        "full_diagnostics": "ap.add_argument('--full-diff'"
        in (directory / "fnvcheck/fnvcheck.py").read_text(),
    }


def environment(value, claim):
    directory = value["directory"]
    compilers = value.get("toolchains", {})
    variables = []
    if compilers:
        variables.append("FNV_MSVC9_ROOT=" + compilers["sp1"]["root"])
    # Never silently enable an unrecorded ambient fallback on a legacy assignment.
    variables.append("FNVCHECK_RTM_ROOT=" + compilers.get("rtm", {}).get("root", ""))
    variables.append("FNVCHECK_VC8_ROOT=" + compilers.get("vc8", {}).get("root", ""))
    # The official CLI selects VC8 from pack.json. Ambient overrides must never
    # silently select a different compiler for ordinary or historical packs.
    variables.append("FNVCHECK_COMPILER=sp1")
    return [
        "PYTHONDONTWRITEBYTECODE=1",
        "FNVCHECK_TOOLS=" + str(value["checker"]),
        "FNVCHECK_INDEX=" + str(directory / "function_index.json"),
        "FNVCHECK_SYMBOLS=" + str(directory / "fnvdecomp_symbols.json"),
        "FNVCHECK_CACHE=" + str(Path(claim) / "work/tool-cache"),
        *variables,
    ]


def pack_files(claim):
    claim = Path(claim)
    result = {}
    with tarfile.open(claim / "pack.tar.gz") as archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            path = (claim / "pack" / member.name).resolve()
            if (
                not member.isfile()
                or not path.is_relative_to((claim / "pack").resolve())
                or path.is_symlink()
            ):
                raise ValueError("Invalid original pack member")
            raw = archive.extractfile(member).read()
            if path.read_bytes() != raw:
                raise ValueError("Original pack contents changed")
            result[str(path)] = digest(path)
    return result
