"""Durable per-shop/source failure streaks; dashboard alerts, no external sends."""
import hashlib
import json
import logging
import time
from uuid import uuid4
from sqlalchemy import select
from app import store

THRESHOLD = 10
LABELS = {'orders': '订单', 'finance': '结算与费用', 'affiliate': '联盟达人',
          'performance': '渠道GMV', 'videos': '商品关联视频'}
PIPELINES = {'analytics': '经营看板', 'order_poll': '订单中心'}
logger = logging.getLogger(__name__)


def identity(*values):
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def record(conn, workspace, pipeline, shop_id, source, attempt_id, status):
    """One completed logical attempt. Partial reads count as incomplete syncs.

    Persist the outcome, counter and alert in the caller's transaction so lease
    recovery cannot double count a committed result. Counters start on adoption.
    """
    if pipeline not in PIPELINES or source not in LABELS or status not in ('complete', 'partial', 'failed'):
        raise ValueError('Unknown sync outcome')
    key = identity(workspace, pipeline, shop_id, source)
    receipt_id = identity(key, attempt_id)
    if store.get(receipt_id, 'sync_outcome', workspace, conn):
        return
    conn.execute(select(store.records.c.id).where(store.records.c.kind == 'sync_health',
                 store.records.c.id == key, store.records.c.shop == workspace).with_for_update()).first()
    health = store.get(key, 'sync_health', workspace, conn) or {
        'id': key, 'pipeline': pipeline, 'shop_id': shop_id, 'source': source,
        'consecutive_failures': 0, 'active_alert_id': None,
    }
    now = time.time()
    health.update(last_attempt_at=now, last_status=status)
    alert = store.get(health.get('active_alert_id'), 'sync_alert', workspace, conn) if health.get('active_alert_id') else None
    if status == 'complete':
        health.update(consecutive_failures=0, first_failure_at=None, last_success_at=now, active_alert_id=None)
        if alert:
            alert.update(status='resolved', resolved_at=now, updated_at=now)
            store.put(conn, 'sync_alert', alert, workspace)
            logger.warning('sync_alert_resolved alert_id=%s', alert['id'])
    else:
        health['consecutive_failures'] += 1
        if health['consecutive_failures'] == 1:
            health['first_failure_at'] = now
        if health['consecutive_failures'] >= THRESHOLD:
            if not alert:
                alert = {'id': str(uuid4()), 'shop_id': shop_id, 'source': source,
                         'pipeline': pipeline, 'status': 'active', 'severity': 'error',
                         'threshold': THRESHOLD, 'opened_at': now,
                         'first_failure_at': health['first_failure_at']}
                health['active_alert_id'] = alert['id']
                logger.error('sync_alert_opened alert_id=%s pipeline=%s source=%s', alert['id'], pipeline, source)
            alert.update(consecutive_failures=health['consecutive_failures'], updated_at=now,
                         last_status=status, last_success_at=health.get('last_success_at'),
                         message=f"{PIPELINES[pipeline]} / {LABELS[source]}已连续{health['consecutive_failures']}次同步失败或未完整读取，请检查授权、网络和来源状态。")
            store.put(conn, 'sync_alert', alert, workspace)
    store.put(conn, 'sync_health', health, workspace)
    store.put(conn, 'sync_outcome', {'id': receipt_id, 'recorded_at': now}, workspace)


def record_job(conn, workspace, job, units):
    """A job counts once per current-period shop/source, not per gap or retry.

    Comparison/year-ago backfills are not current-source health checks. In
    particular unsupported historical analysis dates must not page operators.
    """
    if job.get('sync_health_recorded') or job['status'] not in ('completed', 'partial', 'failed'):
        return
    from app.analytics_sources import identity as unit_id
    errors = {unit_id(e) for e in job.get('errors', [])}
    partial = set(job.get('partial_sources', []))
    completed = set(job.get('completed', []))
    groups = {}
    for unit in units:
        if unit.get('period') == 'current':
            groups.setdefault((unit['shop_id'], unit['source']), []).append(unit_id(unit))
    for (shop_id, source), ids in groups.items():
        state = 'failed' if any(i in errors or i not in completed for i in ids) else 'partial' if any(i in partial for i in ids) else 'complete'
        record(conn, workspace, 'analytics', shop_id, source, job['id'], state)
    job['sync_health_recorded'] = True


def active(workspace, shop_ids=None):
    allowed = set(shop_ids) if shop_ids is not None else None
    alerts = []
    for health in store.list_records('sync_health', workspace):
        if allowed is not None and health['shop_id'] not in allowed:
            continue
        if health.get('active_alert_id'):
            alert = store.get(health['active_alert_id'], 'sync_alert', workspace)
            if alert and alert['status'] == 'active':
                alerts.append(alert)
    return sorted(alerts, key=lambda a: a['opened_at'], reverse=True)
