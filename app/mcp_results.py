"""Business success is distinct from successful MCP/HTTP transport."""

import json


def payload(result):
    value = result.get("structuredContent")
    if not isinstance(value, dict):
        blocks = result.get("content") or []
        if len(blocks) != 1 or blocks[0].get("type") != "text":
            return None
        try:
            value = json.loads(blocks[0]["text"])
        except (KeyError, TypeError, ValueError):
            return None
    return value if isinstance(value, dict) else None


def success(result):
    value = payload(result)
    return (
        not result.get("isError")
        and value is not None
        and str(value.get("code")) == "0"
    )
