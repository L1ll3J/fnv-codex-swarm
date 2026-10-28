"""Durable batch-round admission around the existing recorded checker."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


PINNED_DRIVER='bc2c8d08b074337635871590f8d26ab3fb7c4092351fb5403a06e3d5a6f62270'


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path,data):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data,indent=2)+'\n')
    temporary.replace(path)


def verified_record(path,claim,source_digest):
    row=json.loads(path.read_text())
    assert row['claim_id']==claim and row['source_sha256']==source_digest
    assert row['checker_unchanged'] and row['source_unchanged']
    assert sha(row['source_path'])==source_digest and sha(row['log_path'])==row['log_sha256']
    return row


def run_batch(assignment,batch_id,candidates,invoke=None):
    assert isinstance(batch_id,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',batch_id),'Invalid batch ID'
    assert 1<=len(candidates)<=8,'A round requires 1–8 candidates'
    claim=Path(assignment['claim_dir']).resolve()
    records=claim/'work/recorded-checks';records.mkdir(parents=True,exist_ok=True)
    rounds=claim/'work/batch-rounds';rounds.mkdir(parents=True,exist_ok=True)
    sources=[]
    for candidate in candidates:
        source=Path(candidate).resolve()
        assert source.is_relative_to(claim) and source.is_file(),'Source escaped assigned claim'
        assert 0<source.stat().st_size<=65536,'Candidate must contain 1–65,536 bytes'
        sources.append({'path':str(source),'sha256':sha(source)})
    assert len({s['sha256'] for s in sources})==len(sources),'Duplicate source within round'
    with (rounds/'.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        path=rounds/(batch_id+'.json')
        identity={'claim_id':assignment['claim_id'],'sources':[s['sha256'] for s in sources]}
        if path.exists():
            batch=json.loads(path.read_text());assert batch['identity']==identity,'Batch ID already bound to different sources'
            if batch['state']=='complete':return batch
        else:
            assert len(list(rounds.glob('*.json')))<assignment['max_batch_rounds'],'Assigned batch-round budget exhausted'
            assert not any(json.loads(p.read_text()).get('checker_result',{}).get('status')=='match' for p in records.glob('check-*.json')),'Stop at first official MATCH'
            batch={'identity':identity,'batch_id':batch_id,'state':'running','results':[],'snapshots':[]}
            directory=rounds/batch_id;directory.mkdir()
            for i,source in enumerate(sources):
                snapshot=directory/('candidate-%02d.cpp'%(i+1))
                snapshot.write_bytes(Path(source['path']).read_bytes());snapshot.chmod(0o444)
                assert sha(snapshot)==source['sha256'],'Source changed while snapshotting'
                batch['snapshots'].append(str(snapshot))
            save(path,batch)
        for index in range(len(batch['results']),len(sources)):
            snapshot=Path(batch['snapshots'][index]);digest=identity['sources'][index]
            assert sha(snapshot)==digest,'Batch snapshot changed'
            pending=batch.get('pending')
            if pending:
                assert pending['candidate_index']==index
                record=Path(pending['record'])
                assert record.exists(),'Interrupted checker dispatch has no receipt; no automatic replay'
            else:
                number=len(list(records.glob('check-*.json')))+1
                record=records/('check-%03d.json'%number)
                assert not record.exists()
                batch['pending']={'candidate_index':index,'record':str(record)};save(path,batch)
                driver=Path(assignment['checker_workflow']['single_check_driver'])
                assert sha(driver)==assignment['checker_workflow']['single_check_driver_sha256'],'Underlying recorder changed'
                command=[sys.executable,str(driver),'--assignment',assignment['assignment_path'],str(snapshot)]
                result=(invoke or (lambda cmd:subprocess.run(cmd,capture_output=True)))(command)
                (Path(batch['snapshots'][index]).parent/('checker-%02d.stdout'%(index+1))).write_bytes(result.stdout or b'')
                (Path(batch['snapshots'][index]).parent/('checker-%02d.stderr'%(index+1))).write_bytes(result.stderr or b'')
                assert record.exists(),'Checker did not produce a receipt; stop and preserve evidence'
            row=verified_record(record,assignment['claim_id'],digest)
            matched=row['checker_exit_code']==0 and row['checker_result'].get('status')=='match'
            batch['results'].append({'candidate_index':index+1,'source_sha256':digest,
                'manifest':str(record),'checker':row['checker_result'],'exit_code':row['checker_exit_code'],'match':matched})
            batch.pop('pending',None);save(path,batch)
            if matched:break
        batch['state']='complete';batch['match']=any(r['match'] for r in batch['results']);save(path,batch)
        return batch


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--assignment',type=Path,required=True)
    parser.add_argument('--batch-id');parser.add_argument('--finish',type=Path);parser.add_argument('--residual',default='')
    parser.add_argument('candidates',type=Path,nargs='*');args=parser.parse_args()
    assignment=json.loads(args.assignment.read_text());assignment['assignment_path']=str(args.assignment.resolve())
    if args.finish:
        driver=Path(assignment['checker_workflow']['single_check_driver'])
        assert sha(driver)==assignment['checker_workflow']['single_check_driver_sha256']
        return subprocess.run([sys.executable,str(driver),'--assignment',str(args.assignment),
            '--finish',str(args.finish),'--residual',args.residual]).returncode
    batch=run_batch(assignment,args.batch_id,args.candidates)
    print(json.dumps(batch));return 0 if batch['match'] else 1


if __name__=='__main__':raise SystemExit(main())
