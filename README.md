# TikTok Shop AI Agent 工作台

基于 **LangGraph + FastAPI + React + 阿里云百炼** 的本地运营应用。v0.5 提供会话与店铺记忆、持久化任务、Ads/Social 只读 MCP 接入，以及统一会话分派。

## 已完成

- **运营对话**：新建、切换、删除会话，刷新/重启后保留消息；百炼连接测试和可读的错误原因。
- **会话记忆**：保存完整原始消息；模型使用最近 12 条消息、当前问题和历史摘要。超过窗口后压缩已退出窗口的消息。离线演示使用有长度上限的摘录，不伪装为模型总结。
- **店铺长期偏好**：目标市场、品牌风格、禁用表述和备注；人工保存即确认，支持修改与清空。没有自动推测用户偏好。修订号防止旧页面覆盖新内容。
- **商品/任务记忆**：商品信息、生成时偏好快照、文案、审核版本及事件，任务通过 ID 共享信息。
- **持久化工作流**：商品校验 → 示例价格 → 文案 → 审核中断 → 模拟上架。数据库检查点支持跨进程继续；失败后从失败节点重试。
- **独立后台进程**：持久化任务队列、领取租约、心跳续约，进程退出后未完成任务在租约到期后重新领取。审核决定和排队在同一事务保存。
- **隔离与访问**：按店铺标识隔离会话、偏好和任务。可选工作台访问令牌。

## 一键本地启动

安装 Python 3.11+、Node.js 20+ 后，在项目根目录运行：

```sh
sh start.sh
```

首次安装依赖并构建页面，随后一起启动 API 与后台进程，访问 http://127.0.0.1:8000 。默认 SQLite、离线演示；按 Ctrl-C 关闭两个进程。百炼模式需先编辑本机 `.env`。

## 分别启动（开发，SQLite）

需要 Python 3.11+、Node.js 20+。从项目根目录执行：

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock
cp .env.example .env
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

第二个终端，项目根目录：

```sh
source .venv/bin/activate
python -m app.worker
```

第三个终端：

```sh
cd frontend
npm ci
npm run dev
```

打开 http://127.0.0.1:5173 。API 文档 http://127.0.0.1:8000/docs 。后台和 API 必须从相同根目录加载同一份环境配置。未启动 worker 时任务会保持等待；worker 重启后可继续领取。默认租约 120 秒，心跳间隔 20 秒。

## 连接百炼

在本机 `.env` 设置：

```dotenv
MODEL_MODE=bailian
DASHSCOPE_API_KEY=填入你自己的百炼密钥
BAILIAN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
BAILIAN_MODEL=qwen-plus
```

重启 API 和 worker，在「店铺记忆 → 测试模型连接」验证。默认是北京兼容地址，其他地域/工作空间需使用控制台对应地址及可用模型。代码通过 HTTP 调用百炼兼容接口，不需要 OpenAI 平台账号。

默认 `MODEL_MODE=demo` 离线运行，不调用真实模型。界面始终标识演示模式。
官方参考：https://help.aliyun.com/en/model-studio/qwen-api-via-openai-chat-completions

## PostgreSQL

```sh
docker compose up -d db
```

在 `.env` 中设置以下地址并重启 API 和 worker：

```dotenv
DATABASE_URL=postgresql://shop:shop_local@127.0.0.1:5432/shop
```

使用同一 PostgreSQL 数据库保存业务记录、队列与图检查点；SQLite 配置此时不再使用。Compose 默认密码仅适合本机开发。自定义 POSTGRES_PASSWORD 后同步更新 DATABASE_URL。切换数据库不会自动搬迁旧数据。

数据库初始化目前通过建表完成；已有表结构的正式升级后续应引入版本化迁移。

## 使用与记忆管理

1. 在「店铺记忆」保存市场、风格等要求。
2. 在「运营对话」新建会话，后续问题自动读取该会话与当前偏好。
3. 长会话的历史摘要可在会话页展开查看；原始消息仍保留。
4. 在「商品任务」创建商品，等待方案生成后批准或驳回。任务保留创建时的偏好快照。
5. 删除会话会删除原始消息和摘要；清空偏好仅清空长期偏好，历史会话、任务及图检查点中的既有记录不会被连带擦除。
6. 店铺标识可在顶部设置，输入后按回车切换。令牌仅存浏览器会话存储。

`APP_TOKEN` 非空时所有 API 需要 `Authorization: Bearer ...`。这是**单一所有者工作台**：店铺选择用于分类和查询隔离，不是不同用户间的安全授权。尚无多用户登录，不要作为公开的多租户服务部署。

## 测试与构建

```sh
python -m pytest -q
cd frontend
npm run build
```

覆盖审批版本校验、幂等提交、跨进程恢复、租约回收、模型失败重试、会话上下文、摘要、店铺隔离、偏好修改/清空、访问令牌和模型请求契约。真实百炼调用需要你的密钥；契约测试使用模拟 HTTP，不证明账号配额和网络可用。

## 从 v0.1 迁移

先备份原 SQLite 文件，停止旧服务，配置目标数据库，然后执行：

```sh
python -m scripts.migrate_legacy /absolute/path/to/old-shop.sqlite
```

只读旧库，按任务 ID 跳过已导入数据。旧的非终态任务会重新排队、重新准备方案并要求审批；旧版没有图检查点，不能假装恢复旧执行位置。

## 当前边界

- TikTok Shop 上架仍是 **mock**，仅生成稳定 `mock-` 编号。没有访问真实店铺。
- 图片、视频仍为明确占位，未接入生成模型；定价仅考虑输入成本，不含平台费、税费、运费和退款。
- 百炼模式下，运营对话可分派到 Ads/Social 只读查询；尚未自动创建商品任务，任务仍从商品页创建。
- 订单、财务尚未接入真实数据。Ads 与 Social 的连接及部分只读业务已完成真实联调；百炼路由与真实数据的联合测试仍需本机有效模型密钥。
- Ads/Social 模块已有受限的只读工具循环；统一会话每轮分派一个操作，不包含多团队并行协作。候选记忆自动提取、向量检索和效果评估尚未实现。
- 任务是至少一次执行；崩溃在模型响应与检查点落盘之间可能重复调用并产生费用。当前模拟上架稳定编号，不代表真实外部 API 已实现幂等。建议先使用一个 worker；运行中的任务不支持取消。
- 图状态是执行记忆；店铺长期偏好是业务数据库中的结构化记录，未使用 LangGraph Store 或向量数据库。

TikTok 官方接入入口：https://partner.tiktokshop.com/docv2/page/hhw7w40d 。后续核对授权、签名和接口契约后再接入真实操作。

## 本次验证记录

已验证干净 Python 环境的 SQLite 测试、跨进程恢复、HTTP 对话入口和前端生产构建。PostgreSQL 部署尚未实测。2026-11-19 已配置本机百炼并验证模型连接、统一会话的真实广告账户发现、Social 账号查询、GMV Max 报告及连续日期追问。申请演示使用独立原型，主工作台仍需单独进行完整视觉验收。

## v0.3 TikTok Ads 官方 MCP（只读）

「广告分析」页现在包含授权连接、工具检查、真实数据分析、证据和历史报告。广告接口与 TikTok Shop 上架接口独立。

### 配置与连接

1. 安装更新后的依赖：`pip install -r requirements.lock`。
2. `python -m scripts.setup_ads` 在本机 `.env` 生成令牌加密密钥，不会输出密钥；一键启动脚本会自动执行。请备份 `.env`，丢失密钥将无法解密已保存授权。不要将 `.env` 提交 Git。
3. 配置百炼为 `MODEL_MODE=bailian`，填入本机密钥；重启 API 与 worker。
4. 打开「广告分析」，点击连接，等待出现「打开 TikTok 授权页面」，在 TikTok 登录并授权。返回工作台等待显示已连接。
5. 输入 Ads Manager 中的广告账户 ID、日期范围和问题，开始查询。没有真实连接或仍处于离线演示模式时，不会伪造广告数据。

`ADS_OAUTH_REDIRECT_URI` 默认 `http://127.0.0.1:8000/api/ads/oauth/callback`，必须与浏览器能够访问的后端地址一致。如果 TikTok 实际注册规则不接受回环地址，请配置可访问的 HTTPS 回调，不能随意替换已注册地址。本机已通过该回环地址完成 Ads 与 Social 真实授权；其他部署地址仍需使用各自注册的准确回调。开启 APP_TOKEN 时回调仍通过一次性 state 验证，不依赖浏览器携带工作台令牌。

### 连接实现

使用 MCP Python SDK v1 的 Streamable HTTP 和 OAuthClientProvider，通过官方服务的标准元数据发现完成动态客户端注册、PKCE、令牌交换和刷新。客户端信息与访问/刷新令牌均加密入库；原始授权码也加密暂存，完成后移除。回调访问日志被过滤，避免记录 URL 中的授权码。

固定连接 `https://business-api.tiktok.com/open_mcp/tt-ads-mcp-flat`。本版选择 flat 是为了直接取得完整工具定义，再在本机过滤；不会把全部工具塞给模型。尚未接入 layer 服务的渐进工具发现流程。

授权过程最多等待6分钟，当前运行在 API 进程内；服务重启时需断开并重新开始授权。已有授权令牌与客户端信息可跨重启保存。连接状态表示曾成功建立连接；是否仍有效以工具请求和刷新结果为准。

「断开并删除本地授权」只删除本机凭据并使旧授权流程失效，不会自动撤销 TikTok 侧授权。远程撤销请在 TikTok for Business 授权设置完成。删除本地授权不会删除历史分析报告；报告可单独删除。

### 只读策略和分析

- 默认只允许名称属于查询类、明确声明 `readOnlyHint=true` 的工具。写操作和通用执行器始终拦截。
- 若官方工具未给只读注解，可先查看工具列表及官方定义，将审核过的**精确只读工具名**填入 `ADS_READ_TOOL_ALLOWLIST`（逗号分隔），重启后生效。这个配置不能解除写操作名称拦截，不可填入未经检查的工具。
- 每次从可用工具中选择最多24个，LangGraph 管理「模型判断 → 参数校验 → 调用 → 观察」循环，最多6次调用；没有成功工具证据时不生成经营结论。
- 根级 advertiser_id/advertiser_ids 固定到所选账户；嵌套的其他 advertiser_id 被拒绝。根级 start_date/end_date 若在 schema 中则固定为输入日期。其他时间过滤形式需结合工具定义及原始参数核对，不保证工具已覆盖所选日期。
- 报告记录工具名、参数、原始结果、查询时间与证据编号。模型读取有限预览；完整原始结果可展开查看。
- 仅当 structuredContent 中有明确报表行和相应指标时，程序按行计算 CTR、CPC、CPA；不会跨分页或跨维度擅自合并。币种、时区和归因口径以官方结果为准。
- 报表接口尚未加入统一分页拉取或完整性校验；模型最多6次调用不代表已读取全部数据。超大结果会要求缩小范围。

### 验证范围

新增测试通过模拟 HTTP 完整运行真实 MCP SDK 的 OAuth 注册、PKCE、回调和后续工具请求；也验证令牌不以明文保存、断开后旧流程不能重写令牌、跨店铺隔离、只读拦截、参数校验、证据与调用上限。

上述自动化测试使用模拟服务。后续已通过本机代理完成真实 Ads 授权、5个广告账户发现和 GMV Max 查询；Social 已完成账号信息和作品查询。工具列表和原始结果始终以运行时官方返回为准。2026-11-19 已完成真实百炼的统一会话广告账户发现、Social 账号查询、GMV Max 报告与连续日期追问；其他问题与工具组合仍需按实际使用验证。

官方接入依据：
- https://business-api.tiktok.com/portal/docs/how-to-connect-a-custom-agent-to-tiktok-for-business-mcp-server/v1.3
- https://business-api.tiktok.com/portal/docs/mcp-authorization/v1.3
- https://business-api.tiktok.com/portal/docs/exchange-an-mcp-authorization-code-for-tokens/v1.3

广告分析当前是单次 API 请求中的有界流程，成功后保存报告。请求期间服务重启需要重新查询；它尚未接入商品任务使用的持久化队列和检查点。

## v0.4 TikTok Social MCP

增加独立 Social 后端连接、OAuth/可选 Organic 用户 token 认证、只读工具调用与自然语言查询。复用传输实现，保留现有 Ads 授权记录及接口。Social 和 Ads 的凭据、回调、状态、报告分别保存；发布和评论修改被拦截。该阶段未修改前端。

配置、API 与完整文档依据见 [TikTok Social 后端说明](docs/tiktok-social.md)。Social 标准 OAuth 的用户授权窗口是一年，与 Ads 的30天不同。本机已单独完成 Social 授权并验证账号及作品数据；新安装环境仍需自行授权。


## v0.5 统一运营会话

现有运营对话入口可通过百炼选择普通回复、广告账户发现、Ads 查询或 Social 查询。查询结果与原始证据关联；广告账户名称从实际授权列表匹配，日期按账户时区解析，缺参数会追问。支持保存本轮账户和查询上下文，便于处理“那前天呢”等后续消息。

配置、查询示例和执行记录接口见 [统一会话说明](docs/conversation.md)。本轮没有改动前端；现有消息接口仍返回原会话结构，并增加执行记录元数据。

新增端到端自动化测试验证路由、账户名称与时区、追问续接、业务错误、证据隔离和会话删除事务。真实账户解析、百炼连接、统一会话广告账户发现、Social 账号查询、GMV Max 报告与连续日期追问已在本机验证。新环境需配置自己的百炼密钥并独立授权；模拟模型测试不能代替真实联调。
