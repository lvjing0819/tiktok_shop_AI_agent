from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from urllib.parse import urlsplit, parse_qs
from app import shop_api as shop

HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Shop-Callback": "tiktok-shop-agent-v0.6",
}


class CompleteRedirect(BaseModel):
    callback_url: str = Field(min_length=1, max_length=8192)


FINISH_HTML = """<!doctype html><meta charset="utf-8"><title>完成 TikTok Shop 本机授权</title>
<style>body{max-width:780px;margin:65px auto;padding:0 24px;font:17px/1.7 system-ui;color:#163443}textarea,input{width:100%;box-sizing:border-box;margin:10px 0 24px;padding:14px;border:1px solid #aebec7;border-radius:8px;font:15px monospace}textarea{height:140px}button{background:#0c7b74;color:white;padding:13px 24px;border:0;border-radius:8px;font:17px system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>
<h1>完成 TikTok Shop 本机授权</h1><p>用于当前公网回调尚未接入本项目时的一次性联调。请在同一台电脑完成授权。</p><ol><li>从本机授权入口开始，并在 TikTok Shop 完成卖家授权。</li><li>如果停在配置的回调地址，将浏览器地址栏里的完整地址粘贴到下面。不要发到聊天里。</li><li>本机后端会验证 state，换取令牌，并查询真实授权店铺。此页面不会访问粘贴的 URL。</li></ol>
<label for="callback">授权完成后的回调地址</label><textarea id="callback" autocomplete="off" spellcheck="false" placeholder="https://你的回调域名/api/tiktokshop/callback?code=...&state=..."></textarea>
<label for="token">工作台访问令牌（仅在你配置了 APP_TOKEN 时填写）</label><input id="token" type="password" autocomplete="off"><button id="submit">提交给本机后端验证</button><pre id="result" role="status"></pre>
<script>document.querySelector('#submit').onclick=async()=>{let b=document.querySelector('#submit'),i=document.querySelector('#callback'),t=document.querySelector('#token'),o=document.querySelector('#result');b.disabled=true;o.textContent='正在验证授权并查询店铺…';try{let r=await fetch('/api/shop/complete-redirect',{method:'POST',headers:{'Content-Type':'application/json',...(t.value?{'Authorization':'Bearer '+t.value}:{})},body:JSON.stringify({callback_url:i.value.trim()})});let d=await r.json();if(!r.ok)throw Error(d.detail||'处理失败');i.value='';t.value='';o.textContent=JSON.stringify(d,null,2)}catch(e){o.textContent=e.message}finally{b.disabled=false}};</script>"""


def router(scope):
    r = APIRouter(tags=["TikTok Shop seller authorization"])

    @r.get("/api/shop/connection")
    def connection(workspace=Depends(scope)):
        return shop.status(workspace)

    @r.post("/api/shop/connect", status_code=202)
    def connect(workspace=Depends(scope)):
        try:
            return shop.begin(workspace)
        except shop.ShopError as exc:
            raise HTTPException(400, str(exc))

    @r.post("/api/shop/import-token")
    async def import_token(workspace=Depends(scope)):
        try:
            return await shop.import_existing(workspace)
        except shop.ShopError as exc:
            raise HTTPException(502, str(exc))

    @r.get("/api/shop/login")
    def login(workspace=Depends(scope)):
        try:
            value = shop.begin(workspace)
            return RedirectResponse(
                value["authorization_url"], status_code=303, headers=HEADERS
            )
        except shop.ShopError as exc:
            raise HTTPException(400, str(exc))

    @r.get("/api/shop/finish", response_class=HTMLResponse)
    def finish_page():
        return HTMLResponse(
            FINISH_HTML,
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
            },
        )

    @r.post("/api/shop/complete-redirect")
    async def complete_redirect(body: CompleteRedirect, workspace=Depends(scope)):
        try:
            cfg = shop.config(oauth=True)
            try:
                supplied = urlsplit(body.callback_url)
                expected = urlsplit(cfg["redirect_uri"])
                valid = (
                    supplied.scheme,
                    supplied.hostname,
                    supplied.port or 443,
                    supplied.path,
                ) == (
                    expected.scheme,
                    expected.hostname,
                    expected.port or 443,
                    expected.path,
                )
            except ValueError:
                raise shop.ShopError("回调地址格式无效") from None
            if not valid or supplied.username or supplied.fragment:
                raise shop.ShopError(
                    "仅接收本项目已注册的 Shop 回调地址，不会访问输入的 URL"
                )
            params = parse_qs(supplied.query, keep_blank_values=True)
            if (
                len(params.get("state", [])) != 1
                or len(params["state"][0]) < 32
                or any(len(params.get(k, [])) > 1 for k in ["code", "error"])
            ):
                raise shop.ShopError("回调缺少唯一有效 state，请从本机授权入口重新开始")
            # Check the pending state belongs to this caller's local workspace
            # before the shared callback consumes it.
            import hashlib, time
            from app import store

            pending = store.get(
                hashlib.sha256(params["state"][0].encode()).hexdigest(),
                "shop_oauth_state",
                "_oauth",
            )
            if (
                not pending
                or pending["target_shop"] != workspace
                or pending["expires_at"] < time.time()
            ):
                raise shop.ShopError("回调不属于当前工作区，或授权已过期")
            return await shop.callback(
                params["state"][0],
                params.get("code", [None])[0],
                denied=bool(params.get("error")),
            )
        except shop.ShopError as exc:
            raise HTTPException(400, str(exc))

    @r.get("/api/tiktokshop/callback", response_class=HTMLResponse)
    @r.get(
        "/api/shop/oauth/callback", response_class=HTMLResponse, include_in_schema=False
    )
    async def callback(
        state: str | None = Query(default=None, max_length=256),
        code: str | None = Query(default=None, max_length=4096),
        error: str | None = Query(default=None, max_length=200),
    ):
        if state is None and code is None and error is None:
            return HTMLResponse(
                '<meta charset="utf-8"><p>TikTok Shop callback endpoint is ready. No authorization was accepted.</p>',
                headers=HEADERS,
            )
        if not state or len(state) < 32:
            raise HTTPException(400, "Shop 授权 state 缺失或无效")
        try:
            result = await shop.callback(state, code, denied=bool(error))
            text = (
                "卖家未完成授权，请从本机登录入口重试。"
                if result["status"] == "denied"
                else f"Shop 授权与店铺查询已成功，查到 {len(result['shops'])} 个店铺。请返回本机工作台。"
            )
            return HTMLResponse(
                '<meta charset="utf-8"><p>' + text + "</p>", headers=HEADERS
            )
        except shop.ShopError as exc:
            # Fixed adapter errors never contain token URLs or raw platform text.
            import html

            return HTMLResponse(
                '<meta charset="utf-8"><p>' + html.escape(str(exc)) + "</p>",
                status_code=400,
                headers=HEADERS,
            )

    @r.get("/api/shop/shops")
    async def shops(workspace=Depends(scope)):
        try:
            return await shop.shops(workspace)
        except shop.ShopError as exc:
            raise HTTPException(502, str(exc))

    @r.delete("/api/shop/connection")
    def disconnect(workspace=Depends(scope)):
        try:
            return shop.disconnect(workspace)
        except shop.ShopError as exc:
            raise HTTPException(400, str(exc))

    return r
