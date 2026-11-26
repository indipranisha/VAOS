import tempfile
import unittest
from pathlib import Path
from daksh_ai import Daksh,DakshError

class DakshTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.d=Daksh(Path(self.tmp.name)/'state.db');self.host={'memory_total_gb':36,'memory_used_gb':20,'memory_percent':56}
 def test_defaults_and_persistence(self):
  self.d.configure({'enabled':False});self.assertFalse(Daksh(self.d.path).settings()['enabled'])
 def test_invalid_settings(self):
  for p in [{'mode':'automatic'},{'reserve_gib':0},{'enabled':1},{'unknown':True}]:
   with self.assertRaises(DakshError):self.d.configure(p)
 def test_bounds(self):
  for n in [0,11,True,1.5,'6']:
   with self.assertRaises(DakshError):self.d.preflight({'batch_size':n},self.host)
 def test_recommendation_and_replay(self):
  p={'batch_size':10};r=self.d.preflight(p,self.host,now=1);self.assertLess(r['effective_batch_size'],10)
  self.assertEqual(self.d.accept(r['recommendation_id'],p,self.host,'recommended',now=2),r['effective_batch_size'])
  with self.assertRaises(DakshError):self.d.accept(r['recommendation_id'],p,self.host,'recommended',now=3)
 def test_expired_changed_and_pressure(self):
  p={'batch_size':4};r=self.d.preflight(p,self.host,now=1)
  for payload,host,now in [({'batch_size':3},self.host,2),(p,self.host,62),(p,{**self.host,'memory_used_gb':35},2)]:
   with self.assertRaises(DakshError):self.d.accept(r['recommendation_id'],payload,host,'manual',now=now)
 def test_unknown_and_busy(self):
  self.assertFalse(self.d.preflight({},{} )['can_start']);self.assertFalse(self.d.preflight({},self.host,True)['can_start'])
 def test_incident_persistence_and_recovery(self):
  t={'status':'running','run_id':'r','stats':{}};h={**self.host,'memory_percent':90}
  self.assertEqual(self.d.observe(t,h,now=1),[]);self.assertEqual(len(self.d.observe(t,h,now=40)),1)
  self.d.observe(t,self.host,now=41);self.assertFalse(self.d.incidents()[0]['active'])
