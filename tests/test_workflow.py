import json
import time
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import store, model
from app.worker import process_one

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.delenv('APP_TOKEN', raising=False)
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path/'test.sqlite'))
    monkeypatch.setenv('CHECKPOINT_PATH', str(tmp_path/'checkpoint.sqlite'))
    monkeypatch.setenv('MODEL_MODE', 'demo')
    return TestClient(app)


def create(client, stock=10):
    r=client.post('/api/tasks', json={'product':{'name':'Cup','sku':'C1','cost':7,'stock':stock},'target_margin':.3})
    assert r.status_code==202
    return r.json()['id']


def test_approval_restart_and_idempotency(client):
    task_id=create(client)
    assert client.get('/api/tasks/'+task_id).json()['status']=='queued'
    assert process_one()
    # A fresh worker opens the same persistent graph to resume approval.
    t=client.get('/api/tasks/'+task_id).json()
    assert t['status']=='awaiting_review'
    assert t['price']=='10.00'
    payload={'approved':True,'version':1}
    first=client.post(f'/api/tasks/{task_id}/review',json=payload)
    assert first.status_code==202
    assert client.post(f'/api/tasks/{task_id}/review',json=payload).status_code==202
    process_one()
    result=client.get('/api/tasks/'+task_id).json()
    assert result['listing_id']=='mock-'+task_id
    assert result['status']=='published'
    assert client.post(f'/api/tasks/{task_id}/review',json=payload).json()['listing_id']==result['listing_id']
    assert not process_one()


def test_stock_failure_blocks_publication(client):
    task_id=create(client,0)
    process_one()
    assert client.get('/api/tasks/'+task_id).json()['status']=='failed'
    assert client.post(f'/api/tasks/{task_id}/review',json={'approved':True,'version':1}).status_code==409


def test_rejection_and_invalid_version(client):
    task_id=create(client)
    process_one()
    assert client.post(f'/api/tasks/{task_id}/review',json={'approved':True,'version':2}).status_code==409
    client.post(f'/api/tasks/{task_id}/review',json={'approved':False,'version':1})
    process_one()
    assert client.get('/api/tasks/'+task_id).json()['status']=='rejected'
    assert client.post(f'/api/tasks/{task_id}/review',json={'approved':True,'version':1}).status_code==409
    assert client.post('/api/tasks',json={'product':{'name':'Cup','sku':'C1','cost':7,'stock':10},'target_margin':1}).status_code==422


def test_lease_reclamation(client):
    task_id=create(client)
    job=store.claim()
    assert job['id']==task_id
    assert store.claim() is None
    with store.db() as conn:
        conn.execute(store.jobs.update().where(store.jobs.c.id==task_id).values(lease=time.time()-1))
    assert process_one()
    # Original crashed worker cannot overwrite the new owner's result.
    assert not store.finish(job, {'id':task_id,'status':'failed'})
    assert client.get('/api/tasks/'+task_id).json()['status']=='awaiting_review'


def test_failed_model_retry_resumes_graph(client,monkeypatch):
    task_id=create(client)
    def failure(*args): raise model.ModelError('模型不可用')
    monkeypatch.setattr(model,'complete',failure)
    process_one()
    assert client.get('/api/tasks/'+task_id).json()['status']=='failed'
    monkeypatch.setattr(model,'complete',lambda *args:'恢复后的商品文案')
    client.post(f'/api/tasks/{task_id}/retry')
    process_one()
    t=client.get('/api/tasks/'+task_id).json()
    assert t['status']=='awaiting_review'
    assert t['copy']=='恢复后的商品文案'
    assert sum('商品校验完成' in x for x in t['events'])==1


def test_session_memory_summary_and_isolation(client,monkeypatch):
    seen=[]
    def answer(messages):
        seen.append(messages)
        return '已读取条件'
    monkeypatch.setattr(model,'chat',answer)
    session=client.post('/api/sessions',json={'title':'我的店铺'}).json()
    sid=session['id']
    prefs={'market':'美国','brand_style':'简洁','forbidden_claims':'不夸大功效','notes':'','revision':0}
    assert client.put('/api/memory/preferences',json=prefs).status_code==200
    for i in range(9):
        r=client.post(f'/api/sessions/{sid}/messages',json={'content':f'条件{i}'})
        assert r.status_code==200
    # Last request has bounded recent messages and an inspectable historical summary.
    assert len(seen[-1])<=14
    assert '美国' in seen[-1][0]['content']
    assert r.json()['summary']
    assert len(TestClient(app).get('/api/sessions/'+sid).json()['messages'])==18
    assert client.get('/api/sessions/'+sid,headers={'X-Shop-ID':'other'}).status_code==404
    assert client.get('/api/memory/preferences',headers={'X-Shop-ID':'other'}).json()['market']==''
    assert client.get('/api/tasks',headers={'X-Shop-ID':'other'}).json()==[]
    assert client.delete('/api/sessions/'+sid).status_code==200
    assert client.get('/api/sessions/'+sid).status_code==404


def test_preference_revision_and_forgetting(client):
    data={'market':'美国','revision':0}
    r=client.put('/api/memory/preferences',json=data)
    assert r.status_code==200
    assert client.put('/api/memory/preferences',json=data).status_code==409
    cleared=client.delete('/api/memory/preferences').json()
    assert cleared['market']==''
    assert cleared['revision']==2


def test_auth_and_safe_model_error(client,monkeypatch):
    monkeypatch.setenv('APP_TOKEN','test-access')
    assert client.get('/api/tasks').status_code==401
    assert client.get('/api/tasks',headers={'Authorization':'Bearer test-access'}).status_code==200
    monkeypatch.delenv('APP_TOKEN')
    monkeypatch.setenv('MODEL_MODE','bailian')
    monkeypatch.delenv('DASHSCOPE_API_KEY',raising=False)
    r=client.post('/api/model/check')
    assert r.status_code==502
    assert '尚未配置' in r.json()['detail']


def test_chat_failure_does_not_corrupt_history(client,monkeypatch):
    sid=client.post('/api/sessions',json={}).json()['id']
    def fail(*args):raise model.ModelError('连接失败')
    monkeypatch.setattr(model,'chat',fail)
    assert client.post(f'/api/sessions/{sid}/messages',json={'content':'hello'}).status_code==502
    current=client.get('/api/sessions/'+sid).json()
    assert current['messages']==[]
    assert current['busy_until']==0


def test_real_worker_process_restart(client):
    import subprocess
    import sys
    task_id=create(client)
    subprocess.run([sys.executable,'-m','app.worker','--once'],check=True,timeout=30)
    assert client.get('/api/tasks/'+task_id).json()['status']=='awaiting_review'
    client.post(f'/api/tasks/{task_id}/review',json={'approved':True,'version':1})
    subprocess.run([sys.executable,'-m','app.worker','--once'],check=True,timeout=30)
    assert client.get('/api/tasks/'+task_id).json()['status']=='published'


def test_bailian_request_contract(monkeypatch):
    import httpx
    seen=[]
    def handle(request):
        seen.append(request)
        return httpx.Response(200,json={'choices':[{'message':{'content':'连接成功'}}]})
    real_client=httpx.Client
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:real_client(transport=httpx.MockTransport(handle),**kwargs))
    monkeypatch.setenv('MODEL_MODE','bailian')
    monkeypatch.setenv('DASHSCOPE_API_KEY','test-not-a-real-secret')
    monkeypatch.setenv('BAILIAN_BASE_URL','https://example.test/compatible-mode/v1')
    assert model.complete('role','hello')=='连接成功'
    assert str(seen[0].url)=='https://example.test/compatible-mode/v1/chat/completions'
    assert json.loads(seen[0].content)['messages'][1]['content']=='hello'
    from app import execution_context
    token=execution_context.thinking.set(False)
    try:
        assert model.complete('role','summary')=='连接成功'
        assert json.loads(seen[-1].content)['enable_thinking'] is False
    finally:execution_context.thinking.reset(token)


def test_memory_used_in_new_product_tasks(client):
    client.put('/api/memory/preferences',json={'market':'美国','brand_style':'极简','revision':0})
    task_id=create(client)
    assert client.get('/api/tasks/'+task_id).json()['preferences']['brand_style']=='极简'
    client.put('/api/memory/preferences',json={'market':'英国','revision':1})
    assert client.get('/api/tasks/'+task_id).json()['preferences']['market']=='美国'


def test_crash_after_checkpoint_does_not_regenerate_copy(client,monkeypatch):
    task_id=create(client)
    calls=[]
    monkeypatch.setattr(model,'complete',lambda *args:calls.append(1) or '已保存文案')
    real_finish=store.finish
    monkeypatch.setattr(store,'finish',lambda *args:False)
    process_one() # Simulate process loss between graph checkpoint and queue completion.
    with store.db() as conn:
        conn.execute(store.jobs.update().where(store.jobs.c.id==task_id).values(lease=time.time()-1))
    monkeypatch.setattr(store,'finish',real_finish)
    process_one()
    t=client.get('/api/tasks/'+task_id).json()
    assert t['status']=='awaiting_review'
    assert t['copy']=='已保存文案'
    assert calls==[1]


def test_legacy_migration_preserves_source(client,tmp_path):
    import sqlite3
    from scripts.migrate_legacy import migrate
    path=tmp_path/'legacy.sqlite'
    t={'id':'old-task','version':1,'status':'awaiting_review','events':[],
       'product':{'name':'Cup','sku':'C1','cost':7,'stock':10},'target_margin':.3}
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE tasks (payload TEXT)')
        conn.execute('INSERT INTO tasks VALUES (?)',(json.dumps(t),))
    migrate(str(path))
    migrate(str(path))
    assert len(client.get('/api/tasks').json())==1
    process_one()
    assert client.get('/api/tasks/old-task').json()['status']=='awaiting_review'
    with sqlite3.connect(path) as conn:
        assert json.loads(conn.execute('SELECT payload FROM tasks').fetchone()[0])==t
