"""Exercise the copied post-submission audit without a compiler or service."""

import hashlib
import json
from pathlib import Path
import runpy
import shutil
import sys
import tarfile
import types

import pytest

from native_swarm.checker_result import RTM_SUFFIX, VC8_SUFFIX
from native_swarm.state import atomic, read
from native_swarm.tool_runtime import configure_submission_audit


def setup_audit(tmp_path, monkeypatch, output, *, compiler=None, code=0):
    shutil.copy2(Path(__file__).parent / 'fixtures/submission_audit.py', tmp_path / 'audit.py')
    configure_submission_audit(tmp_path)
    claim = tmp_path / 'claims/test-claim'
    pack = {'flags': ['/O2'], 'accept_also': [['/Od']]}
    if compiler:
        pack['compiler'] = compiler
    atomic(claim / 'pack/pack.json', pack)
    (claim / 'pack/unit.cpp').write_text('void target();\n')
    with tarfile.open(claim / 'pack.tar.gz', 'w:gz') as archive:
        for path in (claim / 'pack').iterdir():
            archive.add(path, arcname=path.name)
    (claim / 'submission.cpp').write_text('void target() {}\n')
    (claim / 'submission-check.log').write_text(output)
    digest = lambda name: hashlib.sha256((claim / name).read_bytes()).hexdigest()
    atomic(claim / 'submission-check.json', {
        'actual_exit_code': code,
        'source_sha256': digest('submission.cpp'),
        'checker_log_sha256': digest('submission-check.log'),
        'pack_archive_sha256': digest('pack.tar.gz'),
    })
    atomic(claim / 'submission-response.json', {'submission_id': 'submitted'})
    monkeypatch.setitem(sys.modules, 'service', types.SimpleNamespace(
        ROOT=tmp_path, save=lambda name, value: atomic(tmp_path / name, value)))
    return claim


@pytest.mark.parametrize('output,compiler', [
    ('MATCH\n', None),
    ('MATCH' + RTM_SUFFIX + '\n', None),
    ('MATCH' + VC8_SUFFIX + '\n', 'vc8'),
    ('MATCH  (under /Od, a flag set this function also accepts: submit it as it is)\n', None),
])
def test_official_success_passes_complete_post_submission_audit(tmp_path, monkeypatch, output, compiler):
    setup_audit(tmp_path, monkeypatch, output, compiler=compiler)
    runpy.run_path(str(tmp_path / 'audit.py'))
    assert read(tmp_path / 'submission-audit.json')['passed'] is True


@pytest.mark.parametrize('output,compiler,code', [
    ('MATCH' + RTM_SUFFIX, None, 1),
    ('MATCH' + VC8_SUFFIX, None, 0),
    ('MATCH', 'vc8', 0),
    ('diagnostic\nMATCH', None, 0),
    ('MISMATCH', None, 0),
    ('MATCH  (under /O1, a flag set this function also accepts: submit it as it is)', None, 0),
])
def test_invalid_checker_evidence_still_stops_audit(tmp_path, monkeypatch, output, compiler, code):
    setup_audit(tmp_path, monkeypatch, output, compiler=compiler, code=code)
    with pytest.raises(AssertionError):
        runpy.run_path(str(tmp_path / 'audit.py'))
    assert read(tmp_path / 'submission-audit.json')['submissions'][0]['errors'] == ['invalid checker evidence']


@pytest.mark.parametrize('artifact,error', [
    ('submission.cpp', 'source digest changed'),
    ('submission-check.log', 'checker log digest changed'),
    ('pack/unit.cpp', 'pack differs unit.cpp'),
    ('pack/pack.json', 'pack differs pack.json'),
    ('pack.tar.gz', 'archive digest changed'),
])
def test_annotated_match_never_bypasses_integrity_checks(tmp_path, monkeypatch, artifact, error):
    claim = setup_audit(tmp_path, monkeypatch, 'MATCH' + RTM_SUFFIX + '\n')
    if artifact == 'pack/pack.json':
        atomic(claim / artifact, {'flags': ['/Od']})
    elif artifact == 'pack.tar.gz':
        # A valid changed gzip archive still reads successfully.
        with tarfile.open(claim / artifact, 'w:gz') as archive:
            for path in (claim / 'pack').iterdir():
                archive.add(path, arcname=path.name)
            archive.add(claim / 'submission.cpp', arcname='extra.cpp')
        shutil.copy2(claim / 'submission.cpp', claim / 'pack/extra.cpp')
    else:
        with (claim / artifact).open('a') as stream:
            stream.write('\nchanged\n')
    with pytest.raises(AssertionError):
        runpy.run_path(str(tmp_path / 'audit.py'))
    assert error in read(tmp_path / 'submission-audit.json')['submissions'][0]['errors']


def test_unknown_audit_template_is_preserved(tmp_path):
    path = tmp_path / 'audit.py'
    path.write_text('raise RuntimeError("unknown audit")\n')
    before = path.read_bytes()
    with pytest.raises(ValueError, match='Unsupported submission audit'):
        configure_submission_audit(tmp_path)
    assert path.read_bytes() == before
