"""Lightweight local-ledger polling, independent of the selected calendar window."""
import hashlib
import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app import store, analytics_cache as cache


def settings():
    try:
        interval = int(os.getenv('ANALYTICS_SYNC_INTERVAL_SECONDS', '900'))
        lookback = int(os.getenv('ANALYTICS_SYNC_LOOKBACK_DAYS', '7'))
    except ValueError:
        interval, lookback = 900, 7
    return (0 if interval == 0 else max(300, interval), max(1, min(31, lookback)))


def register(workspace, shops):
    interval, _ = settings()
    if not interval:
        return
    with store.db(write=True) as conn:
        for shop in shops:
            identity = hashlib.sha256(json.dumps([workspace, shop['id']]).encode()).hexdigest()
            if not store.get(identity, 'analytics_poll', workspace, conn):
                store.put(conn, 'analytics_poll', {'id': identity, 'shop_id': shop['id'], 'next_at': time.time()+interval}, workspace)


def tick():
    """Enqueue bounded low-priority reads; active polls never overlap themselves."""
    from app import analytics_sources as sources
    interval, lookback = settings()
    if not interval:
        return
    with store.db() as conn:
        configs = [(json.loads(payload), workspace) for payload, workspace in conn.execute(
            select(store.records.c.payload, store.records.c.shop).where(store.records.c.kind == 'analytics_poll'))]
    migrated = set()
    for config, workspace in configs:
        selected = sources.shops(workspace)
        if workspace not in migrated:
            cache.migrate_legacy(workspace, selected, sources.Window); migrated.add(workspace)
        if config['next_at'] > time.time():
            continue
        shop = next((s for s in selected if s['id'] == config['shop_id']), None)
        if not shop or not shop['timezone']:
            continue
        with store.db(write=True) as conn:
            current = store.get(config['id'], 'analytics_poll', workspace, conn)
            if not current or current['next_at'] > time.time():
                continue
            previous = store.get(current.get('job_id', ''), 'analytics_job', workspace, conn)
            if previous and previous['status'] in ('queued', 'running'):
                continue
            if previous and previous.get('finished_at', 0)+interval > time.time():
                current['next_at'] = previous['finished_at']+interval
                store.put(conn, 'analytics_poll', current, workspace); continue
            # A short scheduling lease also covers multiple worker processes.
            current['next_at'] = time.time()+interval
            store.put(conn, 'analytics_poll', current, workspace)
        today = datetime.now(ZoneInfo(shop['timezone'])).date()
        window = sources.Window(start_date=today-timedelta(days=lookback-1), end_date=today, shop_id=shop['id'])
        job = sources.enqueue(workspace, window, source_names=cache.LOCAL_SOURCES, include_previous=False, priority='background')
        with store.db(write=True) as conn:
            current = store.get(config['id'], 'analytics_poll', workspace, conn)
            current['job_id'] = job['id']; store.put(conn, 'analytics_poll', current, workspace)
