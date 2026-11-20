"""Fixed Shop read endpoints, bounded pagination, and deterministic summaries."""
import json
import re
import time
import unicodedata
from collections import Counter
from datetime import date, datetime, time as day_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app import shop_api as api

ID = Annotated[str, Field(pattern=r"^[0-9]{1,40}$")]
PRODUCT_STATUSES = Literal['ALL', 'DRAFT', 'PENDING', 'FAILED', 'ACTIVATE', 'SELLER_DEACTIVATED', 'PLATFORM_DEACTIVATED', 'FREEZE', 'DELETED']
ORDER_STATUSES = Literal['UNPAID', 'ON_HOLD', 'AWAITING_SHIPMENT', 'PARTIALLY_SHIPPING', 'AWAITING_COLLECTION', 'IN_TRANSIT', 'DELIVERED', 'COMPLETED', 'CANCELLED']
PATHS = {
    'products': '/product/202502/products/search',
    'orders': '/order/202309/orders/search',
    'inventory': '/product/202309/inventory/search',
}
# These are market-derived defaults, not a timezone returned by TikTok.
MARKET_ZONES = {'MY': 'Asia/Kuala_Lumpur', 'TH': 'Asia/Bangkok', 'SG': 'Asia/Singapore', 'VN': 'Asia/Ho_Chi_Minh', 'PH': 'Asia/Manila', 'GB': 'Europe/London', 'JP': 'Asia/Tokyo'}


class BaseQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    shop_id: ID


class Pages(BaseQuery):
    page_size: int = Field(default=100, ge=1, le=100)
    max_pages: int = Field(default=5, ge=1, le=5)
    page_token: str = Field(default='', max_length=4096)


class ProductsQuery(Pages):
    status: PRODUCT_STATUSES = 'ALL'
    seller_skus: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(default_factory=list, max_length=100)


class OrdersQuery(Pages):
    start_date: date | None = None
    end_date: date | None = None
    relative_date: Literal['today', 'yesterday', 'day_before_yesterday'] | None = None
    timezone: str | None = Field(default=None, max_length=100)
    order_status: ORDER_STATUSES | None = None

    @model_validator(mode='after')
    def valid_dates(self):
        if self.relative_date and (self.start_date or self.end_date):
            raise ValueError('相对日期与明确日期不能同时填写')
        if not self.relative_date and not (self.start_date and self.end_date):
            raise ValueError('订单查询需要日期，例如昨天或明确的起止日期')
        if self.start_date and self.end_date:
            if not 0 <= (self.end_date - self.start_date).days < 31:
                raise ValueError('订单查询起止日期需为正序，单次最多31天')
        return self


class InventoryQuery(BaseQuery):
    product_ids: list[ID] = Field(default_factory=list, max_length=100)
    sku_ids: list[ID] = Field(default_factory=list, max_length=600)
    seller_skus: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(default_factory=list, max_length=100)
    max_pages: int = Field(default=5, ge=1, le=5)

    @model_validator(mode='after')
    def valid_ids(self):
        if sum(bool(x) for x in (self.product_ids, self.sku_ids, self.seller_skus)) > 1:
            raise ValueError('product_ids、sku_ids、seller_skus 请只填写一种')
        return self


def normalize(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())


async def discover(workspace, selector=None):
    result = await api.shops(workspace)
    rows = result['shops']
    candidates = []
    if selector:
        exact = [r for r in rows if str(r['id']) == selector or normalize(r.get('name') or '') == normalize(selector)]
        candidates = exact or [r for r in rows if normalize(selector) == normalize(r.get('region') or '') or normalize(selector) in normalize(r.get('name') or '')]
    elif len(rows) == 1:
        candidates = rows
    return result, candidates[0] if len(candidates) == 1 else None


def evidence(operation, result, arguments):
    return {
        'evidence_id': f'shop-{operation}-{time.time_ns()}',
        'tool': 'shop_' + operation,
        'arguments': arguments,
        'retrieved_at': time.time(),
        'result': {'isError': False, 'structuredContent': result},
    }


def public_shop(row):
    return {k: row.get(k) for k in ('id', 'name', 'region')}


async def post(workspace, selected, operation, body, query=None):
    """Only the three audited POST search endpoints are reachable here."""
    if operation not in PATHS:
        raise api.ShopError('未开放此 Shop 操作')
    cfg = api.config()
    encoded = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
    for attempt in range(2):
        tokens, generation = await api.access(workspace, cfg, force=bool(attempt))
        current = api.connection(workspace)
        if not current or current['generation'] != generation or not current.get('sealed'):
            raise api.ShopError('Shop 连接已变化，请重新查询')
        sealed = api.unseal(current['sealed'], cfg)
        row = next((s for s in sealed.get('shops', []) if s['id'] == selected['id']), None)
        if not row:
            raise api.ShopError('店铺不在当前授权列表，请重新读取店铺列表')
        params = {'app_key': cfg['app_key'], 'timestamp': str(int(time.time())), 'shop_cipher': row['cipher'], **(query or {})}
        params['sign'] = api.sign(PATHS[operation], params, encoded, cfg['app_secret'])
        try:
            value = await api.request_json(api.API_ROOT + PATHS[operation], method='POST', params=params, content=encoded, headers={'x-tts-access-token': tokens['access_token'], 'Content-Type': 'application/json'})
        except api.ShopAPIError as exc:
            if exc.code == 105002 and attempt == 0:
                continue
            raise
        # A result obtained during disconnect/re-auth must not be returned.
        with api.store.db() as conn:
            api.locked(conn, workspace, generation, cfg)
        return value


def list_field(data, field):
    rows = data.get(field)
    # Empty responses must explicitly return an empty list (or total_count=0).
    if rows is None and data.get('total_count') == 0:
        rows = []
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise api.ShopError(f'Shop 返回的 {field} 列表格式异常')
    return rows


def clean_sku(s):
    return {k: s[k] for k in ('id', 'seller_sku', 'price', 'inventory', 'status_info') if k in s}


def clean_product(r):
    return {**{k: r[k] for k in ('id', 'title', 'status', 'create_time', 'update_time') if k in r}, 'skus': [clean_sku(s) for s in r.get('skus', [])]}


def clean_order(r):
    # Operational reads do not expose buyer identities, messages, or addresses.
    return {**{k: r[k] for k in ('id', 'status', 'create_time', 'update_time', 'paid_time', 'is_cod') if k in r},
            'payment': {k: r['payment'][k] for k in ('currency', 'total_amount', 'sub_total', 'shipping_fee', 'tax') if k in (r.get('payment') or {})},
            'line_items': [{k: s[k] for k in ('id', 'product_id', 'product_name', 'sku_id', 'seller_sku', 'sku_name', 'sale_price', 'currency', 'display_status') if k in s} for s in r.get('line_items', [])]}


async def paged(workspace, selected, operation, body, request):
    field = 'products' if operation == 'products' else 'orders'
    token = request.page_token
    seen_tokens, seen_ids, rows, refs = set(), set(), [], []
    total = None
    warnings = []
    for _ in range(request.max_pages):
        query = {'page_size': str(request.page_size)}
        if token:
            if token in seen_tokens:
                warnings.append('平台重复返回分页游标，已停止翻页')
                break
            seen_tokens.add(token)
            query['page_token'] = token
        if operation == 'orders':
            query.update(sort_field='create_time', sort_order='ASC')
        value = await post(workspace, selected, operation, body, query)
        data = value['data']
        page = list_field(data, field)
        cleaned = []
        for r in page:
            if not isinstance(r.get('id'), str) or not r['id']:
                raise api.ShopError('Shop 列表缺少有效 ID')
            if r['id'] in seen_ids:
                warnings.append('分页间有重复记录，已去重；查询期间数据可能变化')
                continue
            seen_ids.add(r['id'])
            cleaned.append(clean_product(r) if operation == 'products' else clean_order(r))
        rows.extend(cleaned)
        count = data.get('total_count')
        if type(count) is int and count >= 0:
            if total is not None and count != total:
                warnings.append('分页期间平台总数变化，请稍后复核')
            total = count
        refs.append(evidence(operation, {'code': 0, 'request_id': value.get('request_id'), 'data': {field: cleaned, 'total_count': count}}, {'shop_id': selected['id'], 'filters': body, 'page': len(refs) + 1, 'page_size': request.page_size}))
        token = data.get('next_page_token') or ''
        if not isinstance(token, str) or len(token) > 4096:
            raise api.ShopError('Shop 分页游标格式异常')
        if not token:
            break
    complete = not token and not request.page_token and (total is None or len(rows) == total) and not warnings
    if total is not None and not token and len(rows) != total and not request.page_token:
        warnings.append('返回条数与平台总数不一致，不能视为完整结果')
    return {'rows': rows, 'total_count': total, 'retrieved_count': len(rows), 'pages_fetched': len(refs), 'complete': complete, 'next_page_token': token or None, 'warnings': list(dict.fromkeys(warnings)), 'evidence': refs}


def date_window(request, selected, now=None):
    zone_name = request.timezone or MARKET_ZONES.get(selected.get('region'))
    if not zone_name:
        raise api.ShopError('该市场没有唯一默认时区，请明确提供 IANA timezone')
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise api.ShopError('时区无效，请使用 Asia/Bangkok 等 IANA 时区') from None
    if request.relative_date:
        today = (now or datetime.now(timezone.utc)).astimezone(zone).date()
        offset = {'today': 0, 'yesterday': 1, 'day_before_yesterday': 2}[request.relative_date]
        start = end = today - timedelta(days=offset)
    else:
        start, end = request.start_date, request.end_date
    return {'start_date': start.isoformat(), 'end_date': end.isoformat(), 'timezone': zone_name,
            'timezone_source': 'user' if request.timezone else 'market_region_default',
            'create_time_ge': int(datetime.combine(start, day_time.min, zone).timestamp()),
            'create_time_lt': int(datetime.combine(end + timedelta(days=1), day_time.min, zone).timestamp()),
            'date_basis': 'order_create_time'}


def order_summary(rows):
    groups = {}
    missing_amounts = 0
    for r in rows:
        p = r.get('payment') or {}
        try:
            amount = Decimal(p['total_amount'])
            if not amount.is_finite() or not isinstance(p.get('currency'), str) or not p['currency']:
                raise ValueError()
        except (KeyError, InvalidOperation, ValueError, TypeError):
            missing_amounts += 1
            continue
        group = groups.setdefault(p['currency'], {'all_status_total_amount': Decimal(0), 'paid_non_cancelled_total_amount': Decimal(0), 'paid_non_cancelled_count': 0})
        group['all_status_total_amount'] += amount
        # COD and unconfirmed paid orders must not be counted as collected cash.
        if r.get('paid_time') and r.get('status') not in ('UNPAID', 'CANCELLED'):
            group['paid_non_cancelled_total_amount'] += amount
            group['paid_non_cancelled_count'] += 1
    return {'scope': 'retrieved_rows', 'status_counts': dict(Counter(r.get('status', 'UNKNOWN') for r in rows)),
            'payment_amounts_by_currency': {c: {k: format(v, 'f') if isinstance(v, Decimal) else v for k, v in g.items()} for c, g in groups.items()},
            'missing_payment_count': missing_amounts,
            'note': '金额来自 payment.total_amount（买家支付总额）；不是结算收入、利润或广告GMV。已支付口径要求 paid_time 且排除未支付与取消订单，含退款可能，未扣退款。'}


async def inventory(workspace, selected, request):
    refs, catalog, warnings = [], None, []
    ids = list(dict.fromkeys(request.sku_ids or request.product_ids))
    key = 'sku_ids' if request.sku_ids else 'product_ids'
    if not ids:
        product_filters = {'status': 'ALL'}
        if request.seller_skus:
            product_filters['seller_skus'] = request.seller_skus
        catalog = await paged(workspace, selected, 'products', product_filters, ProductsQuery(shop_id=selected['id'], max_pages=request.max_pages))
        refs.extend(catalog['evidence'])
        ids = [r['id'] for r in catalog['rows']]
        warnings.extend(catalog['warnings'])
    rows, seen_skus = [], set()
    for index in range(0, len(ids), 600 if key == 'sku_ids' else 100):
        batch = ids[index:index + (600 if key == 'sku_ids' else 100)]
        value = await post(workspace, selected, 'inventory', {key: batch})
        page = list_field(value['data'], 'inventory')
        cleaned = []
        for r in page:
            if not isinstance(r.get('product_id'), str) or not isinstance(r.get('skus'), list) or not r['skus']:
                raise api.ShopError('库存结果缺少有效商品或SKU信息，不能视为零库存')
            skus = []
            for s in r['skus']:
                if not isinstance(s, dict) or not isinstance(s.get('id'), str) or not s['id']:
                    raise api.ShopError('库存结果缺少有效SKU编号')
                sku_key = (r['product_id'], s['id'])
                if sku_key in seen_skus:
                    warnings.append('库存返回重复SKU，已去重，不能确认完整性')
                    continue
                seen_skus.add(sku_key)
                skus.append({k: s[k] for k in ('id', 'seller_sku', 'total_available_quantity', 'total_committed_quantity', 'warehouse_inventory') if k in s})
            cleaned.append({'product_id': r['product_id'], 'skus': skus})
        rows.extend(cleaned)
        refs.append(evidence('inventory', {'code': 0, 'request_id': value.get('request_id'), 'data': {'inventory': cleaned}}, {'shop_id': selected['id'], key: batch}))
    returned = {s.get('id') for r in rows for s in r['skus']} if key == 'sku_ids' else {r.get('product_id') for r in rows}
    missing = [i for i in ids if i not in returned]
    if missing:
        warnings.append('部分商品或SKU没有返回库存，不能当作零库存')
    available, committed = 0, 0
    unknown = 0
    for r in rows:
        for s in r['skus']:
            if type(s.get('total_available_quantity')) is int and type(s.get('total_committed_quantity')) is int:
                available += s['total_available_quantity']
                committed += s['total_committed_quantity']
            else:
                unknown += 1
    if unknown:
        warnings.append('部分SKU库存字段缺失，汇总仅含字段完整的SKU')
    return {'rows': rows, 'retrieved_count': len(rows), 'requested_count': len(ids), 'missing_ids': missing,
            'complete': (catalog['complete'] if catalog else True) and not missing and not unknown and not warnings,
            'catalog_complete': catalog['complete'] if catalog else None,
            'next_page_token': catalog['next_page_token'] if catalog else None,
            'summary': {'scope': 'retrieved_skus', 'sku_count': sum(len(r['skus']) for r in rows), 'available_quantity': available, 'committed_quantity': committed, 'unknown_sku_count': unknown},
            'warnings': warnings, 'evidence': refs}


async def execute(workspace, operation, request, selected=None):
    evidence_rows = []
    if selected is None:
        discovery, selected = await discover(workspace, request.shop_id)
        evidence_rows.append(evidence('shops', {'code': 0, 'request_id': discovery.get('request_id'), 'data': {'shops': discovery['shops']}}, {}))
    if not selected or selected['id'] != request.shop_id:
        raise api.ShopError('所选店铺不在当前授权范围')
    filters = {}
    if operation == 'products':
        filters = {'status': request.status}
        if request.seller_skus:
            filters['seller_skus'] = request.seller_skus
        result = await paged(workspace, selected, operation, filters, request)
    elif operation == 'orders':
        window = date_window(request, selected)
        filters = {k: window[k] for k in ('create_time_ge', 'create_time_lt')}
        if request.order_status:
            filters['order_status'] = request.order_status
        result = await paged(workspace, selected, operation, filters, request)
        result.update(date_window=window, summary=order_summary(result['rows']))
        if result['summary']['missing_payment_count']:
            result['warnings'].append('部分订单缺少有效金额或币种，金额汇总不包含这些订单')
    elif operation == 'inventory':
        result = await inventory(workspace, selected, request)
    else:
        raise api.ShopError('未开放此 Shop 操作')
    result['evidence'] = evidence_rows + result['evidence']
    return {'status': 'completed', 'data_verified': True, 'read_only': True, 'operation': operation,
            'shop': public_shop(selected), 'retrieved_at': time.time(), **result}


def cell(value):
    return str(value if value is not None else '未返回').replace('|', '\\|').replace('\n', ' ')[:250]


def render(result):
    op = result['operation']
    rows = result['rows']
    label = {'products': '商品', 'orders': '订单', 'inventory': '库存'}[op]
    lines = [f"{label}查询：{result['shop']['name']}（{result['shop']['region']}）。"]
    if op == 'orders':
        w = result['date_window']
        lines.append(f"按订单创建时间：{w['start_date']} 至 {w['end_date']}；时区 {w['timezone']}" + ('（按店铺市场地区推定，可指定其他时区）' if w['timezone_source'] == 'market_region_default' else '') + '。')
    lines.append(f"本次读取 {result['retrieved_count']} 条；" + ('指定范围已完整读取。' if result['complete'] else '结果不完整，不能作为全店或整个日期范围的汇总。'))
    if op == 'products':
        lines += ['\n| 商品ID | 商品标题 | 状态 | SKU数 |', '|---|---|---|---|']
        lines += ['| ' + ' | '.join(map(cell, [r.get('id'), r.get('title'), r.get('status'), len(r['skus'])])) + ' |' for r in rows[:20]]
    elif op == 'orders':
        lines.append('状态分布（本次读取范围）：' + json.dumps(result['summary']['status_counts'], ensure_ascii=False))
        for currency, amounts in result['summary']['payment_amounts_by_currency'].items():
            lines.append(f"{currency}：所有状态订单 total_amount 合计 {amounts['all_status_total_amount']}；已支付且未取消订单合计 {amounts['paid_non_cancelled_total_amount']}（{amounts['paid_non_cancelled_count']} 单）。")
        lines.append(result['summary']['note'])
        lines += ['\n| 订单ID | 状态 | 买家支付总额 | 币种 |', '|---|---|---|---|']
        lines += ['| ' + ' | '.join(map(cell, [r.get('id'), r.get('status'), r['payment'].get('total_amount'), r['payment'].get('currency')])) + ' |' for r in rows[:20]]
    else:
        s = result['summary']
        lines.append(f"本次读取SKU {s['sku_count']} 个，可下单库存合计 {s['available_quantity']}，订单占用库存合计 {s['committed_quantity']}。")
        lines += ['\n| 商品ID | SKU | 可下单库存 | 订单占用库存 |', '|---|---|---|---|']
        flat = [(r, s) for r in rows for s in r['skus']]
        lines += ['| ' + ' | '.join(map(cell, [r.get('product_id'), s.get('seller_sku') or s.get('id'), s.get('total_available_quantity'), s.get('total_committed_quantity')])) + ' |' for r, s in flat[:20]]
    if (sum(len(r['skus']) for r in rows) if op == 'inventory' else len(rows)) > 20:
        lines.append('表格仅展示前20条，完整已读取结果保存在本轮证据记录。')
    lines.extend(result.get('warnings', []))
    return '\n\n'.join(lines[:3]) + '\n' + '\n'.join(lines[3:])
