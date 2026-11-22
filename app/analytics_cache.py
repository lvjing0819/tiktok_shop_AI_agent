"""Deduplicated Shop facts and explicit daily coverage, never inferred from rows.

Orders use shop-local creation days. Statements/transactions use UTC statement
days. Interval-only platform metrics deliberately stay outside this cache.
"""
import hashlib
import json
import time
from datetime import date, datetime, time as midnight, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import Table, Column, String, Text, Float, Boolean, Index, select, update, delete, func
from app import store

entities = Table('analytics_entities_v1', store.metadata,
    Column('workspace', String(64), primary_key=True), Column('shop_id', String(40), primary_key=True),
    Column('source', String(20), primary_key=True), Column('kind', String(20), primary_key=True),
    Column('entity_id', String(160), primary_key=True), Column('event_time', Float, nullable=False),
    Column('payload', Text, nullable=False), Column('retrieved_at', Float, nullable=False))
Index('ix_analytics_entities_window', entities.c.workspace, entities.c.shop_id, entities.c.source, entities.c.kind, entities.c.event_time)
coverage = Table('analytics_days_v1', store.metadata,
    Column('workspace', String(64), primary_key=True), Column('shop_id', String(40), primary_key=True),
    Column('source', String(20), primary_key=True), Column('zone', String(80), primary_key=True),
    Column('day', String(10), primary_key=True), Column('complete', Boolean, nullable=False),
    Column('retrieved_at', Float, nullable=False), Column('evidence', Text, nullable=False))
LOCAL_SOURCES = ('orders', 'finance')


def days(window):
    return [(window.start_date + timedelta(days=i)).isoformat()
            for i in range((window.end_date-window.start_date).days+1)]


def zone_for(source, shop_zone):
    return 'UTC' if source == 'finance' else shop_zone


def limits(window, zone):
    tz = ZoneInfo(zone)
    return (datetime.combine(window.start_date, midnight.min, tz).timestamp(),
            datetime.combine(window.end_date+timedelta(days=1), midnight.min, tz).timestamp())


def scope(table, workspace, shop_id, source):
    return (table.c.workspace == workspace, table.c.shop_id == shop_id, table.c.source == source)


def day_rows(workspace, shop_id, source, window, zone):
    with store.db() as conn:
        return [dict(r) for r in conn.execute(select(coverage).where(
            *scope(coverage, workspace, shop_id, source), coverage.c.zone == zone,
            coverage.c.day >= str(window.start_date), coverage.c.day <= str(window.end_date))).mappings()]


def missing_ranges(workspace, shop_id, source, window, shop_zone):
    """Return contiguous unobserved days, retaining known partial vs missing."""
    known = {r['day'] for r in day_rows(workspace, shop_id, source, window, zone_for(source, shop_zone))}
    spans = []
    for day in days(window):
        if day in known:
            continue
        value = date.fromisoformat(day)
        if spans and value == spans[-1][1]+timedelta(days=1):
            spans[-1] = (spans[-1][0], value)
        else:
            spans.append((value, value))
    return spans


def persist(conn, workspace, shop_id, source, window, shop_zone, snapshot, *, reconcile=True):
    """Atomically upsert facts and coverage; return False for legacy unindexable data.

    Partial page checkpoints never delete previous good facts or downgrade daily
    completeness. Only a successfully completed source can reconcile removals.
    """
    if source not in LOCAL_SOURCES:
        return False
    data = snapshot['data']; stamp = snapshot['retrieved_at']
    groups = [('order', data.get('rows', []), 'create_time')] if source == 'orders' else [
        ('statement', data.get('statements', []), 'statement_time'),
        ('transaction', data.get('rows', []), 'statement_time')]
    facts = []
    for kind, rows, field in groups:
        for row in rows:
            if not row.get('id') or not isinstance(row.get(field), (int, float)):
                return False
            # A transaction ID is scoped to its statement, not to its order.
            identity = f"{row.get('statement_id', '')}:{row['id']}" if kind == 'transaction' else str(row['id'])
            facts.append((kind, identity, row[field], row))
    zone = zone_for(source, shop_zone)
    lower, upper = limits(window, zone)
    if any(not lower <= event < upper for _, _, event, _ in facts):
        return False
    filters = scope(entities, workspace, shop_id, source)
    for kind, identity, event, payload in facts:
        where = (*filters, entities.c.kind == kind, entities.c.entity_id == identity)
        old = conn.execute(select(entities.c.retrieved_at).where(*where)).scalar_one_or_none()
        values = {'event_time': event, 'payload': json.dumps(payload, ensure_ascii=False), 'retrieved_at': stamp}
        if old is None:
            conn.execute(entities.insert().values(workspace=workspace, shop_id=shop_id, source=source, kind=kind, entity_id=identity, **values))
        elif old <= stamp:
            conn.execute(update(entities).where(*where).values(**values))
    if snapshot['complete'] and reconcile:
        retained = {(kind, identity) for kind, identity, _, _ in facts}
        previous = conn.execute(select(entities.c.kind, entities.c.entity_id).where(
            *filters, entities.c.event_time >= lower, entities.c.event_time < upper, entities.c.retrieved_at <= stamp)).all()
        for kind, identity in previous:
            if (kind, identity) not in retained:
                conn.execute(delete(entities).where(*filters, entities.c.kind == kind, entities.c.entity_id == identity))
    for day in days(window):
        where = (*scope(coverage, workspace, shop_id, source), coverage.c.zone == zone, coverage.c.day == day)
        old = conn.execute(select(coverage).where(*where)).mappings().first()
        # Keep a prior complete result while a new partial fetch is in flight.
        if old and (old['retrieved_at'] > stamp or (old['complete'] and not snapshot['complete'])):
            continue
        values = {'complete': snapshot['complete'], 'retrieved_at': stamp,
                  'evidence': json.dumps(snapshot.get('evidence', [])[-3:], ensure_ascii=False)}
        if old:
            conn.execute(update(coverage).where(*where).values(**values))
        else:
            conn.execute(coverage.insert().values(workspace=workspace, shop_id=shop_id, source=source, zone=zone, day=day, **values))
    return True


class Rows:
    """Re-iterable SQL range, streamed in chunks; not the whole history in RAM."""
    def __init__(self, workspace, shop_id, source, kind, window, zone):
        lo, hi = limits(window, zone)
        self.where = (*scope(entities, workspace, shop_id, source), entities.c.kind == kind,
                      entities.c.event_time >= lo, entities.c.event_time < hi)

    def __iter__(self):
        with store.db() as conn:
            result = conn.execution_options(yield_per=200).execute(select(entities.c.payload).where(*self.where).order_by(entities.c.event_time, entities.c.entity_id))
            for row in result:
                yield json.loads(row[0])

    def __len__(self):
        with store.db() as conn:
            return conn.execute(select(func.count()).select_from(entities).where(*self.where)).scalar_one()

    def page(self, offset, limit):
        with store.db() as conn:
            return [json.loads(r[0]) for r in conn.execute(select(entities.c.payload).where(*self.where)
                .order_by(entities.c.event_time, entities.c.entity_id).offset(offset).limit(limit))]


def lookup(workspace, shop_id, source, window, shop_zone):
    if source not in LOCAL_SOURCES:
        return None
    zone = zone_for(source, shop_zone)
    known = day_rows(workspace, shop_id, source, window, zone)
    if not known:
        return None
    observed = {r['day'] for r in known}
    missing = [day for day in days(window) if day not in observed]
    data = {'rows': Rows(workspace, shop_id, source, 'order' if source == 'orders' else 'transaction', window, zone)}
    if source == 'finance':
        data.update(statements=Rows(workspace, shop_id, source, 'statement', window, zone), date_basis='statement_generated_date_UTC')
    return {'shop_id': shop_id, 'source': source, 'start_date': str(window.start_date), 'end_date': str(window.end_date),
            'complete': not missing and all(r['complete'] for r in known), 'retrieved_at': min(r['retrieved_at'] for r in known),
            'storage': 'local_database', 'observed_days': len(known), 'total_days': len(days(window)), 'missing_dates': missing,
            'evidence': json.loads(max(known, key=lambda r: r['retrieved_at'])['evidence']), 'data': data}


def store_snapshot(conn, workspace, shop, source, window, snapshot):
    if persist(conn, workspace, shop['id'], source, window, shop['timezone'], snapshot):
        # Keep provenance, not repeated copies of the same orders and transactions.
        snapshot = {**snapshot, 'storage': 'local_database', 'data': {k: v for k, v in snapshot['data'].items() if k not in ('rows', 'statements')}}
    store.put(conn, 'analytics_source', snapshot, workspace)


def migrate_legacy(workspace, shops, window_type):
    """Idempotent bounded migration; original records change only on success."""
    marker = hashlib.sha256(workspace.encode()).hexdigest()
    if store.get(marker, 'analytics_cache_migration', workspace):
        return 0
    by_id = {s['id']: s for s in shops}; last = ''; migrated = 0
    while True:
        with store.db() as conn:
            batch = conn.execute(select(store.records.c.id, store.records.c.payload).where(
                store.records.c.kind == 'analytics_source', store.records.c.shop == workspace,
                store.records.c.id > last).order_by(store.records.c.id).limit(10)).all()
        if not batch:
            break
        last = batch[-1][0]
        for _, payload in batch:
            item = json.loads(payload); source = item['source']; shop = by_id.get(item['shop_id'])
            if source not in LOCAL_SOURCES or not shop or item.get('storage') == 'local_database':
                continue
            window = window_type(start_date=item['start_date'], end_date=item['end_date'])
            with store.db(write=True) as conn:
                # Merging historical overlapping observations must not erase newer facts.
                if persist(conn, workspace, shop['id'], source, window, shop['timezone'], item, reconcile=False):
                    item = {**item, 'storage': 'local_database', 'data': {k: v for k, v in item['data'].items() if k not in ('rows', 'statements')}}
                    store.put(conn, 'analytics_source', item, workspace); migrated += 1
    store.save({'id': marker, 'version': 1, 'completed_at': time.time()}, 'analytics_cache_migration', workspace)
    return migrated
