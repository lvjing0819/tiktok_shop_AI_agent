import asyncio
import time
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from app import social_mcp as social, social_agent, store, model

background = set()


class ToolCall(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    arguments: dict = Field(default_factory=dict)


class SocialQuery(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


def router(scope):
    r = APIRouter(prefix="/api/social", tags=["TikTok Social MCP"])

    @r.get("/connection")
    def connection(shop=Depends(scope)):
        return {
            **social.public_status(shop),
            "service": "social",
            "access_token_lifetime_days": 1,
            "authorization_window_days": 365,
        }

    @r.post("/connect", status_code=202)
    async def connect(shop=Depends(scope)):
        try:
            if social.auth_mode() == "api_token":
                return await social.verify_api(shop)
            attempt, fresh = social.begin_connection(shop)
            if fresh:
                task = asyncio.create_task(
                    social.authorize(shop, attempt["id"], attempt["generation"])
                )
                background.add(task)
                task.add_done_callback(background.discard)
            return {"attempt_id": attempt["id"], "expires_at": attempt["expires_at"]}
        except social.SocialError as exc:
            raise HTTPException(400, str(exc))

    @r.get("/login")
    async def login(shop=Depends(scope)):
        if social.auth_mode() != "oauth":
            raise HTTPException(400, "API token 模式无需浏览器登录")
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        if social.public_status(shop)["status"] == "connected":
            return HTMLResponse(
                '<meta charset="utf-8"><p>Social 已连接，可以返回后端调用工具。</p>',
                headers=headers,
            )
        started = await connect(shop)
        for _ in range(20):
            attempt = store.get(started["attempt_id"], "social_attempt", shop)
            if (
                attempt
                and attempt.get("authorization_url")
                and attempt["expires_at"] > time.time()
            ):
                return RedirectResponse(
                    attempt["authorization_url"], status_code=303, headers=headers
                )
            if social.public_status(shop)["status"] == "error":
                raise HTTPException(502, "Social 授权地址生成失败，请检查网络和代理")
            await asyncio.sleep(0.5)
        return HTMLResponse(
            '<meta charset="utf-8"><p>正在连接 TikTok，页面将自动重试。</p>',
            headers={**headers, "Refresh": "3"},
        )

    @r.get("/attempts/{attempt_id}")
    def attempt_status(attempt_id: str, shop=Depends(scope)):
        attempt = store.get(attempt_id, "social_attempt", shop)
        if not attempt:
            raise HTTPException(404, "Social 授权会话不存在")
        return {
            **social.public_status(shop),
            "expires_at": attempt["expires_at"],
            "authorization_url": (
                attempt.get("authorization_url")
                if attempt["expires_at"] > time.time()
                else None
            ),
        }

    @r.get("/oauth/callback", response_class=HTMLResponse)
    def callback(
        state: str = Query(min_length=16, max_length=1000),
        code: str | None = Query(default=None, min_length=1, max_length=4000),
        error: str | None = Query(default=None, max_length=200),
    ):
        try:
            if not code and not error:
                raise social.SocialError("Social 授权回调缺少结果")
            social.accept_callback(state, code, denied=bool(error))
            text = (
                "未完成授权，请返回工作台重试。"
                if error
                else "Social 授权已接收，请返回工作台查看连接状态。"
            )
            return HTMLResponse(
                '<meta charset="utf-8"><p>' + text + "</p>",
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
        except social.SocialError as exc:
            raise HTTPException(400, str(exc))

    @r.delete("/connection")
    def disconnect(shop=Depends(scope)):
        return social.disconnect(shop)

    @r.get("/tools")
    async def tools(shop=Depends(scope)):
        try:
            async with social.session(shop) as mcp:
                available = await social.tools(mcp)
                return [
                    {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "allowed": social.permitted(t),
                        "input_schema": t["inputSchema"],
                    }
                    for t in available
                ]
        except social.SocialError as exc:
            raise HTTPException(502, str(exc))

    @r.post("/tools/call")
    async def call(body: ToolCall, shop=Depends(scope)):
        # Reject mutating names before even opening the remote connection.
        if not social.permitted({"name": body.name}):
            raise HTTPException(403, "Social 当前只允许已确认的只读工具")
        try:
            async with social.session(shop) as mcp:
                available = await social.tools(mcp)
                result = await social.call(mcp, available, body.name, body.arguments)
                record = {
                    "id": str(uuid4()),
                    "tool": body.name,
                    "arguments": body.arguments,
                    "result": result,
                    "read_only": True,
                    "created_at": time.time(),
                }
                store.save(record, "social_call", shop)
                return record
        except social.SocialError as exc:
            raise HTTPException(502, str(exc))

    @r.post("/query")
    async def query(body: SocialQuery, shop=Depends(scope)):
        try:
            async with asyncio.timeout(240):
                return await social_agent.query(shop, body.question)
        except (social.SocialError, model.ModelError) as exc:
            raise HTTPException(502, str(exc))
        except TimeoutError:
            raise HTTPException(504, "Social 查询超时，请缩小范围")

    @r.get("/reports")
    def reports(shop=Depends(scope)):
        return [
            {k: v for k, v in p.items() if k not in ("evidence", "answer")}
            for p in store.list_records("social_report", shop)
        ]

    @r.get("/reports/{report_id}")
    def report(report_id: str, shop=Depends(scope)):
        result = store.get(report_id, "social_report", shop)
        if not result:
            raise HTTPException(404, "Social 报告不存在")
        return result

    return r
