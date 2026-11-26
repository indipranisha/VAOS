"""Bounded CPU/RSS measurement of rules supervision using generated telemetry."""
import json
from pathlib import Path
import sys
import tempfile
import time
import psutil
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from daksh_runtime import Runtime
with tempfile.TemporaryDirectory(prefix='daksh-monitor-benchmark-') as folder:
 host={'memory_total_gb':36,'memory_used_gb':20,'memory_percent':56,'swap_used_bytes':0,'timestamp':1000}
 rt=Runtime(Path(folder),host=lambda:host);process=psutil.Process();rss=process.memory_info().rss;before=process.cpu_times();start=time.perf_counter()
 state={'status':'running','run_id':'fixture','model_signature':'fixture','epoch':1,'batches':1000,'batch':0,'stats':{'batch_size':4,'batches_per_second':2}}
 for i in range(100):
  host['timestamp']=1000+i*5;state['batch']=i;rt.monitor(state,lambda _:None,host['timestamp'])
 elapsed=time.perf_counter()-start;after=process.cpu_times();cpu=(after.user+after.system)-(before.user+before.system)
 print(json.dumps({'samples':100,'wall_seconds':round(elapsed,4),'cpu_seconds':round(cpu,4),'rss_delta_mib':round((process.memory_info().rss-rss)/2**20,2),'estimated_one_core_percent_at_5s':round(cpu/100/5*100,3),'scope':'generated rules-monitor telemetry; excludes GPU probes and learned inference'}))
