"""Import numeric run/checkpoint companions without loading executable .pt files.

Historical epoch timings train an experimental duration model. Missing historical
RAM/VRAM measurements never become fabricated labels for the safety predictor.
"""
from __future__ import annotations
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import time
import yaml

FEATURES=['batch','image_size','workers','backend','dataset_code','epoch']

def schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS history(run TEXT,epoch INTEGER,updated REAL,body TEXT,PRIMARY KEY(run,epoch))')

def number(value):
    if isinstance(value,bool):return None
    try:
        result=float(value)
        return result if math.isfinite(result) else None
    except (ValueError,TypeError):return None

def import_history(runtime,training_root):
    root=Path(training_root).resolve();report={'runs':0,'epochs':0,'inserted':0,'updated':0,'skipped':0,'checkpoints':0,'issues':[]}
    policy=runtime.config()
    files=sorted([*(root/'runs').glob('*/results.csv'),*(root/'person_candidates').glob('*/results.csv')])
    with runtime.db() as db:
        schema(db)
        db.execute('DELETE FROM history WHERE updated<?',(time.time()-policy['retention_days']*86400,))
        for path in files[:2000]:
            if report['epochs']>=min(50000,policy['maximum_records']):break
            try:
                # Both CSV and configuration must stay within the configured training root.
                args_path=path.parent/'args.yaml'
                for source in (path,args_path):
                    source.resolve().relative_to(root)
                    if source.is_symlink() or source.stat().st_size>16*1024**2:raise ValueError('Unsafe or oversized companion file')
                args=yaml.safe_load(args_path.read_text())
                if not isinstance(args,dict):raise ValueError('Invalid run configuration')
                batch=number(args.get('batch'));size=number(args.get('imgsz'));workers=number(args.get('workers',0))
                if batch is None or batch<=0 or size is None or size<=0 or workers is None or workers<0:raise ValueError('Missing resolved batch/image/worker configuration')
                device=str(args.get('device','cpu'));backend=1 if device=='mps' else 0 if device=='cpu' else 2
                dataset=hashlib.sha256(str(args.get('data','unknown')).encode()).hexdigest()
                run=hashlib.sha256(str(path.parent.relative_to(root)).encode()).hexdigest()[:24]
                # Ignore an unfinished trailing row while the training process writes.
                content=path.read_text();content=content[:content.rfind('\n')+1]
                previous=0.;accepted=0;last_epoch=0
                for index,row in enumerate(csv.DictReader(io.StringIO(content))):
                    if index>=10000 or report['epochs']+accepted>=min(50000,policy['maximum_records']):break
                    row={k.strip():v.strip() for k,v in row.items() if k is not None and v is not None}
                    epoch=number(row.get('epoch'));elapsed=number(row.get('time'))
                    if epoch is None or epoch!=int(epoch) or epoch<1 or elapsed is None or elapsed<=previous:
                        report['skipped']+=1;continue
                    # A timing gap cannot be reconstructed into individual epochs.
                    if epoch<=last_epoch:report['skipped']+=1;continue
                    duration=elapsed-previous;previous=elapsed
                    if epoch!=last_epoch+1:report['skipped']+=1;last_epoch=epoch;continue
                    last_epoch=epoch
                    metrics={k:v for k,raw in list(row.items())[:64] if len(k)<=128 and k not in {'epoch','time'} and (v:=number(raw)) is not None}
                    body={'features':[batch,size,workers,backend,int(dataset[:8],16),int(epoch)],'epoch_seconds':duration,'elapsed_seconds':elapsed,'metrics':metrics,'dataset_id':dataset,'peak_gib':None,'provenance':'historical epoch CSV; memory unavailable'}
                    serialized=json.dumps(body,allow_nan=False)
                    old=db.execute('SELECT body FROM history WHERE run=? AND epoch=?',(run,int(epoch))).fetchone()
                    if old is None:report['inserted']+=1
                    elif old[0]!=serialized:report['updated']+=1
                    db.execute('INSERT OR REPLACE INTO history VALUES(?,?,?,?)',(run,int(epoch),time.time(),serialized))
                    accepted+=1
                if accepted:
                    report['runs']+=1;report['epochs']+=accepted
                    report['checkpoints']+=sum(p.is_file() and not p.is_symlink() for p in (path.parent/'weights'/'last.pt',path.parent/'weights'/'best.pt'))
            except (OSError,ValueError,TypeError,csv.Error,yaml.YAMLError):
                report['issues'].append({'run_id':hashlib.sha256(str(path.parent.name).encode()).hexdigest()[:12],'reason':'Companion missing, invalid or outside configured root'})
        limit=policy['maximum_records']
        db.execute('DELETE FROM history WHERE rowid NOT IN (SELECT rowid FROM history ORDER BY updated DESC LIMIT ?)',(limit,))
        # Keep the newest payloads within the historical numeric-data budget.
        db.execute('DELETE FROM history WHERE rowid NOT IN (SELECT rowid FROM (SELECT rowid,SUM(length(body)) OVER (ORDER BY updated DESC,rowid DESC) AS bytes FROM history) WHERE bytes<=?)',(policy['maximum_data_mb']*1024**2,))
    runtime.emit('history_imported',{k:v for k,v in report.items() if k!='issues'})
    return report

def summary(runtime):
    with runtime.db() as db:
        schema(db)
        row=db.execute('SELECT count(*),count(DISTINCT run),max(updated) FROM history').fetchone()
    return {'epochs':row[0],'runs':row[1],'last_import':row[2],'memory_labels':0}

def train_candidate(request,directory):
    import sqlite3
    import numpy as np
    import lightgbm as lgb
    from daksh_runtime import digest_file
    from daksh_worker import write_result
    root=Path(request['root'])
    with sqlite3.connect(root/'state.sqlite3') as db:
        schema(db);rows=db.execute('SELECT run,body FROM history ORDER BY run,epoch LIMIT 50000').fetchall()
    groups=sorted({r[0] for r in rows})
    if len(rows)<20 or len(groups)<2:raise ValueError('Import at least 20 completed epochs from two independent runs for historical adviser training.')
    # Whole-run holdout avoids leaking neighboring epochs into both partitions.
    held=set(groups[-max(1,len(groups)//4):]);train=[json.loads(b) for r,b in rows if r not in held];test=[json.loads(b) for r,b in rows if r in held]
    if min(len(train),len(test))<5:raise ValueError('Historical run holdout needs five epochs in each partition.')
    x=np.array([r['features'] for r in train]);xt=np.array([r['features'] for r in test]);y=np.array([r['epoch_seconds'] for r in train]);yt=np.array([r['epoch_seconds'] for r in test])
    model=lgb.train({'objective':'regression_l1','verbosity':-1,'num_threads':1,'num_leaves':7,'max_depth':3,'min_data_in_leaf':5,'seed':42},lgb.Dataset(x,label=y),num_boost_round=80)
    predictions=np.maximum(0,model.predict(xt,num_threads=1))
    output=root/'versions'/request['job_id'];output.mkdir(parents=True,exist_ok=False)
    artifact=output/'history_duration.json';artifact.write_text(json.dumps(model.dump_model(),allow_nan=False))
    meta={'id':request['job_id'],'schema_version':1,'purpose':'historical epoch-duration candidate','features':FEATURES,'hashes':{artifact.name:digest_file(artifact)},'metrics':{'duration_mae_seconds':float(np.mean(abs(predictions-yt))),'relative_duration_error':float(np.mean(abs(predictions-yt)/np.maximum(yt,.01)))},'eligible':False,'train_records':len(train),'test_records':len(test),'runs':len(groups),'machines':None,'bytes':artifact.stat().st_size,'coverage':'held-out complete historical runs; hardware provenance unavailable','limitations':'Historical timing and loss metrics imported. RAM/VRAM labels are absent; this experimental duration model cannot replace the resource-safety predictor. Calibration and benchmark coverage are required before promotion.'}
    (output/'manifest.json').write_text(json.dumps(meta,allow_nan=False));write_result(directory,{'version':meta,'promoted':False})
