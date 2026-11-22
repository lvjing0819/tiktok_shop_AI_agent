import asyncio
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import store, analytics as a, analytics_sources as s

SHOP={'id':'1','name':'MY Shop','region':'MY','currency':'MYR','timezone':'Asia/Kuala_Lumpur'}
WINDOW=s.Window(start_date='2026-11-14',end_date='2026-11-20')

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False);monkeypatch.delenv('APP_TOKEN',raising=False)
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'analytics.sqlite'))
    monkeypatch.setattr(s,'shops',lambda workspace:[SHOP] if workspace=='default' else [])
    return TestClient(app)

def snapshot(source,data,window=WINDOW,complete=True):
    row={'id':s.key('default','1',source,window.start_date,window.end_date),'shop_id':'1','source':source,'start_date':str(window.start_date),'end_date':str(window.end_date),'data':data,'complete':complete,'retrieved_at':time.time(),'evidence':[]}
    store.save(row,'analytics_source')

def tx(id,order,net,fee=None):
    return {'id':id,'type':'ORDER','order_id':order,'currency':'MYR','settlement_amount':net,'revenue_amount':'10','fee_tax_breakdown':{'fee':{'affiliate_commission_amount':fee,'affiliate_commission_amount_before_pit':fee}},'shipping_cost_breakdown':{'actual_shipping_fee_amount':'-2','supplementary_component':{'fbm_shipping_cost_amount':'-2'}}}

def test_missing_is_not_zero_and_workspace_isolation(client):
    r=client.get('/api/analytics/dashboard?start_date=2026-11-14&end_date=2026-11-20').json()
    assert r['shops'][0]['current']['orders'] is None
    assert r['shops'][0]['current']['coverage']['orders']['status']=='missing'
    assert client.get('/api/analytics/dashboard?start_date=2026-11-14&end_date=2026-11-20',headers={'X-Shop-ID':'other'}).json()['shops']==[]
    assert client.get('/api/analytics/dashboard?start_date=2026-10-01&end_date=2026-11-20').status_code==422
    assert client.get('/api/analytics/dashboard?start_date=2026-11-21&end_date=2026-11-14').status_code==422

def test_decimal_fees_signed_and_no_supplementary_double_count():
    rows=[tx('1','o1','-0.10','-0.10'),tx('2','o2','0','0'),tx('3','o3','1.20',None)]
    data=a.aggregate_finance({'rows':rows,'statements':[{'currency':'MYR','settlement_amount':'1.10','revenue_amount':'30','fee_amount':'-28.90','payment_status':'PAID'}]},'MYR')
    assert data['net_settlement']=='1.10'
    assert data['negative']=={'orders':1,'amount':'-0.10'}
    assert data['zero']['orders']==1 and data['positive']['orders']==1
    e=next(e for e in data['expenses'] if e['id']=='fee.affiliate_commission_amount')
    assert e['amount']=='-0.10' and e['has_orders']==1 and e['no_orders']==1 and e['unknown_orders']==1
    assert all('before_pit' not in e['id'] and 'supplementary' not in e['id'] for e in data['expenses'])
    assert a.change(10,0) is None

def test_orders_local_hour_and_real_units_no_estimated_sales():
    stamp=datetime(2026,11,20,0,15,tzinfo=ZoneInfo('Asia/Kuala_Lumpur')).timestamp()
    rows=[{'id':'1','paid_time':1,'status':'IN_TRANSIT','create_time':stamp,'region':'Selangor','payment':{'currency':'MYR','total_amount':'0.10'},'line_items':[{'sku_id':'s1','unit_count':2}]},
          {'id':'2','paid_time':1,'status':'CANCELLED','cancel_reason':'Buyer cancelled','payment':{'currency':'THB','total_amount':'1000'},'line_items':[{'sku_id':'s1'}]}]
    r=a.aggregate_orders(rows,SHOP)
    assert r['total']==2 and r['cancelled']==1 and r['in_transit']==1 and r['hours'][0]==1
    assert r['paid_order_amounts']=={'MYR':'0.10'}
    assert r['skus'][0]['units']==2
    assert r['cancellation_reasons']==[{'label':'Buyer cancelled','value':1}]

def test_clean_details_never_retains_buyer_pii():
    r=s.clean_details({'id':'1','buyer_message':'secret','buyer_email':'secret','recipient_address':{'name':'secret','phone_number':'secret','address_line1':'secret','postal_code':'secret','district_info':[{'address_level':'L0','address_name':'Malaysia'},{'address_level':'L1','address_name':'Johor'},{'address_level':'L2','address_name':'private area'}]},'line_items':[{'sku_id':'2','sub_item_info':[{'id':'a'},{'id':'b'}]}]})
    assert r['region']=='Johor' and r['line_items'][0]['unit_count']==2
    assert 'secret' not in json.dumps(r) and 'private area' not in json.dumps(r)

def test_affiliate_unknown_not_self_operated_or_zero_commission():
    sku={'creator_username':'creator','quantity':1,'settlement_status':'To-SETTLE','actual_paid_commission':{},'estimated_paid_commission':{'amount':'2.50','currency':'MYR'}}
    r=a.aggregate_affiliate({'rows':[{'id':'o1','skus':[sku]}]},'MYR',{'o1','o2'})
    assert r['affiliate_orders']==1 and r['unclassified_orders']==1
    assert r['actual_std'] is None and r['creators'][0]['estimated_std']=='2.50'
    sku.update(settlement_status='SETTLED',actual_paid_commission={'amount':'0','currency':'MYR'})
    r=a.aggregate_affiliate({'rows':[{'id':'o1','skus':[sku]}]},'MYR',{'o1'})
    assert r['actual_std']=='0'

def test_compare_previous_only_skus_and_partial_zero_not_assumed(client):
    order={'id':'o1','paid_time':1,'status':'DELIVERED','line_items':[{'sku_id':'sku1','seller_sku':'old'}]}
    snapshot('orders',{'rows':[order]},WINDOW.previous())
    snapshot('orders',{'rows':[]},complete=False)
    r=a.report('default',WINDOW)['shops'][0]
    assert r['skus'][0]['current_units'] is None
    assert r['skus'][0]['previous_units']==1
    snapshot('orders',{'rows':[]})
    assert a.report('default',WINDOW)['shops'][0]['skus'][0]['current_units']==0

def test_fee_drilldown_distinct_orders_and_csv_formula_safety(client,monkeypatch):
    snapshot('finance',{'rows':[tx('1','o1','1','-1'),tx('2','o1','2','-2'),tx('3','o2','0','0')],'statements':[]})
    d=client.get('/api/analytics/details?start_date=2026-11-14&end_date=2026-11-20&kind=expense&selected_shop=1&field=fee.affiliate_commission_amount').json()
    assert d['total']==1 and d['rows'][0]['amount']=='-3'
    snapshot('orders',{'rows':[{'id':'x','paid_time':1,'line_items':[{'sku_id':'s','seller_sku':'=HYPERLINK("bad")'}]}]})
    result=a.report('default',WINDOW);result['shops'][0]['shop']['name']='=external()'
    csv=a.export_csv(result)
    assert "'=external()" in csv and 'MYR' in csv
    assert client.get('/api/analytics/details?start_date=2026-11-14&end_date=2026-11-20&kind=orders&selected_shop=2').status_code==422

def test_refresh_idempotent_and_authentication(client,monkeypatch):
    body=WINDOW.model_dump(mode='json')
    first=client.post('/api/analytics/refresh',json=body);assert first.status_code==202
    assert client.post('/api/analytics/refresh',json=body).json()['id']==first.json()['id']
    assert client.get('/api/analytics/refresh/'+first.json()['id'],headers={'X-Shop-ID':'other'}).status_code==404
    monkeypatch.setenv('APP_TOKEN','local-access')
    assert client.get('/api/analytics/shops').status_code==401
    assert client.get('/api/analytics/shops',headers={'Authorization':'Bearer local-access'}).status_code==200

def test_pagination_completeness_and_stale_cursor(monkeypatch):
    async def request(workspace,shop,path,query,body=None):
        return {'request_id':'test','data':{'videos':[{'id':'1'}],'total_count':2,'next_page_token':''}}
    monkeypatch.setattr(s,'request',request)
    r=asyncio.run(s.fetch_source('default',SHOP,'videos',WINDOW))
    assert not r['complete'] and len(r['data']['rows'])==1


def test_ai_summary_excludes_order_ids_and_buyer_data(client,monkeypatch):
    snapshot('orders',{'rows':[{'id':'private-order-123','paid_time':1,'status':'DELIVERED','payment':{'currency':'MYR','total_amount':'20'},'line_items':[{'sku_id':'s1','seller_sku':'SKU-1'}]}]})
    snapshot('finance',{'rows':[tx('tx1','private-order-123','10','-2')],'statements':[]})
    monkeypatch.setattr('app.model.mode',lambda:'bailian')
    def complete(role,prompt):
        assert 'private-order-123' not in prompt
        assert 'order_ids' not in prompt and 'recipient_address' not in prompt
        assert 'previous_coverage' in prompt and 'MYR' in prompt
        return '真实汇总解读'
    monkeypatch.setattr('app.model.complete',complete)
    r=client.post('/api/analytics/ask',json={**WINDOW.model_dump(mode='json'),'question':'分析费用'})
    assert r.status_code==200 and r.json()['answer']=='真实汇总解读'
    assert len(r.json()['evidence'])==5


def test_worker_partial_refresh_preserves_last_good_source(client,monkeypatch):
    snapshot('finance',{'statements':[],'rows':[]})
    old=s.snapshot('default','1','finance',WINDOW)
    async def fetch(workspace,shop,source,window,heartbeat,**kwargs):
        heartbeat()
        if source=='finance':raise s.api.ShopAPIError(105005,'safe-request')
        return {'id':s.key(workspace,shop['id'],source,window.start_date,window.end_date),'shop_id':shop['id'],'source':source,'data':{},'complete':source!='videos','retrieved_at':time.time(),'evidence':[]}
    monkeypatch.setattr(s,'fetch_source',fetch)
    j=s.enqueue('default',WINDOW)
    for _ in range(10): assert s.process_one()
    job=store.get(j['id'],'analytics_job')
    assert job['status']=='partial' and job['progress']==10 and len(job['partial_sources'])==2
    assert len(job['errors'])==2 and job['errors'][0]['platform_code']==105005
    assert s.snapshot('default','1','finance',WINDOW)==old


def test_ensure_reuses_running_all_shop_job_for_single_shop_and_previous_window(client,monkeypatch):
    other={**SHOP,'id':'2','region':'TH','currency':'THB','timezone':'Asia/Bangkok'}
    monkeypatch.setattr(s,'shops',lambda workspace:[SHOP,other] if workspace=='default' else [])
    active=s.enqueue('default',WINDOW)
    response=client.post('/api/analytics/ensure',json={**WINDOW.model_dump(mode='json'),'site':'MY'})
    assert response.status_code==200
    assert response.json()['job']['id']==active['id'] and response.json()['reason']=='running'
    assert len(response.json()['missing'])==10
    assert len(store.list_records('analytics_job'))==1


def test_ensure_no_remote_refresh_when_complete_or_partial_snapshots_exist(client):
    for window in (WINDOW,WINDOW.previous()):
        for source in s.SOURCES:snapshot(source,{},window,complete=source!='videos')
    result=client.post('/api/analytics/ensure',json=WINDOW.model_dump(mode='json')).json()
    assert result=={'job':None,'missing':[],'reason':'ready'}
    assert store.list_records('analytics_job')==[]


def test_ensure_rollover_enqueues_new_window_and_cools_down_failed_sources(client):
    for source in s.SOURCES:snapshot(source,{},WINDOW)
    shifted=s.Window(start_date='2026-11-15',end_date='2026-11-21')
    result=s.ensure('default',shifted)
    assert result['reason']=='enqueued' and len(result['missing'])==10
    assert result['job']['window']['start_date']=='2026-11-15'
    store.remove(result['job']['id'],'analytics_job','default')
    store.save({'id':s.key('default','1','orders',shifted.start_date,shifted.end_date),'status':'failed','attempted_at':time.time()},'analytics_attempt')
    blocked=s.ensure('default',shifted)
    assert blocked['job'] is None and blocked['reason']=='recent_failure'
    assert store.list_records('analytics_job')==[]
    assert client.post('/api/analytics/ensure',json=shifted.model_dump(mode='json'),headers={'X-Shop-ID':'other'}).status_code==409


def test_ensure_worker_fetches_only_missing_period_sources(client, monkeypatch):
    for window in (WINDOW, WINDOW.previous()):
        for source in s.SOURCES:
            if window == WINDOW.previous() and source in ('orders', 'finance'):
                continue
            snapshot(source, {}, window)
    calls = []
    async def fetch(workspace, shop, source, window, heartbeat, **kwargs):
        calls.append((source, window.start_date))
        return {'id': s.key(workspace, shop['id'], source, window.start_date, window.end_date),
                'shop_id': shop['id'], 'source': source, 'data': {}, 'complete': True,
                'retrieved_at': time.time(), 'evidence': []}
    monkeypatch.setattr(s, 'fetch_source', fetch)
    job = s.ensure('default', WINDOW)['job']
    assert job['total'] == 2
    assert s.ensure('default', WINDOW)['job']['id'] == job['id']
    assert s.process_one()
    assert s.process_one()
    assert calls == [('orders', WINDOW.previous().start_date), ('finance', WINDOW.previous().start_date)]
    assert store.get(job['id'], 'analytics_job')['status'] == 'completed'
    assert s.ensure('default', WINDOW)['reason'] == 'ready'


@pytest.mark.parametrize('failure', ['network', '503', '429', 'expired', 'denied', 'oversize', 'persistent'])
def test_read_retry_is_bounded_and_refreshes_tokens_only_when_expired(client, monkeypatch, failure):
    forces, calls = [], []
    async def access(*args, force=False):
        forces.append(force)
        return {'access_token': 'test-token'}, 'generation'
    async def remote(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1 or failure == 'persistent':
            if failure in ('expired', 'denied'):
                raise s.api.ShopAPIError(105002 if failure == 'expired' else 105005, 'test-request')
            messages = {'network': 'TikTok Shop 网络或返回格式异常，请稍后重试',
                        'persistent': 'TikTok Shop 网络或返回格式异常，请稍后重试',
                        '503': 'TikTok Shop 服务返回 HTTP 503，请核对网络、应用和授权',
                        '429': 'TikTok Shop 服务返回 HTTP 429，请核对网络、应用和授权',
                        'oversize': 'TikTok Shop 返回数据过大'}
            raise s.api.ShopError(messages[failure])
        return {'data': {}}
    async def no_sleep(*args):
        pass
    monkeypatch.setattr(s.api, 'config', lambda: {'app_key': 'test', 'app_secret': 'test'})
    monkeypatch.setattr(s.api, 'access', access)
    monkeypatch.setattr(s.api, 'connection', lambda workspace: {'generation': 'generation', 'sealed': 'test'})
    monkeypatch.setattr(s.api, 'unseal', lambda *args: {'shops': [{'id': '1', 'cipher': 'test'}]})
    monkeypatch.setattr(s.api, 'locked', lambda *args: None)
    monkeypatch.setattr(s.api, 'request_json', remote)
    monkeypatch.setattr(s.asyncio, 'sleep', no_sleep)
    if failure in ('denied', 'oversize', 'persistent'):
        with pytest.raises(s.api.ShopError):
            asyncio.run(s.request('default', '1', '/order/202309/orders/search', {}, {}))
        assert len(calls) == (3 if failure == 'persistent' else 1)
    else:
        assert asyncio.run(s.request('default', '1', '/order/202309/orders/search', {}, {})) == {'data': {}}
        assert len(calls) == 2
    assert forces == ([False, True] if failure == 'expired' else [False] * len(calls))
