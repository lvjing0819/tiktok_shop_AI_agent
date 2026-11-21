import json
import pytest
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from app.main import app
from app import business as b, store, shop_read


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False);monkeypatch.delenv('APP_TOKEN',raising=False)
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'business.sqlite'))
    return TestClient(app)


def setup_stock(client,on_hand=100):
    p=client.post('/api/business/products',json={'name':'真实手表','facts':'人工确认的商品事实'}).json()
    sku=client.post('/api/business/skus',json={'product_id':p['id'],'code':'WATCH-01'}).json()
    cn=client.post('/api/business/warehouses',json={'name':'大陆仓','country':'CN','kind':'mainland'}).json()
    my=client.post('/api/business/warehouses',json={'name':'马来仓','country':'MY','kind':'overseas'}).json()
    r=client.post('/api/business/inventory/snapshot',json={'warehouse_id':cn['id'],'sku_id':sku['id'],'on_hand':on_hand,'warehouse_reserved':10,'safety_stock':5,'expected_revision':0,'operation_id':'snapshot-cn-01','reason':'首次盘点'})
    assert r.status_code==200,r.text
    return p,sku,cn,my


def test_unknown_stock_does_not_become_platform_or_zero(client):
    _,sku,cn,my=setup_stock(client)
    d=client.get('/api/business/dashboard').json()
    rows=d['inventory']['rows'];c=next(r for r in rows if r['country']=='CN');m=next(r for r in rows if r['country']=='MY')
    assert c['on_hand']==100 and c['available']==85
    assert m['on_hand'] is None and m['available'] is None
    assert not d['inventory']['channel_inventory_is_physical']
    assert all(w['api_status']=='not_connected' for w in d['inventory']['warehouses'])
    assert d['finance']['by_currency']=={}


def test_stock_revision_idempotency_and_tenant_isolation(client):
    _,sku,cn,my=setup_stock(client)
    body={'warehouse_id':cn['id'],'sku_id':sku['id'],'on_hand':90,'warehouse_reserved':10,'expected_revision':1,'operation_id':'snapshot-cn-02','reason':'再次盘点'}
    a=client.post('/api/business/inventory/snapshot',json=body);assert a.status_code==200
    assert client.post('/api/business/inventory/snapshot',json=body).json()['revision']==2
    assert client.post('/api/business/inventory/snapshot',json={**body,'on_hand':80}).status_code==409
    assert client.post('/api/business/inventory/snapshot',json={**body,'operation_id':'snapshot-cn-03'}).status_code==409
    assert client.post('/api/business/inventory/snapshot',json=body,headers={'X-Shop-ID':'other'}).status_code==409
    assert client.get('/api/business/dashboard',headers={'X-Shop-ID':'other'}).json()['inventory']['rows']==[]


def test_reservation_warehouse_ack_ship_and_cancel_no_double_deduction(client):
    _,sku,cn,my=setup_stock(client)
    body={'warehouse_id':cn['id'],'sku_id':sku['id'],'order_ref':'order-line-1','quantity':20,'operation_id':'reserve-01','reason':'订单占用'}
    r=client.post('/api/business/inventory/reservations',json=body);assert r.status_code==201
    key=r.json()['id']
    duplicate=client.post('/api/business/inventory/reservations',json={**body,'operation_id':'reserve-02'}).json();assert duplicate['id']==key
    def view():return next(r for r in client.get('/api/business/inventory').json()['rows'] if r['country']=='CN')
    assert view()['available']==65
    ack={'action':'warehouse_ack','warehouse_confirmed':True,'operation_id':'ack-order-01','reason':'仓库接收确认'}
    assert client.post(f'/api/business/inventory/reservations/{key}/action',json=ack).status_code==200
    assert view()['available']==65 and view()['warehouse_reserved']==30 and view()['local_reserved']==0
    assert client.post(f'/api/business/inventory/reservations/{key}/action',json=ack).status_code==200
    assert view()['available']==65
    assert client.post(f'/api/business/inventory/reservations/{key}/action',json={'action':'cancel','operation_id':'cancel-order-1','reason':'平台已取消'}).status_code==409
    ship={'action':'ship','warehouse_confirmed':True,'operation_id':'ship-order-01','reason':'实际出库确认'}
    assert client.post(f'/api/business/inventory/reservations/{key}/action',json=ship).status_code==200
    assert view()['on_hand']==80 and view()['warehouse_reserved']==10 and view()['available']==65
    assert client.post(f'/api/business/inventory/reservations/{key}/action',json=ship).status_code==200
    assert view()['on_hand']==80


def test_transfer_in_transit_and_receipt_no_double_count(client):
    _,sku,cn,my=setup_stock(client)
    client.post('/api/business/inventory/snapshot',json={'warehouse_id':my['id'],'sku_id':sku['id'],'on_hand':0,'expected_revision':0,'operation_id':'snapshot-my-01','reason':'初始盘点'})
    body={'source_id':cn['id'],'destination_id':my['id'],'sku_id':sku['id'],'quantity':20,'operation_id':'transfer-01','reason':'发往海外仓'}
    r=client.post('/api/business/inventory/transfers',json=body);assert r.status_code==201,r.text
    key=r.json()['id'];d=client.get('/api/business/inventory').json()
    assert sum(r['on_hand'] for r in d['rows'])==80
    assert d['transfers'][0]['state']=='in_transit'
    assert client.post('/api/business/inventory/transfers',json=body).json()['id']==key
    receive={'operation_id':'receive-01','reason':'海外仓实收'}
    assert client.post(f'/api/business/inventory/transfers/{key}/receive',json=receive).status_code==200
    assert client.post(f'/api/business/inventory/transfers/{key}/receive',json=receive).status_code==200
    assert sum(r['on_hand'] for r in client.get('/api/business/inventory').json()['rows'])==100
    assert client.post(f'/api/business/inventory/transfers/{key}/receive',json={'operation_id':'receive-02','reason':'重复通知'}).status_code==409


def test_concurrent_reservations_cannot_oversell(client):
    _,sku,cn,my=setup_stock(client,on_hand=30)
    def reserve(i):
        body=b.ReserveInput(warehouse_id=cn['id'],sku_id=sku['id'],quantity=10,order_ref='line-'+str(i),operation_id='concurrent-'+str(i),reason='订单占用')
        try:b.reserve('default',body);return True
        except b.BusinessError:return False
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(reserve,[1,2]))
    assert sum(results)==1
    assert next(r for r in b.inventory_view('default')['rows'] if r['country']=='CN')['available']==5


def test_order_sync_missing_cost_profit_and_currency_separation(client,monkeypatch):
    async def execute(workspace,kind,request):
        assert workspace=='default' and kind=='orders' and request.shop_id=='1'
        return {'complete':True,'date_window':{'start_date':'2026-11-19','end_date':'2026-11-19'},'warnings':[],'retrieved_count':2,'evidence':[],'rows':[{'id':'10','status':'DELIVERED','paid_time':1,'create_time':1000,'payment':{'currency':'MYR','total_amount':'100.10'}},{'id':'20','status':'UNPAID','create_time':1001,'payment':{'currency':'USD','total_amount':'10.20'}}]}
    monkeypatch.setattr(shop_read,'execute',execute)
    body={'shop_id':'1','start_date':'2026-11-19','end_date':'2026-11-19'}
    assert client.post('/api/business/orders/sync',json=body).status_code==200
    assert client.post('/api/business/orders/sync',json=body).status_code==200
    d=client.get('/api/business/dashboard').json();assert d['orders']['count']==2
    assert d['finance']['by_currency']['MYR']['estimated_contribution_known_orders'] is None
    costs={'currency':'MYR','procurement':'20','logistics':'5','advertising':'10','platform_fee':'8','refund':'0','other':'1','settled_net':'92.10','costs_complete':True,'expected_revision':0,'source_note':'人工核对成本及净结算'}
    assert client.put('/api/business/orders/1/10/finance',json=costs).status_code==200
    d=client.get('/api/business/dashboard').json();g=d['finance']['by_currency']['MYR']
    assert g['estimated_contribution_known_orders']=='56.10'
    assert g['settled_profit_known_orders']=='56.10' # fee not deducted a second time
    assert d['finance']['by_currency']['USD']['confirmed_paid_non_cancelled_amount']=='0'
    assert client.put('/api/business/orders/1/10/finance',json=costs).status_code==409
    assert client.put('/api/business/orders/1/20/finance',json=costs).status_code==409


def test_sku_mapping_and_pricing_source_versions(client):
    p,sku,cn,my=setup_stock(client)
    body={'sku_id':sku['id'],'source':'warehouse','account_id':cn['id'],'external_sku':'WH-WATCH-01'}
    assert client.post('/api/business/sku-mappings',json=body).status_code==201
    assert client.post('/api/business/sku-mappings',json=body).status_code==201
    pricing={'unit_cost':'10','logistics':'2','fixed_fees':'1','currency':'MYR','fee_rate':'0.1','target_margin':'0.3','expected_revision':0,'source_note':'人工确认'}
    assert client.put('/api/business/skus/'+sku['id']+'/pricing/MY',json=pricing).status_code==200
    assert client.put('/api/business/skus/'+sku['id']+'/pricing/MY',json=pricing).status_code==409
    assert client.put('/api/business/skus/'+sku['id']+'/pricing/TH',json={**pricing,'fee_rate':'0.9'}).status_code==409


def test_dashboard_never_silently_truncates_at_1000_orders(client):
    rows=[]
    for i in range(1001):
        payload={'id':str(i),'status':'DELIVERED','paid_time':1,'payment':{'currency':'MYR','total_amount':'1.10'}}
        rows.append({'id':'order-'+str(i),'workspace':'default','shop_id':'1','order_id':str(i),'create_time':i,'status':'DELIVERED','payload':json.dumps(payload),'synced_at':1})
    with store.db(write=True) as conn:conn.execute(b.orders.insert(),rows)
    data=b.dashboard('default')
    assert data['orders']['count']==1001
    assert data['finance']['by_currency']['MYR']['confirmed_paid_non_cancelled_amount']=='1101.10'
