import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from mcp.shared.auth import OAuthToken
from mcp.types import Tool, ListToolsResult, CallToolResult
from app import ads_mcp as ads, ads_analysis, store, model
from app.main import app


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.delenv('APP_TOKEN',raising=False)
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'ads.sqlite'))
    monkeypatch.setenv('ADS_TOKEN_ENCRYPTION_KEY',Fernet.generate_key().decode())
    monkeypatch.setenv('ADS_OAUTH_REDIRECT_URI','http://127.0.0.1:8000/api/ads/oauth/callback')
    monkeypatch.setenv('MODEL_MODE','bailian')
    monkeypatch.delenv('ADS_READ_TOOL_ALLOWLIST',raising=False)
    return TestClient(app)


def test_oauth_storage_encryption_disconnect(client):
    attempt,fresh=ads.begin_connection('default')
    assert fresh
    storage=ads.Storage('default',attempt['generation'])
    asyncio.run(storage.set_tokens(OAuthToken(access_token='fake-access',refresh_token='fake-refresh',expires_in=3600)))
    item=ads.connection('default')
    assert 'fake-access' not in json.dumps(item)
    assert asyncio.run(storage.get_tokens()).access_token=='fake-access'
    assert 'sealed' not in client.get('/api/ads/connection').json()
    client.delete('/api/ads/connection')
    with pytest.raises(ads.AdsError):
        asyncio.run(storage.set_tokens(OAuthToken(access_token='stale')))
    assert ads.connection('default')['sealed'] is None


def test_callback_state_one_time_and_scope(client):
    attempt,_=ads.begin_connection('default')
    state='random-state-'+('x'*24)
    hashed=hashlib.sha256(state.encode()).hexdigest()
    store.save({'id':hashed,'shop':'default','attempt_id':attempt['id'],'expires_at':time.time()+60},'ads_state','_oauth')
    bad=client.get('/api/ads/oauth/callback',params={'code':'fake-code','state':'wrong-state-1234567'})
    assert bad.status_code==400
    ok=client.get('/api/ads/oauth/callback',params={'code':'fake-code','state':state})
    assert ok.status_code==200
    assert 'fake-code' not in ok.text
    stored=store.get(attempt['id'],'ads_attempt','default')
    assert 'fake-code' not in json.dumps(stored)
    assert ads.unseal(stored['callback'])['code']=='fake-code'
    assert client.get('/api/ads/oauth/callback',params={'code':'fake-code','state':state}).status_code==400
    assert client.get('/api/ads/attempts/'+attempt['id'],headers={'X-Shop-ID':'other'}).status_code==404


def test_read_policy_and_account_binding(client,monkeypatch):
    assert ads.permitted({'name':'report_integrated_get','annotations':{'readOnlyHint':True}})
    assert not ads.permitted({'name':'campaign_update','annotations':{'readOnlyHint':True}})
    assert not ads.permitted({'name':'execute_tool','annotations':{'readOnlyHint':True}})
    assert not ads.permitted({'name':'report_get'})
    monkeypatch.setenv('ADS_READ_TOOL_ALLOWLIST','report_get,campaign_update')
    assert ads.permitted({'name':'report_get'})
    assert not ads.permitted({'name':'campaign_update'})
    assert ads.bind_account({'advertiser_id':'999'},{'properties':{'advertiser_id':{}}},'123')['advertiser_id']=='123'
    with pytest.raises(ads.AdsError):
        ads.bind_account({'filter':{'advertiser_id':'999'}},{},'123')


class FakeMCP:
    def __init__(self):self.calls=[]
    async def list_tools(self,cursor=None):
        return ListToolsResult(tools=[Tool(name='report_get',description='report',inputSchema={'type':'object','properties':{'advertiser_id':{'type':'string'},'start_date':{'type':'string'},'end_date':{'type':'string'}},'required':['advertiser_id']},annotations={'readOnlyHint':True}),Tool(name='campaign_update',inputSchema={'type':'object'},annotations={'readOnlyHint':False})])
    async def call_tool(self,name,args):
        self.calls.append((name,args))
        return CallToolResult(content=[],structuredContent={'code':0,'data':{'list':[{'metrics':{'spend':'12','clicks':'6','impressions':'100','conversion':'2'}}]}})


def mock_session(monkeypatch,fake):
    @asynccontextmanager
    async def session(*args,**kwargs):yield fake
    monkeypatch.setattr(ads,'session',session)


def test_analysis_tool_loop_evidence_and_history(client,monkeypatch):
    fake=FakeMCP();mock_session(monkeypatch,fake)
    seen=[]
    def message(messages,tools):
        seen.append(messages)
        assert all(t['function']['name']!='campaign_update' for t in tools)
        if messages[-1]['role']=='tool':return {'role':'assistant','content':'依据 E1，CPC 为2。仅提供建议。'}
        return {'role':'assistant','tool_calls':[{'id':'call1','type':'function','function':{'name':'report_get','arguments':json.dumps({'advertiser_id':'999','start_date':'2026-11-01','end_date':'2026-11-07'})}}]}
    monkeypatch.setattr(model,'message',message)
    r=client.post('/api/ads/analyze',json={'advertiser_id':'123','question':'分析广告','start_date':'2026-11-01','end_date':'2026-11-07'})
    assert r.status_code==200,r.text
    report=r.json()
    assert fake.calls[0][1]['advertiser_id']=='123'
    assert report['evidence'][0]['calculations'][0]['values']['CPC']=='2.0000'
    assert report['evidence'][0]['calculations'][0]['values']['CTR_percent']=='6.0000'
    assert report['read_only']
    assert client.get('/api/ads/reports/'+report['id']).status_code==200
    assert client.get('/api/ads/reports/'+report['id'],headers={'X-Shop-ID':'other'}).status_code==404
    assert client.delete('/api/ads/reports/'+report['id']).status_code==200
    assert client.get('/api/ads/reports/'+report['id']).status_code==404


def test_model_cannot_call_write_tool(client,monkeypatch):
    fake=FakeMCP();mock_session(monkeypatch,fake)
    monkeypatch.setattr(model,'message',lambda *args:{'role':'assistant','tool_calls':[{'id':'evil','function':{'name':'campaign_update','arguments':'{}'}}]})
    r=client.post('/api/ads/analyze',json={'advertiser_id':'123','question':'修改预算','start_date':'2026-11-01','end_date':'2026-11-07'})
    assert r.status_code==502
    assert fake.calls==[]


def test_no_data_no_analysis_and_call_limit(client,monkeypatch):
    fake=FakeMCP();mock_session(monkeypatch,fake)
    monkeypatch.setattr(model,'message',lambda *args:{'role':'assistant','content':'凭空的结论'})
    payload={'advertiser_id':'123','question':'分析','start_date':'2026-11-01','end_date':'2026-11-07'}
    assert client.post('/api/ads/analyze',json=payload).status_code==502
    monkeypatch.setattr(model,'message',lambda *args:{'role':'assistant','tool_calls':[{'id':str(time.time()),'function':{'name':'report_get','arguments':'{}'}}]})
    r=client.post('/api/ads/analyze',json=payload)
    assert r.status_code==200
    assert len(fake.calls)==6
    assert '上限' in r.json()['answer']


def test_bad_dates_and_missing_key(client,monkeypatch):
    assert client.post('/api/ads/analyze',json={'advertiser_id':'123','question':'分析','start_date':'2026-11-07','end_date':'2026-11-01'}).status_code==422
    monkeypatch.delenv('ADS_TOKEN_ENCRYPTION_KEY')
    assert client.post('/api/ads/connect').status_code==400


def test_sdk_oauth_pkce_and_mcp_protocol(client,monkeypatch):
    """Exercise real SDK against a fake HTTP server, not a fake MCP session."""
    issued=[]
    auth_base='https://business-api.tiktok.com/open_mcp/tt-ads-mcp-flat'
    def handler(request):
        path=request.url.path
        if 'oauth-protected-resource' in path:
            return httpx.Response(200,json={'resource':auth_base,'authorization_servers':[auth_base],'scopes_supported':['mcp:tt4b']})
        if 'oauth-authorization-server' in path or 'openid-configuration' in path:
            return httpx.Response(200,json={'issuer':auth_base,'authorization_endpoint':auth_base+'/oauth/authorize','token_endpoint':auth_base+'/oauth/token','registration_endpoint':auth_base+'/oauth/register','response_types_supported':['code'],'grant_types_supported':['authorization_code','refresh_token'],'code_challenge_methods_supported':['S256'],'token_endpoint_auth_methods_supported':['none'],'scopes_supported':['mcp:tt4b']})
        if path.endswith('/oauth/register'):
            body=json.loads(request.content)
            return httpx.Response(201,json={**body,'client_id':'fake-client-id'})
        if path.endswith('/oauth/token'):
            form=parse_qs(request.content.decode())
            assert form['grant_type']==['authorization_code']
            assert form['code']==['fake-auth-code']
            assert len(form['code_verifier'][0])>=43
            issued.append(True)
            return httpx.Response(200,json={'access_token':'fake-sdk-token','token_type':'bearer','expires_in':3600,'refresh_token':'fake-sdk-refresh','scope':'mcp:tt4b'})
        if request.headers.get('authorization')!='Bearer fake-sdk-token':
            return httpx.Response(401,headers={'www-authenticate':f'Bearer resource_metadata="https://business-api.tiktok.com/.well-known/oauth-protected-resource/open_mcp/tt-ads-mcp-flat"'})
        if request.method=='GET':return httpx.Response(405)
        if request.method=='DELETE':return httpx.Response(200)
        body=json.loads(request.content)
        if body['method'].startswith('notifications/'):
            return httpx.Response(202)
        if body['method']=='initialize':
            result={'protocolVersion':body['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'fake-official','version':'1'}}
        elif body['method']=='tools/list':
            result={'tools':[{'name':'report_get','inputSchema':{'type':'object'},'annotations':{'readOnlyHint':True}}]}
        else:
            assert body['method']=='tools/call'
            result={'content':[{'type':'text','text':'fake report'}]}
        return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':result})

    real_client=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:real_client(transport=httpx.MockTransport(handler),**kwargs))
    attempt,_=ads.begin_connection('default')
    async def exercise():
        flow=asyncio.create_task(ads.authorize('default',attempt['id'],attempt['generation']))
        try:
            url=None
            for _ in range(100):
                a=store.get(attempt['id'],'ads_attempt','default')
                if a.get('authorization_url'):
                    url=a['authorization_url'];break
                if flow.done():break
                await asyncio.sleep(.02)
            assert url,'SDK did not initiate authorization'
            query=parse_qs(urlparse(url).query)
            assert query['code_challenge_method']==['S256']
            assert query['redirect_uri']==['http://127.0.0.1:8000/api/ads/oauth/callback']
            ads.accept_callback(query['state'][0],'fake-auth-code')
            await asyncio.wait_for(flow,5)
            assert ads.public_status('default')['status']=='connected'
            async with ads.session('default') as mcp:
                discovered=await ads.tools(mcp)
                assert discovered[0]['name']=='report_get'
                result=await mcp.call_tool('report_get',{})
                assert result.content[0].text=='fake report'
        finally:
            if not flow.done():
                flow.cancel()
                try:await flow
                except asyncio.CancelledError:pass
    asyncio.run(exercise())
    assert issued==[True]


def test_date_binding_and_log_redaction(client):
    from app.ads_routes import NoOAuthAccessLog
    import logging
    args=ads.bind_account({'start_date':'1990-01-01'},{'properties':{'start_date':{},'end_date':{}}},'123','2026-11-01','2026-11-07')
    assert args['start_date']=='2026-11-01' and args['end_date']=='2026-11-07'
    record=logging.LogRecord('uvicorn.access',20,'',0,'GET /api/ads/oauth/callback?code=secret',(),None)
    assert not NoOAuthAccessLog().filter(record)


def test_expired_callback_does_not_store_code(client):
    state='expired-state-123456789'
    hashed=hashlib.sha256(state.encode()).hexdigest()
    store.save({'id':hashed,'shop':'default','attempt_id':'expired','expires_at':time.time()-1},'ads_state','_oauth')
    assert client.get('/api/ads/oauth/callback',params={'state':state,'code':'secret'}).status_code==400


def test_token_storage_preserves_rotated_refresh(client):
    a,_=ads.begin_connection('default')
    storage=ads.Storage('default',a['generation'])
    asyncio.run(storage.set_tokens(OAuthToken(access_token='old',refresh_token='refresh-old')))
    asyncio.run(storage.set_tokens(OAuthToken(access_token='new',refresh_token='refresh-new')))
    reopened=ads.Storage('default',a['generation'])
    t=asyncio.run(reopened.get_tokens())
    assert t.access_token=='new' and t.refresh_token=='refresh-new'


def test_report_text_json_and_zero_denominator(client):
    value={'content':[{'type':'text','text':json.dumps({'data':{'list':[{'metrics':{'spend':'2','clicks':'0','impressions':'10','conversion':'0'}}]}})}]}
    calculations=ads_analysis.row_calculations(value)[0]['values']
    assert calculations['CTR_percent']=='0.0000'
    assert 'CPC' not in calculations and 'CPA_conversion' not in calculations
    assert ads_analysis.row_calculations({'content':[{'type':'text','text':'not JSON'}]})==[]


def test_denied_oauth_returns_to_workbench(client):
    a,_=ads.begin_connection('default')
    state='denied-state-123456789'
    hashed=hashlib.sha256(state.encode()).hexdigest()
    store.save({'id':hashed,'shop':'default','attempt_id':a['id'],'expires_at':time.time()+60},'ads_state','_oauth')
    response=client.get('/api/ads/oauth/callback',params={'state':state,'error':'access_denied'})
    assert response.status_code==200
    assert ads.public_status('default')['status']=='error'
    assert ads.connection('default')['sealed'] is None
