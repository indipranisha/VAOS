'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const d = require('../static/class_distribution.js');
const fixture = () => ({counts:{confirmed:3,corrected:1,pending:100},confirmed_class_counts:[{label:'person',count:3},{label:'car',count:1}],reviewable_class_counts:[{label:'truck',count:100}],trained_model:{installed:true,metadata:{class_counts:{person:20,car:0},run_id:'fixture-run',trained_at:'2026-11-24T00:00:00Z',path:'/private/model',token:'never-export'}}});
test('sources preserve distinct units and totals independent of grid data',()=>{
 const snap=d.snapshot(fixture(),'2026-11-24T01:00:00Z');
 assert.equal(d.view(snap,'reviewed','').total,4);
 assert.equal(d.view(snap,'reviewed','car').total,4);
 assert.equal(d.view(snap,'reviewed','car').rows[0].percentage_of_source_total,25);
 assert.equal(d.view(snap,'model','').total,20);
 assert.equal(d.view(snap,'model','car').rows[0].count,0);
});
test('snapshot is isolated and all/filtered exports match scope',()=>{
 const data=fixture(),snap=d.snapshot(data);data.confirmed_class_counts[0].count=999;
 assert.equal(d.view(snap,'reviewed','').total,4);
 assert.equal(d.exportData(snap,'reviewed','car',false,false).rows.length,2);
 const filtered=d.exportData(snap,'reviewed','car',true,true);
 assert.equal(filtered.rows.length,1);assert.equal(filtered.source_total,4);assert.equal(filtered.stale,true);
 assert.equal(filtered.count_unit,'reviewed crops');
 const model=JSON.stringify(d.exportData(snap,'model','',false,false));
 assert.ok(!model.includes('/private/model'));assert.ok(!model.includes('never-export'));
});
test('missing model counts stay unavailable; empty reviewed data is valid',()=>{
 const data=fixture();data.trained_model.metadata={classes:['person']};
 assert.match(d.snapshot(data).modelNote,/not recorded/);
 data.trained_model.installed=false;assert.match(d.snapshot(data).modelNote,/No trained model/);
 data.counts={};data.confirmed_class_counts=[];assert.equal(d.view(d.snapshot(data),'reviewed','').total,0);
});
test('malformed counts and inconsistent totals are rejected',()=>{
 for(const value of [-1,NaN,Infinity,'3',null,1.5]){
  const data=fixture();data.confirmed_class_counts[0].count=value;assert.throws(()=>d.snapshot(data));
 }
 const data=fixture();data.counts.confirmed=99;assert.throws(()=>d.snapshot(data),/totals/);
 delete data.confirmed_class_counts;assert.throws(()=>d.snapshot(data),/unavailable/);
});
test('CSV escapes Unicode, separators, newlines and spreadsheet formulas',()=>{
 const data=fixture();data.counts={confirmed:4};data.confirmed_class_counts=[{label:'=HYPERLINK("bad")',count:1},{label:' café, "车"\nname',count:1},{label:'\t+cmd',count:1},{label:'normal',count:1}];
 const out=d.csv(d.exportData(d.snapshot(data),'reviewed','',false,false));
 assert.ok(out.includes("'="));assert.ok(out.includes("'\t+cmd"));assert.ok(out.includes('café, ""车""\nname'));
 assert.ok(out.startsWith('source,class_label,count,'));
});
