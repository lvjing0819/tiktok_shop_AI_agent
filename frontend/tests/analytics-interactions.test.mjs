import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { JSDOM } from 'jsdom';

const markup = (await readFile(new URL('../analytics.html',import.meta.url),'utf8')).replace(/<style>[\s\S]*?<\/style>/g,'').replace(/<script[\s\S]*?<\/script>/g,'');
// Execute the production entry and its real DOM handlers. Only canvas drawing,
// Hub navigation and the network are replaced; no TikTok or model requests run.
const source = (await readFile(new URL('../src/analytics.js',import.meta.url),'utf8')).replace(/^import .*;\n/gm,'');
const shop = {id:'1',name:'Test Shop',region:'MY',currency:'MYR',timezone:'Asia/Kuala_Lumpur'};
const sources = ['orders','finance','affiliate','performance','videos'];
function report(filters) {
 const period = () => ({coverage:Object.fromEntries(sources.map(s=>[s,{status:'missing',retrieved_at:null,evidence:[],stale:false}])),orders:null,finance:null,affiliate:null,performance:null});
 return {window:filters,previous_window:{...filters,start_date:'2026-11-08',end_date:'2026-11-14'},shops:[{shop,current:period(),previous:period(),skus:[]}],notes:[]};
}
async function app(t) {
 const dom = new JSDOM(markup,{url:'http://localhost/analytics.html?view=dashboard',runScripts:'outside-only',pretendToBeVisual:true});
 t.after(()=>dom.window.close());
 const w=dom.window,logs=[],timers=new Map();let nextTimer=0;
 const NativeDate=w.Date;
 w.Date=class extends NativeDate {constructor(...args){super(...(args.length?args:[2026,10,22,12]));}static now(){return new NativeDate(2026,10,22,12).getTime();}};
 w.Chart=class {static defaults={};destroy(){}resize(){}};
 w.initializeHub=()=>w.document.getElementById('dashboardView').classList.remove('hidden');
 w.setInterval=()=>0;
 w.setTimeout=(fn)=>{const id=++nextTimer;timers.set(id,fn);return id;};w.clearTimeout=id=>timers.delete(id);
 w.fetch=async(path,options={})=>{
  const url=new URL(path,'http://localhost');const body=options.body?JSON.parse(options.body):null;logs.push({url,body});
  let data;
  if(url.pathname.endsWith('/shops')) data={shops:[shop]};
  else if(url.pathname.endsWith('/dashboard')) data=report(Object.fromEntries(url.searchParams));
  else if(url.pathname.endsWith('/ensure')) data={reason:'running',job:{id:body.start_date,status:'running',total:10,progress:0,created_at:w.Date.now()/1000}};
  else if(url.pathname.includes('/refresh/')) data={id:url.pathname.split('/').at(-1),status:'running',total:10,progress:0,created_at:w.Date.now()/1000};
  else throw Error('Unexpected request: '+path);
  return {ok:true,status:200,json:async()=>data};
 };
 w.eval(source);
 const wait=async(predicate)=>{for(let i=0;i<40;i++){if(predicate())return;await new Promise(resolve=>setImmediate(resolve));}assert.ok(predicate(),'UI did not settle: '+w.document.getElementById('dataStatus').textContent);};
 await wait(()=>logs.some(r=>r.url.pathname.includes('/refresh/')));
 const $=id=>w.document.getElementById(id);
 const apply=async(start,end)=>{$('startDateInput').value=start;$('endDateInput').value=end;$('endDateInput').dispatchEvent(new w.Event('change',{bubbles:true}));await new Promise(resolve=>setImmediate(resolve));};
 return {w,$,logs,timers,wait,apply};
}

test('custom range keeps the calendar open after the first click and applies both dates',async t=>{
 const {$,wait}=await app(t);$('openPickerBtn').click();
 $('dualCalendars').querySelector('[data-date="2026-11-10"]').click();
 assert.equal($('datePopover').classList.contains('hidden'),false,'first date must not dismiss the popup');
 $('dualCalendars').querySelector('[data-date="2026-11-18"]').click();
 assert.equal($('datePopover').classList.contains('hidden'),false);
 $('applyRangeBtn').click();await wait(()=>$('startDateInput').value==='2026/11/10');
 assert.equal($('endDateInput').value,'2026/11/18');assert.equal($('datePopover').classList.contains('hidden'),true);
});

test('month navigation stays open and adjacent-month dates remain selectable',async t=>{
 const {$}=await app(t);$('openPickerBtn').click();$('dualCalendars').querySelector('[data-month="-1"]').click();
 assert.equal($('datePopover').classList.contains('hidden'),false,'month navigation must not dismiss the popup');
 assert.equal($('dualCalendars').querySelector('[data-date="2026-10-31"]').disabled,false);
});

test('returning A → B → A resumes the first range job instead of leaving blank cards',async t=>{
 const {logs,apply,wait}=await app(t);
 await apply('2026/11/14','2026/11/20');await wait(()=>logs.some(r=>r.url.pathname.endsWith('/ensure')&&r.body.start_date==='2026-11-14'));
 await apply('2026/11/15','2026/11/21');
 await wait(()=>logs.filter(r=>r.url.pathname.endsWith('/ensure')&&r.body.start_date==='2026-11-15').length===2);
 assert.ok(logs.filter(r=>r.url.pathname.endsWith('/refresh/2026-11-15')).length>=2,'must reattach to the existing job');
});

test('the 30-day preset navigates to its start month without applying until confirmed',async t=>{
 const {$}=await app(t);$('openPickerBtn').click();$('datePopover').querySelector('[data-preset="last30"]').click();
 assert.equal($('startDateInput').value,'2026/11/15');
 assert.match($('dualCalendars').textContent,/2026年 10月/);
 const start=$('dualCalendars').querySelector('[data-date="2026-10-24"]');assert.ok(start);assert.equal(start.disabled,false);
});
