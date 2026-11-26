"""Offline Daksh orchestration and safety contracts; no user datasets or GPU work."""
import datetime as dt
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
from daksh_ai import DakshError
from daksh_runtime import Runtime,next_occurrence,features,digest_file

class RuntimeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.host={'memory_total_gb':36,'memory_used_gb':16,'memory_percent':44,'timestamp':1000,'swap_used_bytes':0,'cpu_count':8}
  self.events=[];self.rt=Runtime(Path(self.tmp.name),host=lambda:self.host,audit=lambda *v:self.events.append(v))
 def spec(self):return {'kind':'retraining','enabled':True,'frequency':'daily','hour':8,'minute':0,'timezone':'Asia/Kolkata','defer_seconds':7200,'payload':{}}
 def test_config_validation(self):
  for p in [{'automatic_stop':1},{'sampling_seconds':0},{'maximum_records':999999},{'unknown':True}]:
   with self.assertRaises(DakshError):self.rt.configure(p)
  self.rt.configure({'sampling_seconds':2});self.assertEqual(self.rt.config()['sampling_seconds'],2)
 def test_lease_lifecycle(self):
  self.rt.acquire('a')
  with self.assertRaises(DakshError):self.rt.acquire('b')
  self.rt.release('wrong');self.assertEqual(len(self.rt.leased()),1)
  self.rt.release('a');self.rt.acquire('b');self.assertEqual(self.rt.leased()[0]['owner'],'b')
 def test_scheduler_lease_not_training_contention(self):
  self.rt.acquire('scheduler','scheduler');self.assertEqual(self.rt.leased(),[])
 def test_stale_pid_identity(self):
  with self.rt.db() as db:db.execute('INSERT INTO leases VALUES(?,?,?,?,?)',('training','old',os.getpid(),0,0))
  self.rt.acquire('new');self.assertEqual(self.rt.leased()[0]['owner'],'new')
 def test_cross_process_lease(self):
  self.rt.acquire('parent')
  code='from daksh_runtime import Runtime;from pathlib import Path;Runtime(Path('+repr(self.tmp.name)+')).acquire("child")'
  result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=15)
  self.assertNotEqual(result.returncode,0);self.assertIn('Another managed job',result.stderr)
 def test_context_exception_release(self):
  with self.assertRaises(ValueError):
   with self.rt.resource('owner'):raise ValueError('fixture')
  self.assertEqual(self.rt.leased(),[])
 def test_idempotency_cancel(self):
  j=self.rt.create_job('retraining',{},'request-001');self.assertEqual(j['id'],self.rt.create_job('retraining',{},'request-001')['id'])
  with self.assertRaises(DakshError):self.rt.create_job('retraining',{'x':1},'request-001')
  self.assertEqual(self.rt.cancel(j['id'])['state'],'cancelled');self.rt.cancel(j['id'])
 def test_queue_limit(self):
  for i in range(8):self.rt.create_job('retraining',{},f'request-{i}')
  with self.assertRaises(DakshError):self.rt.create_job('retraining',{},'request-full')
 def test_job_payload_redaction(self):
  j=self.rt.create_job('calibration',{'model_path':'private','data_path':'private'},'request-safe')
  self.assertNotIn('model_path',j['payload']);self.assertNotIn('data_path',j['payload'])
 def test_schedule_timezone(self):
  spec=self.spec();now=dt.datetime(2026,11,26,0,tzinfo=dt.timezone.utc).timestamp();next_=next_occurrence(spec,now)
  self.assertEqual(dt.datetime.fromtimestamp(next_,ZoneInfo('Asia/Kolkata')).hour,8)
 def test_schedule_dst_gap_and_fold(self):
  spec={**self.spec(),'timezone':'America/New_York','hour':2,'minute':30}
  now=dt.datetime(2026,3,8,0,tzinfo=dt.timezone.utc).timestamp();v=dt.datetime.fromtimestamp(next_occurrence(spec,now),ZoneInfo(spec['timezone']));self.assertEqual((v.hour,v.minute),(3,0))
  spec.update(hour=1,minute=30);now=dt.datetime(2026,11,1,0,tzinfo=dt.timezone.utc).timestamp();first=next_occurrence(spec,now);second=next_occurrence(spec,first+1);self.assertGreater(second-first,20*3600)
 def test_schedule_single_dispatch(self):
  now=dt.datetime(2026,11,26,0,tzinfo=dt.timezone.utc).timestamp();ident=self.rt.schedule(self.spec(),now=now)
  due=self.rt.schedules()[0]['next']
  with patch.object(self.rt,'unmanaged',return_value=[]):self.rt.tick_schedules(due+1);self.rt.tick_schedules(due+2)
  self.assertEqual(len(self.rt.jobs()),1);self.assertEqual(self.rt.schedules()[0]['id'],ident)
 def test_busy_defer_and_skip(self):
  self.rt.busy=lambda:True;self.rt.schedule(self.spec(),now=1000);due=self.rt.schedules()[0]['next']
  self.rt.tick_schedules(due+10);self.assertEqual(self.rt.jobs(),[]);self.assertEqual(self.rt.schedules()[0]['next'],due)
  self.rt.tick_schedules(due+7201);self.assertGreater(self.rt.schedules()[0]['next'],due)
 def test_schedule_disabled_invalid(self):
  self.rt.schedule({**self.spec(),'enabled':False},now=1000);self.rt.tick_schedules(1e9);self.assertEqual(self.rt.jobs(),[])
  for changes in [{'timezone':'bad/zone'},{'hour':24},{'frequency':'hourly'},{'enabled':'yes'}]:
   with self.assertRaises(DakshError):self.rt.schedule({**self.spec(),**changes})
 def test_audit_retry_identity(self):
  ident=self.rt.emit('fixture',{'job_id':'x'});self.rt.audit=lambda *args:False;self.rt.flush_events()
  with self.rt.db() as db:self.assertEqual(db.execute('SELECT delivered FROM events WHERE id=?',(ident,)).fetchone()[0],0)
  self.rt.audit=lambda *args:self.events.append(args);self.rt.flush_events();self.rt.flush_events();self.assertEqual(len(self.events),1);self.assertEqual(self.events[0][2],ident)
 def test_unknown_hardware_prediction_fallback(self):self.assertIsNone(self.rt.predict(self.host,{}))
 def test_record_validation_and_recoverable_clear(self):
  body={'features':features(self.host,{}),'speed':5,'peak_gib':20};self.rt.add_record('run','sig',body);self.assertEqual(len(self.rt.export_records()),1)
  with self.assertRaises(DakshError):self.rt.add_record('x','y',{**body,'speed':float('nan')})
  result=self.rt.delete_records();self.assertEqual(self.rt.export_records(),[]);self.assertTrue((self.rt.root/(result['backup_id']+'.jsonl')).exists());self.assertEqual(self.rt.restore_records(result['backup_id'])['restored'],1)
 def test_monitor_nonfinite_auto_stop(self):
  self.rt.configure({'automatic_stop':True});stops=[]
  self.rt.monitor({'status':'running','run_id':'r','epoch':1,'batch':1,'batches':100,'stats':{'loss_nonfinite':True}},stops.append,now=1000)
  self.assertEqual(stops,['r']);self.assertTrue(self.rt.service.incidents()[0]['active'])
 def test_memory_persistence_and_recovery(self):
  training={'status':'running','run_id':'r','epoch':1,'batch':1,'batches':100,'stats':{}};self.host['memory_percent']=96
  self.rt.configure({'automatic_stop':True});stops=[];self.rt.monitor(training,stops.append,1000);self.assertEqual(stops,[])
  self.host['timestamp']=1040;self.rt.monitor(training,stops.append,1040);self.assertEqual(stops,['r'])
  self.host['memory_percent']=44;self.host['timestamp']=1045;self.rt.monitor(training,stops.append,1045)
  self.assertFalse(next(i for i in self.rt.service.incidents() if i['condition']=='memory_pressure')['active'])
 def test_stale_telemetry_never_auto_stops(self):
  self.rt.configure({'automatic_stop':True});stops=[];self.host['timestamp']=1;self.host['memory_percent']=99
  self.rt.monitor({'status':'running','run_id':'r','stats':{}},stops.append,1000);self.assertEqual(stops,[])
 def test_validation_epochs_not_poll_count(self):
  t={'status':'running','run_id':'r','epoch':1,'batch':10,'batches':20,'validation_epoch':1,'stats':{'map50_95':.8}}
  for i in range(3):self.rt.monitor(t,lambda _:None,1000+i)
  with self.rt.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM validation_metrics').fetchone()[0],1)
 def test_monitor_swap_and_stall(self):
  self.rt.configure({'stall_seconds':60});t={'status':'running','run_id':'r','epoch':1,'batch':10,'batches':20,'stats':{}}
  self.rt.monitor(t,lambda _:None,1000);self.host.update(timestamp=1060,swap_used_bytes=1024**3);self.rt.monitor(t,lambda _:None,1060)
  with self.rt.db() as db:conditions={r[0] for r in db.execute('SELECT condition FROM incidents WHERE active=1')}
  self.assertIn('swap_growth',conditions);self.assertIn('stalled_progress',conditions)
 def test_calibration_candidate_recommendation_bound(self):
  result=self.rt.recommend({'batch_size':10},self.host)
  self.assertEqual(result['heading'],'Daksh suggests:');self.assertLessEqual(result['effective_batch_size'],10)
 def test_version_reject_unknown_and_unvalidated(self):
  with self.assertRaises(DakshError):self.rt.promote('../escape')
  with self.rt.db() as db:db.execute('INSERT INTO versions VALUES(?,?,?,?,?)',('x',0,json.dumps({'eligible':False}),0,0))
  with self.assertRaises(DakshError):self.rt.promote('x')
 def test_cancel_queued_worker_does_not_spawn(self):
  j=self.rt.create_job('retraining',{},'request-stop');self.rt.cancel(j['id'])
  with patch('daksh_runtime.subprocess.Popen') as spawn:self.rt.run_job(j['id']);spawn.assert_not_called()
 def test_worker_is_terminated_on_monitor_exception(self):
  class Child:
   pid=os.getpid()
   terminated=False
   def poll(self):return 1 if self.terminated else None
   def terminate(self):self.terminated=True
   def kill(self):self.terminated=True
   def wait(self,timeout=None):return 1
  child=Child();j=self.rt.create_job('retraining',{},'request-host-error')
  self.rt.host=lambda:(_ for _ in ()).throw(RuntimeError('telemetry fixture failed'))
  with patch.object(self.rt,'unmanaged',return_value=[]),patch('daksh_runtime.subprocess.Popen',return_value=child):self.rt.run_job(j['id'])
  self.assertTrue(child.terminated);self.assertEqual(self.rt.job(j['id'])['state'],'failed');self.assertEqual(self.rt.leased(),[])

 def test_busy_job_remains_queued(self):
  self.rt.busy=lambda:True;j=self.rt.create_job('retraining',{},'request-busy');self.rt.run_job(j['id']);self.assertEqual(self.rt.job(j['id'])['state'],'queued')
