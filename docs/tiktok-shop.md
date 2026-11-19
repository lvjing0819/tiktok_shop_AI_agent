# TikTok Shop 授权与只读店铺验证

v0.6 增加独立 Shop HTTP 适配器，支持卖家新授权、已有令牌导入、刷新和签名店铺查询。它不是 Ads/Social MCP；三者的凭据和状态独立保存。本版本没有真实上架、库存修改，也未把 Shop 查询接入统一会话。

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
| DELETE | `/api/shop/connection` | 删除本地凭据并使待处理授权失效；不是远程撤销 |

除公开回调/静态接收页面外，操作沿用 `X-Shop-ID` 和可选 `APP_TOKEN` 校验。本地接收页支持填写工作台令牌。删除本地连接不会删除 `.env` 中用户自己配置的导入源，也不会自动撤销平台授权。

## 签名与验证

业务接口固定为 `GET https://open-api.tiktokglobalshop.com/authorization/202309/shops`，没有任意 URL 执行器。使用 HMAC-SHA256，按序拼接实际查询参数及原样请求体字节，排除 `sign`、`access_token`；令牌通过 `x-tts-access-token` 发送。解析并保留平台 request_id。HTTP 200 或授权页成功不单独构成店铺访问成功，业务 code 必须为0。

测试覆盖官方签名向量、真实 httpx 传输构造、授权回调、重放拒绝、到期刷新、断开并发、权限失败、加密存储、已有令牌导入和手动回调工作区校验。模拟服务测试不代表已完成真实 Shop 账号联调，实际结果以本机连接与店铺请求为准。

## 官方依据

- [授权总览](https://partner.tiktokshop.com/docv2/page/authorization-overview-202407)
- [卖家授权](https://partner.tiktokshop.com/docv2/page/seller-authorization-guide)
- [取得授权码与测试令牌](https://partner.tiktokshop.com/docv2/page/generate-test-access-token)
- [请求签名](https://partner.tiktokshop.com/docv2/page/sign-your-api-request)
- [首次店铺请求](https://partner.tiktokshop.com/docv2/page/call-get-authorized-shops)
