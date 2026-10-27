import json
from pathlib import Path
from unittest.mock import Mock
import pytest
from native_swarm.backend import Backend, ControlError
from native_swarm.state import atomic,read


def setup_backend(tmp_path):
 state=tmp_path/'state';run=tmp_path/'run';run.mkdir();atomic(state/'config.json',{'observed_runs':[str(run)],'current_run':str(run),'workers':10,'bands':['<=255']})
 return Backend(state),run


def test_pause_preserves_existing_integrity_stop(tmp_path):
 b,r=setup_backend(tmp_path);(r/'STOP_NEW_CLAIMS').write_text('integrity failure')
 b.pause();assert (r/'STOP_NEW_CLAIMS').read_text()=='integrity failure'


def test_managed_pause_is_resumable_marker_not_global_stop(tmp_path):
 b,r=setup_backend(tmp_path);atomic(r/'native-app-managed.json',{})
 b.pause();assert (r/'operator-admissions-paused.json').exists() and not (r/'STOP_NEW_CLAIMS').exists()
 b.drain();assert (r/'operator-drain.json').exists()


def test_settings_validate_multi_brackets_and_do_not_mutate_worker_assignments(tmp_path):
 b,r=setup_backend(tmp_path);atomic(r/'native-app-managed.json',{});atomic(r/'operator-control.json',{})
 b.settings(6,['<=255','512-1K'])
 assert read(r/'operator-control.json')['native_app_bands']==['<=255','512-1K']
 with pytest.raises(ControlError):b.settings(11,['<=255'])
 with pytest.raises(ControlError):b.settings(5,[])


def test_changed_template_is_not_silently_trusted(tmp_path):
 b,r=setup_backend(tmp_path);(r/'controller.py').write_text('changed')
 with pytest.raises(ControlError,match='template changed'):
  b.verify_template(r,{'template_sha256':{'controller.py':'wrong'}})


def test_normal_accounting_updates_do_not_invalidate_pinned_code(tmp_path):
 import hashlib
 b,r=setup_backend(tmp_path);(r/'controller.py').write_text('unchanged')
 atomic(r/'report-style-trial/ledger.json',{'admission_closed':True})
 cfg={'template_sha256':{'controller.py':hashlib.sha256((r/'controller.py').read_bytes()).hexdigest()},'template_ledger_sha256':hashlib.sha256((r/'report-style-trial/ledger.json').read_bytes()).hexdigest()}
 atomic(r/'campaign-counts.json',{'today':10});b.verify_template(r,cfg)
 atomic(r/'campaign-counts.json',{'today':11});b.verify_template(r,cfg)


def test_review_is_only_local(tmp_path,monkeypatch):
 b,r=setup_backend(tmp_path);external=Mock(side_effect=AssertionError('Unexpected external operation'));monkeypatch.setattr('native_swarm.backend.subprocess.run',external)
 b.review('evidence-path','reviewed');assert read(b.state/'reviews.json')['evidence-path']['status']=='reviewed';external.assert_not_called()


def test_start_creates_detached_controller_only_after_explicit_action(tmp_path,monkeypatch):
 import hashlib
 from types import SimpleNamespace
 b=Backend(tmp_path/'state');template=tmp_path/'template';template.mkdir()
 for name,value in {'operator-control.json':{},'rolling-admission.json':{},'run-config.json':{}}.items():atomic(template/name,value)
 (template/'worker-contract.xml').write_text('<approved>unchanged</approved>')
 atomic(template/'report-style-trial/ledger.json',{'admission_closed':True})
 files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in template.iterdir() if p.is_file()}
 atomic(b.state/'config.json',{'template_run':str(template),'template_sha256':files,'template_ledger_sha256':hashlib.sha256((template/'report-style-trial/ledger.json').read_bytes()).hexdigest(),'observed_runs':[],'runs_root':str(tmp_path/'runs'),'quota_path':str(tmp_path/'quota'),'workers':6,'bands':['<=255']})
 monkeypatch.setattr('native_swarm.backend.resolve_codex',lambda:Path('/fake/codex'));monkeypatch.setattr(b,'quota',lambda cfg:None);monkeypatch.setattr(b,'preflight',lambda *args,**kw:{'open_claims':[]})
 monkeypatch.setattr('native_swarm.backend.subprocess.run',lambda *args,**kw:SimpleNamespace(returncode=0))
 launches=[]
 def popen(args,**kwargs):
  root=Path(kwargs['cwd']);launches.append((args,kwargs));pid=100+len(launches)
  if str(args[-1]).endswith('app_controller.py'):atomic(root/'.rolling-executor-owner.json',{'pid':pid})
  return SimpleNamespace(pid=pid,poll=lambda:None)
 monkeypatch.setattr('native_swarm.backend.subprocess.Popen',popen);monkeypatch.setattr(b,'lock_held',lambda p:True)
 assert not launches
 assert 'started' in b.start()
 root=b.current
 assert len(launches)==2 and all(kw['start_new_session'] for _,kw in launches)
 assert not (root/'STOP_NEW_CLAIMS').exists()
 assert (root/'worker-contract.xml').read_text()==b.prompt()
 assert read(root/'operator-control.json')['target_pool']==6
 assert read(root/'native-app-quota.json')['path']==str(tmp_path/'quota')
 assert read(root/'native-app-codex.json')['executable']=='/fake/codex'
