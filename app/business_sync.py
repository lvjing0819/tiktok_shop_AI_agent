"""Opt-in order polling, independent of model conversations and task budgets."""
import asyncio
import hashlib
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pydantic import Field
from sqlalchemy import select
from app import business as b, store, shop_read


class SyncSettings(b.Input):
    shop_id: str = Field(pattern=r'^[0-9]{1,40}$')
    enabled: bool
    interval_seconds: int = Field(default=300, ge=60, le=86400)
    lookback_days: int = Field(default=7, ge=1, le=31)
    timezone: str = Field(default='Asia/Kuala_Lumpur', max_length=80)
    expected_revision: int = Field(ge=0)


def key(workspace, shop_id):
    return hashlib.sha256(json.dumps([workspace, shop_id]).encode()).hexdigest()


async def configure(workspace, body):
    try:ZoneInfo(body.timezone)
    except Exception:raise b.BusinessError('时区无效') from None
    _, selected=await shop_read.discover(workspace, body.shop_id)
    if not selected:raise b.BusinessError('请先选择当前工作区已授权的店铺')
    with store.db(write=True) as conn:
        k=key(workspace,body.shop_id)
        conn.execute(select(store.records.c.id).where(store.records.c.kind=='order_poll',store.records.c.id==k).with_for_update()).first()
        old=store.get(k,'order_poll',workspace,conn)
        if (old['revision'] if old else 0)!=body.expected_revision:raise b.BusinessError('同步设置版本已变化')
        item={**(old or {}),**body.model_dump(),'id':k,'revision':body.expected_revision+1,'next_at':time.time(),'updated_at':time.time()}
        return store.put(conn,'order_poll',item,workspace)


def process_one():
    now=time.time();selected=None
    with store.db(write=True) as conn:
        rows=conn.execute(select(store.records.c.payload,store.records.c.shop).where(store.records.c.kind=='order_poll').with_for_update(skip_locked=True)).all()
        for payload, workspace in rows:
            config=json.loads(payload)
            if config['enabled'] and config.get('next_at',0)<=now and config.get('lease_until',0)<=now:
                # A fixed maximum read duration plus lease prevents overlapping pollers.
                config['lease_until']=now+600
                config['next_at']=now+config['interval_seconds']
                store.put(conn,'order_poll',config,workspace);selected=(workspace,config);break
    if not selected:return False
    workspace,config=selected
    try:
        today=datetime.now(ZoneInfo(config['timezone'])).date()
        body=b.OrderSyncInput(shop_id=config['shop_id'],start_date=(today-timedelta(days=config['lookback_days']-1)).isoformat(),end_date=today.isoformat(),timezone=config['timezone'])
        async def bounded():
            async with asyncio.timeout(300):return await b.sync_orders(workspace,body)
        receipt=asyncio.run(bounded())
        status='completed' if receipt['complete'] else 'partial'
        result={'last_status':status,'last_success_at':time.time(),'last_receipt':receipt,'last_error':None}
    except Exception:
        # Never persist upstream error text, which may contain private requests.
        result={'last_status':'failed','last_error':'订单定时同步失败，请检查Shop连接、权限和同步日期范围。'}
    with store.db(write=True) as conn:
        current=store.get(config['id'],'order_poll',workspace,conn)
        if current and current.get('lease_until')==config['lease_until']:
            current.update(result,last_attempt_at=now,lease_until=0)
            current['next_at']=time.time()+current['interval_seconds']
            store.put(conn,'order_poll',current,workspace)
            from app import sync_monitor
            sync_monitor.record(conn,workspace,'order_poll',config['shop_id'],'orders',str(config['lease_until']),
                                'complete' if result['last_status']=='completed' else result['last_status'])
    return True
