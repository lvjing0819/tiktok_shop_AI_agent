import asyncio
import time
import pytest
from app import business as b, business_sync as sync, shop_read, store


@pytest.fixture
def config(tmp_path,monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'poll.sqlite'))
    async def discover(workspace,selector):return {},{'id':selector,'region':'TH'}
    monkeypatch.setattr(shop_read,'discover',discover)
    return sync.SyncSettings(shop_id='1',enabled=True,interval_seconds=60,lookback_days=3,timezone='Asia/Bangkok',expected_revision=0)


def test_polling_opt_in_lease_window_and_partial_status(config,monkeypatch):
    assert not sync.process_one()
    item=asyncio.run(sync.configure('default',config));calls=[]
    async def orders(workspace,body):
        calls.append(body)
        assert not sync.process_one() # Active lease blocks another worker.
        return {'complete':False,'retrieved_count':500}
    monkeypatch.setattr(b,'sync_orders',orders)
    assert sync.process_one() and len(calls)==1
    assert calls[0].timezone=='Asia/Bangkok'
    from datetime import date
    assert (date.fromisoformat(calls[0].end_date)-date.fromisoformat(calls[0].start_date)).days==2
    current=store.get(item['id'],'order_poll')
    assert current['last_status']=='partial' and current['lease_until']==0
    assert not sync.process_one()
    with pytest.raises(b.BusinessError):asyncio.run(sync.configure('default',config))


def test_failed_poll_does_not_fake_freshness_or_leak_tokens(config,monkeypatch):
    item=asyncio.run(sync.configure('default',config))
    async def fail(*args):raise RuntimeError('access_token=private-test')
    monkeypatch.setattr(b,'sync_orders',fail)
    assert sync.process_one()
    current=store.get(item['id'],'order_poll')
    assert current['last_status']=='failed' and not current.get('last_success_at')
    assert 'private-test' not in str(current)


def test_workspace_keys_and_disabled_polling(config):
    a=asyncio.run(sync.configure('one',config));c=asyncio.run(sync.configure('two',config))
    assert a['id']!=c['id']
    for workspace,item in [('one',a),('two',c)]:
        disabled=config.model_copy(update={'enabled':False,'expected_revision':1})
        asyncio.run(sync.configure(workspace,disabled))
    assert not sync.process_one()
