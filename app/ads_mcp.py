"""TikTok Ads MCP profile; existing authorization records remain compatible."""

import re
import os
import httpx
from app.mcp_connection import OAuthMCP, TokenStorage, MCPError

AdsError = MCPError
client = OAuthMCP(
    "ads",
    "https://business-api.tiktok.com/open_mcp/tt-ads-mcp-flat",
    "mcp:tt4b",
    "Shop AI Ads Read-only Workbench",
    "ADS_TOKEN_ENCRYPTION_KEY",
    "ADS_OAUTH_REDIRECT_URI",
    "TikTok Ads",
)
server_url = client.server_url
cipher = client.cipher
seal = client.seal
unseal = client.unseal
connection = client.connection
public_status = client.public_status
locked_connection = client.locked_connection
redirect_uri = client.redirect_uri
session = client.session
begin_connection = client.begin_connection
authorize = client.authorize
accept_callback = client.accept_callback
disconnect = client.disconnect


class Storage(TokenStorage):
    def __init__(self, shop, generation):
        super().__init__(client, shop, generation)


WRITE_WORDS = re.compile(
    r"(^|_)(create|update|delete|remove|set|upload|enable|disable|publish|execute|call|revoke|grant|transfer|payment|recharge|appeal)(_|$)",
    re.I,
)
READ_WORDS = re.compile(r"(^|_)(get|list|query|search|report|fetch|read)(_|$)", re.I)


def permitted(tool):
    name = tool["name"]
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
        return False
    # Never trust a model to classify tool safety; generic dispatchers are excluded.
    if WRITE_WORDS.search(name) or not READ_WORDS.search(name):
        return False
    if any(
        x in name.lower()
        for x in ("tool", "schema", "discovery", "endpoint", "http", "api_request")
    ):
        return False
    configured = {
        s.strip()
        for s in os.getenv("ADS_READ_TOOL_ALLOWLIST", "").split(",")
        if s.strip()
    }
    annotations = tool.get("annotations") or {}
    return annotations.get("readOnlyHint") is True or name in configured


async def tools(mcp):
    result = []
    cursor = None
    for _ in range(20):
        page = await mcp.list_tools(cursor=cursor)
        result.extend(t.model_dump(mode="json", by_alias=True) for t in page.tools)
        cursor = page.nextCursor
        if not cursor:
            return result
    raise AdsError("工具列表分页过多，停止加载")


def bind_account(arguments, schema, advertiser_id, start_date=None, end_date=None):
    def verify(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "advertiser_id" and str(item) != advertiser_id:
                    raise AdsError("工具参数引用了其他广告账户")
                if key == "advertiser_ids" and item != [advertiser_id]:
                    raise AdsError("仅允许查询选定广告账户")
                verify(item)
        elif isinstance(value, list):
            for item in value:
                verify(item)

    arguments = dict(arguments)
    properties = schema.get("properties", {})
    if "advertiser_id" in properties:
        arguments["advertiser_id"] = advertiser_id
    if "advertiser_ids" in properties:
        arguments["advertiser_ids"] = [advertiser_id]
    if start_date and "start_date" in properties:
        arguments["start_date"] = start_date
    if end_date and "end_date" in properties:
        arguments["end_date"] = end_date
    verify(arguments)
    return arguments
