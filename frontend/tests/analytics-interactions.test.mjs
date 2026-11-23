import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { JSDOM } from 'jsdom';

const markup = (await readFile(new URL('../analytics.html',import.meta.url),'utf8')).replace(/<style>[\s\S]*?<\/style>/g,'').replace(/<script[\s\S]*?<\/script>/g,'');
// Execute the production entry and its real DOM handlers. Only canvas drawing,
// Hub navigation and the network are replaced; no TikTok or model requests run.
const source = (await readFile(new URL('../src/analytics.js',import.meta.url),'utf8')).replace(/^import .*;\n/gm,'');
const definitions=JSON.parse(await readFile(new URL('../../app/metric_definitions.json',import.meta.url),'utf8'));
const metricSource=(await readFile(new URL('../src/metricDisplay.js',import.meta.url),'utf8')).replace(/^import .*;\n/gm,'').replace(/^export /gm,'');
const shop = {id:'1',name:'Test Shop',region:'MY',currency:'MYR',timezone:'Asia/Kuala_Lumpur'};
const sources = ['orders','finance','affiliate','performance','videos'];
function report(filters) {
 const period = () => ({coverage:Object.fromEntries(sources.map(s=>[s,{status:'missing',retrieved_at:null,evidence:[],stale:false}])),orders:null,finance:null,affiliate:null,performance:null});
 return {window:filters,previous_window:{...filters,start_date:'2026-11-08',end_date:'2026-11-14'},shops:[{shop,current:period(),previous:period(),skus:[]}],notes:[]};
}
async function app(t, reply = value => value) {
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
  data = reply(data, url);
  return {ok:true,status:200,json:async()=>data};
 };
 w.metricCatalog=definitions;w.eval(metricSource);
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

test('page checkpoints render local orders even when source completion count is unchanged',async t=>{
 let phase=0;
 const {$,timers,wait}=await app(t,(data,url)=>{
  if(url.pathname.includes('/refresh/')) return {...data,data_version:phase,active_source:{source:'orders',shop:'Test Shop',period:'current',rows_read:2,rows_total:100}};
  if(phase && url.pathname.endsWith('/dashboard')) {
   data.shops[0].current.orders={total:2,delivered:1,cancelled:0,in_transit:1,statuses:[],regions:[],cancellation_reasons:[],hours:Array(24).fill(0),skus:[]};
   data.shops[0].current.coverage.orders={status:'partial',storage:'local_database',observed_days:7,total_days:7,evidence:[]};
  }
  return data;
 });
 phase=1; const next=timers.values().next().value;assert.ok(next);await next();
 await wait(()=>$('kpiTotalOrders').textContent==='已读取 2');
 assert.match($('dataStatus').textContent,/订单明细 2\/100/);
 assert.match($('sourceSummary').textContent,/本地覆盖 7\/7 天/);
});

test('cached order cards remain visible while platform-only analysis waits in queue',async t=>{
 const {$,wait}=await app(t,(data,url)=>{
  if(url.pathname.includes('/refresh/')) return {...data,status:'queued'};
  if(url.pathname.endsWith('/dashboard')) {
   data.shops[0].current.orders={total:333,delivered:300,cancelled:3,in_transit:30,statuses:[],regions:[],cancellation_reasons:[],hours:Array(24).fill(0),skus:[]};
   data.shops[0].current.coverage.orders={status:'complete',storage:'local_database',observed_days:7,total_days:7,evidence:[]};
  }
  return data;
 });
 await wait(()=>$('dataStatus').textContent.includes('同步排队中'));
 assert.equal($('kpiTotalOrders').textContent,'333');
});


test('metric values show server comparisons and preserve an open definition after refresh',async t=>{
 let version=1;
 const {$,timers,wait}=await app(t,(data,url)=>{
  if(url.pathname.includes('/refresh/'))return {...data,data_version:version};
  if(url.pathname.endsWith('/dashboard')){
   data.year_ago_window={start_date:'2025-11-15',end_date:'2025-11-21'};
   data.shops[0].current.orders={total:120,delivered:60,cancelled:10,in_transit:50,statuses:[],regions:[],cancellation_reasons:[],hours:Array(24).fill(0),skus:[]};
   data.shops[0].current.coverage.orders.status='complete';
   const entry=(key,value,prior,yoy,unit='%')=>({key,definition:key,label:key,value:String(value),previous:{status:'ok',change:prior,unit},year_ago:{status:'ok',change:yoy,unit}});
   data.shops[0].comparisons={'orders.total':entry('orders.total',120,20,-10),'orders.delivered_rate':entry('orders.delivered_rate',50,10,-2,'百分点')};
  }
  return data;
 });
 await wait(()=>$('kpiTotalOrders').textContent==='120');
 const area=()=>$.call(null,'kpiTotalOrders').parentElement.querySelector('[data-metric-for="kpiTotalOrders"]');
 assert.match(area().textContent,/环比 \+20\.0%/);assert.match(area().textContent,/同比 -10\.0%/);
 const help=area().querySelector('details');help.open=true;
 assert.match(help.textContent,/2025-11-15/);
 version=2;await timers.values().next().value();
 assert.equal(area().querySelector('details').open,true,'background refresh must not close a definition being read');
 const rate=$('kpiDeliveredRate').parentElement.querySelector('[data-metric-for="kpiDeliveredRate"]');
 assert.match(rate.textContent,/\+10\.0百分点/);
});
