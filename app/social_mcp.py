"""TikTok Social MCP: independent TikTok-user OAuth and read-only tools."""

import asyncio
import os
import hashlib
import time
from contextlib import asynccontextmanager
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from jsonschema import Draft202012Validator
from app import store
from app.ads_mcp import tools
from app.mcp_connection import OAuthMCP, MCPError, TokenStorage

SocialError = MCPError
client = OAuthMCP(
    "social",
    "https://business-api.tiktok.com/open_mcp/tt-ads-mcp-organic-social",
    "user.info.basic user.info.username user.info.stats user.info.profile user.account.type user.insights video.list video.insights comment.list discovery.search.words",
    "Shop AI TikTok Social Read-only Agent",
    "SOCIAL_TOKEN_ENCRYPTION_KEY",
    "SOCIAL_OAUTH_REDIRECT_URI",
    "TikTok Social",
)
server_url = client.server_url
connection = client.connection
begin_connection = client.begin_connection
authorize = client.authorize
accept_callback = client.accept_callback


def disconnect(shop):
    store.save({"id": "social-api:" + shop, "disabled": True}, "social_api_check", shop)
    client.disconnect(shop)
    return public_status(shop)


def auth_mode():
    value = os.getenv("SOCIAL_AUTH_MODE", "oauth")
    if value not in ("oauth", "api_token"):
        raise SocialError("SOCIAL_AUTH_MODE 仅支持 oauth 或 api_token")
    return value


def public_status(shop):
    if auth_mode() == "oauth":
        return {**client.public_status(shop), "auth_mode": "oauth"}
    token = os.getenv("SOCIAL_API_ACCESS_TOKEN", "").strip()
    verified = store.get("social-api:" + shop, "social_api_check", shop)
    fingerprint = hashlib.sha256(token.encode()).hexdigest() if token else None
    if verified and verified.get("disabled"):
        return {
            "status": "disconnected",
            "server": server_url(),
            "auth_mode": "api_token",
            "read_only": True,
        }
    state = (
        "connected"
        if token and verified and verified.get("fingerprint") == fingerprint
        else "configured" if token else "disconnected"
    )
    return {
        "status": state,
        "server": server_url(),
        "auth_mode": "api_token",
        "read_only": True,
    }


@asynccontextmanager
async def session(shop, allow_disabled=False):
    if auth_mode() == "oauth":
        async with client.session(shop) as mcp:
            yield mcp
        return
    setting = store.get("social-api:" + shop, "social_api_check", shop)
    if setting and setting.get("disabled") and not allow_disabled:
        raise SocialError("Social API token 连接已断开，请重新连接")
    token = os.getenv("SOCIAL_API_ACCESS_TOKEN", "").strip()
    if not token:
        raise SocialError("尚未在本机配置 Organic API 用户 access token")
    try:
        async with asyncio.timeout(120):
            async with httpx.AsyncClient(
                headers={"Authorization": "Bearer " + token}, timeout=30
            ) as http:
                async with streamable_http_client(server_url(), http_client=http) as (
                    read,
                    write,
                    _,
                ):
                    async with ClientSession(read, write) as mcp:
                        await mcp.initialize()
                        yield mcp
    except SocialError:
        raise
    except Exception:
        raise SocialError(
            "Social API token 连接失败，请检查用户令牌、权限及有效期"
        ) from None


async def verify_api(shop):
    if auth_mode() != "api_token":
        raise SocialError("当前使用 OAuth，请先完成浏览器授权")
    async with session(shop, allow_disabled=True) as mcp:
        found = await tools(mcp)
    fingerprint = hashlib.sha256(
        os.environ["SOCIAL_API_ACCESS_TOKEN"].strip().encode()
    ).hexdigest()
    store.save(
        {
            "id": "social-api:" + shop,
            "fingerprint": fingerprint,
            "verified_at": time.time(),
        },
        "social_api_check",
        shop,
    )
    return {**public_status(shop), "tool_count": len(found)}


# Exact read operations from the five official documents. Writes are not exposed.
READ_TOOLS = frozenset("""account_user_get account_user_post_list_get
account_user_benchmark_get account_user_post_setting_get account_user_comment_list_get
account_user_comment_reply_list_get account_user_publish_status_get
account_user_hashtag_suggestion_get account_user_post_authorize_status_get
discovery_cml_list_get discovery_cml_post_list_get discovery_search
discovery_search_recommend_get account_user_mention_post_list_get
account_user_mention_post_get account_user_mention_top_word_list_get
account_user_mention_top_hashtag_list_get account_user_mention_hashtag_video_list_get
account_user_mention_comment_list_get account_user_mention_comment_get
account_user_mention_hashtag_verify_list_get account_user_mention_hashtag_manage_list_get
account_user_post_recommend_get""".split())


def permitted(tool):
    annotations = tool.get("annotations") or {}
    return tool["name"] in READ_TOOLS and annotations.get("readOnlyHint") is not False


def validate_arguments(tool, arguments):
    if not permitted(tool):
        raise SocialError("此 Social 工具不属于已确认的只读工具，已阻止调用")
    try:
        Draft202012Validator(tool["inputSchema"]).validate(arguments)
    except Exception:
        raise SocialError("Social 工具参数不符合官方定义，请查看工具参数表") from None


async def call(mcp, available, name, arguments):
    tool = next((t for t in available if t["name"] == name), None)
    if tool is None:
        raise SocialError("当前授权未提供这个 Social 工具")
    validate_arguments(tool, arguments)
    result = (await mcp.call_tool(name, arguments)).model_dump(
        mode="json", by_alias=True
    )
    import json

    if len(json.dumps(result).encode()) > 2_000_000:
        raise SocialError("Social 返回数据过大，请缩小查询范围")
    return result


def success(result):
    import json

    if result.get("isError"):
        return False
    value = result.get("structuredContent")
    if not isinstance(value, dict):
        blocks = result.get("content") or []
        if len(blocks) != 1 or blocks[0].get("type") != "text":
            return False
        try:
            value = json.loads(blocks[0]["text"])
        except (ValueError, KeyError):
            return False
    return isinstance(value, dict) and str(value.get("code")) == "0"
