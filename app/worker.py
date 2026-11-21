"""Run with python -m app.worker. Leases reclaim jobs after a worker crash."""
import json
import logging
import threading
import time
from dotenv import load_dotenv
from langgraph.types import Command
from app import store, model, business  # Register business tables before first DB access.
from app.checkpoints import saver
from app.workflow import build

load_dotenv()
logger = logging.getLogger(__name__)


def process_one():
    job = store.claim()
    if not job:
        from app import harness, business_sync
        return harness.process_one() or business_sync.process_one()
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(20):
            try:
                if not store.renew(job):
                    break
            except Exception:
                logger.warning('任务租约续期失败')
                break
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    t = store.get(job['id'], shop=job['shop'])
    try:
        t['status'] = 'running'
        store.save(t, shop=job['shop'])
        with saver() as cp:
            graph = build(cp)
            config = {'configurable': {'thread_id': 'product:'+job['id']}}
            snapshot = graph.get_state(config)
            resume = json.loads(job['payload'])['resume']
            if snapshot.values:
                if not snapshot.next:
                    result = snapshot.values
                elif any(x.interrupts for x in snapshot.tasks):
                    result = graph.invoke(Command(resume=resume), config) if resume is not None else snapshot.values
                else:
                    result = graph.invoke(None, config)
            else:
                result = graph.invoke({'task': t}, config)
            t = result['task']
            t.pop('error', None)
    except Exception as exc:
        t['status'] = 'failed'
        t['error'] = str(exc) if isinstance(exc, (model.ModelError, ValueError)) else '执行失败，请检查后台服务日志和配置'
        t['events'] = t['events'] + [t['error']]
    finally:
        stop.set()
        thread.join(timeout=1)
    store.finish(job, t)
    return True


def main():
    import sys
    if '--once' in sys.argv:
        process_one()
        return
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    while True:
        try:
            if not process_one():
                time.sleep(1)
        except Exception:
            logger.error('后台任务暂时不可用，稍后重试')
            time.sleep(3)


if __name__ == '__main__':
    main()
