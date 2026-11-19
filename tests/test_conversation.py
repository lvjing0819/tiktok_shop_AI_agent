import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from mcp.types import CallToolResult, ListToolsResult, Tool
from app import ads_accounts, ads_mcp, model, social_agent, store
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_TOKEN", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "conversation.sqlite"))
    monkeypatch.setenv("MODEL_MODE", "bailian")
    return TestClient(app)


def choice(name, args):
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "routing-only",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


class FakeAds:
    def __init__(self):
        self.calls = []
        self.fail_report = False

    async def list_tools(self, cursor=None):
        definitions = {
            "auth_advertiser_get": ({}, []),
            "advertiser_info_get": (
                {
                    "advertiser_ids": {"type": "array", "items": {"type": "string"}},
                    "fields": {"type": "array", "items": {"type": "string"}},
                },
                ["advertiser_ids"],
            ),
            "gmv_max_report_get": (
                {
                    "advertiser_id": {"type": "string"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                },
                ["advertiser_id", "start_date", "end_date"],
            ),
        }
        return ListToolsResult(
            tools=[
                Tool(
                    name=k,
                    inputSchema={"type": "object", "properties": p, "required": r},
                    annotations={"readOnlyHint": True},
                )
                for k, (p, r) in definitions.items()
            ]
        )

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "auth_advertiser_get":
            data = {
                "list": [
                    {"advertiser_id": "123", "advertiser_name": "示例马来账户"},
                    {"advertiser_id": "456", "advertiser_name": "示例泰国账户"},
                ]
            }
        elif name == "advertiser_info_get":
            data = {
                "list": [
                    {
                        "advertiser_id": args["advertiser_ids"][0],
                        "name": "示例马来账户",
                        "timezone": "America/Los_Angeles",
                        "currency": "USD",
                    }
                ]
            }
        else:
            data = {"list": [{"metrics": {"cost": "10.90", "net_cost": "10.89"}}]}
        return CallToolResult(
            content=[],
            structuredContent={
                "code": (
                    40001 if self.fail_report and name == "gmv_max_report_get" else 0
                ),
                "message": "fake-only",
                "request_id": "fake-only",
                "data": data,
            },
        )


def fake_ads(monkeypatch):
    fake = FakeAds()

    @asynccontextmanager
    async def session(*args, **kwargs):
        yield fake

    monkeypatch.setattr(ads_mcp, "session", session)
    real_dates = ads_accounts.dates
    monkeypatch.setattr(
        ads_accounts,
        "dates",
        lambda a, tz: real_dates(
            a, tz, datetime(2026, 11, 19, 0, 30, tzinfo=timezone.utc)
        ),
    )
    return fake


def scripted_router(monkeypatch, decisions):
    pending = iter(decisions)

    def message(messages, tools):
        names = [t["function"]["name"] for t in tools]
        if "query_ads" in names:
            name, args = next(pending)
            return choice(name, args)
        if messages[-1]["role"] == "tool":
            return {"role": "assistant", "content": "依据 E1，样例消耗为10.90 USD。"}
        assert "gmv_max_report_get" in names
        return choice(
            "gmv_max_report_get",
            {
                "advertiser_id": "999",
                "start_date": "1999-01-01",
                "end_date": "1999-01-02",
            },
        )

    monkeypatch.setattr(model, "message", message)


def new_session(client):
    return client.post("/api/sessions", json={"title": "会话测试"}).json()["id"]


def send(client, sid, text):
    return client.post(f"/api/sessions/{sid}/messages", json={"content": text})


def test_ads_query_and_followup_resolve_live_account_and_timezone(client, monkeypatch):
    fake = fake_ads(monkeypatch)
    scripted_router(
        monkeypatch,
        [
            (
                "query_ads",
                {
                    "question": "GMV Max昨天消耗",
                    "account": "马来",
                    "relative_date": "yesterday",
                },
            ),
            (
                "query_ads",
                {
                    "question": "那前天呢",
                    "relative_date": "day_before_yesterday",
                    "reuse_previous_account": True,
                },
            ),
        ],
    )
    sid = new_session(client)
    first = send(client, sid, "查马来昨天GMV Max消耗")
    assert first.status_code == 200, first.text
    assert first.json()["query_context"]["last_ads"]["start_date"] == "2026-11-17"
    second = send(client, sid, "那前天呢")
    assert second.status_code == 200, second.text
    # UTC Nov 19 is still Nov 18 in this account; day-before-yesterday is Nov 16.
    assert second.json()["query_context"]["last_ads"]["start_date"] == "2026-11-16"
    calls = [a for n, a in fake.calls if n == "gmv_max_report_get"]
    assert [a["advertiser_id"] for a in calls] == ["123", "123"]
    assert [a["start_date"] for a in calls] == ["2026-11-17", "2026-11-16"]
    run_id = second.json()["last_run"]["id"]
    run = client.get(f"/api/sessions/{sid}/runs/{run_id}").json()
    assert run["data_verified"] and run["resolved_account"]["currency"] == "USD"
    assert len(run["evidence"]) == 3
    assert all(s["api_success"] for s in run["sources"])
    assert (
        client.get(
            f"/api/sessions/{sid}/runs/{run_id}", headers={"X-Shop-ID": "other"}
        ).status_code
        == 404
    )
    other = new_session(client)
    assert client.get(f"/api/sessions/{other}/runs/{run_id}").status_code == 404


def test_clarify_account_then_resume_original_question(client, monkeypatch):
    fake = fake_ads(monkeypatch)
    scripted_router(
        monkeypatch,
        [
            (
                "query_ads",
                {"question": "GMV Max昨天消耗", "relative_date": "yesterday"},
            ),
            ("query_ads", {"question": "选择马来账户", "account": "马来"}),
        ],
    )
    sid = new_session(client)
    first = send(client, sid, "查昨天GMV Max消耗").json()
    assert first["last_run"]["status"] == "needs_input"
    assert not first["last_run"]["data_verified"]
    assert "示例马来账户" in first["messages"][-1]["content"]
    assert not any(n == "gmv_max_report_get" for n, _ in fake.calls)
    second = send(client, sid, "马来").json()
    assert second["last_run"]["data_verified"]
    assert "GMV Max" in second["query_context"]["last_ads"]["question"]
    assert "pending_ads" not in second["query_context"]


def test_missing_date_and_explicit_date_replacement(client, monkeypatch):
    fake_ads(monkeypatch)
    scripted_router(
        monkeypatch,
        [
            ("query_ads", {"question": "GMV Max消耗", "account": "马来"}),
            (
                "query_ads",
                {
                    "question": "2026年11月10日至11日",
                    "start_date": "2026-11-10",
                    "end_date": "2026-11-11",
                },
            ),
        ],
    )
    sid = new_session(client)
    first = send(client, sid, "马来GMV Max消耗").json()
    assert first["last_run"]["status"] == "needs_input"
    second = send(client, sid, "11月10日至11日").json()
    assert second["query_context"]["last_ads"]["start_date"] == "2026-11-10"


def test_unknown_account_id_never_becomes_report_target(client, monkeypatch):
    fake = fake_ads(monkeypatch)
    scripted_router(
        monkeypatch,
        [
            (
                "query_ads",
                {
                    "question": "GMV Max消耗",
                    "account": "999",
                    "relative_date": "yesterday",
                },
            )
        ],
    )
    r = send(client, new_session(client), "查询999昨天GMV Max").json()
    assert r["last_run"]["status"] == "needs_input"
    assert [n for n, _ in fake.calls] == ["auth_advertiser_get"]


def test_name_ambiguity_and_date_validation():
    accounts = [
        {"advertiser_id": "1", "name": "品牌 TH1"},
        {"advertiser_id": "2", "name": "品牌 TH2"},
    ]
    assert len(ads_accounts.matches(accounts, "品牌")) == 2
    assert ads_accounts.matches(accounts, " 品牌ＴＨ２ ")[0]["advertiser_id"] == "2"
    with pytest.raises(ValueError):
        ads_accounts.dates({"relative_date": "yesterday"}, "not-a-timezone")
    with pytest.raises(ValueError):
        ads_accounts.dates(
            {"start_date": "2026-02-30", "end_date": "2026-03-01"}, "UTC"
        )
    with pytest.raises(ValueError):
        ads_accounts.dates(
            {"relative_date": "yesterday", "start_date": "2026-11-01"}, "UTC"
        )


def test_social_dispatch_receives_followup_history(client, monkeypatch):
    scripted_router(
        monkeypatch,
        [
            ("query_social", {"question": "查账号粉丝"}),
            ("query_social", {"question": "再看一下作品"}),
        ],
    )
    seen = []

    async def query(shop, question, conversation_context=None):
        seen.append(conversation_context)
        return {
            "id": "fake-report",
            "answer": "依据 S1，样例账号粉丝299。",
            "data_verified": True,
            "evidence": [
                {
                    "evidence_id": "S1",
                    "tool": "account_user_get",
                    "arguments": {},
                    "result": {
                        "structuredContent": {
                            "code": 0,
                            "request_id": "fake-only",
                            "data": {"followers_count": 299},
                        },
                        "isError": False,
                    },
                }
            ],
        }

    monkeypatch.setattr(social_agent, "query", query)
    sid = new_session(client)
    assert send(client, sid, "查账号粉丝").status_code == 200
    second = send(client, sid, "再看一下作品").json()
    assert second["last_run"]["route"] == "query_social"
    assert "粉丝" in json.dumps(seen[-1], ensure_ascii=False)
    assert seen[-1]["query_context"]["last_domain"] == "social"


def test_list_accounts_and_delete_session_removes_execution_records(
    client, monkeypatch
):
    fake_ads(monkeypatch)
    scripted_router(monkeypatch, [("list_ads_accounts", {})])
    sid = new_session(client)
    body = send(client, sid, "授权了哪些广告账户").json()
    rid = body["last_run"]["id"]
    assert body["last_run"]["data_verified"]
    assert len(client.get(f"/api/sessions/{sid}/runs").json()) == 1
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    assert store.get(rid, "conversation_run", "default") is None


@pytest.mark.parametrize(
    "reply",
    [
        choice("campaign_update", {}),
        {"role": "assistant", "content": "未经查询的数字"},
        choice("query_ads", {"question": "a", "unexpected": True}),
    ],
)
def test_bad_router_output_rolls_back_history(client, monkeypatch, reply):
    monkeypatch.setattr(model, "message", lambda *a: reply)
    sid = new_session(client)
    assert send(client, sid, "查询").status_code == 502
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["messages"] == [] and session["busy_until"] == 0
    assert store.list_records("conversation_run", "default") == []


def test_tiktok_business_error_is_not_a_successful_report(client, monkeypatch):
    fake = fake_ads(monkeypatch)
    fake.fail_report = True
    scripted_router(
        monkeypatch,
        [
            (
                "query_ads",
                {
                    "question": "GMV Max消耗",
                    "account": "马来",
                    "relative_date": "yesterday",
                },
            )
        ],
    )
    sid = new_session(client)
    assert send(client, sid, "查马来昨天GMV Max").status_code == 502
    assert client.get(f"/api/sessions/{sid}").json()["messages"] == []


def test_delete_while_query_runs_cannot_create_orphan_record(client, monkeypatch):
    sid = new_session(client)

    def reply(*args):
        store.remove(sid, "session", "default")
        return choice("reply", {"kind": "general", "message": "普通建议"})

    monkeypatch.setattr(model, "message", reply)
    assert send(client, sid, "你好").status_code == 409
    assert store.list_records("conversation_run", "default") == []


def test_missing_bailian_key_does_not_fake_query(client):
    sid = new_session(client)
    r = send(client, sid, "查账号粉丝")
    assert r.status_code == 502 and "尚未配置" in r.json()["detail"]


def test_demo_mode_stays_explicitly_offline(client, monkeypatch):
    monkeypatch.setenv("MODEL_MODE", "demo")
    sid = new_session(client)
    result = send(client, sid, "查账号粉丝").json()
    assert "未调用模型" in result["messages"][-1]["content"]
    assert "last_run" not in result
