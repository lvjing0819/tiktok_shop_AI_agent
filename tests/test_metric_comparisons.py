import time
import hashlib
from datetime import date, datetime, timedelta, timezone
import pytest
from app import metrics, analytics as a, analytics_sources as s, business_metrics as bm, store

SHOP={'id':'1','name':'MY','region':'MY','currency':'MYR','timezone':'Asia/Kuala_Lumpur'}
W=s.Window(start_date='2026-11-01',end_date='2026-11-07')

@pytest.fixture(autouse=True)
def database(tmp_path,monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'metrics.sqlite'))
    monkeypatch.setattr(s,'shops',lambda workspace:[SHOP] if workspace=='default' else [])


def snapshot(window,source,data,complete=True):
    store.save({'id':s.key('default','1',source,window.start_date,window.end_date),'shop_id':'1','source':source,
                'start_date':str(window.start_date),'end_date':str(window.end_date),'complete':complete,
                'retrieved_at':time.time(),'evidence':[],'data':data},'analytics_source')


def test_calendar_periods_and_leap_day_are_explicit():
    assert str(W.previous().start_date)=='2026-10-25'
    assert str(W.previous().end_date)=='2026-10-31'
    assert str(W.year_ago().start_date)=='2025-11-01'
    leap=s.Window(start_date='2024-02-29',end_date='2024-02-29')
    assert str(leap.year_ago().start_date)=='2023-02-28'
    spans=s.Window(start_date='2024-02-28',end_date='2024-03-01')
    assert metrics.compare(3,2,same_length=False)['status']=='different_length'
    assert not any(label=='year_ago' for label,_ in s.comparison_periods(spans))


@pytest.mark.parametrize('args,kwargs,status,value',[
    ((120,100),{},'ok',20),((80,100),{},'ok',-20),
    ((60,50),{'kind':'percentage_points'},'ok',10),
    ((-20,-10),{'kind':'magnitude'},'ok',100),
    ((10,-10),{'kind':'magnitude'},'direction_changed',None),
    ((0,0),{},'both_zero',None),((12,0),{},'zero_baseline',None),
    ((12,None),{},'missing_value',None),
    ((12,10),{'baseline_status':'partial'},'partial_baseline',None),
    ((12,10),{'current_status':'partial_detail'},'partial_current',None),
    ((12,10),{'kind':'none'},'not_applicable',None),
    ((12,10),{'baseline_status':'missing_field'},'missing_baseline_field',None),
])
def test_comparison_never_invents_missing_or_infinite_values(args,kwargs,status,value):
    result=metrics.compare(*args,**kwargs)
    assert result['status']==status and result['change']==value


def test_three_period_report_uses_the_same_shop_and_real_baselines():
    for window,count in [(W,3),(W.previous(),2),(W.year_ago(),5)]:
        snapshot(window,'orders',{'rows':[{'id':str(i),'status':'DELIVERED','line_items':[]} for i in range(count)]})
    report=a.report('default',W)
    entry=report['shops'][0]['comparisons']['orders.total']
    assert entry['previous']['change']==50
    assert entry['year_ago']['change']==-40
    assert report['year_ago_window']['start_date']=='2025-11-01'
    assert entry['source']=='orders'
    csv=a.export_csv(report)
    assert '同比开始日期' in csv and '2025-11-01' in csv
    assert a.report('other',W)['shops']==[]


def test_partial_current_source_has_no_growth_even_when_numbers_exist():
    snapshot(W,'orders',{'rows':[{'id':'1','status':'DELIVERED','line_items':[]}]},complete=False)
    snapshot(W.previous(),'orders',{'rows':[]})
    value=a.report('default',W)['shops'][0]['comparisons']['orders.total']['previous']
    assert value['status']=='partial_current' and value['change'] is None


def test_fee_zero_is_retained_for_baselines_and_unknown_fields_stay_unknown():
    for window,fee in [(W,'-2'),(W.previous(),'0'),(W.year_ago(),'-1')]:
        snapshot(window,'finance',{'statements':[],'rows':[{'id':'t','order_id':'o','type':'ORDER','currency':'MYR','settlement_amount':'1','fee_tax_breakdown':{'fee':{'platform_commission_amount':fee}}}]})
    r=a.report('default',W)['shops'][0]
    assert r['previous']['finance']['expenses']==[]
    e=r['comparisons']['expense|fee.platform_commission_amount|amount']
    assert e['previous']['status']=='zero_baseline'
    assert e['year_ago']['change']==100


def test_new_sku_known_empty_period_is_zero_but_missing_year_is_unknown():
    snapshot(W,'orders',{'rows':[{'id':'a','paid_time':1,'status':'DELIVERED','line_items':[{'sku_id':'s','unit_count':2}]}]})
    snapshot(W.previous(),'orders',{'rows':[]})
    e=a.report('default',W)['shops'][0]['comparisons']['sku|s|units']
    assert e['previous']['status']=='zero_baseline' and e['previous']['baseline']=='0'
    assert e['year_ago']['status']=='missing_baseline' and e['year_ago']['baseline'] is None


def test_creator_base_definition_uses_creation_period_and_does_not_call_it_commission():
    for window,amount in [(W,'100'),(W.previous(),'50'),(W.year_ago(),'25')]:
        snapshot(window,'affiliate',{'rows':[{'id':'a','skus':[{'creator_username':'creator','quantity':1,'settlement_status':'SETTLED','actual_commission_base':{'amount':amount,'currency':'MYR'},'actual_paid_commission':{},'actual_paid_shop_ads_commission':{}}]}]})
    r=a.report('default',W)['shops'][0]['comparisons']
    assert r['creator|creator|actual_base']['previous']['change']==100
    assert r['creator|creator|actual_std']['previous']['change'] is None
    definition=metrics.CATALOG['metrics']['creator.actual_base']
    assert '创建' in definition['date_basis'] and '不是达人赚到的佣金' in definition['formula']


def test_inventory_snapshot_compares_previous_day_and_keeps_unknown_year(monkeypatch):
    class Clock(datetime):
        day=datetime(2026,11,22,12,tzinfo=bm.ZONE)
        @classmethod
        def now(cls,zone=None):return cls.day
    monkeypatch.setattr(bm,'datetime',Clock)
    bm.capture_point_metrics('default',{'inventory.available':{'value':10,'status':'complete'}})
    Clock.day+=timedelta(days=1)
    result=bm.capture_point_metrics('default',{'inventory.available':{'value':12,'status':'complete'}})['inventory.available']
    assert result['previous']['change']==20
    assert result['year_ago']['change'] is None and '未留存' in result['year_ago']['reason']
    assert len(store.list_records('business_metric_day'))==2
    assert bm.capture_point_metrics('other',{'inventory.available':{'value':20,'status':'complete'}})['inventory.available']['previous']['change'] is None


def test_comparisons_cover_chart_dimensions_and_retain_decimal_precision():
    for window,fee in [(W,'-0.30'),(W.previous(),'-0.10'),(W.year_ago(),'-0.20')]:
        snapshot(window,'finance',{'statements':[],'rows':[{'id':'a','order_id':'o','type':'ORDER','currency':'MYR','settlement_amount':fee,'fee_tax_breakdown':{'fee':{'platform_commission_amount':fee}}}]})
        snapshot(window,'orders',{'rows':[{'id':'a','status':'CANCELLED','region':'Johor','cancel_reason':'buyer','line_items':[]}]})
    result=a.report('default',W)['shops'][0]['comparisons']
    assert result['expense|fee.platform_commission_amount|amount']['previous']['change']==200
    assert result['expense|fee.platform_commission_amount|amount']['year_ago']['change']==50
    assert result['dimension|regions|Johor']['previous']['change']==0
    assert result['dimension|statuses|已取消']['year_ago']['change']==0


def test_ad_comparisons_require_same_scope_complete_pages_and_real_period_evidence():
    from app import ad_metric_comparisons as ads
    periods=ads.windows('2026-11-01','2026-11-07')
    evidence=[]
    for i,(period,value) in enumerate([('current','120'),('previous','100'),('year_ago','80')]):
        evidence.append({'evidence_id':f'E{i}','tool':'report_get','arguments':{'advertiser_id':'1',**periods[period],'attribution_window':'7d'},'is_error':False,'date_filter_in_schema':True,'result':{'structuredContent':{'data':{'list':[{'dimensions':{'campaign_id':'c'},'metrics':{'spend':value,'impressions':'1000'}}]}}}})
    result=ads.build(evidence,periods)
    spend=next(r for r in result if r['field']=='spend')
    assert spend['previous']['change']==20 and spend['year_ago']['change']==50
    evidence[1]['arguments']['attribution_window']='1d'
    assert next(r for r in ads.build(evidence,periods) if r['field']=='spend')['previous']['status']=='missing_baseline'
    evidence[1]['arguments']['attribution_window']='7d'
    evidence[1]['result']['structuredContent']['data']['page_info']={'total_page':'2'}
    assert next(r for r in ads.build(evidence,periods) if r['field']=='spend')['previous']['status']=='partial_baseline'
    evidence[0]['date_filter_in_schema']=False
    assert ads.build(evidence,periods)==[]


def test_historical_platform_rejection_is_not_reported_as_zero():
    snapshot(W,'performance',{'performance':{'intervals':[{'sales':{'gmv':{'amount':'100','currency':'MYR'}}}]}})
    store.save({'id':s.key('default','1','performance',W.year_ago().start_date,W.year_ago().end_date),'status':'failed','platform_code':28001022,'attempted_at':time.time()},'analytics_attempt')
    result=a.report('default',W)['shops'][0]['comparisons']['performance.gmv']['year_ago']
    assert result['status']=='unavailable_range' and result['change'] is None


def test_inventory_unknown_or_stale_records_never_create_a_fake_zero_growth():
    view={'warehouses':[{'id':'w'}],'rows':[{'warehouse_id':'w','sku_id':'s','on_hand':5,'warehouse_reserved':0,'local_reserved':0,'safety_stock':0,'available':5,'stale':True,'over_reserved':False}]}
    values=bm.inventory_values(view)
    assert values['inventory.available']['status']=='partial_detail'
    result=bm.capture_point_metrics('default',values)
    assert result['inventory.available']['previous']['change'] is None


def test_inventory_catalog_count_does_not_require_a_warehouse():
    values=bm.inventory_values({'sku_count':3,'warehouses':[],'rows':[]})
    assert values['inventory.sku_count']['value']==3
    assert values['inventory.available']['value'] is None


def test_seven_day_chart_compares_same_weekday_not_toolbar_thirty_days():
    today=datetime.now(bm.ZONE).date()
    stamps=lambda day:datetime.combine(day,datetime.min.time(),bm.ZONE).timestamp()+3600
    days=[today,today,today-timedelta(days=7),today.replace(year=today.year-1)]
    rows=[{'shop_id':'1','order_id':str(i),'create_time':stamps(day),'currency':'MYR','status':'DELIVERED','total_amount':'10'} for i,day in enumerate(days)]
    result={'orders':{'rows':rows,'sync':[{'shop_id':'1','complete':True,'window':{'create_time_ge':stamps(today-timedelta(days=400))-3600,'create_time_lt':stamps(today+timedelta(days=1))}}]},'inventory':{'warehouses':[],'rows':[]},'finance':{}}
    result=bm.decorate('default',result,days=30,shop_id='1')
    chart=result['order_chart'][-1]
    assert chart['count']==2 and chart['previous']['change']==100 and chart['year_ago']['change']==100
    assert chart['previous']['baseline_day']==str(today-timedelta(days=7))


def test_unqueryable_year_sources_do_not_loop_refresh_jobs():
    for label,period in s.comparison_periods(W):
        for source in s.SOURCES:
            if label=='year_ago' and source in ('performance','videos'):
                store.save({'id':s.key('default','1',source,period.start_date,period.end_date),'status':'failed','platform_code':28001022,'attempted_at':time.time()},'analytics_attempt')
            else:snapshot(period,source,{})
    result=s.ensure('default',W)
    assert result['reason']=='history_unavailable' and result['job'] is None and len(result['unavailable'])==2


def test_gmv_max_cost_field_is_compared_from_matching_real_evidence():
    from app import ad_metric_comparisons as ads
    periods=ads.windows('2026-11-01','2026-11-07')
    evidence=[{'evidence_id':name,'tool':'gmv_max_report_get','arguments':{'advertiser_id':'1',**window},'date_filter_in_schema':True,'result':{'structuredContent':{'data':{'list':[{'dimensions':{'store_id':'1'},'metrics':{'cost':value}}]}}}} for (name,window),value in zip(periods.items(),('120','100','80'))]
    row=ads.build(evidence,periods)[0]
    assert row['definition']=='ads.cost' and row['previous']['change']==20 and row['year_ago']['change']==50
