# TikTok Shop 授权与业务只读查询

v0.6 增加独立 Shop HTTP 适配器，支持卖家新授权、已有令牌导入、刷新和签名店铺查询。它不是 Ads/Social MCP；三者的凭据和状态独立保存。v0.7 新增商品、订单和库存查询，并接入统一会话；没有真实上架或库存修改。

## 本机配置

```dotenv
SHOP_APP_KEY=在本机填写
SHOP_APP_SECRET=在本机填写
SHOP_AUTHORIZATION_URL=https://services.tiktokshop.com/open/authorize?service_id=应用的service_id
SHOP_OAUTH_REDIRECT_URI=https://你的域名/api/tiktokshop/callback
```

可用 `SHOP_SERVICE_ID` 代替复制的授权链接，`SHOP_AUTH_REGION=ROW` 或 `US` 决定授权域名；复制的官方链接优先确定域名，两处 service_id 不一致时拒绝开始。App Secret 不进入模型提示词或接口返回。授权配置必须与 Partner Center 保持一致。

`SHOP_TOKEN_ENCRYPTION_KEY` 可单独配置；为空时使用本机已有 `ADS_TOKEN_ENCRYPTION_KEY`。数据库内的 Shop token 和店铺 cipher 加密保存，并绑定当前 App Key。不要提交 `.env`、数据库或真实授权结果。

## 新授权：本机联调

1. 保持本机后端运行。
2. 同一台电脑打开 `http://127.0.0.1:8000/api/shop/login`，登录 TikTok Shop 并完成卖家授权。
3. 如公网回调未部署处理程序，浏览器可能停在该域名的错误页；在授权成功返回后，地址栏应包含 `code` 和 `state`。
4. 打开 `http://127.0.0.1:8000/api/shop/finish`，将完整回调 URL 粘贴到本机表单，由本机提交给后端。不要将 URL 发到聊天或公开日志。
5. 后端验证回调域名/路径、当前工作区、有效的一次性 state，换取 token 后实际调用 Get Authorized Shops。返回业务成功和店铺列表后才标记为 connected。

这一流程不要求现在部署 SiteGround 程序；它是授权回调尚未部署时的手动交接方式，官方测试指引也说明了从地址栏取得授权码的做法。正式常态化使用时，仍应将已注册的回调路径接入实际后端。仅“URL 可访问”不构成真实授权证明。

新授权的 state 持久化且有效30分钟。回调通过 `code` 接收授权码，调用 token 接口时使用 `auth_code`，`grant_type` 必须为 `authorized_code`。令牌到期字段是 Unix 时间戳，不是剩余秒数。只接受 seller/global-selling 类型（0、4、5）；Creator/TAP 凭据不能用于此适配器。

## 复用原系统令牌

已有同一应用的有效令牌时，无需新授权和回调；App Key、App Secret 不能代替卖家令牌，也不能据此找回遗失的旧令牌。

```dotenv
SHOP_ACCESS_TOKEN=原系统的Shop访问令牌
SHOP_REFRESH_TOKEN=原系统的Shop刷新令牌
```

更新后重启后端，调用 `POST /api/shop/import-token`。可仅提供其中一种：访问令牌可用则直接验证店铺，只有刷新令牌时先刷新。只在真实店铺请求成功后保存新连接。导入失败不会覆盖原连接。

仅导入 access token 时到期时间未知，返回 `expiry_known=false`，不会捏造“还能使用7天”；API 实际报过期后，如有刷新令牌则刷新，否则提示重新导入或授权。刷新时更新返回的令牌、权限和到期时间，并核对已知的卖家身份。刷新租约和代次校验防止并发刷新或断开后旧任务重写凭据。

## 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/shop/connection` | 配置和连接状态，不返回 token |
| POST | `/api/shop/connect` | 生成本次官方授权链接与一次性 state |
| GET | `/api/shop/login` | 从浏览器开始新授权 |
| GET | `/api/tiktokshop/callback` | 实际部署回调时使用；无参数访问仅是就绪探测 |
| GET | `/api/shop/finish` | 当前本机手动回调接收页 |
| POST | `/api/shop/complete-redirect` | 接收 `{"callback_url":"..."}`，只解析该 URL，不请求它 |
| POST | `/api/shop/import-token` | 从本机环境导入并验证已有令牌 |
| GET | `/api/shop/shops` | 签名调用官方授权店铺列表 |
| POST | `/api/shop/products/query` | 商品目录与SKU、价格信息，支持状态及seller_skus过滤 |
| POST | `/api/shop/orders/query` | 按创建时间读取订单，支持订单状态过滤 |
| POST | `/api/shop/inventory/query` | 指定商品/SKU/seller_skus或全目录的当前库存 |
| DELETE | `/api/shop/connection` | 删除本地凭据并使待处理授权失效；不是远程撤销 |

除公开回调/静态接收页面外，操作沿用 `X-Shop-ID` 和可选 `APP_TOKEN` 校验。本地接收页支持填写工作台令牌。删除本地连接不会删除 `.env` 中用户自己配置的导入源，也不会自动撤销平台授权。

## 签名与验证

业务域名固定为 `https://open-api.tiktokglobalshop.com`，仅开放授权店铺 GET 和下文列出的三个 POST 搜索接口，没有任意 URL 或写操作执行器。使用 HMAC-SHA256，按序拼接实际查询参数及原样请求体字节，排除 `sign`、`access_token`；令牌通过 `x-tts-access-token` 发送。解析并保留平台 request_id。HTTP 200 或授权页成功不单独构成店铺访问成功，业务 code 必须为0。

测试覆盖官方签名向量、真实 httpx 传输构造、授权回调、重放拒绝、到期刷新、断开并发、权限失败、加密存储、已有令牌导入和手动回调工作区校验。模拟服务测试不代表已完成真实 Shop 账号联调，实际结果以本机连接与店铺请求为准。

## 官方依据

- [授权总览](https://partner.tiktokshop.com/docv2/page/authorization-overview-202407)
- [卖家授权](https://partner.tiktokshop.com/docv2/page/seller-authorization-guide)
- [取得授权码与测试令牌](https://partner.tiktokshop.com/docv2/page/generate-test-access-token)
- [请求签名](https://partner.tiktokshop.com/docv2/page/sign-your-api-request)
- [首次店铺请求](https://partner.tiktokshop.com/docv2/page/call-get-authorized-shops)


## 业务接口使用

以下内部接口的请求体 `shop_id` 必须来自当前授权店铺列表。`X-Shop-ID` 是本地工作区隔离键，不能代替真实平台店铺 ID。

```http
POST /api/shop/products/query
Content-Type: application/json

{"shop_id":"你的平台店铺ID","status":"ACTIVATE","page_size":100,"max_pages":5}
```

```http
POST /api/shop/orders/query
Content-Type: application/json

{"shop_id":"你的平台店铺ID","relative_date":"yesterday"}
```

订单也支持 `start_date`、`end_date`（YYYY-MM-DD，起止日均包含，最多31天），不能与 `relative_date` 同时使用。`timezone` 可指定 IANA 时区。MY 默认 Asia/Kuala_Lumpur，TH 默认 Asia/Bangkok；这是市场地区推定值，回答会明确标识，并非平台返回的账号时区。无唯一默认值的市场须显式填写。底层转换为 `create_time_ge` 与下一天零点的 `create_time_lt`，不使用服务器本地日期拼接。

```http
POST /api/shop/inventory/query
Content-Type: application/json

{"shop_id":"你的平台店铺ID","product_ids":["目标商品ID"]}
```

库存也可指定 `sku_ids` 或 `seller_skus`，三者只能选一种；全部省略则先查询全商品目录，再分批查库存。仅支持当前库存，不支持按昨日/历史日期还原库存。库存专用接口分别返回 `total_available_quantity`（可下单数量）与 `total_committed_quantity`（订单占用数量）；不把占用库存再从可下单数量中重复扣除，也不把缺失结果当作零。

商品与订单每次最多5页、每页100条。返回 `complete`、`retrieved_count`、`total_count`（平台有返回时）、`next_page_token`、`warnings`。达到上限、重复游标、跨页重复、数量不一致都不能作为完整汇总。可通过 `page_token` 继续商品/订单查询；从非首游标开始的查询会标记为部分结果。全目录库存达到上限时需缩小为指定商品/SKU或先分页取目录再分批查询。分页期间业务数据可能变化，不构成数据库快照。

`data_verified=true` 表示取得业务成功的实际结果，**不表示 `complete=true`**。订单金额按币种分别汇总 `payment.total_amount`；所有状态金额与已支付且未取消金额分别展示，未扣退款，也不等同于结算收入、利润或 Ads GMV。返回内容及证据均过滤买家个人信息字段。业务结果和模型上下文不包含 token、app_secret 或 shop_cipher；授权令牌与 shop_cipher 仅在适配器内使用并加密保存。

## 对话入口

继续使用 `POST /api/sessions/{session_id}/messages`，例如：

- “列出我已授权的 TikTok Shop 店铺”
- “查询 MY 店铺昨天的订单和当前库存”
- “那 TH 同一天的订单呢？”
- “查看 TH 店铺的商品列表”

店铺通过本轮官方列表匹配名称、ID或市场代码；多个候选或缺少订单日期时先追问，不查询其他店铺。每项结果保留请求编号和独立证据。同一轮部分接口失败会返回 `partial` 和明确原因；全部失败时 `data_verified=false`。只读查询在请求内执行，进程重启或超过会话时间上限需重试。

## 本次接口依据

- [Search Products 202502](https://partner.tiktokshop.com/docv2/page/search-products-202502)：`POST /product/202502/products/search`。
- [Get Order List 202309](https://partner.tiktokshop.com/docv2/page/get-order-list-202309)：`POST /order/202309/orders/search`。
- [Inventory Search 202309](https://partner.tiktokshop.com/docv2/page/inventory-search-202309)：`POST /product/202309/inventory/search`。

自动化测试使用隔离数据库与模拟服务；另已完成本机百炼联合真实 Shop 查询，包含店铺发现、完整商品/库存分页、指定日期订单与跨店铺追问。真实授权、商品、订单和库存结果不进入 Git。
