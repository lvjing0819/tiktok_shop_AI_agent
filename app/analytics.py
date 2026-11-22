"""Deterministic dashboard metrics; all money stays decimal and currency scoped."""
import csv
import io
import json
import time
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
from app import analytics_sources as sources, store

STATUS_NAMES = {'UNPAID': '未支付', 'ON_HOLD': '暂挂', 'AWAITING_SHIPMENT': '待发货', 'PARTIALLY_SHIPPING': '部分发货', 'AWAITING_COLLECTION': '待揽收', 'IN_TRANSIT': '运输中', 'DELIVERED': '已签收', 'COMPLETED': '已完成', 'CANCELLED': '已取消'}
FEE_NAMES = {'actual_shipping_fee_amount': '实际物流运费', 'platform_commission_amount': '平台佣金', 'transaction_fee_amount': '支付交易费', 'affiliate_commission_amount': '达人标准佣金', 'affiliate_ads_commission_amount': '达人广告佣金', 'affiliate_partner_commission_amount': '联盟合作伙伴佣金', 'sst_amount': '销售与服务税 SST', 'return_shipping_fee_amount': '退货运费', 'customer_paid_shipping_fee_amount': '买家承担运费', 'shipping_fee_discount_amount': '平台物流补贴', 'sfp_service_fee_amount': '免运计划服务费', 'seller_self_shipping_service_fee_amount': '卖家自发货服务费', 'gmv_max_ad_fee_amount': 'GMV Max 平台扣费', 'bonus_cashback_service_fee_amount': '返现计划服务费', 'vn_fix_infrastructure_fee': '平台基础设施费（平台字段名）', 'affiliate_commission_deposit': '达人佣金预留', 'affiliate_commission_release': '达人佣金预留释放', 'international_leg_logistics_amount': '国际段物流费', 'vat_amount': '增值税 VAT', 'pit_amount': '个人所得税 PIT'}


def decimal(v):
    try:
        d = Decimal(str(v))
        return d if d.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def money(values):
    values = [decimal(v) for v in values]
    return format(sum((v for v in values if v is not None), Decimal(0)), 'f') if values and any(v is not None for v in values) else None


def change(current, previous):
    a, b = decimal(current), decimal(previous)
    return float((a-b)/abs(b)*100) if a is not None and b not in (None, Decimal(0)) else None


def count_groups(values):
    return [{'label': k, 'value': v} for k, v in Counter(values).most_common()]


def expense_fields(transaction):
    # Only contributory leaves. supplementary_component and before-PIT are nested
    # explanations of other amounts, so summing them would double-count fees.
    rows = {}
    for section in ('fee', 'tax'):
        for name, value in (transaction.get('fee_tax_breakdown', {}).get(section) or {}).items():
            if name == 'affiliate_commission_amount_before_pit':
                continue
            if decimal(value) is not None:
                rows[f'{section}.{name}'] = str(value)
    for name, value in (transaction.get('shipping_cost_breakdown') or {}).items():
        if not isinstance(value, (dict, list)) and decimal(value) is not None:
            rows[f'shipping.{name}'] = str(value)
    return rows


def aggregate_orders(rows, shop):
    zone = ZoneInfo(shop['timezone'])
    paid = [r for r in rows if r.get('paid_time') and r.get('status') not in ('UNPAID', 'CANCELLED')]
    skus = {};hours = [0]*24
    for r in rows:
        if r.get('create_time'):
            hours[datetime.fromtimestamp(r['create_time'], zone).hour] += 1
    for r in paid:
        for line in r.get('line_items', []):
            if line.get('display_status') in ('CANCELLED', 'UNPAID'):
                continue
            identity = str(line.get('sku_id') or line.get('seller_sku') or 'UNKNOWN')
            sku = skus.setdefault(identity, {'id': identity, 'sku': line.get('seller_sku') or identity, 'name': line.get('product_name') or line.get('sku_name') or identity, 'product_id': line.get('product_id'), 'units': 0, 'order_ids': set()})
            sku['units'] += line.get('unit_count', 1);sku['order_ids'].add(r['id'])
    for sku in skus.values():
        sku['orders'] = len(sku.pop('order_ids'))
    currencies = sorted({r.get('payment', {}).get('currency') for r in paid if r.get('payment', {}).get('currency')})
    paid_amounts = {c: money(r['payment'].get('total_amount') for r in paid if r.get('payment', {}).get('currency') == c) for c in currencies}
    return {'total': len(rows), 'delivered': sum(r.get('status') in ('DELIVERED', 'COMPLETED') for r in rows),
            'in_transit': sum(r.get('status') in ('AWAITING_SHIPMENT', 'PARTIALLY_SHIPPING', 'AWAITING_COLLECTION', 'IN_TRANSIT', 'ON_HOLD') for r in rows),
            'cancelled': sum(r.get('status') == 'CANCELLED' for r in rows),
            'paid_non_cancelled': len(paid), 'paid_order_amounts': paid_amounts,
            'missing_payment_amount': sum(decimal(r.get('payment', {}).get('total_amount')) is None or not r.get('payment', {}).get('currency') for r in paid),
            'statuses': count_groups(STATUS_NAMES.get(r.get('status'), r.get('status') or '未知') for r in rows),
            'regions': count_groups(r.get('region') or '省/州未返回' for r in rows),
            'cancellation_reasons': count_groups(r.get('cancel_reason') or '平台未返回取消原因' for r in rows if r.get('status') == 'CANCELLED'),
            'hours': hours, 'skus': sorted(skus.values(), key=lambda r: -r['units']),
            'note': '订单按店铺当地创建日期筛选；已支付金额要求paid_time且剔除未支付/取消，未扣退款，包含运费与税，不等于平台GMV。SKU为件数，未扣售后退货。'}


def aggregate_finance(data, currency):
    # Stream selected rows once and parse each fee leaf once. Keep per-order
    # Decimal summaries instead of retaining whole transaction JSON in memory.
    grouped = {}; adjustments = []; transaction_count = 0
    transaction_sum = Decimal(0); net_complete = True; all_fields = set()
    for r in data.get('rows', []):
        if r.get('currency') != currency:
            continue
        transaction_count += 1
        net = decimal(r.get('settlement_amount'))
        net_complete = net_complete and net is not None
        if net is not None:transaction_sum += net
        if r.get('type') != 'ORDER':
            adjustments.append({'type': r.get('type'), 'order_id': r.get('adjustment_order_id'), 'amount': r.get('settlement_amount')})
            continue
        if not r.get('order_id'):
            continue
        group = grouped.setdefault(r['order_id'], {'count': 0, 'net': Decimal(0), 'known_net': 0,
                                                  'revenue': Decimal(0), 'known_revenue': 0, 'fields': {}})
        group['count'] += 1
        if net is not None:
            group['net'] += net; group['known_net'] += 1
        revenue = decimal(r.get('revenue_amount'))
        if revenue is not None:
            group['revenue'] += revenue; group['known_revenue'] += 1
        fields = expense_fields(r); all_fields.update(fields)
        for field, value in fields.items():
            total, known = group['fields'].get(field, (Decimal(0), 0))
            group['fields'][field] = (total+decimal(value), known+1)
    known = [{'order_id': id, 'net': format(g['net'], 'f') if g['known_net'] else None} for id, g in grouped.items()]
    def split(sign):
        rows = [r for r in known if decimal(r['net']) is not None and (1 if decimal(r['net']) > 0 else -1 if decimal(r['net']) < 0 else 0) == sign]
        return {'orders': len(rows), 'amount': money(r['net'] for r in rows) or '0'}
    expenses = []
    for field in sorted(all_fields):
        order_values = {}; has = []; no = []
        for id, group in grouped.items():
            total, count = group['fields'].get(field, (Decimal(0), 0))
            # A field is known only when returned for every transaction of an order.
            if count != group['count']:
                continue
            value = format(total, 'f'); order_values[id] = value
            (has if total != 0 else no).append(id)
        amount = money(order_values.values())
        if amount is None or decimal(amount) == 0:
            continue
        revenues = [format(grouped[id]['revenue'], 'f') if grouped[id]['known_revenue'] else None for id in has]
        base = money(revenues) if all(v is not None for v in revenues) else None
        expenses.append({'id': field, 'name': FEE_NAMES.get(field.split('.')[-1], field.split('.')[-1].replace('_', ' ')), 'amount': amount,
                         'has_orders': len(has), 'no_orders': len(no), 'unknown_orders': len(grouped)-len(has)-len(no),
                         'base_revenue': base, 'share_pct': float(abs(decimal(amount))/decimal(base)*100) if decimal(base) not in (None, Decimal(0)) else None,
                         'order_ids': has, 'zero_order_ids': no})
    statements = [r for r in data.get('statements', []) if r.get('currency') == currency]
    statement_sum = money(r.get('settlement_amount') for r in statements)
    transaction_sum = format(transaction_sum, 'f') if transaction_count and net_complete else None
    difference = format(decimal(statement_sum)-decimal(transaction_sum), 'f') if decimal(statement_sum) is not None and decimal(transaction_sum) is not None else None
    return {'statement_count': len(statements), 'order_count': len(grouped), 'transaction_count': transaction_count, 'transaction_net': transaction_sum, 'statement_transaction_difference': difference,
            'net_settlement': money(r.get('settlement_amount') for r in statements) or ('0' if not statements else None),
            'revenue': money(r.get('revenue_amount') for r in statements) or ('0' if not statements else None),
            'fees': money(r.get('fee_amount') for r in statements) or ('0' if not statements else None),
            'paid_statement_amount': money(r.get('settlement_amount') for r in statements if r.get('payment_status') == 'PAID') or '0',
            'statement_statuses': count_groups(r.get('payment_status') or 'UNKNOWN' for r in statements),
            'positive': split(1), 'zero': split(0), 'negative': split(-1), 'expenses': sorted(expenses, key=lambda r: -abs(decimal(r['amount']))),
            'adjustments': adjustments,
            'unknown_net_orders': sum(r['net'] is None for r in known), 'note': '按UTC账单生成日期筛选，订单可能创建于更早日期。正/零/负是本期订单结算流水净额，不是银行逐单到账；没有流水不能认定为未回款。费用保留平台原始正负号，正数可能是退费/补贴。'}


def affiliate_amount(line, field, currency):
    obj = line.get(field) or {}
    return obj.get('amount') if obj.get('currency') == currency and decimal(obj.get('amount')) is not None else None


def aggregate_affiliate(data, currency, order_ids):
    creators = {}; matched = set(); all_ids = set()
    for order in data.get('rows', []):
        all_ids.add(order['id'])
        if order['id'] in order_ids:
            matched.add(order['id'])
        for sku in order.get('skus', []):
            name = sku.get('creator_username') or '平台未返回达人名'
            c = creators.setdefault(name, {'name': name, 'order_ids': set(), 'units': 0, 'actual_bases': [], 'actual_std': [], 'actual_ads': [], 'estimated_std': [], 'estimated_ads': [], 'settled_lines': 0, 'lines': 0})
            c['order_ids'].add(order['id']);c['units'] += sku.get('quantity', 0);c['lines'] += 1
            if sku.get('settlement_status') == 'SETTLED':
                c['settled_lines'] += 1
                c['actual_bases'].append(affiliate_amount(sku, 'actual_commission_base', currency))
                c['actual_std'].append(affiliate_amount(sku, 'actual_paid_commission', currency))
                c['actual_ads'].append(affiliate_amount(sku, 'actual_paid_shop_ads_commission', currency))
            c['estimated_std'].append(affiliate_amount(sku, 'estimated_paid_commission', currency))
            c['estimated_ads'].append(affiliate_amount(sku, 'estimated_paid_shop_ads_commission', currency))
    result = []
    for c in creators.values():
        result.append({'name': c['name'], 'orders': len(c['order_ids']), 'units': c['units'], 'actual_base': money(c['actual_bases']), 'actual_std': money(c['actual_std']), 'actual_ads': money(c['actual_ads']),
                       'estimated_std': money(c['estimated_std']), 'estimated_ads': money(c['estimated_ads']), 'settled_lines': c['settled_lines'], 'lines': c['lines'],
                       'actual_std_known_lines': sum(v is not None for v in c['actual_std']), 'actual_ads_known_lines': sum(v is not None for v in c['actual_ads'])})
    return {'affiliate_orders': len(all_ids), 'matched_orders': len(matched), 'unclassified_orders': len(order_ids-matched),
            'actual_std': money(c['actual_std'] for c in result), 'actual_ads': money(c['actual_ads'] for c in result),
            'creators': sorted(result, key=lambda c: -c['orders']),
            'note': '联盟接口仅返回有佣金资格的订单，未匹配订单保持未分类，不等于自营。实际佣金仅汇总SETTLED且明确返回金额的行；空对象为未知，预估佣金另列。达人订单跨SKU/达人可能重叠，不可直接求和为总订单。'}


def aggregate_performance(data, currency):
    intervals = (data.get('performance') or {}).get('intervals') or []
    rows = [];gmv = [];orders = [];aov = []
    for interval in intervals:
        sales = interval.get('sales') or {}
        value = sales.get('avg_order_value') or {}
        if value.get('currency') == currency:aov.append(value.get('amount'))
        for name, field in [('视频归因GMV', 'video_attributed_gmv'), ('直播归因GMV', 'live_attributed_gmv'), ('商品卡归因GMV', 'product_card_attributed_gmv')]:
            obj = sales.get(field) or {}
            if obj.get('currency') == currency and decimal(obj.get('amount')) is not None:
                rows.append({'label': name, 'value': obj['amount']})
        obj = sales.get('gmv') or {}
        if obj.get('currency') == currency:
            gmv.append(obj.get('amount'))
        if isinstance(sales.get('orders_count'), int):
            orders.append(sales['orders_count'])
    return {'channels': rows, 'avg_order_value': aov[0] if len(aov) == 1 else None, 'gmv': money(gmv), 'paid_orders': sum(orders) if orders else None, 'latest_available_date': data.get('latest_available_date'),
            'note': '平台GMV按支付期统计，包含取消和退款，与按创建日期筛选的订单金额口径不同。渠道为归因GMV，可能存在归因重叠，不当作成交单量，也不强制归一成100%。'}


def period(workspace, shop, window):
    snaps = {s: sources.snapshot(workspace, shop['id'], s, window, shop['timezone']) for s in sources.SOURCES}
    coverage = {s: {'status': 'complete' if snap and snap['complete'] else 'partial' if snap else 'missing', 'retrieved_at': snap.get('retrieved_at') if snap else None,
                    'evidence': snap.get('evidence', []) if snap else [], 'stale': bool(snap and time.time()-snap['retrieved_at'] > 600)} for s, snap in snaps.items()}
    for source, entry in coverage.items():
        snap = snaps[source]
        entry.update({k: snap[k] for k in ('storage', 'observed_days', 'total_days', 'missing_dates') if snap and k in snap})
        attempt = store.get(sources.key(workspace, shop['id'], source, window.start_date, window.end_date), 'analytics_attempt', workspace)
        entry['last_attempt'] = attempt
    def data(s):return snaps[s]['data'] if snaps[s] else None
    orders = data('orders'); finance = data('finance');affiliate = data('affiliate');performance = data('performance');videos = data('videos')
    result = {'coverage': coverage, 'orders': aggregate_orders(orders['rows'], shop) if orders is not None else None,
              'finance': aggregate_finance(finance, shop['currency']) if finance is not None else None,
              'affiliate': aggregate_affiliate(affiliate, shop['currency'], {r['id'] for r in (orders or {}).get('rows', [])}) if affiliate is not None else None,
              'performance': aggregate_performance(performance, shop['currency']) if performance is not None else None}
    video_index = defaultdict(set)
    for v in (videos or {}).get('rows', []):
        for p in v.get('products', []):
            id = str(p.get('id') or p.get('product_id') or '')
            if id:video_index[id].add(v['id'])
    if result['affiliate'] and orders is None:
        result['affiliate']['matched_orders'] = None
        result['affiliate']['unclassified_orders'] = None
    if result['orders']:
        for sku in result['orders']['skus']:
            sku['product_video_count'] = len(video_index.get(sku['product_id'], [])) if videos is not None else None
    return result


def report(workspace, window):
    selected = sources.selected_shops(workspace, window)
    result = []
    for s in selected:
        current = period(workspace, s, window);previous = period(workspace, s, window.previous())
        current_skus = {v['id']: v for v in (current.get('orders') or {}).get('skus', [])}
        previous_skus = {v['id']: v for v in (previous.get('orders') or {}).get('skus', [])}
        skus = []
        for id in dict.fromkeys([*current_skus, *previous_skus]):
            a = current_skus.get(id);b = previous_skus.get(id)
            curr = a['units'] if a else 0 if current['coverage']['orders']['status'] == 'complete' else None
            prev = b['units'] if b else 0 if previous['coverage']['orders']['status'] == 'complete' else None
            skus.append({**(a or b), 'product_video_count': a.get('product_video_count') if a else None, 'current_units': curr, 'previous_units': prev, 'change_pct': change(curr, prev), 'unit_delta': curr-prev if curr is not None and prev is not None else None})
        result.append({'shop': s, 'current': current, 'previous': previous, 'skus': sorted(skus, key=lambda v: -(v['current_units'] or 0))})
    return {'generated_at': time.time(), 'window': window.model_dump(mode='json'), 'previous_window': window.previous().model_dump(mode='json'), 'shops': result,
            'currency_policy': '每家店铺按原币展示，跨币种金额不相加', 'scope': 'exact cached source windows',
            'capabilities': {'ads_sku_attribution': False, 'logistics_exception_details': False, 'warehouse_api': False},
            'notes': ['数据源各自刷新，完整性和最后更新时间见coverage；今日平台分析数据可能尚未生成。', '广告账户已连接不代表已建立SKU与广告归因；当前不生成因果结论。']}


def drilldown(workspace, window, shop_id, kind, field=None, zero=False, page=1, page_size=50):
    if not any(s['id'] == shop_id for s in sources.selected_shops(workspace, window)):
        raise ValueError('店铺不在当前授权筛选内')
    source = 'finance' if kind in ('expense', 'settlement') else 'orders'
    snap = sources.snapshot(workspace, shop_id, source, window)
    rows = (snap or {}).get('data', {}).get('rows', [])
    if kind == 'expense':
        if not field or not re_field(field):raise ValueError('费用字段无效')
        groups = defaultdict(list)
        for r in rows:
            if r.get('type') == 'ORDER' and r.get('order_id'):groups[r['order_id']].append(r)
        rows = []
        for id, group in groups.items():
            values = [expense_fields(r).get(field) for r in group]
            if any(v is None for v in values):continue
            amount = money(values)
            if (decimal(amount) == 0) == zero:
                rows.append({'order_id': id, 'amount': amount, 'currency': group[0].get('currency'), 'revenue': money(r.get('revenue_amount') for r in group)})
    elif kind == 'sku':
        rows = [r for r in rows if any(str(v.get('sku_id')) == field for v in r.get('line_items', []))]
    elif kind not in ('orders', 'settlement'):
        raise ValueError('不支持的明细类型')
    start = (page-1)*page_size
    selected = rows.page(start, page_size) if hasattr(rows, 'page') else rows[start:start+page_size]
    return {'total': len(rows), 'page': page, 'page_size': page_size, 'rows': selected, 'coverage': 'missing' if not snap else 'complete' if snap['complete'] else 'partial'}


def re_field(value):
    import re
    return bool(re.fullmatch(r'(fee|tax|shipping)\.[a-z0-9_]{1,100}', value))


def export_csv(result):
    out = io.StringIO();writer = csv.writer(out)
    writer.writerow(['店铺', '市场', '币种', '开始日期', '结束日期', '模块', '指标', '数值', '完整性', '口径'])
    for row in result['shops']:
        s = row['shop'];p = row['current'];w = result['window']
        def add(source, metric, value, note):
            # Neutralize spreadsheet formulas in external product/shop labels.
            def safe(v):
                text = '' if v is None else str(v)
                return "'"+text if text.lstrip().startswith(('=', '+', '@')) or (text.lstrip().startswith('-') and decimal(text) is None) else text
            writer.writerow([safe(v) for v in [s['name'], s['region'], s['currency'], w['start_date'], w['end_date'], source, metric, value, p['coverage'][source]['status'], note]])
        if p['orders']:
            add('orders', '总订单数', p['orders']['total'], p['orders']['note'])
            for sku in row['skus']:add('orders', 'SKU件数 '+sku['sku'], sku['current_units'], '已支付非取消订单，未扣售后退货')
        if p['finance']:
            add('finance', '净结算', p['finance']['net_settlement'], p['finance']['note'])
            for expense in p['finance']['expenses']:add('finance', expense['id'], expense['amount'], '平台原始正负号；UTC账单生成日期')
        if p['performance']:
            for channel in p['performance']['channels']:add('performance', channel['label'], channel['value'], p['performance']['note'])
        if p['affiliate']:
            for c in p['affiliate']['creators']:
                for field in ('orders', 'actual_std', 'actual_ads'):add('affiliate', c['name']+' '+field, c[field], p['affiliate']['note'])
    return '\ufeff'+out.getvalue()
