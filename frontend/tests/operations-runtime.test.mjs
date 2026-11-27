import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import fs from 'node:fs';
import ts from 'typescript';
import {JSDOM} from 'jsdom';
const require=createRequire(import.meta.url);
require.extensions['.tsx']=(module,filename)=>module._compile(ts.transpileModule(fs.readFileSync(filename,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText,filename);

async function render(t,overrides={}) {
 const dom=new JSDOM('<!doctype html><html><body><div id="app"></div></body></html>',{url:'http://localhost/?view=operations'});
 dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
 dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
 const keys=['window','document','HTMLElement','Element','Node','MouseEvent','navigator','confirm','IS_REACT_ACT_ENVIRONMENT','setInterval','clearInterval'];
 let poll;
 const old=new Map(keys.map(k=>[k,Object.getOwnPropertyDescriptor(globalThis,k)]));
 for(const [key,value] of Object.entries({window:dom.window,document:dom.window.document,HTMLElement:dom.window.HTMLElement,Element:dom.window.Element,Node:dom.window.Node,MouseEvent:dom.window.MouseEvent,navigator:dom.window.navigator,confirm:()=>true,IS_REACT_ACT_ENVIRONMENT:true,setInterval:callback=>{poll=callback;return 0;},clearInterval:()=>{}}))Object.defineProperty(globalThis,key,{value,writable:true,configurable:true});
 const React=require('react'),{createRoot}=require('react-dom/client'),{default:Panel}=require('../src/OperationsPanel.tsx');
 const logs=[];let run={id:'r1',revision:2,state_version:9,question:'Prepare content',status:'waiting_input',created_at:1,plan:{title:'Task',steps:[{id:'s1',agent:'content',goal:'Copy',tool:'content.draft',arguments:{},bindings:{},depends_on:[],success_criteria:'Draft'}]},steps:{s1:{status:'completed',result:{status:'completed',summary:'Draft ready',artifact_ref:{id:'a1',version:1,checksum:'hash'}}}},usage:{model_calls:1,tool_calls:1,remote_calls:0,reported_tokens:10},...overrides};
 let file={id:"file",name:"pricing.json",revision:5,pinned:true,state:"available"};
 const api=async(path,body)=>{
  logs.push({path,body:body?JSON.parse(JSON.stringify(body)):body});
  if(path==='/agent-files/file')return file;
  if(path==='/agent-files/file/retention')return file={...file,pinned:body.pinned,revision:file.revision+1};
  if(path==='/agent-files/file/events')return [{action:'published'}];
  if(path==='/operations')return [run];
  if(path==='/operations/tools')return {tools:[]};
  if(path==='/operations/r1')return run;
  if(path.endsWith('/result'))return {summary:'Draft ready',data:{title:'Actual saved title'},evidence:[]};
  if(path.endsWith('/resume'))return run={...run,status:'queued',state_version:10};
  if(path.endsWith('/cancel'))return run={...run,status:'cancelled',state_version:10};
  throw Error('Unexpected request '+path);
 };
 const root=createRoot(dom.window.document.getElementById('app'));
 await React.act(async()=>{root.render(React.createElement(Panel,{api,initialRunId:'r1'}));});
 t.after(async()=>{await React.act(async()=>root.unmount());dom.window.close();for(const key of keys){const descriptor=old.get(key);if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];}});
 const click=async(text)=>{const button=[...dom.window.document.querySelectorAll('button')].find(b=>b.textContent.includes(text));assert.ok(button,'missing button '+text);await React.act(async()=>button.click());};
 return {document:dom.window.document,logs,click,refresh:async changes=>{run={...run,...changes};await React.act(async()=>poll());}};
}

test('task result bodies are fetched only when requested',async t=>{
 const {document,logs,click}=await render(t);
 assert.equal(logs.filter(l=>l.path.endsWith('/result')).length,0);
 assert.ok(!document.body.textContent.includes('Actual saved title'));
 await click('读取完整交付物');
 assert.equal(logs.filter(l=>l.path.endsWith('/result')).length,1);
 assert.match(document.body.textContent,/Actual saved title/);
});

test('resume and cancellation send the observed plan and state versions',async t=>{
 const {logs,click}=await render(t);
 await click('已补充资料');
 assert.deepEqual(logs.find(l=>l.path.endsWith('/resume')).body,{revision:2,state_version:9});
 await click('停止任务');
 assert.deepEqual(logs.find(l=>l.path.endsWith('/cancel')).body,{revision:2,state_version:10});
});

test('archived tasks do not expose execution controls',async t=>{
 const {document}=await render(t,{archived:true,status:'failed'});
 const buttons=[...document.querySelectorAll('button')];
 assert.ok(!buttons.some(b=>b.textContent.includes('重试未完成')));
 assert.ok(!buttons.some(b=>b.textContent.includes('停止任务')));
 assert.ok(buttons.find(b=>b.textContent.includes('保存新版本')).disabled);
});

test('old backend responses keep unsupported runtime controls hidden',async t=>{
 const {document}=await render(t,{state_version:undefined});
 assert.ok(![...document.querySelectorAll('button')].some(b=>b.textContent.includes('已补充资料')||b.textContent.includes('停止任务')));
});


test('new delivery versions never display cached earlier result bodies',async t=>{
 const {document,click,refresh}=await render(t);
 await click('读取完整交付物');
 assert.match(document.body.textContent,/Actual saved title/);
 await refresh({revision:3,steps:{s1:{status:'completed',result:{summary:'Revised draft',artifact_ref:{id:'a2',version:1,checksum:'new'}}}}});
 assert.ok(!document.body.textContent.includes('Actual saved title'));
 assert.ok([...document.querySelectorAll('button')].some(b=>b.textContent.includes('读取完整交付物')));
});


test('file retention controls load state before changing the observed version',async t=>{
 const {document,logs,click}=await render(t,{steps:{s1:{status:'completed',result:{summary:'Exported',data:{file_refs:[{file_id:'file',version:1,checksum:'hash'}]}}}}});
 assert.ok([...document.querySelectorAll('button')].find(b=>b.textContent.includes('读取信息后设置保留')).disabled);
 await click('读取文件信息');
 await click('取消长期保留');
 assert.deepEqual(logs.find(l=>l.path.endsWith('/retention')).body,{revision:5,pinned:false});
 await click('文件审计');
 assert.match(document.body.textContent,/published/);
});
