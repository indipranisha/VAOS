"""History import and independent held-out CPU model acceptance tests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from daksh_runtime import Runtime
from daksh_history import import_history,summary

class HistoryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.rt=Runtime(self.root/'daksh');self.training=self.root/'training'
 def run_fixture(self,name='a',epochs=30):
  p=self.training/'runs'/name;p.mkdir(parents=True)
  (p/'args.yaml').write_text('batch: 4\nimgsz: 640\nworkers: 2\ndevice: cpu\ndata: /private/dataset.yaml\n')
  (p/'results.csv').write_text('epoch,time,train/box_loss\n'+''.join(f'{i},{i*10},{1/i}\n' for i in range(1,epochs+1)))
  return p
 def test_import_is_idempotent_and_memory_is_unknown(self):
  self.run_fixture();a=import_history(self.rt,self.training);b=import_history(self.rt,self.training)
  self.assertEqual((a['inserted'],b['inserted'],summary(self.rt)['epochs']),(30,0,30))
  with self.rt.db() as db:body=json.loads(db.execute('SELECT body FROM history LIMIT 1').fetchone()[0])
  self.assertIsNone(body['peak_gib']);self.assertNotIn('/private',json.dumps(body));self.assertEqual(body['epoch_seconds'],10)
 def test_partial_rows_and_epoch_gaps_not_fabricated(self):
  p=self.run_fixture();(p/'results.csv').write_text('epoch,time\n1,10\n3,30\n4,40\n5,50')
  r=import_history(self.rt,self.training);self.assertEqual(r['epochs'],2)
  with self.rt.db() as db:self.assertEqual([r[0] for r in db.execute('SELECT epoch FROM history ORDER BY epoch')],[1,4])
 def test_external_symlink_rejected_and_invalid_numbers_skipped(self):
  p=self.run_fixture();outside=self.root/'outside';outside.write_text('epoch,time\n1,10\n');(p/'results.csv').unlink();(p/'results.csv').symlink_to(outside)
  r=import_history(self.rt,self.training);self.assertEqual(r['epochs'],0);self.assertEqual(len(r['issues']),1)
  (p/'results.csv').unlink();(p/'results.csv').write_text('epoch,time\n1,nan\n2,inf\n');self.assertEqual(import_history(self.rt,self.training)['epochs'],0)
 def test_cpu_historical_candidate_is_never_safety_promotable(self):
  for i in range(4):self.run_fixture(str(i))
  import_history(self.rt,self.training);directory=self.root/'job';directory.mkdir();req=directory/'request.json';req.write_text(json.dumps({'root':str(self.rt.root),'job_id':'candidate','kind':'retraining','history_only':True}))
  env={**os.environ,'OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1'}
  p=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'daksh_worker.py'),'--request',str(req)],env=env,capture_output=True,text=True,timeout=60)
  self.assertEqual(p.returncode,0,p.stderr);v=json.loads((directory/'result.json').read_text())['version'];self.assertFalse(v['eligible']);self.assertEqual(v['runs'],4);self.assertEqual(v['train_records']+v['test_records'],120);self.assertIn('history_duration.json',v['hashes'])
 def test_engine_stop_persists_without_stopping_detector(self):
  stop=[]
  self.rt.control_engine(False,lambda:{},lambda run:stop.append(run))
  other=Runtime(self.rt.root);other.start(lambda:{},lambda run:stop.append(run));self.assertFalse(other.engine_status()['running']);self.assertFalse(other.engine_status()['enabled']);self.assertEqual(stop,[])
  with patch.object(other,'monitor'),patch.object(other,'tick_schedules'),patch.object(other,'flush_events'):
   other.control_engine(True,lambda:{},lambda run:stop.append(run));self.assertTrue(other.engine_status()['running']);other.control_engine(False,lambda:{},lambda run:stop.append(run))
  self.assertFalse(other.engine_status()['running']);self.assertEqual(stop,[])
