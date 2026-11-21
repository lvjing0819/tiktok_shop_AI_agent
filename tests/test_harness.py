import asyncio
import json
import time
import pytest
from fastapi.testclient import TestClient
from app import harness, leader, model, store, execution_context
from app.main import app
from app.tool_registry import Tool, Registry, ExecutionError


@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.delenv('APP_TOKEN',raising=False)
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'harness.sqlite'))
    monkeypatch.setenv('MODEL_MODE','bailian')
    calls=[]
    async def echo(ctx,args):
        calls.append((ctx,args))
        execution_context.remote_call('fake:echo')
        return {'status':'completed','data':{'value':args['value']},'data_verified':True,'summary':'真实测试数据','evidence':[{'request_id':'fake-only'}]}
    async def missing(ctx,args):return {'status':'waiting_input','data':{},'data_verified':False,'summary':'请补充资料','evidence':[]}
    schema={'type':'object','properties':{'value':{'type':'string'}},'required':['value'],'additionalProperties':False}
    reg=Registry([Tool('read.echo','测试只读工具',('research','product'),schema,echo),Tool('read.missing','缺资料',('product',),schema,missing),Tool('write.external','测试外部写',('product',),schema,echo,effect='external_write'),Tool('media.disabled','能力未接入',('content',),schema,echo,available=False)])
    monkeypatch.setattr(harness,'registry',lambda:reg)
    def message(messages,tools):
        execution_context.model_start(2000);execution_context.model_end({'total_tokens':25})
        if tools[0]['function']['name']=='submit_summary':return choice('submit_summary',{'answer':'完成测试交接','source_steps':['s1','s2'],'limitations':[]})
        return choice('review_result',{'decision':'accept','summary':'完成标准已满足'})
    monkeypatch.setattr(model,'message',message)
    return TestClient(app),calls,reg


def choice(name,args):
    return {'tool_calls':[{'id':'fake-choice','type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}


def spec(tool='read.echo',agent='research',**changes):
    return harness.Step(id='s1',agent=agent,goal='读取测试数据',tool=tool,arguments={'value':'one'},success_criteria='返回指定数据',**changes)


def create(steps=None,budget=None):
    return harness.create('default','任务',harness.Plan(title='测试计划',steps=steps or [spec()]),budget)


def execute(run):
    harness.start('default',run['id'],run['revision'])
    assert harness.process_one()
    return harness.get(run['id'],'default')


def test_plan_execute_handoff_telemetry_and_scoped_trace(setup):
    client,calls,reg=setup
    second=harness.Step(id='s2',agent='product',goal='使用交接数据',tool='read.echo',arguments={},bindings={'value':harness.Binding(step='s1',path=['value'])},depends_on=['s1'],success_criteria='使用上游结果')
    run=execute(create([spec(),second]))
    assert run['status']=='completed' and len(calls)==2
    assert run['finalization_status']=='completed' and run['final_delivery']['source_steps']==['s1','s2']
    assert calls[1][1]['value']=='one'
    assert run['usage']=={'model_calls':3,'tool_calls':2,'remote_calls':2,'reported_tokens':75,'unreported_usage_calls':0}
    traces=client.get('/api/operations/'+run['id']+'/traces').json()
    assert len(traces)==2 and all(t['status']=='completed' and t['duration_ms']>=0 for t in traces)
    assert client.get('/api/operations/'+run['id'],headers={'X-Shop-ID':'other'}).status_code==404
    assert client.get('/api/operations/metrics').json()['completed']==1


def test_invalid_dag_tool_ownership_and_parameters(setup):
    with pytest.raises(ValueError):
        harness.Plan(title='bad',steps=[spec(depends_on=['s2'])])
    with pytest.raises(ExecutionError,match='权限'):
        create([spec(agent='ads')])
    with pytest.raises(ExecutionError,match='格式'):
        create([harness.Step(id='s1',agent='research',goal='bad',tool='read.echo',arguments={'unknown':'x'},success_criteria='bad')])
    with pytest.raises(ValueError):
        harness.Plan(title='bad',steps=[])


def test_waiting_input_and_disabled_are_not_completed(setup):
    client,calls,_=setup
    r=execute(create([spec(tool='read.missing',agent='product')]))
    assert r['status']=='waiting_input' and not calls
    assert client.post('/api/operations/'+r['id']+'/start',json={'revision':1}).status_code==409
    r=execute(create([spec(tool='media.disabled',agent='content')]))
    assert r['status']=='blocked' and not calls


def test_exact_revision_and_frozen_arguments_approval(setup):
    client,calls,_=setup
    run=execute(create([spec(tool='write.external',agent='product')]))
    assert run['status']=='waiting_approval' and not calls
    assert client.post('/api/operations/'+run['id']+'/approval',json={'revision':2,'approved':True}).status_code==409
    assert client.post('/api/operations/'+run['id']+'/approval',json={'revision':1,'approved':True}).status_code==202
    assert harness.process_one()
    done=harness.get(run['id'],'default')
    assert done['status']=='completed' and len(calls)==1
    assert client.put('/api/operations/'+run['id']+'/plan',json={'revision':1,'plan':{'title':'overwrite','steps':[spec().model_dump()]}}).status_code==409
    assert not harness.process_one()


def test_revised_plan_invalidates_prior_approval(setup):
    _,calls,_=setup
    r=execute(create([spec(tool='write.external',agent='product')]))
    revised=harness.revise('default',r['id'],1,harness.Plan(title='新版本',steps=[spec(tool='write.external',agent='product')]))
    assert revised['revision']==2 and revised['approval'] is None
    with pytest.raises(ExecutionError):harness.approve('default',r['id'],1,True)
    r=execute(revised)
    assert r['status']=='waiting_approval' and not calls


def test_call_budget_and_model_usage_budget(setup):
    _,calls,_=setup
    second=harness.Step(id='s2',agent='research',goal='第二次查询',tool='read.echo',arguments={'value':'two'},depends_on=['s1'],success_criteria='完成')
    r=execute(create([spec(),second],harness.Budget(max_tool_calls=1)))
    assert r['status']=='budget_exceeded' and len(calls)==1
    assert r['usage']['tool_calls']==1
    r=create(budget=harness.Budget(max_model_calls=1))
    r['usage']['model_calls']=1;store.save(r,'operation_run')
    r=execute(r)
    assert r['status']=='budget_exceeded' and r['usage']['model_calls']==1


def test_lease_recovery_skips_completed_steps(setup):
    _,calls,_=setup
    second=harness.Step(id='s2',agent='product',goal='读取第二项',tool='read.echo',arguments={'value':'two'},depends_on=['s1'],success_criteria='完成')
    r=create([spec(),second]);harness.start('default',r['id'],1)
    key,workspace,owner=harness.claim();harness.execute_next(key,workspace,owner)
    assert len(calls)==1
    old=harness.get(key,workspace);old['lease_until']=time.time()-1;store.save(old,'operation_run')
    assert harness.process_one()
    done=harness.get(key,workspace)
    assert done['status']=='completed' and len(calls)==2
    with pytest.raises(ExecutionError):harness.Scope(key,workspace,owner).remote_call('stale')


def test_external_unknown_outcome_is_not_repeated(setup):
    _,calls,_=setup
    r=create([spec(tool='write.external',agent='product')]);r['status']='running';r['lease_until']=time.time()-1;r['steps']['s1'].update(status='running',external_started=True)
    store.save(r,'operation_run')
    assert harness.process_one()
    r=harness.get(r['id'],'default')
    assert r['status']=='failed' and not calls
    with pytest.raises(ExecutionError,match='结果未知'):harness.start('default',r['id'],1)


def test_leader_and_plan_api_are_real_model_only(setup,monkeypatch):
    client,_,_=setup
    def message(messages,tools):
        execution_context.model_start(2000);execution_context.model_end({'total_tokens':40})
        return choice('submit_plan',{'title':'单项任务','steps':[spec().model_dump()]})
    monkeypatch.setattr(model,'message',message)
    r=client.post('/api/operations/plan',json={'question':'读取一项数据'})
    assert r.status_code==201,r.text
    d=r.json();assert d['status']=='draft' and d['usage']['reported_tokens']==40
    assert client.post('/api/operations/'+d['id']+'/start',json={'revision':1}).status_code==202
    monkeypatch.setenv('MODEL_MODE','demo')
    assert client.post('/api/operations/plan',json={'question':'任何任务'}).status_code==502


def test_sensitive_fields_and_values_are_not_saved(setup,monkeypatch):
    monkeypatch.setenv('FAKE_API_KEY','fake-private-key-123')
    with pytest.raises(ExecutionError,match='敏感'):
        create([harness.Step(id='s1',agent='research',goal='read',tool='read.echo',arguments={'value':'fake-private-key-123'},success_criteria='done')])
    value=harness.redact({'access_token':'fake','data':'fake-private-key-123'})
    assert value=={'access_token':'[redacted]','data':'[redacted]'}


def test_api_owner_token_and_invalid_binding(setup,monkeypatch):
    client,_,_=setup
    second=harness.Step(id='s2',agent='product',goal='invalid binding',tool='read.echo',arguments={},bindings={'value':harness.Binding(step='s1',path=['not_present'])},depends_on=['s1'],success_criteria='done')
    r=execute(create([spec(),second]));assert r['status']=='failed'
    monkeypatch.setenv('APP_TOKEN','fake-owner-token')
    assert client.get('/api/operations/tools').status_code==401
    assert client.get('/api/operations/tools',headers={'Authorization':'Bearer fake-owner-token'}).status_code==200


def test_retry_cannot_add_new_scope_fields(setup,monkeypatch):
    _,calls,reg=setup
    original=reg.get('read.echo','research')
    retry_tool=Tool('selection.search','测试检索',('research',),{'type':'object','properties':{'market':{'type':'string'},'keywords':{'type':'array'},'end_date':{'type':'string'}},'required':['market'],'additionalProperties':False},original.handler)
    async def search(ctx,args):return {'status':'completed','data':{},'summary':'测试检索不足','data_verified':True}
    retry_tool=Tool('selection.search','测试检索',('research',),retry_tool.schema,search)
    monkeypatch.setattr(harness,'registry',lambda:Registry([retry_tool]))
    def message(*args):return choice('review_result',{'decision':'retry','summary':'尝试新日期','retry_arguments':{'market':'MY','keywords':['watch'],'end_date':'2026-11-19'}})
    monkeypatch.setattr(model,'message',message)
    s=harness.Step(id='s1',agent='research',goal='检索',tool='selection.search',arguments={'market':'MY'},success_criteria='返回结果')
    r=execute(create([s]))
    assert r['status']=='failed' and r['usage']['tool_calls']==1


def test_leader_repairs_invalid_dependency_once_before_creating_run(setup,monkeypatch):
    client,_,_=setup;attempts=[]
    def message(messages,tools):
        attempts.append(messages)
        execution_context.model_start(2000);execution_context.model_end({'total_tokens':30})
        if len(attempts)==1:
            bad=spec().model_dump();bad['depends_on']=['not-existing']
            return choice('submit_plan',{'title':'invalid','steps':[bad]})
        assert 'validation_error' in messages[-1]['content']
        return choice('submit_plan',{'title':'修正计划','steps':[spec().model_dump()]})
    monkeypatch.setattr(model,'message',message)
    r=client.post('/api/operations/plan',json={'question':'执行有效任务'})
    assert r.status_code==201,r.text
    assert r.json()['usage']['model_calls']==2 and len(store.list_records('operation_run'))==1
    assert r.json()['usage']['reported_tokens']==60 and r.json()['status']=='draft'
