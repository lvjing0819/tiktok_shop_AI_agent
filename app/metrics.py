"""Shared metric definitions and deterministic, coverage-aware comparisons."""
import json
from pathlib import Path
from decimal import Decimal, InvalidOperation

CATALOG = json.loads(Path(__file__).with_name('metric_definitions.json').read_text())


def coverage_state(entry):
    state=entry.get('status','missing');attempt=entry.get('last_attempt') or {}
    if state=='missing' and attempt.get('status')=='failed':
        return 'unavailable_range' if attempt.get('platform_code')==28001022 else 'failed'
    return state


def number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def compare(current, baseline, *, current_status='complete', baseline_status='complete', kind='relative', same_length=True):
    a, b = number(current), number(baseline)
    result = {'current': str(a) if a is not None else None, 'baseline': str(b) if b is not None else None,
              'change': None, 'unit': '百分点' if kind == 'percentage_points' else '%'}
    def unavailable(status, reason):
        return {**result, 'status': status, 'reason': reason}
    if kind == 'none':
        return unavailable('not_applicable', '该指标不按百分比比较')
    if not same_length:
        return unavailable('different_length', '闰年导致同期天数不同，未计算增长率')
    if current_status == 'missing_field':
        return unavailable('missing_value', '本期字段未返回或分母为0')
    if current_status == 'failed':
        return unavailable('failed_current', '本期来源接口读取失败，详见数据来源状态')
    if current_status == 'unavailable_range':
        return unavailable('unavailable_range', '平台拒绝该本期日期范围（28001022）')
    if current_status != 'complete':
        return unavailable('partial_current' if current_status.startswith('partial') else 'missing_current', '本期数据或明细不完整' if current_status.startswith('partial') else '本期数据未同步或字段未返回')
    if baseline_status != 'complete':
        if baseline_status == 'missing_field':return unavailable('missing_baseline_field', '基期字段未返回或分母为0')
        if baseline_status == 'failed':return unavailable('failed_baseline', '基期来源接口读取失败，详见数据来源状态')
        if baseline_status == 'unavailable_range':return unavailable('unavailable_range', '平台拒绝该基期日期范围（28001022），未生成增长率')
        return unavailable('partial_baseline' if baseline_status.startswith('partial') else 'missing_baseline', '基期数据或明细不完整' if baseline_status.startswith('partial') else '基期数据未同步或字段未返回')
    if a is None or b is None:
        return unavailable('missing_value', '金额或分母未返回，不能按0计算')
    if kind == 'percentage_points':
        change = a-b
    else:
        if b == 0:
            return unavailable('both_zero' if a == 0 else 'zero_baseline', '本期与基期均为0' if a == 0 else '基期为0，增长率无定义')
        if kind == 'magnitude':
            if a*b < 0:
                return unavailable('direction_changed', '金额正负方向变化，不比较规模增长率')
            a, b = abs(a), abs(b)
        change = (a-b)/abs(b)*100
    return {**result, 'status': 'ok', 'reason': '', 'change': float(change)}


def value_map(period):
    """Stable keys align dimensions across periods; absent != unknown."""
    out = {}; coverage = period['coverage']
    def put(key, definition, value, label=None, status=None, zero_absent=False):
        spec = CATALOG['metrics'][definition]
        source = spec['source']
        source_coverage = coverage.get(source, {})
        state = status or coverage_state(source_coverage)
        out[key] = {'definition': definition, 'label': label or spec['label'], 'source': source,
                    'unit': spec['unit'], 'value': str(value) if number(value) is not None else None,
                    'status': state if value is not None else 'missing_field' if state == 'complete' else state,
                    'zero_when_absent': zero_absent}
    orders = period.get('orders'); finance = period.get('finance'); affiliate = period.get('affiliate'); performance = period.get('performance')
    for key in ('total', 'delivered', 'in_transit', 'cancelled', 'paid_non_cancelled'):
        put('orders.'+key, 'orders.'+key, orders.get(key) if orders else None)
    for key, numerator in (('delivered_rate', 'delivered'), ('cancelled_rate', 'cancelled')):
        put('orders.'+key, 'orders.'+key, number(orders[numerator])/orders['total']*100 if orders and orders['total'] else None)
    for key in ('net_settlement', 'revenue', 'fees', 'paid_statement_amount', 'transaction_net', 'statement_transaction_difference', 'statement_count', 'order_count', 'transaction_count', 'unknown_net_orders'):
        put('finance.'+key, 'finance.'+key, finance.get(key) if finance else None)
    for key, value in (('adjustment_count', len(finance.get('adjustments', [])) if finance else None), ('expense_field_count', len(finance.get('expenses', [])) if finance else None)):
        put('finance.'+key, 'finance.'+key, value)
    for part in ('positive', 'zero', 'negative'):
        for field in ('orders', 'amount', 'share'):
            key = f'finance.{part}.{field}'
            value = (finance[part][field] if field != 'share' else number(finance[part]['orders'])/finance['order_count']*100 if finance['order_count'] else None) if finance else None
            put(key, key, value)
    for key in ('affiliate_orders', 'matched_orders', 'unclassified_orders', 'actual_std', 'actual_ads', 'actual_total'):
        value = affiliate.get(key) if affiliate and key != 'actual_total' else None
        if affiliate and key == 'actual_total':
            values = [number(affiliate.get(k)) for k in ('actual_std', 'actual_ads')]
            value = sum(v for v in values if v is not None) if any(v is not None for v in values) else None
        state = None
        if key in ('matched_orders', 'unclassified_orders') and coverage['orders']['status'] != 'complete':
            state = 'partial_detail'
        if affiliate and key.startswith('actual_') and not affiliate.get('actual_fields_complete', False):
            state = 'partial_detail'
        put('affiliate.'+key, 'affiliate.'+key, value, status=state)
    for key in ('gmv', 'avg_order_value', 'paid_orders'):
        put('performance.'+key, 'performance.'+key, performance.get(key) if performance else None)
    for row in (finance or {}).get('expense_values', (finance or {}).get('expenses', [])):
        for field in ('amount', 'has_orders', 'no_orders', 'unknown_orders', 'share_pct'):
            state = 'partial_detail' if row.get('unknown_orders') and field in ('amount', 'share_pct') else None
            put(f"expense|{row['id']}|{field}", 'expense.'+field, row.get(field), row.get('name', row['id'])+' · '+CATALOG['metrics']['expense.'+field]['label'], status=state)
    for row in (orders or {}).get('skus', []):
        put(f"sku|{row['id']}|units", 'sku.units', row['units'], row['sku']+' · 件数', zero_absent=True)
        put(f"sku|{row['id']}|product_video_count", 'sku.product_video_count', row.get('product_video_count'), row['sku']+' · 商品关联视频', zero_absent=False)
    creator_fields = ('orders', 'units', 'actual_base', 'actual_std', 'actual_ads', 'actual_total', 'estimated_std', 'estimated_ads', 'settled_lines', 'lines')
    for row in (affiliate or {}).get('creators', []):
        for field in creator_fields:
            value = row.get(field); state = None
            if field == 'actual_total':
                values = [number(row.get(k)) for k in ('actual_std', 'actual_ads')]
                value = sum(v for v in values if v is not None) if any(v is not None for v in values) else None
            if field.startswith('actual_'):
                known = row.get(field+'_known_lines', 0)
                if field == 'actual_total':
                    known = min(row.get('actual_std_known_lines', 0), row.get('actual_ads_known_lines', 0))
                if known < row['settled_lines']:state = 'partial_detail'
            if field.startswith('estimated_') and row.get(field+'_known_lines', 0) < row['lines']:
                state = 'partial_detail'
            put(f"creator|{row['name']}|{field}", 'creator.'+field, value, '@'+row['name']+' · '+CATALOG['metrics']['creator.'+field]['label'], status=state, zero_absent=True)
    for dimension in ('regions', 'statuses', 'cancellation_reasons'):
        for row in (orders or {}).get(dimension, []):
            put(f"dimension|{dimension}|{row['label']}", 'dimension.orders', row['value'], row['label'], zero_absent=True)
    for hour, value in enumerate((orders or {}).get('hours', [])):
        put(f'dimension|hours|{hour}', 'dimension.orders', value, f'{hour}:00', zero_absent=True)
    for row in (performance or {}).get('channels', []):
        put('channel|'+row['label'], 'dimension.channel_gmv', row['value'], row['label'])
    for row in (finance or {}).get('statement_statuses', []):
        put('statement_status|'+row['label'], 'finance.statement_status', row['value'], row['label'], zero_absent=True)
    return out


def build_comparisons(current, previous, year_ago, *, same_year_length=True):
    maps = [value_map(p) for p in (current, previous, year_ago)]
    visible_fees = {row['id'] for p in (current,previous,year_ago) for row in (p.get('finance') or {}).get('expenses',[])}
    keys = dict.fromkeys(k for values in maps for k in values if not k.startswith('expense|') or k.split('|')[1] in visible_fees)
    result = {}
    for key in keys:
        template = next(values[key] for values in maps if key in values)
        def resolve(index):
            if key in maps[index]:return maps[index][key]
            state = coverage_state((current, previous, year_ago)[index]['coverage'].get(template['source'], {}))
            return {**template, 'value': '0' if template['zero_when_absent'] and state == 'complete' else None, 'status': state}
        a, b, y = [resolve(i) for i in range(3)]
        kind = CATALOG['metrics'][template['definition']]['comparison']
        result[key] = {'key': key, **{k: template[k] for k in ('definition', 'label', 'source', 'unit')}, 'value': a['value'],
                       'previous': compare(a['value'], b['value'], current_status=a['status'], baseline_status=b['status'], kind=kind),
                       'year_ago': compare(a['value'], y['value'], current_status=a['status'], baseline_status=y['status'], kind=kind, same_length=same_year_length)}
    return result
