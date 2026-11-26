"""Opt-in isolated CPU smoke: real checkpoint stop/resume and bounded calibration.

Run with .venv/bin/python tools/validate_daksh_cpu.py. Uses generated images,
random tiny weights, one CPU thread and a temporary VAOS data directory.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

def main():
 with tempfile.TemporaryDirectory(prefix='daksh-cpu-validation-') as folder:
  root=Path(folder);os.environ['VAOS_DATA_DIR']=str(root/'data');os.environ['MODEL_DIR']=str(root/'models');os.environ['VAOS_UPDATE_CHECK']='0'
  import torch
  torch.set_num_threads(1)
  from PIL import Image
  import yaml
  import ultralytics
  from ultralytics import YOLO
  tiny=root/'tiny.yaml';tiny.write_text(yaml.safe_dump({'nc':1,'backbone':[[-1,1,'Conv',[8,3,2]],[-1,1,'Conv',[16,3,2]],[-1,1,'Conv',[32,3,2]]],'head':[[[1,2],1,'Detect',['nc']]]}))
  detector=YOLO(str(tiny),task='detect');active=root/'data'/'ml_training'/'active';active.mkdir(parents=True);weights=active/'best.pt';detector.save(str(weights));del detector
  dataset=root/'dataset'
  for split in ('train','val'):
   for kind in ('images','labels'):(dataset/kind/split).mkdir(parents=True)
   for i in range(4):
    Image.new('RGB',(64,64),(100+i*10,120,140)).save(dataset/'images'/split/f'{i}.jpg')
    (dataset/'labels'/split/f'{i}.txt').write_text('0 0.5 0.5 0.4 0.4\n')
  manifest=dataset/'dataset.yaml';manifest.write_text(yaml.safe_dump({'path':str(dataset),'train':'images/train','val':'images/val','names':{0:'person'}}))
  import main as app
  app.available_cpu_threads=lambda:1
  # This fixture uses CPU only and explicitly excludes unrelated live GPU jobs.
  app.daksh_runtime().unmanaged=lambda:[]
  app.load_public_dataset_imports=lambda:{'fixture':{'id':'fixture','managed_local_dataset':True,'data_yaml':str(manifest),'classes':['person'],'name':'CPU fixture','image_count':8,'train_count':4,'val_count':4}}
  original=ultralytics.YOLO
  def stopping_model(*args,**kwargs):
   model=original(*args,**kwargs)
   model.add_callback('on_train_batch_end',lambda trainer:app.ml_training_state.update({'stop_requested':True}))
   return model
  ultralytics.YOLO=stopping_model
  app.ml_training_state={'status':'preparing','stop_requested':False};app.ml_training_lock.acquire()
  app.run_ml_training('cpu:1','dataset:fixture','best.pt',False,2,1,None,{'image_size':320,'workers':0,'precision':'fp32'})
  assert app.ml_training_state['status']=='stopped',app.ml_training_state
  ident=app.ml_training_state['checkpoint_id']
  with app.daksh_runtime().db() as db:meta=json.loads(db.execute('SELECT body FROM checkpoints WHERE id=?',(ident,)).fetchone()[0])
  checkpoint=torch.load(meta['path'],map_location='cpu',weights_only=False)
  assert checkpoint.get('optimizer') is not None,'Optimizer stripped before preservation'
  assert checkpoint['epoch']==0
  del checkpoint
  ultralytics.YOLO=original
  app.ml_training_state={'status':'preparing','stop_requested':False};app.ml_training_lock.acquire()
  app.run_ml_training('cpu:1','dataset:fixture','best.pt',False,2,2,meta,None)
  assert app.ml_training_state['status']=='complete',app.ml_training_state
  assert app.ml_training_state['epoch']==2,app.ml_training_state
  from daksh_worker import calibrate
  from daksh_runtime import digest_file
  before=digest_file(weights);directory=root/'calibration';directory.mkdir()
  calibrate({'model_path':str(weights),'data_path':str(manifest),'backend':'cpu','device':'cpu','batches':[1],'image_size':320,'precision':'fp32','timeout_seconds':60},directory)
  result=json.loads((directory/'result.json').read_text());assert result['records'];assert digest_file(weights)==before
  print(json.dumps({'checkpoint_epoch':meta['epoch'],'resumed_final_epoch':app.ml_training_state['epoch'],'batch_changed_to':2,'calibration_records':len(result['records']),'weights_unchanged':True,'device':'cpu','threads':1}))
if __name__=='__main__':main()
