"""Daksh-AI: bounded, local training preflight and advisory supervision.

No learned predictor is enabled until validated benchmark coverage exists.
"""
from __future__ import annotations
import hashlib
import json
import math
import secrets
import sqlite3
import time
from pathlib import Path

DEFAULTS = {'enabled': True, 'mode': 'advisory', 'reserve_gib': 6, 'warning_percent': 85, 'persistence_seconds': 30}

class DakshError(ValueError):
    pass

class Daksh:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS recommendations(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,body TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS incidents(id INTEGER PRIMARY KEY,run TEXT,condition TEXT,started REAL,updated REAL,active INTEGER,body TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS active_condition ON incidents(run,condition) WHERE active=1;''')

    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def settings(self):
        with self.db() as db:
            row = db.execute('SELECT body FROM settings WHERE id=1').fetchone()
        return {**DEFAULTS, **(json.loads(row[0]) if row else {})}

    def configure(self, payload):
        if not isinstance(payload, dict) or set(payload)-set(DEFAULTS):
            raise DakshError('Unsupported Daksh setting.')
        settings = {**self.settings(), **payload}
        if type(settings['enabled']) is not bool or settings['mode'] != 'advisory':
            raise DakshError('Preflight supports advisory mode only; configure automatic checkpoint stops in supervisor settings.')
        for key, low, high in [('reserve_gib',6,64),('warning_percent',70,95),('persistence_seconds',10,300)]:
            value=settings[key]
            if type(value) is not int or not low<=value<=high:
                raise DakshError(f'{key} must be an integer from {low} to {high}.')
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES(1,?)',(json.dumps(settings),))
        return settings

    @staticmethod
    def fingerprint(payload):
        return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

    def preflight(self, payload, host, busy=False, now=None):
        now=time.time() if now is None else now
        if not isinstance(payload,dict): raise DakshError('Training settings must be an object.')
        requested=payload.get('batch_size')
        if requested is not None and (type(requested) is not int or not 1<=requested<=10):
            raise DakshError('Batch size must be null or an integer from 1 to 10.')
        settings=self.settings()
        total=host.get('memory_total_gb');used=host.get('memory_used_gb')
        known=all(type(x) in (int,float) and math.isfinite(x) and x>=0 for x in (total,used)) and total>0 and used<=total
        reserve=max(settings['reserve_gib'],total*.2) if known else settings['reserve_gib']
        available=max(0,total-used) if known else 0
        cap=max(1,min(10,int(max(0,available-reserve)/1.5))) if known else 1
        if host.get('memory_percent',0)>=settings['warning_percent']:cap=min(cap,2)
        vram_known=True
        if str(payload.get('device','')).startswith('gpu:'):
            gpu_total=host.get('gpu_memory_total_mb');gpu_used=host.get('gpu_memory_used_mb')
            vram_known=all(type(v) in (int,float) and math.isfinite(v) for v in (gpu_total,gpu_used)) and gpu_total>0
            if vram_known:cap=min(cap,max(1,int(max(0,(gpu_total-gpu_used)/1024-2)/1.5)))
            else:cap=1
        effective=min(requested or 4,cap)
        reasons=['Conservative rule estimate; no validated learned predictor is installed.']
        if not known: reasons.append('Memory telemetry unavailable; limited confidence and minimum batch.')
        if effective!=(requested or 4): reasons.append('Batch reduced to reserve memory for the operating system.')
        if not vram_known: reasons.append('CUDA memory capacity is unavailable; starting requires verified capacity.')
        if busy: reasons.append('Training is active; wait before starting another job.')
        result={'heading':'Daksh suggests:', 'recommendation_id':secrets.token_hex(16),'requested_batch_size':requested,'effective_batch_size':effective,
                'confidence':'limited','method':'rules','reasons':reasons,'busy':bool(busy),'can_start':not busy and known and vram_known and available>reserve,
                'snapshot':{'total_gib':total,'available_gib':available if known else None,'reserve_gib':reserve,'timestamp':now},
                'expires_at':now+60,'capabilities':{'learned_predictor':False,'calibration':False,'scheduled_jobs':False,'checkpoint_resume':False}}
        with self.db() as db:
            db.execute('DELETE FROM recommendations WHERE expires<?',(now,))
            db.execute('INSERT INTO recommendations VALUES(?,?,?,?)',(result['recommendation_id'],self.fingerprint(payload),json.dumps(result),now+60))
        return result

    def accept(self, identifier, payload, host, mode, now=None, return_settings=False):
        now=time.time() if now is None else now
        if mode not in ('recommended','manual'): raise DakshError('Choose recommended or manual.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM recommendations WHERE id=?',(identifier,)).fetchone()
            if not row or row['expires']<now or row['fingerprint']!=self.fingerprint(payload):
                raise DakshError('Recommendation expired or settings changed. Run preflight again.')
            original=json.loads(row['body'])
            # Recompute fresh bounds without writing inside this transaction.
            total=host.get('memory_total_gb');used=host.get('memory_used_gb')
            if not all(type(x) in (int,float) and math.isfinite(x) for x in (total,used)) or total<=0:
                raise DakshError('Fresh memory telemetry is required.')
            reserve=max(self.settings()['reserve_gib'],total*.2)
            available=total-used
            cap=max(1,min(10,int(max(0,available-reserve)/1.5)))
            if host.get('memory_percent',0)>=self.settings()['warning_percent']:cap=min(cap,2)
            if str(payload.get('device','')).startswith('gpu:'):
                gpu_total=host.get('gpu_memory_total_mb');gpu_used=host.get('gpu_memory_used_mb')
                if not all(type(v) in (int,float) and math.isfinite(v) for v in (gpu_total,gpu_used)) or gpu_total<=0:
                    raise DakshError('Fresh CUDA memory capacity is required.')
                cap=min(cap,max(1,int(max(0,(gpu_total-gpu_used)/1024-2)/1.5)))
            chosen=original['effective_batch_size'] if mode=='recommended' else payload.get('batch_size') or 4
            if not original['can_start'] or available<=reserve or chosen>cap:
                raise DakshError('Resource conditions do not permit these settings. Run preflight again.')
            effective=original.get('recommended_settings',{'batch_size':chosen})
            if mode=='manual':
                if payload.get('image_size',effective.get('image_size',640))>effective.get('image_size',768) or payload.get('workers',effective.get('workers',2))>effective.get('workers',4):
                    raise DakshError('Manual resolution/workers exceed the current recommendation. Reconfigure and run preflight again.')
                effective={**effective,**{k:payload[k] for k in ('image_size','workers','precision') if k in payload},'batch_size':chosen}
            db.execute('DELETE FROM recommendations WHERE id=?',(identifier,))
        return effective if return_settings else chosen

    def observe(self, training, host, now=None):
        now=time.time() if now is None else now
        if not self.settings()['enabled'] or training.get('status')!='running':return []
        run=str(training.get('run_id') or training.get('source') or 'current')[:160]
        stats=training.get('stats') or {}
        conditions={
            'memory_pressure':type(host.get('memory_percent')) in (int,float) and host['memory_percent']>=self.settings()['warning_percent'],
            'nonfinite_loss':any(type(stats.get(k)) in (int,float) and not math.isfinite(stats[k]) for k in ['box_loss','class_loss','dfl_loss','l1_loss'])}
        with self.db() as db:
            for condition,active in conditions.items():
                row=db.execute('SELECT * FROM incidents WHERE run=? AND condition=? AND active=1',(run,condition)).fetchone()
                if active:
                    body=json.dumps({'condition':condition,'action':'Review resources and request a safe checkpoint stop if needed.','automatic_action':False})
                    if row:db.execute('UPDATE incidents SET updated=?,body=? WHERE id=?',(now,body,row['id']))
                    else:db.execute('INSERT INTO incidents(run,condition,started,updated,active,body) VALUES(?,?,?,?,1,?)',(run,condition,now,now,body))
                elif row:db.execute('UPDATE incidents SET active=0,updated=? WHERE id=?',(now,row['id']))
            db.execute('DELETE FROM incidents WHERE updated<?',(now-90*86400,))
        return self.incidents(now)

    def incidents(self, now=None):
        now=time.time() if now is None else now
        with self.db() as db:rows=db.execute('SELECT * FROM incidents ORDER BY id DESC LIMIT 100').fetchall()
        persistence=self.settings()['persistence_seconds']
        return [{**dict(row),'details':json.loads(row['body'])} for row in rows if row['condition']=='nonfinite_loss' or row['updated']-row['started']>=persistence]
