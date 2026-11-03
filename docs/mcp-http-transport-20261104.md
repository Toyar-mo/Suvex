# v1.62：MCP 支持 streamable-HTTP 传输

## 病根（用户报的 bug）

会话里，用户贴了一条 MCP 配置：

```json
{"mcpServers":{"ctf2-suvex":{"type":"http","url":"https://ctf2.dasctf.com/api/ai/v1/mcp"}}}
```

模型照着调了 `add_mcp_server`，界面弹红条：

> 失败：试连失败，没有保存。原因：MCP 连接 `ctf2-suvex` 的 stdout 已关闭（server 退出了）。

这条报错**把问题说反了**。真正的根因不是"server 崩了"，而是：

> 整条 MCP 链路只实现了一种传输 —— **stdio**（`command` 拉起一个子进程，用 stdin/stdout 走 JSON-RPC）。
> 用户贴的是 **http** 类型：它没有 `command`，只有一个远端 URL。
> 界面/工具逼着填 `command`，模型只好拿 `npx` 去 spawn 一个进程 ——
> 那个进程当然连不到用户想连的远端端点，stdout 一关就报"server 退出了"。

也就是说，`url` 类型的连接**根本不该走 stdio 分支**。这是功能缺口，不是孤立 bug。

## 修法

在 `app/kernel/mcp.py` 增加 **streamable-HTTP**（JSON-RPC over HTTP POST + SSE）传输，
与既有 stdio 并存，按 `type` 分派。配置模型加 `type` / `url` / `headers`。

### 后端

| 文件 | 改动 |
| --- | --- |
| `app/kernel/mcp.py` | `McpServerConfig` 加 `type`/`url`/`headers` + `is_http()`；`parse_server_configs` 认得 http 条目（只要 `name`+`url`，不需 `command`）；新增 `_parse_sse_messages`/`_safe_excerpt`；`McpClient` 加 `_start_http`/`_close_http`/`_http_headers`/`_http_post`/`_http_request`/`_http_notify`，`_start`/`close`/`_request`/`_initialize` 按 `is_http()` 分派，stdio 分支原样保留；`McpRegistry.summaries` 回显 `type`/`endpoint` |
| `app/kernel/service.py` | `test_mcp_server` 按类型校验（http 缺 `url` / stdio 缺 `command` 各自的人话）；`_public_server_view` 加 `type`/`url`/`has_headers`（headers 只回 bool）；`save_mcp_servers` 落 `type`/`url`/`headers`，支持 `preserve_headers` 保留旧值 |
| `app/kernel/schemas.py` | `McpServerIn` 加 `type`/`url`/`headers`/`preserveHeaders`，`command` 从必填改默认空串；`McpServerOut` 加 `type`/`url`/`hasHeaders` |
| `app/chat/engine.py` | `_tool_add_mcp_server` 支持 http（取 `type`/`url`/`headers`，缺 url 报错）；`_tool_list_mcp_servers` 按类型展示；**修了一个潜在 AttributeError**：保存回执里 `test_mcp_server` 回的 `tools` 是字符串列表，原写 `t.get("name")`（dict 用法）会崩 |
| `app/chat/prompts.py` | `add_mcp_server` 的工具声明更新：支持 `type`/`url`/`headers`，明确"用户贴的 `{type:http,url:...}` 用 type=http+url，不要再塞 npx" |

### 前端

| 文件 | 改动 |
| --- | --- |
| `src/data/tools.ts` | `McpServer` 加 `type`/`url`/`hasHeaders`；`McpServerDraft` 加 `type`/`url`/`headers`/`preserveHeaders`；`validateDraft` 按类型校验（http 要 url 且须 http(s):// 开头，stdio 要 command）；新增 `parseHeaders`（每行 `KEY: VALUE` 或 `KEY=VALUE`，值里含分隔符不截断） |
| `src/components/tools/McpPanel.tsx` | 「传输类型」下拉（stdio/http）；http 时显示「端点 URL」+「请求头」框、隐藏 command/args；列表里显示 `HTTP` 徽标 + `http <url>`；`preserveHeaders` 与 `preserveEnv` 同一纪律（编辑时留空 = 保留原值，避免凭据被静默清掉） |

## 验证

### 测试

- 后端新增 `tests/test_kernel_mcp_http.py` **20 条**：配置解析（http 只要 name+url / headers 落库不回显 / 缺 url 跳过 / 缺 type 按 stdio 向后兼容）、SSE 解析（data 行 / batch / 跳过非 JSON）、HTTP 客户端（`httpx.MockTransport` 桩：initialize 抓 `Mcp-Session-Id`、tools/list 走 SSE、tools/call 走 JSON、JSON-RPC error→McpError、HTTP 500→McpError、**stdio 起子进程不回归**）、service（save 落 type/url/headers、http 缺 url 400、stdio 缺 command 400、preserve_headers 保留旧值、test_mcp_server http 握手）、engine（`_tool_add_mcp_server` http 保存成功）。
- 回归：`tests/test_kernel_mcp.py`（46 旧）+ 新 20 = **66 passed**；`tests/kernel/test_tools_catalog.py` + `tests/test_chat.py` = **79 passed**。ruff 改动文件干净（engine.py 剩余的是既有无关 emit/note 告警）。
- 前端：`tools.test.ts` 69 passed（+10：validateDraft 按类型 4 条、parseHeaders 6 条）；新建 `McpPanel.test.tsx` 5 passed（默认 stdio 无 url 框 / 切 http 出 url+请求头框且 command 消失 / 已存 http 列表显示 `http <url>` 与 HTTP 徽标 / http 缺 url 就地报错不回调 / http 合法 url 提交草稿 type=http、url 保留、command 空）。全前端 **78 files / 1157 passed**；`tsc -b --force`、eslint（改动文件 `--max-warnings 0`）、`npm run build` 全绿。

### 部署 + 线上端到端铁证

- 后端 5 文件 scp → prod `install -m 644`（带 `.bak-v162-<ts>`），prod 5 文件 sha **逐字节 == 本地**；重启 `Application startup complete`、health 200。
- **线上真连用户那条 URL**（prod venv 直跑 `McpClient`）：

  ```
  parse: [('ctf2-suvex', 'http', True, 'https://ctf2.dasctf.com/api/ai/v1/mcp')]
  connect result -> McpError : MCP 连接 `ctf2-suvex` 返回 HTTP 401：{"error":"invalid_token"}
  ```

  **关键对比**：修复前报 `stdout 已关闭（server 退出了）`（假象：像 server 崩了）；
  修复后真实到达远端 HTTP 端点、拿到协议级响应 `401 invalid_token` ——
  失败原因诚实且可操作（差一个鉴权 token），而不是误导。带 `Authorization` 头时
  头真被发出去（server 依旧因假 token 拒绝，证明 header 链路通）。

- 前端 dist 打包 → prod（`find dist -mindepth 1 -delete` 后 `cp -a`，不删 dist 目录本身），`index-oRm_7NE7.js` sha 两侧一致；`ToolsPage-CAxGOg-k.js` 含新文案。
- **视觉复核**（`artifacts/mcp-http-panel-v162.png`，真组件 + 真 prod CSS 渲染）：已存列表 `ctf2-suvex`【HTTP】【带请求头】→ `http https://ctf2.dasctf.com/api/ai/v1/mcp`；`demo-stdio`【stdio】→ `npx -y server`；添加表单有「传输类型」下拉、http 时出现「端点 URL」+「请求头（每行 KEY: VALUE）」框、无 command 框。

## 已知边界

- 用户那条 `ctf2.dasctf.com` 端点需要鉴权 token（服务端回 401 invalid_token）。本修复只保证**协议/传输正确**；真正调通还需用户在请求头里填有效 token（面板已支持）。
- HTTP 传输即用即连即断（每次 fetch_tools/describe/call 起一个 client），无跨任务持有 transport —— 规避了 strix 记录的 "cancel scope 绑任务" 陷阱。
