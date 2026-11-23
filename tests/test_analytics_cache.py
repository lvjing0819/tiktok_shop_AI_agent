import asyncio
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pytest
from sqlalchemy import select, func
from app import analytics as a, analytics_cache as c, analytics_sources as s, analytics_sync as sync, store

SHOP = {'id': '1', 'name': 'MY', 'region': 'MY', 'currency': 'MYR', 'timezone': 'Asia/Kuala_Lumpur'}
W = s.Window(start_date='2026-11-14', end_date='2026-11-20', site='MY')


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path/'ledger.sqlite'))
    monkeypatch.setattr(s, 'shops', lambda workspace: [SHOP] if workspace == 'default' else [])


def stamp(day, zone=SHOP['timezone'], hour=12):
    return datetime.fromisoformat(day).replace(hour=hour, tzinfo=ZoneInfo(zone)).timestamp()


def order(id, day, **values):
    return {'id': id, 'create_time': stamp(day), 'status': 'IN_TRANSIT', 'paid_time': 1,
            'payment': {'currency': 'MYR', 'total_amount': '0.10'}, 'line_items': [], **values}


def write(source, window, data, complete=True, workspace='default', retrieved_at=None):
    snap = {'id': s.key(workspace, '1', source, window.start_date, window.end_date), 'source': source,
            'shop_id': '1', 'start_date': str(window.start_date), 'end_date': str(window.end_date),
            'complete': complete, 'retrieved_at': retrieved_at or time.time(), 'evidence': [], 'data': data}
    with store.db(write=True) as conn:
        c.store_snapshot(conn, workspace, SHOP, source, window, snap)
    return snap


def test_overlap_subranges_deduplicate_update_and_are_locally_queryable(monkeypatch):
    write('orders', W, {'rows': [order('a', '2026-11-15'), order('b', '2026-11-19')]})
    narrower = s.Window(start_date='2026-11-18', end_date='2026-11-20', site='MY')
    write('orders', narrower, {'rows': [order('b', '2026-11-19', status='DELIVERED')]})
    inside = s.Window(start_date='2026-11-19', end_date='2026-11-19', site='MY')
    result = a.report('default', inside)['shops'][0]['current']
    assert result['orders']['total'] == 1 and result['orders']['delivered'] == 1
    assert result['coverage']['orders']['status'] == 'complete'
    with store.db() as conn:
        assert conn.execute(select(func.count()).select_from(c.entities)).scalar_one() == 2
    persisted = store.get(s.key('default', '1', 'orders', W.start_date, W.end_date), 'analytics_source')
    assert 'rows' not in persisted['data']
    assert s.snapshot('other', '1', 'orders', inside, SHOP['timezone']) is None
    job = s.ensure('default', inside)['job']
    assert all(u['source'] != 'orders' or u['period']=='year_ago' for u in store.get(job['id'], 'analytics_job')['units'])
    # Ordinary drilldown performs LIMIT/OFFSET in SQL, not full-history loading.
    details = a.drilldown('default', W, '1', 'orders', None, False, 2, 1)
    assert details['total'] == 2 and details['rows'][0]['id'] == 'b'


def test_gaps_are_not_zero_and_only_missing_days_are_scheduled():
    single = s.Window(start_date='2026-11-18', end_date='2026-11-18', site='MY')
    write('orders', single, {'rows': []})
    assert a.report('default', single)['shops'][0]['current']['orders']['total'] == 0
    window = s.Window(start_date='2026-11-17', end_date='2026-11-19', site='MY')
    snap = s.snapshot('default', '1', 'orders', window)
    assert not snap['complete'] and snap['missing_dates'] == ['2026-11-17', '2026-11-19']
    job = s.ensure('default', window)['job']
    units = [u for u in store.get(job['id'], 'analytics_job')['units'] if u['source'] == 'orders' and u['period'] == 'current']
    assert [(u['start_date'], u['end_date']) for u in units] == [('2026-11-17', '2026-11-17'), ('2026-11-19', '2026-11-19')]


def test_local_midnight_and_statement_utc_do_not_mix():
    # At 00:30 MY on Nov 19 it is still Nov 18 UTC.
    created = stamp('2026-11-19', hour=0)+1800
    write('orders', W, {'rows': [order('a', '2026-11-19', create_time=created)]})
    st = {'id': 's', 'statement_time': created, 'currency': 'MYR', 'settlement_amount': '0.30'}
    tx = {'id': 'tx', 'statement_id': 's', 'statement_time': created, 'currency': 'MYR', 'type': 'ORDER', 'order_id': 'a', 'settlement_amount': '0.30'}
    write('finance', W, {'statements': [st], 'rows': [tx]})
    for day, orders, amount in [('2026-11-18', 0, '0.30'), ('2026-11-19', 1, '0')]:
        r = a.report('default', s.Window(start_date=day, end_date=day, site='MY'))['shops'][0]['current']
        assert r['orders']['total'] == orders and r['finance']['net_settlement'] == amount


def test_transaction_identity_is_statement_scoped_and_decimal_is_exact():
    statements = [{'id': id, 'statement_time': stamp('2026-11-19', 'UTC'), 'currency': 'MYR', 'settlement_amount': amount}
                  for id, amount in [('s1', '0.10'), ('s2', '0.20')]]
    rows = [{**st, 'id': 'same-transaction-id', 'statement_id': st['id'], 'type': 'ORDER', 'order_id': 'a'} for st in statements]
    write('finance', W, {'statements': statements, 'rows': rows})
    write('finance', W, {'statements': statements, 'rows': rows})
    r = a.report('default', W)['shops'][0]['current']['finance']
    assert r['net_settlement'] == '0.30' and r['transaction_count'] == 2 and r['order_count'] == 1


def test_partial_checkpoint_is_visible_before_source_completion_and_keeps_good_facts():
    write('orders', W, {'rows': [order('a', '2026-11-19')]}, retrieved_at=10)
    write('orders', W, {'rows': [order('a', '2026-11-19', status='DELIVERED')]}, complete=False, retrieved_at=20)
    assert s.snapshot('default', '1', 'orders', W)['complete']
    assert a.report('default', W)['shops'][0]['current']['orders']['delivered'] == 1
    other = s.Window(start_date='2026-11-21', end_date='2026-11-21')
    write('orders', other, {'rows': [order('b', '2026-11-21')]}, complete=False)
    assert not s.snapshot('default', '1', 'orders', other)['complete']
    assert a.report('default', other)['shops'][0]['current']['orders']['total'] == 1


def test_migration_preserves_newer_facts_is_idempotent_and_removes_repeated_arrays():
    for source_stamp, status, window in [(10, 'IN_TRANSIT', W), (20, 'DELIVERED', s.Window(start_date='2026-11-19', end_date='2026-11-19'))]:
        data = {'id': s.key('default', '1', 'orders', window.start_date, window.end_date), 'source': 'orders', 'shop_id': '1',
                'start_date': str(window.start_date), 'end_date': str(window.end_date), 'retrieved_at': source_stamp,
                'complete': True, 'evidence': [], 'data': {'rows': [order('a', '2026-11-19', status=status)]}}
        store.save(data, 'analytics_source')
    assert c.migrate_legacy('default', [SHOP], s.Window) == 2
    assert c.migrate_legacy('default', [SHOP], s.Window) == 0
    assert a.report('default', W)['shops'][0]['current']['orders']['delivered'] == 1
    assert all('rows' not in row['data'] for row in store.list_records('analytics_source'))


def test_newly_viewed_range_preempts_old_range_at_source_boundary(monkeypatch):
    calls = []
    async def fetch(workspace, shop, source, window, heartbeat, **kwargs):
        calls.append((str(window.start_date), source)); heartbeat()
        return {'id': s.key(workspace, shop['id'], source, window.start_date, window.end_date), 'data': {},
                'source': source, 'shop_id': shop['id'], 'complete': True, 'retrieved_at': time.time(), 'evidence': []}
    monkeypatch.setattr(s, 'fetch_source', fetch)
    old = s.enqueue('default', W)
    assert s.process_one()
    assert store.get(old['id'], 'analytics_job')['progress'] == 1
    next_window = s.Window(start_date='2026-11-20', end_date='2026-11-20', site='MY')
    newer = s.enqueue('default', next_window)
    assert s.process_one()
    assert calls == [('2026-11-14', 'orders'), ('2026-11-20', 'orders')]
    assert store.get(newer['id'], 'analytics_job')['progress'] == 1


def test_periodic_ledger_sync_is_low_priority_bounded_and_does_not_overlap(monkeypatch):
    sync.register('default', [SHOP]); config = store.list_records('analytics_poll')[0]
    config['next_at'] = 0; store.save(config, 'analytics_poll')
    sync.tick()
    jobs = store.list_records('analytics_job'); assert len(jobs) == 1
    assert jobs[0]['priority'] == 'background' and jobs[0]['total'] == 2
    assert {u['source'] for u in jobs[0]['units']} == {'orders', 'finance'}
    config = store.list_records('analytics_poll')[0]; config['next_at'] = 0; store.save(config, 'analytics_poll')
    sync.tick(); assert len(store.list_records('analytics_job')) == 1
    monkeypatch.setenv('ANALYTICS_SYNC_INTERVAL_SECONDS', '0')
    sync.tick(); assert len(store.list_records('analytics_job')) == 1


def test_finance_checkpoints_publish_each_statement(monkeypatch):
    async def request(workspace, shop, path, query, body=None):
        if path.endswith('/statements'):
            return {'data': {'statements': [{'id': '1', 'statement_time': stamp('2026-11-19','UTC'), 'currency': 'MYR'},
                                             {'id': '2', 'statement_time': stamp('2026-11-20','UTC'), 'currency': 'MYR'}]}}
        return {'data': {'transactions': [{'id': 'tx', 'settlement_amount': '1'}]}}
    monkeypatch.setattr(s, 'request', request)
    progress = []
    result = asyncio.run(s.fetch_source('default', SHOP, 'finance', W, progress=lambda snap, counts: progress.append((snap, counts))))
    assert result['complete'] and len(progress) == 2
    assert progress[0][1]['statements_read'] == 1 and progress[1][1]['statements_read'] == 2
    assert all(not snap['complete'] for snap, _ in progress)


def test_finance_parses_each_fee_once_and_accepts_a_single_pass_stream(monkeypatch):
    calls = []
    original = a.expense_fields
    def parse(row):
        calls.append(row['id']); return original(row)
    monkeypatch.setattr(a, 'expense_fields', parse)
    rows = ({'id': str(i), 'type': 'ORDER', 'order_id': str(i), 'currency': 'MYR',
             'settlement_amount': '0.10', 'revenue_amount': '1',
             'fee_tax_breakdown': {'fee': {'platform_commission_amount': '-0.01', 'affiliate_commission_amount': '0'}}}
            for i in range(1000))
    result = a.aggregate_finance({'rows': rows, 'statements': []}, 'MYR')
    assert len(calls) == 1000
    assert result['transaction_net'] == '100.00'
    assert result['expenses'][0]['amount'] == '-10.00'
    assert result['order_count'] == 1000
