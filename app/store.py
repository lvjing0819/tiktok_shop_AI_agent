"""Shared relational persistence, scoped records and leased background jobs."""
import json
import os
import time
from contextlib import contextmanager
from functools import lru_cache
from uuid import uuid4
from sqlalchemy import create_engine, MetaData, Table, Column, String, Text, Float, select, update, delete, or_, and_

metadata = MetaData()
records = Table('records_v2', metadata,
    Column('kind', String(32), primary_key=True), Column('id', String(100), primary_key=True),
    Column('shop', String(100), nullable=False, index=True), Column('payload', Text, nullable=False))
jobs = Table('jobs_v2', metadata, Column('id', String(100), primary_key=True),
    Column('shop', String(100), nullable=False), Column('state', String(20), nullable=False),
    Column('lease', Float, nullable=False, default=0), Column('token', String(100)),
    Column('payload', Text, nullable=False))


def database_url():
    url = os.getenv('DATABASE_URL')
    if url:
        return url.replace('postgresql://', 'postgresql+psycopg://', 1)
    return 'sqlite:///' + os.getenv('DATABASE_PATH', 'shop.sqlite')


@lru_cache(maxsize=8)
def engine_for(url):
    engine = create_engine(url, connect_args={'check_same_thread': False, 'timeout': 30} if url.startswith('sqlite') else {}, pool_pre_ping=True)
    metadata.create_all(engine)
    return engine


@contextmanager
def db(write=False):
    with engine_for(database_url()).connect() as conn:
        if write and database_url().startswith('sqlite'):
            conn.exec_driver_sql('BEGIN IMMEDIATE')
        else:
            conn.begin()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def put(conn, kind, item, shop='default'):
    key = and_(records.c.kind == kind, records.c.id == item['id'], records.c.shop == shop)
    data = json.dumps(item, ensure_ascii=False)
    if conn.execute(select(records.c.id).where(key)).first():
        conn.execute(update(records).where(key).values(payload=data))
    else:
        conn.execute(records.insert().values(kind=kind, id=item['id'], shop=shop, payload=data))
    return item


def save(item, kind='task', shop='default'):
    with db() as conn:
        return put(conn, kind, item, shop)


def get(item_id, kind='task', shop='default', conn=None):
    if conn is None:
        with db() as opened:
            return get(item_id, kind, shop, opened)
    row = conn.execute(select(records.c.payload).where(records.c.kind == kind, records.c.id == item_id, records.c.shop == shop)).first()
    return json.loads(row[0]) if row else None


def list_records(kind, shop='default'):
    with db() as conn:
        return [json.loads(r[0]) for r in conn.execute(select(records.c.payload).where(records.c.kind == kind, records.c.shop == shop))]


def all_tasks(shop='default'):
    return list_records('task', shop)


def remove(item_id, kind, shop):
    with db() as conn:
        conn.execute(delete(records).where(records.c.kind == kind, records.c.id == item_id, records.c.shop == shop))


def enqueue(task, resume=None, conn=None):
    if conn is None:
        with db(write=True) as opened:
            return enqueue(task, resume, opened)
    if conn is not None:
        put(conn, 'task', task, task['shop'])
        values = dict(shop=task['shop'], state='queued', lease=0, token=None, payload=json.dumps({'resume': resume}))
        exists = conn.execute(select(jobs.c.id).where(jobs.c.id == task['id'])).first()
        if exists:
            conn.execute(update(jobs).where(jobs.c.id == task['id']).values(**values))
        else:
            conn.execute(jobs.insert().values(id=task['id'], **values))


def claim(lease_seconds=120):
    now = time.time()
    available = or_(jobs.c.state == 'queued', and_(jobs.c.state == 'running', jobs.c.lease < now))
    with db() as conn:
        row = conn.execute(select(jobs).where(available).order_by(jobs.c.id).limit(1)).mappings().first()
        if not row:
            return None
        token = str(uuid4())
        result = conn.execute(update(jobs).where(jobs.c.id == row['id'], available).values(state='running', lease=now+lease_seconds, token=token))
        if result.rowcount != 1:
            return None
        return {**dict(row), 'token': token}


def renew(job, lease_seconds=120):
    with db() as conn:
        return conn.execute(update(jobs).where(jobs.c.id == job['id'], jobs.c.token == job['token'], jobs.c.state == 'running').values(lease=time.time()+lease_seconds)).rowcount == 1


def finish(job, task):
    with db() as conn:
        result = conn.execute(update(jobs).where(jobs.c.id == job['id'], jobs.c.token == job['token'], jobs.c.state == 'running').values(state='done', lease=0))
        if result.rowcount == 1:
            put(conn, 'task', task, job['shop'])
            return True
        return False
