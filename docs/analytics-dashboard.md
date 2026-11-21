# 经营数据看板

入口 `/analytics.html`，运营助手中的「数据看台 / 经营总览」也会打开此页面。界面采用最新提供的 A-Brain 深色 HTML 原型：对话中心与数据看板在顶部切换，保留极光背景、玻璃卡片、底部输入和折叠维度。原型的全部示例数据、倍数计算、预设 Agent 回复和预设归因文案已移除。Chart.js 随本项目打包，不依赖第三方 CDN。

## 数据与功能

| 模块 | 已实现 | 数据来源 / 限制 |
| --- | --- | --- |
| 店铺与日期 | 已授权店铺发现、市场筛选、自定义1–31天、预设日期、前一等长周期 | MY和TH按各店铺时区；不提供虚构店铺 |
| 订单概览 | 总数、签收/完成、在途/待发、取消、平台GMV及AOV | 订单按创建日期；平台GMV按支付期，包含取消/退款 |
| SKU | 已支付非取消件数、同期涨跌、筛选、关联订单明细 | 未扣售后退货；商品级视频关联数不是SKU转化归因 |
| 结算 | 账单金额、支付状态、正/零/负净额订单、调整流水、分页明细 | 按UTC账单生成日期；订单可以在更早日期创建；净结算不是利润或银行逐单到账 |
| 费用 | 所有非零明细字段、同期变化、非零/明确零/未知订单、对应营收比例、穿透明细 | 使用平台原始正负号；排除补充解释字段重复求和；不把缺字段当0 |
| 地区 | 省/州订单分布 | 只保存L1省州；不保存买家姓名、电话、街道、邮编 |
| 时段与状态 | 店铺当地24小时时段、订单状态、平台取消原因 | 没有物流异常明细时不编造拒收/丢件/退货原因 |
| 渠道 | 视频、直播、商品卡归因GMV | 使用平台Shop Performance；不冒充渠道订单数或强制凑成100% |
| 达人 | 联盟订单、件数、实际/预估佣金、排序 | SETTLED且有金额才算已知实际；空对象未知；未匹配不等于自营；达人订单可能跨SKU重叠 |
| 对比与导出 | 跨店铺原币对比、CSV导出、布局密度切换 | 不直接求和MYR和THB；CSV包括来源完整性和口径 |
| 数据解读 | 本地计算信号、百炼真实问答 | 服务器重读当前筛选；仅发送获准的经营汇总，不发送订单ID/买家资料/密钥；不可执行修改 |

尚未实现：SKU级广告投放/消耗/ROAS归因、物流异常与完整售后原因、未结算订单的全量资金追踪、仓库API。界面明确显示对应数据限制，保留后续扩展入口。

## API

所有接口复用工作区 `X-Shop-ID` 和可选 `Authorization: Bearer APP_TOKEN`。单一所有者本地工作台，不是多用户身份系统。

- `GET /api/analytics/shops`：已授权店铺、时区、币种、来源列表。
- `GET /api/analytics/dashboard?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&site=MY&shop_id=ALL`：当前和前一等长周期，按店铺返回orders/finance/affiliate/performance及source coverage。
- `POST /api/analytics/refresh`：相同日期/店铺筛选，返回202和持久化同步任务；不会等待所有远端查询完成。
- `GET /api/analytics/refresh/{job_id}`：进度、活动来源、失败/部分来源。相同筛选的活动任务复用同一ID。
- `GET /api/analytics/details`：同一筛选，加`selected_shop`，`kind=orders|sku|expense|settlement`；SKU/费用加`field`，费用`zero=true`查明确零净额；`page`/`page_size`分页。
- `GET /api/analytics/export`：当前筛选的UTF-8 CSV，包含口径与完整性，并防范电子表格公式注入。
- `POST /api/analytics/ask`：日期筛选和question，返回真实百炼解读及来源状态；demo模式不会返回预设分析。

## 刷新与恢复

运行正常的API和 `python -m app.worker` 即可，不需要另起服务。后台优先处理运营任务，再领取看板刷新和原有订单定时同步。看板刷新使用独立的持久化任务记录和租约，长分页请求前续约；进程退出后可在租约过期后重领，已完成来源保留。每个来源/店铺/准确日期窗口单独保存快照，查询不会拿另一日期窗口代替当前筛选。

页面每60秒重读本地快照；点击「刷新真实数据」才调用平台更新所选周期及对比周期。这不是平台实时推送，平台分析本身也有`latest_available_date`。没有启动worker会显示等待；失败不会覆盖上次成功数据。来源区显示完整/部分/未同步、抓取时间、请求数量；旧快照可以手动刷新。

每个来源分页上限20页，每页最多100条，每个来源超时300秒；达到上限保留partial，不声称全量。订单明细按50个订单一批查询。业务数据只在本地数据库，授权仍保存在既有加密存储；均不会纳入Git。

## 官方端点

- [订单详情 v202507](https://partner.tiktokshop.com/docv2/page/get-order-detail-202507)
- [结算账单 v202309](https://partner.tiktokshop.com/docv2/page/get-statements-202309)
- [账单交易 v202501](https://partner.tiktokshop.com/docv2/page/get-transactions-by-statement-202501)
- [卖家联盟订单 v202410](https://partner.tiktokshop.com/docv2/page/search-seller-affiliate-orders-202410)
- [店铺表现 v202609](https://partner.tiktokshop.com/docv2/page/get-shop-performance-202609)
- [店铺视频表现 v202605](https://partner.tiktokshop.com/docv2/page/6a0eaab9f76068049e936023)

验证：货币精度和隔离、完整/缺失/明确零区分、SKU同期缺项、订单当地时区、佣金预估/实际、费用去重与穿透、分页完整性、工作区隔离、刷新幂等、AI摘要不含订单身份，以及真实MY/TH读取和浏览器交互。离线测试不替代平台授权与网络检查。

## A-Brain 对话中心

默认首页和 `/?view=chat` 打开 `/analytics.html?view=hub`，看板仍可通过 `/analytics.html?view=dashboard` 打开。五张卡片对应 Leader、商品运营、广告、选品、内容的协作入口，复用现有会话及工具路由服务，不新建另一套 Agent 后端。卡片只补充当前协作角色上下文，真实业务任务仍由已有 Leader / Harness 规划和执行。每个入口的活动会话 ID 在本浏览器按工作区保存，消息和长期记忆继续由服务端存储；历史会话可恢复，新建会话不会删除旧记录。

任务中心、连接设置、订单、实物库存与成本利润保留既有入口。对话浮层使用原生 dialog，支持 Escape 和键盘焦点；支持减少动态效果偏好。
