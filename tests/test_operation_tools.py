import asyncio
import json
import time
import pytest
from app import business as b, market_tools as market, operation_tools as tools, store, model, shop_read
from app.tool_registry import ExecutionError


@pytest.fixture
def workspace(tmp_path,monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'tools.sqlite'))
    return 'default'


def run(coro):return asyncio.run(coro)


def market_fixture(monkeypatch,pages,region='MY',rankings=None):
    calls=[]
    async def discover(workspace,selector):return {'shops':[]},{'id':'1','region':region}
    async def request(workspace,selected,path,filters,body=None):
        calls.append((path,filters,body))
        if path==market.CATEGORIES:
            data={'categories':[{'id':'605248','parent_id':'0','local_name':'Fashion'},{'id':'605254','parent_id':'605248','local_name':"Women's Watches"}]}
        elif path==market.RANKING:data={'products':rankings or []}
        else:data=pages.pop(0)
        return {'code':0,'data':data,'request_id':'proof-'+str(len(calls))}
    monkeypatch.setattr(shop_read,'discover',discover);monkeypatch.setattr(market,'request',request)
    return calls


def product(id='p',category='605254',region='MY',units=100):
    return {'id':id,'title':'Ladies watch','sale_region':region,'shop':{'name':'Real shop'},'category_chains':[{'id':category,'is_leaf':True}],'units_sold':units,'sales_price':{'currency':'MYR','minimum_amount':'10','maximum_amount':'20'}}


def test_leaf_cumulative_uses_first_level_api_filter_and_paginates_matching_items(monkeypatch):
    calls=market_fixture(monkeypatch,[{'products':[product('other','999')],'next_page_token':'next'},{'products':[product('one'),product('two',units=90)]}])
    r=run(market.search('default',{'market':'MY','category_id':'605254','ranking':'cumulative','limit':2}))
    assert r['status']=='completed' and r['data']['returned_count']==2
    assert r['data']['affiliate_scanned_count']==3
    assert [p['id'] for p in r['data']['products']]==['one','two']
    api_calls=[c for c in calls if c[0]==market.AFFILIATE]
    assert all(c[2]['category']['id']=='605248' for c in api_calls)
    assert api_calls[1][1]['page_token']=='next'
    assert r['data']['complete_target_ranking'] and r['evidence'][1]['filters']['category_id']=='605248'


def test_cross_market_affiliate_never_supplies_price_or_units(monkeypatch):
    market_fixture(monkeypatch,[{'products':[product(region='MY')]}],region='TH',rankings=[{'id':'p','rank':1,'name':'Ladies watch','gmv_range':'100 - 200'}])
    r=run(market.search('default',{'market':'TH','category_id':'605254','ranking':'gmv','limit':1,'end_date':'2026-11-19'}))
    p=r['data']['products'][0]
    assert p['sales_price'] is None and p['cumulative_units_sold'] is None and p['estimated_units'] is None
    assert not r['data']['complete_target_ranking']
    market_fixture(monkeypatch,[{'products':[product(region='MY')]}],region='TH')
    r=run(market.search('default',{'market':'TH','ranking':'cumulative'}))
    assert r['status']=='waiting_input'


def test_gmv_filter_coverage_and_outward_estimate(monkeypatch):
    market_fixture(monkeypatch,[{'products':[product()]}],rankings=[{'id':'p','rank':7,'name':'Ladies watch','gmv_range':'101 - 201'},{'id':'q','rank':8,'name':'Men watch','gmv_range':'10 - 20'}])
    r=run(market.search('default',{'market':'MY','ranking':'gmv','category_id':'605254','limit':30,'end_date':'2026-11-19','keywords':['ladies']}))
    assert len(r['data']['products'])==1 and r['data']['products'][0]['rank']==7
    assert r['data']['products'][0]['estimated_units']['lower']==5
    assert r['data']['products'][0]['estimated_units']['upper']==21
    assert not r['data']['complete_target_ranking']
    assert market.estimate('100 - 200',product()['sales_price'],'THB') is None
    assert market.estimate('100+',product()['sales_price'],'MYR') is None


def test_historical_cumulative_and_unknown_timezone_require_input(monkeypatch):
    market_fixture(monkeypatch,[],region='US')
    assert run(market.search('default',{'market':'US','ranking':'gmv'}))['status']=='waiting_input'
    assert run(market.search('default',{'market':'US','ranking':'cumulative','end_date':'2026-11-19'}))['status']=='waiting_input'


def setup_product(workspace):
    p=b.create_goods(workspace,b.GoodsInput(name='手表',facts='已确认：白色表盘。其他材质和防水等级未知。'))
    sku=b.create_sku(workspace,b.SKUInput(product_id=p['id'],code='W1'))
    wh=b.create_warehouse(workspace,b.WarehouseInput(name='MY warehouse',country='MY',kind='overseas'))
    return p,sku,wh


def test_content_draft_reused_and_listing_requires_real_facts_inventory_and_rules(workspace,monkeypatch):
    p,sku,wh=setup_product(workspace);calls=[]
    def message(messages,functions):
        calls.append(messages)
        data={'title':'Jam tangan','description':'Dail putih','image_briefs':['白色表盘实物展示'],'video_script':'展示表盘','claims_to_verify':[]}
        return {'tool_calls':[{'function':{'name':'submit_content_draft','arguments':json.dumps(data)}}]}
    monkeypatch.setattr(model,'message',message)
    ctx={'workspace':workspace,'operation_id':'test-step','run_id':'test-run'}
    content=run(tools.content_draft(ctx,{'product_id':p['id'],'market':'MY'}))['data']
    assert content['media_status']=='not_generated' and content['approval_status']=='not_reviewed'
    assert run(tools.content_draft(ctx,{'product_id':p['id'],'market':'MY'}))['data']['id']==content['id'] and len(calls)==1
    args={'product_id':p['id'],'sku_id':sku['id'],'warehouse_id':wh['id'],'market':'MY','content_draft_id':content['id'],'content_draft_revision':content['revision']}
    assert run(tools.product_draft(ctx,args))['status']=='waiting_input'
    b.set_stock(workspace,b.StockInput(warehouse_id=wh['id'],sku_id=sku['id'],on_hand=30,warehouse_reserved=5,safety_stock=2,expected_revision=0,operation_id='snapshot-test',reason='真实盘点依据'))
    assert run(tools.product_draft(ctx,args))['status']=='waiting_input'
    profile=b.PricingInput(unit_cost='10',logistics='2',fixed_fees='1',currency='MYR',fee_rate='0.1',target_margin='0.3',listing_cap=8,expected_revision=0,source_note='人工确认')
    b.save_pricing(workspace,sku['id'],'MY',profile)
    draft=run(tools.product_draft(ctx,args))['data']
    assert draft['proposed_price']=='21.67' and draft['proposed_channel_quantity']==8
    assert draft['stock_revision']==1 and not draft['ready_to_publish']
    assert 'generated_and_uploaded_product_images' in draft['missing']
    with pytest.raises(b.BusinessError):b.save_pricing(workspace,sku['id'],'TH',profile)
    with store.db(write=True) as conn:conn.execute(b.stock.update().values(updated_at=time.time()-90000))
    assert run(tools.product_draft(ctx,args))['status']=='waiting_input'


def test_content_mismatch_and_unavailable_capabilities(workspace):
    registry=tools.build_registry()
    for name,role in [('product.publish','product'),('media.generate','content'),('warehouse.sync','product')]:
        assert not registry.get(name,role).available
    with pytest.raises(ExecutionError):registry.get('product.publish','content')
