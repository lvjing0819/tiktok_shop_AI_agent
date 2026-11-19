import hmac
import os
from uuid import uuid4
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from app import store, model, memory, conversation
from app.mcp_connection import MCPError

load_dotenv()
app = FastAPI(title="TikTok Shop Agent Workbench", version="0.5.0")


def scope(
    x_shop_id: str = Header(default="default", pattern=r"^[a-zA-Z0-9_-]{1,64}$"),
    authorization: str = Header(default=""),
):
    token = os.getenv("APP_TOKEN", "")
    if token and not hmac.compare_digest(authorization, "Bearer " + token):
        raise HTTPException(401, "请填写工作台访问令牌")
    return x_shop_id


class Product(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    sku: str = Field(min_length=1, max_length=100)
    cost: float = Field(gt=0, le=1000000, allow_inf_nan=False)
    stock: int = Field(ge=0)


class CreateTask(BaseModel):
    product: Product
    target_margin: float = Field(default=0.3, ge=0, lt=0.95, allow_inf_nan=False)


class Review(BaseModel):
    approved: bool
    version: int = Field(ge=1)


class Analysis(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


class SessionBody(BaseModel):
    title: str = Field(default="新会话", min_length=1, max_length=100)


class MessageBody(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market: str = Field(default="", max_length=100)
    brand_style: str = Field(default="", max_length=1000)
    forbidden_claims: str = Field(default="", max_length=2000)
    notes: str = Field(default="", max_length=2000)
    revision: int = Field(ge=0)


def require(task_id, shop, conn=None):
    task = store.get(task_id, shop=shop, conn=conn)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@app.get("/api/health")
def health(shop=Depends(scope)):
    return {
        "model_mode": model.mode(),
        "key_configured": bool(os.getenv("DASHSCOPE_API_KEY")),
        "shop_mode": "mock",
        "media_mode": "mock",
        "database": (
            "postgresql" if store.database_url().startswith("postgresql") else "sqlite"
        ),
        "version": "0.5.0",
        "conversation_routing": model.mode() == "bailian",
    }


@app.post("/api/model/check")
def model_check(shop=Depends(scope)):
    try:
        return {
            "mode": model.mode(),
            "response": model.complete("仅回复：连接成功", "连接测试"),
        }
    except model.ModelError as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/tasks")
def tasks(shop=Depends(scope)):
    return store.all_tasks(shop)


@app.get("/api/tasks/{task_id}")
def task(task_id: str, shop=Depends(scope)):
    return require(task_id, shop)


@app.post("/api/tasks", status_code=202)
def create(body: CreateTask, shop=Depends(scope)):
    t = {
        "id": str(uuid4()),
        "shop": shop,
        **body.model_dump(),
        "preferences": memory.preferences(shop),
        "status": "queued",
        "version": 1,
        "events": ["已创建任务，等待后台执行"],
    }
    store.enqueue(t)
    return t


def locked_task(conn, task_id, shop):
    # SQLite callers take a write lock; PostgreSQL callers take a row lock.
    conn.execute(
        select(store.records.c.id)
        .where(
            store.records.c.kind == "task",
            store.records.c.id == task_id,
            store.records.c.shop == shop,
        )
        .with_for_update()
    ).first()
    return require(task_id, shop, conn)


@app.post("/api/tasks/{task_id}/retry", status_code=202)
def retry(task_id: str, shop=Depends(scope)):
    with store.db(write=True) as conn:
        t = locked_task(conn, task_id, shop)
        if t["status"] != "failed":
            raise HTTPException(409, "仅失败任务可重试")
        t["status"] = "queued"
        t["events"].append("已请求从持久化检查点重试")
        row = conn.execute(
            select(store.jobs.c.payload).where(store.jobs.c.id == task_id)
        ).first()
        import json

        resume = json.loads(row[0])["resume"] if row else None
        store.enqueue(t, resume, conn)
        return t


@app.post("/api/tasks/{task_id}/review", status_code=202)
def review(task_id: str, body: Review, shop=Depends(scope)):
    with store.db(write=True) as conn:
        t = locked_task(conn, task_id, shop)
        if body.version != t["version"]:
            raise HTTPException(409, "审核版本已变化，请刷新任务")
        if (
            t["status"] in ("published", "queued_review", "rejected")
            and t.get("decision") == body.model_dump()
        ):
            return t
        if t["status"] != "awaiting_review":
            raise HTTPException(409, "当前任务不处于待审核状态")
        t["decision"] = body.model_dump()
        t["status"] = "queued_review"
        store.enqueue(t, body.model_dump(), conn)
        return t


@app.get("/api/memory/preferences")
def get_preferences(shop=Depends(scope)):
    return memory.preferences(shop)


@app.put("/api/memory/preferences")
def set_preferences(body: Preferences, shop=Depends(scope)):
    # Revision CAS also handles writes from multiple browser tabs.
    from sqlalchemy import update
    import json

    with store.db(write=True) as conn:
        item = store.get("preferences:" + shop, "memory", shop, conn)
        if item is None:
            if body.revision != 0:
                raise HTTPException(409, "偏好已变化，请刷新")
            return store.put(
                conn,
                "memory",
                {"id": "preferences:" + shop, **body.model_dump(), "revision": 1},
                shop,
            )
        if item["revision"] != body.revision:
            raise HTTPException(409, "偏好已变化，请刷新")
        result = {
            "id": item["id"],
            **body.model_dump(),
            "revision": item["revision"] + 1,
        }
        changed = conn.execute(
            update(store.records)
            .where(
                store.records.c.kind == "memory",
                store.records.c.id == item["id"],
                store.records.c.shop == shop,
                store.records.c.payload == json.dumps(item, ensure_ascii=False),
            )
            .values(payload=json.dumps(result, ensure_ascii=False))
        )
        if changed.rowcount != 1:
            raise HTTPException(409, "偏好已变化，请刷新")
        return result


@app.delete("/api/memory/preferences")
def clear_preferences(shop=Depends(scope)):
    # Keep revision monotonic to prevent stale editors restoring forgotten data.
    item = memory.preferences(shop)
    return set_preferences(Preferences(revision=item["revision"]), shop)


@app.post("/api/sessions", status_code=201)
def create_session(body: SessionBody, shop=Depends(scope)):
    return memory.new_session(shop, body.title)


@app.get("/api/sessions")
def sessions(shop=Depends(scope)):
    return [
        {k: v for k, v in s.items() if k != "messages"}
        for s in store.list_records("session", shop)
    ]


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str, shop=Depends(scope)):
    session = store.get(session_id, "session", shop)
    if not session:
        raise HTTPException(404, "会话不存在")
    return session


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str, shop=Depends(scope)):
    import json
    from sqlalchemy import delete

    with store.db(write=True) as conn:
        rows = conn.execute(
            select(store.records.c.id, store.records.c.payload).where(
                store.records.c.kind == "conversation_run", store.records.c.shop == shop
            )
        ).all()
        for rid, payload in rows:
            if json.loads(payload).get("session_id") == session_id:
                conn.execute(
                    delete(store.records).where(
                        store.records.c.id == rid,
                        store.records.c.kind == "conversation_run",
                        store.records.c.shop == shop,
                    )
                )
        conn.execute(
            delete(store.records).where(
                store.records.c.id == session_id,
                store.records.c.kind == "session",
                store.records.c.shop == shop,
            )
        )
    return {"deleted": True}


@app.get("/api/sessions/{session_id}/runs")
def conversation_runs(session_id: str, shop=Depends(scope)):
    get_session(session_id, shop)
    rows = [
        r
        for r in store.list_records("conversation_run", shop)
        if r["session_id"] == session_id
    ]
    return [
        {k: v for k, v in r.items() if k not in ("evidence", "answer")}
        for r in sorted(rows, key=lambda r: r["created_at"], reverse=True)[:50]
    ]


@app.get("/api/sessions/{session_id}/runs/{run_id}")
def conversation_run(session_id: str, run_id: str, shop=Depends(scope)):
    get_session(session_id, shop)
    run = store.get(run_id, "conversation_run", shop)
    if not run or run["session_id"] != session_id:
        raise HTTPException(404, "会话执行记录不存在")
    return run


@app.post("/api/sessions/{session_id}/messages")
def send_message(session_id: str, body: MessageBody, shop=Depends(scope)):
    try:
        return conversation.send(session_id, shop, body.content)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except model.ModelError as exc:
        raise HTTPException(502, str(exc))
    except MCPError as exc:
        raise HTTPException(502, str(exc))
    except TimeoutError:
        raise HTTPException(504, "本轮查询超时，请缩小查询范围后重试")


@app.post("/api/analyze")
def analyze(body: Analysis, shop=Depends(scope)):
    import json

    evidence = {
        "source": "local_tasks",
        "tasks": [
            {"id": t["id"], "status": t["status"], "sku": t["product"]["sku"]}
            for t in store.all_tasks(shop)
        ][-50:],
    }
    try:
        answer = model.complete(
            "你是经营分析专员。仅依据数据回答，没有订单财务广告数据时明确说明缺失，不能编造指标。",
            json.dumps(
                {"question": body.question, "evidence": evidence}, ensure_ascii=False
            ),
        )
    except model.ModelError as exc:
        raise HTTPException(502, str(exc))
    return {"answer": answer, "evidence": evidence, "mode": model.mode()}


from app.ads_routes import router as ads_router

app.include_router(ads_router(scope))
from app.social_routes import router as social_router

app.include_router(social_router(scope))

# A production build can be served by the same local API process.
from pathlib import Path
from fastapi.staticfiles import StaticFiles

frontend_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="workbench")
