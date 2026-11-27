import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from app import harness as h, store, model, execution_context, agent_artifacts as artifacts, agent_state, agent_maintenance as maintenance, memory
from app.main import app
from app.tool_registry import Tool,Registry,ExecutionError


@pytest.fixture(autouse=True)
def env(tmp_path,monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False);monkeypatch.delenv('APP_TOKEN',raising=False)
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'runtime.sqlite'))
    monkeypatch.setenv('MODEL_MODE','bailian')
    monkeypatch.setenv('AGENT_CLEANUP_ENABLED','0')
    from app import sandbox
    import runpy
    async def fake_job(ctx,job,data):
        assert job=='pricing_v1'
        return runpy.run_path('sandbox/worker.py')['pricing'](data)
    monkeypatch.setattr(sandbox,'run_job',fake_job)
    calls=[]
    async def echo(ctx,args):
        calls.append((ctx,args));execution_context.remote_call('test:read')
        return {'status':'completed','data':{'value':args['value']},'summary':'测试结果','evidence':[],'data_verified':True}
    async def missing(ctx,args):return {'status':'waiting_input','data':{},'summary':'缺资料','evidence':[]}
    schema={'type':'object','properties':{'value':{'type':'string'}},'required':['value'],'additionalProperties':False}
    monkeypatch.setattr(h,'registry',lambda:Registry([Tool('test.read','read',('product',),schema,echo),Tool('test.missing','missing',('product',),schema,missing),Tool('test.write','write',('product',),schema,echo,effect='external_write')]))
    def reply(messages,tools):
        execution_context.model_start(100);execution_context.model_end({'total_tokens':20})
        name=tools[0]['function']['name']
        data={'decision':'accept','summary':'已检查'} if name=='review_result' else {'answer':'已完成','source_steps':['a','b'],'limitations':[]}
        return {'tool_calls':[{'function':{'name':name,'arguments':json.dumps(data)}}]}
    monkeypatch.setattr(model,'message',reply)
    return TestClient(app),calls


def step(id='a',tool='test.read',value='one',depends=()):
    return h.Step(id=id,agent='product',goal='测试',tool=tool,arguments={'value':value},depends_on=list(depends),success_criteria='完成')


def create(steps=None):return h.create('default','测试',h.Plan(title='任务',steps=steps or [step()]))


def execute(run):
    h.start('default',run['id'],run['revision']);h.process_one()
    return h.get(run['id'],'default')


def test_one_body_shared_by_task_trace_and_receipt(env):
    client,calls=env;r=execute(create())
    result=r['steps']['a']['result'];trace=store.list_records('tool_trace')[0];receipt=store.list_records('agent_call')[0]
    assert 'data' not in result and 'evidence' not in trace['result']
    assert result['artifact_ref']==trace['result']['artifact_ref']==receipt['result']['artifact_ref']
    assert len(store.list_records('agent_artifact'))==1
    assert client.get(f"/api/operations/{r['id']}/steps/a/result").json()['data']=={'value':'one'}
    assert client.get('/api/agent-artifacts/'+result['artifact_ref']['id'],headers={'X-Shop-ID':'other'}).status_code==404


def test_artifact_checksum_workspace_and_expiry_generation(env):
    with store.db(write=True) as conn:
        a=artifacts.put(conn,'default',{'data':{'v':1}});b=artifacts.put(conn,'other',{'data':{'v':1}})
        assert a['id']!=b['id']
        old=store.get(a['id'],'agent_artifact',conn=conn);old.pop('payload');old['expired']=True;store.put(conn,'agent_artifact',old)
        new=artifacts.put(conn,'default',{'data':{'v':1}})
        assert artifacts.put(conn,'default',{'data':{'v':1}})==new and a!=new
    with pytest.raises(ExecutionError):artifacts.read(a,'default')
    with pytest.raises(ExecutionError):artifacts.read({**new,'checksum':'bad'},'default')


def test_api_requires_current_state_version_and_heartbeat_does_not_bump(env):
    client,_=env;r=create();path=f"/api/operations/{r['id']}"
    assert client.post(path+'/start',json={'revision':1}).status_code==422
    assert client.post(path+'/start',json={'revision':1,'state_version':r['state_version']}).status_code==202
    assert client.put(path+'/plan',json={'revision':1,'state_version':r['state_version'],'plan':r['plan']}).status_code==409
    key,workspace,owner=h.claim();v=h.get(key,workspace)['state_version']
    h.renew(key,workspace,owner)
    assert h.get(key,workspace)['state_version']==v
    assert client.get(path,headers={'X-Shop-ID':'other'}).status_code==404


def test_old_result_and_exception_cannot_overwrite_new_attempt(env,monkeypatch):
    _,calls=env
    async def late(ctx,args):
        with store.db(write=True) as conn:
            r=h.get(ctx['run_id'],'default',conn);r['steps']['a']['attempt_id']='new-attempt';agent_state.save(conn,r,'default')
        return {'status':'completed','data':{'value':'stale'},'summary':'stale'}
    schema={'type':'object','properties':{'value':{'type':'string'}},'required':['value']}
    monkeypatch.setattr(h,'registry',lambda:Registry([Tool('test.read','read',('product',),schema,late)]))
    r=create();h.start('default',r['id'],1);key,w,owner=h.claim();h.execute_next(key,w,owner)
    saved=h.get(key,w)
    assert saved['steps']['a']['attempt_id']=='new-attempt' and saved['steps']['a']['status']=='running'
    assert not saved['steps']['a'].get('result')


def test_cancel_during_tool_keeps_cancelled_state(env,monkeypatch):
    async def late(ctx,args):
        r=h.get(ctx['run_id'],'default');h.cancel('default',r['id'],1,r['state_version'])
        return {'status':'completed','data':{'value':'too late'},'summary':'late'}
    monkeypatch.setattr(h,'registry',lambda:Registry([Tool('test.read','read',('product',),{'type':'object'},late)]))
    r=execute(create())
    assert r['status']=='cancelled' and r['steps']['a']['status']=='cancelled'
    assert not store.list_records('agent_artifact')


def test_revise_keeps_unaffected_completed_steps_and_invalidates_descendants(env):
    _,calls=env
    r=execute(create([step(),step('b'),step('c','test.missing',depends=['a']),step('d',depends=['c'])]))
    assert r['status']=='waiting_input' and len(calls)==2
    old=h.get(r['id'],'default')
    plan=h.Plan(title='补充资料',steps=[step(),step('b'),step('c',value='new',depends=['a']),step('d',depends=['c'])])
    revised=h.revise('default',r['id'],1,plan,r['state_version'])
    assert revised['steps']['a']['status']=='completed' and revised['steps']['b']['status']=='completed'
    assert revised['steps']['a']['execution_id']==old['steps']['a']['execution_id']
    assert revised['steps']['c']['execution_id']!=old['steps']['c']['execution_id']
    assert execute(revised)['status']=='completed' and len(calls)==4


def test_resume_rechecks_waiting_steps_without_repeating_completed_ones(env,monkeypatch):
    r=execute(create([step(),step('b','test.missing',depends=['a'])]));assert r['status']=='waiting_input'
    resumed=h.resume('default',r['id'],1,r['state_version'])
    assert resumed['steps']['a']['status']=='completed' and resumed['steps']['b']['status']=='pending'
    with pytest.raises(ExecutionError):h.resume('default',r['id'],1,r['state_version'])


def test_committed_tool_result_reused_after_model_review_failure(env,monkeypatch):
    _,calls=env;original=model.message
    monkeypatch.setattr(model,'message',lambda *a:(_ for _ in ()).throw(model.ModelError('review down')))
    r=execute(create());assert r['status']=='failed' and len(calls)==1
    monkeypatch.setattr(model,'message',original)
    assert execute(r)['status']=='completed' and len(calls)==1


def test_committed_external_outcome_is_reused_and_unknown_outcome_is_blocked(env,monkeypatch):
    _,calls=env;r=execute(create([step(tool='test.write')]))
    r=h.approve('default',r['id'],1,True,r['state_version']);original=model.message
    monkeypatch.setattr(model,'message',lambda *a:(_ for _ in ()).throw(model.ModelError('review down')))
    h.process_one();r=h.get(r['id'],'default');assert r['status']=='failed' and len(calls)==1
    monkeypatch.setattr(model,'message',original)
    assert execute(r)['status']=='completed' and len(calls)==1


def test_output_contract_rejects_invalid_completed_result(env,monkeypatch):
    async def bad(*a):return {'status':'completed','data':{},'summary':'bad'}
    monkeypatch.setattr(h,'registry',lambda:Registry([Tool('test.read','read',('product',),{'type':'object'},bad,output_schema={'type':'object','required':['value']})]))
    r=execute(create());assert r['status']=='failed'
    assert not store.list_records('agent_artifact')


def age_run(run,days):
    run=store.get(run['id'],'operation_run');run['finished_at']=run['updated_at']=run['created_at']=time.time()-days*86400
    store.save(run,'operation_run');return run


def apply_preview(report):return maintenance.apply('default',report['id'],report['confirmation'])


def test_cleanup_preview_grace_period_and_archive_never_touch_business_tables(env,monkeypatch):
    r=execute(create());ref=r['steps']['a']['result']['artifact_ref'];age_run(r,200)
    artifact=store.get(ref['id'],'agent_artifact');artifact['created_at']=time.time()-200*86400;store.save(artifact,'agent_artifact')
    store.save({'id':'private-ledger','rows':[{'amount':'99'}]},'analytics_source')
    store.save({'id':'keep-forever','notes':'confirmed'},'memory')
    report=maintenance.preview('default')
    assert any(a['action']=='mark' for a in report['actions'])
    assert artifacts.read(ref,'default')['data']['value']=='one'
    apply_preview(report)
    assert h.get(r['id'],'default')['archived']
    with pytest.raises(ExecutionError):h.start('default',r['id'],1)
    assert artifacts.read(ref,'default')
    artifact=store.get(ref['id'],'agent_artifact');artifact['cleanup_due_at']=time.time()-1;store.save(artifact,'agent_artifact')
    apply_preview(maintenance.preview('default'))
    with pytest.raises(ExecutionError):artifacts.read(ref,'default')
    assert store.get('private-ledger','analytics_source')['rows']==[{'amount':'99'}]
    assert store.get('keep-forever','memory')['notes']=='confirmed'
    assert apply_preview(report)['status']=='applied'


def test_active_and_approved_references_protect_old_artifacts(env):
    r=execute(create());ref=r['steps']['a']['result']['artifact_ref'];age_run(r,200)
    artifact=store.get(ref['id'],'agent_artifact');artifact['created_at']=time.time()-200*86400;store.save(artifact,'agent_artifact')
    store.save({'id':'draft','artifact_ref':ref,'approval_status':'approved','retired':True},'content_draft')
    assert not any(a['id']==ref['id'] for a in maintenance.preview('default')['actions'])
    store.remove('draft','content_draft','default')
    run=store.get(r['id'],'operation_run');run['status']='waiting_input';store.save(run,'operation_run')
    assert maintenance.preview('default')['actions']==[]


def test_new_reference_invalidates_cleanup_preview_and_no_partial_deletion(env):
    r=execute(create());ref=r['steps']['a']['result']['artifact_ref'];age_run(r,200)
    artifact=store.get(ref['id'],'agent_artifact');artifact['created_at']=time.time()-200*86400;store.save(artifact,'agent_artifact')
    report=maintenance.preview('default')
    store.save({'id':'new','artifact_ref':ref},'content_draft')
    with pytest.raises(ExecutionError):apply_preview(report)
    assert store.list_records('tool_trace') and not h.get(r['id'],'default').get('archived')


def test_external_operation_evidence_remains_protected(env):
    r=execute(create([step(tool='test.write')]))
    h.approve('default',r['id'],1,True,r['state_version']);h.process_one();r=h.get(r['id'],'default')
    age_run(r,200);ref=r['steps']['a']['result']['artifact_ref']
    artifact=store.get(ref['id'],'agent_artifact');artifact['created_at']=time.time()-200*86400;store.save(artifact,'agent_artifact')
    report=maintenance.preview('default')
    assert not any(a['id']==ref['id'] for a in report['actions'])
    assert not any(a['kind']=='tool_trace' for a in report['actions'])


def test_legacy_migration_is_idempotent_and_preserves_bindings(env):
    r=create();raw=store.get(r['id'],'operation_run');raw.pop('runtime_schema');raw.pop('state_version')
    raw['steps']['a'].update(status='completed',result={'status':'completed','data':{'value':'legacy'},'evidence':[],'summary':'old'})
    raw['status']='completed';store.save(raw,'operation_run')
    store.save({'id':'trace','run_id':r['id'],'result':raw['steps']['a']['result']},'tool_trace')
    assert maintenance.migrate('default')['runs']==1
    assert 'data' in store.get(r['id'],'operation_run')['steps']['a']['result']
    maintenance.migrate('default',apply_changes=True)
    saved=h.get(r['id'],'default');assert artifacts.load(saved['steps']['a']['result'],'default')['data']=={'value':'legacy'}
    assert len(store.list_records('agent_artifact'))==1
    assert maintenance.migrate('default',apply_changes=True)['runs']==0


def test_memory_candidate_requires_human_confirmation_current_revision_and_source(env):
    session=memory.new_session('default','test');session['messages']=[{'role':'user','content':'品牌简洁'},{'role':'assistant','content':'建议红色'}];store.save(session,'session')
    with pytest.raises(ValueError):memory.propose('default','brand_style','红色',session['id'],1)
    candidate=memory.propose('default','brand_style','简洁',session['id'],0)
    assert memory.preferences('default')['brand_style']==''
    memory.review_candidate('default',candidate['id'],1,True)
    assert memory.preferences('default')['brand_style']=='简洁' and memory.preferences('default')['source']=='user_confirmed'
    with pytest.raises(RuntimeError):memory.review_candidate('default',candidate['id'],1,True)
    candidate=memory.propose('default','notes','test',session['id'],0)
    prefs=memory.preferences('default');prefs['revision']+=1;store.save(prefs,'memory')
    with pytest.raises(RuntimeError):memory.review_candidate('default',candidate['id'],1,True)


def test_memory_char_budget_and_summary_coverage_are_bounded(env,monkeypatch):
    monkeypatch.setenv('MODEL_MODE','demo');monkeypatch.setenv('MEMORY_RECENT_MAX_CHARS','2000')
    session=memory.new_session('default','long');session['messages']=[{'role':'user' if i%2==0 else 'assistant','content':str(i)+'x'*500} for i in range(20)];store.save(session,'session')
    seen=[]
    saved=memory.send(session['id'],'default','current',responder=lambda messages,session:(seen.append(messages) or 'answer'))
    assert len(saved['messages'])==22  # Context trimming does not delete source history.
    assert sum(len(m['content']) for m in seen[0][1:-1])<=2000
    assert saved['summary_until']==saved['memory_context']['recent_start']
    assert memory.preferences('default')['notes']==''


def test_maintenance_api_auth_confirmation_and_default_schedule(env,monkeypatch):
    client,_=env
    report=client.post('/api/agent-maintenance/preview',json={}).json()
    assert client.post('/api/agent-maintenance/apply',json={'preview_id':report['id'],'confirmation':'bad'}).status_code==409
    assert client.post('/api/agent-maintenance/apply',json={'preview_id':report['id'],'confirmation':report['confirmation']},headers={'X-Shop-ID':'other'}).status_code==409
    assert maintenance.tick() is False
    monkeypatch.setenv('APP_TOKEN','owner')
    assert client.post('/api/agent-maintenance/preview',json={}).status_code==401


def test_real_content_tool_has_one_body_and_versions_are_pinned(env,monkeypatch):
    from app import business as b, operation_tools as tools
    p=b.create_goods('default',b.GoodsInput(name='手表',facts='白色表盘'))
    registry=tools.build_registry();monkeypatch.setattr(h,'registry',lambda:registry)
    def reply(messages,functions):
        name=functions[0]['function']['name']
        data={'title':'Watch','description':'White dial','image_briefs':['Show dial'],'video_script':'Show watch','claims_to_verify':[]} if name=='submit_content_draft' else {'decision':'accept','summary':'已完成'}
        return {'tool_calls':[{'function':{'name':name,'arguments':json.dumps(data)}}]}
    monkeypatch.setattr(model,'message',reply)
    s=h.Step(id='a',agent='content',goal='文案',tool='content.draft',arguments={'product_id':p['id'],'market':'MY'},success_criteria='返回文案')
    r=execute(create([s]));assert r['status']=='completed'
    result=r['steps']['a']['result'];draft=artifacts.load(result,'default')['data']
    assert len(draft['id'])>64 and len(store.list_records('agent_artifact'))==1
    assert store.get(draft['id'],'content_draft')['artifact_ref']==result['artifact_ref']
    updated=artifacts.revise_content('default',draft['id'],1,{'title':'New title'})
    assert updated['revision']==2
    assert artifacts.load(result,'default')['data']['title']=='Watch'  # Old task remains pinned.
    assert artifacts.get_draft(draft['id'],'content_draft','default')['title']=='New title'
    with pytest.raises(ExecutionError):artifacts.revise_content('default',draft['id'],1,{'title':'stale'})
    artifact=store.get(result['artifact_ref']['id'],'agent_artifact');artifact['created_at']=time.time()-200*86400;store.save(artifact,'agent_artifact')
    # Explicit retirement is needed before the current draft can be collected.
    assert not any(a['id']==store.get(draft['id'],'content_draft')['artifact_ref']['id'] for a in maintenance.preview('default')['actions'])


def test_result_contract_and_conflicting_state_update_are_atomic(env):
    r=create()
    with store.db(write=True) as conn:
        older=h.get(r['id'],'default',conn);fresh=h.get(r['id'],'default',conn)
        fresh['status']='queued';agent_state.save(conn,fresh,'default')
        older['status']='queued'
        with pytest.raises(ExecutionError):agent_state.save(conn,older,'default')
    with store.db(write=True) as conn:
        run=h.get(r['id'],'default',conn);run['status']='completed'
        with pytest.raises(ExecutionError):agent_state.save(conn,run,'default')


def test_cleanup_schedule_is_opt_in_and_records_preview(env,monkeypatch):
    r=execute(create());age_run(r,200)
    monkeypatch.setenv('AGENT_CLEANUP_ENABLED','1')
    assert maintenance.tick() is True
    assert maintenance.tick() is False
    assert store.list_records('agent_cleanup_plan')[0]['status']=='applied'


def test_candidate_rejection_and_deleted_source_cannot_change_preferences(env):
    session=memory.new_session('default','source');session['messages']=[{'role':'user','content':'remember'}];store.save(session,'session')
    candidate=memory.propose('default','notes','maybe',session['id'],0)
    memory.review_candidate('default',candidate['id'],1,False)
    assert memory.preferences('default')['notes']==''
    candidate=memory.propose('default','notes','again',session['id'],0)
    store.remove(session['id'],'session','default')
    with pytest.raises(RuntimeError):memory.review_candidate('default',candidate['id'],1,True)


def test_cleanup_after_grace_requires_fresh_preview(env):
    r=execute(create());ref=r['steps']['a']['result']['artifact_ref'];age_run(r,200)
    item=store.get(ref['id'],'agent_artifact');item['created_at']=time.time()-200*86400;store.save(item,'agent_artifact')
    plan=maintenance.preview('default');plan['expires_at']=time.time()-1;store.save(plan,'agent_cleanup_plan')
    with pytest.raises(ExecutionError):apply_preview(plan)
    assert artifacts.read(ref,'default')


def test_content_to_listing_uses_exact_artifact_version_and_source_guards(env,monkeypatch):
    from app import business as b,operation_tools as tools
    p=b.create_goods('default',b.GoodsInput(name='Watch',facts='White dial'))
    sku=b.create_sku('default',b.SKUInput(product_id=p['id'],code='W1'))
    wh=b.create_warehouse('default',b.WarehouseInput(name='MY',country='MY',kind='overseas'))
    b.set_stock('default',b.StockInput(warehouse_id=wh['id'],sku_id=sku['id'],on_hand=10,expected_revision=0,operation_id='count-001',reason='Confirmed count'))
    b.save_pricing('default',sku['id'],'MY',b.PricingInput(unit_cost='10',logistics='1',fixed_fees='1',currency='MYR',fee_rate='0.1',target_margin='0.2',listing_cap=5,expected_revision=0,source_note='Confirmed'))
    monkeypatch.setattr(h,'registry',tools.build_registry)
    def reply(messages,functions):
        name=functions[0]['function']['name']
        data={'title':'Watch','description':'White dial','image_briefs':['Dial'],'video_script':'Display','claims_to_verify':[]} if name=='submit_content_draft' else {'decision':'accept','summary':'Checked'} if name=='review_result' else {'answer':'Drafts ready','source_steps':['a','b'],'limitations':[]}
        return {'tool_calls':[{'function':{'name':name,'arguments':json.dumps(data)}}]}
    monkeypatch.setattr(model,'message',reply)
    content=h.Step(id='a',agent='content',goal='Copy',tool='content.draft',arguments={'product_id':p['id'],'market':'MY'},success_criteria='Draft')
    listing=h.Step(id='b',agent='product',goal='Listing',tool='product.prepare_listing',arguments={'product_id':p['id'],'sku_id':sku['id'],'warehouse_id':wh['id'],'market':'MY'},depends_on=['a'],bindings={'content_draft_id':h.Binding(step='a',path=['id']),'content_draft_revision':h.Binding(step='a',path=['revision'])},success_criteria='Listing draft')
    run=execute(create([content,listing]));assert run['status']=='completed'
    result=artifacts.load(run['steps']['b']['result'],'default')['data']
    assert result['content_draft_revision']==1 and result['proposed_channel_quantity']==5
    assert len(store.list_records('agent_artifact'))==2
    with pytest.raises(ExecutionError):artifacts.retire_draft('default','content_draft',result['content_draft_id'],1)
    artifacts.revise_content('default',result['content_draft_id'],1,{'title':'Changed'})
    with pytest.raises(ExecutionError):tools.validate_delivery('product.prepare_listing',result,'default')
    # A pending downstream step bound to version 1 must not silently use version 2.
    with pytest.raises(ExecutionError):
        asyncio.run(tools.product_draft({'workspace':'default','run_id':'manual','operation_id':'new'},
            {**listing.arguments,'content_draft_id':result['content_draft_id'],'content_draft_revision':1}))
