"""Resolve names from the current grant, then read account-owned timezone data."""

import json
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from jsonschema import Draft202012Validator
from app import ads_mcp as ads
from app.mcp_results import payload, success
from app.api_gateway import isolated


def normalize(value):
    return "".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def matches(accounts, selector):
    needle = normalize(selector)
    exact = [
        a
        for a in accounts
        if needle in (normalize(a["advertiser_id"]), normalize(a["name"]))
    ]
    return exact or (
        [a for a in accounts if needle in normalize(a["name"])] if needle else []
    )


@isolated('ads.accounts')
async def inspect(shop, selector=None):
    evidence = []
    async with ads.session(shop) as mcp:
        available = {t["name"]: t for t in await ads.tools(mcp)}

        async def call(name, arguments):
            tool = available.get(name)
            if not tool or not ads.permitted(tool):
                raise ads.AdsError("当前授权未提供允许使用的账户查询工具：" + name)
            try:
                Draft202012Validator(tool["inputSchema"]).validate(arguments)
            except Exception:
                raise ads.AdsError(
                    "官方账户查询工具参数已变化，请检查工具定义"
                ) from None
            result = (await mcp.call_tool(name, arguments)).model_dump(
                mode="json", by_alias=True
            )
            if len(json.dumps(result).encode()) > 2_000_000:
                raise ads.AdsError("账户查询返回过大，请缩小范围")
            if not success(result):
                raise ads.AdsError("账户查询未成功，不能据此匹配账户或时区")
            evidence.append(
                {
                    "evidence_id": "A" + str(len(evidence) + 1),
                    "tool": name,
                    "arguments": arguments,
                    "result": result,
                    "api_success": True,
                    "retrieved_at": time.time(),
                }
            )
            body = payload(result)
            if "data" not in body:
                raise ads.AdsError("官方账户查询未返回可识别的数据")
            return body["data"]

        data = await call("auth_advertiser_get", {})
        rows = data.get("list") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ads.AdsError("授权广告账户列表格式无法识别")
        accounts = []
        for row in rows:
            if isinstance(row, dict) and str(row.get("advertiser_id", "")).isdigit():
                accounts.append(
                    {
                        "advertiser_id": str(row["advertiser_id"]),
                        "name": row.get("advertiser_name")
                        or row.get("name")
                        or str(row["advertiser_id"]),
                    }
                )
        result = {
            "accounts": accounts,
            "candidates": [],
            "selected": None,
            "evidence": evidence,
        }
        if selector is None:
            return result
        candidates = matches(accounts, selector)
        result["candidates"] = candidates
        if len(candidates) != 1:
            return result
        selected = dict(candidates[0])
        data = await call(
            "advertiser_info_get",
            {
                "advertiser_ids": [selected["advertiser_id"]],
                "fields": ["advertiser_id", "name", "timezone", "currency"],
            },
        )
        rows = data.get("list") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ads.AdsError("广告账户时区返回格式无法识别")
        info = next(
            (
                r
                for r in rows
                if isinstance(r, dict)
                and str(r.get("advertiser_id")) == selected["advertiser_id"]
            ),
            None,
        )
        if info is None:
            raise ads.AdsError("官方未返回所选广告账户的信息")
        selected.update(timezone=info.get("timezone"), currency=info.get("currency"))
        result["selected"] = selected
        return result


def dates(arguments, account_timezone, now=None):
    relative = arguments.get("relative_date")
    start, end = arguments.get("start_date"), arguments.get("end_date")
    if relative and (start or end):
        raise ValueError("请使用相对日期或明确起止日期中的一种")
    if relative:
        offsets = {"today": 0, "yesterday": 1, "day_before_yesterday": 2}
        if relative not in offsets:
            raise ValueError("相对日期不支持，请指定明确日期")
        try:
            tz = ZoneInfo(account_timezone)
        except (ZoneInfoNotFoundError, TypeError, ValueError):
            raise ValueError("未能确认广告账户时区，请提供明确的起止日期") from None
        current = now or datetime.now(timezone.utc)
        day = current.astimezone(tz).date() - timedelta(days=offsets[relative])
        return day.isoformat(), day.isoformat()
    if not start or not end:
        raise ValueError("请指定查询日期，例如昨天，或明确的起止日期")
    try:
        a, b = date.fromisoformat(start), date.fromisoformat(end)
        if a.isoformat() != start or b.isoformat() != end:
            raise ValueError()
        if not 0 <= (b - a).days <= 90:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError("日期应为 YYYY-MM-DD，起止顺序正确且跨度不超过90天") from None
    return start, end
