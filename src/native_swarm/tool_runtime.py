"""Adapt copied audited controller scripts to per-assignment official generations."""

import ast
from pathlib import Path


def _local(pattern):
    return pattern.replace("$FNV_ROOT", str(Path.home() / "fnvdecomp"))


def configure(root):
    from .runtime import replace_once

    root = Path(root)
    helper = root / "prepare_assignment.py"
    helper.write_text(
        helper.read_text()
        + "\nfrom native_swarm.official_tools import attach as attach_tools\nassignment=attach_tools(ROOT,cid,json.loads((ROOT/f'claims/{cid}/assignment.json').read_text()))\nsave(f'assignments/{worker}.json',assignment);save(f'claims/{cid}/assignment.json',assignment)\n"
    )
    recorder = root / "recorded_fnv_check_v2.py"
    replace_once(
        recorder,
        "def main():",
        "def main():\n global TOOLS,MAX_COMPILERS_PER_CHECK,AUDITED_COMPONENTS\n",
    )
    replace_once(
        recorder,
        "  budget=compiler_budget(records,limit)",
        "  from native_swarm.official_tools import context,environment\n  tool_context=context(assignment)\n  if tool_context:\n   TOOLS=tool_context['checker'];MAX_COMPILERS_PER_CHECK=tool_context['bound'];AUDITED_COMPONENTS=tool_context['components']\n  budget=compiler_budget(records,limit)",
    )
    # Validate the selected generation before the legacy audit guard too.
    replace_once(
        recorder,
        "  assert all(digest(TOOLS/name)==h for name,h in AUDITED_COMPONENTS.items()),'Official checker changed; compiler bound requires fresh audit'",
        "  from native_swarm.official_tools import context\n  selected=context(assignment)\n  if selected:TOOLS=selected['checker'];AUDITED_COMPONENTS=selected['components'];MAX_COMPILERS_PER_CHECK=selected['bound']\n  assert all(digest(TOOLS/name)==h for name,h in AUDITED_COMPONENTS.items()),'Official checker generation changed'",
    )
    replace_once(
        recorder,
        "    repair = any(",
        "    if MAX_COMPILERS_PER_CHECK != 4:\n        return (0, MAX_COMPILERS_PER_CHECK)\n    repair = any(",
    )
    replace_once(
        recorder,
        "'recorded-check',str(BASE/'.venv/bin/python')",
        "'recorded-check','env',*(environment(tool_context,claim) if tool_context else []),str(BASE/'.venv/bin/python')",
    )
    # New generations support complete diagnostic artifacts without additional
    # compiler calls. Leave legacy assignments on their original CLI options.
    replace_once(
        recorder,
        "str(snapshot),'--json']",
        "str(snapshot),'--json',*(['--full-diff',str(records/('diff-%03d.txt'%n)),'--slots'] if tool_context and tool_context.get('full_diagnostics') else [])]",
    )
    replace_once(
        recorder,
        "'checker_result':check,'log_path':str(log)",
        "'checker_result':check,'full_diff_path':str(records/('diff-%03d.txt'%n)) if (records/('diff-%03d.txt'%n)).is_file() else None,'log_path':str(log)",
    )
    replace_once(
        recorder,
        "'schema':'fnv-recorded-check/2'",
        "'schema':'fnv-recorded-check/2','official_tools_id':assignment.get('official_tools',{}).get('id'),'compiler_toolchains':tool_context.get('toolchains',{}) if tool_context else {}",
    )
    # Recorder wrappers must bind to the actual copied, adapted recorder.
    import hashlib

    new_hash = hashlib.sha256(recorder.read_bytes()).hexdigest()
    for name in ("recorded_batch.py", "recorded_check_with_history.py"):
        path = root / name
        if path.exists():
            node = next(
                n
                for n in ast.parse(path.read_text()).body
                if isinstance(n, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "PINNED_DRIVER"
                    for t in n.targets
                )
            )
            replace_once(path, node.value.value, new_hash)
    history = root / "attempt_history.py"
    if history.exists():
        replace_once(
            history,
            " if not all((BASE/'tools/fnvcheck'/n).exists() and sha(BASE/'tools/fnvcheck'/n)==v for n,v in row['checker_components'].items()):continue",
            " from native_swarm.official_tools import context\n  selected=context(assignment);checker=selected['checker'] if selected else BASE/'tools/fnvcheck'\n  if selected and read(row['manifest_path']).get('official_tools_id')!=assignment['official_tools']['id']:continue\n  if not all((checker/n).exists() and sha(checker/n)==v for n,v in row['checker_components'].items()):continue",
        )
    if history.exists():
        replace_once(
            history,
            "'checker_fingerprint':fingerprint",
            "'checker_fingerprint':fingerprint,'official_tools_id':d.get('official_tools_id')",
        )
        replace_once(
            history,
            "key=(row['source_sha256'],row['pack_archive_sha256'],row['checker_fingerprint'])",
            "key=(row['source_sha256'],row['pack_archive_sha256'],row['checker_fingerprint'],row.get('official_tools_id'))",
        )
    evidence = root / "evidence.py"
    replace_once(
        evidence,
        "def audit_worker(claim,result):",
        "def audit_worker(claim,result):\n from native_swarm.official_tools import context\n selected=context(json.loads((claim/'assignment.json').read_text()));CHECKER=selected['checker']/'fnvcheck.py' if selected else globals()['CHECKER']",
    )
    replace_once(
        evidence,
        _local(
            "if not q.resolve().is_relative_to(pathlib.Path('$FNV_ROOT/tools').resolve()) or not q.is_file():"
        ),
        "if not q.resolve().is_relative_to(CHECKER.parent.resolve()) or not q.is_file():",
    )
    replace_once(
        evidence,
        "+incomplete*4",
        "+incomplete*assignment.get('official_internal_retry_bound',4)",
    )
    validator = root / "validate_submit.py"
    replace_once(
        validator,
        "from evidence import audit_worker",
        _local(
            "from native_swarm.official_tools import context,environment\nfrom native_swarm.checker_result import accepted_match\ntool_context=context(a)\nCHECKER_DIR=tool_context['checker'] if tool_context else pathlib.Path('$FNV_ROOT/tools/fnvcheck')\nCHECKER_ARCHIVE=tool_context['directory']/'fnvcheck.tar.gz' if tool_context else pathlib.Path('$FNV_ROOT/downloads/fnvcheck.tar.gz')\nfrom evidence import audit_worker"
        ),
    )
    text = validator.read_text().replace(
        _local("tarfile.open('$FNV_ROOT/downloads/fnvcheck.tar.gz')"),
        "tarfile.open(CHECKER_ARCHIVE)",
    )
    text = text.replace(
        _local("pathlib.Path('$FNV_ROOT/tools/fnvcheck')"), "CHECKER_DIR"
    ).replace(
        "else CHECKER_DIR", _local("else pathlib.Path('$FNV_ROOT/tools/fnvcheck')")
    )
    text = text.replace(
        _local("pathlib.Path('$FNV_ROOT/downloads/fnvcheck.tar.gz')"),
        "CHECKER_ARCHIVE",
    ).replace(
        "else CHECKER_ARCHIVE",
        _local("else pathlib.Path('$FNV_ROOT/downloads/fnvcheck.tar.gz')"),
    )
    text = text.replace(
        _local("pathlib.Path('$FNV_ROOT/tools/fnvcheck/fnvcheck.py')"),
        "(CHECKER_DIR/'fnvcheck.py')",
    )
    text = text.replace(
        _local(
            'source $FNV_ROOT/env.sh; python $FNV_ROOT/tools/fnvcheck/fnvcheck.py --pack "$1" "$2"'
        ),
        _local('source $FNV_ROOT/env.sh; exec env "$@"'),
    )
    text = text.replace(
        "'check',str(p/'pack'),str(snapshot)",
        "'check',*(environment(tool_context,p) if tool_context else []),'python',str(CHECKER_DIR/'fnvcheck.py'),'--pack',str(p/'pack'),str(snapshot)",
    )
    text = text.replace(
        "'MATCH' in log.splitlines()", "accepted_match(x.returncode,log,manifest)[0]"
    )
    text = text.replace(
        "'MATCH' in log.read_text().splitlines()",
        "accepted_match(previous['actual_exit_code'],log.read_text(),manifest)[0]",
    )
    text = text.replace(
        "'checked_at':datetime.datetime.now",
        "'accepted_flags':accepted_match(x.returncode,log,manifest)[1],'official_tools_id':a.get('official_tools',{}).get('id'),'checked_at':datetime.datetime.now",
    )
    text = text.replace(
        _local("pathlib.Path('$FNV_ROOT/tools/msvc9/VC/bin/cl.exe')"),
        _local(
            "(pathlib.Path(tool_context['toolchains']['sp1']['root'])/'VC/bin/cl.exe' if tool_context and tool_context.get('toolchains',{}).get('sp1') else pathlib.Path('$FNV_ROOT/tools/msvc9/VC/bin/cl.exe'))"
        ),
    )
    text = text.replace(
        "versions={str(q):digest(q) for q in version_files}",
        "version_files += [tool_context['directory']/n for n in a['official_tools']['manifest']['files']] if tool_context else []\nfrom native_swarm.toolchains import files as compiler_files\nversion_files += [pathlib.Path(p) for p in compiler_files(tool_context.get('toolchains',{}))] if tool_context else []\nversions={str(q):digest(q) for q in version_files}",
    )
    validator.write_text(text)
    for path in root.glob("*.py"):
        ast.parse(path.read_text(), filename=str(path))
