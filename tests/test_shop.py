import asyncio
import hashlib
import json
import logging
import time
from urllib.parse import parse_qs, urlsplit
from contextlib import asynccontextmanager
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.main import app
from app import shop_api as shop, store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_TOKEN", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "shop.sqlite"))
    for key, value in {
        "SHOP_APP_KEY": "fake-key",
        "SHOP_APP_SECRET": "fake-secret",
        "SHOP_SERVICE_ID": "111",
        "SHOP_AUTHORIZATION_URL": "https://services.tiktokshop.com/open/authorize?service_id=111",
        "SHOP_OAUTH_REDIRECT_URI": "https://example.test/api/tiktokshop/callback",
        "SHOP_AUTH_REGION": "ROW",
        "SHOP_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    }.items():
        monkeypatch.setenv(key, value)
    return TestClient(app)


def tokens(**changes):
    return {
        "access_token": "fake-access",
        "refresh_token": "fake-refresh",
        "open_id": "fake-seller",
        "user_type": 0,
        "access_token_expire_in": int(time.time()) + 604800,
        "refresh_token_expire_in": int(time.time()) + 864000,
        "granted_scopes": ["fake-only"],
        **changes,
    }


def provider(monkeypatch, fail_shops=False, on_token=None):
    seen = []

    def handler(request):
        seen.append(request)
        params = dict(request.url.params)
        if request.url.path == "/api/v2/token/get":
            assert params["grant_type"] == "authorized_code"
            assert params["app_secret"] == "fake-secret"
            if on_token:
                on_token()
            return httpx.Response(
                200, json={"code": 0, "data": tokens(), "request_id": "fake-token"}
            )
        if request.url.path == "/api/v2/token/refresh":
            assert params["grant_type"] == "refresh_token"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": tokens(
                        access_token="fake-refreshed", refresh_token="fake-rotated"
                    ),
                    "request_id": "fake-refresh",
                },
            )
        assert request.url.path == "/authorization/202309/shops"
        assert "access_token" not in params and "app_secret" not in params
        assert request.headers["x-tts-access-token"] in (
            "fake-access",
            "fake-refreshed",
        )
        assert params["sign"] == shop.sign(request.url.path, params, b"", "fake-secret")
        return httpx.Response(
            200,
            json={
                "code": 105005 if fail_shops else 0,
                "request_id": "fake-shops",
                "data": {
                    "shops": [
                        {
                            "id": "123",
                            "name": "Fake shop",
                            "region": "MY",
                            "seller_type": "LOCAL",
                            "cipher": "fake-shop-cipher",
                        }
                    ]
                },
            },
        )

    actual = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: actual(transport=httpx.MockTransport(handler), **kw),
    )
    return seen


def begin(client):
    r = client.post("/api/shop/connect")
    assert r.status_code == 202, r.text
    return parse_qs(urlsplit(r.json()["authorization_url"]).query)["state"][0]


def test_official_signature_vector_and_exact_body():
    params = {"app_key": "29a39d", "timestamp": "1623812664"}
    expected = "b596b73e0cc6de07ac26f036364178ab16b0a907af13d43f0a0cd2345f582dc8"
    assert (
        shop.sign("/authorization/202309/shops", params, b"", "e59af819cc") == expected
    )
    assert (
        shop.sign(
            "/authorization/202309/shops",
            {**params, "access_token": "excluded", "sign": "excluded"},
            b"",
            "e59af819cc",
        )
        == expected
    )
    assert shop.sign("/p", {}, b'{"a":1}', "secret") != shop.sign(
        "/p", {}, b'{"a": 1}', "secret"
    )


def test_full_seller_callback_encrypted_credentials_and_signed_shop_query(
    client, monkeypatch
):
    seen = provider(monkeypatch)
    state = begin(client)
    assert client.get("/api/shop/connection").json()["status"] == "connecting"
    response = client.get(
        "/api/tiktokshop/callback", params={"state": state, "code": "fake-code"}
    )
    assert response.status_code == 200, response.text
    status = client.get("/api/shop/connection").json()
    assert status["status"] == "connected" and status["shop_count"] == 1
    assert "fake-access" not in json.dumps(status)
    r = client.get("/api/shop/shops")
    assert r.status_code == 200, r.text
    assert r.json()["shops"][0]["name"] == "Fake shop"
    assert "cipher" not in r.json()["shops"][0]
    with store.db() as conn:
        raw = " ".join(conn.execute(select(store.records.c.payload)).scalars())
    for value in (
        "fake-access",
        "fake-refresh",
        "fake-secret",
        "fake-code",
        "fake-shop-cipher",
    ):
        assert value not in raw
    count = len(seen)
    assert (
        client.get(
            "/api/tiktokshop/callback", params={"state": state, "code": "fake-code"}
        ).status_code
        == 400
    )
    assert len(seen) == count


def test_deny_and_expired_state_do_not_exchange(client, monkeypatch):
    seen = provider(monkeypatch)
    state = begin(client)
    assert (
        client.get(
            "/api/tiktokshop/callback",
            params={"state": state, "code": "null", "error": "auth_denied"},
        ).status_code
        == 200
    )
    assert seen == []
    assert client.get("/api/shop/connection").json()["status"] == "denied"
    state = begin(client)
    key = hashlib.sha256(state.encode()).hexdigest()
    pending = store.get(key, "shop_oauth_state", "_oauth")
    pending["expires_at"] = 0
    store.save(pending, "shop_oauth_state", "_oauth")
    assert (
        client.get(
            "/api/tiktokshop/callback", params={"state": state, "code": "fake-code"}
        ).status_code
        == 400
    )
    assert seen == []


def test_grant_alone_is_not_verified_shop_access(client, monkeypatch):
    provider(monkeypatch, fail_shops=True)
    state = begin(client)
    response = client.get(
        "/api/tiktokshop/callback", params={"state": state, "code": "fake-code"}
    )
    assert response.status_code == 400
    assert client.get("/api/shop/connection").json()["status"] == "authorized"
    assert client.get("/api/shop/shops").status_code == 502


def test_disconnect_during_exchange_cannot_restore_credentials(client, monkeypatch):
    provider(monkeypatch, on_token=lambda: shop.disconnect("default"))
    state = begin(client)
    assert (
        client.get(
            "/api/tiktokshop/callback", params={"state": state, "code": "fake-code"}
        ).status_code
        == 400
    )
    assert shop.connection("default")["sealed"] is None
    assert client.get("/api/shop/connection").json()["status"] == "disconnected"


def test_expired_access_refreshes_without_new_browser_grant(client, monkeypatch):
    seen = provider(monkeypatch)
    state = begin(client)
    assert (
        client.get(
            "/api/tiktokshop/callback", params={"state": state, "code": "fake-code"}
        ).status_code
        == 200
    )
    cfg = shop.config()
    item = shop.connection("default")
    data = shop.unseal(item["sealed"], cfg)
    data["tokens"]["access_token_expire_in"] = time.time() - 1
    item["sealed"] = shop.seal(data)
    store.save(item, "shop_connection", "default")
    r = client.get("/api/shop/shops")
    assert r.status_code == 200, r.text
    assert sum(x.url.path.endswith("/refresh") for x in seen) == 1
    assert (
        shop.unseal(shop.connection("default")["sealed"], cfg)["tokens"][
            "refresh_token"
        ]
        == "fake-rotated"
    )


def test_callback_public_probe_and_api_token_guard(client, monkeypatch):
    monkeypatch.setenv("APP_TOKEN", "fake-workbench-token")
    r = client.get("/api/tiktokshop/callback")
    assert (
        r.status_code == 200
        and r.headers["x-shop-callback"] == "tiktok-shop-agent-v0.6"
    )
    assert client.get("/api/shop/connection").status_code == 401
    assert (
        client.get("/api/tiktokshop/callback", params={"code": "fake-code"}).status_code
        == 400
    )


def test_configuration_binding_and_workspace_isolation(client, monkeypatch):
    provider(monkeypatch)
    state = begin(client)
    assert (
        client.get(
            "/api/tiktokshop/callback", params={"state": state, "code": "fake-code"}
        ).status_code
        == 200
    )
    assert (
        client.get("/api/shop/shops", headers={"X-Shop-ID": "another"}).status_code
        == 502
    )
    assert (
        client.get("/api/shop/connection", headers={"X-Shop-ID": "another"}).json()[
            "status"
        ]
        == "disconnected"
    )
    monkeypatch.setenv("SHOP_APP_KEY", "different-app")
    assert (
        client.get("/api/shop/connection").json()["status"] == "needs_reauthorization"
    )
    assert client.get("/api/shop/shops").status_code == 502


def test_creator_identity_and_credential_logs_rejected(client):
    with pytest.raises(shop.ShopError):
        shop.validate_tokens(tokens(user_type=1))
    with pytest.raises(shop.ShopError):
        shop.validate_tokens(tokens(access_token_expire_in=604800))
    for text in [
        "GET /api/tiktokshop/callback?code=private",
        "HTTP Request: GET https://auth.tiktok-shops.com/api/v2/token/get?app_secret=private",
    ]:
        assert not shop.HideShopSecrets().filter(
            logging.LogRecord("httpx", logging.INFO, "", 0, text, (), None)
        )


def test_existing_access_token_import_needs_no_redirect_or_service_id(
    client, monkeypatch
):
    seen = provider(monkeypatch)
    monkeypatch.setenv("SHOP_ACCESS_TOKEN", "fake-access")
    monkeypatch.setenv("SHOP_REFRESH_TOKEN", "")
    for key in ("SHOP_SERVICE_ID", "SHOP_AUTHORIZATION_URL", "SHOP_OAUTH_REDIRECT_URI"):
        monkeypatch.delenv(key, raising=False)
    r = client.post("/api/shop/import-token")
    assert r.status_code == 200, r.text
    assert r.json()["data_verified"] and not r.json()["expiry_known"]
    assert [x.url.path for x in seen] == ["/authorization/202309/shops"]
    assert client.get("/api/shop/shops").status_code == 200
    assert not client.get("/api/shop/connection").json()["oauth_ready"]


def test_existing_refresh_token_import_needs_no_browser(client, monkeypatch):
    seen = provider(monkeypatch)
    monkeypatch.setenv("SHOP_ACCESS_TOKEN", "")
    monkeypatch.setenv("SHOP_REFRESH_TOKEN", "fake-refresh")
    r = client.post("/api/shop/import-token")
    assert r.status_code == 200, r.text
    assert r.json()["expiry_known"]
    assert [x.url.path for x in seen] == [
        "/api/v2/token/refresh",
        "/authorization/202309/shops",
    ]


def test_manual_callback_validation_does_not_fetch_supplied_url(client, monkeypatch):
    seen = provider(monkeypatch)
    state = begin(client)
    from urllib.parse import urlencode

    link = "https://example.test/api/tiktokshop/callback?" + urlencode(
        {"state": state, "code": "fake-code"}
    )
    assert (
        client.post(
            "/api/shop/complete-redirect",
            json={"callback_url": link},
            headers={"X-Shop-ID": "another"},
        ).status_code
        == 400
    )
    assert seen == []
    assert (
        client.post(
            "/api/shop/complete-redirect",
            json={"callback_url": link.replace("example.test", "attacker.test")},
        ).status_code
        == 400
    )
    assert seen == []
    r = client.post("/api/shop/complete-redirect", json={"callback_url": link})
    assert r.status_code == 200, r.text
    assert r.json()["data_verified"]
    assert all(
        x.url.host in ("auth.tiktok-shops.com", "open-api.tiktokglobalshop.com")
        for x in seen
    )
    assert (
        client.post(
            "/api/shop/complete-redirect", json={"callback_url": link}
        ).status_code
        == 400
    )
