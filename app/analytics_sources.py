"""Audited, read-only dashboard sources and durable refresh jobs.

Snapshots are keyed by workspace, shop, source and exact requested window.
An empty page, missing field, partial page and failed request remain distinct.
"""
import asyncio
import hashlib
import json
import re
import time
from datetime import date, datetime, time as midnight, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from app import store, shop_api as api, shop_read

SOURCES = ('orders', 'finance', 'affiliate', 'performance', 'videos')
CURRENCIES = {'MY': 'MYR', 'TH': 'THB', 'SG': 'SGD', 'ID': 'IDR', 'VN': 'VND', 'PH': 'PHP', 'US': 'USD', 'GB': 'GBP', 'JP': 'JPY'}


class Window(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start_date: date
    end_date: date
    site: str = Field(default='ALL', pattern=r'^(ALL|[A-Z]{2})$')
    shop_id: str = Field(default='ALL', pattern=r'^(ALL|[0-9]{1,40})$')

    @model_validator(mode='after')
    def dates(self):
        if not 0 <= (self.end_date - self.start_date).days < 31:
            raise ValueError('单次请选择1至31天，系统自动对比前一个等长周期')
        if self.start_date < date(2023, 7, 1) or self.end_date > datetime.now(timezone.utc).date() + timedelta(days=1):
            raise ValueError('日期须在2023-07-01至今天之间')
        return self

    def previous(self):
        days = (self.end_date - self.start_date).days + 1
        return self.model_copy(update={'start_date': self.start_date - timedelta(days=days), 'end_date': self.start_date - timedelta(days=1)})


def key(*values):
    return hashlib.sha256(json.dumps(values, default=str).encode()).hexdigest()


def shops(workspace):
    cfg = api.config()
    row = api.connection(workspace)
    if not row or not row.get('sealed'):
        return []
    return [{**shop_read.public_shop(s), 'timezone': shop_read.MARKET_ZONES.get(s.get('region')), 'currency': CURRENCIES.get(s.get('region'))}
            for s in api.unseal(row['sealed'], cfg).get('shops', [])]


def selected_shops(workspace, window):
    return [s for s in shops(workspace) if (window.site == 'ALL' or s['region'] == window.site) and (window.shop_id == 'ALL' or s['id'] == window.shop_id)]


def snapshot(workspace, shop_id, source, window):
    return store.get(key(workspace, shop_id, source, window.start_date, window.end_date), 'analytics_source', workspace)


def bounds(window, zone):
    tz = ZoneInfo(zone)
    return {'create_time_ge': int(datetime.combine(window.start_date, midnight.min, tz).timestamp()),
            'create_time_lt': int(datetime.combine(window.end_date + timedelta(days=1), midnight.min, tz).timestamp())}


async def request(workspace, shop_id, path, query, body=None):
    # Keep endpoint selection internal; never accept paths from callers/models.
    if not (path in ('/order/202309/orders/search', '/order/202507/orders', '/finance/202309/statements', '/affiliate_seller/202410/orders/search', '/analytics/202609/shop/performance', '/analytics/202605/shop_videos/performance')
            or re.fullmatch(r'/finance/202501/statements/[0-9]{1,40}/statement_transactions', path)):
        raise ValueError('未开放此数据源')
    cfg = api.config()
    encoded = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode() if body is not None else b''
    refresh_token = False
    refreshed = False
    for attempt in range(3):
        tokens, generation = await api.access(workspace, cfg, force=refresh_token)
        refresh_token = False
        current = api.connection(workspace)
        if not current or current['generation'] != generation or not current.get('sealed'):
            raise api.ShopError('连接发生变化，请重新同步')
        shop = next((s for s in api.unseal(current['sealed'], cfg).get('shops', []) if s['id'] == shop_id), None)
        if not shop:
            raise api.ShopError('店铺已不在授权列表')
        params = {'app_key': cfg['app_key'], 'timestamp': str(int(time.time())), 'shop_cipher': shop['cipher'], **query}
        params['sign'] = api.sign(path, params, encoded, cfg['app_secret'])
        try:
            result = await api.request_json(api.API_ROOT + path, method='POST' if body is not None else 'GET', params=params, content=encoded or None,
                                            headers={'x-tts-access-token': tokens['access_token'], 'Content-Type': 'application/json'})
        except api.ShopAPIError as exc:
            if exc.code == 105002 and not refreshed and attempt < 2:
                refresh_token = refreshed = True
                continue
            raise
        except api.ShopError as exc:
            message = str(exc)
            transient = message == 'TikTok Shop 网络或返回格式异常，请稍后重试' or bool(re.search(r'HTTP (429|500|502|503|504)\b', message))
            if transient and attempt < 2:
                await asyncio.sleep((5 if '429' in message else 2) * (attempt + 1))
                continue
            raise
        with store.db() as conn:
            api.locked(conn, workspace, generation, cfg)
        await asyncio.sleep(0.6)
        return result


def clean_details(order):
    cleaned = shop_read.clean_order(order)
    # Only province/state is retained. Never persist a name, street, phone or postal code.
    districts = (order.get('recipient_address') or {}).get('district_info') or []
    province = next((d.get('address_name') for d in districts if d.get('address_level') == 'L1'), None)
    cleaned['region'] = province if isinstance(province, str) and len(province) <= 150 else None
    for field in ('delivery_time', 'cancel_time', 'cancel_reason', 'cancellation_initiator'):
        if field in order:
            cleaned[field] = order[field]
    for target, original in zip(cleaned['line_items'], order.get('line_items', [])):
        # v202507 sub_item_info lists each physical unit once; no guessed quantities.
        sub = original.get('sub_item_info')
        target['unit_count'] = len(sub) if isinstance(sub, list) and sub else 1
        target['cancel_reason'] = original.get('cancel_reason')
    return cleaned


async def fetch_source(workspace, shop, source, window, heartbeat=lambda: None):
    refs = []
    async def call(path, query, body=None):
        heartbeat()
        value = await request(workspace, shop['id'], path, query, body)
        refs.append({'path': path, 'request_id': value.get('request_id'), 'retrieved_at': time.time()})
        return value['data']
    async def pages(path, field, query, body=None, limit=20):
        token = ''; seen_tokens = set(); rows = []; seen = set(); total = None; consistent = True
        for _ in range(limit):
            data = await call(path, {**query, **({'page_token': token} if token else {})}, body)
            page = data.get(field)
            if page is None and data.get('total_count') == 0:
                page = []
            if not isinstance(page, list):
                raise api.ShopError('数据源未返回有效列表，不能视为空结果')
            for row in page:
                identity = str(row.get('id'))
                # Affiliate API counts SKU orders, and the same order can span pages.
                if source == 'affiliate':
                    rows.append(row)
                elif identity not in seen:
                    seen.add(identity); rows.append(row)
                else:consistent = False
            count = data.get('total_count')
            if total is not None and count is not None and total != count:consistent = False
            if count is not None:total = count
            token = data.get('next_page_token') or ''
            if not token:
                expected = sum(len(r.get('skus', [])) for r in rows) if source == 'affiliate' else len(rows)
                return rows, consistent and (total is None or expected == total)
            if token in seen_tokens:
                return rows, False
            seen_tokens.add(token)
        return rows, False
    time_filter = bounds(window, shop['timezone'])
    date_filter = {'start_date_ge': window.start_date.isoformat(), 'end_date_lt': (window.end_date + timedelta(days=1)).isoformat(), 'currency': 'LOCAL'}
    complete = True
    if source == 'orders':
        rows, complete = await pages('/order/202309/orders/search', 'orders', {'page_size': '100', 'sort_field': 'create_time', 'sort_order': 'ASC'}, time_filter)
        details = {}
        for offset in range(0, len(rows), 50):
            d = await call('/order/202507/orders', {'ids': ','.join(r['id'] for r in rows[offset:offset+50])})
            for r in d.get('orders', []):
                details[r['id']] = clean_details(r)
        result = {'rows': [details.get(r['id'], shop_read.clean_order(r)) for r in rows], 'details_complete': len(details) == len(rows)}
        complete = complete and result['details_complete']
    elif source == 'finance':
        # The filter is statement generation date in UTC, explicitly exposed in UI/API.
        utc_filter = bounds(window, 'UTC')
        statements, complete = await pages('/finance/202309/statements', 'statements', {'statement_time_ge': utc_filter['create_time_ge'], 'statement_time_lt': utc_filter['create_time_lt'], 'page_size': '100', 'sort_field': 'statement_time', 'sort_order': 'ASC'})
        tx = []
        for st in statements:
            batch, ok = await pages(f"/finance/202501/statements/{st['id']}/statement_transactions", 'transactions', {'page_size': '100', 'sort_field': 'order_create_time', 'sort_order': 'ASC'})
            complete = complete and ok
            tx.extend({**r, 'statement_id': st['id'], 'statement_time': st['statement_time'], 'currency': st['currency'], 'payment_status': st.get('payment_status')} for r in batch)
        result = {'statements': statements, 'rows': tx, 'date_basis': 'statement_generated_date_UTC'}
    elif source == 'affiliate':
        rows, complete = await pages('/affiliate_seller/202410/orders/search', 'orders', {'page_size': '100'}, time_filter)
        # Merge repeated orders; deduplicate the actual attribution line, not just order ID.
        grouped = {}; seen = set()
        for r in rows:
            g = grouped.setdefault(r['id'], {**r, 'skus': []})
            for sku in r.get('skus', []):
                k = (r['id'], sku.get('sku_id'), sku.get('creator_username'), sku.get('content_id'))
                if k not in seen:
                    seen.add(k);g['skus'].append(sku)
        result = {'rows': list(grouped.values())}
    elif source == 'performance':
        result = await call('/analytics/202609/shop/performance', {**date_filter, 'granularity': 'ALL'})
    elif source == 'videos':
        rows, complete = await pages('/analytics/202605/shop_videos/performance', 'videos', {**date_filter, 'page_size': '100', 'sort_field': 'views', 'sort_order': 'DESC', 'account_type': 'ALL'})
        # Preserve metrics and IDs used by the dashboard only.
        result = {'rows': [{k: r[k] for k in ('id', 'title', 'views', 'gmv', 'products', 'creator', 'sku_orders', 'items_sold') if k in r} for r in rows]}
    else:
        raise ValueError('无效来源')
    return {'id': key(workspace, shop['id'], source, window.start_date, window.end_date), 'shop_id': shop['id'], 'source': source,
            'start_date': str(window.start_date), 'end_date': str(window.end_date), 'complete': complete, 'retrieved_at': time.time(),
            'schema_version': 1, 'evidence': refs, 'data': result}


def enqueue(workspace, window, *, only_missing=False):
    selected = selected_shops(workspace, window)
    if not selected:
        raise ValueError('没有符合筛选条件的已授权店铺')
    if any(not s['timezone'] for s in selected):
        raise ValueError('该店铺需要先配置明确时区')
    requested = [f"{s['id']}:{source}:{w.start_date}:{w.end_date}"
                 for s in selected for w in (window, window.previous()) for source in SOURCES
                 if not only_missing or snapshot(workspace, s['id'], source, w) is None]
    fingerprint = key(workspace, window.model_dump(mode='json'), 'missing') if only_missing else key(workspace, window.model_dump(mode='json'))
    with store.db(write=True) as conn:
        rows = conn.execute(select(store.records.c.payload).where(store.records.c.kind == 'analytics_job', store.records.c.shop == workspace).with_for_update()).all()
        for row in rows:
            job = json.loads(row[0])
            if job.get('fingerprint') == fingerprint and job['status'] in ('queued', 'running'):
                return public_job(job)
        job = {'id': str(uuid4()), 'fingerprint': fingerprint, 'window': window.model_dump(mode='json'), 'shop_ids': [s['id'] for s in selected], 'status': 'queued', 'created_at': time.time(), 'lease_until': 0, 'completed': [], 'errors': [], 'partial_sources': [], 'requested_sources': requested, 'total': len(requested)}
        store.put(conn, 'analytics_job', job, workspace)
    return public_job(job)


def public_job(job):
    return {k: job.get(k) for k in ('id', 'status', 'created_at', 'finished_at', 'progress', 'completed', 'errors', 'total', 'window', 'active_source', 'partial_sources')}


def process_one():
    claimed = None; now = time.time()
    with store.db(write=True) as conn:
        rows = conn.execute(select(store.records.c.payload, store.records.c.shop).where(store.records.c.kind == 'analytics_job').with_for_update(skip_locked=True)).all()
        for payload, workspace in rows:
            job = json.loads(payload)
            if job['status'] in ('queued', 'running') and job.get('lease_until', 0) < now:
                job.update(status='running', lease_until=now+120, lease_token=str(uuid4()))
                store.put(conn, 'analytics_job', job, workspace);claimed = workspace, job;break
    if not claimed:
        return False
    workspace, job = claimed
    def save():
        with store.db(write=True) as conn:
            current = store.get(job['id'], 'analytics_job', workspace, conn)
            if not current or current.get('lease_token') != job['lease_token']:
                raise ValueError('同步租约已变化')
            job['lease_until'] = time.time()+120
            store.put(conn, 'analytics_job', job, workspace)
    async def run():
        window = Window(**job['window'])
        selected = [s for s in shops(workspace) if s['id'] in job['shop_ids']]
        for s in selected:
            for w in (window, window.previous()):
                for source in SOURCES:
                    identity = f"{s['id']}:{source}:{w.start_date}:{w.end_date}"
                    if identity in job['completed'] or ('requested_sources' in job and identity not in job['requested_sources']):
                        continue
                    job['active_source'] = {'shop': s['name'], 'source': source, 'start_date': str(w.start_date), 'end_date': str(w.end_date)};save()
                    try:
                        async with asyncio.timeout(300):
                            result = await fetch_source(workspace, s, source, w, save)
                        with store.db(write=True) as conn:
                            current = store.get(job['id'], 'analytics_job', workspace, conn)
                            if not current or current.get('lease_token') != job['lease_token']:raise ValueError('同步租约已变化')
                            store.put(conn, 'analytics_source', result, workspace)
                            store.put(conn, 'analytics_attempt', {'id': result['id'], 'status': 'complete' if result['complete'] else 'partial', 'attempted_at': time.time()}, workspace)
                        job['completed'].append(identity)
                        if not result['complete']:job.setdefault('partial_sources', []).append(identity)
                    except (api.ShopError, TimeoutError, ValueError) as exc:
                        # Keep prior good snapshots. No raw HTTP errors/URLs or credentials in jobs.
                        job['errors'].append({'shop_id': s['id'], 'source': source, 'start_date': str(w.start_date), 'end_date': str(w.end_date), 'message': '同步失败，请检查授权、接口权限和网络后重试', 'platform_code': exc.code if isinstance(exc, api.ShopAPIError) else None})
                        with store.db(write=True) as conn:
                            current = store.get(job['id'], 'analytics_job', workspace, conn)
                            if not current or current.get('lease_token') != job['lease_token']:raise ValueError('同步租约已变化')
                            store.put(conn, 'analytics_attempt', {'id': key(workspace,s['id'],source,w.start_date,w.end_date), 'status': 'failed', 'attempted_at': time.time(), 'platform_code': exc.code if isinstance(exc, api.ShopAPIError) else None}, workspace)
                    job['progress'] = len(job['completed'])+len(job['errors']);save()
        job['status'] = 'partial' if job['errors'] or job.get('partial_sources') or len(job['completed']) != job['total'] else 'completed'
    try:
        asyncio.run(run())
    except Exception:
        job['status'] = 'failed'
    job.update(finished_at=time.time(), active_source=None)
    save()
    return True


def ensure(workspace, window):
    """Reuse an active refresh, or enqueue missing windows once; never fabricate data."""
    selected = selected_shops(workspace, window)
    if not selected:
        raise ValueError('没有符合筛选条件的已授权店铺')
    selected_ids = {s['id'] for s in selected}
    active = [j for j in store.list_records('analytics_job', workspace)
              if j['status'] in ('queued', 'running')
              and j['window']['start_date'] == str(window.start_date)
              and j['window']['end_date'] == str(window.end_date)
              and selected_ids.issubset(set(j['shop_ids']))]
    missing = []
    for shop in selected:
        for label, period in (('current', window), ('previous', window.previous())):
            for source in SOURCES:
                if snapshot(workspace, shop['id'], source, period) is None:
                    missing.append({'shop_id': shop['id'], 'source': source, 'period': label,
                                    'start_date': str(period.start_date), 'end_date': str(period.end_date)})
    if active:
        return {'job': public_job(min(active, key=lambda j: j['created_at'])), 'missing': missing, 'reason': 'running'}
    if not missing:
        return {'job': None, 'missing': [], 'reason': 'ready'}
    for entry in missing:
        attempt = store.get(key(workspace, entry['shop_id'], entry['source'], entry['start_date'], entry['end_date']), 'analytics_attempt', workspace)
        if attempt and attempt.get('status') == 'failed' and time.time() - attempt.get('attempted_at', 0) < 300:
            return {'job': None, 'missing': missing, 'reason': 'recent_failure', 'retry_after_seconds': max(1, int(300 - (time.time() - attempt['attempted_at'])))}
    return {'job': enqueue(workspace, window, only_missing=True), 'missing': missing, 'reason': 'enqueued'}
