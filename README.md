# TikTok Shop AI Agent

基于 LangGraph、FastAPI、React 和阿里云百炼的运营工作台原型。

## 已实现
- 商品任务：校验库存 → 示例价格计算 → 百炼生成文案/素材需求 → 等待审核 → 模拟上架。
- 审批状态校验、模拟上架幂等、任务持久化、失败重试、基于已保存结果跳过文案重复生成。
- 经营分析入口，显式返回数据依据，不伪造订单或财务指标。
- React 工作台：任务创建、状态轮询、审核、驳回、重试。

## 运行
需要 Python 3.11+、Node.js 20+。

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 127.0.0.1 --port 8000
```
另开终端：
```sh
cd frontend
npm install
npm run dev
```
打开终端显示的本地地址。API 文档： http://127.0.0.1:8000/docs 。

## 百炼配置
默认 `MODEL_MODE=demo`，不调用模型。设置 `MODEL_MODE=bailian`，在本机 `.env` 填写 `DASHSCOPE_API_KEY`，并配置 `BAILIAN_MODEL`、`BAILIAN_BASE_URL`。密钥不可提交。
默认接口是北京地域兼容地址；不同地域/工作空间应使用百炼控制台对应地址。
文档：https://help.aliyun.com/en/model-studio/qwen-api-via-openai-chat-completions

## 测试
```sh
python -m pytest -q
cd frontend
npm run build
```

## 当前边界
这是可运行的第一阶段原型，不是完整多 agent 产品：目前只有商品模型节点和独立分析调用，没有主管动态规划、ReAct 工具循环或完整素材 agent。图片/视频只返回明确标记的占位，未生成媒体。TikTok 上架完全模拟，不会访问真实店铺。报价仅包含商品成本，非实际经营定价。

开发环境采用 SQLite，未启用 PostgreSQL。任务由进程内 BackgroundTasks 执行；进程中断时 running/queued 任务不会自动恢复；failed 可重试。尚无登录、多租户和真实审批人身份，不应暴露公网。下一阶段引入数据库任务领取/租约、认证、持久化图检查点与真实工具适配。

## TikTok Shop 接入依据
用户提供的官方文档入口：https://partner.tiktokshop.com/docv2/page/hhw7w40d
接入前应核对应用授权、API 版本、签名规则、店铺权限及各接口请求响应；当前未实现或猜测真实接口契约。
