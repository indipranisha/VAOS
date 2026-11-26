"""Isolated Daksh calibration / CPU-only adviser training worker.

Only Runtime constructs request paths. Inputs resolve to registered, local assets.
"""
from __future__ import annotations
import argparse
import json
import math
import os
from pathlib import Path
import shutil
import time
from daksh_runtime import FEATURES, digest_file, features, host_snapshot

def write_result(directory,result):
    temporary=directory/'result.tmp';temporary.write_text(json.dumps(result,allow_nan=False));temporary.replace(directory/'result.json')

def write_progress(directory,result):
    temporary=directory/'progress.tmp';temporary.write_text(json.dumps(result));temporary.replace(directory/'progress.json')

def signature(model_path,backend,image_size):
    import importlib.metadata as md
    import platform
    versions={}
    for name in ['torch','ultralytics']:
        try:versions[name]=md.version(name)
        except md.PackageNotFoundError:versions[name]='unavailable'
    import hashlib
    return hashlib.sha256(json.dumps({'weights':digest_file(model_path),'backend':backend,'image_size':image_size,'machine':platform.machine(),'os':platform.system(),'cpu_count':os.cpu_count(),'ram_gib':round(host_snapshot()['memory_total_gb']),'versions':versions},sort_keys=True).encode()).hexdigest()

def bounded_images(data_path,limit=32):
    import yaml
    raw=yaml.safe_load(Path(data_path).read_text());base=Path(raw.get('path') or Path(data_path).parent)
    if not base.is_absolute():base=Path(data_path).parent/base
    train=raw.get('train');sources=train if isinstance(train,list) else [train];images=[]
    for source in sources:
        if not isinstance(source,str):continue
        p=Path(source);p=p if p.is_absolute() else base/p
        if p.is_dir():
            for image in p.rglob('*'):
                if image.suffix.lower() in {'.jpg','.jpeg','.png'}:images.append(image)
                if len(images)>=limit:break
        elif p.suffix=='.txt':
            with p.open() as handle:
                for line in handle:
                    v=Path(line.strip());v=v if v.is_absolute() else p.parent/v
                    if v.is_file() and v.suffix.lower() in {'.jpg','.jpeg','.png'}:images.append(v)
                    if len(images)>=limit:break
        if len(images)>=limit:break
    if not images:raise ValueError('Registered dataset contains no accessible calibration images.')
    return images[:limit],raw['names']

def calibrate(request,directory):
    import torch
    import yaml
    from ultralytics import YOLO
    from PIL import Image
    torch.set_num_threads(1)
    model_path=Path(request['model_path']);data_path=Path(request['data_path'])
    before=digest_file(model_path);backend=request.get('backend','cpu');image_size=request.get('image_size',640)
    images,names=bounded_images(data_path)
    subset=directory/'subset'
    for split in ['train','val']:
        for kind in ['images','labels']:(subset/kind/split).mkdir(parents=True,exist_ok=True)
        for i,src in enumerate(images):
            # Bound copied bytes as well as image count; reject decompression bombs.
            if src.stat().st_size>16*1024**2:raise ValueError('Calibration image exceeds the 16 MiB input limit.')
            Image.MAX_IMAGE_PIXELS=20_000_000
            with Image.open(src) as image:image.verify()
            target=subset/'images'/split/f'{i}{src.suffix.lower()}';shutil.copyfile(src,target)
            parts=list(src.parts)
            if 'images' in parts:parts[len(parts)-1-parts[::-1].index('images')]='labels'
            label=Path(*parts).with_suffix('.txt')
            if label.exists() and label.stat().st_size>1024**2:raise ValueError('Calibration labels exceed input limit.')
            text=label.read_text() if label.exists() else ''
            (subset/'labels'/split/f'{i}.txt').write_text(text)
    manifest=subset/'data.yaml';manifest.write_text(yaml.safe_dump({'path':str(subset.resolve()),'train':'images/train','val':'images/val','names':names}))
    records=[];host=host_snapshot();deadline=time.monotonic()+request.get('timeout_seconds',300)-5
    for batch in request['batches']:
        if time.monotonic()>deadline:break
        h=host_snapshot();available=h['memory_total_gb']-h['memory_used_gb']
        if available<max(6,h['memory_total_gb']*.2)+batch*.5:break
        measured=[];peaks=[];last=None;seen=0
        model=YOLO(str(model_path))
        def on_batch(trainer):
            nonlocal last,seen
            now=time.monotonic();seen+=1
            if last is not None and seen>2:measured.append(now-last)
            last=now;current=host_snapshot();peaks.append(current['memory_used_gb']);write_progress(directory,{'phase':'calibration','candidate_batch':batch,'batches_measured':seen,'candidates_completed':len(records)})
            if backend=='mps':torch.mps.synchronize()
            if backend=='cuda':torch.cuda.synchronize()
            if seen>=8 or now>=deadline or current['memory_percent']>=85:trainer.stop=True
        def on_start(trainer):
            # Keep calibration from performing optimizer changes beyond one bounded epoch.
            trainer.args.save=False
        model.add_callback('on_train_batch_end',on_batch);model.add_callback('on_train_start',on_start)
        result=model.train(data=str(manifest),epochs=1,batch=batch,imgsz=image_size,device=request.get('device','cpu'),workers=0,
                           cache=False,project=str(directory/'calibration'),name=f'batch_{batch}',exist_ok=False,save=False,
                           plots=False,val=False,verbose=False,amp=request.get('precision','fp32')=='amp',seed=42)
        if measured:
            speed=batch/(sum(measured)/len(measured));records.append({'features':features(host,{'batch':batch,'backend':backend,'image_size':image_size,'workers':0,'model_mb':model_path.stat().st_size/2**20}),
                'speed':speed,'peak_gib':max(peaks)})
        del result,model
        import gc;gc.collect()
        if backend=='mps':torch.mps.empty_cache()
        if backend=='cuda':torch.cuda.empty_cache()
    if digest_file(model_path)!=before:raise RuntimeError('Active weight integrity changed during calibration; results rejected.')
    if not records:raise ValueError('No candidate completed within resource/time limits.')
    sig=signature(model_path,backend,image_size)
    write_result(directory,{'signature':sig,'records':records,'maximum_batch':max(r['features'][5] for r in records),'weight_unchanged':True,'scope':'bounded subset; not a full training or accuracy benchmark'})

def retrain(request,directory):
    import sqlite3
    import numpy as np
    import lightgbm as lgb
    write_progress(directory,{'phase':'loading retained benchmark records'})
    root=Path(request['root'])
    if request.get('history_only'):
        from daksh_history import train_candidate
        return train_candidate(request,directory)
    with sqlite3.connect(root/'state.sqlite3') as db:
        rows=db.execute('SELECT run,machine,signature,body FROM records ORDER BY id LIMIT 50000').fetchall()
    if len(rows)<100:
        with sqlite3.connect(root/'state.sqlite3') as db:
            from daksh_history import schema
            schema(db);historical=db.execute('SELECT count(*) FROM history').fetchone()[0]
        if historical:
            from daksh_history import train_candidate
            return train_candidate(request,directory)
        raise ValueError('Collect at least 100 benchmark windows before adviser retraining.')
    groups=sorted({r[0] for r in rows})
    machines=sorted({r[1] for r in rows})
    if len(groups)<6:raise ValueError('At least six independent runs/calibrations are needed for held-out evaluation.')
    # Hold out complete machines when possible; otherwise complete runs.
    test_machine=machines[-1] if len(machines)>1 else None
    test_runs=set(groups[-max(2,len(groups)//5):])
    train=[];test=[]
    for run,machine,sig,raw in rows:
        target=test if (machine==test_machine if test_machine else run in test_runs) else train
        target.append((json.loads(raw),sig))
    if min(len(train),len(test))<20:raise ValueError('Insufficient independent held-out coverage.')
    x=np.array([r['features'] for r,_ in train]);xt=np.array([r['features'] for r,_ in test])
    meta={'id':request['job_id'],'features':FEATURES,'schema_version':1,'signatures':sorted({r[2] for r in rows}),
          'minimum':x.min(axis=0).tolist(),'maximum':x.max(axis=0).tolist(),'hashes':{},'metrics':{},'eligible':False,
          'train_records':len(train),'test_records':len(test),'runs':len(groups),'machines':len(machines),'coverage':'held-out machine' if test_machine else 'held-out runs; local hardware only',
          'policy':{'version':1,'max_memory_underprediction_rate':.05,'max_relative_speed_error':.30,'must_beat_constant_speed_baseline':True}}
    output=root/'versions'/meta['id'];output.mkdir(parents=True,exist_ok=False)
    write_progress(directory,{'phase':'training and held-out evaluation','training_records':len(train),'test_records':len(test)})
    for key,name,objective in [('speed','speed.txt','regression'),('peak_gib','memory.txt','quantile')]:
        y=np.array([r[key] for r,_ in train]);yt=np.array([r[key] for r,_ in test])
        model=lgb.train({'objective':objective,'alpha':.95,'verbosity':-1,'num_threads':1,'num_leaves':15,'max_depth':4,'min_data_in_leaf':10,'seed':42},lgb.Dataset(x,label=y),num_boost_round=80)
        p=np.maximum(0,model.predict(xt,num_threads=1));model.save_model(str(output/name));json_name=name.replace('.txt','.json');(output/json_name).write_text(json.dumps(model.dump_model(),allow_nan=False));meta['hashes'][json_name]=digest_file(output/json_name)
        if key=='speed':
            error=float(np.mean(np.abs(p-yt)/np.maximum(yt,.01)));baseline=float(np.mean(np.abs(np.median(y)-yt)/np.maximum(yt,.01)))
            meta['metrics'].update({'relative_speed_error':error,'constant_baseline_error':baseline,'speed_mae':float(np.mean(np.abs(p-yt)))})
        else:
            # Add a 10% memory margin to the learned quantile during evaluation/inference.
            under=float(np.mean(yt>p*1.1));meta['metrics'].update({'memory_underprediction_rate':under,'memory_coverage':1-under})
    m=meta['metrics'];meta['eligible']=m['relative_speed_error']<=.30 and m['relative_speed_error']<m['constant_baseline_error'] and m['memory_underprediction_rate']<=.05
    size=sum(p.stat().st_size for p in output.iterdir())
    if size>10*1024**2:raise ValueError('Adviser exceeds 10 MiB model budget.')
    meta['bytes']=size;(output/'manifest.json').write_text(json.dumps(meta,allow_nan=False));write_result(directory,{'version':meta,'promoted':False})

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--request',required=True);args=parser.parse_args()
    path=Path(args.request);request=json.loads(path.read_text());directory=path.parent
    if request.get('guardian'):
        import threading
        from daksh_runtime import process_alive
        def watchdog():
            deadline=time.monotonic()+request.get('timeout_seconds',300)
            while True:
                if not process_alive(*request['guardian']) or time.monotonic()>deadline:
                    os._exit(2)  # isolated worker only; never the detector training process
                time.sleep(1)
        threading.Thread(target=watchdog,name='daksh-worker-watchdog',daemon=True).start()
    try:
        if request['kind']=='calibration':calibrate(request,directory)
        elif request['kind']=='retraining':retrain(request,directory)
        else:raise ValueError('Unsupported worker kind.')
    except Exception as exc:
        import sys
        reason=type(exc).__name__+': '+str(exc)
        for key in ('model_path','data_path','root'):
            if request.get(key):reason=reason.replace(str(request[key]),'[local artifact]')
        reason=reason[:240]
        write_result(directory,{'error':True,'reason':reason})
        print(reason,file=sys.stderr)
        raise SystemExit(1)
if __name__=='__main__':main()
