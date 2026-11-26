import copy
import time
import pytest
from fastapi.testclient import TestClient
from app import store,agent_artifacts as artifacts,agent_maintenance as maintenance,report_retention as reports
from app.main import app
from app.tool_registry import ExecutionError

DAY=86400

@pytest.fixture(autouse=True)
def env(tmp_path,monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.delenv('APP_TOKEN',raising=False)
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'reports.sqlite'))
    monkeypatch.setenv('AGENT_CLEANUP_ENABLED','0')
    return TestClient(app)


def report(key='report',kind='ads_report',age=31,workspace='default',**extra):
    item={'id':key,'created_at':time.time()-age*DAY,'question':'Query','answer':'Analysis',
          'evidence':[{'result':{'spend':10},'evidence_id':'e1','tool':'read'}],
          'advertiser_id':'123','start_date':'2026-10-01','end_date':'2026-10-02',
          'data_verified':True,**extra}
    return store.save(item,kind,workspace)


def apply(plan):return maintenance.apply('default',plan['id'],plan['confirmation'])


def actions():return maintenance.preview('default')['actions']


def end_grace(key='report',kind='ads_report'):
    item=store.get(key,kind);item['cleanup_due_at']=time.time()-1;store.save(item,kind)


def active_run(key='run',tool='ads.analyze',status='waiting_input',step_status='completed',result=None):
    item={'id':key,'status':status,'created_at':time.time(),'plan':{'steps':[{'id':'a','tool':tool}]},
          'steps':{'a':{'status':step_status,**({'result':result} if result else {})}}}
    return store.save(item,'operation_run')


@pytest.mark.parametrize('kind,route',[('ads_report','ads'),('social_report','social')])
def test_age_grace_expiration_and_http_tombstones(env,kind,route):
    report(kind=kind)
    first=maintenance.preview('default')
    assert first['actions'][0]['action']=='mark_record'
    apply(first)
    assert store.get('report',kind)['answer']=='Analysis'
    assert not actions()
    end_grace(kind=kind)
    plan=maintenance.preview('default')
    assert plan['actions'][0]['action']=='expire_report' and plan['estimated_reclaim_bytes']>0
    apply(plan)
    saved=store.get('report',kind)
    assert saved['expired'] and 'answer' not in saved and 'evidence' not in saved and 'question' not in saved
    assert env.get(f'/api/{route}/reports/report').status_code==410
    rows=env.get(f'/api/{route}/reports').json()
    assert rows[0]['expired'] and rows[0]['created_at'] and rows[0]['expired_at']
    assert not actions()


def test_recent_and_unknown_timestamp_reports_are_not_candidates(env):
    report(age=29);report('unknown',created_at=None);report('future',age=-1)
    assert not actions()
    assert any(a['id']=='report' for a in maintenance.preview('default',maintenance.Policy(report_days=20))['actions'])


def test_pin_rechecks_preview_and_unpin_starts_new_grace(env):
    report();plan=maintenance.preview('default')
    saved=reports.pin('report','ads_report','default',1,True)
    assert saved['pinned'] and saved['retention_revision']==2
    with pytest.raises(ExecutionError):apply(plan)
    assert not actions()
    reports.pin('report','ads_report','default',2,False)
    apply(maintenance.preview('default'));end_grace()
    due=maintenance.preview('default')
    reports.pin('report','ads_report','default',3,True)
    with pytest.raises(ExecutionError):apply(due)
    reports.pin('report','ads_report','default',4,False)
    assert maintenance.preview('default')['actions'][0]['action']=='mark_record'


def test_pin_api_is_scoped_versioned_and_authenticated(env,monkeypatch):
    report()
    path='/api/agent-reports/ads_report/report/retention'
    assert env.put(path,json={'retention_revision':1,'pinned':True},headers={'X-Shop-ID':'other'}).status_code==404
    assert env.put(path,json={'retention_revision':1,'pinned':True}).status_code==200
    assert env.put(path,json={'retention_revision':1,'pinned':False}).status_code==409
    assert env.put(path,json={'pinned':False}).status_code==422
    assert env.put(path.replace('ads_report','session'),json={'retention_revision':1,'pinned':False}).status_code==422
    monkeypatch.setenv('APP_TOKEN','owner')
    assert env.put(path,json={'retention_revision':2,'pinned':False}).status_code==401


def test_expired_report_cannot_be_restored_by_bookmark(env):
    report();apply(maintenance.preview('default'));end_grace();apply(maintenance.preview('default'))
    result=env.put('/api/agent-reports/ads_report/report/retention',json={'retention_revision':2,'pinned':True})
    assert result.status_code==410


@pytest.mark.parametrize('packed',[False,True])
def test_unfinished_task_reports_are_protected_without_protecting_unrelated_reports(env,packed):
    item=report();report('unrelated')
    result={'data':item,'evidence':[],'status':'completed'}
    if packed:
        with store.db(write=True) as conn:result=artifacts.pack(conn,'default',result)
    active_run(result=result)
    selected=actions()
    assert not any(a['kind']=='ads_report' and a['id']=='report' for a in selected)
    assert any(a['kind']=='ads_report' and a['id']=='unrelated' for a in selected)


@pytest.mark.parametrize('record_kind',['agent_call','tool_trace'])
def test_receipts_protect_report_when_step_result_is_not_committed(env,record_kind):
    item=report();active_run(step_status='failed')
    with store.db(write=True) as conn:result=artifacts.pack(conn,'default',{'data':item,'evidence':[]})
    store.save({'id':'receipt','run_id':'run','step_id':'a','effect':'read','result':result},record_kind)
    assert not any(a['kind']=='ads_report' for a in actions())


def test_new_task_reference_invalidates_already_generated_cleanup_preview(env):
    item=report();apply(maintenance.preview('default'));end_grace()
    plan=maintenance.preview('default');active_run(result={'data':item})
    with pytest.raises(ExecutionError):apply(plan)
    assert store.get('report','ads_report')['answer']=='Analysis'


def test_unknown_running_analysis_is_conservatively_protected(env):
    report();report('social',kind='social_report')
    active_run(step_status='running',status='running')
    selected=actions()
    assert not any(a['kind']=='ads_report' for a in selected)
    assert any(a['kind']=='social_report' for a in selected)


def test_cleanup_never_changes_chats_memory_or_business_records(env):
    report()
    fixtures={
        'session':{'id':'chat','messages':[{'role':'user','content':'Query'}],'summary':'Summary','query_context':{'last_ads':{'report_id':'report'}}},
        'memory':{'id':'preferences:default','revision':1,'notes':'Long term'},
        'conversation_run':{'id':'conversation','session_id':'chat','answer':'Existing analysis'},
        'analytics_day':{'id':'day','orders':10},'shop_grant':{'id':'grant','opaque':'saved'}}
    for kind,item in fixtures.items():store.save(item,kind)
    before=copy.deepcopy(fixtures)
    apply(maintenance.preview('default'));end_grace();apply(maintenance.preview('default'))
    assert all(store.get(item['id'],kind)==item for kind,item in before.items())


def test_scheduler_discovers_report_only_workspaces_and_honors_report_days(env,monkeypatch):
    report(age=35,workspace='report-only')
    assert maintenance.tick() is False
    monkeypatch.setenv('AGENT_CLEANUP_ENABLED','1');monkeypatch.setenv('AGENT_REPORT_RETENTION_DAYS','40')
    assert maintenance.tick()
    assert not store.get('report','ads_report','report-only').get('cleanup_due_at')
    monkeypatch.setenv('AGENT_REPORT_RETENTION_DAYS','30')
    store.save({'id':'retention','next_at':0},'agent_maintenance','system')
    assert maintenance.tick()
    assert store.get('report','ads_report','report-only')['cleanup_due_at']>time.time()


def test_report_scope_does_not_apply_to_another_workspace(env):
    report(workspace='other')
    assert not actions()
    assert store.get('report','ads_report','other')['answer']=='Analysis'
