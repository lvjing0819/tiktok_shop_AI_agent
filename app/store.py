import json
import os
import sqlite3
from contextlib import contextmanager


@contextmanager
def db():
    conn = sqlite3.connect(os.getenv('DATABASE_PATH', 'shop.sqlite'), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS listings (task_id TEXT PRIMARY KEY, listing_id TEXT NOT NULL)')
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save(task):
    with db() as conn:
        conn.execute('INSERT INTO tasks VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                     (task['id'], json.dumps(task, ensure_ascii=False)))
    return task


def get(task_id):
    with db() as conn:
        row = conn.execute('SELECT payload FROM tasks WHERE id=?', (task_id,)).fetchone()
    return json.loads(row['payload']) if row else None


def all_tasks():
    with db() as conn:
        return [json.loads(r['payload']) for r in conn.execute('SELECT payload FROM tasks ORDER BY rowid DESC')]
