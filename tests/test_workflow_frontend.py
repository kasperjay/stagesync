import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node is required for frontend state tests')
class WorkflowFrontendTests(unittest.TestCase):
    def test_order_stale_response_and_failed_advance(self):
        source = Path(__file__).resolve().parents[1] / 'stagesync/web/static/workflow.js'
        script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const nodes = new Map();
class Element {
  constructor() { this.style = {}; this.value = ''; this.open = false; this.html = ''; }
  set innerHTML(value) {
    this.html = value;
    for (const match of value.matchAll(/id="([^"]+)"/g)) nodes.set('#' + match[1], new Element());
    if (value.includes('role="status"')) nodes.set('[role=status]', new Element());
  }
  get innerHTML() { return this.html; }
  querySelector(key) { return nodes.get(key); }
  querySelectorAll() { return []; }
  append() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
}
const requests = [];
let mode = 'normal', loads = 0;
const pending = new Map();
function state(id) { return {review:{source:id, confidence:null, evidence:{}, confirmations:{venue:true}, values:{venue:{name:'Venue'},show:{name:'Show',date:'2026-01-01'},artist:'Artist',song:'Song'},revision:'r1',resolution:'manual',original_artist:'',flagged:false,approved:false},draft:null,jobs:[]}; }
const context = {console,Intl,Date,JSON,Math,Number,String,Error,encodeURIComponent,
 document:{createElement:()=>new Element(),body:new Element(),head:new Element(),getElementById:()=>null},
 alert:()=>{},prompt:()=>null,confirm:()=>false,loadData:async()=>{loads++},
 openVenueAssistModal:()=>{},openSetlistAssistModal:()=>{},openBandAssistModal:()=>{},openSongAssistModal:()=>{},
 fetch:async(path,options)=>{
   requests.push(path);
   if(mode==='deferred') return new Promise(resolve=>pending.set(path,resolve));
   if(path.endsWith('/approve')) return {ok:false,status:409,json:async()=>({error:'Save failed'})};
   return {ok:true,json:async()=>state(path)};
 }};
context.window=context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
(async()=>{
  await context.openWorkflow('first');
  let html=nodes.get('#wf-controls').innerHTML;
  const keys=['venue','show','artist','song'];
  const indexes=keys.map(key=>html.indexOf('id="wf-confirm-'+key+'"'));
  assert(indexes.every((v,i)=>v>=0 && (!i||v>indexes[i-1])), 'Review fields must render in dependency order');
  assert.match(html,/id="wf-confirm-artist" disabled/);
  assert.match(html,/id="wf-confirm-song" disabled/);
  assert.doesNotMatch(html,/id="wf-confirm-show" disabled/);
  const before=requests.length;
  await nodes.get('#wf-approve').onclick();
  assert.equal(loads,0,'Failed approval must not reload or advance');
  assert.deepEqual(requests.slice(before),['/api/workflow/first/approve']);
  assert.equal(nodes.get('[role=status]').textContent,'Save failed');
  mode='deferred';
  const old=context.openWorkflow('old');
  const newer=context.openWorkflow('new');
  pending.get('/api/workflow/new')({ok:true,json:async()=>state('new evidence')});
  await newer;
  const current=nodes.get('#wf-controls');
  pending.get('/api/workflow/old')({ok:true,json:async()=>state('old evidence')});
  await old;
  assert.equal(nodes.get('#wf-controls'),current,'Stale response must not replace current controls');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run(['node', '-e', script, str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_root_navigation_reaches_workflow_controls(self):
        source = Path(__file__).resolve().parents[1] / 'stagesync/web/index.html'
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(process.argv[1],'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1].replace('refreshDashboard(); setInterval(refreshDashboard,15000);','');
const calls=[];
const child={setTab:value=>calls.push(['tab',value]),setStatusFilter:value=>calls.push(['filter',value]),closeWorkflow:()=>calls.push(['close']),openWorkflowLibrary:value=>calls.push(['library',value]),openPublicationQueue:()=>calls.push(['publish']),openScanModal:()=>calls.push(['scan'])};
const element={style:{},classList:{toggle:()=>{}},contentWindow:child,addEventListener:()=>{}};
const context={document:{getElementById:()=>element,querySelector:()=>element,querySelectorAll:()=>[]},console};
vm.createContext(context);vm.runInContext(script,context);
context.switchView('review');assert.deepEqual(calls.at(-1),['library','review']);
context.switchView('shows');assert.deepEqual(calls.at(-1),['library','library']);
context.switchView('archive');assert.deepEqual(calls.at(-1),['library','archive']);
context.switchView('publish');assert.deepEqual(calls.at(-1),['publish']);
assert(!calls.some(c=>c[0]==='filter'&&c[1]==='APPROVED'),'Publish must open the editor, not an approved catalog filter');
context.openScanModal();assert.deepEqual(calls.at(-1),['scan']);
'''
        result = subprocess.run(['node', '-e', script, str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
