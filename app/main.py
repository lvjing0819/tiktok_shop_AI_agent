import json
import os
from uuid import uuid4
from threading import Lock
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field
from app import store
from app.model import complete
from app.workflow import run

load_dotenv()
app = FastAPI(title='TikTok Shop Agent Workbench', version='0.1.0')
locks = Lock()


class Product(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    sku: str = Field(min_length=1, max_length=100)
    cost: float = Field(gt=0, le=1000000)
    stock: int = Field(ge=0)


class CreateTask(BaseModel):
    product: Product
    target_margin: float = Field(default=.3, ge=0, lt=.95)


class Review(BaseModel):
    approved: bool


class Analysis(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def require(task_id):
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, '任务不存在')
    return task


@app.get('/api/health')
def health():
    return {'model_mode': os.getenv('MODEL_MODE', 'demo'), 'shop_mode': 'mock', 'media_mode': 'mock'}


@app.get('/api/tasks')
def tasks():
    return store.all_tasks()


@app.get('/api/tasks/{task_id}')
def task(task_id: str):
    return require(task_id)


@app.post('/api/tasks', status_code=202)
def create(body: CreateTask, background: BackgroundTasks):
    t = store.save({'id': str(uuid4()), **body.model_dump(), 'status': 'queued', 'version': 1, 'events': ['已创建商品准备任务']})
    background.add_task(run, t)
    return t


@app.post('/api/tasks/{task_id}/retry', status_code=202)
def retry(task_id: str, background: BackgroundTasks):
    with locks:
        t = require(task_id)
        if t['status'] != 'failed':
            raise HTTPException(409, '仅失败任务可重试')
        t['status'] = 'queued'
        t['events'].append('已请求重试')
        store.save(t)
    background.add_task(run, t)
    return t


@app.post('/api/tasks/{task_id}/review')
def review(task_id: str, body: Review):
    with locks, store.db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT payload FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, '任务不存在')
        t = json.loads(row['payload'])
        if t['status'] == 'published' and body.approved:
            return t
        if t['status'] != 'awaiting_review':
            raise HTTPException(409, '当前任务不处于待审核状态')
        t['approved_version'] = t['version'] if body.approved else None
        t['status'] = 'published' if body.approved else 'rejected'
        if body.approved:
            listing = 'mock-' + task_id
            conn.execute('INSERT OR IGNORE INTO listings VALUES (?,?)', (task_id, listing))
            t['listing_id'] = listing
        t['events'].append('已批准并模拟上架（未调用 TikTok）' if body.approved else '已驳回')
        conn.execute('UPDATE tasks SET payload=? WHERE id=?', (json.dumps(t, ensure_ascii=False), task_id))
        return t


@app.post('/api/analyze')
def analyze(body: Analysis):
    # Tool-provided evidence: demo catalog only, no invented financial results.
    evidence = {'source': 'local_demo_tasks', 'tasks': [{'id': t['id'], 'status': t['status'], 'sku': t['product']['sku']} for t in store.all_tasks()]}
    try:
        answer = complete('你是经营分析专员。仅依据提供数据回答，区分事实和建议。没有订单、财务、广告数据时明确说明缺失，不能编造指标。', json.dumps({'question': body.question, 'evidence': evidence}, ensure_ascii=False))
    except Exception:
        raise HTTPException(502, '百炼调用失败，请检查模型与接口配置')
    return {'answer': answer, 'evidence': evidence, 'mode': os.getenv('MODEL_MODE', 'demo')}
