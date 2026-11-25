"""Agent migration/retention: previews by default, explicit confirmed writes."""
import argparse
import json
import sqlite3
from pathlib import Path
from dotenv import load_dotenv
from app import agent_maintenance as maintenance, store


def main():
    load_dotenv()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['migrate','preview','apply'])
    parser.add_argument('--workspace',default='default')
    parser.add_argument('--write',action='store_true',help='Apply migration; requires a new SQLite backup path')
    parser.add_argument('--backup')
    parser.add_argument('--preview-id')
    parser.add_argument('--confirmation')
    args=parser.parse_args()
    if args.action=='migrate':
        if args.write:
            if not store.database_url().startswith('sqlite:///') or not args.backup:
                parser.error('写迁移需要SQLite和--backup全新备份路径；先停止API/worker')
            target=Path(args.backup)
            if target.exists():parser.error('备份路径已存在，不能覆盖')
            target.parent.mkdir(parents=True,exist_ok=True)
            source=store.database_url().removeprefix('sqlite:///')
            # Exclusive creation protects an existing backup, including symlinks.
            with target.open('xb'):
                pass
            target.chmod(0o600)
            with sqlite3.connect(source) as db, sqlite3.connect(target) as backup:
                db.backup(backup)
        result=maintenance.migrate(args.workspace,apply_changes=args.write)
    elif args.action=='preview':result=maintenance.preview(args.workspace)
    else:
        if not args.preview_id or not args.confirmation:parser.error('执行清理需要预览ID及confirmation')
        result=maintenance.apply(args.workspace,args.preview_id,args.confirmation)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
