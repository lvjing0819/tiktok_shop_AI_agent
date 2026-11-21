import {useEffect,useState} from 'react';
import type {Api} from './BusinessPanel';
const status:Record<string,string>={draft:'待开始',queued:'排队中',running:'执行中',completed:'计划步骤已完成',waiting_input:'等待资料',waiting_approval:'等待审批',blocked:'能力未接通',failed:'执行失败',budget_exceeded:'达到预算上限',rejected:'已驳回',pending:'待执行'};
const roles:Record<string,string>={research:'选品研究',product:'商品运营',content:'内容策划',ads:'广告优化'};
type Step={id:string;agent:string;goal:string;tool:string;arguments:Record<string,unknown>;bindings:Record<string,unknown>;depends_on:string[];success_criteria:string};
type Plan={title:string;steps:Step[];clarification?:string};
type Run={id:string;revision:number;status:string;question:string;plan:Plan;steps?:Record<string,{status:string;error?:string;result?:{summary:string;agent_summary?:string;data?:unknown}}> ;final_delivery?:{answer:string;limitations:string[];source_steps:string[]};usage:Record<string,number>;events?:{at:number;kind:string;error?:string;step?:string}[];pending_approval?:{step_id:string;arguments:unknown}};
export default function OperationsPanel({api}:{api:Api}){
 const [runs,setRuns]=useState<Run[]>([]),[active,setActive]=useState<Run|null>(null),[question,setQuestion]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false),[editor,setEditor]=useState(''),[traces,setTraces]=useState<unknown[]>([]),[tools,setTools]=useState<{name:string;description:string;available:boolean;effect:string}[]>([]);
 useEffect(()=>{api('/operations').then(setRuns).catch(e=>setError(e.message));api('/operations/tools').then(d=>setTools(d.tools)).catch(e=>setError(e.message));},[api]);
 useEffect(()=>{let live=true;const id=setInterval(async()=>{try{const list=await api('/operations');if(live)setRuns(list);if(active){const d=await api('/operations/'+active.id);if(live)setActive(d);}}catch(e){if(live)setError(String(e));}},4000);return()=>{live=false;clearInterval(id)};},[api,active?.id]);
 async function act(fn:()=>Promise<void>){setBusy(true);setError('');try{await fn();setRuns(await api('/operations'))}catch(e){setError(String(e))}finally{setBusy(false)}}
 async function select(id:string){const r=await api('/operations/'+id);setActive(r);setEditor(JSON.stringify(r.plan,null,2));setTraces([])}
 return <section className="operations-panel">
  <article><h2>运营任务</h2><p>描述目标，Leader 会拆分任务。先查看计划，再开始执行；外部发布仍须审批，未接入的能力会明确停止。</p>
  <form onSubmit={e=>{e.preventDefault();act(async()=>{const r=await api('/operations/plan',{question});setActive(r);setEditor(JSON.stringify(r.plan,null,2));setTraces([])})}}><label>运营目标<textarea required maxLength={4000} value={question} onChange={e=>setQuestion(e.target.value)} placeholder="例如：查询 MY 女士手表累计销量前30名，并整理选品依据"/></label><button disabled={busy||!question.trim()}>生成任务计划</button></form>{error&&<p className="error" role="alert">{error}</p>}</article>
  <div className="chat-grid"><article><h3>任务记录</h3>{runs.length===0&&<p>尚无任务。</p>}{runs.map(r=><button key={r.id} className="session secondary" onClick={()=>act(()=>select(r.id))}>{r.plan.title}<small className="block">{status[r.status]||r.status}</small></button>)}</article>
  <article>{!active?<p>选择任务查看计划与进度。</p>:<><div className="row"><h3>{active.plan.title}</h3><strong>{status[active.status]||active.status}</strong></div><p>{active.question}</p><small>版本 {active.revision} · {active.id}</small>
  {active.plan.clarification&&<p className="notice">需要补充：{active.plan.clarification}</p>}
  {active.final_delivery&&<article><h3>Leader 汇总</h3><p className="answer">{active.final_delivery.answer}</p><ul>{active.final_delivery.limitations.map((v,i)=><li key={i}>{v}</li>)}</ul><small>来源步骤：{active.final_delivery.source_steps.join("、")}</small></article>}
  <ol>{active.plan.steps.map(s=>{const state=active.steps?.[s.id];return <li key={s.id}><strong>{roles[s.agent]}：{s.goal}</strong><p>完成标准：{s.success_criteria}</p><small>状态：{status[state?.status||'pending']} · 依赖：{s.depends_on.join('、')||'无'}</small>{state?.error&&<p className="error">{state.error}</p>}{state?.result&&<><p>{state.result.agent_summary||state.result.summary}</p><details><summary>查看交付数据</summary><pre>{JSON.stringify(state.result.data,null,2)}</pre></details></>}</li>})}</ol>
  {['draft','failed'].includes(active.status)&&<button disabled={busy} onClick={()=>act(async()=>{setActive(await api('/operations/'+active.id+'/start',{revision:active.revision}))})}>开始／重试任务</button>}
  {active.status==='waiting_approval'&&<div className="notice"><p>请核对本版本待执行的操作及参数。</p><pre>{JSON.stringify(active.pending_approval,null,2)}</pre><button disabled={busy} onClick={()=>act(async()=>{setActive(await api('/operations/'+active.id+'/approval',{revision:active.revision,approved:true}))})}>批准本版本操作</button><button className="secondary" disabled={busy} onClick={()=>act(async()=>{setActive(await api('/operations/'+active.id+'/approval',{revision:active.revision,approved:false}))})}>驳回</button></div>}
  <p>模型调用 {active.usage.model_calls} 次 · 业务工具 {active.usage.tool_calls} 次 · 外部请求 {active.usage.remote_calls} 次 · 已报告 Token {active.usage.reported_tokens}（不等于实际账单金额）</p>
  <details><summary>补充资料／修改计划</summary><p>适用于等待资料或需要修改的任务。修改后产生新版本，原审批失效。</p><textarea aria-label="结构化计划" className="json-editor" value={editor} onChange={e=>setEditor(e.target.value)}/><button disabled={busy||['running','queued'].includes(active.status)} onClick={()=>act(async()=>{const r=await api('/operations/'+active.id+'/plan',{revision:active.revision,plan:JSON.parse(editor)},'PUT');setActive(r);setEditor(JSON.stringify(r.plan,null,2))})}>保存新版本</button></details>
  <details><summary onClick={()=>api('/operations/'+active.id+'/traces').then(setTraces).catch(e=>setError(e.message))}>执行记录与工具证据</summary><pre>{JSON.stringify(traces,null,2)}</pre></details>
  <details><summary>任务事件</summary><ul>{active.events?.map((e,i)=><li key={i}>{new Date(e.at*1000).toLocaleString()} · {e.kind} {e.step} {e.error}</li>)}</ul></details>
  </>}</article></div>
  <details><summary>当前可用能力</summary><ul>{tools.map(t=><li key={t.name}><strong>{t.available?'已接入':'待接入'} · {t.name}</strong>：{t.description}</li>)}</ul></details>
 </section>
}
