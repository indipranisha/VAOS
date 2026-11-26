(() => {
 'use strict';
 const $=id=>document.getElementById(id);
 async function request(path,method='GET',body){
  const r=await fetch('/api/admin/training-adviser/'+path,{method,credentials:'same-origin',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  let d;try{d=await r.json();}catch{throw Error('Daksh-AI is unavailable. Restart the updated backend after training finishes.');}
  if(!r.ok)throw Error(r.status===404?'Daksh-AI requires the updated backend.':typeof d.detail==='string'?d.detail:'Daksh request failed');return d;
 }
 const status=message=>{const target=$('daksh-status')||$('training-message');if(target)target.textContent=message;};
 function node(tag,text){const e=document.createElement(tag);if(text!=null)e.textContent=text;return e;}
 function action(label,fn){const b=node('button',label);b.type='button';b.className='button button-secondary button-small';b.addEventListener('click',async()=>{b.disabled=true;try{await fn();}catch(e){status(e.message);}finally{b.disabled=false;}});return b;}
 const number=id=>Number($(id).value);
 function workload(){const max=number('daksh-max-batch');if(!Number.isInteger(max)||max<1||max>10)throw Error('Choose a maximum batch from 1 to 10.');return {source:$('daksh-dataset').value,initial_model:$('daksh-model').value,device:$('daksh-device').value,batches:Array.from({length:max},(_,i)=>i+1),image_size:640,timeout_seconds:number('daksh-duration'),precision:'fp32'};}
 const panel=$('daksh-settings');
 if(panel){
  let policyLoaded=false;
  async function load(initial=false){
   try{
    const [d,jobs,schedules,versions]=await Promise.all([request('status'),request('jobs'),request('schedules'),request('versions')]);
    const engine=d.engine||{running:d.monitor_running,enabled:true};$('daksh-engine-state').textContent=engine.stopping?'Daksh-AI is stopping':engine.running?'Daksh-AI is running':engine.enabled?'Daksh-AI is waiting to start':'Daksh-AI is stopped';$('daksh-engine-start').disabled=engine.running;$('daksh-engine-stop').disabled=!engine.running;
    if(initial||!policyLoaded){$('daksh-enabled').checked=d.settings.enabled;$('daksh-auto-stop').checked=d.settings.supervisor.automatic_stop;
     for(const [id,key] of [['daksh-reserve','reserve_gib'],['daksh-pressure','warning_percent'],['daksh-persistence','persistence_seconds']])$(id).value=d.settings[key];
     for(const [id,key] of [['daksh-swap', 'swap_growth_mb'], ['daksh-speed-drop', 'throughput_drop_percent'], ['daksh-stall', 'stall_seconds'], ['daksh-validation-patience', 'validation_patience'], ['daksh-retention', 'retention_days'], ['daksh-record-limit', 'maximum_records'], ['daksh-data-limit', 'maximum_data_mb']])$(id).value=d.settings.supervisor[key];$('daksh-sampling').value=d.settings.supervisor.sampling_seconds;$('daksh-cooldown').value=d.settings.supervisor.cooldown_seconds;policyLoaded=true;}
    $('daksh-jobs').replaceChildren(...jobs.jobs.map(j=>{const row=node('div');row.className='api-setting-toggle';const text=node('span',`${j.kind} · ${j.state} · ${new Date(j.updated*1000).toLocaleString()} · ${j.result.reason||j.result.phase||''}${j.result.candidate_batch?' · batch '+j.result.candidate_batch:''}`);row.append(text);if(!['completed','failed','cancelled','skipped'].includes(j.state))row.append(action('Cancel',async()=>{await request(`jobs/${j.id}/cancel`,'POST',{});await load();}));return row;}));
    $('daksh-schedules').replaceChildren(...schedules.schedules.map(s=>{const row=node('div');row.className='api-setting-toggle';row.append(node('span',`${s.spec.kind} · ${s.spec.enabled?'enabled':'disabled'} · next ${new Date(s.next*1000).toLocaleString()} (${s.spec.timezone})`));row.append(action(s.spec.enabled?'Disable':'Enable',async()=>{const allowed=['source','initial_model','device','batches','image_size','timeout_seconds','precision'];const payload=s.spec.kind==='calibration'?Object.fromEntries(Object.entries(s.spec.payload||{}).filter(([key])=>allowed.includes(key))):{};await request('schedules/'+s.id,'PATCH',{...s.spec,enabled:!s.spec.enabled,payload});await load();}));row.append(action('Edit',()=>{$('daksh-schedule-form').dataset.editId=s.id;$('daksh-schedule-kind').value=s.spec.kind;$('daksh-frequency').value=s.spec.frequency;$('daksh-time').value=String(s.spec.hour).padStart(2,'0')+':'+String(s.spec.minute).padStart(2,'0');$('daksh-timezone').value=s.spec.timezone;$('daksh-schedule-enabled').checked=s.spec.enabled;if(s.spec.kind==='calibration'){const p=s.spec.payload;$('daksh-dataset').value=p.source;$('daksh-model').value=p.initial_model||'best.pt';$('daksh-device').value=p.device;$('daksh-max-batch').value=Math.max(...p.batches);$('daksh-duration').value=p.timeout_seconds;}$('daksh-schedule-form').querySelector('button[type=submit]').textContent='Save schedule changes';$('daksh-schedule-kind').focus();}));row.append(action('Remove schedule',async()=>{if(!confirm('Remove this Daksh schedule?'))return;await request('schedules/'+s.id,'DELETE');await load();}));return row;}));
    $('daksh-versions').replaceChildren(...versions.versions.map(v=>{const row=node('div');row.className='api-setting-toggle';row.append(node('span',`${v.id} · ${v.active?'active':v.body.eligible?'eligible candidate':'did not pass promotion policy'} · ${v.body.coverage||''}`));if(v.body.eligible&&!v.active)row.append(action('Promote',async()=>{if(!confirm('Promote this validated adviser?'))return;await request(`versions/${v.id}/promote`,'POST',{});await load();}));if(v.previous&&!v.active)row.append(action('Roll back',async()=>{if(!confirm('Restore this previous adviser?'))return;await request(`versions/${v.id}/rollback`,'POST',{});await load();}));return row;}));
    $('daksh-incidents').replaceChildren(...d.incidents.map(i=>node('p',`${i.active?'Active':'Recovered'} · ${i.condition} · ${new Date(i.updated*1000).toLocaleString()}`)));
    if(!d.incidents.length)$('daksh-incidents').textContent='No sustained incidents recorded.';
    status(`${d.monitor_running?'Supervisor running':'Supervisor not running'} · ${d.leases.length} resource lease(s) · ${d.unmanaged_training_pids.length} potential conflicting process(es). ${d.limitations}${d.supervisor_error?' Supervisor error: '+d.supervisor_error:''}`);
   }catch(e){status(e.message);}
  }
  for(const command of ['start','stop'])$('daksh-engine-'+command).addEventListener('click',async()=>{try{await request('engine/'+command,'POST',{});await load();}catch(e){status(e.message);}});
  $('daksh-history-train').addEventListener('click',async()=>{const b=$('daksh-history-train');b.disabled=true;try{const r=await request('history/retrain','POST',{});$('daksh-history-status').textContent=`Imported ${r.import.epochs} epochs from ${r.import.runs} runs (${r.import.checkpoints} checkpoint files). ${r.note}`;await load();}catch(e){status(e.message);}finally{b.disabled=false;}});
  $('daksh-policy-form').addEventListener('submit',async event=>{event.preventDefault();try{await request('settings','PATCH',{enabled:$('daksh-enabled').checked,reserve_gib:number('daksh-reserve'),warning_percent:number('daksh-pressure'),persistence_seconds:number('daksh-persistence'),supervisor:{automatic_stop:$('daksh-auto-stop').checked,sampling_seconds:number('daksh-sampling'),cooldown_seconds:number('daksh-cooldown'),swap_growth_mb:number('daksh-swap'),throughput_drop_percent:number('daksh-speed-drop'),stall_seconds:number('daksh-stall'),validation_patience:number('daksh-validation-patience'),retention_days:number('daksh-retention'),maximum_records:number('daksh-record-limit'),maximum_data_mb:number('daksh-data-limit')}});await load(true);}catch(e){status(e.message);}});
  async function createJob(kind){const configuration=kind==='calibration'?workload():{};if(!confirm(kind==='calibration'?'Run bounded hardware calibration when idle?':'Retrain the adviser from retained measurements when idle?'))return;const result=await request('jobs','POST',{kind,configuration,idempotency_key:crypto.randomUUID()});status('Queued '+result.kind);await load();}
  $('daksh-calibrate').addEventListener('click',()=>createJob('calibration').catch(e=>status(e.message)));$('daksh-retrain').addEventListener('click',()=>createJob('retraining').catch(e=>status(e.message)));
  $('daksh-schedule-form').addEventListener('submit',async e=>{e.preventDefault();try{const [hour,minute]=$('daksh-time').value.split(':').map(Number);const kind=$('daksh-schedule-kind').value;const edit=$('daksh-schedule-form').dataset.editId;await request(edit?'schedules/'+edit:'schedules',edit?'PATCH':'POST',{kind,enabled:$('daksh-schedule-enabled').checked,frequency:$('daksh-frequency').value,hour,minute,weekday:0,timezone:$('daksh-timezone').value,defer_seconds:7200,payload:kind==='calibration'?workload():{}});delete $('daksh-schedule-form').dataset.editId;$('daksh-schedule-form').querySelector('button[type=submit]').textContent='Add schedule';await load();}catch(error){status(error.message);}});
  $('daksh-refresh').addEventListener('click',()=>load());
  $('daksh-export').addEventListener('click',async()=>{try{const data=await request('records');const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=node('a');a.href=url;a.download='daksh-benchmarks.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){status(e.message);}});
  $('daksh-restore').addEventListener('click',async()=>{const backup_id=prompt('Enter the recovery identifier shown when records were cleared:');if(!backup_id)return;try{const result=await request('records/restore','POST',{backup_id});status('Restored '+result.restored+' records.');}catch(e){status(e.message);}});
  $('daksh-clear').addEventListener('click',async()=>{if(!confirm('Clear retained numeric benchmark records? A local recovery copy will be kept.'))return;try{const r=await request('records','DELETE');status('Records cleared. Recovery copy: '+r.backup_id);}catch(e){status(e.message);}});
  async function loadSources(){try{const r=await fetch('/api/admin/ml/datasets/managed',{credentials:'same-origin'});if(!r.ok)throw Error('Dataset catalogue unavailable.');const d=await r.json();$('daksh-dataset').replaceChildren(...(d.datasets||[]).map(item=>{const option=node('option',item.name||item.id);option.value='dataset:'+item.id;return option;}));const m=await fetch('/api/admin/ml/model-sources',{credentials:'same-origin'});if(m.ok){const sources=await m.json();for(const item of sources.sources||[]){const option=node('option',item.name);option.value=item.id;$('daksh-model').append(option);}}}catch(e){status(e.message);}}
  $('daksh-timezone').value=Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC';loadSources();load(true);const poll=setInterval(()=>{if(!document.hidden)load();},10000);window.addEventListener('pagehide',()=>clearInterval(poll),{once:true});
 }
 window.DakshPreflight=async payload=>{
  const d=await request('preflight','POST',payload);
  return new Promise(resolve=>{
   const dialog=node('dialog');dialog.className='distribution-modal';dialog.setAttribute('aria-label','Daksh suggests:');
   const body=node('div');body.className='distribution-body';const title=node('h2','Daksh suggests:');
   const summary=node('p',`Requested batch: ${d.requested_batch_size??'automatic'}. Recommended: ${d.effective_batch_size}. Confidence: ${d.confidence}. `+d.reasons.join(' '));
   const settings=node('p',`Image size ${d.recommended_settings?.image_size||640}, workers ${d.recommended_settings?.workers??2}, precision ${d.recommended_settings?.precision||'fp32'}. Calibration: ${d.calibration_status||'not available'}.`);
   body.append(title,summary,settings);const previousFocus=document.activeElement;let settled=false;
   function finish(value){if(settled)return;settled=true;if(dialog.open)dialog.close();dialog.remove();previousFocus?.focus();resolve(value);}
   for(const [label,mode] of [['Accept recommendations','recommended'],['Use manual settings','manual'],['Cancel',null]]){const b=node('button',label);b.type='button';b.className='button button-secondary';b.disabled=!!mode&&!d.can_start;b.addEventListener('click',()=>finish(mode?{...payload,daksh_recommendation_id:d.recommendation_id,daksh_mode:mode}:null));body.append(b);}
   dialog.append(body);dialog.addEventListener('cancel',()=>finish(null));document.body.append(dialog);dialog.showModal();
  });
 };
 // Ongoing training advisories are sampled independently of page polling.
 if($('start-training')){
  const notice=node('div');notice.className='helper';notice.setAttribute('role','status');$('training-message').after(notice);
  const resume=action('Review checkpoint resume',async()=>{
   const r=await fetch('/api/admin/ml/train/checkpoints',{credentials:'same-origin'});const d=await r.json();const c=(d.checkpoints||[]).find(x=>x.resume_supported);
   if(!c)throw Error('No verified resumable checkpoint is available.');
   const payload={checkpoint_id:c.id,batch_size:Number($('training-batch-size').value),device:c.device};
   if(!confirm(`Resume after epoch ${c.epoch} toward the original ${c.epochs}-epoch target? Batch changes may affect optimization.`))return;
   const accepted=await window.DakshPreflight(payload);if(!accepted)return;
   const response=await fetch('/api/admin/ml/train/resume',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(accepted)});const result=await response.json();if(!response.ok)throw Error(result.detail||'Resume failed.');notice.textContent=result.note;
  });$('training-message').after(resume);
  async function incidents(){try{const d=await request('status');const active=d.incidents.filter(i=>i.active);notice.textContent=active.length?'Daksh advisories: '+active.map(i=>i.condition).join(', ')+'. Use Stop safely to request a checkpoint stop.':'';}catch{notice.textContent='Daksh monitoring status unavailable; check backend version.';}}
  const timer=setInterval(incidents,10000);window.addEventListener('pagehide',()=>clearInterval(timer),{once:true});incidents();
 }
})();
