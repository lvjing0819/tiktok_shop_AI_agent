"""Protocol/policy tests. Only opt-in Docker tests prove container isolation."""
import asyncio
import json
import os
import sys
import time
import pytest
from mcp.types import CallToolResult, ListToolsResult
from fastapi.testclient import TestClient
from app import api_gateway as g, sandbox as s, store, harness as h
from app.main import app
from test_sandbox import engine


@pytest.fixture(autouse=True)
def env(tmp_path,monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.delenv('APP_TOKEN',raising=False)
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'gateway.sqlite'))
    monkeypatch.setenv('SOCIAL_AUTH_MODE','oauth')


def ctx(**extra):
    return {'workspace':'default','run_id':'run','step_id':'read','attempt_id':'attempt',
            'assert_current':lambda conn:None,'deadline_monotonic':time.monotonic()+120,**extra}


def run(coro):return asyncio.run(coro)


async def grant_test(fn, **arguments):
    with g.execution(ctx(),'ads.analyze',{'advertiser_id':'123','start_date':'2026-11-01','end_date':'2026-11-02'}) as scope:
        return await fn(scope,**arguments)


def test_request_runs_in_a_new_offline_container_and_audits_no_body(engine):
    called=[]
    async def perform():called.append(1);return {'cost':12,'currency':'MYR'}
    async def task(scope):
        a=await scope.invoke('tools.call:report_get',{'advertiser_id':'123'},perform)
        b=await scope.invoke('tools.call:report_get',{'advertiser_id':'123'},perform)
        return a,b
    assert run(grant_test(task))==({'cost':12,'currency':'MYR'},)*2
    assert len(called)==2
    _,calls=engine
    creates=[args for args,_ in calls if args[0]=='create']
    assert len(creates)==2 and creates[0][creates[0].index('--name')+1]!=creates[1][creates[1].index('--name')+1]
    assert all(args[args.index('--network')+1]=='none' and '--env' not in args for args in creates)
    audit=store.list_records('sandbox_run')
    assert len(audit)==2 and all(r['stopped_confirmed'] and r['gateway']['status']=='completed' for r in audit)
    assert all('capability' not in json.dumps(r) and 'advertiser_id' not in json.dumps(r) for r in audit)


@pytest.mark.parametrize('change',[
    lambda f:f.update(capability='wrong'),lambda f:f.update(service='shop'),
    lambda f:f.update(operation='tools.call:campaign_update'),
    lambda f:f['arguments'].update(advertiser_id='999'),
    lambda f:f.update(workspace='other'),lambda f:f.update(protocol=True),
])
def test_tampered_request_is_denied_before_upstream(engine,change):
    engine[0]['frame_mutate']=change
    called=[]
    async def perform():called.append(1);return {}
    async def task(scope):return await scope.invoke('tools.call:report_get',{'advertiser_id':'123'},perform)
    with pytest.raises(s.SandboxError):run(grant_test(task))
    assert not called and engine[0]['container'] is None


def test_forged_worker_result_is_not_accepted(engine):
    engine[0]['result_mutate']=lambda value:{'cost':999}
    async def perform():return {'cost':12}
    async def task(scope):return await scope.invoke('tools.call:report_get',{},perform)
    with pytest.raises(s.SandboxError,match='交付'):run(grant_test(task))


def test_one_use_expiration_and_closed_scope():
    called=[]
    async def perform():called.append(1);return {'ok':True}
    async def task(scope):
        grant=g.Grant(scope,'tools.list',{},perform)
        frame={'protocol':1,'kind':'gateway_request',**grant.input()}
        assert (await grant.dispatch(frame))['data']=={'ok':True}
        with pytest.raises(s.SandboxError):await grant.dispatch(frame)
        expired=g.Grant(scope,'tools.list',{},perform);expired.expires=0
        with pytest.raises(s.SandboxError):await expired.dispatch({'protocol':1,'kind':'gateway_request',**expired.input()})
        scope.open=False
        with pytest.raises(s.SandboxError):await scope.current()
    run(grant_test(task));assert called==[1]


@pytest.mark.parametrize('value',[{'access_token':'secret'}, {'nested':[{'authorization':'Bearer x'}]}, {'x':'a'*2_000_001}])
def test_credentials_and_large_results_are_not_sent_to_worker(engine,value):
    async def perform():return value
    async def task(scope):return await scope.invoke('tools.list',{},perform)
    with pytest.raises(s.SandboxError):run(grant_test(task))
    assert engine[0]['container'] is None


def test_known_secret_inside_text_is_rejected(engine,monkeypatch):
    monkeypatch.setenv('TEST_API_KEY','private-long-credential')
    async def perform():return {'content':'echo private-long-credential'}
    async def task(scope):return await scope.invoke('tools.list',{},perform)
    with pytest.raises(s.SandboxError,match='凭据'):run(grant_test(task))
    assert 'private-long-credential' not in json.dumps(store.list_records('sandbox_run'))


def test_request_cannot_send_credentials_to_container(engine):
    async def perform():raise AssertionError('Must not call upstream')
    async def task(scope):return await scope.invoke('tools.list',{'access_token':'secret'},perform)
    with pytest.raises(s.SandboxError):run(grant_test(task))
    assert not any(args[0]=='create' for args,_ in engine[1])


def test_budget_and_late_attempt_are_denied_before_upstream(engine):
    called=[];valid=[True]
    def fence(conn):
        if not valid[0]:raise s.ExecutionError('stale')
    async def perform():called.append(1);return {}
    async def task():
        with g.execution(ctx(assert_current=fence),'social.analyze',{}) as scope:
            scope.calls=g.MAX_CALLS
            with pytest.raises(s.SandboxError):await scope.invoke('tools.list',{},perform)
            scope.calls=0;valid[0]=False
            with pytest.raises(s.ExecutionError):await scope.invoke('tools.list',{},perform)
    run(task());assert not called


def test_cancellation_during_gateway_drops_delivery_and_cleans_container(engine):
    valid=[True]
    def fence(conn):
        if not valid[0]:raise s.ExecutionError('stale')
    async def perform():valid[0]=False;return {'cost':12}
    async def task():
        with g.execution(ctx(assert_current=fence),'social.analyze',{}) as scope:
            return await scope.invoke('tools.list',{},perform)
    with pytest.raises(s.ExecutionError):run(task())
    assert engine[0]['container'] is None


def test_upstream_domain_error_preserved_only_after_cleanup(engine):
    from app.shop_api import ShopAPIError
    async def perform():raise ShopAPIError(105002)
    async def task(scope):return await scope.invoke('tools.list',{},perform)
    with pytest.raises(ShopAPIError) as error:run(grant_test(task))
    assert error.value.code==105002 and engine[0]['container'] is None


class MCP:
    def __init__(self):
        from app import ads_mcp,social_mcp
        self.calls=[]
        for service,profile in [('ads',ads_mcp),('social',social_mcp)]:
            store.save({'id':service+':default','generation':'original','status':'connected','server':profile.server_url()},service+'_connection','default')
    async def list_tools(self,cursor=None):
        return ListToolsResult.model_validate({'tools':[
            {'name':'report_get','inputSchema':{'type':'object','properties':{'advertiser_id':{'type':'string'},'start_date':{'type':'string'},'end_date':{'type':'string'}},'required':['advertiser_id','start_date','end_date'],'additionalProperties':False},'annotations':{'readOnlyHint':True}},
            {'name':'campaign_update','inputSchema':{'type':'object'},'annotations':{'readOnlyHint':True}},
            {'name':'account_user_get','inputSchema':{'type':'object'},'annotations':{'readOnlyHint':True}}]})
    async def call_tool(self,name,arguments):
        self.calls.append((name,arguments))
        return CallToolResult.model_validate({'content':[],'structuredContent':{'spend':12},'isError':False})


def test_mcp_gateway_rechecks_read_policy_account_and_dates(engine):
    raw=MCP()
    async def task(scope):
        mcp=g.wrap_mcp(raw,'default','ads')
        found=await mcp.list_tools()
        assert 'campaign_update' not in [t.name for t in found.tools]
        good={'advertiser_id':'123','start_date':'2026-11-01','end_date':'2026-11-02'}
        for name,args in [('campaign_update',{}),('report_get',{**good,'advertiser_id':'999'}),('report_get',{**good,'end_date':'2026-11-05'}),('report_get',{**good,'access_token':'bad'})]:
            with pytest.raises(s.SandboxError):await mcp.call_tool(name,args)
        result=await mcp.call_tool('report_get',good)
        assert result.structuredContent=={'spend':12}
        with pytest.raises(s.SandboxError):await g.MCPSession(raw,'other','ads').list_tools()
    run(grant_test(task));assert len(raw.calls)==1


def test_social_gateway_does_not_admit_ads_tools(engine):
    raw=MCP()
    async def task():
        with g.execution(ctx(),'social.analyze',{}) as scope:
            mcp=g.wrap_mcp(raw,'default','social')
            result=await mcp.list_tools()
            assert [t.name for t in result.tools]==['account_user_get']
            with pytest.raises(s.SandboxError):await mcp.call_tool('report_get',{})
            await mcp.call_tool('account_user_get',{})
    run(task());assert raw.calls==[('account_user_get',{})]


def test_mcp_reauthorization_during_query_rejects_old_result(engine):
    raw=MCP()
    original=raw.call_tool
    async def changed(name,arguments):
        result=await original(name,arguments)
        item=store.get('social:default','social_connection');item['generation']='new'
        store.save(item,'social_connection')
        return result
    raw.call_tool=changed
    async def task():
        with g.execution(ctx(),'social.analyze',{}):
            mcp=g.wrap_mcp(raw,'default','social');await mcp.list_tools()
            await mcp.call_tool('account_user_get',{})
    with pytest.raises(s.SandboxError,match='授权'):run(task())
    assert engine[0]['container'] is None


def test_docker_absence_blocks_request_no_host_fallback(monkeypatch):
    called=[]
    def unavailable():raise s.SandboxError('RUNTIME_UNAVAILABLE','Docker unavailable')
    monkeypatch.setattr(s,'docker',unavailable)
    async def perform():called.append(1);return {}
    async def task(scope):return await scope.invoke('tools.list',{},perform)
    with pytest.raises(s.SandboxError) as error:run(grant_test(task))
    assert error.value.observation['code']=='RUNTIME_UNAVAILABLE' and not called


def test_worker_stdio_handshake_with_real_pipes_not_container_proof(monkeypatch):
    monkeypatch.setattr(s,'docker',lambda:sys.executable)
    script="import json,runpy,sys; w=runpy.run_path('sandbox/worker.py'); r=json.loads(sys.stdin.readline()); v=w['api_request'](r['data']); print(json.dumps(v))"
    async def perform():return {'rows':[{'cost':12}],'label':'中文'}
    async def task(scope):
        grant=g.Grant(scope,'tools.list',{},perform)
        rc,out,err=await s.command(['-c',script],input=g.encoded({'data':grant.input()})+b'\n',gateway=grant,limit=2_100_000)
        assert rc==0 and not err and json.loads(out)=={'rows':[{'cost':12}],'label':'中文'}
    run(grant_test(task))


def test_no_plaintext_credentials_in_container_input(engine,monkeypatch):
    monkeypatch.setenv('PLATFORM_ACCESS_TOKEN','secret-platform-token')
    async def perform():return {'rows':[]}
    async def task(scope):return await scope.invoke('tools.list',{},perform)
    run(grant_test(task))
    request=engine[0]['job']
    assert 'secret-platform-token' not in json.dumps(request)
    assert set(request['data'])=={'capability','service','operation','arguments'}


def test_harness_entry_uses_gateway_policy_and_returns_blocker(monkeypatch):
    from app.tool_registry import Tool,Registry
    async def handler(context,args):
        assert g.active.get().ctx['run_id']==context['run_id']
        async def perform():raise AssertionError('Must not reach upstream')
        await g.active.get().invoke('shop.shops',{},perform)
    tool=Tool('shop.discover','test',('product',),{'type':'object','additionalProperties':False},handler,
              execution='service_with_api_sandbox',sandbox_job='api_gateway_v1',version='2')
    monkeypatch.setattr(h,'registry',lambda:Registry([tool]))
    monkeypatch.setattr(s,'docker',lambda:(_ for _ in ()).throw(s.SandboxError('RUNTIME_UNAVAILABLE','Docker unavailable')))
    task=h.create('default','query',h.Plan(title='query',steps=[h.Step(id='a',agent='product',tool='shop.discover',arguments={},goal='query',success_criteria='data')]))
    h.start('default',task['id'],1);h.process_one()
    result=h.get(task['id'],'default')
    assert result['status']=='blocked' and g.active.get() is None
    assert store.list_records('sandbox_run')[0]['stopped_confirmed']


@pytest.mark.skipif(os.getenv('RUN_DOCKER_SANDBOX_TESTS')!='1',reason='Requires actual Docker; protocol mocks are not isolation proof')
def test_real_docker_gateway_delivery_and_denied_direct_network():
    async def perform():return {'verified_fixture':'not live TikTok','orders':3}
    async def task(scope):return await scope.invoke('tools.list',{},perform)
    assert run(grant_test(task))['orders']==3
    record=store.list_records('sandbox_run')[0]
    # Every actual worker execution first verifies network-none, UID, seccomp,
    # read-only root, cgroup limits; a failure prevents the capability request.
    assert record['stopped_confirmed'] and record['gateway']['status']=='completed'
