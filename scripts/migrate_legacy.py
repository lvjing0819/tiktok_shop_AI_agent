"""Read legacy v0.1 SQLite tasks and import into configured v0.2 database.
Run: python -m scripts.migrate_legacy /path/to/old.sqlite
Never edits the source database. Existing task IDs are skipped.
"""
import json
import sqlite3
import sys
from dotenv import load_dotenv
from app import store
from app.memory import preferences


def migrate(path):
    source=sqlite3.connect('file:'+path+'?mode=ro', uri=True)
    count=0
    try:
        for (payload,) in source.execute('SELECT payload FROM tasks'):
            t=json.loads(payload)
            shop='default'
            if store.get(t['id'],shop=shop):
                continue
            t['shop']=shop
            t['preferences']=preferences(shop)
            if t['status'] not in ('published','rejected'):
                t['status']='queued'
                t['events'].append('旧版任务已迁移；首次运行将重新准备方案并要求审核')
                store.enqueue(t)
            else:
                if t['status']=='published':
                    t['decision']={'approved':True,'version':t['version']}
                store.save(t,shop=shop)
            count+=1
    finally:
        source.close()
    print(f'Imported {count} tasks')


if __name__=='__main__':
    load_dotenv()
    migrate(sys.argv[1])
