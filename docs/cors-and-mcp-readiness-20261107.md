# CORS 写请求拦截修复 + MCP 远端连接就绪性核验（2026-11-07）

## 一、生产前端写请求被 CSRF 层拦截（CORS 报错）

### 现象
Kali 生产前端（`http://192.168.44.128:8080`）发起写请求时报：
> 该请求来自未授权的来源，已被拦截。若这是你自己部署的前端，请把它的地址加进 SUPEX_CORS_ORIGINS。

### 根因
这条不是浏览器 CORS 中间件（`CORSMiddleware`）抛的，而是 **CSRF 防线**
`app/auth/deps.py::enforce_csrf`：Cookie 通道的写操作（非 SAFE 方法）要求
`Origin` 落在 `_allowed_origins()` 里，该集合 = `SUPEX_CORS_ORIGINS` ∪ 本请求
自己的源（`request.url.scheme://netloc`）。

生产 `/etc/suvex/api.env` 里 `SUPEX_CORS_ORIGINS` 只写了
`http://localhost:5173,http://127.0.0.1:5173`（dev 值），**没有**
`http://192.168.44.128:8080` → 从生产前端发起的写请求 403 `CROSS_ORIGIN_BLOCKED`。

> 注：本机曾另起 vite dev（5173）+ 本地 uvicorn（8000）与生产前端争端口，已一并停掉。

### 修法（仅生产 env，非代码）
**关键：先查清浏览器实际用的是哪个源。** nginx access log 的 `Referer` 显示
用户真正在用的是 **`https://suvex.asia`**（前面还有一层反向代理终止 TLS，
本机无 443 监听，转发到本机 `:8080`），而不是 `http://192.168.44.128:8080`。
只加 IP:8080 并不能解决用户的问题 —— 必须把**域名源**加进去。

`/etc/suvex/api.env`（最终）：
```
SUPEX_CORS_ORIGINS=https://suvex.asia,http://suvex.asia,http://192.168.44.128:8080,https://192.168.44.128:8080,http://127.0.0.1:8080,http://localhost:8080,http://localhost:5173,http://127.0.0.1:5173
```
重启 `suvex-api`。

### 排查方法（下次同类先做这个）
不要凭猜。用真实写请求探每个候选源，看返回码：
- `400`（走到业务校验）= **源被放行**
- `403 CROSS_ORIGIN_BLOCKED` = 源被拦

实测结果（修前 vs 修后）：
| 源 | 修前 | 修后 |
|---|---|---|
| `http://192.168.44.128:8080` | 403 | ✅ 400 |
| `https://suvex.asia` | 403 | ✅ 400 |
| `http://localhost:8080` | 403 | ✅ 400 |
| `http://192.168.44.128`（裸 IP 无端口） | 403 | 403（仍挡，正常） |

### 验证（真机）
- 重启后 `health` = 200。
- 经 `http://192.168.44.128:8080` 登录（`Origin: http://192.168.44.128:8080`）→ 200；
  随后写 `POST /chat/sessions` → **201**（修前是 403）。
- 恶意源 `Origin: http://evil.example.com` 仍被挡。
- `.env.example` 补注释，提醒部署时必须把前端实际源（含端口）加进来。

## 二、MCP 远端连接（CTF2）就绪性核验

用户计划比赛时提供新的 MCP 连接 + 口令（如
`{"mcpServers":{"ctf2-suvex":{"type":"http","url":"https://ctf2.dasctf.com/api/ai/v1/mcp"}}}` +
`ctf2_xxx` token）。核验「给了有效凭据就能跑」：

### 端点实测
- `https://ctf2.dasctf.com/api/ai/v1/mcp` 是标准 **Streamable-HTTP MCP**，
  用 OAuth/Bearer（`www-authenticate: Bearer resource_metadata=...`）。
- 官方文档（`/api/ai/v1/skills/ctf2-platform/references/open-api.md`）：PAT 用
  `Authorization: Bearer <token>` 或 `X-CTF2-API-Key: <token>`。
- 用户当前给的 token（`ctf2_1436…`）用两种头部都返回 `401 invalid_token`
  → **该 token 已失效/吊销**（不是格式问题）。

### 我们的客户端（`app/kernel/mcp.py`）就绪性
- http 传输完整支持：`type="http"` + `url` + `headers`（可含 `Authorization`）。
- 请求头带 `Content-Type: application/json` + `Accept: application/json, text/event-stream`，
  支持 `Mcp-Session-Id` 会话、解析 JSON 与 SSE 两种响应。
- `add_mcp_server` 工具「**先试连、再保存**」：连不上就不存，避免配了个坏的还以为加上了
  （用户此前 `add_mcp_server ctf2-suvex 失败` 正是 token 没放进 headers + token 失效）。

### 自测证明「能跑」（本地 mock，无需真 token）
用桩 httpx 走 `McpClient` 完整握手：
- `TOOLS: ['ctf2_list_challenges', 'ctf2_submit_flag']`（工具发现正常）
- `AUTH_SENT: True`（`Authorization: Bearer <token>` 确实发出）
- `ACCEPT_SENT: True`（SSE 兼容 Accept 头确实发出）

结论：**代码链路已就绪**；用户下次给有效的 MCP url + 把 PAT 放进
`headers.Authorization`（或 `X-CTF2-API-Key`），即可完成
`add_mcp_server` → 列题 → 解题 → 交 flag。

## 三、已知死链（非本次修）
对话页「拉比赛题」默认地址 `https://ycb-test.dasctf.com`（`app/chat/competition.py`
`DEFAULT_COMPETITION_BASE_URL`）**当前不可用**：该域名已解析到腾讯云 CDN
（证书 SAN 全是 `*.cdn.myqcloud.com` 等），并非 CTF 平台 → 必然 SSL 主机名不匹配。
比赛结束后域名被释放所致，非代码 bug。比赛时若换新平台/新地址，需相应更新。

## 四、变更清单
- 生产 `/etc/suvex/api.env`：`SUPEX_CORS_ORIGINS` 加生产前端源（**仅服务器 env，不入库**）。
- 仓库：`suvex-api/.env.example` 补 CORS 部署注释（本文件同批提交）。
