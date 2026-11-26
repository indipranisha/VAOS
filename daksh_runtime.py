"""Persistent Daksh job orchestration. No training code is imported by the API process."""
from __future__ import annotations
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import psutil
from daksh_ai import Daksh, DakshError

TERMINAL={'completed','failed','cancelled','skipped'}
FEATURES=['total_gib','available_gib','cpu_count','backend','os','batch','image_size','workers','model_mb']
BACKENDS={'cpu':0,'mps':1,'cuda':2}
OS={'Darwin':0,'Linux':1,'Windows':2}

def finite(value, default=None):
    return float(value) if type(value) in (int,float) and math.isfinite(value) else default

def identity():
    p=psutil.Process();return p.pid,p.create_time()

def process_alive(pid,started):
    try:return abs(psutil.Process(pid).create_time()-started)<.01 and psutil.Process(pid).is_running()
    except psutil.Error:return False

def digest_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def host_snapshot():
    m=psutil.virtual_memory();swap=psutil.swap_memory()
    return {'memory_total_gb':m.total/2**30,'memory_used_gb':(m.total-m.available)/2**30,'memory_percent':m.percent,
            'swap_used_bytes':swap.used,'cpu_percent':psutil.cpu_percent(), 'cpu_count':psutil.cpu_count() or 1,'timestamp':time.time()}

def features(host,config):
    return [finite(host.get('memory_total_gb'),0),max(0,finite(host.get('memory_total_gb'),0)-finite(host.get('memory_used_gb'),0)),
            finite(host.get('cpu_count'),psutil.cpu_count() or 1),BACKENDS.get(config.get('backend','cpu'),0),OS.get(platform.system(),3),
            int(config.get('batch',1)),int(config.get('image_size',640)),int(config.get('workers',0)),finite(config.get('model_mb'),0)]

def next_occurrence(spec, after):
    """One occurrence on fold 0; advance a missing DST local minute to first valid one."""
    zone=ZoneInfo(spec['timezone']);local=dt.datetime.fromtimestamp(after,zone)
    for offset in range(370):
        day=local.date()+dt.timedelta(days=offset)
        if spec['frequency']=='weekly' and day.weekday()!=spec.get('weekday',0):continue
        naive=dt.datetime.combine(day,dt.time(spec['hour'],spec['minute']))
        for shift in range(181):
            candidate=(naive+dt.timedelta(minutes=shift)).replace(tzinfo=zone,fold=0)
            if dt.datetime.fromtimestamp(candidate.timestamp(),zone).replace(tzinfo=None)!=candidate.replace(tzinfo=None):continue
            if candidate.timestamp()>after:return candidate.timestamp()
            break
    raise DakshError('No next schedule occurrence.')

class Runtime:
    def __init__(self, root, host=host_snapshot, busy=lambda:False, audit=None):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.service=Daksh(self.root/'state.sqlite3');self.host=host;self.busy=busy;self.audit=audit or (lambda *args:None)
        self.engine_lock=threading.RLock();self.stop_event=threading.Event();self.thread=None;self.processes={};self.sample_history={};self._last_record={};self.last_error=None
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS leases(resource TEXT PRIMARY KEY,owner TEXT,pid INTEGER,started REAL,heartbeat REAL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT,state TEXT,created REAL,updated REAL,payload TEXT,result TEXT,cancel INTEGER DEFAULT 0,idem TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY,spec TEXT,next REAL,last REAL,occurrence REAL,job TEXT);
            CREATE TABLE IF NOT EXISTS records(id INTEGER PRIMARY KEY,created REAL,run TEXT,machine TEXT,signature TEXT,body TEXT);
            CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,created REAL,body TEXT,active INTEGER DEFAULT 0,previous INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,created REAL,event TEXT,body TEXT,delivered INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS runtime_settings(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT);
            CREATE TABLE IF NOT EXISTS checkpoints(id TEXT PRIMARY KEY,created REAL,body TEXT);
            CREATE TABLE IF NOT EXISTS external_training(owner TEXT PRIMARY KEY,updated REAL,body TEXT,stop INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS engine_state(id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS validation_metrics(run TEXT,epoch INTEGER,map REAL,PRIMARY KEY(run,epoch));
            ''')

    def db(self):return self.service.db()
    def emit(self,event,body):
        # Persist intent before audit delivery. Never include paths, images or secrets.
        event_id=secrets.token_hex(16)
        with self.db() as db:db.execute('INSERT INTO events VALUES(?,?,?,?,0)',(event_id,time.time(),event,json.dumps(body,allow_nan=False)))
        return event_id

    def flush_events(self):
        with self.db() as db:rows=db.execute('SELECT * FROM events WHERE delivered=0 ORDER BY created LIMIT 20').fetchall()
        for row in rows:
            if self.audit(row['event'],json.loads(row['body']),row['id']) is not False:
                with self.db() as db:db.execute('UPDATE events SET delivered=1 WHERE id=?',(row['id'],))

    def config(self):
        defaults={'automatic_stop':False,'sampling_seconds':5,'cooldown_seconds':300,'retention_days':90,'maximum_records':50000,'maximum_data_mb':100,
                  'throughput_drop_percent':30,'swap_growth_mb':512,'stall_seconds':180,'validation_patience':3}
        with self.db() as db:r=db.execute('SELECT body FROM runtime_settings WHERE id=1').fetchone()
        return {**defaults,**(json.loads(r[0]) if r else {})}

    def configure(self,payload,validate_only=False):
        current=self.config()
        if not isinstance(payload,dict) or set(payload)-set(current):raise DakshError('Unsupported supervisor setting.')
        for k,v in payload.items():
            if k=='automatic_stop':
                if type(v) is not bool:raise DakshError('automatic_stop must be boolean.')
            else:
                low,high={'sampling_seconds':(1,10),'cooldown_seconds':(30,3600),'retention_days':(1,365),'maximum_records':(100,50000),'maximum_data_mb':(1,1024),'throughput_drop_percent':(10,80),'swap_growth_mb':(128,4096),'stall_seconds':(60,3600),'validation_patience':(3,20)}[k]
                if type(v) is not int or not low<=v<=high:raise DakshError(f'Invalid {k}.')
        result={**current,**payload}
        if validate_only:return result
        with self.db() as db:db.execute('INSERT OR REPLACE INTO runtime_settings VALUES(1,?)',(json.dumps(result),))
        self.emit('settings_changed',{'fields':sorted(payload)})
        return result

    def unmanaged(self):
        ours={os.getpid(),*[p.pid for p in self.processes.values()]}
        found=[]
        for p in psutil.process_iter(['pid','cmdline']):
            try:
                args=p.info['cmdline'] or []
                if not args or not any(name in Path(args[0]).name.lower() for name in ('python','yolo')):continue
                cmd=' '.join(args)
                if p.pid not in ours and ('train_person_detector.py' in cmd or ('ultralytics' in cmd and ' train ' in cmd) or 'uvicorn main:app' in cmd):
                    found.append(p.pid)
            except (psutil.Error,TypeError):continue
        return found

    def acquire(self,owner,resource='training'):
        pid,started=identity()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM leases WHERE resource=?',(resource,)).fetchone()
            if row and row['owner']!=owner and process_alive(row['pid'],row['started']):raise DakshError('Another managed job owns this resource.')
            db.execute('INSERT OR REPLACE INTO leases VALUES(?,?,?,?,?)',(resource,owner,pid,started,time.time()))
        return owner

    def release(self,owner):
        with self.db() as db:db.execute('DELETE FROM leases WHERE owner=?',(owner,))

    @contextlib.contextmanager
    def resource(self,owner):
        self.acquire(owner)
        try:yield
        finally:self.release(owner)

    def leased(self):
        with self.db() as db:rows=db.execute('SELECT * FROM leases').fetchall()
        return [dict(r) for r in rows if r['resource']!='scheduler' and process_alive(r['pid'],r['started'])]

    def create_job(self,kind,payload,idem):
        if kind not in {'calibration','retraining'}:raise DakshError('Choose calibration or retraining.')
        if not isinstance(idem,str) or not 8<=len(idem)<=128:raise DakshError('Provide an idempotency key of 8–128 characters.')
        if not isinstance(payload,dict):raise DakshError('Job payload must be an object.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM jobs WHERE idem=?',(idem,)).fetchone()
            if old:
                if old['kind']!=kind or json.loads(old['payload'])!=payload:raise DakshError('Idempotency key already used for another request.')
                return self.job(old['id'],public=True)
            if db.execute("SELECT COUNT(*) FROM jobs WHERE state NOT IN ('completed','failed','cancelled','skipped')").fetchone()[0]>=8:raise DakshError('Job queue is full.')
            ident=secrets.token_hex(16);now=time.time()
            db.execute('INSERT INTO jobs(id,kind,state,created,updated,payload,result,idem) VALUES(?,?,?,?,?,?,?,?)',(ident,kind,'queued',now,now,json.dumps(payload),json.dumps({}),idem))
        self.emit('job_queued',{'job_id':ident,'kind':kind})
        return self.job(ident)

    def job(self,ident,public=True):
        with self.db() as db:row=db.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone()
        if not row:raise DakshError('Job not found.')
        result={**dict(row),'payload':json.loads(row['payload']),'result':json.loads(row['result'])}
        if public:
            result['payload']={k:v for k,v in result['payload'].items() if k not in {'model_path','data_path'}}
        return result

    def jobs(self):
        with self.db() as db:ids=[r[0] for r in db.execute('SELECT id FROM jobs ORDER BY created DESC LIMIT 100')]
        return [self.job(i) for i in ids]

    def cancel(self,ident):
        j=self.job(ident)
        if j['state'] not in TERMINAL:
            with self.db() as db:db.execute("UPDATE jobs SET cancel=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,updated=? WHERE id=?",(time.time(),ident))
            self.emit('job_cancel_requested',{'job_id':ident})
        return self.job(ident)

    def transition(self,ident,state,result=None):
        if state not in {'queued','running',*TERMINAL}:raise DakshError('Invalid job state.')
        with self.db() as db:db.execute('UPDATE jobs SET state=?,updated=?,result=? WHERE id=?',(state,time.time(),json.dumps(result or {}),ident))
        self.emit('job_'+state,{'job_id':ident})

    def schedule(self,spec,ident=None,now=None):
        now=time.time() if now is None else now
        allowed={'kind','enabled','frequency','hour','minute','weekday','timezone','defer_seconds','payload'}
        if not isinstance(spec,dict) or set(spec)-allowed:raise DakshError('Unsupported schedule fields.')
        if spec.get('kind') not in {'calibration','retraining'} or spec.get('frequency') not in {'daily','weekly'} or type(spec.get('enabled')) is not bool:raise DakshError('Invalid schedule kind, frequency or enabled state.')
        for k,low,high in [('hour',0,23),('minute',0,59),('weekday',0,6),('defer_seconds',0,7200)]:
            if type(spec.get(k,0)) is not int or not low<=spec.get(k,0)<=high:raise DakshError(f'Invalid {k}.')
        try:ZoneInfo(spec['timezone'])
        except (KeyError,ZoneInfoNotFoundError,TypeError):raise DakshError('Use an IANA timezone.')
        if not isinstance(spec.get('payload',{}),dict):raise DakshError('Invalid schedule payload.')
        ident=ident or secrets.token_hex(16)
        with self.db() as db:db.execute('INSERT OR REPLACE INTO schedules(id,spec,next,last,occurrence,job) VALUES(?,?,?,NULL,NULL,NULL)',(ident,json.dumps(spec),next_occurrence(spec,now)))
        self.emit('schedule_saved',{'schedule_id':ident,'kind':spec['kind']})
        return ident

    def schedules(self):
        with self.db() as db:rows=db.execute('SELECT * FROM schedules ORDER BY next').fetchall()
        return [{**dict(r),'spec':json.loads(r['spec'])} for r in rows]

    def remove_schedule(self,ident):
        with self.db() as db:db.execute('DELETE FROM schedules WHERE id=?',(ident,))
        self.emit('schedule_removed',{'schedule_id':ident})

    def tick_schedules(self,now=None):
        now=time.time() if now is None else now
        for item in self.schedules():
            spec=item['spec'];due=item['next']
            if not spec['enabled'] or due>now:continue
            if now-due>spec.get('defer_seconds',7200):
                outcome='skipped';job=None
            elif self.busy() or self.leased() or self.unmanaged():continue
            else:
                job=self.create_job(spec['kind'],spec.get('payload',{}),f"schedule:{item['id']}:{int(due)}")['id'];outcome='queued'
            with self.db() as db:
                db.execute('UPDATE schedules SET next=?,last=?,occurrence=?,job=? WHERE id=? AND next=?',(next_occurrence(spec,now),now,due,job,item['id'],due))
            self.emit('schedule_'+outcome,{'schedule_id':item['id'],'job_id':job})

    def add_record(self,run,signature,body,machine=None):
        if not isinstance(body,dict) or set(body)!={'features','speed','peak_gib'}:raise DakshError('Invalid benchmark record.')
        x=body['features']
        if not isinstance(x,list) or len(x)!=len(FEATURES) or any(finite(v) is None for v in x) or finite(body['speed']) is None or body['speed']<=0 or finite(body['peak_gib']) is None or body['peak_gib']<0:raise DakshError('Invalid benchmark numbers.')
        machine=machine or hashlib.sha256((platform.node()+platform.machine()).encode()).hexdigest()[:16]
        with self.db() as db:
            db.execute('INSERT INTO records(created,run,machine,signature,body) VALUES(?,?,?,?,?)',(time.time(),str(run)[:160],machine,str(signature)[:160],json.dumps(body)))
            db.execute('DELETE FROM records WHERE created<?',(time.time()-self.config()['retention_days']*86400,))
            db.execute('DELETE FROM records WHERE id NOT IN (SELECT id FROM records ORDER BY id DESC LIMIT ?)',(self.config()['maximum_records'],))
            # Enforce serialized payload budget as well as count/age.
            while db.execute('SELECT COALESCE(SUM(length(body)),0) FROM records').fetchone()[0]>self.config()['maximum_data_mb']*1024**2:
                db.execute('DELETE FROM records WHERE id IN (SELECT id FROM records ORDER BY id LIMIT 100)')

    def export_records(self):
        with self.db() as db:rows=db.execute('SELECT created,run,machine,signature,body FROM records ORDER BY created DESC LIMIT 5000').fetchall()
        return [{**dict(r),'body':json.loads(r['body'])} for r in rows]

    def delete_records(self):
        path=self.root/f'records-backup-{secrets.token_hex(8)}.jsonl'
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            with path.open('x') as output:
                os.chmod(path,0o600)
                for r in db.execute('SELECT created,run,machine,signature,body FROM records ORDER BY id'):
                    output.write(json.dumps({**dict(r),'body':json.loads(r['body'])})+'\n')
                output.flush();os.fsync(output.fileno())
            db.execute('DELETE FROM records')
        self.emit('records_cleared',{'recoverable':True})
        return {'backup_id':path.stem,'recoverable':True}

    def restore_records(self,backup_id):
        import re
        if not isinstance(backup_id,str) or not re.fullmatch(r'records-backup-[a-f0-9]{16}',backup_id):raise DakshError('Invalid recovery identifier.')
        path=self.root/(backup_id+'.jsonl')
        if path.is_symlink() or not path.is_file() or path.stat().st_size>1024**3:raise DakshError('Recovery copy unavailable.')
        # Validate the entire bounded local snapshot before a transactional restore.
        rows=[]
        with path.open() as source:
            for line in source:
                r=json.loads(line);body=r['body']
                if len(rows)>=50000 or set(body)!={'features','speed','peak_gib'} or len(body['features'])!=len(FEATURES) or any(finite(v) is None for v in body['features']) or finite(body['speed']) is None or finite(body['peak_gib']) is None:raise DakshError('Recovery copy failed validation.')
                rows.append((r['created'],r['run'],r['machine'],r['signature'],json.dumps(body)))
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT COUNT(*) FROM records').fetchone()[0]+len(rows)>self.config()['maximum_records']:raise DakshError('Restore would exceed retention count. Export or clear current records first.')
            db.executemany('INSERT INTO records(created,run,machine,signature,body) VALUES(?,?,?,?,?)',rows)
        self.emit('records_restored',{'count':len(rows)})
        return {'restored':len(rows)}

    def versions(self):
        with self.db() as db:rows=db.execute('SELECT * FROM versions ORDER BY created DESC LIMIT 20').fetchall()
        return [{**dict(r),'body':json.loads(r['body'])} for r in rows]

    def promote(self,ident,rollback=False):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');row=db.execute('SELECT * FROM versions WHERE id=?',(ident,)).fetchone()
            if not row:raise DakshError('Adviser version not found.')
            meta=json.loads(row['body'])
            if not rollback and not meta.get('eligible'):raise DakshError('Candidate did not pass promotion policy.')
            if rollback and not row['previous']:raise DakshError('Rollback requires a previously active version.')
            directory=self.root/'versions'/ident
            for name,digest in meta['hashes'].items():
                path=directory/name
                if path.is_symlink() or not path.is_file() or path.stat().st_size>100*1024**2 or digest_file(path)!=digest:raise DakshError('Adviser artifact integrity failed.')
            db.execute('UPDATE versions SET previous=1 WHERE active=1');db.execute('UPDATE versions SET active=0');db.execute('UPDATE versions SET active=1 WHERE id=?',(ident,))
        self.emit('adviser_rollback' if rollback else 'adviser_promoted',{'version_id':ident})

    def predict(self,host,config):
        active=next((v for v in self.versions() if v['active']),None)
        if not active:return None
        meta=active['body'];signature=config.get('signature')
        if signature not in meta.get('signatures',[]):return None
        try:
            directory=self.root/'versions'/active['id'];x=features(host,config)
            if any(v<lo or v>hi for v,lo,hi in zip(x,meta['minimum'],meta['maximum'])):return None
            values={}
            for name,digest in meta['hashes'].items():
                p=directory/name
                if p.is_symlink() or digest_file(p)!=digest:return None
                model=json.loads(p.read_text())
                def tree_value(tree):
                    while 'leaf_value' not in tree:
                        index=tree['split_feature'];threshold=tree['threshold']
                        if tree.get('decision_type')!='<=':raise ValueError('Unsupported tree split')
                        tree=tree['left_child'] if x[index]<=float(threshold) else tree['right_child']
                    return float(tree['leaf_value'])
                values[name]=sum(tree_value(tree['tree_structure']) for tree in model['tree_info'])
            return {'version':active['id'],'speed':max(0,values['speed.json']),'peak_upper_gib':max(0,values['memory.json'])*1.1}
        except (ImportError,ValueError,OSError,KeyError,TypeError):return None

    def incident(self,run,condition,evidence,active=True,now=None):
        now=time.time() if now is None else now
        with self.db() as db:
            row=db.execute('SELECT * FROM incidents WHERE run=? AND condition=? AND active=1',(run,condition)).fetchone()
            if not active:
                if row:db.execute('UPDATE incidents SET active=0,updated=? WHERE id=?',(now,row['id']))
                return False
            # Incident event rate is bounded independently from status sampling.
            body={'evidence':evidence,'automatic_action':False}
            if row:
                old=json.loads(row['body']);last=old.get('notified',0);start=row['started']
                notify=now-start>=self.service.settings()['persistence_seconds'] and now-last>=self.config()['cooldown_seconds']
                body['notified']=now if notify else last
                db.execute('UPDATE incidents SET updated=?,body=? WHERE id=?',(now,json.dumps(body),row['id']))
            else:
                notify=condition=='nonfinite_loss';body['notified']=now if notify else 0
                db.execute('INSERT INTO incidents(run,condition,started,updated,active,body) VALUES(?,?,?,?,1,?)',(run,condition,now,now,json.dumps(body)))
        if notify:self.emit('supervisor_incident',{'run_id':run,'condition':condition,'evidence':evidence})
        return notify

    def external_update(self,owner,state):
        with self.db() as db:
            db.execute('INSERT INTO external_training(owner,updated,body,stop) VALUES(?,?,?,0) ON CONFLICT(owner) DO UPDATE SET updated=excluded.updated,body=excluded.body',(owner,time.time(),json.dumps(state,allow_nan=False)))

    def external_state(self):
        owners={r['owner'] for r in self.leased()}
        with self.db() as db:rows=db.execute('SELECT * FROM external_training ORDER BY updated DESC').fetchall()
        for row in rows:
            if row['owner'] in owners:return json.loads(row['body'])
        return None

    def external_stop(self,owner):
        with self.db() as db:
            row=db.execute('SELECT stop FROM external_training WHERE owner=?',(owner,)).fetchone()
        return bool(row and row[0])

    def request_external_stop(self,owner):
        with self.db() as db:db.execute('UPDATE external_training SET stop=1 WHERE owner=?',(owner,))
        self.emit('external_checkpoint_stop_requested',{'run_id':owner})

    def monitor(self,training,stop_callback,now=None):
        now=time.time() if now is None else now
        if not self.service.settings()['enabled'] or training.get('status')!='running':
            with self.db() as db:db.execute('UPDATE incidents SET active=0 WHERE active=1')
            return
        host=self.host();run=str(training.get('run_id') or 'current');stats=training.get('stats') or {};config=self.config()
        timestamp=finite(host.get('timestamp'),now)
        if now-timestamp>max(30,3*config['sampling_seconds']):
            self.incident(run,'telemetry_stale',{'age_seconds':now-timestamp},now=now);return
        history=self.sample_history.setdefault(run,[])
        position=(int(training.get('epoch',1))-1)*int(training.get('batches',0))+int(training.get('batch',0))
        speed=finite(stats.get('batches_per_second'),0)
        validation=training.get('phase') in {'validation','checkpointing'} or training.get('batch')==training.get('batches')
        sample={'time':now,'position':position,'speed':speed,'swap':finite(host.get('swap_used_bytes'),0),'epoch':training.get('epoch'),'map':finite(stats.get('map50_95'))}
        history.append(sample);history[:]=[v for v in history if now-v['time']<=600]
        faults={}
        faults['memory_pressure']=finite(host.get('memory_percent'),0)>=self.service.settings()['warning_percent']
        faults['nonfinite_loss']=bool(stats.get('loss_nonfinite')) or any(type(stats.get(k)) in (int,float) and not math.isfinite(stats[k]) for k in ('box_loss','class_loss','dfl_loss','l1_loss'))
        window=[v for v in history if now-v['time']<=60]
        faults['swap_growth']=len(window)>1 and now-window[0]['time']>=45 and sample['swap']-window[0]['swap']>=config['swap_growth_mb']*1024**2
        baseline=[v['speed'] for v in history if 120<=now-v['time']<=240 and v['speed']>0]
        recent=[v['speed'] for v in history if now-v['time']<=60 and v['speed']>0]
        faults['throughput_drop']=not validation and bool(baseline and recent) and sum(recent)/len(recent)<sum(baseline)/len(baseline)*(1-config['throughput_drop_percent']/100)
        progress=[v for v in history if v['position']!=position]
        faults['stalled_progress']=not validation and bool(history) and now-(progress[-1]['time'] if progress else history[0]['time'])>=config['stall_seconds']
        # One metric per completed epoch; repeated polls never count as new epochs.
        with self.db() as db:
            if training.get('validation_epoch') and finite(stats.get('map50_95')) is not None:
                db.execute('INSERT OR REPLACE INTO validation_metrics VALUES(?,?,?)',(run,training['validation_epoch'],stats['map50_95']))
            rows=db.execute('SELECT map FROM validation_metrics WHERE run=? ORDER BY epoch DESC LIMIT ?',(run,config['validation_patience'])).fetchall()
        maps=[r[0] for r in reversed(rows)]
        faults['validation_decline']=len(maps)>=config['validation_patience'] and all(b<a for a,b in zip(maps,maps[1:]))
        for condition,active in faults.items():
            notify=self.incident(run,condition,{'phase':training.get('phase','training'),'memory_percent':host.get('memory_percent'),'batch_speed':speed},active,now)
            critical=condition=='nonfinite_loss' or condition=='memory_pressure' and finite(host.get('memory_percent'),0)>=95
            if notify and critical and config['automatic_stop']:
                stop_callback(run);self.emit('checkpoint_stop_requested',{'run_id':run,'condition':condition})
        if speed>0 and now-self._last_record.get(run,0)>=60 and training.get('model_signature'):
            self.add_record(run,training['model_signature'],{'features':features(host,{'batch':stats.get('batch_size',1),'backend':training.get('backend','cpu'),'image_size':training.get('image_size',640),'workers':training.get('workers',0),'model_mb':training.get('model_mb',0)}),'speed':speed*max(1,stats.get('batch_size',1)),'peak_gib':finite(host.get('memory_used_gb'),0)})
            self._last_record[run]=now

    def run_job(self,ident):
        job=self.job(ident,False)
        if job['state']!='queued':return
        if self.busy() or self.unmanaged():return
        try:self.acquire(ident)
        except DakshError:return
        directory=self.root/'jobs'/ident;directory.mkdir(parents=True,exist_ok=True)
        proc=None
        try:
            self.transition(ident,'running');request={**job['payload'],'kind':job['kind'],'root':str(self.root.resolve()),'job_id':ident,'guardian':identity()}
            request_path=directory/'request.json';request_path.write_text(json.dumps(request));os.chmod(request_path,0o600)
            with (directory/'worker.log').open('wb') as output:
                env={**os.environ,'OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1','MKL_NUM_THREADS':'1'}
                proc=subprocess.Popen([sys.executable,str(Path(__file__).with_name('daksh_worker.py')),'--request',str(request_path)],stdout=output,stderr=output,env=env)
                self.processes[ident]=proc;deadline=time.monotonic()+int(job['payload'].get('timeout_seconds',300))
                while proc.poll() is None:
                    latest=self.job(ident,False);memory=self.host()
                    progress_path=directory/'progress.json'
                    if progress_path.exists() and progress_path.stat().st_size<16384:
                        try:
                            progress=json.loads(progress_path.read_text())
                            with self.db() as db:db.execute('UPDATE jobs SET updated=?,result=? WHERE id=?',(time.time(),json.dumps(progress),ident))
                        except (OSError,ValueError):pass
                    available=finite(memory.get('memory_total_gb'),0)-finite(memory.get('memory_used_gb'),0)
                    total=finite(memory.get('memory_total_gb'),0)
                    rss=sum(p.memory_info().rss for p in [psutil.Process(proc.pid),*psutil.Process(proc.pid).children(recursive=True)] if p.is_running())/2**30
                    budget=min(1,total*.05) if job['kind']=='retraining' else max(0,available-self.service.settings()['reserve_gib'])+rss
                    if latest['cancel'] or self.stop_event.is_set() or not self.engine_status()['enabled'] or time.monotonic()>deadline or available<max(self.service.settings()['reserve_gib'],total*.2) or rss>budget:
                        proc.terminate()
                        try:proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
                        if latest['cancel']:self.transition(ident,'cancelled',{'reason':'User cancellation'})
                        elif self.stop_event.is_set() or not self.engine_status()['enabled']:self.transition(ident,'cancelled',{'reason':'Daksh engine stopped; detector training unaffected'})
                        else:self.transition(ident,'failed',{'reason':'Time/resource budget or service shutdown'})
                        return
                    time.sleep(.5)
                result_path=directory/'result.json'
                if not result_path.exists():raise DakshError('Worker failed; inspect bounded local job log.')
                result=json.loads(result_path.read_text())
                if proc.returncode or result.get('error'):raise DakshError(result.get('reason','Worker failed.'))
                if job['kind']=='calibration':
                    for record in result.get('records',[]):self.add_record(ident,result['signature'],record)
                elif job['kind']=='retraining':
                    meta=result['version'];version=meta['id']
                    with self.db() as db:db.execute('INSERT INTO versions VALUES(?,?,?,0,0)',(version,time.time(),json.dumps(meta)))
                self.transition(ident,'completed',result)
        except Exception as exc:self.transition(ident,'failed',{'reason':str(exc)[:240]})
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:proc.wait(timeout=5)
                except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
            self.processes.pop(ident,None);self.release(ident)

    def engine_status(self):
        with self.db() as db:
            row=db.execute('SELECT enabled FROM engine_state WHERE id=1').fetchone()
            lease=db.execute("SELECT pid,started FROM leases WHERE resource='scheduler'").fetchone()
        enabled=bool(row[0]) if row else True
        local_running=bool(self.thread and self.thread.is_alive())
        running=local_running or bool(lease and process_alive(lease[0],lease[1]))
        return {'enabled':enabled,'running':running,'stopping':running and (not enabled or (local_running and self.stop_event.is_set()))}

    def control_engine(self,enabled,training,stop_callback):
        if type(enabled) is not bool:raise DakshError('Engine enabled must be a boolean.')
        with self.engine_lock:
            if enabled and self.engine_status()['stopping']:raise DakshError('Engine is still stopping; retry when stopped.')
            with self.db() as db:db.execute('INSERT OR REPLACE INTO engine_state VALUES(1,?)',(int(enabled),))
            if enabled:self.start(training,stop_callback)
            else:self.close()
            self.emit('engine_start_requested' if enabled else 'engine_stop_requested',{'enabled':enabled})
            try:self.flush_events()
            except Exception:self.last_error='Audit delivery pending; event retained for retry.'
            return self.engine_status()

    def start(self,training,stop_callback):
        with self.engine_lock:self._start(training,stop_callback)

    def _start(self,training,stop_callback):
        if not self.engine_status()['enabled']:return
        if self.thread and self.thread.is_alive():return
        self.stop_event.clear()
        def loop():
            owner='scheduler:'+str(os.getpid())
            try:
                while not self.stop_event.is_set() and self.engine_status()['enabled']:
                    try:
                        self.acquire(owner,'scheduler')
                        state=training()
                        if state.get('status')!='running':state=self.external_state() or state
                        self.monitor(state,stop_callback);self.tick_schedules();self.flush_events()
                        # Recover jobs owned by dead processes, never auto-resume detector training.
                        for j in self.jobs():
                            if j['state']=='running' and j['id'] not in self.processes and not any(x['owner']==j['id'] for x in self.leased()):self.transition(j['id'],'failed',{'reason':'Worker interrupted; explicit retry required.'})
                        queued=next((j for j in reversed(self.jobs()) if j['state']=='queued'),None)
                        if queued:self.run_job(queued['id'])
                    except Exception as exc:
                        # A failed monitor cannot terminate detector training; expose its failure.
                        self.last_error=type(exc).__name__+": "+str(exc)[:180]
                    self.stop_event.wait(self.config()['sampling_seconds'])
            finally:
                self.release(owner)
        self.thread=threading.Thread(target=loop,name='daksh-supervisor',daemon=True);self.thread.start()

    def close(self):
        with self.engine_lock:
            self.stop_event.set()
            if self.thread:self.thread.join(timeout=8)

    def recommend(self,payload,host,busy=False,model_path=None):
        result=self.service.preflight(payload,host,busy)
        from runtime_compat import training_resource_profile
        available=finite(host.get('memory_total_gb'),0)-finite(host.get('memory_used_gb'),0)
        profile=training_resource_profile(cpu_threads=psutil.cpu_count() or 1,available_ram_gb=available,accelerator='cpu')
        image_size=min(payload.get('image_size',640),int(profile['image_size']))
        workers=min(payload.get('workers',2),int(profile['workers']),2)
        precision=payload.get('precision','fp32')
        result['recommended_settings']={'batch_size':result['effective_batch_size'],'image_size':image_size,'workers':workers,'precision':precision}
        result['calibration_status']='not calibrated for this workload'
        if model_path and Path(model_path).is_file():
            from daksh_worker import signature
            backend='mps' if payload.get('device')=='mps' else 'cuda' if str(payload.get('device','')).startswith('gpu:') else 'cpu'
            sig=signature(model_path,backend,image_size);reserve=result['snapshot']['reserve_gib'];total=finite(host.get('memory_total_gb'),0)
            completed=[j for j in self.jobs() if j['kind']=='calibration' and j['state']=='completed' and j['result'].get('signature')==sig and time.time()-j['updated']<30*86400]
            if completed:
                measured=completed[0]['result']['records'];requested=payload.get('batch_size') or result['effective_batch_size']
                candidates=[r for r in measured if r['features'][5]<=requested and r['peak_gib']*1.1<=total-reserve]
                result['calibration_status']='fresh' if candidates else 'fresh; no candidate fits current reserve'
                if candidates:
                    best=max(candidates,key=lambda r:r['speed']);result['effective_batch_size']=min(result['effective_batch_size'],int(best['features'][5]))
                    result['reasons'].append('Checked against measured calibration for this model/runtime/hardware signature.')
            predictions=[]
            for batch in range(1,result['effective_batch_size']+1):
                config={'signature':sig,'backend':backend,'batch':batch,'image_size':image_size,'workers':workers,'model_mb':Path(model_path).stat().st_size/2**20}
                prediction=self.predict(host,config)
                if prediction and prediction['peak_upper_gib']<=total-reserve:predictions.append((batch,prediction))
            if predictions:
                batch,prediction=max(predictions,key=lambda item:item[1]['speed']);result.update({'effective_batch_size':batch,'method':'validated predictor + hard rules','confidence':'within measured coverage','prediction':prediction})
                result['reasons'].append('Prediction is advisory; memory guard remains authoritative.')
        result['recommended_settings']['batch_size']=result['effective_batch_size']
        with self.db() as db:db.execute('UPDATE recommendations SET body=? WHERE id=?',(json.dumps(result),result['recommendation_id']))
        return result
