import json,pathlib,hashlib,tarfile,datetime
from service import ROOT,save
rows=[]
for p in (ROOT/'claims').iterdir():
 if not (p/'submission-response.json').exists():continue
 m=json.loads((p/'submission-check.json').read_text());src=p/'submission.cpp';log=(p/'submission-check.log').read_text();errors=[]
 if m['actual_exit_code']!=0 or 'MATCH' not in log.splitlines():errors.append('invalid checker evidence')
 if hashlib.sha256(src.read_bytes()).hexdigest()!=m['source_sha256']:errors.append('source digest changed')
 if hashlib.sha256((p/'submission-check.log').read_bytes()).hexdigest()!=m['checker_log_sha256']:errors.append('checker log digest changed')
 if hashlib.sha256((p/'pack.tar.gz').read_bytes()).hexdigest()!=m['pack_archive_sha256']:errors.append('archive digest changed')
 with tarfile.open(p/'pack.tar.gz') as t:
  for member in t.getmembers():
   if member.isfile() and (p/'pack'/member.name).read_bytes()!=t.extractfile(member).read():errors.append('pack differs '+member.name)
 response=json.loads((p/'submission-response.json').read_text())
 if not response.get('submission_id'):errors.append('missing submission id')
 rows.append({'claim_id':p.name,'submission_id':response.get('submission_id'),'sha256':m['source_sha256'],'errors':errors})
a={'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'submitted_snapshots':len(rows),'passed':all(not r['errors'] for r in rows),'submissions':rows};save('submission-audit.json',a)
print(json.dumps({k:v for k,v in a.items() if k!='submissions'}));assert a['passed']
