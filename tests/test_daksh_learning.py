"""CPU-only synthetic learning/registry validation. No detector training or user data."""
import importlib.util
import json
from pathlib import Path
import random
import platform
from daksh_runtime import OS
import tempfile
import unittest
from daksh_runtime import Runtime,FEATURES,digest_file
import subprocess
import sys

def retrain(request,directory):
    request={**request,"kind":"retraining"}
    path=directory/"request.json";path.write_text(json.dumps(request))
    worker=Path(__file__).resolve().parents[1]/"daksh_worker.py"
    result=subprocess.run([sys.executable,str(worker),"--request",str(path)],capture_output=True,text=True,timeout=60)
    if result.returncode:raise ValueError(result.stderr)
from daksh_ai import DakshError

@unittest.skipUnless(importlib.util.find_spec('lightgbm'),'optional LightGBM unavailable')
class LearningTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.rt=Runtime(self.root)
 def test_insufficient_data_refused(self):
  directory=self.root/'job';directory.mkdir()
  with self.assertRaisesRegex(ValueError,'100 benchmark'):retrain({'root':str(self.root),'job_id':'candidate'},directory)
 def test_train_evaluate_native_artifacts_and_integrity(self):
  rng=random.Random(42)
  for group in range(8):
   for i in range(40):
    batch=1+i%10;available=10+rng.random()*5
    x=[36,available,8,0,OS.get(platform.system(),3),batch,640,0,5]
    self.rt.add_record(f'run{group}','signature',{'features':x,'speed':2+batch*.8+rng.random()*.05,'peak_gib':20+batch*.1},machine='fixture')
  directory=self.root/'job';directory.mkdir();retrain({'root':str(self.root),'job_id':'candidate'},directory)
  result=json.loads((directory/'result.json').read_text());meta=result['version']
  self.assertEqual(meta['features'],FEATURES);self.assertLess(meta['bytes'],10*1024**2);self.assertGreater(meta['test_records'],20)
  self.assertIn('memory_coverage',meta['metrics']);self.assertFalse(result['promoted'])
  # Registry safety is exercised with explicit test approval independent of model quality.
  meta['eligible']=True
  with self.rt.db() as db:db.execute('INSERT INTO versions VALUES(?,?,?,?,?)',('candidate',0,json.dumps(meta),0,0))
  self.rt.promote('candidate');self.assertTrue(self.rt.versions()[0]['active'])
  prediction=self.rt.predict({'memory_total_gb':36,'memory_used_gb':24,'cpu_count':8},{'signature':'signature','backend':'cpu','batch':4,'image_size':640,'workers':0,'model_mb':5})
  self.assertIsNotNone(prediction);self.assertGreater(prediction['speed'],0);self.assertGreater(prediction['peak_upper_gib'],0)
  self.assertIsNone(self.rt.predict({'memory_total_gb':36,'memory_used_gb':20},{'signature':'unseen'}))
  artifact=self.root/'versions'/'candidate'/'speed.json';artifact.write_text('tampered')
  with self.assertRaisesRegex(DakshError,'integrity'):self.rt.promote('candidate')
 def test_complete_run_split_required(self):
  for i in range(110):self.rt.add_record('same-run','sig',{'features':[36,10,8,0,1,4,640,0,5],'speed':4,'peak_gib':20})
  directory=self.root/'job';directory.mkdir()
  with self.assertRaisesRegex(ValueError,'six independent'):retrain({'root':str(self.root),'job_id':'candidate'},directory)
