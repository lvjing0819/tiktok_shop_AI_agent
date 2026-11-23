from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Literal
from pydantic import Field
from app import business as b, store, shop_api, business_sync


class Receive(b.Mutation):
    pass


def router(scope):
    r=APIRouter(tags=['Canonical business data'])
    def call(fn,*args):
        try:return fn(*args)
        except b.BusinessError as e:raise HTTPException(409,str(e))
    @r.get('/api/business/dashboard')
    def dashboard(days: Literal['7','30','all']='all', shop_id: str=Query(default='',pattern=r'^[0-9]{0,40}$'), search: str=Query(default='',max_length=200), order_status: str=Query(default='',max_length=40), workspace=Depends(scope)):
        return b.dashboard(workspace, days=None if days=='all' else int(days), shop_id=shop_id, search=search, order_status=order_status)
    @r.get('/api/business/catalog')
    def catalog(workspace=Depends(scope)):
        return {'products':b.list_rows(b.goods,workspace),'skus':b.list_rows(b.skus,workspace),'mappings':b.list_rows(b.mappings,workspace),'pricing':store.list_records('pricing_profile',workspace)}
    @r.post('/api/business/products',status_code=201)
    def product(body:b.GoodsInput,workspace=Depends(scope)):return call(b.create_goods,workspace,body)
    @r.post('/api/business/skus',status_code=201)
    def sku(body:b.SKUInput,workspace=Depends(scope)):return call(b.create_sku,workspace,body)
    @r.post('/api/business/sku-mappings',status_code=201)
    def mapping(body:b.MappingInput,workspace=Depends(scope)):return call(b.create_mapping,workspace,body)
    @r.put('/api/business/skus/{sku_id}/pricing/{market}')
    def pricing(sku_id:str,market:str,body:b.PricingInput,workspace=Depends(scope)):
        if market not in ('MY','TH','US','GB','SG','VN','PH','JP'):raise HTTPException(422,'不支持该市场')
        return call(b.save_pricing,workspace,sku_id,market,body)
    @r.get('/api/business/warehouses')
    def warehouses(workspace=Depends(scope)):return [{**w,'api_status':'not_connected'} for w in b.list_rows(b.warehouses,workspace)]
    @r.post('/api/business/warehouses',status_code=201)
    def warehouse(body:b.WarehouseInput,workspace=Depends(scope)):return call(b.create_warehouse,workspace,body)
    @r.get('/api/business/inventory')
    def inventory(workspace=Depends(scope)):return b.inventory_view(workspace)
    @r.post('/api/business/inventory/snapshot')
    def snapshot(body:b.StockInput,workspace=Depends(scope)):return call(b.set_stock,workspace,body)
    @r.post('/api/business/inventory/reservations',status_code=201)
    def reserve(body:b.ReserveInput,workspace=Depends(scope)):return call(b.reserve,workspace,body)
    @r.post('/api/business/inventory/reservations/{key}/action')
    def action(key:str,body:b.ReservationAction,workspace=Depends(scope)):return call(b.reservation_action,workspace,key,body)
    @r.post('/api/business/inventory/transfers',status_code=201)
    def transfer(body:b.TransferInput,workspace=Depends(scope)):return call(b.dispatch_transfer,workspace,body)
    @r.post('/api/business/inventory/transfers/{key}/receive')
    def receive(key:str,body:Receive,workspace=Depends(scope)):return call(b.receive_transfer,workspace,key,body)
    @r.get('/api/business/inventory/events')
    def events(workspace=Depends(scope)):
        import json
        return [{**r,'payload':json.loads(r['payload'])} for r in b.list_rows(b.events,workspace)]
    @r.get('/api/business/orders/sync-settings')
    def sync_settings(workspace=Depends(scope)):
        return store.list_records('order_poll',workspace)
    @r.put('/api/business/orders/sync-settings')
    async def configure_sync(body:business_sync.SyncSettings,workspace=Depends(scope)):
        try:return await business_sync.configure(workspace,body)
        except b.BusinessError as e:raise HTTPException(409,str(e))
        except shop_api.ShopError as e:raise HTTPException(502,str(e))
    @r.post('/api/business/orders/sync')
    async def sync(body:b.OrderSyncInput,workspace=Depends(scope)):
        try:return await b.sync_orders(workspace,body)
        except (shop_api.ShopError,ValueError) as e:raise HTTPException(502,str(e))
    @r.put('/api/business/orders/{shop_id}/{order_id}/finance')
    def finance(shop_id:str,order_id:str,body:b.FinanceInput,workspace=Depends(scope)):return call(b.save_finance,workspace,shop_id,order_id,body)
    @r.get('/api/business/drafts')
    def drafts(workspace=Depends(scope)):
        return {'content':store.list_records('content_draft',workspace),'listings':store.list_records('listing_draft',workspace)}
    return r
