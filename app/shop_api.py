"""TikTok Shop seller OAuth and a deliberately restricted read-only API client."""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4
import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select
from app import store, execution_context

TOKEN_ROOT = "https://auth.tiktok-shops.com/api/v2/token"
API_ROOT = "https://open-api.tiktokglobalshop.com"
CALLBACK_PATHS = ("/api/tiktokshop/callback", "/api/shop/oauth/callback")
SELLER_HOSTS = {"services.tiktokshop.com", "services.us.tiktokshop.com"}


class ShopError(Exception):
    pass


class ShopAPIError(ShopError):
    def __init__(self, code, request_id=None):
        self.code = code
        self.request_id = request_id
        labels = {
            105001: "店铺授权无效，请重新授权",
            105002: "店铺令牌已过期",
            105005: "店铺权限不足，请核对应用权限和卖家授权",
            106001: "Shop 请求签名验证失败",
        }
        super().__init__(
            labels.get(code, "TikTok Shop 业务请求失败")
            + f"（code={code}）"
            + (f"，request_id={request_id}" if request_id else "")
        )


class HideShopSecrets(logging.Filter):
    def filter(self, record):
        text = record.getMessage()
        return not any(
            part in text
            for part in (*CALLBACK_PATHS, "auth.tiktok-shops.com", "/api/v2/token/", "shop_cipher")
        )


for logger in ("uvicorn.access", "httpx", "httpcore"):
    logging.getLogger(logger).addFilter(HideShopSecrets())


def config(oauth=False):
    app_key = os.getenv("SHOP_APP_KEY", "").strip()
    app_secret = os.getenv("SHOP_APP_SECRET", "").strip()
    if not app_key or not app_secret:
        raise ShopError("请在本机 .env 填写 SHOP_APP_KEY 和 SHOP_APP_SECRET")
    binding = hashlib.sha256(
        json.dumps(["tiktok_shop", app_key, API_ROOT]).encode()
    ).hexdigest()
    cfg = {
        "app_key": app_key,
        "app_secret": app_secret,
        "binding": binding,
        "redirect_uri": None,
    }
    if not oauth:
        return cfg
    copied = os.getenv("SHOP_AUTHORIZATION_URL", "").strip()
    service_id = os.getenv("SHOP_SERVICE_ID", "").strip()
    region = os.getenv("SHOP_AUTH_REGION", "ROW").strip().upper()
    if region not in ("US", "ROW"):
        raise ShopError("SHOP_AUTH_REGION 仅支持 US 或 ROW")
    host = "services.us.tiktokshop.com" if region == "US" else "services.tiktokshop.com"
    if copied:
        try:
            parsed = urlsplit(copied)
            port = parsed.port
        except ValueError:
            raise ShopError("Shop 授权链接格式无效") from None
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in SELLER_HOSTS
            or parsed.path.rstrip("/") != "/open/authorize"
            or port not in (None, 443)
            or parsed.username
            or parsed.fragment
        ):
            raise ShopError("SHOP_AUTHORIZATION_URL 必须是官方卖家授权链接")
        if (
            set(query) - {"service_id", "state"}
            or len(query.get("service_id", [])) != 1
        ):
            raise ShopError(
                "授权链接必须包含唯一 service_id，不能填写授权回调或令牌链接"
            )
        link_id = query["service_id"][0]
        if service_id and service_id != link_id:
            raise ShopError("授权链接与 SHOP_SERVICE_ID 不一致")
        service_id, host = link_id, parsed.hostname
    if not re.fullmatch(r"[0-9]{1,40}", service_id):
        raise ShopError("新授权需要 SHOP_AUTHORIZATION_URL 或 SHOP_SERVICE_ID")
    redirect = os.getenv("SHOP_OAUTH_REDIRECT_URI", "").strip()
    try:
        parsed = urlsplit(redirect)
    except ValueError:
        raise ShopError("Shop 回调地址格式无效") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.query
        or parsed.fragment
        or parsed.path not in CALLBACK_PATHS
    ):
        raise ShopError(
            "SHOP_OAUTH_REDIRECT_URI 须为已注册的 HTTPS 回调，路径为 /api/tiktokshop/callback"
        )
    cfg.update(
        service_id=service_id,
        host=host,
        redirect_uri=redirect,
        oauth_binding=hashlib.sha256(
            json.dumps([binding, service_id, host, redirect]).encode()
        ).hexdigest(),
    )
    return cfg


def cipher():
    key = os.getenv("SHOP_TOKEN_ENCRYPTION_KEY", "") or os.getenv(
        "ADS_TOKEN_ENCRYPTION_KEY", ""
    )
    try:
        return Fernet(key.encode())
    except (TypeError, ValueError):
        raise ShopError("Shop 令牌加密密钥缺失或无效，请先配置本机加密密钥") from None


def seal(value):
    return cipher().encrypt(json.dumps(value).encode()).decode()


def unseal(value, cfg):
    try:
        data = json.loads(cipher().decrypt(value.encode()))
    except (InvalidToken, ValueError, TypeError):
        raise ShopError("无法解密 Shop 授权，请使用原加密密钥或重新授权") from None
    if data.get("service") != "tiktok_shop" or data.get("binding") != cfg["binding"]:
        raise ShopError("凭据不属于当前 Shop 应用，拒绝复用")
    return data


def connection(shop, conn=None):
    return store.get("shop:" + shop, "shop_connection", shop, conn)


def locked(conn, shop, generation, cfg):
    conn.execute(
        select(store.records.c.id)
        .where(
            store.records.c.id == "shop:" + shop,
            store.records.c.kind == "shop_connection",
            store.records.c.shop == shop,
        )
        .with_for_update()
    ).first()
    item = connection(shop, conn)
    if (
        not item
        or item["generation"] != generation
        or item.get("binding") != cfg["binding"]
    ):
        raise ShopError("Shop 连接已变化，请重新操作")
    return item


def status(shop):
    try:
        cfg = config()
    except ShopError as exc:
        return {
            "status": "not_configured",
            "configured": False,
            "error": str(exc),
            "read_only": True,
        }
    try:
        oauth_cfg = config(oauth=True)
        cfg["redirect_uri"] = oauth_cfg["redirect_uri"]
        oauth_ready = True
    except ShopError:
        oauth_ready = False
    item = connection(shop)
    if item and item.get("binding") != cfg["binding"]:
        return {
            "status": "needs_reauthorization",
            "configured": True,
            "error": "Shop 应用已变化，请导入相应应用令牌或重新授权",
            "read_only": True,
        }
    return {
        "status": item.get("status", "disconnected") if item else "disconnected",
        "configured": True,
        "read_only": True,
        "oauth_ready": oauth_ready,
        "redirect_uri": cfg["redirect_uri"],
        "shop_count": item.get("shop_count", 0) if item else 0,
        "access_token_expires_at": (
            item.get("access_token_expires_at") if item else None
        ),
        "refresh_token_expires_at": (
            item.get("refresh_token_expires_at") if item else None
        ),
        "last_verified_at": item.get("last_verified_at") if item else None,
        "last_request_id": item.get("last_request_id") if item else None,
        "error": item.get("error") if item else None,
    }


def begin(shop):
    cfg = config(oauth=True)
    cipher()
    state = secrets.token_urlsafe(32)
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    expires_at = time.time() + 1800
    generation = str(uuid4())
    url = (
        "https://"
        + cfg["host"]
        + "/open/authorize?"
        + urlencode({"service_id": cfg["service_id"], "state": state})
    )
    with store.db(write=True) as conn:
        store.put(
            conn,
            "shop_connection",
            {
                "id": "shop:" + shop,
                "generation": generation,
                "binding": cfg["binding"],
                "status": "connecting",
                "sealed": None,
                "error": None,
            },
            shop,
        )
        store.put(
            conn,
            "shop_oauth_state",
            {
                "id": state_hash,
                "target_shop": shop,
                "generation": generation,
                "binding": cfg["oauth_binding"],
                "expires_at": expires_at,
            },
            "_oauth",
        )
    return {
        "authorization_url": url,
        "expires_at": expires_at,
        "redirect_uri": cfg["redirect_uri"],
    }


def sign(path, params, body, app_secret):
    # Sign exactly the query values and body bytes sent by the HTTP client.
    if not isinstance(body, bytes):
        raise ShopError("签名请求体必须是实际发送的字节")
    message = (
        path.encode()
        + "".join(
            str(k) + str(params[k])
            for k in sorted(params)
            if k not in ("sign", "access_token")
        ).encode()
        + body
    )
    secret = app_secret.encode()
    return hmac.new(secret, secret + message + secret, hashlib.sha256).hexdigest()


async def request_json(url, *, params, headers=None, method="GET", content=None):
    # URLs are fixed by the adapter, never supplied by a model or API caller.
    execution_context.remote_call(urlsplit(url).path)
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.request(
                method, url, params=params, headers=headers, content=content
            )
            if response.status_code != 200:
                raise ShopError(
                    f"TikTok Shop 服务返回 HTTP {response.status_code}，请核对网络、应用和授权"
                )
            if len(response.content) > 2_000_000:
                raise ShopError("TikTok Shop 返回数据过大")
            value = response.json()
    except (httpx.HTTPError, ValueError):
        # Never reflect HTTP exceptions: token APIs put app_secret in the URL.
        raise ShopError("TikTok Shop 网络或返回格式异常，请稍后重试") from None
    if not isinstance(value, dict) or type(value.get("code")) is not int:
        raise ShopError("TikTok Shop 未返回有效业务状态码")
    if value["code"] != 0:
        rid = str(value.get("request_id", ""))
        raise ShopAPIError(
            value["code"], rid if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", rid) else None
        )
    if not isinstance(value.get("data"), dict):
        raise ShopError("TikTok Shop 未返回有效业务数据")
    return value


def validate_tokens(data, previous=None):
    if type(data.get("user_type")) is not int or data["user_type"] not in (0, 4, 5):
        raise ShopError("返回的不是卖家或跨境卖家令牌，不能用于 Shop 店铺查询")
    for key in ("access_token", "refresh_token", "open_id"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ShopError("Shop 令牌返回缺少必要字段")
    for key in ("access_token_expire_in", "refresh_token_expire_in"):
        if (
            type(data.get(key)) not in (int, float)
            or not time.time() < data[key] < 100_000_000_000
        ):
            raise ShopError("Shop 令牌到期时间不是有效的 Unix 时间戳")
    if not isinstance(data.get("granted_scopes", []), list) or not all(
        isinstance(s, str) for s in data.get("granted_scopes", [])
    ):
        raise ShopError("Shop 权限列表格式无效")
    if previous and (
        (previous.get("open_id") is not None and data["open_id"] != previous["open_id"])
        or (
            previous.get("user_type") is not None
            and data["user_type"] != previous["user_type"]
        )
    ):
        raise ShopError("刷新返回的卖家身份已变化，请重新授权")
    return {
        k: data[k]
        for k in (
            "access_token",
            "refresh_token",
            "access_token_expire_in",
            "refresh_token_expire_in",
            "open_id",
            "user_type",
            "granted_scopes",
            "seller_name",
            "seller_base_region",
        )
        if k in data
    }


def store_tokens(conn, shop, item, cfg, tokens, shops=None):
    item.update(
        sealed=seal(
            {
                "service": "tiktok_shop",
                "binding": cfg["binding"],
                "tokens": tokens,
                "shops": shops or [],
            }
        ),
        access_token_expires_at=tokens["access_token_expire_in"],
        refresh_token_expires_at=tokens["refresh_token_expire_in"],
        error=None,
    )
    store.put(conn, "shop_connection", item, shop)


async def callback(state, code=None, denied=False):
    cfg = config(oauth=True)
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    with store.db(write=True) as conn:
        conn.execute(
            select(store.records.c.id)
            .where(
                store.records.c.id == state_hash,
                store.records.c.kind == "shop_oauth_state",
            )
            .with_for_update()
        ).first()
        pending = store.get(state_hash, "shop_oauth_state", "_oauth", conn)
        if (
            not pending
            or pending["expires_at"] < time.time()
            or pending["binding"] != cfg["oauth_binding"]
        ):
            raise ShopError("Shop 授权 state 无效、已过期或已使用，请重新开始")
        shop, generation = pending["target_shop"], pending["generation"]
        item = locked(conn, shop, generation, cfg)
        conn.execute(
            delete(store.records).where(
                store.records.c.id == state_hash,
                store.records.c.kind == "shop_oauth_state",
            )
        )
        item.update(
            status="denied" if denied else "exchanging",
            error="卖家未完成授权" if denied else None,
        )
        store.put(conn, "shop_connection", item, shop)
    if denied:
        return {"status": "denied", "shop_count": 0}
    if not code or code == "null":
        mark_error(shop, generation, cfg, "授权回调缺少有效 code", "error")
        raise ShopError("授权回调缺少有效 code，请重新授权")
    try:
        value = await request_json(
            TOKEN_ROOT + "/get",
            params={
                "app_key": cfg["app_key"],
                "app_secret": cfg["app_secret"],
                "auth_code": code,
                "grant_type": "authorized_code",
            },
        )
        tokens = validate_tokens(value["data"])
        with store.db(write=True) as conn:
            item = locked(conn, shop, generation, cfg)
            item.update(status="authorized")
            store_tokens(conn, shop, item, cfg, tokens)
    except ShopError as exc:
        mark_error(shop, generation, cfg, str(exc), "error")
        raise
    # Only a successful signed shops request establishes a verified connection.
    return await shops(shop)


def mark_error(shop, generation, cfg, message, state=None):
    with store.db(write=True) as conn:
        current = connection(shop, conn)
        if (
            current
            and current["generation"] == generation
            and current.get("binding") == cfg["binding"]
        ):
            current["error"] = message
            if state:
                current["status"] = state
            store.put(conn, "shop_connection", current, shop)


async def access(shop, cfg, force=False):
    deadline = time.monotonic() + 40
    expected_generation = None
    forced_revision = None
    while True:
        current = connection(shop)
        if (
            not current
            or current["status"] not in ("authorized", "connected")
            or current.get("binding") != cfg["binding"]
            or not current.get("sealed")
        ):
            raise ShopError("请先完成本项目的 TikTok Shop 卖家授权")
        generation = current["generation"]
        if expected_generation is not None and expected_generation != generation:
            raise ShopError("Shop 连接已变化，请重新查询")
        expected_generation = generation
        if force and forced_revision is None:
            forced_revision = current.get("token_revision", 0)
        data = unseal(current["sealed"], cfg)
        tokens = data["tokens"]
        if (not force or current.get("token_revision", 0) > forced_revision) and (
            tokens.get("access_token_expire_in") is None
            or tokens["access_token_expire_in"] > time.time() + 120
        ):
            return tokens, generation
        if not tokens.get("refresh_token") or (
            tokens.get("refresh_token_expire_in") is not None
            and tokens["refresh_token_expire_in"] <= time.time()
        ):
            mark_error(
                shop,
                generation,
                cfg,
                "Shop 访问令牌已失效，且没有可用刷新令牌；请重新导入有效令牌或授权",
                "needs_reauthorization",
            )
            raise ShopError(
                "Shop 访问令牌已失效，且没有可用刷新令牌；请重新导入有效令牌或授权"
            )
        owner = str(uuid4())
        acquired = False
        with store.db(write=True) as conn:
            item = locked(conn, shop, generation, cfg)
            latest = unseal(item["sealed"], cfg)["tokens"]
            if (not force or item.get("token_revision", 0) > forced_revision) and (
                latest.get("access_token_expire_in") is None
                or latest["access_token_expire_in"] > time.time() + 120
            ):
                return latest, generation
            if item.get("refresh_lease_until", 0) <= time.time():
                item.update(refresh_owner=owner, refresh_lease_until=time.time() + 35)
                store.put(conn, "shop_connection", item, shop)
                acquired = True
        if acquired:
            try:
                value = await request_json(
                    TOKEN_ROOT + "/refresh",
                    params={
                        "app_key": cfg["app_key"],
                        "app_secret": cfg["app_secret"],
                        "refresh_token": tokens["refresh_token"],
                        "grant_type": "refresh_token",
                    },
                )
                updated = validate_tokens(value["data"], tokens)
                with store.db(write=True) as conn:
                    item = locked(conn, shop, generation, cfg)
                    if item.get("refresh_owner") != owner:
                        raise ShopError("Shop 刷新流程已变化，请重试")
                    item["token_revision"] = item.get("token_revision", 0) + 1
                    store_tokens(conn, shop, item, cfg, updated, data.get("shops"))
                return updated, generation
            except ShopError as exc:
                mark_error(shop, generation, cfg, str(exc))
                raise
            finally:
                with store.db(write=True) as conn:
                    item = connection(shop, conn)
                    if (
                        item
                        and item["generation"] == generation
                        and item.get("refresh_owner") == owner
                    ):
                        item.update(refresh_owner=None, refresh_lease_until=0)
                        store.put(conn, "shop_connection", item, shop)
        if time.monotonic() >= deadline:
            raise ShopError("另一个 Shop 刷新任务仍在运行，请稍后重试")
        await asyncio.sleep(0.2)


async def shops(shop):
    cfg = config()
    for attempt in range(2):
        tokens, generation = await access(shop, cfg, force=bool(attempt))
        params = {"app_key": cfg["app_key"], "timestamp": str(int(time.time()))}
        params["sign"] = sign(
            "/authorization/202309/shops", params, b"", cfg["app_secret"]
        )
        try:
            value = await request_json(
                API_ROOT + "/authorization/202309/shops",
                params=params,
                headers={
                    "x-tts-access-token": tokens["access_token"],
                    "Content-Type": "application/json",
                },
            )
            break
        except ShopAPIError as exc:
            if exc.code == 105002 and attempt == 0:
                continue
            mark_error(shop, generation, cfg, str(exc))
            raise
        except ShopError as exc:
            mark_error(shop, generation, cfg, str(exc))
            raise
    rows = value["data"].get("shops")
    if not isinstance(rows, list) or not all(
        isinstance(r, dict)
        and isinstance(r.get("id"), str)
        and isinstance(r.get("cipher"), str)
        and r["id"]
        and r["cipher"]
        for r in rows
    ):
        raise ShopError("授权店铺返回格式无效，尚未验证店铺访问")
    with store.db(write=True) as conn:
        item = locked(conn, shop, generation, cfg)
        # Keep any token refreshed by another caller instead of overwriting it.
        latest = unseal(item["sealed"], cfg)
        item.update(
            status="connected",
            shop_count=len(rows),
            last_verified_at=time.time(),
            last_request_id=value.get("request_id"),
            error=None,
        )
        store_tokens(conn, shop, item, cfg, latest["tokens"], rows)
    return {
        "status": "connected",
        "data_verified": True,
        "read_only": True,
        "request_id": value.get("request_id"),
        "retrieved_at": time.time(),
        "shops": [
            {k: r.get(k) for k in ("id", "name", "region", "seller_type", "code")}
            for r in rows
        ],
    }


def disconnect(shop):
    with store.db(write=True) as conn:
        store.put(
            conn,
            "shop_connection",
            {
                "id": "shop:" + shop,
                "generation": str(uuid4()),
                "binding": config()["binding"],
                "status": "disconnected",
                "sealed": None,
            },
            shop,
        )
        rows = conn.execute(
            select(store.records.c.id, store.records.c.payload).where(
                store.records.c.kind == "shop_oauth_state",
                store.records.c.shop == "_oauth",
            )
        ).all()
        for key, value in rows:
            if json.loads(value).get("target_shop") == shop:
                conn.execute(
                    delete(store.records).where(
                        store.records.c.id == key,
                        store.records.c.kind == "shop_oauth_state",
                    )
                )
    return status(shop)


async def import_existing(shop):
    """Verify an existing app-specific token before replacing any saved grant."""
    cfg = config()
    cipher()
    access_token = os.getenv("SHOP_ACCESS_TOKEN", "").strip()
    refresh_token = os.getenv("SHOP_REFRESH_TOKEN", "").strip()
    if not access_token and not refresh_token:
        raise ShopError(
            "请将原系统的 Shop access_token 或 refresh_token 填入本机 .env；App Key/Secret 不能代替令牌"
        )
    old = connection(shop)
    original_generation = old["generation"] if old else None
    candidate = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "access_token_expire_in": None,
        "refresh_token_expire_in": None,
        "open_id": None,
        "user_type": None,
        "granted_scopes": [],
    }

    async def refresh():
        if not refresh_token:
            raise ShopError(
                "现有访问令牌已失效且未提供刷新令牌，请从原系统取得有效令牌或重新授权"
            )
        response = await request_json(
            TOKEN_ROOT + "/refresh",
            params={
                "app_key": cfg["app_key"],
                "app_secret": cfg["app_secret"],
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        return validate_tokens(response["data"])

    if not access_token:
        candidate = await refresh()
    for attempt in range(2):
        params = {"app_key": cfg["app_key"], "timestamp": str(int(time.time()))}
        params["sign"] = sign(
            "/authorization/202309/shops", params, b"", cfg["app_secret"]
        )
        try:
            response = await request_json(
                API_ROOT + "/authorization/202309/shops",
                params=params,
                headers={
                    "x-tts-access-token": candidate["access_token"],
                    "Content-Type": "application/json",
                },
            )
            break
        except ShopAPIError as exc:
            if attempt == 0 and exc.code in (105001, 105002) and refresh_token:
                candidate = await refresh()
                continue
            raise
    rows = response["data"].get("shops")
    if not isinstance(rows, list) or not all(
        isinstance(r, dict)
        and isinstance(r.get("id"), str)
        and isinstance(r.get("cipher"), str)
        and r["id"]
        and r["cipher"]
        for r in rows
    ):
        raise ShopError("现有令牌没有返回有效店铺列表，未保存为已连接")
    with store.db(write=True) as conn:
        current = connection(shop, conn)
        if (current["generation"] if current else None) != original_generation:
            raise ShopError("导入期间 Shop 连接已变化，未覆盖新连接")
        item = {
            "id": "shop:" + shop,
            "generation": str(uuid4()),
            "binding": cfg["binding"],
            "status": "connected",
            "source": "existing_token",
            "shop_count": len(rows),
            "last_request_id": response.get("request_id"),
            "last_verified_at": time.time(),
        }
        store_tokens(conn, shop, item, cfg, candidate, rows)
    return {
        "status": "connected",
        "source": "existing_token",
        "data_verified": True,
        "read_only": True,
        "request_id": response.get("request_id"),
        "shops": [
            {k: r.get(k) for k in ("id", "name", "region", "seller_type", "code")}
            for r in rows
        ],
        "expiry_known": candidate["access_token_expire_in"] is not None,
    }
