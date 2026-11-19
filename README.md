# TikTok Shop AI Agent 工作台

基于 **LangGraph + FastAPI + React + 阿里云百炼** 的本地运营应用。v0.2 加入可管理的记忆、数据库任务队列和持久化图执行。

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
- 对话只能给建议，尚未自动创建任务；任务从商品页创建。
- 订单、财务、广告没有真实数据；分析不得编造经营指标。
- 暂无完整主管多 agent、ReAct 工具循环、候选记忆自动提取、向量检索和效果评估。
- 任务是至少一次执行；崩溃在模型响应与检查点落盘之间可能重复调用并产生费用。当前模拟上架稳定编号，不代表真实外部 API 已实现幂等。建议先使用一个 worker；运行中的任务不支持取消。
- 图状态是执行记忆；店铺长期偏好是业务数据库中的结构化记录，未使用 LangGraph Store 或向量数据库。

TikTok 官方接入入口：https://partner.tiktokshop.com/docv2/page/hhw7w40d 。后续核对授权、签名和接口契约后再接入真实操作。

## 本次验证记录

已验证干净 Python 环境的 SQLite 测试、跨进程恢复、HTTP 对话入口和前端生产构建。当前环境没有 PostgreSQL 服务及百炼真实密钥，PostgreSQL 部署与真实模型调用尚未实测。浏览器内置预览超时，未完成视觉验收。
