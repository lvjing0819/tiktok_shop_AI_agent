# TikTok Social MCP 后端接入

Social 管理 TikTok 自然账号、内容、评论、趋势和提及；Ads 管理广告。两者是独立授权，Social 不会复用 Ads 的 token。该阶段不修改前端。

## 已实现

- 独立的 Social OAuth 注册、PKCE、回调、刷新及加密存储。
- 独立连接/授权状态；Social 断开不会删除 Ads 凭据。
- 官方运行时工具发现，以及只读工具参数校验和调用。
- 百炼 + LangGraph 的 Social 查询循环，最多6次工具调用；结果保留证据和成功状态。
- 可选 Organic API 用户 access token 认证模式。
- 拦截发布、评论回复、点赞、删除和授权修改。标准 MCP OAuth 本身暂不支持图片/视频发布；即使切换用户 API token，本项目也不开放写工具。

## 官方连接参数

- MCP：`https://business-api.tiktok.com/open_mcp/tt-ads-mcp-organic-social`
- OAuth metadata：`/open_mcp/tt-ads-mcp-organic-social/oauth/.well-known/openid-configuration`
- 注册、授权、换 token 的地址均从官方元数据动态发现，Social 的实际地址包含 `/oauth/tiktok/`，不同于 Ads。
- OAuth access token 一天，refresh token/用户授权窗口一年；实际权限取决于账号、地域、token scope 和平台返回。
- 本地回调：`http://127.0.0.1:8000/api/social/oauth/callback`。
- 默认请求读取账号信息、视频列表/洞察、评论列表和趋势搜索相关 scope，不请求发布和评论管理 scope。

## 启动与代理

重启后端即可加载 Social 路由；Swagger：`http://127.0.0.1:8000/docs`。

若使用 macOS 系统代理，Python 不一定自动读取系统设置。可在本机 `.env` 配置实际地址（下例只是本机排查得到的端口，不是其他机器的默认值）：

```dotenv
HTTP_PROXY=http://127.0.0.1:9674
HTTPS_PROXY=http://127.0.0.1:9674
```

应让 localhost 和 127.0.0.1 直连。不要把本机密钥或 token 提交到 Git。

## OAuth 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/social/connection` | 连接状态和授权模式，不返回密钥 |
| GET | `/api/social/login` | 打开/自动生成有效 TikTok 授权页面，避免复制过期链接 |
| POST | `/api/social/connect` | 开始注册与授权，返回 attempt_id |
| GET | `/api/social/attempts/{attempt_id}` | 读取授权地址与状态 |
| GET | `/api/social/oauth/callback` | 一次性 state 校验的浏览器回调 |
| DELETE | `/api/social/connection` | 断开本地连接；不会自动撤销 TikTok 侧授权 |
| GET | `/api/social/tools` | 实际工具列表、schema 和只读策略 |
| POST | `/api/social/tools/call` | 直接调用已确认的只读工具，无需模型 |
| POST | `/api/social/query` | 自然语言 Social agent 查询，需要真实百炼 |
| GET | `/api/social/reports` | 已保存的查询报告列表 |
| GET | `/api/social/reports/{id}` | 报告与原始证据 |

沿用 `X-Shop-ID` 工作区隔离和可选 `Authorization: Bearer APP_TOKEN`。这是单一所有者本地应用，不是多用户权限系统。

OAuth 授权需在 TikTok 页面由用户完成。`connect` 返回202仅表示开始，`tools/list` 成功也只验证工具连接；要证明实际账号访问，还需成功调用只读工具并核对返回的业务 code/request_id。授权流程在 API 进程内等待，重启期间未完成的授权需重新发起。

本地 Social 授权等待窗口为15分钟，TikTok 侧的有效期和浏览器 Cookie 校验独立生效。若 TikTok 页面显示 `Invalid OAuth state or browser binding`，说明尚未跳回本地后端；请从 `/api/social/login` 重新开始，全程使用同一个普通浏览器窗口并允许 Cookie。不要刷新或复用带 `code` 的旧回调页面，也不要在中途切换内置浏览器和 Chrome。延长本地等待窗口不能绕过 TikTok 的浏览器绑定检查。

## 无需模型的只读调用示例

在完成授权后，先 `GET /api/social/tools` 查看当前 schema。官方文档提供以下标签建议示例；以实际工具参数为准：

```json
{
  "name": "account_user_hashtag_suggestion_get",
  "arguments": {"keyword": "watch"}
}
```

提交至 `POST /api/social/tools/call`。接口返回工具名、参数和原始结果；`isError=false` 不等于业务成功，应检查 TikTok 返回的 `code=0`。

自然语言提交至 `POST /api/social/query`：

```json
{"question":"查找与手表相关的标签建议，并说明数据来源。"}
```

首次查询可能需要参数或额外权限。agent 不应编造账号 ID、视频 ID 或指标。没有成功业务数据时报告 `data_verified=false`；报告中的文本仍应结合原始证据审核。

## API token 模式

如果已有对应 TikTok 用户的 Organic API token，可在本机配置：

```dotenv
SOCIAL_AUTH_MODE=api_token
SOCIAL_API_ACCESS_TOKEN=填入本机用户token
```

重启后 `POST /api/social/connect` 会通过真实 MCP 握手和工具发现验证令牌，而不是仅根据配置存在就宣称已连接。请求使用 `Authorization: Bearer ...`。

该凭据是 Organic API 的 TikTok 用户 token，不应直接替换为 Ads Marketing API 长期 token。文档的用户 token 示例为一天有效、refresh token 一年；本模式未实现 Organic API 的换取和刷新流程，需由现有 Organic 集成维护并更新环境配置。OAuth 模式的刷新由 SDK 处理。

API token 存在本机环境中，不写入普通业务记录。DELETE connection 会禁用当前工作区的本地使用并删除验证状态；不会修改其他工作区共享的环境变量，也不会撤销远程授权。

## 记忆与执行边界

Social 查询读取已确认店铺偏好，保存独立 `social_report`；直接调用保存 `social_call`。本阶段没有把 Social 路由自动接入普通运营对话，也不支持跨查询会话规划。

查询目前是单次有界请求，不具有商品任务的队列恢复能力；接口/模型失败后需重试。不会自动分页读取全部数据，模型只看有限预览，完整工具结果保留用于核验。工具权限、可用性和业务数据须以实际账号联调为准。

## 已阅读的官方文档

五篇正文、参数、示例和限制均已完整读取。读取版本为 v1.3；运行时官方元数据仍是连接参数的来源。

1. [Social MCP 总览](https://business-api.tiktok.com/portal/docs/tiktok-social-mcp-server/v1.3)，doc_id 1881011036718146。
2. [现成 agent 连接](https://business-api.tiktok.com/portal/docs/how-to-connect-a-ready-made-agent-to-tiktok-social-mcp-server-as-an-advertiser/v1.3)，doc_id 1881011298395137。
3. [自建 agent 连接](https://business-api.tiktok.com/portal/docs/how-to-connect-a-custom-agent-to-tiktok-social-mcp-server-as-a-developer/v1.3)，doc_id 1881372842952961。
4. [用户 API token 认证](https://business-api.tiktok.com/portal/docs/authenticate-tiktok-social-mcp-server-requests-with-an-api-access-token-in-a-custom-agent/v1.3)，doc_id 1881372923264002。
5. [工具列表](https://business-api.tiktok.com/portal/docs/available-tools-in-tiktok-social-mcp-server/v1.3)，doc_id 1881011302590657。

## 令牌跨进程刷新修正

`TokenStorage` 在加密记录中保存 `_token_expires_at`。对于已有 Social 刷新令牌和已注册客户端，绝对到期时间不足60秒或旧记录没有该时间时，会先从官方服务发现 token endpoint，再用原 refresh token 与 client_id 刷新。校验 issuer 与原客户端一致、固定 TikTok 域名/路径，拒绝重定向与扩大 scope；原响应未轮换 refresh token 时保留旧值。

刷新由工作区、服务、连接 generation 及数据库租约约束。断开或重授权后，旧刷新不能覆盖新连接。请求失败不删除凭据；服务明确拒绝刷新时才提示重新连接。新增测试覆盖跨会话绝对到期、保留未轮换刷新令牌、错误端点不发送凭据，以及刷新失败后释放租约。
