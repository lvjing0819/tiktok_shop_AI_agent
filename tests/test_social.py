import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from mcp.types import ListToolsResult, Tool, CallToolResult
from mcp.shared.auth import OAuthToken
from app import social_mcp as social, ads_mcp as ads, store, model
from app.main import app
from app.mcp_connection import TokenStorage


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_TOKEN", raising=False)
    monkeypatch.delenv("SOCIAL_TOKEN_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("SOCIAL_API_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.sqlite"))
    monkeypatch.setenv("ADS_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SOCIAL_AUTH_MODE", "oauth")
    monkeypatch.setenv(
        "SOCIAL_OAUTH_REDIRECT_URI", "http://127.0.0.1:8000/api/social/oauth/callback"
    )
    monkeypatch.setenv("MODEL_MODE", "bailian")
    return TestClient(app)


def test_credentials_service_isolation(client):
    a, _ = ads.begin_connection("default")
    b, _ = social.begin_connection("default")
    ast = ads.Storage("default", a["generation"])
    sst = TokenStorage(social.client, "default", b["generation"])
    asyncio.run(ast.set_tokens(OAuthToken(access_token="fake-ads")))
    asyncio.run(sst.set_tokens(OAuthToken(access_token="fake-social")))
    assert asyncio.run(ast.get_tokens()).access_token == "fake-ads"
    assert asyncio.run(sst.get_tokens()).access_token == "fake-social"
    client.delete("/api/social/connection")
    assert asyncio.run(ast.get_tokens()).access_token == "fake-ads"
    with pytest.raises(social.SocialError):
        asyncio.run(sst.get_tokens())


def test_ads_ciphertext_cannot_be_reused_by_social(client):
    a, _ = ads.begin_connection("default")
    b, _ = social.begin_connection("default")
    asyncio.run(
        ads.Storage("default", a["generation"]).set_tokens(
            OAuthToken(access_token="ads-secret")
        )
    )
    record = social.connection("default")
    record["sealed"] = ads.connection("default")["sealed"]
    store.save(record, "social_connection", "default")
    with pytest.raises(social.SocialError):
        asyncio.run(
            TokenStorage(social.client, "default", b["generation"]).get_tokens()
        )


def test_cross_callback_and_replay(client):
    b, _ = social.begin_connection("default")
    state = "social-state-1234567890"
    hashed = hashlib.sha256(state.encode()).hexdigest()
    store.save(
        {
            "id": hashed,
            "shop": "default",
            "attempt_id": b["id"],
            "expires_at": time.time() + 60,
        },
        "social_state",
        "_oauth",
    )
    assert (
        client.get(
            "/api/ads/oauth/callback", params={"state": state, "code": "social-code"}
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/api/social/oauth/callback", params={"state": state, "code": "social-code"}
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/social/oauth/callback", params={"state": state, "code": "social-code"}
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/api/social/attempts/" + b["id"], headers={"X-Shop-ID": "other"}
        ).status_code
        == 404
    )


def test_writes_blocked_before_connection(client):
    for name in [
        "account_user_post_publish",
        "account_user_photo_publish",
        "account_user_comment_create",
        "account_user_comment_reply_create",
        "account_user_comment_delete",
        "account_user_mention_hashtag_add",
    ]:
        assert (
            client.post("/api/social/tools/call", json={"name": name}).status_code
            == 403
        )
    assert social.permitted(
        {
            "name": "account_user_publish_status_get",
            "annotations": {"readOnlyHint": True},
        }
    )
    assert not social.permitted(
        {"name": "account_user_get", "annotations": {"readOnlyHint": False}}
    )
    assert not social.permitted(
        {"name": "unknown_get", "annotations": {"readOnlyHint": True}}
    )


class FakeSocial:
    def __init__(self):
        self.calls = []

    async def list_tools(self, cursor=None):
        return ListToolsResult(
            tools=[
                Tool(
                    name="account_user_hashtag_suggestion_get",
                    description="Get recommended hashtags",
                    inputSchema={
                        "type": "object",
                        "properties": {"keyword": {"type": "string"}},
                        "required": ["keyword"],
                    },
                    annotations={"readOnlyHint": True},
                ),
                Tool(
                    name="account_user_post_publish",
                    inputSchema={"type": "object"},
                    annotations={"readOnlyHint": False},
                ),
            ]
        )

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return CallToolResult(
            content=[
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "code": 0,
                            "request_id": "fake-only",
                            "data": {"suggestions": [{"name": "watches"}]},
                        }
                    ),
                }
            ]
        )


def use_fake(monkeypatch, fake):
    @asynccontextmanager
    async def session(*args, **kwargs):
        yield fake

    monkeypatch.setattr(social, "session", session)


def test_direct_query_schema_and_evidence(client, monkeypatch):
    fake = FakeSocial()
    use_fake(monkeypatch, fake)
    assert (
        client.post(
            "/api/social/tools/call",
            json={"name": "account_user_hashtag_suggestion_get", "arguments": {}},
        ).status_code
        == 502
    )
    assert fake.calls == []
    r = client.post(
        "/api/social/tools/call",
        json={
            "name": "account_user_hashtag_suggestion_get",
            "arguments": {"keyword": "watch"},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["read_only"]
    assert social.success(r.json()["result"])
    assert fake.calls == [("account_user_hashtag_suggestion_get", {"keyword": "watch"})]


def test_social_agent_tool_loop(client, monkeypatch):
    fake = FakeSocial()
    use_fake(monkeypatch, fake)

    def reply(messages, tools):
        assert all(t["function"]["name"] != "account_user_post_publish" for t in tools)
        if messages[-1]["role"] == "tool":
            return {"role": "assistant", "content": "依据 S1，可以考虑 watches 标签。"}
        return {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "account_user_hashtag_suggestion_get",
                        "arguments": '{"keyword":"watch"}',
                    },
                }
            ],
        }

    monkeypatch.setattr(model, "message", reply)
    r = client.post("/api/social/query", json={"question": "查找手表相关标签"})
    assert r.status_code == 200, r.text
    assert r.json()["data_verified"] and r.json()["tool_calls"] == 1
    rid = r.json()["id"]
    assert client.get("/api/social/reports/" + rid).status_code == 200
    assert (
        client.get(
            "/api/social/reports/" + rid, headers={"X-Shop-ID": "other"}
        ).status_code
        == 404
    )


def test_agent_blocks_hallucinated_publish(client, monkeypatch):
    fake = FakeSocial()
    use_fake(monkeypatch, fake)
    monkeypatch.setattr(
        model,
        "message",
        lambda *a: {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "c1",
                    "function": {
                        "name": "account_user_post_publish",
                        "arguments": "{}",
                    },
                }
            ],
        },
    )
    assert (
        client.post("/api/social/query", json={"question": "发布视频"}).status_code
        == 502
    )
    assert fake.calls == []


def test_api_mode_never_marks_config_as_verified(client, monkeypatch):
    monkeypatch.setenv("SOCIAL_AUTH_MODE", "api_token")
    monkeypatch.setenv("SOCIAL_API_ACCESS_TOKEN", "fake-organic")
    assert client.get("/api/social/connection").json()["status"] == "configured"
    fake = FakeSocial()
    use_fake(monkeypatch, fake)
    r = client.post("/api/social/connect")
    assert r.status_code == 202 and r.json()["status"] == "connected"
    assert "fake-organic" not in json.dumps(
        store.list_records("social_api_check", "default")
    )
    monkeypatch.setenv("SOCIAL_API_ACCESS_TOKEN", "different-token")
    assert client.get("/api/social/connection").json()["status"] == "configured"


def test_legacy_ads_credentials_still_readable(client):
    a, _ = ads.begin_connection("default")
    record = ads.connection("default")
    record["sealed"] = ads.seal(
        {"tokens": {"access_token": "legacy", "token_type": "Bearer"}, "client": None}
    )
    store.save(record, "ads_connection", "default")
    assert (
        asyncio.run(ads.Storage("default", a["generation"]).get_tokens()).access_token
        == "legacy"
    )


def test_result_error_not_verified(client):
    assert not social.success(
        {
            "isError": False,
            "content": [
                {"type": "text", "text": '{"code":40001,"message":"permission denied"}'}
            ],
        }
    )


def test_sdk_social_dcr_pkce_and_client_id(client, monkeypatch):
    base = social.server_url()
    issued = []
    published_scopes = social.client.scope.split() + [
        "video.publish",
        "video.upload",
        "comment.list.manage",
        "biz.spark.auth",
    ]

    def handler(request):
        path = request.url.path
        if "oauth-protected-resource" in path:
            return httpx.Response(
                200,
                json={
                    "resource": base,
                    "authorization_servers": [base + "/oauth"],
                    "scopes_supported": published_scopes,
                },
            )
        if "oauth-authorization-server" in path or "openid-configuration" in path:
            return httpx.Response(
                200,
                json={
                    "issuer": base + "/oauth",
                    "authorization_endpoint": base + "/oauth/tiktok/authorize",
                    "token_endpoint": base + "/oauth/tiktok/token",
                    "registration_endpoint": base + "/oauth/tiktok/register",
                    "response_types_supported": ["code"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    "scopes_supported": published_scopes,
                },
            )
        if path.endswith("/oauth/tiktok/register"):
            body = json.loads(request.content)
            assert body["redirect_uris"] == [
                "http://127.0.0.1:8000/api/social/oauth/callback"
            ]
            return httpx.Response(201, json={**body, "client_id": "fake-social-client"})
        if path.endswith("/oauth/tiktok/token"):
            body = parse_qs(request.content.decode())
            assert body["client_id"] == ["fake-social-client"]
            assert len(body["code_verifier"][0]) >= 43
            issued.append(1)
            return httpx.Response(
                200,
                json={
                    "access_token": "fake-social-token",
                    "refresh_token": "fake-social-refresh",
                    "token_type": "Bearer",
                    "scope": social.client.scope,
                    "expires_in": 86400,
                    "refresh_token_expires_in": 31536000,
                },
            )
        if request.headers.get("authorization") != "Bearer fake-social-token":
            return httpx.Response(
                401,
                headers={
                    "www-authenticate": 'Bearer resource_metadata="https://business-api.tiktok.com/.well-known/oauth-protected-resource/open_mcp/tt-ads-mcp-organic-social"'
                },
            )
        if request.method == "GET":
            return httpx.Response(405)
        if request.method == "DELETE":
            return httpx.Response(200)
        body = json.loads(request.content)
        if body["method"].startswith("notifications/"):
            return httpx.Response(202)
        if body["method"] == "initialize":
            result = {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-social", "version": "1"},
            }
        elif body["method"] == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "account_user_get",
                        "inputSchema": {"type": "object"},
                        "annotations": {"readOnlyHint": True},
                    }
                ]
            }
        else:
            result = {"content": [{"type": "text", "text": '{"code":0,"data":{}}'}]}
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    actual = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: actual(transport=httpx.MockTransport(handler), **kwargs),
    )
    a, _ = social.begin_connection("default")

    async def flow():
        task = asyncio.create_task(
            social.authorize("default", a["id"], a["generation"])
        )
        try:
            url = None
            for _ in range(100):
                p = store.get(a["id"], "social_attempt", "default")
                if p.get("authorization_url"):
                    url = p["authorization_url"]
                    break
                if task.done():
                    break
                await asyncio.sleep(0.02)
            assert url
            params = parse_qs(urlparse(url).query)
            assert params["code_challenge_method"] == ["S256"]
            assert params["resource"] == [base]
            assert set(params["scope"][0].split()) == set(social.client.scope.split())
            assert "video.publish" not in params["scope"][0]
            social.accept_callback(params["state"][0], "fake-social-code")
            await asyncio.wait_for(task, 5)
            assert social.public_status("default")["status"] == "connected"
            assert ads.public_status("default")["status"] == "disconnected"
            async with social.session("default") as mcp:
                assert (await social.tools(mcp))[0]["name"] == "account_user_get"
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(flow())
    assert issued == [1]


def test_api_disconnect_stops_calls(client, monkeypatch):
    monkeypatch.setenv("SOCIAL_AUTH_MODE", "api_token")
    monkeypatch.setenv("SOCIAL_API_ACCESS_TOKEN", "fake-env-token")
    assert client.delete("/api/social/connection").json()["status"] == "disconnected"

    async def check():
        with pytest.raises(social.SocialError):
            async with social.session("default"):
                pytest.fail("disconnected session opened")

    asyncio.run(check())


def test_login_redirect_uses_current_attempt(client):
    a, _ = social.begin_connection("default")
    a["authorization_url"] = (
        "https://business-api.tiktok.com/open_mcp/tt-ads-mcp-organic-social/oauth/tiktok/authorize?state=test-state"
    )
    store.save(a, "social_attempt", "default")
    response = client.get("/api/social/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == a["authorization_url"]
    assert response.headers["cache-control"] == "no-store"
