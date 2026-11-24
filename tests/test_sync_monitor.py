import asyncio
import time
import pytest
from fastapi.testclient import TestClient
from app import store, sync_monitor as monitor, analytics_sources as sources, business_sync, business
from app.main import app


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.delenv('APP_TOKEN', raising=False)
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'alerts.sqlite'))
    monkeypatch.setattr(sources, 'shops', lambda workspace: [{'id':'1','name':'MY','region':'MY','currency':'MYR','timezone':'Asia/Kuala_Lumpur'}] if workspace == 'default' else [])


def outcome(attempt, status='failed', workspace='default', shop='1', source='orders', pipeline='analytics'):
    with store.db(write=True) as conn:
        monitor.record(conn, workspace, pipeline, shop, source, str(attempt), status)


def test_threshold_duplicate_delivery_and_recovery_survive_storage():
    for i in range(9):outcome(i)
    assert monitor.active('default') == []
    outcome(9)
    alert = monitor.active('default')[0]
    assert alert['consecutive_failures'] == 10
    outcome(10)
    outcome(0)  # Even an old duplicate cannot count again.
    assert len(store.list_records('sync_alert')) == 1
    assert monitor.active('default')[0]['consecutive_failures'] == 11
    outcome(11, 'complete')
    assert monitor.active('default') == []
    assert store.get(alert['id'], 'sync_alert')['status'] == 'resolved'
    for i in range(12,22):outcome(i, 'partial')
    assert monitor.active('default')[0]['id'] != alert['id']


def test_source_shop_pipeline_and_workspace_do_not_share_streaks():
    for i in range(10):outcome(i)
    for changes in ({'source':'finance'}, {'shop':'2'}, {'workspace':'other'}, {'pipeline':'order_poll'}):
        outcome('success', 'complete', **changes)
    assert monitor.active('default', ['1'])[0]['consecutive_failures'] == 10
    assert monitor.active('default', ['2']) == []
    assert monitor.active('other') == []


def test_split_units_count_one_attempt_and_comparison_errors_do_not_count():
    units=[{'shop_id':'1','source':'orders','period':period,'start_date':date,'end_date':date}
           for period,date in [('current','2026-11-01'),('current','2026-11-03'),('year_ago','2025-11-01')]]
    for i in range(10):
        job={'id':str(i),'status':'partial','completed':[sources.identity(units[0])], 'errors':units[1:], 'partial_sources':[]}
        with store.db(write=True) as conn:
            monitor.record_job(conn,'default',job,units)
            monitor.record_job(conn,'default',job,units)
    assert monitor.active('default')[0]['consecutive_failures'] == 10
    assert len(store.list_records('sync_health')) == 1
    job={'id':'success','status':'partial','completed':[sources.identity(u) for u in units[:2]],'errors':[units[2]]}
    with store.db(write=True) as conn:monitor.record_job(conn,'default',job,units)
    assert monitor.active('default') == []


def test_actual_analytics_jobs_raise_and_resolve_alert(monkeypatch):
    async def fail(*args, **kwargs):raise RuntimeError('secret-do-not-persist')
    monkeypatch.setattr(sources,'fetch_source',fail)
    window=sources.Window(start_date='2026-11-01',end_date='2026-11-01')
    for i in range(10):
        sources.enqueue('default',window,source_names=['orders'],include_previous=False)
        assert sources.process_one()
    assert len(monitor.active('default')) == 1
    assert 'secret-do-not-persist' not in str(store.list_records('sync_alert'))
    async def success(workspace,shop,source,w,*args,**kwargs):
        return {'id':sources.key(workspace,shop['id'],source,w.start_date,w.end_date),'shop_id':shop['id'],'source':source,'start_date':str(w.start_date),'end_date':str(w.end_date),'complete':True,'retrieved_at':time.time(),'evidence':[],'data':{'rows':[]}}
    monkeypatch.setattr(sources,'fetch_source',success)
    sources.enqueue('default',window,source_names=['orders'],include_previous=False)
    sources.process_one()
    assert monitor.active('default') == []


def test_legacy_order_poll_counts_partial_and_recovers(monkeypatch):
    config={'id':'poll','shop_id':'1','enabled':True,'next_at':0,'interval_seconds':60,'lookback_days':7,'timezone':'Asia/Kuala_Lumpur','revision':1}
    async def incomplete(*args):return {'complete':False}
    monkeypatch.setattr(business,'sync_orders',incomplete)
    store.save(config,'order_poll')
    for i in range(10):
        config=store.get('poll','order_poll');config['next_at']=0;store.save(config,'order_poll')
        assert business_sync.process_one()
    assert monitor.active('default')[0]['pipeline'] == 'order_poll'
    async def complete(*args):return {'complete':True}
    monkeypatch.setattr(business,'sync_orders',complete)
    config=store.get('poll','order_poll');config['next_at']=0;store.save(config,'order_poll')
    assert business_sync.process_one() and monitor.active('default') == []


def test_alert_api_honors_filters_and_auth(monkeypatch):
    for i in range(10):outcome(i)
    client=TestClient(app)
    assert len(client.get('/api/analytics/alerts?site=MY').json()['alerts']) == 1
    assert client.get('/api/analytics/alerts?site=TH').json()['alerts'] == []
    assert client.get('/api/analytics/alerts',headers={'X-Shop-ID':'other'}).json()['alerts'] == []
    report=client.get('/api/analytics/dashboard?start_date=2026-11-01&end_date=2026-11-01').json()
    assert len(report['sync_alerts']) == 1
    monkeypatch.setenv('APP_TOKEN','test-token')
    assert client.get('/api/analytics/alerts').status_code == 401


def test_transaction_rollback_does_not_count_an_outcome():
    with pytest.raises(RuntimeError):
        with store.db(write=True) as conn:
            monitor.record(conn,'default','analytics','1','orders','attempt','failed')
            raise RuntimeError('rollback')
    assert store.list_records('sync_health') == []
    assert store.list_records('sync_outcome') == []
