"""Shared OAuth transport with isolated Ads and Social credentials."""

import asyncio
import hashlib
import json
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse
from uuid import uuid4
import httpx
from cryptography.fernet import Fernet, InvalidToken
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientMetadata, OAuthToken, OAuthClientInformationFull
from sqlalchemy import select, delete
from app import store
from app.model import ModelError


class MCPError(Exception):
    pass


class RequestedScopeProvider(OAuthClientProvider):
    """Pin consent scopes: MCP SDK 1.30 discovery otherwise selects every scope."""

    def __init__(self, *args, requested_scope, **kwargs):
        super().__init__(*args, **kwargs)
        self.requested_scope = requested_scope

    async def _perform_authorization_code_grant(self):
        # Registration metadata may advertise available scopes; only this subset
        # is requested from the user. Keep this override covered on SDK upgrades.
        self.context.client_metadata.scope = self.requested_scope
        return await super()._perform_authorization_code_grant()


class OAuthMCP:
    def __init__(
        self,
        prefix,
        url,
        scope,
        client_name,
        key_env,
        redirect_env,
        label,
        *,
        pin_scope=False,
        authorization_timeout=360,
    ):
        self.prefix, self.url, self.scope = prefix, url, scope
        self.pin_scope = pin_scope
        self.authorization_timeout = authorization_timeout
        self.client_name, self.key_env, self.redirect_env, self.label = (
            client_name,
            key_env,
            redirect_env,
            label,
        )

    def kind(self, suffix):
        return self.prefix + "_" + suffix

    def server_url(self):
        return self.url

    def cipher(self):
        key = os.getenv(self.key_env, "") or os.getenv("ADS_TOKEN_ENCRYPTION_KEY", "")
        if not key:
            raise MCPError("请先运行 python -m scripts.setup_ads，生成本机令牌加密密钥")
        try:
            return Fernet(key.encode())
        except (ValueError, TypeError):
            raise MCPError(self.label + " 令牌加密密钥格式无效")

    def seal(self, value):
        return self.cipher().encrypt(json.dumps(value).encode()).decode()

    def unseal(self, value):
        try:
            return json.loads(self.cipher().decrypt(value.encode()))
        except (InvalidToken, ValueError):
            raise MCPError(
                "无法解密 " + self.label + " 授权，请使用原加密密钥或重新授权"
            )

    def connection(self, shop):
        return store.get(self.prefix + ":" + shop, self.kind("connection"), shop)

    def public_status(self, shop):
        item = self.connection(shop)
        if not item:
            return {
                "status": "disconnected",
                "server": self.server_url(),
                "read_only": True,
            }
        return {
            k: item.get(k) for k in ("status", "server", "connected_at", "error")
        } | {"read_only": True}

    def locked_connection(self, conn, shop, generation):
        conn.execute(
            select(store.records.c.id)
            .where(
                store.records.c.kind == self.kind("connection"),
                store.records.c.id == self.prefix + ":" + shop,
                store.records.c.shop == shop,
            )
            .with_for_update()
        ).first()
        current = store.get(
            self.prefix + ":" + shop, self.kind("connection"), shop, conn
        )
        if not current or current["generation"] != generation:
            raise MCPError("连接已变化，请重新操作")
        return current

    def redirect_uri(self):
        uri = os.getenv(
            self.redirect_env,
            "http://127.0.0.1:8000/api/" + self.prefix + "/oauth/callback",
        )
        parsed = urlparse(uri)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
        ):
            raise MCPError("回调地址须为 HTTPS 或本机回环地址")
        if parsed.query or parsed.fragment or parsed.username:
            raise MCPError("回调地址不可包含查询参数、片段或用户信息")
        return uri

    @asynccontextmanager
    async def session(self, shop, generation=None, attempt_id=None):
        item = self.connection(shop)
        if not item or (not attempt_id and item["status"] != "connected"):
            raise MCPError("请先连接 " + self.label)
        if item.get("server") != self.url:
            raise MCPError("授权服务地址不匹配，请重新授权")
        generation = generation or item["generation"]

        async def redirect_handler(url):
            if not attempt_id:
                raise MCPError(self.label + " 授权已失效，请重新连接")
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname not in (
                "business-api.tiktok.com",
                "ads.tiktok.com",
                "business.tiktok.com",
            ):
                raise MCPError("授权地址不是受支持的 TikTok 官方地址")
            state = parse_qs(parsed.query).get("state", [None])[0]
            if self.pin_scope and set(
                parse_qs(parsed.query).get("scope", [""])[0].split()
            ) != set(self.scope.split()):
                raise MCPError("授权请求权限与配置不一致，请检查 MCP SDK 版本")
            if not state:
                raise MCPError("授权服务未提供 state 校验参数")
            with store.db(write=True) as conn:
                current = self.locked_connection(conn, shop, generation)
                attempt = store.get(attempt_id, self.kind("attempt"), shop, conn)
                if not attempt or attempt["expires_at"] < time.time():
                    raise MCPError("授权会话已过期")
                attempt["authorization_url"] = url
                attempt["state_hash"] = hashlib.sha256(state.encode()).hexdigest()
                store.put(conn, self.kind("attempt"), attempt, shop)
                # This index contains no authorization code or tokens.
                store.put(
                    conn,
                    self.kind("state"),
                    {
                        "id": attempt["state_hash"],
                        "shop": shop,
                        "attempt_id": attempt_id,
                        "expires_at": attempt["expires_at"],
                    },
                    "_oauth",
                )

        async def callback_handler():
            deadline = time.monotonic() + self.authorization_timeout
            while time.monotonic() < deadline:
                current = self.connection(shop)
                if not current or current["generation"] != generation:
                    raise MCPError("授权已取消")
                attempt = store.get(attempt_id, self.kind("attempt"), shop)
                if not attempt or attempt["expires_at"] < time.time():
                    raise MCPError("授权会话已过期，请重试")
                if attempt.get("callback"):
                    data = self.unseal(attempt["callback"])
                    return data["code"], data["state"]
                await asyncio.sleep(1)
            raise MCPError("等待授权超时，请重新连接")

        provider = RequestedScopeProvider if self.pin_scope else OAuthClientProvider
        auth = provider(
            **({"requested_scope": self.scope} if self.pin_scope else {}),
            server_url=self.server_url(),
            client_metadata=OAuthClientMetadata(
                redirect_uris=[self.redirect_uri()],
                client_name=self.client_name,
                token_endpoint_auth_method="none",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope=self.scope,
            ),
            storage=TokenStorage(self, shop, generation),
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
            timeout=self.authorization_timeout,
        )
        body_error = None
        try:
            async with asyncio.timeout(
                self.authorization_timeout + 60 if attempt_id else 300
            ):
                async with httpx.AsyncClient(
                    auth=auth, timeout=30, follow_redirects=False
                ) as client:
                    async with streamable_http_client(
                        self.server_url(), http_client=client
                    ) as (read, write, _):
                        async with ClientSession(read, write) as mcp:
                            await mcp.initialize()
                            try:
                                yield mcp
                            except (MCPError, ModelError) as exc:
                                # MCP task groups may wrap caller exceptions during
                                # cleanup; retain safe domain errors for the API.
                                body_error = exc
                                raise
        except MCPError:
            raise
        except TimeoutError:
            raise MCPError(
                "授权等待已超时，请从登录入口重新开始，并在同一浏览器完成"
                if attempt_id
                else "TikTok MCP 查询等待超时，请缩小范围后重试"
            ) from None
        except Exception:
            if body_error is not None:
                raise body_error from None
            # Do not reflect exception groups: HTTP errors may contain sensitive URLs.
            raise MCPError(
                "TikTok MCP 连接或授权失败，请检查网络、授权状态及回调地址"
            ) from None

    def begin_connection(self, shop):
        self.cipher()
        self.redirect_uri()
        generation = str(uuid4())
        attempt_id = str(uuid4())
        with store.db(write=True) as conn:
            old = store.get(
                self.prefix + ":" + shop, self.kind("connection"), shop, conn
            )
            if old and old["status"] == "connecting":
                pending = store.get(
                    old.get("attempt_id", ""), self.kind("attempt"), shop, conn
                )
                if pending and pending["expires_at"] > time.time():
                    return pending, False
            client_data = {}
            if old and old.get("sealed") and old.get("server") == self.url:
                cached = self.unseal(old["sealed"]).get("client")
                if cached and self.redirect_uri() in cached.get("redirect_uris", []):
                    client_data = {
                        "client": cached,
                        "_service": self.prefix,
                        "_server": self.url,
                    }
            item = {
                "id": self.prefix + ":" + shop,
                "generation": generation,
                "server": self.server_url(),
                "status": "connecting",
                "sealed": self.seal(client_data) if client_data else None,
                "attempt_id": attempt_id,
            }
            store.put(conn, self.kind("connection"), item, shop)
            attempt = {
                "id": attempt_id,
                "generation": generation,
                "expires_at": time.time() + self.authorization_timeout,
                "authorization_url": None,
                "callback": None,
            }
            store.put(conn, self.kind("attempt"), attempt, shop)
        return attempt, True

    async def authorize(self, shop, attempt_id, generation):
        try:
            async with self.session(shop, generation, attempt_id):
                pass
            with store.db(write=True) as conn:
                item = self.locked_connection(conn, shop, generation)
                item.update(status="connected", connected_at=time.time(), error=None)
                store.put(conn, self.kind("connection"), item, shop)
        except Exception as exc:
            with store.db(write=True) as conn:
                item = store.get(
                    self.prefix + ":" + shop, self.kind("connection"), shop, conn
                )
                if item and item["generation"] == generation:
                    item.update(
                        status="error",
                        error=(
                            str(exc)
                            if isinstance(exc, MCPError)
                            else "连接未完成；请检查网络与回调地址后重新授权"
                        ),
                    )
                    store.put(conn, self.kind("connection"), item, shop)
        finally:
            with store.db(write=True) as conn:
                attempt = store.get(attempt_id, self.kind("attempt"), shop, conn)
                if attempt:
                    attempt["callback"] = None
                    attempt["authorization_url"] = None
                    store.put(conn, self.kind("attempt"), attempt, shop)
                    if attempt.get("state_hash"):
                        conn.execute(
                            delete(store.records).where(
                                store.records.c.kind == self.kind("state"),
                                store.records.c.id == attempt["state_hash"],
                            )
                        )

    def accept_callback(self, state, code=None, denied=False):
        state_hash = hashlib.sha256(state.encode()).hexdigest()
        with store.db(write=True) as conn:
            conn.execute(
                select(store.records.c.id)
                .where(
                    store.records.c.kind == self.kind("state"),
                    store.records.c.id == state_hash,
                )
                .with_for_update()
            ).first()
            lookup = store.get(state_hash, self.kind("state"), "_oauth", conn)
            if not lookup or lookup["expires_at"] < time.time():
                raise MCPError("授权 state 无效、已使用或已过期")
            shop = lookup["shop"]
            attempt = store.get(lookup["attempt_id"], self.kind("attempt"), shop, conn)
            if not attempt or attempt.get("callback"):
                raise MCPError("授权回调已处理")
            current = self.locked_connection(conn, shop, attempt["generation"])
            if denied:
                attempt["expires_at"] = 0
                current.update(status="error", error="授权未完成，可以重新连接")
                store.put(conn, self.kind("connection"), current, shop)
            else:
                attempt["callback"] = self.seal({"code": code, "state": state})
            store.put(conn, self.kind("attempt"), attempt, shop)
            conn.execute(
                delete(store.records).where(
                    store.records.c.kind == self.kind("state"),
                    store.records.c.id == state_hash,
                )
            )

    def disconnect(self, shop):
        # Local forget invalidates in-flight writers. Remote revocation is separate.
        with store.db(write=True) as conn:
            store.put(
                conn,
                self.kind("connection"),
                {
                    "id": self.prefix + ":" + shop,
                    "generation": str(uuid4()),
                    "status": "disconnected",
                    "server": self.server_url(),
                    "sealed": None,
                },
                shop,
            )
            for kind in (self.kind("attempt"),):
                conn.execute(
                    delete(store.records).where(
                        store.records.c.kind == kind, store.records.c.shop == shop
                    )
                )
        return self.public_status(shop)


class TokenStorage:
    def __init__(self, service, shop, generation):
        self.service = service
        self.shop, self.generation = shop, generation

    def read(self):
        item = self.service.connection(self.shop)
        if not item or item["generation"] != self.generation:
            raise MCPError("连接已断开")
        data = self.service.unseal(item["sealed"]) if item.get("sealed") else {}
        if data and (
            data.get(
                "_service",
                self.service.prefix if self.service.prefix == "ads" else None,
            )
            != self.service.prefix
            or data.get("_server", self.service.url) != self.service.url
        ):
            raise MCPError("授权凭据属于另一个服务，拒绝复用")
        return data

    def write(self, key, value):
        with store.db(write=True) as conn:
            item = self.service.locked_connection(conn, self.shop, self.generation)
            data = self.service.unseal(item["sealed"]) if item.get("sealed") else {}
            data["_service"] = self.service.prefix
            data["_server"] = self.service.url
            data[key] = value
            item["sealed"] = self.service.seal(data)
            store.put(conn, self.service.kind("connection"), item, self.shop)

    async def get_tokens(self):
        value = self.read().get("tokens")
        return OAuthToken.model_validate(value) if value else None

    async def set_tokens(self, tokens):
        self.write("tokens", tokens.model_dump(mode="json"))

    async def get_client_info(self):
        value = self.read().get("client")
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def set_client_info(self, client_info):
        self.write("client", client_info.model_dump(mode="json"))
