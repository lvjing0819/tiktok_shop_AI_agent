import pytest
from fastapi.testclient import TestClient
from app.main import app

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path/'test.sqlite'))
    monkeypatch.setenv('MODEL_MODE', 'demo')
    return TestClient(app)


def create(client, stock=10):
    return client.post('/api/tasks', json={'product':{'name':'Cup','sku':'C1','cost':7,'stock':stock},'target_margin':.3}).json()['id']


def test_approval_and_idempotency(client):
    task_id=create(client)
    t=client.get('/api/tasks/'+task_id).json()
    assert t['status']=='awaiting_review'
    assert t['price']=='10.00'
    first=client.post(f'/api/tasks/{task_id}/review',json={'approved':True})
    second=client.post(f'/api/tasks/{task_id}/review',json={'approved':True})
    assert first.json()['listing_id']==second.json()['listing_id']


def test_stock_failure_blocks_publication(client):
    task_id=create(client,0)
    assert client.get('/api/tasks/'+task_id).json()['status']=='failed'
    assert client.post(f'/api/tasks/{task_id}/review',json={'approved':True}).status_code==409


def test_rejection_and_invalid_margin(client):
    task_id=create(client)
    assert client.post(f'/api/tasks/{task_id}/review',json={'approved':False}).json()['status']=='rejected'
    assert client.post(f'/api/tasks/{task_id}/review',json={'approved':True}).status_code==409
    assert client.post('/api/tasks',json={'product':{'name':'Cup','sku':'C1','cost':7,'stock':10},'target_margin':1}).status_code==422


def test_evidence_and_persistence(client):
    create(client)
    result=client.post('/api/analyze',json={'question':'利润如何？'}).json()
    assert result['mode']=='demo'
    assert len(result['evidence']['tasks'])==1
    assert len(TestClient(app).get('/api/tasks').json())==1
