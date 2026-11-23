"""Definitions/comparisons for saved-order, manual-cost and physical-stock pages."""
import hashlib
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from calendar import monthrange
from app import store, metrics

ZONE = timezone(timedelta(hours=8))


def groups(rows):
    result = {}
    for row in rows:
        currency = row['currency']
        if not currency:continue
        g = result.setdefault(currency, {key: Decimal(0) for key in ('all_status_order_amount','confirmed_paid_non_cancelled_amount','estimated_contribution_known_orders','settled_profit_known_orders')}
                              | {'cost_complete_orders': 0, 'settled_known_orders': 0})
        amount = metrics.number(row.get('total_amount'))
        if amount is not None:
            g['all_status_order_amount'] += amount
            if row.get('paid_time') and row['status'] not in ('UNPAID','CANCELLED'):
                g['confirmed_paid_non_cancelled_amount'] += amount
        for field, count in (('estimated_contribution','cost_complete_orders'),('settled_profit','settled_known_orders')):
            value = metrics.number(row.get(field))
            if value is not None:
                g[field+'_known_orders'] += value;g[count] += 1
    for g in result.values():
        if not g['cost_complete_orders']:g['estimated_contribution_known_orders'] = None
        if not g['settled_known_orders']:g['settled_profit_known_orders'] = None
    return {currency: {key:format(value,'f') if isinstance(value,Decimal) else value for key,value in g.items()} for currency,g in result.items()}


def inventory_values(view):
    rows = view['rows']; result = {}
    result['inventory.warehouse_count'] = {'value':len(view['warehouses']), 'status':'complete'}
    result['inventory.sku_count'] = {'value':view.get('sku_count', len({r['sku_id'] for r in rows})), 'status':'complete'}
    result['inventory.transfer_count'] = {'value':len(view.get('transfers', [])), 'status':'complete'}
    result['inventory.reservation_count'] = {'value':len(view.get('reservations', [])), 'status':'complete'}
    result['inventory.review_count'] = {'value':sum(r['on_hand'] is None or r['stale'] or r['over_reserved'] for r in rows), 'status':'complete'}
    for field in ('on_hand','warehouse_reserved','local_reserved','safety_stock','available'):
        known = [r[field] for r in rows if r[field] is not None]
        result['inventory.'+field] = {'value':sum(known) if known else None, 'status':'complete' if known and len(known)==len(rows) and not any(r.get('stale') for r in rows) else 'partial_detail' if known else 'missing'}
        for row in rows:
            result[f"inventory|{row['warehouse_id']}|{row['sku_id']}|{field}"] = {'value':row[field], 'status':'complete' if row[field] is not None and not row.get('stale') else 'partial_detail' if row[field] is not None else 'missing'}
    return result


def capture_point_metrics(workspace, values):
    today = datetime.now(ZONE).date()
    def key(day):return hashlib.sha256(f'{workspace}:{day}'.encode()).hexdigest()
    references = [store.get(key(today-timedelta(days=1)), 'business_metric_day', workspace),
                  store.get(key(today.replace(year=today.year-1,day=min(today.day, monthrange(today.year-1,today.month)[1]))), 'business_metric_day', workspace)]
    # At most once per minute; each day retains only the most recent sample.
    with store.db(write=True) as conn:
        current = store.get(key(today), 'business_metric_day', workspace, conn)
        if not current or time.time()-current['recorded_at'] >= 60:
            store.put(conn, 'business_metric_day', {'id':key(today), 'day':str(today), 'recorded_at':time.time(), 'values':values}, workspace)
    result = {}
    for name, value in values.items():
        result[name] = {}
        for label, reference in zip(('previous','year_ago'),references):
            baseline = (reference or {}).get('values', {}).get(name, {})
            result[name][label] = {**metrics.compare(value['value'],baseline.get('value'),current_status=value['status'],baseline_status=baseline.get('status','missing')), 'baseline_day':str(today-timedelta(days=1) if label=='previous' else today.replace(year=today.year-1,day=min(today.day,monthrange(today.year-1,today.month)[1]))), 'baseline_recorded_at':(reference or {}).get('recorded_at')}
            if not reference:result[name][label]['reason'] = '未留存对应日期的状态快照，不能还原历史库存/待办'
    return result


def decorate(workspace, result, *, days=None, shop_id='', search='', order_status=''):
    from app.analytics_sources import Window
    today = datetime.now(ZONE).date(); all_rows = result['orders']['rows']
    def selected(row):
        return (not shop_id or row['shop_id']==shop_id) and (not order_status or row['status']==order_status) and (not search or search.lower() in row['order_id'].lower())
    candidates = [r for r in all_rows if selected(r)]
    window = Window(start_date=today-timedelta(days=(days or 7)-1),end_date=today)
    periods = [window,window.previous(),window.year_ago()]
    cohorts = []
    for period in periods:
        lower, upper = (datetime.combine(period.start_date,datetime.min.time(),ZONE).timestamp(),datetime.combine(period.end_date+timedelta(days=1),datetime.min.time(),ZONE).timestamp())
        cohorts.append([r for r in candidates if r.get('create_time') and lower<=r['create_time']<upper])
    if days is None:cohorts[0] = candidates
    selected_ids = {shop_id} if shop_id else {r['shop_id'] for r in all_rows}
    def complete(period):
        lo=datetime.combine(period.start_date,datetime.min.time(),ZONE).timestamp();hi=datetime.combine(period.end_date+timedelta(days=1),datetime.min.time(),ZONE).timestamp()
        receipts = result['orders']['sync']
        return bool(selected_ids) and all(any(r['shop_id']==sid and r.get('complete') and r.get('window',{}).get('create_time_ge',float('inf'))<=lo and r.get('window',{}).get('create_time_lt',0)>=hi for r in receipts) for sid in selected_ids)
    states = ['complete' if complete(p) else 'partial_detail' for p in periods]
    scalar = lambda rows: {'business.orders':len(rows),'business.fulfillment':sum(r['status'] in ('AWAITING_SHIPMENT','PARTIALLY_SHIPPING','AWAITING_COLLECTION') for r in rows),'business.in_transit':sum(r['status']=='IN_TRANSIT' for r in rows),'business.delivered':sum(r['status'] in ('DELIVERED','COMPLETED') for r in rows)}
    values = [scalar(rows) for rows in cohorts]; comparisons = {}
    def pair(name, vals, state=states):
        return {label:metrics.compare(vals[0],vals[i],current_status=state[0],baseline_status=state[i],kind=metrics.CATALOG['metrics'][name]['comparison'] if days else 'none',same_length=i!=2 or (periods[0].end_date-periods[0].start_date).days==(periods[2].end_date-periods[2].start_date).days) for label,i in (('previous',1),('year_ago',2))}
    for name in values[0]:comparisons[name] = pair(name,[v[name] for v in values])
    finance = [groups(rows) for rows in cohorts]
    by_currency = {}
    for currency in set().union(*(set(g) for g in finance)):
        by_currency[currency] = {}
        for field in ('all_status_order_amount','confirmed_paid_non_cancelled_amount','estimated_contribution_known_orders','settled_profit_known_orders','cost_complete_orders','settled_known_orders'):
            key='business.'+field; vals=[g.get(currency,{}).get(field) for g in finance]
            state = states[:]
            if field.endswith('_known_orders'):
                count = 'cost_complete_orders' if field.startswith('estimated') else 'settled_known_orders'
                for i,rows in enumerate(cohorts):
                    if finance[i].get(currency,{}).get(count,0) != sum(r['currency']==currency for r in rows):state[i] = 'partial_detail'
            by_currency[currency][key] = pair(key,vals,state)
    # The overview chart always shows seven days, independent of the toolbar
    # interval. Compare each displayed day with day-7 and the same calendar day.
    chart = []
    for offset in range(6, -1, -1):
        day = today-timedelta(days=offset)
        day_windows = [Window(start_date=day,end_date=day), Window(start_date=day-timedelta(days=7),end_date=day-timedelta(days=7))]
        day_windows.append(day_windows[0].year_ago())
        counts = []
        for period in day_windows:
            counts.append(sum(bool(r.get('create_time')) and datetime.fromtimestamp(r['create_time'],ZONE).date()==period.start_date for r in candidates))
        chart.append({'date':str(day), 'count':counts[0], **{label:{**metrics.compare(counts[0],counts[i],current_status='complete' if complete(day_windows[0]) else 'partial_detail',baseline_status='complete' if complete(day_windows[i]) else 'partial_detail'), 'baseline_day':str(day_windows[i].start_date)} for label,i in (('previous',1),('year_ago',2))}})
    result['order_chart'] = chart
    inv = inventory_values(result['inventory'])
    runs = store.list_records('operation_run',workspace)
    inv['operations.backlog'] = {'value':sum(r['status'] in ('waiting_input','waiting_approval','failed','blocked') for r in runs),'status':'complete'}
    comparisons.update(capture_point_metrics(workspace,inv))
    if days:
        result['finance']['by_currency'] = finance[0]
    result.update(metric_comparisons=comparisons,metric_comparisons_by_currency=by_currency,
                  metric_context={'window':window.model_dump(mode='json') if days else None,'previous_window':window.previous().model_dump(mode='json') if days else None,
                                  'year_ago_window':window.year_ago().model_dump(mode='json') if days else None,'point_reference':'库存/待办：前一日最后记录；去年同日最后记录。'})
    return result
