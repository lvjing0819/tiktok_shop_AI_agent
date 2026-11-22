"""Back up SQLite before converting repeated analytics snapshots to local facts.

Stop API/worker first. Example:
python -m scripts.migrate_analytics_cache --backup /private/tmp/shop-before.sqlite --compact
"""
import argparse
import sqlite3
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.engine import make_url


def main():
    load_dotenv()
    from app import store, analytics_cache as cache, analytics_sources as sources
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--compact', action='store_true')
    args = parser.parse_args()
    url = make_url(store.database_url())
    if url.get_backend_name() == 'sqlite':
        if not args.backup:
            parser.error('SQLite迁移前必须用 --backup 指定本地备份路径')
        original = Path(url.database).resolve()
        if args.backup.resolve() == original or args.backup.exists():
            parser.error('备份路径须是与数据库不同的全新文件')
        args.backup.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(original)) as source, sqlite3.connect(str(args.backup)) as destination:
            source.backup(destination)
        args.backup.chmod(0o600)
    with store.db() as conn:
        workspaces = list(conn.execute(select(store.records.c.shop).where(store.records.c.kind == 'analytics_source').distinct()).scalars())
    total = 0
    for workspace in workspaces:
        total += cache.migrate_legacy(workspace, sources.shops(workspace), sources.Window)
    if args.compact and url.get_backend_name() == 'sqlite':
        store.engine_for(store.database_url()).dispose()
        with sqlite3.connect(str(original)) as conn:
            conn.execute('VACUUM')
    print({'migrated_snapshots': total, 'workspaces': len(workspaces), 'backup_created': bool(args.backup)})


if __name__ == '__main__':
    main()
