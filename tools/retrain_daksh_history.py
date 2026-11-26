#!/usr/bin/env python3
"""Explicit bounded CPU rebuild of historical Daksh duration model.

Unlike scheduled hardware calibration, this one-thread, <=1 GiB CPU operation can
run alongside detector training. It never imports Torch or opens checkpoint blobs.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psutil
from daksh_runtime import Runtime,identity
from daksh_history import import_history

def run(data_dir):
    rt=Runtime(Path(data_dir).resolve()/'daksh')
    report=import_history(rt,Path(data_dir).resolve()/'ml_training')
    if report['epochs']<20 or report['runs']<2:raise RuntimeError('Insufficient historical independent-run coverage.')
    job=rt.create_job('retraining',{'timeout_seconds':120,'history_only':True},'history-cpu-'+secrets.token_hex(12));ident=job['id']
    rt.acquire(ident,'adviser_cpu');proc=None
    directory=rt.root/'jobs'/ident;directory.mkdir(parents=True,exist_ok=True)
    request=directory/'request.json';request.write_text(json.dumps({**job['payload'],'kind':'retraining','root':str(rt.root),'job_id':ident,'guardian':identity()}));request.chmod(0o600)
    try:
        mem=psutil.virtual_memory()
        if mem.available<max(6*2**30,mem.total*.2):raise RuntimeError('Not enough free memory for bounded CPU history training.')
        rt.transition(ident,'running',{'phase':'explicit one-thread CPU history rebuild'})
        with (directory/'worker.log').open('wb') as output:
            proc=subprocess.Popen([sys.executable,str(Path(__file__).resolve().parents[1]/'daksh_worker.py'),'--request',str(request)],stdout=output,stderr=output,env={**os.environ,'OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1','MKL_NUM_THREADS':'1'})
            deadline=time.monotonic()+120
            while proc.poll() is None:
                m=psutil.virtual_memory()
                try:rss=psutil.Process(proc.pid).memory_info().rss
                except psutil.NoSuchProcess:break
                if time.monotonic()>deadline or rss>min(2**30,m.total*.05) or m.available<max(6*2**30,m.total*.2):raise RuntimeError('CPU history rebuild exceeded its resource budget.')
                time.sleep(.2)
        result=json.loads((directory/'result.json').read_text())
        if proc.wait(timeout=5) or result.get('error'):raise RuntimeError(result.get('reason','History worker failed.'))
        meta=result['version']
        with rt.db() as db:db.execute('INSERT INTO versions VALUES(?,?,?,0,0)',(ident,time.time(),json.dumps(meta)))
        rt.transition(ident,'completed',result);rt.emit('history_candidate_trained',{'job_id':ident,'epochs':report['epochs'],'runs':report['runs'],'promoted':False})
        return {'import':report,'job_id':ident,'version':meta,'promoted':False,'artifact':str(rt.root/'versions'/ident/'history_duration.json')}
    except Exception as exc:
        rt.transition(ident,'failed',{'reason':str(exc)[:240]});raise
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
        rt.release(ident)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--data-dir',type=Path,required=True);args=parser.parse_args();print(json.dumps(run(args.data_dir),indent=2))
