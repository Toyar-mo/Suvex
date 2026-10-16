# Suvex API 契约（v0 草案）

> **状态：待评审。** 这份文档是任务 0 的产物 —— 后续后端任务按它实现，前端按它接入。
>
> **权威顺序**：契约发现遗漏 → 先改本文档 → 再改代码。不要在实现里临时加字段。
>
> 字段命名的唯一依据是**前端现有的 `src/data/*.ts` 类型定义**。它们是需求方
> 迭代了十轮的产物，后端照抄命名，不要另起一套风格。

---

## 0. 三项决策（已定）

合集的前置假设里有三条与代码实际情况不符。**已按下面的结论定稿**，后续任务据此实施。

| #   | 议题     | 决策                                      | 一句话理由                                                             |
| --- | -------- | ----------------------------------------- | ---------------------------------------------------------------------- |
| 1   | 实时通道 | **SSE**                                   | 事件流单向，补发语义内建，前端代码已是此形状                           |
| 2   | 登录     | **要**：单管理员 + 会话 Cookie + 机器令牌 | 不加鉴权的 JSON API + 能对任意目标发包 = **CSRF 风险**，与团队规模无关 |
| 3   | 主键     | **字符串**                                | 前端 `id` 全部声明为 `string`                                          |

### 0.1 实时通道：用 SSE（不用 WebSocket）

|          |                                                                                                                                                                                                                              |
| -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 假设说   | 实时数据用 **WebSocket**                                                                                                                                                                                                     |
| 代码实际 | `src/data/activity-mock.ts` 的接入点注释与 `ActivityPage` 的 TODO 都写的是 **SSE**：`new EventSource(...)`、`Last-Event-ID`、`es.onerror → 重连`。`useActivityStream` 的消费端已按"重连 + 按 `at` 排序 + 120ms 批量合并"设计 |
| **决策** | **SSE**                                                                                                                                                                                                                      |

三条理由：

1. **事件流是单向的**（服务端 → 客户端）。WebSocket 的双向能力在这里用不上，
   却要多维护一层连接状态机。
2. **断线补发是内建的**。SSE 用 `Last-Event-ID` 请求头 + 服务端重放即可，
   浏览器 `EventSource` **自动带上这个头**。WebSocket 要在应用层自己实现补发游标 ——
   而"补发漏掉的事件"正是 `activity-mock.ts` 明确列出的、真实实现必须处理的三个复杂度之一。
3. 前端代码已经是这个形状，零改动。

> 代价（如实记）：SSE 走 HTTP/1.1 时有**每域名 6 连接**的限制。
> 但本平台同时只开一条流（Activity 页），不构成问题。
> 若将来要同时订阅多条流，走 HTTP/2 即可解除。
>
> WebSocket 的唯一实质优势是二进制帧与极低延迟 —— 这两条对"看 agent 在做什么"都用不上。

### 0.2 类型定义位置：`/src/data/`，不是 `/src/types/`

领域模型与数据源放在同一目录（`data/scan.ts` 既定义 `Scan` 类型又导出 `SKILLS` 常量）。
类型和它的取值来源在一起，避免两处漂移。**契约按实际路径写。**

### 0.3 登录：要，但做到最小

|          |                                                                                                                                                                                                     |
| -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 假设说   | 至少覆盖「认证登录」端点                                                                                                                                                                            |
| 代码实际 | 全站无登录界面、无用户态、无鉴权头。所有 `auth`/`login` 命中都是**漏洞数据的内容**（`POST /api/v2/auth/login` 是被测目标的接口），不是本平台的认证。Settings 只有「修改密码」，且 mock 一个固定口令 |
| **决策** | **加登录**：单管理员 + 会话 Cookie + 机器令牌，不做用户管理                                                                                                                                         |

#### 为什么必须加（不是"更安全"这种泛泛的理由）

决定性的是 **CSRF**，它与团队规模无关：

> 一个**无鉴权的 JSON API** 如果监听在 localhost / 内网，那么用户浏览器访问的
> **任意网页**都能向它发请求。攻击者只需在页面里写：
>
> ```html
> <script>
>   fetch('http://127.0.0.1:8000/api/v1/scans', {
>     method: 'POST',
>     body: JSON.stringify({
>       target: 'victim.internal',
>       targetKind: 'url',
>       skills: ['web'],
>       authorization: { confirmed: true, statement: '…' },
>     }),
>   })
> </script>
> ```
>
> 用户打开这个页面，**他的机器就替攻击者发起了一次针对任意目标的攻击扫描**。
> 这不是理论风险 —— "drive-by localhost" 是已知的成熟攻击类别
> （路由器/本地服务被网页扫出并利用）。
>
> 而这个平台的危害不是"读到数据"，是**替攻击者发包**。受害者（目标方）
> 看到的来源是**用户的 IP**。加密与网络隔离都挡不住这一点 ——
> 请求是用户自己的浏览器合法发出的。

第二条理由（次要但真实）：`scan` 级别操作能在目标环境产生副作用，
不该让"能访问到端口"等同于"能发起扫描"。

#### 最小化的实现边界（避免过度设计）

| 做什么                                                 | 不做什么（合集明确不在此轮范围）                              |
| ------------------------------------------------------ | ------------------------------------------------------------- |
| 单管理员账号，首次启动设置口令                         | 多用户、RBAC、用户管理界面                                    |
| **会话 Cookie**（HttpOnly + SameSite=Strict + Secure） | 服务端会话存储换成 JWT（本地单机不需要，且 JWT 无法即时吊销） |
| 登录 / 登出 / 取当前用户 三个端点                      | 注册、找回密码、邮箱验证（没有邮件基础设施）                  |
| 机器访问用既有的 `TokenScope` 体系（Bearer 头）        | 多租户隔离                                                    |

> **人用会话与机器用令牌是两条通道，不要合并**：
> · 人在浏览器里 → 会话 Cookie（自动携带，适合浏览器）
> · CI / 脚本 → `Authorization: Bearer svx_…`（无状态，适合机器）
>
> 这也解释了 `TokenScope`（`read`/`scan`/`admin`）为什么早已存在却与登录无关 ——
> 它服务的是机器通道。

#### CSRF 防护（Cookie 方案的必配项）

| 措施                                            | 作用                                                                                                                                   |
| ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `SameSite=Strict`                               | 跨站发起的请求不带 Cookie，直接挡掉绝大多数 CSRF                                                                                       |
| `HttpOnly`                                      | **不可被 JS 读取** —— 页面上会渲染不可信的漏洞证据文本（`detail.evidence`、`poc.code`），若令牌在 `localStorage` 里，任何 XSS 都能读走 |
| 所有写操作要求 `Content-Type: application/json` | 非简单请求会触发 CORS 预检，跨站表单发不出来                                                                                           |
| `Secure`（生产）                                | 仅在 https 下传输                                                                                                                      |

> **为什么不用 `localStorage` + Bearer**：那个方案没有 CSRF 问题（跨站请求带不上自定义头），
> 但把令牌暴露给 XSS。而本平台**必然渲染不受信的文本**（扫描目标的响应内容、
> agent 产出的 PoC 源码）。两害相权，选 Cookie + SameSite。

---

## 1. 通用约定

### 1.1 基础

| 项       | 值                                                                                                                                     |
| -------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Base URL | `${VITE_API_BASE_URL}`，开发态 `http://127.0.0.1:8000`                                                                                 |
| 版本前缀 | `/api/v1`                                                                                                                              |
| 传输     | JSON（`Content-Type: application/json; charset=utf-8`）                                                                                |
| 时间格式 | **ISO 8601 带时区**（`2026-09-23T15:30:42.123Z`）。与前端 `string` 类型的 `at`/`startedAt`/`discoveredAt` 等字段一致                   |
| 命名风格 | **snake_case？不 —— 用 camelCase**，与前端 TypeScript 字段完全一致（`targetKind`、`agentCount`、`discoveredAt`）。前端不做字段名转换层 |
| 主键格式 | 沿用前端演示数据的风格：`scan_01J8X2`、`agent-03`、`evt_09`、`finding_*`。**是字符串，不是整数**                                       |

> **主键必须是字符串**：前端的 `id` 全部声明为 `string`，且 mock 里形如 `scan_01J8X2`。
> 后端若用自增整数，前端要改成 number 才能对齐 —— 没必要。用 ULID/UUID 或带前缀的字符串。

### 1.2 鉴权（已定：单管理员 + 会话 Cookie + 机器令牌）

两条**互相独立**的通道，不要合并：

| 通道       | 谁用         | 凭证                          | 传输                 |
| ---------- | ------------ | ----------------------------- | -------------------- |
| 人在浏览器 | 管理员本人   | **会话 Cookie**               | 自动携带，`HttpOnly` |
| 机器 / CI  | 脚本、流水线 | `Authorization: Bearer svx_…` | 显式头               |

**为什么分开**：浏览器场景需要"能自动携带、不能被杀掉"的凭证 → Cookie；
机器场景需要"无状态、可吊销、可限定 scope" → 令牌。
用同一套会在两边都别扭（Cookie 传不进 CI；令牌放浏览器里有 XSS 暴露面）。

#### 会话 Cookie 的属性（缺一不可）

```
Set-Cookie: suvex_session=<opaque>; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200
                                              ↑ 生产环境再加 Secure
```

| 属性              | 作用                | 漏掉的后果                                                                      |
| ----------------- | ------------------- | ------------------------------------------------------------------------------- |
| `HttpOnly`        | JS 读不到           | 页面上渲染不可信文本（`detail.evidence`、`poc.code`），XSS 一旦成功就能偷走会话 |
| `SameSite=Strict` | 跨站请求不带 Cookie | **CSRF**：任意网页都能替用户发起扫描（见 §0.3 的完整场景）                      |
| `Secure`          | 仅 https 传输       | 明文信道泄露会话                                                                |
| `Max-Age`         | 12 小时过期         | 会话永久有效                                                                    |

> 会话值应是**不透明随机串**，服务端存哈希。不要用 JWT —— 本地单机部署不需要
> 无状态扩展，而 JWT **无法即时吊销**（改密码后旧令牌仍有效）。

#### 首次启动：设置管理员口令

用户实例上没有账号，所以首次启动走**一次性初始化**：

| 路径       | `/auth/setup`                                                           |
| ---------- | ----------------------------------------------------------------------- |
| 何时可用   | **仅当系统内不存在任何账号时**。存在账号后返回 `409 SETUP_ALREADY_DONE` |
| 请求       | `{ "password": "…" }`                                                   |
| 服务端校验 | 复用前端的密码强度规则（≥12 字符 + 大小写 + 数字，见 `checkPassword`）  |
| 响应       | `201` + 自动登录（下发会话 Cookie）                                     |

> ⚠️ **这个端点必须在"已初始化"后彻底关闭**，否则它就是一个
> "任意人可重设管理员口令"的后门。前端据此决定显示登录页还是初始化页。

#### 鉴权级别定义

| 级别     | 含义                                  | 谁能用                                 |
| -------- | ------------------------------------- | -------------------------------------- |
| `public` | 无需鉴权                              | `/health`、`/auth/setup`（未初始化时） |
| `read`   | 只读：看扫描、漏洞、报告、设置        | 会话 或 `read` scope 令牌              |
| `scan`   | 发起/终止扫描 —— **会对真实目标发包** | 会话 或 `read`+`scan` scope 令牌       |
| `admin`  | 改设置、管 provider、管令牌、改密码   | 会话 或 `admin` scope 令牌             |

> 这三档**不是新发明的**：`src/data/settings.ts` 里的 `TokenScope` 已经是
> `'read' | 'scan' | 'admin'`，且带风险说明文案。契约直接复用，不要另立四级。
>
> **管理员会话隐含 `admin`**：人登录后不必再持有令牌。令牌是给机器用的。

### 1.3 统一错误响应

前端的保存/校验 UI 已经按 `SaveResult`（`{ status, error }`）和 `ActionResult`
两个形状渲染，**错误必须是可渲染的一句话**，不是错误码。

```jsonc
// 任何非 2xx 响应都用这个形状
{
  "error": {
    "code": "SCAN_TARGET_NOT_AUTHORIZED", // 机器可判别，snake 大写下划线
    "message": "该目标尚未确认授权，无法发起扫描。", // 直接显示给用户的一句话
    "details": {
      // 可选，字段级校验错误
      "target": "请先在确认框中勾选授权声明",
    },
  },
}
```

**三条硬要求：**

1. **`message` 必须是可直接展示的中文完整句** —— 前端 `SaveBar` 会把
   `error` 拼进「保存失败：<error>」，所以它不能是 `"invalid_target"` 这种码。
2. **一次返回全部字段级错误**（`details`），不要"改一个报一个"。
   前端表单是逐字段就地报错的（`components/settings/Field.tsx` 的 `error` prop）。
3. **不泄露内部信息**：不返回堆栈、不返回 SQL、不回声用户输入。
   `changePassword` 的错误文案已经定了调子 —— 只写「当前密码不正确」，
   不透露任何额外信息。

#### 错误码表（契约级，实现时逐个落实）

| HTTP | code                    | 何时                                                  |
| ---- | ----------------------- | ----------------------------------------------------- |
| 400  | `VALIDATION_FAILED`     | 字段校验失败，配 `details`                            |
| 401  | `UNAUTHENTICATED`       | 未登录 / 令牌无效                                     |
| 403  | `INSUFFICIENT_SCOPE`    | 令牌 scope 不够（如 `read` 令牌想发起扫描）           |
| 403  | `TARGET_NOT_AUTHORIZED` | **未勾选授权确认**（见 §5）                           |
| 403  | `CROSS_ORIGIN_BLOCKED`  | 带 Cookie 的写请求来自未授权源（见 §1.2 的 CSRF 防护）|
| 404  | `NOT_FOUND`             | 资源不存在                                            |
| 405  | `METHOD_NOT_ALLOWED`    | 路径存在但不支持该方法                                |
| 409  | `SCAN_NOT_ABORTABLE`    | 对已完成扫描调终止（前端 `abortScan` 对此返回 false） |
| 409  | `PROVIDER_KEY_CHANGED`  | 改过 key 未重新验证就拿来跑扫描（见 §3.2）            |
| 409  | `PROVIDER_IS_DEFAULT`   | 删一个还有替补的默认 provider（见 §3.2）              |
| 409  | `PROVIDER_IN_USE`       | 删一个被未结束扫描引用的 provider（见 §3.2 / §6）     |
| 422  | `PROVIDER_UNREACHABLE`  | 验证 provider 时打不通                                |
| 429  | `RATE_LIMITED`          | 限流。带 `Retry-After` 头（已列入 CORS `expose-headers`）|
| 500  | `INTERNAL_ERROR`        | 兜底，`message` 仍是可读中文                          |
| 500  | `PROVIDER_KEY_UNREADABLE` | 密文解不开（`SUPEX_SECRET_KEY` 换了或丢了，见 §2.9）|
| 409  | `SCAN_NOT_RESUMABLE`    | 续跑的前置条件不满足（见 §3.3.2）                    |
| 409  | `WRITEUP_UNAVAILABLE`   | 没有可用的题解报告（见 §3.18）                       |
| 409  | `SCAN_IS_CTF2_SHELL`    | 想续跑一条 CTF2 借的壳扫描（它从没跑过，见 §3.19.4） |
| 409  | `LAST_ADMIN`            | 删/降级最后一个管理员（见 §2.11 的防自锁规则）       |
| 409  | `CANNOT_DELETE_SELF`    | 删当前登录的账号（见 §2.11）                         |
| 409  | `USERNAME_TAKEN`        | 用户名已被占用（大小写不敏感，见 §2.11）             |
| 409  | `SETUP_ALREADY_DONE`    | 已初始化过管理员，再调 `/auth/setup`（v0.21 补记）   |
| 501  | `NOT_IMPLEMENTED`       | 端点已定义但尚未实现（后端分期交付，见 §9）           |

> **405 与 501 是 v0.2 补的**，实现时才发现契约漏了这两种情况：
>
> * 少了 405，Starlette 会回它自己的 `{"detail": "Method Not Allowed"}` ——
>   形状和语言都不对，前端拿不到 `error.code`；
> * 少了 501，就只能在「注册了但没实现」与「干脆不注册（404）」之间选，
>   而两者都在说谎 —— 404 的语义是「资源不存在」，
>   真实情况是「端点还没做」，调用方该做的处理完全不同。

---

## 2. 字段对照表（前端类型 ↔ API 字段）

**这张表是契约的核心。** 后端 DTO 字段名必须与左列**逐字一致**。

### 2.1 Scan（`src/data/scan.ts` 的 `Scan`）

| 字段            | 类型                                                            | 说明                                                                      |
| --------------- | --------------------------------------------------------------- | ------------------------------------------------------------------------- |
| `id`            | string                                                          | `scan_01J8X2`                                                             |
| `name`          | string                                                          | 用户可读名，默认取目标主机名                                              |
| `target`        | string                                                          | **已归一化**（见 §2.8）                                                   |
| `targetKind`    | `'url' \| 'repo' \| 'path' \| 'service' \| 'artifact'`                                     |                                                                           |
| `status`        | `'queued' \| 'running' \| 'completed' \| 'failed' \| 'aborted'` |                                                                           |
| `skills`        | `SkillId[]`                                                     | `'web' \| 'api' \| 'container' \| 'cloud' \| 'dependencies' \| 'secrets' \| 'protocol'` |
| `providerId`    | string                                                          | 对应 Settings 里的 provider                                               |
| `startedAt`     | ISO string                                                      |                                                                           |
| `finishedAt`    | ISO string \| null                                              |                                                                           |
| `durationSec`   | number \| null                                                  | **running 时必须为 null**，由 `startedAt` 推算                            |
| `agentCount`    | number                                                          |                                                                           |
| `findings`      | `Partial<Record<Severity, number>>`                             | 按严重度分档的计数                                                        |
| `failureReason` | string \| null                                                  | 仅 failed/aborted 有值                                                    |
| `progress`      | number \| null                                                  | 0–1，仅 running/queued 有意义                                             |
| `runId`         | string \| null                                                  | **外部执行工具侧的运行标识**（v0.4 新增，见 §3.3.1）                       |
| `archivedAt`    | string \| null                                                  | 归档时刻（v0.13 新增）。**非空 = 已归档**，默认列表不返回它                |
| `deletedAt`     | string \| null                                                  | 软删除时刻（v0.13 新增）。非空 = 在回收站，默认列表也不返回它              |
| `terminatedBy`  | `'user' \| 'system' \| 'unknown'` \| null                       | 谁终止的（v0.13 新增）。仅 `aborted`/`failed` 可能有值                     |
| `stalledAt`     | string \| null                                                  | 疑似卡死的判定时刻（v0.17 新增）。**非空 = 服务端判定「它很久没有新事件了」** |
| `lastEventAt`   | string \| null                                                  | 最近一条活动事件的时刻（v0.17 新增）。没有事件时为 null                     |
| `notice`        | string \| null                                                  | 目标可达性预检的一句人话（v0.17 新增）。**只有创建扫描的响应带它**          |

| `flag`          | string \| null | **本次解出的 flag**；没解出为 `null`（见 §3.18.1）|

> **为什么是 `archivedAt` / `deletedAt` 而不是布尔 `archived` / `deleted`（v0.13 定案）**：
> 布尔少一个维度 —— **什么时候归档的**。"失败 30 天后自动归档"这条规则要能
> 事后解释"这条为什么被归档了"，回收站也要按时间算到期。
> 布尔与 `archivedAt IS NOT NULL` 在查询上完全等价（可建部分索引），
> 所以用时间戳**不损失任何能力**，反而多出审计价值。
>
> **`terminatedBy` 的取值必须诚实（v0.17 精确化）**：
>
> | 值 | 含义 | 写入点 |
> | --- | --- | --- |
> | `user` | **用户**在界面上按了终止 | `abort_scan` |
> | `system` | **服务侧**的生命周期事件带走的 —— 服务重启 / 热重载 / 进程退出 / 启动时回收上次的孤儿 | `_terminate_tracked`（`stop()` 触发）、`recover_orphans` |
> | `unknown` | 无法判定（主要是历史数据） | 不写 |
> | `null` | 非 `aborted`/`failed`，即"没有终止方这个概念" | 不写 |
>
> **`system` 指的是"服务侧的终止"，不是"扫描自己失败"。** 一次
> `failed` 的扫描（Docker 不可用、认证失败、进程崩溃）**没有终止方** ——
> 它是自己挂掉的，没有人停它。所以 `failed` 记录上不要显示「终止方」这一栏，
> 那会把两个不同的概念混成一个词：用户会以为"有人停了它，但不知道是谁"，
> 而事实是"根本没有人停它"。
>
> ⚠️ **这条曾经整体失效**：字段、契约、前端类型、UI 上那栏全都在，
> 而三条终止路径**一条都没赋值** —— 每个值都是 `None`，界面永远显示
> "无法判定"。更糟的是**同一件事有两种措辞**：strix 日志写
> `interrupted by the user`，而 Suvex 记 `服务停止，扫描已终止`，
> 排查时会以为是两种不同的原因。这就是它现在被写死在这里的原因：
> **一个只被读、从来不被写的字段，比没有这个字段更坏** ——
> 它的存在让人以为这件事已经被记录下来了。
>
> **没有 `service` 这个值**：前端类型是 `'user' | 'system' | 'unknown'`，
> 后端必须与之逐字一致。我曾一度在实现里写 `"service"`，那是**凭空多出来的
> 一个词** —— 前端会把它落到 `default` 分支，界面上什么都不显示。

> **`progress` 后端不填。** strix 不提供可靠的进度，而按"已跑时间 ÷ 预期时长"
> 算一个百分比是**编造** —— 它会在第 30 秒显示 50%、在第 5 分钟还显示 50%，
> 然后突然跳到 100%。所以它在 `running` 时是 `null`，
> 前端应渲染成"进行中"（不确定态）。那个联合类型里本来就有 `null`。
>
> **`runId` 是真实产出的，不是摆设**：它是外部工具自己在
> `strix_runs/<runId>/` 下建的那个目录名（`events.jsonl` 就在里面）。
> 任务 3 要求「记录 strix 侧生成的 run 标识，关联到本地的 scan 记录」——
> 而"能关联"意味着人要能看见它、拿去 strix 那边对账，所以它出现在响应里。
> 进程还没起来、或者工具在生成它之前就失败了的时候是 `null`。

> **`stalledAt` / `lastEventAt`：判定在服务端，措辞在前端（v0.17 定案）。**
>
> 起因是一次真实事故：一次扫描跑了 **32 分 16 秒零产出**，而界面从头到尾
> 只是一个转圈的图标。目标是 CTF 主机 `xxx.http-ctf2.dasctf.com`，
> **用户输入里没有协议**，agent 选了 `https://`，而该主机只在 http 下有应答
> （http → 0.06 秒 404，https → TLS 握手失败）。它随后反复用**空输入**
> 轮询同一个 shell 会话（45 次 `write_stdin {"chars":""}`，等待从 15 秒
> 一路涨到 60 秒）—— 这是"一个命令永远不返回"的形态。用户既不知道
> 还该不该等，也不知道哪里出了问题。
>
> 判据是**事件时间戳**而不是 CPU 或输出量：`_import_for` 每轮都把外部
> 工具新写出的事件导进来，所以"事件停了"就等于"agent 确实没在产出"，
> 与它在忙什么无关。用 CPU 判断会误伤"在跑长命令但没输出"的正常情况。
>
> 三个必须写死的规则：
> 1. **阈值与判定都在服务端**（`scan_stall_seconds`，0 = 关掉检测）。
>    前端不自己比时间戳 —— 否则同一屏里会出现"一个标了一个没标"，
>    而用户改不了那个阈值，他没法解释为什么。
> 2. **默认只标记、不终止**（`scan_stall_action="flag"`）。这是个启发式
>    判断，而终止一个正在跑真扫描的进程不可逆；宁可让用户看到提示后
>    自己决定。要真终止需要显式配成 `abort`。
> 3. **标记可自愈**：一旦又有新事件进来就清掉。否则一次误判（某个工具
>    确实安静了 11 分钟才输出）会让界面永远挂着"疑似卡死"，而它其实
>    早已继续。终态时也必须清掉 —— "卡死"是个**悬而未决**的判断，
>    已经有结论的记录不该再提示它。
>
> `lastEventAt` 单独给出来，是为了让前端算「已静默 N 分钟」：**相对时长
> 不敏感服务端与客户端的钟差**，而"是否卡死"敏感 —— 所以前者本地算、
> 后者交给后端。这就是两个字段都给的理由。

> **`notice` 不是可以事后查询的属性（v0.17 定案）。**
>
> 它是"**刚刚那次创建**观察到的事实"，所以 `POST /scans` 的响应带它，
> `GET /scans` 一律为 `null` —— 它不会进列表，也不会被缓存成一条记录。
>
> 为什么值得占一个字段：用户填 `example.com` 时心里想的可能是 `https`，
> 而真相可能相反。不告诉他，他会看着一次毫无产出的扫描猜半天。
> 所以预检的一句话必须**在创建那一刻**到达用户眼前。
>
> **预检永不阻断创建**（探测超时 4 秒 + 连不上也只是"没探到"）：
> 目标是内网地址、防火墙静默丢包、或要过跳板才能到达，都是正常情况，
> 探测失败不能当成"目标有问题"。它的产物只有两样：`probedTarget`
> 和 `notice`。
>
> ⚠️ **探测结果存在 `probedTarget` 这一列，绝不覆盖 `target`。**
> 我第一版把探测到的带协议地址写进了 `target`，被
> `test_target_is_normalized_before_storage` 当场抓住：归一化的语义是
> **剥掉**协议，而"同一目标的三种写法必须收敛成一条记录"正是它要防的。
> 写进去之后 `example.com` 与 `https://example.com` 又会变成两条 ——
> 修法是**新加一列**存探测结论，`target` 保持归一化不变。
> 用户显式写了协议时，预检**绝不改写**它（用户说了算，预检只回答问题）。

> **`findings` / `agentCount` 是服务端维护的汇总（rollup），口径在服务端。**
> 它们不是"列表时现算"的：一屏几十条扫描，逐条去 count 会变成几十个查询。
> 所以计数被冻在扫描行上，**由服务端负责在数据变动时刷新**。
>
> 前端据此可以放心地直接渲染这两个字段（列表页的严重度小标签、
> "N 个智能体"、"累计发现 M 条漏洞"都是它们），但也正因为如此，
> **它们必须是真的** —— 这两个字段一旦腐烂，显示出来的是 `{}` / `0`
> 而不是报错，页面上读起来就是一句理直气壮的"未发现漏洞"。
>
> ⚠️ 任务 7（前端接入）就撞上过这一条：后端刷新的函数写好了却没有调用点，
> 于是扫描列表说"未发现漏洞 / 0 个智能体"，而同一屏旁边的漏洞页写着两条。
> 详见 `suvex-api/docs/verification-report.md` §5。
>
> `findings` 的口径：**按严重度分档**，且**排除已判定为误报的条目**；
> 没有的档位**键不存在**（`Partial`），不是 `0`。所以"把一条漏洞标成误报"
> 会让这个映射变小 —— 这是有意的，不是 bug。

> ⚠️ **`startedAt` 非空 ⇒ `queued` 状态下它等于"任务被受理的时刻"。**
> 上面那张表把 `startedAt` 定为 `ISO string`（不可空）、而 `finishedAt` 明确可空。
> 所以对还没开跑的扫描，它记录的是入队时刻。真正的开跑时刻由 `status`
> 变成 `running` 体现（事件流里有）。
>
> **v0.4 补充：轮到它真正开跑时，`startedAt` 会被改写成真实开跑时刻。**
> 不改的话，`durationSec = finishedAt - startedAt` 会把**排队等待的时间**
> 也算进"扫描耗时"—— 那是两个不同的东西（用户问"这次扫描花了多久"，
> 想知道的是它跑了多久，不是它在队列里躺了多久）。
> 于是 `startedAt` 的含义精确化为：**还没开跑时是受理时刻，开跑后是开跑时刻**。
>
> 代价如实记：**排队等了多久，事后查不出来**。真要这个数据，
> 应该由调度器在认领时写一条 `activity_events`（事件自带时间戳），
> 而不是加一列 —— 加列会让"哪个才是排队开始时间"变成一个每次都要重新判断的问题。
>
> 后端实现**刻意不额外加一列** `actuallyStartedAt`：那会引入第二个时间来源，
> 而「哪个才是开始时间」会立刻变成每次查数据都要重新判断的问题；
> `durationSec = finishedAt - startedAt` 这个算法也会在两处不一致。
> （v0.2 的实现注记。）

> ⚠️ **`durationSec` 在 running 时必须是 `null`，不能是 0。**
> 前端排序逻辑依赖这个区分（`lib/scan-query.ts` 的 `duration` 分支）：
> 把未结束的扫描当 0 处理，会让"正在跑的"排到"最快的"位置。

> ⚠️ **`findings` 是 `Partial`**：没有发现某一级别时**该键不存在**，不是 `0`。
> 前端用 `scan.findings[sev] ?? 0` 读取，两种都能处理，但契约上以"不存在"为准。

### 2.2 Finding（`src/data/finding.ts` 的 `Finding`）

**这是全站最需要精确对齐的类型**，因为它有**两个独立的轴**：

| 字段               | 类型                                                  | 说明                             |
| ------------------ | ----------------------------------------------------- | -------------------------------- |
| `id`               | string                                                |                                  |
| `scanId`           | string                                                |                                  |
| `severity`         | `'critical' \| 'high' \| 'medium' \| 'low' \| 'info'` |                                  |
| `category`         | `FindingCategory`                                     | 8 个值，见下                     |
| `title`            | string                                                |                                  |
| `target`           | string                                                | 受影响目标                       |
| **`verification`** | `'none' \| 'partial' \| 'verified'`                   | **轴一：证据强度（agent 产出）** |
| **`status`**       | `'new' \| 'confirmed' \| 'fixed' \| 'retesting' \| 'closed' \| 'false_positive'` | **轴二：处置状态（人工流转）**   |
| `discoveredAt`     | ISO string                                            |                                  |
| `detail`           | `FindingDetail`                                       | 见下                             |
| `timeline`         | `FindingTimelineEntry[]`                              | **倒序**（最新在前）             |

> ⚠️⚠️ **`verification` 和 `status` 绝不可合并成一个字段。**
> `data/finding.ts` 顶部用一整段注释论证过：一条漏洞可以是
> "已用 PoC 证明存在（verified），但判定不影响业务（false_positive）"。
> 合成一个字段，这个真实场景就无处表达 —— 而它是渗透测试里最常见的争议点。
> **后端表结构也必须是两列**，不要图省事用一列存。

#### `FindingSummary` 上的 `deletedAt`（v0.18）

列表项（`FindingSummary`）新增 `deletedAt: string | null`，
**与 `Scan.archivedAt` / `Scan.deletedAt` 逐字同构**（契约 §2.1）：

* 用**时间戳**不用布尔 —— 布尔丢掉"什么时候删的"，
  而回收站要按时间算到期、界面也要能说"3 天前删的"。
* 它只在 `?deleted=true` 的响应里非空（列表默认排除回收站条目）。
* 前端据此渲染「在回收站」标记与"恢复/永久删除"两个动作。

`FindingCategory` 取值（译句子不译标识符，这些是代码值）：
`injection` / `broken_access_control` / `auth` / `xss` / `ssrf` / `misconfiguration` / `info_disclosure` / `crypto`

#### FindingDetail

| 字段          | 类型                         | 说明                                                      |
| ------------- | ---------------------------- | --------------------------------------------------------- |
| `description` | string                       | 这是什么问题、影响是什么                                  |
| `affected`    | string[]                     | 受影响位置，可能多个                                      |
| `reasoning`   | string                       | **为什么判断这里有问题** —— 不是 `description` 的同义反复 |
| `evidence`    | string                       | 真实响应片段，**原样保留不翻译**                          |
| `poc`         | `{ language, code } \| null` | null = 无脚本                                             |
| `remediation` | string \| null               | null = agent 没把握给建议                                 |

#### FindingTimelineEntry

| 字段      | 类型                                                                        | 说明                     |
| --------- | --------------------------------------------------------------------------- | ------------------------ |
| `id`      | string                                                                      |                          |
| `at`      | ISO string                                                                  |                          |
| `actor`   | `'agent' \| 'user'`                                                         | agent 产出 vs 人工流转   |
| `kind`    | `'detected' \| 'poc_generated' \| 'verified' \| 'status_changed' \| 'note'` |                          |
| `summary` | string                                                                      | 一句话                   |
| `from`    | `FindingStatus \| null`                                                     | 仅 `status_changed` 有值 |
| `to`      | `FindingStatus \| null`                                                     | 同上                     |

### 2.3 状态流转规则（后端必须执行同一张表）

`data/finding.ts` 的 `FINDING_STATUS_TRANSITIONS` 是**业务规则，不是 UI 细节**：

```ts
new:             ['confirmed', 'false_positive']
confirmed:       ['fixed', 'false_positive', 'new']
fixed:           ['retesting', 'closed', 'confirmed']
retesting:       ['closed', 'confirmed']
closed:          ['confirmed']
false_positive:  ['new', 'confirmed']
```

**后端必须校验**：非法流转返回 `400 VALIDATION_FAILED`。
不要依赖前端不显示非法按钮 —— 那是双保险里的第二道，不是唯一一道。

> **v0.14 新增两个状态与三条边**（`retesting` / `closed`）：
>
> - `fixed → retesting`：提交复测。
> - `fixed → closed`：**直连关闭**。保留这条边是刻意的 —— 需求原文写的是
>   「已修复 → 复测中 → 已关闭（**如需要**复测环节）」，"如需要"说明复测
>   不该是唯一路径。强制经过它的代价是：一条明显修好的漏洞（比如改了个
>   响应头）也要多走一次流转，用户会开始乱点。
> - `closed → confirmed`：**可以重新打开**。漏洞会复发，修复也会不彻底；
>   没有回路的终态会逼用户把"重新打开"记成一条新记录，而那会切断历史 ——
>   "这条漏洞反复出现过几次"就再也查不出来了。
>
> `retesting` 的语义是"**正在**验证修复效果"：它是一个进行中的标记，
> 不是审批节点。`fixed` 与 `retesting` 都**不能**直接判误报，
> 必须先退回 `confirmed` —— 让"修复"这个已经发生的动作保持可追溯。

> 另：**幂等**。把已经是 `confirmed` 的条目再标 `confirmed` 应返回成功但
> `changed: 0`（见 §3.4 的响应形状），**且不写时间线** ——
> 否则时间线里会出现"待确认 → 待确认"的噪音记录。

### 2.4 Agent（`src/data/activity.ts` 的 `Agent`）

`GET /scans/{id}/agents` 返回它，Activity 页左侧名册消费。

| 字段           | 类型          | 说明                                       |
| -------------- | ------------- | ------------------------------------------ |
| `id`           | string        | `agent-03`                                 |
| `role`         | `AgentRole`   | 见下                                       |
| `status`       | `AgentStatus` | 见下                                       |
| `target`       | string        | 该 agent 当前负责的目标                    |
| `scanId`       | string        | 所属扫描（多个 agent 同属一次扫描 = 一队） |
| `eventCount`   | number        | 累计动作数                                 |
| `lastActiveAt` | ISO string    | 用于排序                                   |

> ⚠️ **`Agent.id` 必须全局唯一，所以它不是 `agent-03` 这种扫描内序号。**
> 上面那格举例沿用的是演示数据的风格，但 §1.1 的**规则**是「带前缀的字符串」，
> 而全局唯一是硬要求：§4.1 的全局流 `/api/v1/events` 会同时推送多个扫描的 agent，
> 前端 `useActivityStream` 把它们收进一个按 `id` 索引的 map ——
> 用扫描内序号会让两个扫描的 `agent-01` 互相覆盖，**名册上少一个人，且失败是静默的**。
>
> 后端实现取 `agent_<ULID>`。展示层要的「03」由前端按名册顺序渲染，它不需要成为主键。
> （这条是 v0.2 的实现注记：契约的规则对，举例会误导。）

**`AgentRole`**（5 种，角色决定它做什么）：

```ts
;'recon' | 'scanner' | 'exploiter' | 'verifier' | 'reporter'
```

**`AgentStatus`**（5 种）：

```ts
;'running' | 'idle' | 'blocked' | 'done' | 'failed'
```

> ⚠️ **`AgentRole` 与 `AgentPhase` 是两套值，别合并**（`recon` 在两边都有，
> 但 `scanner` vs `scan`、`exploiter` vs `exploit`、`verifier` vs `verify`、
> `reporter` vs `report` 拼写不同）：
>
> |              | 定义在             | 用途                                             |
> | ------------ | ------------------ | ------------------------------------------------ |
> | `AgentRole`  | `data/activity.ts` | **agent 长期承担的角色**（名册上显示"信息收集"） |
> | `AgentPhase` | `data/overview.ts` | **某个事件所处的阶段**（活动流里显示"利用中"）   |
>
> 一个 `exploiter` 角色的 agent 完全可能产生一个 `scan` 阶段的事件。
> 后端若共用一个字段，前端两处标签会有一个显示错。

> 前端目前是**从事件流派生**名册统计的（`useActivityStream` 的 `agents` memo
> 会覆盖 `eventCount` / `lastActiveAt` / 把有事件的 agent 标成 `running`）。
> 所以后端返回的这两个字段是**初始值/兜底值**，前端会按事件流更新。
> 这不算重复 —— 它让"agent 失败了但没有后续事件"这种情况能被表达。

### 2.5 ActivityEvent —— ⚠️ 有**两个形状**，别搞混

这是契约里最容易出错的一处。前端有**两个不同的 `ActivityEvent`**：

|          | 完整事件                                                                                         | Overview 投影                                                 |
| -------- | ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------- |
| 定义在   | `data/activity.ts`                                                                               | `data/overview.ts`                                            |
| 用于     | `/activity` 页、事件流                                                                           | `/overview` 页的实时活动流                                    |
| 字段     | `id, scanId, agentId, type, summary, detail, at`                                                 | `id, scanId, agentId, agentPhase, type, summary, subject, at` |
| 判别式   | `detail.kind`（discriminated union，8 种载荷）                                                   | `agentPhase` + `subject`（扁平）                              |
| 事件类型 | **8 种**：`tool_call, reasoning, discovery, poc_generated, verified, blocked, failed, completed` | **6 种**：少了 `reasoning` 和 `failed`                        |

**处理方式（建议）**：后端**只发完整事件**（8 种，带 `detail`）。

- `/activity` 直接消费完整事件 —— 已有代码路径；
- **除 `/overview` 的 `activity` 外**，投影由前端从完整事件派生，
  不要后端再发一套精简流。

理由：Overview 那 6 种是 `data/overview.ts` 里的**展示层视图模型**，
是为"一行摘要"渲染优化的形状。让后端产出它等于把展示逻辑搬到后端，
一旦 UI 改摘要规则就要动后端。

> ⚠️ **`/overview` 的 `activity` 是一个例外，而且这一节原本自相矛盾**
> （v0.8 才理清）：
>
> §3.6 的 `/overview` 响应里 `activity` 写的是「投影形状，**见 §2.5**」，
> 而 §2.5 说的恰恰是「不要后端发投影」。**后者指向前者、前者否定了后者。**
>
> 实现时必须在两者间择一，最终**选 §3.6**（后端给投影）。理由：
> §2.5 反对后端投影的依据是"投影是展示层视图模型，后端产出它就要跟着
> UI 的摘要规则走"——但看它自己的字段表，投影与完整事件的 `summary`
> 是**同一个字段**，投影真正丢掉的只是 `detail`（换成扁平的 `subject`）。
> 也就是说**"摘要怎么截"本来就在产生事件的那一侧**，后端并没多承担什么。
> 而 §3.6 的 `OverviewData` 是**已经定稿的前端类型**：让响应对不上它，
> 前端接线时要么改类型、要么在页面里再写一次投影 ——
> 而后者正是 §2.5 原本想避免的。
>
> 所以现在的口径是：**`/overview.activity` 由后端投影（6 种、扁平、有
> `subject`）；其余任何地方都只发完整事件。** §2.5 的建议已按此收窄。

> **但 `agentPhase` 是个例外**：它在投影里，而完整事件里**没有**。
> 而 `ActivityFeedItem` 渲染阶段标签（"利用中"）时必须它。
> 所以契约要求：**完整事件里也带 `agentPhase`**，作为附加字段
> （`AgentPhase = 'recon' | 'scan' | 'exploit' | 'verify' | 'report'`）。
> 这是本次契约相对前端现状的**唯一一处新增字段** —— 因为前端两个形状
> 各缺对方有的东西，后端必须带全，前端才好投影。

### 2.6 EventDetail 的 8 种载荷（`detail` 的判别式）

```jsonc
{ "kind": "tool_call",     "tool": "nmap", "command": "...", "exitCode": 0, "durationMs": 4200, "output": "..." }
{ "kind": "reasoning",     "text": "...", "intent": "..." }
{ "kind": "discovery",     "subject": "GET /debug/env", "evidence": "...", "confidence": "high", "severity": "high" }
{ "kind": "poc_generated", "language": "python", "code": "...", "target": "api.acme.internal" }
{ "kind": "verified",      "method": "...", "evidence": "..." }
{ "kind": "blocked",       "attempted": "...", "policy": "...", "reason": "..." }
{ "kind": "failed",        "attempted": "...", "error": "..." }
{ "kind": "completed",     "result": "..." }
```

注意：

- `exitCode` 可为 `null`（进程被信号杀死时没有退出码）；
- `output` 可选（不是每次都抓尾部输出）；
- `severity` 只在 `discovery` 上有（`{ kind: 'discovery' } & WithSeverity`）；
- `detail` **整体可为 `null`**（纯状态事件）。

### 2.7 Report（`src/data/report.ts`）

| 字段                | 类型                                | 说明                                   |
| ------------------- | ----------------------------------- | -------------------------------------- |
| `id`                | string                              |                                        |
| `title`             | string                              |                                        |
| `scope`             | `ReportScope`                       | **discriminated union**，见下          |
| `options`           | `ReportOptions`                     | 4 个布尔                               |
| `createdAt`         | ISO string                          |                                        |
| `findingCount`      | number                              | 生成时命中数（已冻结）                 |
| **`excludedCount`** | number                              | **生成时被选项排除的条数（必须冻结）** |
| `severityCounts`    | `Partial<Record<Severity, number>>` | 快照                                   |
| `format`            | `'pdf' \| 'markdown'`               | 生成时用的格式                         |
| **`findingIds`**    | string[]                            | **冻结的条目 id 列表，按报告内顺序**   |

```ts
type ReportScope =
  | { kind: 'scan'; scanId: string }
  | { kind: 'target'; target: string }
  | { kind: 'range'; from: string; to: string } // 闭区间

type ReportOptions = {
  includePoc: boolean // 默认 false
  includeReasoning: boolean // 默认 false
  onlyConfirmed: boolean // 默认 false
  includeRemediation: boolean // 默认 true
}
```

> ⚠️ **`excludedCount` 必须由后端在生成时算好并冻结，不能让前端重算。**
> 原因（`data/report.ts` 与 `report-document.ts` 都记过）：复现历史报告时
> 快照里只有**被收录**的 id，重算 `inScope - selected` 恒为 0，
> 于是一份"只含已确认"、恰好把 2 条待确认全筛掉的报告会写着
> "本次范围内未发现问题" —— **那不是没问题，那是没看**。

> ⚠️ **`findingIds` 的顺序是报告的一部分**（严重度降序）。后端返回时保住顺序。

#### ★ "冻结"到底冻结了什么（v0.7 说清）

上面那句「报告内容在生成时固定」容易被读成**整份文档**都冻结。
前端自己的 `rebuildReportDocument` 实际只冻结三样，后端照做：

| 冻结的 | 不冻结的 |
| --- | --- |
| `findingIds` —— **收录哪些条目** | 条目的 `statusLabel`（**处置状态**） |
| `position` —— **报告内顺序**（严重度降序） | 条目里的证据、描述文本（本来就来自 agent 产出，不会变） |
| `excludedCount` —— 被选项排除的条数 | —— |

复现时拿**活的** Finding 按冻结 id 取回，所以 `statusLabel` 反映的是**当前**状态。

**这是有意的，不是漏了冻结**：前端预览走的也是这条路径，
后端若改成"把状态也冻结"，预览显示"已修复"而下载的文件里写着"待确认"——
**"预览=导出"当场就破了**，而那是任务 6 的验收标准。

> 顺带记一处**前端注释与它实现之间的落差**：注释里那句
> 「三个月后回看会看到一份'和当初不一样'的报告」论证的是
> **收录集合**要冻结，而实现也确实只冻结了集合与顺序。
> 「这条当初在报告里、现在修了」对读者其实是**更有用**的信息 ——
> 所以这个落差不必修，但要写下来，免得后来者当成 bug 去改。

> ⚠️ **`scope` 是 union，不是"三个可选字段"**：后端 DTO 也要体现这个约束
> （`kind` + 对应字段），否则会出现 `scanId` 和 `target` 同时有值的非法状态。

#### 报告编号 `RPT-YYYYMMDD-XXXX`

由后端分配，**格式固定**（前端 `reportCode()` 已按此解析）。
规则：`RPT-` + 8 位日期 + `-` + 4 位大写字母数字。

> ⚠️ **编号由"生成动作"分配，不是在草稿阶段。** 前端 `ReportDocument.meta`
> 有 `provisional` 标志和一段论证：草稿显示"生成后分配"，
> 因为早期版本预览显示 `RPT-...-3253` 而下载得到 `RPT-...-3310`，
> 客户拿编号对账时对不上。

### 2.8 目标归一化（`src/lib/validate-target.ts`）

`Scan.target` 存的是**归一化后**的值，不是用户原始输入。前端已实现
`normalizeTarget()`，后端**必须用同一套规则**，否则同一个目标会出现多条记录。

| `targetKind` | 归一化规则                                                | 例                                             |
| ------------ | --------------------------------------------------------- | ---------------------------------------------- |
| `url`        | 去掉协议与路径，**保留端口**，不加尾斜杠（取 `URL.host`） | `https://api.example.com/` → `api.example.com` |
| `repo`       | 保留原样（`.git` 后缀与结尾斜杠都可能有意义）             | `https://github.com/acme/backend.git`          |
| `path`       | 保留原样（**大小写与结尾斜杠在 Windows 上有意义**）       | `D:\projects\acme`                             |
| `service`    | **`host:port`**（裸 TCP 服务，端口必留）                  | `10.0.0.5:9999`                                |
| `artifact`   | **保留原样** —— 它存的是**附件文件名**，不是地址（v0.21）  | `tryre.zip`                                    |

> **为什么必须归一化**：`api.example.com`、`https://api.example.com`、
> `https://api.example.com/` 是同一个目标的三种写法。原样存下来，列表里会出现
> 三条看起来不同的记录 —— 而它们其实是同一个东西。

> **为什么 URL 保留端口**：同一主机的不同端口是两个不同目标（`:8443` 是另一个服务）。

> ⚠️ **`repo` 与 `path` 不做小写化或去尾斜杠。** 路径大小写在 Windows 上不敏感、
> 在 Linux 上敏感；仓库地址的 `.git` 后缀也可能影响克隆行为。这两类的"看起来
> 不统一"是**正确的**，不要去"修"它。

**校验规则**（前端 `validateTarget`，后端应保持一致）：

| kind   | 拦什么                                            | 放什么                        |
| ------ | ------------------------------------------------- | ----------------------------- |
| `url`  | 无法解析为 URL；主机名不含 `.` 且不是 `localhost` | 不带协议（自动补 `https://`） |
| `repo` | 不是 `https://…/…` 或 `git@host:…` 形式           | 两种 git 地址形式都收         |
| `path` | 不是绝对路径（POSIX `/` 开头 或 Windows 盘符）    | —                             |
| `service` | 端口不在 1–65535；主机名不含 `.` 且不等于 `localhost` | 粘贴的连接命令会自动识别 |
| `artifact` | **填了任何地址**（这类题目没有可连的目标）      | 附件在向导里单独上传          |

> ⚠️ **刻意不做得太严**（前端注释）：这是安全测试工具，用户会填内网 IP、
> 带端口主机、非标准 TLD、IP 段。过严的正则会拦掉合法输入，而用户**无法绕过**，
> 只能改工具 —— 那比漏校验更烦人。所以只拦"明显不对"的，其余交给后端。

#### 2.8.0 `service` 的粘贴识别（v0.21 补记）

裸 TCP 题（Pwn / 网络 Misc / Crypto 预言机）的用户手上通常是一条
**连接命令**，而不是 `host:port`。所以 `service` 支持直接粘贴：

| 粘贴形式 | 归一化为 |
| --- | --- |
| `10.0.0.5:9999` | `10.0.0.5:9999` |
| `nc 10.0.0.5 9999` / `ncat` / `netcat`（可带 `-v` 等选项） | `10.0.0.5:9999` |
| `socat - TCP:10.0.0.5:9999` | `10.0.0.5:9999` |
| `openssl s_client -connect 10.0.0.5:9999` | `10.0.0.5:9999` |

> **为什么值得单独做**：这类题的题面给的就是一条命令。
> 让用户自己从中抠出 host 和 port，是把他当解析器用 ——
> 而抠错一个数字的代价是 agent 对着错误的端口跑十几分钟。

> ⚠️ **`artifact` 不能填地址。** 它对应的题目形态是"只有附件、
> 没有可连的东西"（逆向 / 纯文件 Misc / 密码学）。给它一个地址的话，
> agent 会对着一个不存在的主机白跑 —— 而这正是它被发明出来要避免的事
> （见 §3.16.2 与 §3.17）。

#### 2.8.1 目标可达性预检（v0.17 新增）

上表最后一行「不带协议（自动补 `https://`）」**正是那次 32 分钟卡死的成因**：

用户填 `xxx.http-ctf2.dasctf.com`，前端补成 `https://…` 交给 agent，
而该主机只在 http 下有应答。agent 的每个请求都撞在 TLS 握手上，
它于是认为"目标暂时不可用"，转而反复重试 / 轮询 shell —— 直到 32 分钟后
被人工终止。**目标本身完全正常**（http 下 0.06 秒就返回 404）。

所以创建扫描时会做一次**预检**（`POST /scans` 内部，不额外占端点）：

| 情况 | 行为 |
| --- | --- |
| 用户**没写协议** | 先试 https，不通再试 http；哪个通就用哪个跑（存进 `probedTarget`） |
| 用户**写了协议** | **绝不改写**。预检只回答问题，不替用户做决定 |
| 两个都不通 / 超时 | 照常创建，`notice` 里说明"没探到应答" |
| 预检自己出错 | 照常创建（预检失败绝不能变成创建失败） |

**判定"通不通"的标准是"有没有 HTTP 应答"，不是状态码。** http 下的
404、403、401 都算**通** —— 目标是活的，只是那个路径不存在或需要认证。
把 404 当成"不可达"会让预检在内网应用上大面积误报，
而误报的代价是用户从此忽略这个提示。

`probedTarget` 只影响**交给外部工具的那个地址**，`ScanOut.target`
始终是归一化后的值。两者分开的理由见 §2.1 的 `notice` 说明。

#### 2.8.2 ★ TCP 目标的预检**不写** `probedTarget`（v0.21 补记）

`service` 形态的预检走的是**另一条路径**，与 §2.8.1 的 HTTP 预检刻意分开：

| | HTTP 预检（`url`） | TCP 预检（`service`） |
| --- | --- | --- |
| 探测方式 | 发一个 HTTP 请求 | **只做三次握手**，连上后**一个字节都不发** |
| 结论存哪 | 新列 `probedTarget` | **只借 `notice` 通道**，`probedTarget` 存 `NULL` |
| 判定"通" | 有没有 HTTP 应答（404/403 都算通） | 握手成不成功 |

> **★ 为什么 TCP 探测连上后不发数据**：CTF 靶机是**有状态**的 ——
> 很多题在连接后就开始计时或计数（"三次答错即断"）。探测时发一个字节，
> 可能就消耗掉一次机会、或者让靶机进入某个状态。
> 而预检的目的是"告诉用户通不通"，不是"帮他探路"。
> 所以它只做握手，代价是拿不到任何协议信息 —— 这是刻意的取舍。

> **★ 为什么不给 TCP 补 `http://` 写进 `probedTarget`**：
> `probedTarget` 的语义是"**探测后实际用的那个地址**"。对 HTTP 目标，
> 补 `https://` 或 `http://` 是有意义的（两者可能是不同服务）。
> 而对裸 TCP，**没有"协议前缀"这个概念** —— 硬补一个 `http://`
> 等于编造一个不存在的层，而后端 `build_spec` 会拿它去发包。
> 所以这里 `effective_target` 刻意留空，`build_spec` 回退用 `scan.target`
> （也就是 `host:port` 本身）。

> **共同点**：两条路径**都不阻断创建**，失败只写一句 `notice`。
> 预检是提示，不是门禁 —— 拦错了的代价是用户从此忽略这个提示。

### 2.9 Settings 相关

#### ProviderAccount（`data/settings.ts`）

| 字段          | 类型                 | 说明                                          |
| ------------- | -------------------- | --------------------------------------------- |
| `id`          | string               |                                               |
| `label`       | string               | 品牌名，**不翻译**（"DeepSeek" 译了不可检索） |
| `model`       | string               | 模型标识符                                    |
| `kind`        | `'cloud' \| 'local'` |                                               |
| `apiKey`      | string               | ⚠️ **见下方安全要求**                         |
| `baseUrl`     | string               | 空串 = 用官方默认                             |
| `isDefault`   | boolean              | 全部账号中至多一个 true                       |
| `verified`    | boolean              | key 是否已在服务端验证通过                    |
| `verifyError` | string \| null       |                                               |
| `note`        | string               | 取舍说明，不是营销文案                        |

> 🔒 **`apiKey` 绝不能出现在 GET 响应里。**
>
> 前端 `SecretField` 有六条安全约束，其中一条是**已保存的密钥不回填** ——
> 这既是 UI 约束，也要求后端支持：GET 只返回 `"apiKey": ""`（空串 = 未回填）
> 加一个 `hasKey: true` 之类的标志；或干脆**不返回该字段**。
>
> 契约采用：**GET 返回 `apiKey: ""`**，并新增只读字段 `hasKey: boolean`。
> 空串在前端有确切含义（"未配置"），所以这里必须配合 `hasKey` 才不歧义。
>
> 保存时的语义：**payload 里省略 `apiKey` 或传空串 = 保持原值不变**，
> 传非空值 = 更新。这与前端 `ProviderPanel` 的"不回填但保存时带上原值"
> 逻辑一致。**漏掉这条，用户改个显示名就会把凭据清空** —— 前端注释里
> 明确记过这个隐蔽 bug。

##### ★ 掩码放在**独立字段** `apiKeyMask`，不放进 `apiKey`（v0.3 新增）

v0.2 起 GET 的 `apiKey` 恒为空串。但界面需要显示"这里确实有一把钥匙、
而且是哪一把"，所以新增一个只读字段：

| 字段         | 类型      | 值                              | 谁用                          |
| ------------ | --------- | ------------------------------- | ----------------------------- |
| `apiKey`     | `string`  | **恒为空串**                    | 契约要求的"不回填"位           |
| `hasKey`     | `boolean` | 有没有存过凭据                  | 区分"配了但不回填"与"根本没配" |
| `apiKeyMask` | `string`  | `sk-****abcd`，无凭据时空串     | 界面上显示"有哪一把钥匙"       |

掩码规则：明文的**前 3 位 + `****` + 后 4 位**；长度不足 12 位的整体隐掉
（`********`）—— 与前端 `lib/secret.ts` 的 `secretPreview` 用的是同一个
12 位阈值，理由也一样：一个 10 位的值用"前 3 后 4"会暴露 7 位，那不是掩码是缩写。

> ⚠️⚠️ **为什么不把掩码直接放进 `apiKey`** —— 这是本契约里最容易埋雷的一处。
>
> 前端 `ProviderPanel` 保存时是这样拼 payload 的：
>
> ```ts
> apiKey: keyEdited ? draft.apiKey : account.apiKey
> ```
>
> 也就是**没动 key 时把 `account.apiKey` 原样带回服务端**。这是"不回填"能成立的
> 前提（输入框是空的，保存时必须带上原值，否则改个显示名就把凭据清空）。
>
> 如果服务端在 `apiKey` 里返回掩码，那么用户只改一个显示名、点保存，
> 服务端收到的就是 `sk-****abcd` —— 它会作为**新的 API key** 被存下来。
> 真凭据被静默毁掉，界面还显示"已保存"，没有任何报错。
>
> 把掩码放进独立字段，`apiKey` 永远是空串，上面那条 payload 拼装就自然正确。
> **这是"让连接本身不产生危险"，而不是"加一层校验挡住危险"。**
>
> 服务端仍然会加一道兜底（值等于掩码 → 视为不改凭据），但那是第二道防线，
> 不是设计依据。

##### 凭据在服务端是加密存储的（v0.3 新增）

`api_key` **不以明文落库**：

* 用 **Fernet**（`cryptography` 库的 AES-128-CBC + HMAC-SHA256）加密后存；
* 密钥来自**独立的**环境变量 `SUPEX_SECRET_KEY`，不复用任何其他密钥
  （本项目没有 JWT 签名密钥，见 ADR-001；这条约束的等价物是
  "这条密钥只做加密，不签名、不派生会话"）；
* 另存一列可展示掩码，**让读接口不必解密** —— 于是"明文只在真的要调 LLM
  的那一刻才被解开"是可验证的，而不只是一句承诺；
* 密钥丢失 ⇒ 已存凭据全部解不开。响应里会给明确的中文说明
  （`PROVIDER_KEY_UNREADABLE`），而不是显示成"未配置"让用户以为是自己没填。

> ⚠️ **`baseUrl` 空串 = 用官方默认地址**这条要求**服务端也持有默认地址表**
> （与前端 `PROVIDER_PRESETS.defaultBaseUrl` 镜像，改一边必须改另一边）。
> 这是继"授权声明"之后第二处两端必须逐字一致的常量。

#### SandboxConfig / NoticeRule / NotifyChannel / ApiToken

字段与 `data/settings.ts` 逐字一致，取值见该文件。三个要点：

1. **`NotifyChannel`（怎么发）与 `NoticeRule`（什么级别才发）是两个维度**，
   后端表结构也要分开 —— 合并会让"邮件收全部、Slack 只收严重"无法表达。
2. **`ApiToken` 只有 `prefix`，永远没有 `value`**。完整令牌只在
   `POST /settings/tokens` 的响应里出现**一次**（前端类型 `CreatedToken.plaintext`）。
   服务端存哈希。
3. **`sandbox.networkIsolated` 的默认值是 `false`（允许出网）** ——
   扫描经常需要出网打外部目标。这不是"更安全就一定更好"的开关。

**执行三件套**（v0.21 补记，任务 13）—— `SandboxConfig` 上多出四个字段：

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `maxConcurrentScans` | 1–8 | 同时跑几次扫描 |
| `scanMaxTurns` | 10–2000 | 单次扫描的回合上限 |
| `scanMode` | `quick` \| `standard` \| `deep` | 扫描深度 |
| `executionSource` | `{maxConcurrentScans, scanMaxTurns, scanMode}` 各为 `'user'` \| `'default'` | **只读**，标出每个值当前来自哪一侧 |

> **★ `executionSource` 是这个设计里最容易被忽略、但最该有的一环。**
>
> 三个值都是"DB 覆盖 → env 兜底"，所以界面上显示的可能是**用户设的**，
> 也可能是**环境变量给的默认值**。不区分这两者的话，用户会对着一个
> 他从没设过的值发呆 —— 想改它，却不知道改了会不会生效（因为看起来
> "它已经是我想要的值了"）。
>
> 所以每个值单独标 `user` / `default`，而不是整体一个开关。

> **⚠️ 沙箱配额有四个字段，但只有三个真的生效**（v0.21 写清）：
>
> | 字段 | 生效 | 说明 |
> | --- | --- | --- |
> | `cpuLimit` / `memoryLimitMb` / `networkIsolated` | ✅ | 真下发给沙箱容器 |
> | `timeoutMinutes` | ❌ | 执行后端**全包没有扫描级墙钟超时** |
> | `allowVolumeWrite` | ❌ | 工作区挂载**恒为读写**，改不了 |
>
> 这条必须写在这里：界面上那两个开关是**能点、能存、但不生效**的。
> 不写清楚的话，用户会以为"设了 30 分钟超时，它就会 30 分钟停" ——
> 而实际不会。这类"看起来生效其实没有"的字段比缺失的字段更糟。

> **⚠️ 配额只接了扫描这一条路**（v0.21 补记）：CTF2 解题路径的
> CPU / 内存配额是**任务 21** 才接上的（见 §3.19）。在那之前，
> CTF2 起的解题容器没有任何 CPU / 内存上限。

### 2.10 CTF2 相关（`src/data/ctf2.ts`）

> **v0.21 新增。** 这一节补的是**整个 CTF2 模块**（25 个端点、5 个枚举、
> 24 个出入参），它此前在契约里零覆盖 —— 详见 §3.19 的说明。

#### 五个枚举

| 类型 | 取值 | 说明 |
| --- | --- | --- |
| `Ctf2ChallengeStatus` | `unsolved` \| `environment_ready` \| `solving` \| `candidate_ready` \| `submitting` \| `solved` \| `failed` | 一道题**在我们这边**的推进状态 |
| `Ctf2SubmissionStatus` | `pending` \| `correct` \| `wrong` \| `rejected` \| `failed` | 一次 flag 提交的结果 |
| `Ctf2EnvironmentStatus` | `stopped` \| `starting` \| `ready` \| `error` \| `expired` | 题目环境（容器）的生命周期 |
| `Ctf2SubmitMode` | `safe` \| `auto` | 提交策略 |
| `Ctf2SandboxProfile` | `ctf2-sandbox` \| `ctf2-sandbox-re` | 用哪个沙箱镜像 |

> **★ `Ctf2ChallengeStatus` 为什么不直接抄 CTF2 的题目状态。**
>
> 平台侧的题目状态只回答「这个用户在**平台**上做没做出来」——
> 它只有"未解出 / 已解出"两种，因为平台不关心你是自己在做还是在
> 这个系统里做。而我们要回答的是「**我们的流水线**走到哪一步了」：
> 环境起没起来、LLM 在不在解、候选 flag 有没有、提交成不成。
>
> 把两者合成一个字段，会出现「平台说未解出、而我们正在跑」这种
> **无法表达**的状态 —— 而它恰恰是最需要显示的中间态。
>
> 其中 `candidate_ready`（"待确认"）是 `safe` 模式的核心：**它在等人**。
> 措辞必须让人一眼看出该自己动手了，而不是"还在跑"。

> **★ `rejected` 与 `wrong` 是两件事，必须分开。**
>
> | 取值 | 含义 | 外部后果 |
> | --- | --- | --- |
> | `wrong` | 请求**真的到了平台**，平台判定这个 flag 不对 | **消耗错误提交额度** |
> | `rejected` | **我们自己在本地就拦下了**，请求根本没发出去 | 不消耗任何额度 |
>
> `rejected` 的四种成因：超出该题错误提交上限、flag 形状不符、
> `safe` 模式下无人确认、以及 §3.19 里 `confirm` 的前置条件不满足。
>
> 合并的代价是实打实的：`rejected` 若记成 `wrong`，"这道题我还剩
> 几次机会"会被本地拦截白白扣掉 —— 而用户看到的是数字自己变小了。

> **`expired` 与 `error` 分开**：CTF2 侧的环境**有租期**，到期后
> `accessUrl` 会变成连不上的死地址。那是**正常老化，不是故障** ——
> 补救动作是"重新启动环境"，而 `error` 的补救动作是"看日志"。
> 混成一档，界面就只能给一句含糊的"环境异常"，用户不知道该点重启
> 还是该找人。

#### 出参形状

| 类型 | 关键字段 |
| --- | --- |
| `Ctf2Account` | `id` / `label` / `status` / `remoteUserId` / `remoteUsername` / `scopes` / **`tokenMask`** / **`hasToken`** / `lastSyncedAt` / `lastError` / `createdAt` / `updatedAt` |
| `Ctf2Settings` | `submitMode` / `maxWrongSubmissions` / `defaultSandboxProfile` / `maxConcurrentSolves` / `solvePollSeconds` / `autoSyncSeconds` / `autoPriorityNew` / `autoStartNew` / `maxTurns` / **`fallbackProviderId`**（v0.35，可空 = 不启用） / `updatedAt` |
| `Ctf2Competition` | `id` / `accountId` / `remoteId` / `kind`(`competition`\|`practice`) / `name` / `remoteStatus` / `startAt` / `endAt` / `challengeCount` / `syncedAt` |
| `Ctf2Challenge` | `id` / `competitionId` / `remoteId` / `stageId` / `title` / `category` / `description` / `attachments` / `score` / `status` / **`candidateFlag`** / **`confirmedFlag`** / `remainingWrongSubmissions` / `wrongSubmissionCount` / `maxWrongSubmissions` / `candidateCount` / `sandboxProfile` / `scanId` / `solvedAt` / `lastError` / `environment` / `priority` / `createdAt` / `updatedAt` |
| `Ctf2Environment` | `id` / `challengeId` / `status` / `sandboxProfile` / `accessUrl` / `accessUrls` / `containerId` / `pollCount` / `startedAt` / `readyAt` / `expiresAt` / `lastError` |
| `Ctf2Platform` | `id` / `slug` / `displayName` / `baseUrl` / `driver` / `source`(`builtin`\|`generated`) / `status` / `lastError` / `attempts` / `isBuilding` / **`active`** / `lastRaw` / `createdAt` / `updatedAt` |
| `Ctf2Snapshot` | `account` / `competition` / `challenges` —— 一次拿到三样，省掉三次请求 |
| `Ctf2Queue` | `running` / `limit` / `queued` / `challenges`（题目 id 列表） |
| `Ctf2SyncResult` | `competitions` / `challengesCreated` / `challengesUpdated` / `errors` / `attachmentsDownloaded` / `attachmentsSkipped` / `attachmentsFailed` / `message` |
| `Ctf2SolveResult` | `challenge` / `environmentId` / `scanId` / `message` |
| `Ctf2SubmitResult` | `status` / `message` / `challenge` |
| `Ctf2PullResult` | `challenge` / `created` / `challengeId` / `competitionName` |
| `Ctf2RemoteCompetition` | `remoteId` / `name` / `startAt` / `endAt` / `status` —— **平台侧**的赛事，不落库 |
| `Ctf2Writeup` | `challengeId` / `title` / `category` / `solved` / `markdown` / `filename` / `attemptId` / `turns` / `elapsedSec` / `generatedAt` / `updatedAt`（v0.36）|
| `Ctf2TokenUsage` | `calls` / **`recordedCalls`** / `promptTokens` / `completionTokens` / `totalTokens` / `byModel[]` / `byChallenge[]` / **`byAttempt[]`** / `since`（v0.36；`byAttempt` v0.37）|
| `Ctf2UsageEntry` | `turn` / `model` / `providerLabel` / `promptTokens` / `completionTokens` / `totalTokens` / `reasoningTokens` / **`usageReported`** / `at`（v0.36）|
| `Ctf2UsageByAttempt` | `attemptId` / `challengeId` / `title` / `mode` / `model` / `calls` / `totalTokens` / `startedAt`（v0.37）|

> **★ `byAttempt` 是"细分到每个任务"那个需求落的字段（v0.37）。**
> `byChallenge` 按**题**聚合，而一道题可以解很多次（实测
> `[ACTF2020 新生赛]Upload` 解过 4 次）—— 只按题聚合会把
> "哪一次跑得贵、哪一次在兜圈子"糊成一行。
> `mode` / `model` / `startedAt` 来自 `ctf2_solve_attempts`（JOIN 取的），
> 所以**不需要新表新列**；`byModel` / `byChallenge` 语义不变（纯增）。

> **⚠️ 这三行曾经比实现短（2026-10-15 补齐）**：`Ctf2Writeup` 少了
> `turns` / `elapsedSec` / `updatedAt`，`Ctf2TokenUsage` 少了 `since`
> （`since` 在 §3.19.6 的正文里有说明，**只有这张表没跟上**），
> `Ctf2UsageEntry` 少了 `reasoningTokens`。
>
> 这类"正文写了、表里没写"的漂移比整块缺失更难发现：审查的人
> 只要读过 §3.19.6 就会以为契约已经覆盖了 `since`。而这张表是
> 前端对字段的核对清单 —— 表短了就有人会按表去删字段。
> 记录在此，因为它正是这份文档反复栽的那类跟头。

> **★ `Ctf2TokenUsage` 里 `calls` 与 `recordedCalls` 必须分开（v0.36）。**
>
> `calls` 是**调用次数**（永远准确，我们从循环里数得出来）；
> `recordedCalls` 是**接入点真的给了 `usage` 的次数**。
>
> 接入点可能不给 `usage`（第三方代理、某些本地模型），那时 token 记 0 ——
> 而"真的是 0 token"与"没记录到"在界面上长得一模一样。
> 合成一个字段的后果是用户看到"消耗 0"以为没跑过，或者反过来
> 以为某次跑得特别省。这与 §2.9 的 `hasKey` / `apiKey` 分两列、
> §3.19 的 `candidateFlag` / `confirmedFlag` 分两列是同一条纪律。

> **★ `Ctf2Account` 里没有 `token` 字段，只有 `tokenMask` + `hasToken`。**
> 与 §2.9 的 `apiKey` / `apiKeyMask` 同一条理由（见 ADR-003）：
> 掩码放进凭据字段会被前端原样回传、覆盖真凭据 —— 静默数据损坏。
>
> 这里的后果更重一层：CTF2 的 PAT 是一把**代表用户去操作外部平台**的
> 钥匙（能提交 flag、能开靶机），毁掉它意味着要重新去平台申请。

> **★ `candidateFlag` 与 `confirmedFlag` 是两列，不是一个字段。**
> 前者是 agent 提出、**尚未提交**的候选；后者是平台**判定正确**的结果。
> 合成一个字段就无法回答"这个 flag 到底提交过没有"——
> 而它决定了要不要再发一次请求（以及会不会白扣一次额度）。

> **`Ctf2Platform.active` 是"当前生效的那一个"**，不是"可用的"。
> 多个平台可以同时 `verified`，但只有一个 `active`（见 §3.19 的
> `POST /ctf2/platforms/{id}/activate`）。

### 2.11 用户与角色（v0.24）

> **这一节补的是"多用户"这个改动。** 在此之前系统是**单管理员**的：
> 登录只输口令（`PasswordRequest` 里没有 `username`），而
> `admin_accounts` 表**永远只有一行**。

#### `UserOut`（`GET /auth/me`、`POST /auth/login`、`POST /auth/setup`）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | `user_01M4K…` |
| `username` | string | **登录名，已归一化**（去空白 + 转小写） |
| `role` | `'admin' \| 'viewer'` | 见下 |
| `createdAt` | ISO string | |

> **⚠️ `username` 存的是归一化后的值。** 大小写不敏感是刻意的：
> 用户不该因为记错大小写而登不进去，而 `Alice` 与 `alice` 是两个账号
> 只会带来困惑。与 `Scan.target` 存归一化值是同一条原则 ——
> **让同一个东西只有一种表示**。

#### 两档角色

| 角色 | 能做什么 | 不能做什么 |
| --- | --- | --- |
| `admin` | 读全部、**发起扫描**、改设置、管 provider / 令牌 / 用户 | — |
| `viewer` | 读扫描、漏洞、报告、活动流、provider 列表 | **发起扫描**、改任何配置、管用户、看审计日志、看用户列表 |

> **为什么只有两档。** 这个平台的危害是**替攻击者发包**，所以权限的
> 分界线只有一条真正重要：**能不能让机器对外动手**。
>
> 中间那些档（"能扫描但不能改设置"）听起来合理，但没有对应的真实诉求：
> 使用者是一个安全团队，而团队里"能看但不能动"的人只有一类 ——
> 甲方、审计、或旁观的同事。
>
> 加更多档的代价是实打实的：**每个端点都要标注所需角色，漏标一处
> 就是一个权限漏洞**。档位越少，标注越不容易错。

> **★ 角色与 `TokenScope` 是两个维度，不要合并。**
>
> | | 管什么 | 谁有 |
> | --- | --- | --- |
> | `TokenScope` | **机器令牌**能做什么（CI 脚本） | 令牌，创建时选定 |
> | `UserRole` | **登录用户**能做什么（人） | 账号，管理员分配 |
>
> 一个人的角色与他签发的令牌 scope 是独立的：`viewer` 也可以创建一个
> `read` 令牌给监控用。合成一个概念会导致"改用户角色顺手改了他所有
> 令牌的权限" —— 而那些令牌可能正被别的系统用着。

#### ★ `require_admin_session`：用户管理不接受机器令牌

用户管理的四个端点用的是比 `admin` 更严的一道：**必须是登录会话**。

> **理由不是"更严一点"，而是一条明确的安全边界。**
>
> 管用户与其它管理员操作有一个本质区别：它是**对权限本身的操作**。
>
> 一个 `admin` scope 的令牌能改设置、能发起扫描，那些都在"这个令牌被
> 授权做的事"范围内。但让它能**创建一个新的管理员**、或**改掉自己
> 签发者的角色**，意味着令牌的权限可以自我放大 —— 而令牌是长期有效的
> （可以设成永不过期），泄漏一个就等于永久拿到用户管理权。
>
> 所以这条线画在"对权限的操作"上，而不是"危害大小"上。

#### ★ 角色变更**立刻生效**

`resolve_identity` **每次请求都读一次账号**（主键查询，微秒级），
而不是把角色缓存在会话行或 Cookie 里。

> 代价是一次索引查询。换来的是：**管理员改了某人的角色，他下一个请求
> 就生效**。
>
> 这不是性能取舍，是安全要求 —— "我刚把他降成只读，他还在发起扫描"
> 是一个真实的权限漏洞。缓存在会话里会让降级延迟到该会话过期
> （默认 12 小时）。

#### 三条防自锁规则

| 规则 | 为什么 |
| --- | --- |
| 不能删**最后一个管理员** | 删完没人能改设置，恢复只能直接改库 |
| 不能删**自己** | 删完立刻掉线；若同时是最后一个管理员就再也进不来 |
| 不能**降级**最后一个管理员 | 同第一条 |

> 三条各自返回专门的错误码（`LAST_ADMIN` / `CANNOT_DELETE_SELF`），
> 因为前端要给出**不同的补救动作**："先指定另一个管理员" vs
> "不能删自己"。拿中文消息去 `includes('管理员')` 判断会在文案一改
> 就静默失效。
>
> **前端也会判一次**（`users-store.ts` 的 `deleteBlockedReason`）——
> 但那只是为了让按钮别可点。真正的拦截在后端，前端的判断不是安全边界。

---

## 3. REST 端点

### 3.1 认证与会话

| 方法 | 路径           | 鉴权                       | 请求           | 响应                                                                  |
| ---- | -------------- | -------------------------- | -------------- | --------------------------------------------------------------------- |
| GET  | `/auth/state`  | public                     | —              | `200 { initialized: boolean, authenticated: boolean }`                |
| POST | `/auth/setup`  | public（**仅未初始化时**） | `{ password }` | `201 { user }` + 下发会话 Cookie。已初始化则 `409 SETUP_ALREADY_DONE` |
| POST | `/auth/login`  | public                     | `{ password }` | `200 { user }` + 下发会话 Cookie                                      |
| POST | `/auth/logout` | **幂等，不要求有效会话**   | —              | `204` + 清除 Cookie                                                   |
| GET  | `/auth/me`     | read                       | —              | `200 UserOut`                                                          |
| GET  | `/auth/users`  | **admin（仅会话）**        | 用户列表        | `200 { users: UserOut[] }`                                              |
| POST | `/auth/users`  | **admin（仅会话）**        | 新增用户 → `201`| `201 UserOut`                                                           |
| PATCH | `/auth/users/{userId}` | **admin（仅会话）** | 改角色        | `200 UserOut`                                                           |
| DELETE | `/auth/users/{userId}` | **admin（仅会话）**| 删除 → `204`  | `204`                                                                   |

> **`logout` 的鉴权级别在 v0.2 从 `read` 放宽为「幂等」**。理由是登出的语义是
> **丢弃自己的凭据**：会话已经过期时返回 `401` 会让前端只能跳登录页、
> 却清不掉那个失效 Cookie，而 Cookie 留在浏览器里没有任何好处。
>
> 放宽也不会被利用：登出不是特权动作，跨站触发它最多让人重新登录一次 ——
> 而 `SameSite=Strict` 下跨站请求根本带不上 Cookie，连这个都做不到。

> **`authenticated` 的判定**：会话有效 **或** 请求带了一个有效的
> `Authorization: Bearer svx_…` 都算 `true`。前端只需要知道"该不该跳登录页"。

**`GET /auth/state` 是前端启动时的第一个请求**，它回答两个问题：
系统初始化了没有（决定显示初始化页还是登录页）、当前是否已登录（决定要不要跳登录）。

> ⚠️ **这个端点是 `public` 且必须保持 public** —— 它在登录前就要能调用。
> 但它**只返回两个布尔值**，不泄露任何其他信息。

**单管理员，所以 `login` 只需要 `password`**（没有用户名）：
一个账号的系统里要用户名词只是多一个必填框。

登录失败统一返回 `401 UNAUTHENTICATED`，`message` 只说"口令不正确"。

> **不做"账号不存在"与"口令错误"的区分** —— 那是账号枚举漏洞。
> 本系统只有一个账号，所以这条更简单：**只有"口令不对"一种失败**，
> 不需要区分。

**限流**：`/auth/login` 与 `/auth/setup` 必须限流（建议同一来源
5 次/分钟、15 次/小时）。口令是本系统唯一的凭据，不限流等于允许在线爆破。

### 3.2 LLM Provider（`admin`）

| 方法   | 路径                      | 说明                                                        |
| ------ | ------------------------- | ----------------------------------------------------------- |
| GET    | `/providers`              | 列表。**`apiKey` 一律为空串**，附 `hasKey` / `apiKeyMask`   |
| POST   | `/providers`              | 新建 → `201` + `SaveResult`+`provider`。id 由**服务端**分配 |
| PATCH  | `/providers/{id}`         | 更新。**省略 / 空串 / 传回掩码，三者都 = 不改凭据**         |
| DELETE | `/providers/{id}`         | 删除。见下方两种 `409`                                      |
| POST   | `/providers/{id}/verify`  | 验证连通性 —— **动作型**，返回成功/失败与**真实耗时**       |
| POST   | `/providers/{id}/default` | 设为默认（已默认时返回 `nochange`）                         |

> ⚠️ **`baseUrl` 的 http/https 校验只在前端做，后端不拦**（v0.12）。
>
> 后端 `llm.py` 只校验"协议是 http 或 https"，**不区分**它是云服务还是本地 ——
> 因为服务端分不出"内网自建网关"与"公网明文端点"，而拦错了会让真实场景不可用。
>
> 前端原来对 `cloud + http + 非回环` 直接判错。但那把一类**真实且正当**
> 的场景挡在门外：内网自建网关（如 `http://10.39.16.9/v1`）—— 它在私网里、
> 可能由运维统一提供、未必有证书。用户只能改 `kind: 'local'` 绕过，
> 而那是**用错误的标签换通行**，比让他在知情后放行更糟。
>
> 所以改成**显式确认**：报错位置旁边给一个「我已知晓风险」的小按钮，
> 点了之后该地址**本次会话内**允许保存，提示语从"应使用 https"降级为
> 一句中性的风险陈述。这与扫描授权确认（§5）是同一个套路 ——
> **不替用户做决定，但要他明确知道自己在做什么**。

> **POST / PATCH / default 的响应统一是 `SaveResult` + `provider`**（v0.3）。
> 用 `SaveResult` 是因为契约 §3.7 要求"保存类端点统一用它的形状"，
> 而前端 `saveProviderAccount` 返回的正是它；
> 附带 `provider` 是因为服务端分配了 `id`、并会改写 `verified` / `verifiedError` ——
> 客户端必须拿到新状态，否则要再发一次请求（而再发一次会取到
> "别人的验证结果"这种竞态）。
>
> `POST` 恒为 `ok`（新建一定发生了动作）；`PATCH` 与 `/default`
> 在**什么都没变**时返回 `nochange` —— 用户点了保存但没改过任何东西，
> 不能报"已保存"（契约 §3.7）。

**探测请求是"最小化"的字面实现**：`POST /providers/{id}/verify` 用**已保存的**
配置发一次请求，`messages` 只有一句 `ping`、不流式、不传工具、不传 system prompt。
凭据**只进请求头**，绝不进 URL —— 进了 URL 就会出现在访问日志、反代日志、
以及任何抓包记录里。

> ⚠️ **v0.11 修正：`max_tokens` 不再是"个位数"，默认改为 256。**
> 原因见下一条 —— 个位数对推理型模型等于"不让它说话"。

> ⚠️ **v0.11 修正（任务 8 收尾时由真实配置暴露）：个位数的 `max_tokens`
> 对推理型模型必然误判。**
>
> 事实经过：配了一个推理型模型（`deepseek-flash`），验证报
> 「对方返回了成功状态，但响应里没有可识别的内容。请确认 baseUrl 指向的是
> 兼容的模型接口。」而对方的**原始响应**是：
>
> ```jsonc
> {"choices":[{"message":{"role":"assistant",
>   "content":"",                                  // ← 空
>   "reasoning_content":"The user just said "ping". This"},
>   "finish_reason":"length"}],
>  "usage":{"completion_tokens":8,
>   "completion_tokens_details":{"reasoning_tokens":8}}}   // ← 8 个预算全用在推理上
> ```
>
> 也就是说：**凭据是好的、接入点是对的、模型是通的** —— 8 个 token 在模型
> "想"的阶段就用完了，`content` 当然是空的。而原判定把它读成了
> "响应结构不对"，还叫用户去查 `baseUrl`。**一句会把人带偏的假指控。**
>
> 所以判定规则明确为：
>
> 1. `choices[0].message.content` 非空 → 回话取它；
> 2. 否则 **`reasoning_content` 非空 → 也**算回话（它就是模型产出的东西），
>    `reply` 取它；
> 3. 两者都空 → 才算失败，且**文案必须说明真实原因**
>    （探测预算只有个位数 token，推理型模型可能还没开始回答就用完了），
>    **不再**叫用户去查 baseUrl。
>
> 判"通不通"看的是**响应结构是否成形**（有 `choices[0].message`），
> 而不是"content 恰好非空" —— 后者把"我们自己给的预算太小"读成了
> "用户的配置有问题"。
>
> **配套地，默认预算从 8 调到 256。** 光认 `reasoning_content` 只够让它"不算失败"，
> 但用户看到的 `reply` 会是一段被截断的英文思考碎片。实测同一个推理型模型
> （prompt 只有 `ping`）：
>
> | `max_tokens` | `content` | `reasoning_tokens` | `finish_reason` |
> | ------------ | --------- | ------------------ | --------------- |
> | 8            | `""`      | 8                  | `length`        |
> | 64           | `""`      | 64                 | `length`        |
> | **256**      | `"Pong! 🏓 …"` | 36           | **`stop`**      |
>
> 8 和 64 下模型**根本没机会回答**。256 的代价可以忽略（一次探测几十个
> output token，且由用户点按钮触发），而"最小化"的本意
> （一句 prompt、不流式、不传工具）一条都没丢。
> 可用 `SUPEX_LLM_PROBE_MAX_TOKENS` 覆盖。

协议按**接入点**判定而不是按 `kind`：host 含 `anthropic` 走 Messages API
（`/v1/messages` + `x-api-key` 头），其余走 OpenAI 兼容的 `/chat/completions`。
`kind` 只有 `cloud` / `local`，区分不出 Anthropic 与 OpenAI —— 两家都是 cloud。
> ⚠️ 这条规则不完美（自建网关转发 Anthropic 协议时会判错）。
> 真要做对得给 `ProviderAccount` 加一个 `protocol` 字段，那属于前端类型变更，
> 记在 §7 的待改清单里。
>
> 本地模型（`kind: 'local'`）的裸 `baseUrl` 会被补上 `/v1`
> （Ollama 的 OpenAI 兼容端点是 `/v1/chat/completions`），
> 不补会打到 `/chat/completions` 上得到 404 —— 而那个 404 会被用户
> 理解成"Ollama 不支持"，方向完全错了。

**★ 改 key 必须作废 `verified`。** 前端 `settings-store.ts` 的 `saveProviderAccount`
里有 `keyChanged` 判断，并注释说明了后果：改 key 后沿用旧的 `verified: true`，
会让一个**从未验证过**的新 key 显示成"已验证可用"；更糟的是新建扫描向导的
`configured` 读的正是它，用户选中后会在**扫描跑到一半时认证失败**。

**后端必须在改 key 时把 `verified` 置回 `false`、`verifyError` 置 `null`。**

`verify` 的响应（复用 `ActionResult` 语义，**没有 nochange 态**，v0.3 起带耗时）：

```jsonc
// 成功
{
  "status": "ok",
  "at": "2026-09-23T15:30:42.123Z",
  "durationMs": 412,                 // ★ 真实往返耗时，不是估算
  "endpoint": "https://api.deepseek.com/chat/completions",  // 不含凭据，可安全展示
  "reply": "pong",                   // 模型的回话，是"真的打通了"的证据
  "provider": { /* 更新后的 ProviderAccount */ },
}
// 失败 —— ★ 仍然是 HTTP 200
{
  "status": "failed",
  "error": "凭据被拒绝（HTTP 401）。请确认 API key 是否正确、是否已过期。",
  "durationMs": 233,
  "endpoint": "https://api.deepseek.com/chat/completions",
  "provider": { /* 更新后的 ProviderAccount */ },
}
```

> ★ **失败也是 `200`，不是 4xx。** 契约把「验证失败」定义为"存进去了但打不通" ——
> 它是一个**正常的业务结果**。返回 4xx 会让前端走"保存失败"的分支，
> 而用户不知道该重填还是该重试 —— 这正好是契约论证
> 「保存与验证必须是两个动作」时举的那个例子。
>
> **失败原因会带上对方返回的错误摘要**（结构化取 `error.message`，脱敏后截断到
> 200 字符）。因为"model 不存在"这类信息只有对方能给，而它对用户极有价值。
> 但它是**不受信文本**，所以过一遍脱敏 —— 有些网关会在错误里复述收到的 API key，
> 而 `verifyError` 会被存库并渲染到界面上。

#### `DELETE /providers/{id}` 的两种拒绝（v0.3 补错误码）

| HTTP | code                  | 何时                                   | 调用方该做什么         |
| ---- | --------------------- | -------------------------------------- | ---------------------- |
| 409  | `PROVIDER_IS_DEFAULT` | 它是当前默认，且**还有别的** provider  | 先把另一个设为默认     |
| 409  | `PROVIDER_IN_USE`     | 被 `queued` / `running` 的扫描引用     | 先终止那些扫描（响应里会列出是哪几次） |

> 拆成两个码而不是一个笼统的 409：两者要做的补救动作不同。
> 给同一个码，前端只能把两句话都显示出来，而其中一句是错的。
>
> ⚠️ **「默认不可删」有一个例外：它是最后一个。** 没有这个例外会死锁 ——
> 新建的第一个 provider 会被自动提为默认，而默认不可删，
> 于是只有一个 provider 的用户**永远删不掉它**，系统也没法被清空。
> 例外是合理的：这条规则的目的是"不要让默认指向一个已删除的行"，
> 而删掉最后一个之后没有任何行需要指向。
>
> ⚠️ **只有未结束的扫描会拦住删除。** 已完成的扫描引用的是**过去**的一次调用，
> 不该拦住清理。（v0.3 为此移除了 `scans.provider_id` 上的外键约束 ——
> 它是**历史引用**不是引用完整性约束，`RESTRICT` 会让任何一条历史记录
> 永久锁死删除。完整性改为在写入时保证。）

> 前端把「保存」和「验证」分成两个动作，因为语义不同：
> 保存失败 = 没存进去；验证失败 = 存进去了但打不通。
> 混成一个，用户不知道该重填还是该重试。**后端也必须是两个端点。**

### 3.3 扫描（Scans）

| 方法 | 路径                 | 鉴权     | 说明                                |
| ---- | -------------------- | -------- | ----------------------------------- |
| GET  | `/scans`             | read     | 列表。支持 `?status=&q=&sort=&dir=` |
| POST | `/scans`             | **scan** | 新建 —— **高风险，见 §5**           |
| GET  | `/scans/{id}`        | read     | 详情（含完整 `Scan`）               |
| POST | `/scans/{id}/abort`  | **scan** | 终止                                |
| POST | `/scans/{id}/rerun`  | **scan** | 重跑                                |
| GET  | `/scans/{id}/agents` | read     | 该扫描的 agent 名册（`Agent[]`）    |
| POST | `/scans/{id}/resume`  | **scan** | 续跑（同一条记录接回上次会话，见 §3.3.2）|
| GET  | `/scans/{id}/writeup` | read     | CTF 题解报告（见 §3.18）             |
| POST | `/scans/batch/archive` | **scan** | 批量归档 / 取消归档（v0.13）      |
| POST | `/scans/batch/delete`  | **scan** | 批量软删除（v0.13）               |
| POST | `/scans/batch/restore` | **scan** | 批量从回收站恢复（v0.13）         |
| POST | `/scans/batch/rerun`   | **scan** | 批量重跑（v0.13，**逐条授权确认**）|
| DELETE | `/scans/{id}/purge`  | **scan** | **永久删除**（v0.13，仅回收站内） |
| GET  | `/scans/compare`     | read     | 对比两条扫描的发现增减（v0.13）     |

#### 批量操作（v0.13 新增）

**请求 / 响应**（与 `POST /findings/status` **同形**，刻意不再发明一套）：

```jsonc
// POST /scans/batch/archive
{ "ids": ["scan_a", "scan_b"], "archived": true }   // archived=false = 取消归档

// POST /scans/batch/delete  /  restore
{ "ids": ["scan_a", "scan_b"] }

// POST /scans/batch/rerun  —— ★ 必须带授权确认，见下
{ "ids": ["scan_a"], "authorization": { "confirmed": true, "statement": "…" } }
```

```jsonc
// 统一响应
{
  "changed": 2,          // 真的变了状态的条数
  "skipped": 0,          // 已经是目标状态的（幂等跳过）
  "scans": [ /* 变更后的完整 Scan[]，仅含 changed 的 */ ],
}
```

> **为什么三个操作共用一个形状**：前端只有一套处理逻辑（提示改了几条、
> 哪几条没生效）。每个操作自造一个响应形状，等于把这段逻辑抄三遍。

> **★ 归档 / 软删除 / 恢复都必须是 `scan` 级而不是 `read`。**
> 它们**改变数据**，而 `read` 的定义是"只读"（§1.2）。给更低级别的话，
> 一个只读令牌就能把整页扫描记录归档或删进回收站 —— 那是**掩盖痕迹**，
> 比读到数据严重得多。

> **⚠️ `batch/rerun` 必须逐条带授权确认。** `POST /scans/{id}/rerun` 强制要求
> 逐字授权声明（§5.3，法律要求）。批量重跑是 **N 次新扫描**，
> **不能因为"批量"就绕过它**。服务端对每一条都校验，并各自落留痕六列。

> **⚠️ 永久删除（`purge`）只在回收站内可用。** 对一条没被软删除的扫描调它
> 返回 `409`——"先删进回收站，再永久删除"是两道闸，
> 合成一步等于把 `purge` 变成一个不需要经过回收站的 `rm -rf`。
查询参数用**前端已有的枚举值**：

- `status`：`queued|running|completed|failed|aborted`（或省略 = 全部）
- `sort`：`recent|severity|duration`（对应 `SORT_LABEL` 的键）
- `dir`：`asc|desc`

**`POST /scans` 请求体**：

```jsonc
{
  "name": "api.acme.internal", // 可空，后端用 target 兜底
  "target": "api.acme.internal", // ⚠️ 已归一化后送上来
  "targetKind": "url",
  "skills": ["web", "api"],
  "providerId": "deepseek",
  "authorization": {
    // ★ 见 §5
    "confirmed": true,
    "statement": "我有权对此目标进行安全测试",
    "confirmedAt": "2026-09-23T15:30:42.123Z",
  },
}
```

**响应**：`201` + 完整 `Scan` 对象，`status` 为 `"queued"`。

> ⚠️ **初始状态必须是 `queued` 而不是 `running`。**
> `scans-store.ts` 里写明了理由：`running` 意味着"agent 已经在打了"，
> 而实际要等调度器接单。状态语义不能撒谎。

> ⚠️ **`rerun` 是新建一条记录，不是把旧记录改回 running。**
> 扫描历史有价值（哪次发现了什么、修没修）。前端注释：
> "把已完成的记录改回 running 会把那次历史抹掉"。

**`abort` 的前置条件**：只允许 `queued` / `running`。
对其它状态返回 `409 SCAN_NOT_ABORTABLE`。前端 `abortScan` 对已完成扫描返回 `false`。

### 3.3.1 扫描是怎么真的跑起来的（v0.4 新增）

契约原先只定了接口形状，没定执行模型。任务 3 把它补上：

```mermaid
flowchart LR
    A["POST /scans"] --> B["queued"]
    B --> C["调度器认领"]
    C --> D["running"]
    D --> E["completed / failed / aborted"]
```

**状态与退出码的映射**（外部工具的退出码契约）：

| 退出码 | 含义 | 本平台的状态 |
| ------ | ---- | ------------ |
| `0` | 跑完（无人值守下表示**没发现漏洞**） | `completed` |
| `1` | 致命错误（含 **Docker 不可用**、缺环境变量） | `failed` |
| `2` | **发现了漏洞** | `completed` ← ★ 见下 |
| 其它 | 异常结束 | `failed` |

> ★ **退出码 `2` 是成功，不是失败。** 把"结果不干净"当成"跑挂了"，
> 会让本来成功的扫描显示成失败，而用户会去重跑它。
> 这是这一层最容易写错的一处。

**并发上限**：同时最多跑 `SUPEX_MAX_CONCURRENT_SCANS` 个（默认 1），
超出的留在 `queued`。这不是性能优化，是安全阀 ——
一次扫描会拉起一个沙箱容器并对真实目标发包，失控的并发意味着
同时打多个目标、费用不可控，而界面上还说不清"现在到底在跑几个"。

`GET /scans/queue` 回答「为什么我的扫描一直排队」：

```jsonc
{ "running": 1, "limit": 1, "queued": 2, "runner": "strix",
  "runnerReady": true, "runnerError": null }
```

`runnerReady: false` 时 `runnerError` 说明原因（典型：strix 没装）。
前端应当把这条显示出来 —— 否则用户点"开始扫描"只会看到扫描立刻失败，
而不知道是环境缺东西。

**凭据只走环境变量**：`STRIX_LLM` / `LLM_API_KEY` / `LLM_API_BASE`。
**绝不进 argv、也不写 `--config` 文件** —— 命令行参数对同机其他用户可见
（`ps aux`），而配置文件会变成磁盘上的第二份明文凭据，
与 §2.9 的「密钥全链路不出现明文落盘」直接冲突。

**服务重启留下的 `running` 扫描**：进程还活着的会被**终止**（置 `aborted`），
进程已不在的置 `failed` 并注明"结果不可知"。**两种都不猜** ——
一个没人能终止的攻击进程比一次白跑的扫描危险得多，
而报 `completed` 则是编造我们并不知道的结论。

#### 3.3.2 续跑（`POST /scans/{id}/resume`，v0.20 补记）

**与「重跑」的区别是本质的**：

| | 记录 | 起点 | 保住的东西 |
| --- | --- | --- | --- |
| 重跑 | **新建一条** | 从零 | 上次那条的完整历史 |
| 续跑 | **同一条** | 接回上次的会话 | 执行后端的 root 与每个未终结子 agent 的**完整 LLM 历史与拓扑**，外加工作目录里的 `notes.json` / `threat_models.json` / `todos.json` / `coverage.json` |

判据不是"哪个更好"，而是"上一次留下了什么"：跑了十几分钟才倒下的扫描，
那些上下文本身就是最贵的产物，从零再来等于把它扔掉。

> **响应是 `201`，但记录不是新的。** 状态就地从 `failed`/`aborted` 回到
> `queued`，`runId` 不变。`201` 表达的是"受理了一个新的执行意图"，
> 不是"新建了一个资源"—— 前端因此**不能**拿它去做"往列表里插一行"。

> **⚠️ 请求体沿用 `ScanRerun`，也就是要重新确认授权。** 续跑会**继续对
> 真实目标发包**，而"上次确认过"不该永久授权（与 §5 对 `rerun` 的要求同源）。

**三条 `409 SCAN_NOT_RESUMABLE`**（各自的话不一样，前端不要自己拼文案）：

| 条件 | message 的要点 |
| --- | --- |
| 在回收站里 | 先恢复再续跑 |
| 状态不是 `failed`/`aborted` | 只有失败或已终止的可以续跑，并带上当前状态 |
| 执行后端的会话文件已不在（`can_resume`） | **必须给出替代动作**：改用「重跑」 |

> **★ `runId` 为空时先回磁盘找一遍，而不是直接报"会话文件已不在"。**
> 这是实测踩到的（2026-09-27）：服务重启会打断扫描——进程句柄在内存里丢了，
> 孤儿回收直接把它标成 `failed`，而平时补录 `runId` 的那段逻辑遍历的
> **正是那张已经空掉的句柄表**。于是 `runId` 永远补不上，续跑稳定失败，
> 而被它挡住的恰恰是最该能续的场景。现在走 `runner.detect_run_id()` 从磁盘
> 找回来并**写回记录**（否则判据每被问一次就要重扫一遍磁盘）。

> **CTF2 的壳不能续跑**（`409 SCAN_IS_CTF2_SHELL`）：它的"上次会话"根本不存在
> （strix 从没跑过它），而这条路径最终会走到那句与事实相反的
> "会话文件已不在"。状态检查放在最前面，正是为了让这句假话说不出口。

### 3.4 漏洞（Findings）

| 方法 | 路径               | 鉴权     | 说明                                                       |
| ---- | ------------------ | -------- | ---------------------------------------------------------- |
| GET  | `/findings`        | read     | 列表。`?severity=&status=&category=&q=&scanId=&sort=&dir=&assigneeId=&unassigned=&slaState=&deleted=` |
| GET  | `/findings/{id}`   | read     | 详情（含 `detail` + `timeline`）                           |
| POST | `/findings/status` | **scan** | **批量**状态流转                                           |
| POST | `/findings/assign` | **scan** | **批量**指派负责人（v0.15）                                 |
| GET  | `/findings/stats`  | read     | 各维度计数（见下）。**收与列表同一套筛选参数**（v0.18）      |
| GET  | `/findings/groups` | read     | 按漏洞标题分组（v0.15）                                     |
| GET  | `/findings/export` | read     | **CSV 导出当前筛选结果**（v0.18）                           |
| POST | `/findings/batch/delete` | **scan** | 批量**软删除**（进回收站，v0.18）                     |
| POST | `/findings/batch/restore` | **scan** | 批量从回收站恢复（v0.18）                            |
| DELETE | `/findings/{id}/purge` | **scan** | **永久删除**（仅回收站内，v0.18）                     |
| GET  | `/findings/{id}/attachments` | read | 附件列表（v0.18）                                    |
| POST | `/findings/{id}/attachments` | **scan** | 上传附件，**multipart/form-data**（v0.18）        |
| GET  | `/findings/{id}/attachments/{aid}/content` | read | 下载附件内容（v0.18）                  |
| DELETE | `/findings/{id}/attachments/{aid}` | **scan** | 删除附件（v0.18）                           |

> **为什么批量端点是 `POST /findings/status` 而不是 `PATCH /findings/{id}`**：
> 前端是全选后一次提交（`BulkActionBar`），逐条 PATCH 会发出 N 个请求。
> 而且响应需要**聚合结果**（改了几条、跳过几条）。

> ⚠️ **路由顺序**：`/findings/stats`、`/findings/assign`、`/findings/groups`、
> `/findings/export`、`/findings/batch/*`
> 都必须声明在 `/findings/{findingId}` **之前**。否则它们会被当成一个 finding
> id 匹配掉，症状是「404 说找不到叫 groups 的漏洞」—— 看起来像数据不存在，
> 实际是声明顺序问题。这个坑在本项目已经踩过三次。

**请求**：

```jsonc
{
  "ids": ["finding_01", "finding_02"],
  "status": "confirmed",
  // 可选，覆盖默认的流转摘要文案。
  // ★ v0.14：当 `status` 为 `false_positive` 时**必填**（去空白后非空），
  //   否则 400 VALIDATION_FAILED。
  //
  //   判为误报必须给理由，因为它要回答一个具体问题：**规则误报率是多少、
  //   是哪一类规则在误报**。没有理由的误报只能计数，没法复盘 ——
  //   而那个数字决定了下一步该去调规则、还是调扫描配置。
  //
  //   批量场景下填**一次**理由即应用到全部选中项，不要求逐条填写：
  //   逐条填会让"批量判误报"重新变得不可用，用户会转而用「已确认」糊过去，
  //   反而把这个信号丢掉了。
  "note": null,
}
```

**响应**（与前端 `setFindingStatus` 的返回值对齐）：

```jsonc
{
  "changed": 1,
  "skipped": 1, // 已经是目标状态的条目（幂等跳过，不写时间线）
  "findings": [/* 更新后的完整 Finding[]，仅含 changed 的 */],
}
```

> ⚠️ **`skipped` 必须返回**：前端会用它提示"实际改了几条、跳过几条"。
> 而"全部选中项都已是目标状态"时前端会禁用按钮 —— 后端仍要做幂等校验（双保险）。

**`GET /findings/stats`** 响应（供筛选 chip 角标与 Overview 图表）：

```jsonc
{
  "total": 13,
  "bySeverity": { "critical": 2, "high": 4, "medium": 3, "low": 3, "info": 1 },
  "byStatus":   { "new": 6, "confirmed": 3, "fixed": 2, "false_positive": 2 },
  "byCategory": { "injection": 2, "auth": 3, ... },
  "falsePositiveCount": 2
}
```

> ⚠️ **`falsePositiveCount` 必须单独返回。** Overview 的指标卡排除了误报，
> 界面上有一句「上面两张指标卡已排除 N 条被判为误报的条目」。
> 前端注释：**"排除是可以的，悄悄排除不行"**。

#### 3.4.1 ★ 统计是**唯一数据源**（v0.6 明确）

`GET /findings/stats` 是**唯一算统计的地方**。Findings 页的 chip 角标与
Overview 页的图表都必须读它 —— **不允许各自的页面再数一遍**。

理由不是洁癖：两次独立的计数查询在并发编辑下会取到**不同的快照**
（一次在改状态之前、一次在之后），于是两页显示 13 与 12。
用户不知道哪个对，而那种不信任会扩散到整个产品。

后端的落实方式：`findings/service.py` 的 `stats()` 是唯一实现，
`overview_metrics()` **内部调它**，不自己写 SQL 数一遍。
契约 §3.6 的 `/overview` 响应里 `severityBreakdown` 与
`falsePositiveCount` 必须来自它。

**两处口径差异必须说清**（契约 §3.6 已有，这里再强调）：

| 字段 | 是否排除误报 |
| --- | --- |
| `severityBreakdown` / `bySeverity` | ❌ **包含全部发现**，与 Findings 页逐条对得上 |
| `metrics.verifiedPocCount` | ✅ 排除误报 |
| `metrics.newHighSeverityThisWeek` | ✅ 排除误报 |

所以 `falsePositiveCount` 一定要返回 —— 界面靠它说明"上面两张卡排除了几条"。

#### 3.4.2 `bySeverity` 是 `Partial`，`byStatus` / `byCategory` 不是（v0.6 明确）

| 字段 | 为 0 时 |
| --- | --- |
| `bySeverity` | **键不存在**（与 §2.1 的 `findings` 同一口径） |
| `byStatus` | **键存在且为 0** |
| `byCategory` | **键存在且为 0** |

这个不一致是**有意的**，不是疏漏：严重度是**分档展示**（Overview 的
四张卡、Findings 的筛选 chip 都是固定档位），而状态与分类是
**筛选维度**——"这一类一条都没有"本身就是界面要显示的信息。
反过来说，严重度的 `Partial` 是前端类型定的（`Partial<Record<Severity, number>>`），
契约只能照抄。

### 3.5 报告（Reports）

| 方法   | 路径                     | 鉴权 | 说明                                                                |
| ------ | ------------------------ | ---- | ------------------------------------------------------------------- |
| GET    | `/reports`               | read | 历史列表                                                            |
| POST   | `/reports`               | read | 生成一份报告                                                        |
| GET    | `/reports/{id}`          | read | 详情（含冻结的 `findingIds`）                                       |
| GET    | `/reports/{id}/download` | read | 下载。**本轮只支持 `format=markdown`**；PDF 走浏览器打印，见 §3.5.1 |
| DELETE | `/reports/{id}`          | **admin** | 删除。⚠️ v0.10 从 `read` 提升，见下                            |
| POST   | `/reports/preview`       | read | 预览（不落库，返回 `ReportDocument` IR）                            |

> ⚠️ **v0.10 修正（任务 8 的鉴权排查发现）：`DELETE /reports/{id}` 原来是 `read`，那是错的。**
>
> 依据是本契约 §1.2 自己的定义 —— `read` 写的是
> **「只读：看扫描、漏洞、报告、设置」**，而删除不是读操作。
>
> 它还是这一组里**唯一不可逆**的动作：报告一旦生成就有编号（`RPT-…`），
> 而编号是客户拿去对账的东西 —— 删掉之后那个编号再也对不上任何内容。
> 让它与"看报告"同权，等于一个只用来跑报表的 `read` 令牌
> 可以把交付物删干净。
>
> 定为 `admin` 而不是 `scan`：`scan` 的定义是「会对真实目标发包」，
> 而删除报告不发包。它与 `DELETE /providers/{id}`、`DELETE /settings/tokens/{id}`
> 同类（**销毁型操作**），所以同级。
>
> 对前端**无影响**：删除按钮走会话，而会话隐含 `admin`（§1.2）。

**`POST /reports` 请求**：

```jsonc
{
  "title": "api.acme.internal 检测报告",
  "scope": { "kind": "scan", "scanId": "scan_01J8X2" },
  "options": {
    "includePoc": false,
    "includeReasoning": false,
    "onlyConfirmed": false,
    "includeRemediation": true,
  },
  "format": "markdown",
}
```

**响应**：`201` + 完整 `Report`（含后端分配的 `id` 与派生的报告编号）。

**下载响应**：

- `format=markdown` → `200` + `text/markdown` + `Content-Disposition: attachment; filename="RPT-20260923-A1B2.md"`
- `format=pdf` → **本轮由前端走浏览器打印**，不调这个端点（见 §3.5.1）。
  将来服务端渲染可用了，同一个路径返回 `application/pdf` + 同样的文件名规则（`.pdf`）

> ⚠️ **文件名必须用报告编号，不是标题。** 前端 `report-api.ts` 说明了原因：
> 标题是中文、可能带空格和斜杠，直接当文件名会出现编码差异甚至被截断。
> 编号是 ASCII 且天然唯一，客户能拿它和正文里的编号对上。

### 3.5.1 PDF 的决策：这一轮保留浏览器打印

|          |                                                                                                                    |
| -------- | ------------------------------------------------------------------------------------------------------------------ |
| 现状     | PDF 由前端用**浏览器打印引擎**产出（`ReportArtifact.kind === 'browser-print'`），用户在打印对话框里选"另存为 PDF"  |
| **决策** | **本轮保留**。服务端 PDF 定为**可选增强**，且实现时必须用**无头 Chromium 打印同一份 HTML**，不得引入第二套排版引擎 |

#### ★ 任务 6 的实测复核（v0.7 补）：方案可行但**不该做**

任务 6 要求「评估两个方案后选一个，并在 `/docs/api-contract.md` 里补充说明选择理由」。
所以这一节不是把上面的决策重述一遍，而是**补上它当初缺的那条事实核查**。

**核查一：服务端 PDF 在本机可行吗？—— 可行，实测过。**

这台机器上 Edge 与 Chrome 都在（`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`），
无头打印实测成功：

```powershell
msedge.exe --headless=new --disable-gpu --no-pdf-header-footer --print-to-pdf=out.pdf file:///t.html
# → exit=0，生成 41196 字节的 PDF
```

所以"做不到"这个说法**不成立**，得如实修正。技术路径是通的。

**核查二：那为什么不选它？—— 因为约束不是能力，是架构。**

任务给的第二个选项是「前端已经能拿到结构化 JSON 时，评估是否继续用前端渲染导出更简单」。
而这里的关键在于：**服务端要打印的那份 HTML 从哪来？**

| 做法 | 结果 |
| --- | --- |
| 后端维护一套 HTML 模板渲染 IR | ❌ **第二套版式** —— 页边距/分页/中文字体/表格断行各自演化，正是前端整套 IR 要消灭的分叉 |
| 后端把前端的 React 组件也搬过去渲染 | ❌ 要引入 Node + 前端构建产物，把"单机 Compose"变成三件套；且 React 组件依赖浏览器 API |
| 前端把**渲染好的 HTML** 发给后端打印 | ⚠️ 可行，但那等于"后端只是个打印驱动"，而前端本来就能直接调打印 |

也就是说：**服务端 PDF 的价值（无人值守）成立的前提，恰恰是它能独立渲染；
而独立渲染就意味着第二套版式。** 两者不可兼得。

所以结论不是"技术上做不到"，而是：**在这条约束下（不得引入第二套排版引擎），
服务端 PDF 无法买到它唯一的优势**。保留浏览器打印，`ReportArtifact` 的
`{ kind: 'file' }` 分支继续留给将来。

> **什么情况下该重新评估**：出现真正的无人值守需求（定时报表、CI 产出、
> 邮件附带报告）。那时正确的做法**仍然不是**写一套 HTML 模板，而是
> 让前端构建产物能产出静态 HTML（把 IR 渲染成 HTML 的那层抽出来），
> 后端只负责调无头 Chromium 打印**那一份 HTML**。
> 这条硬约束在决策表里已经写了，这里补的是"为什么它挡住了本轮"。

#### 为什么不在这轮改成服务端渲染

1. **它会威胁一条已有的验收标准。** 任务 6 要求"预览效果和导出内容一致"。
   现在这条是**物理保证** —— 打印引擎渲染的就是屏幕上那个 DOM。
   若后端用 Python PDF 库（WeasyPrint / ReportLab）重排一遍，
   就有了**第二套版式**：页边距、分页点、中文字体回退、表格断行
   都会各自演化。前端整套报告 IR 就是为了消灭这种分叉而存在的
   （`report-document.ts` 顶部有完整论证），在导出端重新引入它是自相矛盾的。
2. **依赖成本落在最不该加的地方。** 无头 Chromium 镜像约 400MB，
   而任务 1 是**脚手架**任务。给脚手架塞一个重型渲染依赖，
   会让"能不能跑起来"这个最基本的问题先变复杂。
3. **它服务的使用场景不在本批任务里。** 服务端 PDF 的**唯一**真实优势是
   **无人值守生成**（定时报表、邮件附带、CI 产出）。合集的部署形态是
   "本地/单机 Compose"，没有这些需求。

#### 但它不是被否决，而是被定义为"可增量接入"

这正是前端把 `ReportArtifact` 设计成 discriminated union 的原因 ——
类型形状已经预留：

```ts
{ kind: 'browser-print', filename, serverReady: null }   // 现在
{ kind: 'file', filename, mime, content }                 // 将来
```

将来接入时**前端只改分发逻辑，不改任何调用方**。后端实现时的硬约束：

> ⚠️ **必须打印前端渲染的那份 HTML**（无头 Chromium `page.pdf()`），
> 而不是用另一个库重新排版。这样"预览=导出"仍然是物理保证，
> 只是打印引擎从用户浏览器换到了服务端。

#### 如实记录当前实现的代价（不粉饰）

| 代价                       | 说明                                                                                                 |
| -------------------------- | ---------------------------------------------------------------------------------------------------- |
| 必须人工点对话框           | 浏览器无法告知"用户是保存了还是取消了"。前端提示语已如实写成"下一步该做什么"，而不是谎称"PDF 已生成" |
| **打印对话框阻塞渲染线程** | `window.print()` 期间页面无响应。这是浏览器原生行为，不是缺陷                                        |
| 无法批量/自动产出          | 没有服务端渲染就没有无人值守导出                                                                     |

> 三条都是**已知且接受**的，不是遗漏。记在这里以免后来者以为是 bug。

> ⚠️ **`preview` 与生成必须走同一份 IR。** 前端 `buildReportDocument` 是
> "预览与导出的唯一真相"。后端实现时也要保证：预览响应与下载内容
> **从同一份数据结构渲染**，不要写两套模板 —— 那必然漂移。
> 前端有 `npm run verify:report` 做机器化比对（12 组范围×选项组合），
> 后端接入后**这个脚本应当改为对着 API 跑**。

### 3.6 全局统计（Overview）

| 方法 | 路径        | 鉴权 | 说明                     |
| ---- | ----------- | ---- | ------------------------ |
| GET  | `/overview` | read | 一次拿齐 Overview 页所需 |

查询参数：`?scanId=`（可选）—— 把**统计范围**收窄到某一次扫描。
⚠️ 它**不影响** `activeScans` / `hasAnyScan`：那两个描述的是全局的扫描队列，
而不是"这次扫描里有多少个在跑"（那永远是 0 或 1，没有信息量）。
这是一次刻意的**部分收窄**，不是遗漏。

**响应**（对应 `data/overview.ts` 的 `OverviewData`）：

```jsonc
{
  "metrics": {
    "activeScans": 2,
    "newHighSeverityThisWeek": 6,
    "newHighSeverityDelta": null,       // null = 无同比数据（上周为 0）
    "verifiedPocCount": 6,
    "avgScanDurationSec": 840,          // null = 没有已完成扫描
    "falsePositiveCount": 2,
    // ── CTF2 token 消耗（v0.36）───────────────────────────
    // 细分（按题/按模型/逐轮）在 /ctf2/usage 与 /ctf2/challenges/{id}/usage
    "ctf2LlmCalls": 318,                // 调用次数：**永远准确**
    "ctf2RecordedCalls": 316,           // 其中接入点真给了用量的次数
    "ctf2TotalTokens": 4821130,
    "ctf2UsageSince": "2026-10-14T10:02:11Z"  // null = 一条记录都没有
  },
  "severityBreakdown": [ { "severity": "critical", "count": 2 }, ... ],
  "activity": [ /* 最近至多 8 条，**投影形状**，见 §2.5 与下方的例外说明 */ ],
  "hasAnyScan": true
}
```

**三个 null/空值的语义必须守住**（前端注释反复强调）：

1. `newHighSeverityDelta: null` = **没有上周数据**，界面显示"暂无对比数据"，
   **不能返回 0** —— 0 变化和无法比较是两回事。
2. `avgScanDurationSec: null` = 没有已完成扫描。**不能返回 0** ——
   0 秒会让用户以为扫描瞬间完成。
3. `severityBreakdown` **包含全部发现**（与 Findings 页逐条对得上）；
   `metrics` 里只有"本周新增高危"和"已验证 PoC"排除误报。
   这个口径差异**必须在界面上说明**，所以 `falsePositiveCount` 要返回。

**★ 第四个 null 语义（v0.36 新增）：token 统计的"没记录"**

| 字段 | 怎么读 |
| --- | --- |
| `ctf2UsageSince: null` | **一条记录都没有** —— 界面说"还没有数据"，不说"0 token" |
| `ctf2RecordedCalls < ctf2LlmCalls` | `ctf2TotalTokens` 是**下限**：有几次调用接入点没给用量 |
| `ctf2LlmCalls == 0` | 从没跑过 CTF2 解题（与"跑过但没记"是两回事，后者看 `since`） |

> ⚠️ **`ctf2LlmCalls` 与 `ctf2RecordedCalls` 不能合成一个。**
> 前者从行数数得出来（永远准确），后者取决于接入点给不给 `usage`。
> 合成之后，"消耗 0"与"没记录到"在界面上完全一样 ——
> 而"某次解题消耗 0"会让用户以为它没跑过、或者以为那次特别省。
> 与 §2.9 的 `hasKey`/`apiKey` 分两列、§3.19 的
> `candidateFlag`/`confirmedFlag` 分两列是同一条纪律。
>
> **不做 token → 金额换算**：单价随模型、供应商、时段变化，
> 写死在代码里会误导（详见 §3.19.6）。只给 token 数。


> **派生而非写死**：`activeScans`、`avgScanDurationSec`、`severityBreakdown`
> 都应该从集合算出来。前端 mock 就是这么做的，理由是"换真实 API 时派生逻辑
> 一行不用改"，且不会出现"卡片写 12 但列表只有 10 条"。

#### 实现要点（v0.8 补，任务 6 落地）

**每条数据的来源**（全部拼到已有的唯一数据源上，绝不另起一套计数）：

| 字段                       | 来源                                        |
| -------------------------- | ------------------------------------------- |
| `metrics.*`                | `findings.overview_metrics()` → `stats()`    |
| `activeScans` / `hasAnyScan` | `scans` 表（与 `/scans`、`/scans/queue` 同表）|
| `avgScanDurationSec`       | 已完成扫描的 `duration_sec`                  |
| `severityBreakdown`        | `findings.overview_metrics()`（**含误报**）  |
| `activity`                 | `activity_events`（完整事件 → 投影）         |

★ **`avgScanDurationSec` 只算 `completed`**，不含 `failed`/`aborted`：
那两次的耗时反映的是"多久放弃的"，与"扫描一次要多久"不是同一个问题 ——
混进去会把这个数字拉低，而它恰恰是用来做容量预估的。

★ **`severityBreakdown` 与 `/findings/stats` 的口径不同是刻意的**：
这里固定返回**全部 5 档（含 0）**，而 `stats.bySeverity` 是 `Partial`
（为 0 的档键不存在，§3.4.2）。理由与 §3.4.2 记的是同一条 ——
图表里"缺一根柱子"比"一根 0 高的柱子"更容易被误读成漏了。

★ **`metrics` 排除误报，「已修复」仍然算**：这两个指标回答的是
「我该处理什么」。一条被判误报的条目根本不是漏洞，算进"待处理的高危"
会让这个数字**永远降不下来** —— 用户在 Findings 页认真分诊一圈，
Overview 纹丝不动，那次分诊就成了没有反馈的无效劳动。
而"已修复"曾经是个真漏洞，属于"发现过什么"的记录；按状态抹掉会让
"本周新增"变成一个随处置而**回溯变化**的数字，没法用。
一句话：这个过滤器排除的是「**判断错误**」，不是「已处理」。

★ **`activity` 是投影形状**（扁平、`subject` 而非 `detail`、只 6 种类型，
不含 `reasoning` 与 `failed`）—— 这是 §2.5 里那条例外的落地，
理由见 §2.5 的 ⚠️ 说明。实现上先多取一批再过滤：真实产出里
`reasoning` 占比不低（agent 每步思考都是一条），只取 8 条再滤可能
一条都不剩，界面看起来像"扫描没在动"。

★ **`subject` 派生不出时返回 `null`**，不编占位串。前端类型里它就是
`string | null`，编一个假对象会让"这条事件没有具体对象"这个真实信息消失。

#### 一处历史遗留的修正（v0.8）

写 `/overview` 的测试时对账发现：**`/findings/stats` 的 `bySeverity`
并没有满足它自己的 `Partial` 语义** —— 契约 §3.4.2 说"为 0 的档键不存在"，
而实际响应是 `{"critical": 1, "high": null, "medium": null, ...}`。

原因是 `SeverityCounts` 的字段声明为 `int | None`，Pydantic 默认把
`None` 序列化成**显式的 null**，而不是省略。它一直没暴露，因为前端
读写都兼容（`counts[sev] ?? 0`）—— 而**"两边都接受"恰恰是它危险的地方**：
契约说"键不存在"，响应对不上，下一个**严格按契约**写解析逻辑的人就会踩空。

修法是给那条路由加 `response_model_exclude_none=True`。它在
`FindingStatsOut` 上是**精确命中**的：可空的只有 `bySeverity` 的五个字段，
`byStatus` / `byCategory` 是 `dict[str, int]`，永不为 `None`。

> 顺带记一条方法论：任务 5 的验收脚本当时把 `bySeverity` **过滤掉假值**
> 之后再做比较，于是 `null` 和"键不存在"在它眼里是同一件事 ——
> 那条检查是**空转**的。这次是拿"契约逐字 vs 实际响应"硬对才发现的。

### 3.7 设置（Settings，`admin`）

| 方法   | 路径                                   | 说明                                                           |
| ------ | -------------------------------------- | -------------------------------------------------------------- |
| GET    | `/settings`                            | 一次返回 `SettingsSnapshot`（accounts/sandbox/notices/tokens）。**v0.22 起真的返回四块** |
| GET    | `/settings/sandbox`                    | 读沙箱与执行环境配置（v0.21 补记，此前只记了 PUT）             |
| PUT    | `/settings/sandbox`                    | 沙箱配置                                                       |
| GET    | `/settings/finding-sla`                | 漏洞修复时限（v0.16 已有，此处补齐）                           |
| PUT    | `/settings/finding-sla`                | 改漏洞修复时限（v0.16 已有，此处补齐）                         |
| GET    | `/settings/notices/rule`               | 读通知规则（v0.22）                                            |
| PUT    | `/settings/notices/rule`               | 改通知规则                                                     |
| GET    | `/settings/notices/channels`           | 渠道列表（**裸数组**，v0.22）                                  |
| POST   | `/settings/notices/channels`           | 新增渠道                                                       |
| PATCH  | `/settings/notices/channels/{id}`      | 改渠道                                                         |
| DELETE | `/settings/notices/channels/{id}`      | 删渠道                                                         |
| POST   | `/settings/notices/channels/{id}/test` | 发测试通知（**动作型**）                                       |
| GET    | `/settings/tokens`                     | 令牌列表（**只有 prefix**）                                    |
| POST   | `/settings/tokens`                     | 创建 —— **完整令牌只在此响应出现一次**                         |
| DELETE | `/settings/tokens/{id}`                | 吊销                                                           |
| POST   | `/settings/password`                   | 改密码                                                         |
| GET    | `/settings/scan-retention`             | 扫描保留规则（v0.13）                                          |
| PUT    | `/settings/scan-retention`             | 改扫描保留规则（v0.13）                                        |
| GET    | `/settings/scan-views`                 | 「我的视图」列表（v0.13）                                      |
| PUT    | `/settings/scan-views`                 | 覆盖式保存视图列表（v0.13）                                    |

#### 通知（v0.22 —— 本模块最后一块）

**规则与渠道是两个端点、两个维度**，不是"一个配置对象拆成两块"：

| | 端点 | 是什么 | 形态 |
| --- | --- | --- | --- |
| `NoticeRule` | `/settings/notices/rule` | **什么情况下发** | 单例（必须有值）|
| `NotifyChannel` | `/settings/notices/channels` | **怎么发** | 列表（空表正常）|

> **为什么必须分开**：合成一个对象会让「邮件收全部、Slack 只收严重」
> 这种真实需求**无处表达** —— 而它是运维的常态，不是边角情况。

```jsonc
// NoticeRule
{
  "minSeverity": "high",              // critical | high | medium | low | info
  "notifyOnScanComplete": true,       // 扫描完成时是否通知（无论有没有问题）
  "notifyOnCriticalImmediate": true,  // 严重问题是否立即通知，而不是等汇总
  "updatedAt": "2026-10-10T…"
}

// NotifyChannel
{
  "id": "ntc_…", "kind": "email" | "webhook" | "slack",
  "label": "安全组邮件列表",
  "target": "sec@example.com",        // 邮箱 / Webhook URL / Slack Webhook URL
  "enabled": true,
  "verified": false,                  // 服务端的事实，不是用户的声明
  "verifyError": null,
  "createdAt": "…", "updatedAt": "…"
}
```

**四条实现约束**（都是"看起来可以省、其实不能省"的）：

1. **`verified` 不接受客户端传。** schema 里就没有这个字段，传了会被
   `StrictModel` 以 `400 VALIDATION_FAILED` 拒绝（并点名是哪个字段）。
   它是"服务端真的发过一次测试通知"的**事实** —— 让请求体能带它，
   界面上那个绿勾就变得毫无意义，而用户会以为通知链路是通的。

2. **★ 改 `target` 必须把 `verified` 打回 `False`。** `verified` 的含义是
   "**这个地址**发过一次测试并成功了"。地址变了，那个事实不再成立。
   漏掉的症状是"配好了、显示已验证、但真出事时一条通知都没发出去" ——
   而那时已经太晚了。改 `label` / `enabled` **不影响**验证状态
   （地址没变，事实依然成立）。

3. **停用的渠道仍然列出。** 停用是状态，不是删除 —— 隐藏它会让用户
   以为配置丢了、然后重新配一个。
   （与 `GET /settings/tokens` 的"已吊销不列出"**不同**：那里的令牌
   已彻底失效，留着只会造成"它还能用"的误解。）

4. **`POST .../test` 本轮只做本地校验，不真的往外发。** 真实发送需要
   HTTP 客户端 + 每个渠道类型的协议实现（SMTP / Webhook / Slack payload），
   那是独立的一块工作。**假装发了是禁止的** —— 它会让界面显示"已验证"，
   而真出事时一条都发不出去。所以它校验地址形状，并把结论如实写回
   `verifyError`（形状不对说形状问题，"尚未接入真实发送"就照实说）。
   形状判据**刻意宽松**（邮箱只要求有 `@`、webhook 只要求 http/https
   开头）：这是配置校验，不是安全边界，过严会挡掉合法的内网地址。

> **`GET /settings` 从 501 变成 200 的前提**：它长期返回 501 的理由是
> "四块快照缺三块却回 200，调用方会以为自己拿全了"。**那条判断至今成立**，
> 本轮没有推翻它 —— 推翻的是它的前提：现在四块都拼得出来了。
> ⚠️ 这条规矩要留着：**只要还有一块拼不出来，这个端点就该继续 501**。
> 回一个缺字段的 200 比 501 糟得多，501 至少说的是实话。

> **`GET /settings` 的响应里没有 `status` / `error`**：前端
> `SettingsSnapshot` 上那两个字段是**本地加载状态**，描述的是"这次请求
> 进行得怎么样"，不是服务端数据。服务端下发它们等于替前端声明它自己的状态机。

#### 扫描保留规则（v0.13）

```jsonc
// GET /settings/scan-retention
{
  "archiveAfterDays": 30,     // 失败/已终止 N 天后自动归档。0 = 关闭该规则
  "trashRetentionDays": 30,   // 回收站保留期，到期物理删除
  "lastSweepAt": "2026-09-25T01:00:00.000Z"   // 上次清理时刻
}
```

> **`lastSweepAt` 为什么要暴露**：否则"我设了 30 天，为什么这条还没被归档"
> 是一个**无法回答**的问题 —— 用户只能猜是规则没生效还是时间没到。
> 有它就能说清"上次清理是 X 时刻，还没到下一次"。

> **清理时机沿用审计日志的既有做法**（`main.lifespan` 里的 `purge_expired`）：
> **启动时跑一次，不设后台定时任务**。单机部署里为"清理及时几小时"
> 多养一个常驻线程不划算，而记录多留几小时没有任何危害。

#### 「我的视图」（v0.13）

```jsonc
// GET /settings/scan-views → { views: ScanView[] }
// PUT /settings/scan-views  ← { views: ScanView[] }   （覆盖式，全量提交）

// ScanView
{
  "id": "view_01J…",
  "name": "仅看失败",
  "query": "status=failed&sort=severity&dir=desc",   // ★ 就是 URL query 串
  "createdAt": "2026-09-25T01:00:00.000Z"
}
```

> **★ 视图存后端，不存 localStorage。** 两个理由：
> ① 项目既有原则是**凭据与状态不写浏览器存储**（`localStorage` 同源的
> 任何脚本都能读，而"我的视图"里可能含目标名这类敏感信息）；
> ② 视图是用户资产，换个浏览器就该还在。
>
> **`query` 存原始 query 串而不是结构化对象**：视图的语义就是"那一组筛选条件"，
> 存串能保证**将来加筛选维度时老视图不会失效**（新维度就是串里多一个键）。
> 存成固定字段的对象则每次加维度都要迁移。

> **`query` 里的键名是客户端之间的约定**（服务端只把它当不透明串存取，**不解析**）。
> 当前使用的键：
>
> | 键 | 取值 | 省略时 |
> |---|---|---|
> | `status` | `queued` / `running` / `completed` / `failed` / `aborted` | 不筛状态 |
> | `q` | 目标名子串 | 不筛目标 |
> | `sort` | `recent` / `severity` / `duration` | `recent` |
> | `dir` | `asc` / `desc` | `desc` |
>
> **省略即默认**（编码时不写默认值）。所以读取必须「缺键兜默认」而不是
> 「缺键报错」—— 这正是老视图在新增维度后仍然可用的原因。
> 规则一句话：**认不出的键忽略，认得出但值非法的键回退默认**。
>
> **套用视图一律切回默认列表**（`view=default`）：视图表达的是"在扫描列表里
> 怎么筛"，不含"我正在看回收站"这类位置信息。若保留当前位置，用户在回收站里
> 套用一条"失败扫描"的视图会看到空列表，从而以为视图坏了。
> 因此 `query` **不含** `archived` / `deleted` 这类可见性参数。

#### 请求体（v0.2 补充）

初稿只列了路径，没给形状。按前端 `settings-store.ts` 的实际调用补齐：

```jsonc
// POST /settings/tokens  → 201 Created
{
  "label": "CI 流水线",
  "scopes": ["read", "scan"],
  "expiresAt": "2026-12-23T15:30:42.123Z",   // null = 永不过期（显式选项）
}
// 响应：{ token: ApiToken, plaintext: "svx_…" }  ★ plaintext 只此一次

// POST /settings/password  → 200 SaveResult（只有 ok，没有 nochange）
{
  "current": "…",
  "next": "…",
  "confirm": "…",   // 可选：前端已在客户端比对过，传了就顺手校验
}
```

`expiresAt` 用**绝对时刻**而不是 `'30d' | 'never'` 这种相对量：
相对量在服务端与客户端的时钟/时区不一致时有歧义，而前端已经有
`expiryToIso()` 把选项换算成 ISO 了。`scopes` 里出现未知值 → `400 VALIDATION_FAILED`。

`GET /settings/tokens` 返回**裸数组** `ApiToken[]`（不是 `{ tokens: [...] }`）——
与 `SettingsSnapshot.tokens` 同形状，前端两处可以共用同一个渲染组件。
已吊销的令牌**不列出**：界面上一个灰掉的、永远不能用的令牌没有价值。

> **保存类端点的响应统一用 `SaveResult` 形状**：

```jsonc
{ "status": "ok", "at": "2026-09-23T15:30:42.123Z" }
{ "status": "nochange" }
{ "status": "failed", "error": "..." }
```

> ⚠️ **`nochange` 是独立一态**：用户点了保存但没改过任何东西，
> **不能报"已保存"** —— 那是虚假的动作感。
> 前端 `SaveState` 状态机有五个态（`idle/saving/saved/failed/unchanged`），
> 后端要能表达 `nochange` 才能驱动它。

**`POST /settings/tokens` 响应**：

```jsonc
{
  "token": {
    "id": "...",
    "label": "...",
    "prefix": "svx_7f2a",
    "createdAt": "...",
    "lastUsedAt": null,
    "expiresAt": null,
    "scopes": ["read"],
  },
  "plaintext": "svx_7f2aXXXXXXXXXXXXXXXX", // ★ 仅此一次，之后永远拿不到
}
```

> 🔒 令牌前缀是 `svx_`（8 字符含下划线，前端 `prefix: plaintext.slice(0, 8)`）。
> 服务端**存哈希**，不存明文。

### 3.8 健康检查

| 方法 | 路径      | 鉴权   | 说明                                   |
| ---- | --------- | ------ | -------------------------------------- |
| GET  | `/health` | public | `{ "status": "ok", "version": "..." }` |

另建议（非必需，但对"沙箱状态"有用）：
`GET /health/sandbox` → `{ "status": "running"|"stopped"|"error", "image": "..." }`，
对应 Settings 里 `SandboxConfig.status` 的展示。

**★ v0.39 起，这三个 public 端点的暴露面按独立开关收敛**（公网审计 F3/F5/F6）：

| 端点 | 默认 | 开关 | 为什么 |
| --- | --- | --- | --- |
| `/health` | 恒可读 | —— | 只回 `status` + 版本号，编排器探活要用 |
| `/health/sandbox` | `status` 可读，**`image` 默认空** | `SUPEX_EXPOSE_SANDBOX_IMAGE`（默认 false） | 具体到版本号的镜像 tag 可定向已知 CVE |
| `/metrics` | **关闭** | `SUPEX_EXPOSE_METRICS` **且** `SUPEX_METRICS_ENABLED` | 含各路由请求计数，等于公开"多少人在用、在用哪些功能" |
| `/docs`、`/openapi.json` | **关闭** | `SUPEX_EXPOSE_DOCS`（默认 false） | 93 个 path 的完整清单 + 出入参 |

> **★ 为什么这些开关**不**绑在 `SUPEX_ENV` 上（这是最容易写错的地方）**
>
> 直觉写法是"prod 就关文档"。但 `assert_bootable()` 要求 `env=prod` 时
> 必须 `cookie_secure=true`，而线上是 **http** —— `Secure` cookie 在 http 下
> 浏览器**不回传**，**登录会直接坏掉**（症状：登录接口 200，下一个请求仍 401）。
>
> 于是"想关文档"会逼出一个"必须上 HTTPS"的要求，两件事被错误地绑在一起。
> 解耦后：`env` 管部署安全属性（cookie/密钥），这几个开关只管**挂不挂路由**。

> **⚠️ 这些开关不是安全边界**：它们只决定"挂不挂路由"。真正的边界是
> 反代（谁能连上来）与鉴权。公网部署**应当**在反代层再限一次 —— 见
> `deploy/nginx/suvex-frontend.conf`。
>
> ⚠️ 反代层按 IP 限 `/metrics` 在本部署**无效**：走 frp 隧道时 nginx 看到的
> `$remote_addr` **永远是 127.0.0.1**，`allow 127.0.0.1` 会放行所有人。
> 所以本部署改用应用层默认关闭，而不是在那里假装限制。

**★ v0.39 起 OpenAPI 里声明了认证方案**（审计 F4）：

加固前 `/openapi.json` 的 `securitySchemes` 与顶层 `security` 都是 **null** ——
一份自称"机器可读的接口说明"却从没说怎么鉴权。现在声明两套（见 §1.2）：

| scheme | 类型 | 用于 |
| --- | --- | --- |
| `BearerAuth` | `http` / `bearer`（形如 `svx_…`） | 机器令牌（CI / 脚本） |
| `SessionCookie` | `apiKey` / `in: cookie` | 浏览器会话 |

顶层 `security` 是**两个项**（OpenAPI 里项之间是 OR）—— 表达"两者之一"。

### 3.9 审计日志（v0.10 新增，任务 8）

> **为什么审计日志不是"再加一个表"**：应用日志会被日志轮转清理（按大小、
> 按天数），而"谁在什么时候对哪个目标发起了扫描"这类记录，是**事后追责**时
> 唯一拿得出来的东西。它们必须在数据库里、有自己的保留策略，
> 且**只能追加、不可修改**。

| 方法 | 路径     | 鉴权    | 说明                                                     |
| ---- | -------- | ------- | -------------------------------------------------------- |
| GET  | `/audit` | `admin` | 审计日志列表，`?limit=&cursor=&action=&actor=&since=`     |

> 本节随**任务 8（安全加固与上线前检查）**新增。那一轮的逐项结论、
> 发现的缺陷（含 4 个真实缺陷）与整改计划在
> **`suvex-api/docs/security-checklist.md`** —— 上线前要看的是那一份。

**`AuditEntry`**：

| 字段         | 类型                   | 说明                                          |
| ------------ | ---------------------- | --------------------------------------------- |
| `id`         | string                 | `aud_…`                                       |
| `at`         | ISO string             | **服务端时刻**（不可伪造）                    |
| `action`     | `AuditAction`          | 见下                                          |
| `actorKind`  | `'session' \| 'token'` | 凭据通道                                      |
| `actorLabel` | string                 | 会话是「管理员会话」；令牌是它的 label        |
| `actorId`    | string \| null         | 管理员账号 id；令牌通道下是 `token_id`        |
| `target`     | string \| null         | 操作对象（扫描 id / 漏洞 id / 报告编号）      |
| `summary`    | string                 | 可直接展示的中文一句话                        |
| `ip`         | string \| null         | 来源 IP                                       |
| `userAgent`  | string \| null         | 来源 UA                                       |
| `detail`     | object \| null         | 动作相关的最小补充（**绝不含凭据**）          |

**`AuditAction`**（本轮覆盖的六个，都是**有后果**的操作）：

| 值                      | 何时写                                        |
| ----------------------- | --------------------------------------------- |
| `scan.create`           | `POST /scans` **成功**落库之后                |
| `scan.rerun`            | `POST /scans/{id}/rerun` 成功之后             |
| `scan.abort`            | `POST /scans/{id}/abort` 成功之后             |
| `finding.status_change` | `POST /findings/status` **真的改了**≥1 条之后 |
| `finding.assign`        | `POST /findings/assign` **真的改了**≥1 条之后（v0.15） |
| `member.create` / `member.update` / `member.delete` | 处理人名单的写操作（v0.15） |
| `report.download`       | `GET /reports/{id}/download` 产出文件时       |
| `report.delete`         | `DELETE /reports/{id}` 成功之后               |

**其余动作**（v0.21 补记 —— 此前这张表只列了 8 个，而代码里有 **31 个**。
下表补齐缺失的 23 个，分组说明它们各自的"事后要能回答什么"）：

| 值 | 何时写 | 事后要能回答 |
| --- | --- | --- |
| `scan.resume` | `POST /scans/{id}/resume` 成功之后 | 谁把一次失败的扫描接着跑了（v0.20）|
| `scan.archive` | 批量归档 / 取消归档之后 | 谁把它收进了归档（v0.13）|
| `scan.purge` | `DELETE /scans/{id}/purge` 之后 | **谁永久删除了它**（不可恢复）|
| `scan.attach` / `scan.attach_delete` | 题目附件上传 / 删除 | 谁把哪个文件交给了哪次扫描（v0.19）|
| `finding.delete` / `finding.restore` | 软删除 / 恢复之后 | 谁把它扔进回收站又捞了回来（v0.18）|
| `finding.purge` | 永久删除之后 | **谁销毁了这条证据**（不可恢复）|
| `finding.attach` / `finding.attach_delete` | 漏洞附件上传 / 删除 | 谁放了证据、谁撤了它（v0.18）|
| `member.update` / `member.delete` | 处理人名单的改 / 删 | 负责人名单变更（v0.15）|
| `ctf2.account_bind` / `ctf2.account_unbind` | 绑定 / 解绑 CTF2 账号 | **谁把这把外部钥匙放进系统、谁撤掉了它**（v0.20）|
| `ctf2.sync` | 同步赛事与题目之后 | 数据是什么时候从平台拉下来的 |
| `ctf2.solve_start` / `ctf2.solve_cancel` | 开始 / 取消解题 | 谁让它开始跑、谁叫停了 |
| `ctf2.submit` | 提交 flag 之后（**含本地拦下的**） | 谁往外提交了答案（有外部后果）|
| `ctf2.settings_update` | 改解题策略之后 | **谁把 `submitMode` 改成了 `auto`**（v0.20）|
| `ctf2.platform_draft` / `ctf2.platform_activate` / `ctf2.platform_use_builtin` / `ctf2.platform_delete` | 平台接入的四个动作 | 谁换掉了对外连接的平台（v0.25）|
| `user.create` | 新建用户之后 | 谁把谁加进了这个系统（v0.24）|
| `user.role_change` | 改角色之后 | **谁把谁变成了管理员**（v0.24）|
| `user.delete` | 删除用户之后 | 谁把谁移除了（v0.24）|

> **★ 这一组为什么值得单独点出**：它们是**对权限本身的操作**。
> "谁在什么时候把谁变成了管理员"是安全事件复盘的第一问 ——
> 与 `ctf2.settings_update`（谁把 submitMode 改成 auto）属于同一类
> "一次点击改变权限/后果边界"的动作。
>
> ⚠️ 与它们配套的还有一条**接口层面**的约束：用户管理的四个端点
> 要求**必须是登录会话**（`require_admin_session`），机器令牌一律拒绝。
> 理由见 §2.11。

> **★ 为什么 `ctf2.settings_update` 特别重要**：它记的是
> **"谁把 `submitMode` 从 `safe` 改成 `auto`"**。那个改动意味着
> 机器从此可以**无人确认地对外提交 flag** —— 而错误提交消耗平台额度。
> 这是本系统里少数几个"一次点击改变外部后果边界"的操作之一，
> 所以它必须留痕，且要能追到具体是谁在什么时候点的。

> **★ 留痕粒度的统一规则**（v0.15 定，适用于全部 31 个动作）：
> **时间线逐条、审计每次操作一行**。见下面 v0.15 那段。

> **v0.15 补记的粒度规则**（需求明确要求，值得写死）：
>
> * **时间线逐条**：批量改 3 条漏洞，`finding_timeline` 就有 3 行，
>   各带那一条自己的前后状态与操作人。这是"这条漏洞发生过什么"。
> * **审计每次操作一行**：上面那条批量只产生**1 行**审计，
>   `detail.changedIds` 里列出全部 id。这是"谁在什么时候做了一次什么操作"。
>
> 两者**不是替代关系**。审计若也逐条，一次 500 条的批量会产生 500 行，
> 把真正的操作序列淹掉 —— 而审计的价值恰恰在于每一次操作对应一条可追溯的动作。

> ⚠️ **失败的尝试也要留痕吗？** 本轮**不写**，但这是有意的取舍而不是遗漏：
> 被拒的请求（401/403/400）已经由 uvicorn 的 access log 覆盖了
> 「谁打了什么端点、结果码是什么」，而审计表要保留的年限远长于日志 ——
> 把每一次口令爆破尝试都永久存进去，等于给了攻击者一个**写满你磁盘**的入口。
> 要改成全记，先给审计表加来源维度的写入限流。

---

### 3.10 处理人名单（Members，v0.15 新增）

| 方法   | 路径                 | 鉴权      | 说明                     |
| ------ | -------------------- | --------- | ------------------------ |
| GET    | `/members`           | **read**  | 名单，`?includeInactive=` |
| POST   | `/members`           | **admin** | 新增                     |
| PATCH  | `/members/{id}`      | **admin** | 改名 / 改邮箱 / 停用启用 |
| DELETE | `/members/{id}`      | **admin** | 删除（**返回解除指派的条数**） |

```jsonc
// Member
{
  "id": "member_01J…",
  "name": "张三",
  "email": "zhangsan@example.com",   // 可空
  "active": true,
  "createdAt": "2026-09-25T08:00:00.000Z"
}

// DELETE /members/{id} → 200（**不是 204**）
{ "unassigned": 2 }
```

> **★ `DELETE` 为什么不用 `204`。**
>
> 删掉一个处理人会**解除他名下所有漏洞的指派**（`ON DELETE SET NULL`）。
> `204` 没有响应体，前端就拿不到那个条数 —— 于是"5 条漏洞的负责人
> 静默变成了未指派"，而用户看到的只是列表里少了一个人。
>
> 这不是推测：本功能实测时正是这么发生的（删完之后那几条的负责人无声消失，
> 页面上没有任何提示）。所以返回这个数字，让界面能说清代价。

> **★ 为什么"负责人"不是登录账号。**
>
> 本系统是**单管理员**的：`/auth/setup` 一次性初始化，之后没有注册入口，
> 也没有第二个账号。所以"指派给谁"要回答的**不是**"指派给哪个登录用户"，
> 而是"这条漏洞该由**谁**去修" —— 而那个人往往是客户端的人、运维、
> 或者同事，他们**根本不登录这个系统**。
>
> 把 `findings.assigneeId` 指向 `admin_accounts`，会让这个功能在单账号
> 系统里退化成一个只能指派给自己的笑话。所以它是一张**独立的业务名单**。
>
> **为什么用名单表而不是自由文本**：按负责人筛选是它的主要用途之一，
> 而自由文本会被错别字撕成好几组（「张三」/「张 三」/「zhangsan」），
> 于是"这个人手上有几条"就再也数不准了。所以 `name` 上有唯一约束，
> 重名返回 `400 VALIDATION_FAILED`。
>
> **停用而不是删除**：`PATCH {"active": false}` 之后不再出现在候选里，
> 但历史指派**仍然显示名字**。直接删除会让所有指派过他的漏洞突然显示成
> "未指派"，抹掉一段真实的协作记录。
>
> **读放宽到 `read`**：漏洞列表上"负责人"那一列读的就是这批数据。
> 若要求 `admin`，一个只有 `read` 的机器令牌拉列表时会解析不出负责人名字。

#### 3.10.1 指派（`POST /findings/assign`）

```jsonc
// 请求
{
  "ids": ["finding_01", "finding_02"],
  // ★ null 是**明确的操作**（取消指派），不是"没传"。
  //   所以它是必填字段、可以为 null —— 这样"漏传"与"故意清空"
  //   在请求里长得不一样，前端不会误清负责人。
  "assigneeId": "member_01J…"
}

// 响应（与 POST /findings/status 同形）
{
  "changed": 2,
  "skipped": 0,          // 已经是这个负责人的，幂等跳过且不写时间线
  "findings": [ /* FindingSummary[] */ ]
}
```

* `assigneeId` 指向不存在的处理人 → **`404`**（不是静默写入一个悬空 id，
  那样界面上那一列永远是空的，而没人知道为什么）。
* 幂等：重复指派同一个人返回 `changed: 0`，**且不写时间线** ——
  否则时间线里会出现「指派给张三 → 指派给张三」的噪音。
* 逐条留痕：批量改 3 条就有 3 行 `kind="assigned"` 的时间线。
  `assigned` 是**独立的一类**，不混进 `status_changed` ——
  混在一起以后就没法只筛出指派历史（比如复盘"这条漏洞换过几次手"）。

#### 3.10.2 `assigneeId` / `unassigned`（列表筛选）

`GET /findings?assigneeId=<id>` 与 `GET /findings?unassigned=true` 是
**两种不同的查询**，不能互相代替：

| 参数              | 含义                             |
| ----------------- | -------------------------------- |
| `assigneeId=<id>` | 这个人名下的                     |
| `unassigned=true` | **还没人认领的**（`IS NULL`）    |

用 `assigneeId=""` 表达"未指派"会让空串成为一个特殊值，而数据库里存的是
`NULL`。界面上"未指派"是一个筛选项，所以它有一个显式参数。

> `FindingSummary` 同时带 `assigneeId`（给筛选与后续操作）与
> `assigneeName`（给显示）。**不让前端拿 id 再去查名单** ——
> 那会让列表渲染依赖第二个请求，而中间那一刻负责人那一列是空的
> （看起来像"没人负责"）。名字在服务端批量装配（一次查询，不是 N+1）。

### 3.11 按漏洞类型分组（`GET /findings/groups`，v0.15 新增）

```jsonc
// GET /findings/groups?severity=&status=&category=&q=&scanId=&assigneeId=&unassigned=
[
  {
    "title": "调试接口未授权可读环境变量",
    // 组内**最高**严重度 —— 取平均或取第一条会把"这组里有个 critical"藏起来
    "severity": "high",
    "category": "misconfiguration",
    "instanceCount": 2,
    // 影响面：分组视图存在的理由就是它
    "targetCount": 2,
    "targets": ["a.example.com", "b.example.com"],
    // ★ 带 id 而不只是计数：展开时用它筛列表，或对整组做批量操作。
    //   不带的话展开就得再按标题查一次 —— 而标题**可以重复**
    //   （两条不同的漏洞恰好同名），用标题当条件会多带出不属于这组的条目。
    "findingIds": ["finding_01", "finding_02"],
    "byStatus": { "new": 1, "confirmed": 1 }
  }
]
```

> **★ 为什么按 `title` 分组，而不是 `category` 或 id。**
>
> 同一个漏洞会出现在**多个目标**上（截图里的场景）：那是**两条独立记录**
> —— 不同目标各自的实例，处置状态也各自独立 —— 但它们显然是**同一类问题**。
>
> * 按 `category` 太粗：`misconfiguration` 下有几十种不同的东西，折叠等于没分。
> * 按 `source_ref` / id 太细：那是外部工具在单次运行内的编号，每次扫描都重来。
> * **标题才是"人眼里这是同一个问题"的那个粒度。**
>
> 这与扫描对比模块（`scans.service.compare_scans`）用的是**同一个判据**，
> 两处必须一致 —— 否则会出现"对比说这条消失了、分组视图里还在"这种自相矛盾。
>
> **筛选参数与 `/findings` 完全一致**：分组视图不是另一套数据，
> 它是同一批数据换个切法。参数不一致的话，用户切过去会看到不同的范围，
> 而他会以为筛选失效了。
>
> **排序**：严重度降序，同档按**影响面**（影响目标多的先看）降序，
> 再按标题。影响面是分组视图存在的理由，所以它是第二个排序键。
>
> **聚合在 SQL 里做**，不是取回全部再 `groupby`：`findings` 会到几万行，
> 而分组视图恰恰是**最需要看全貌**的场景，不能让它是最慢的那个。

### 3.12 统计跟随筛选（v0.18）

`GET /findings/stats` 现在收**与 `GET /findings` 完全同一套**筛选参数：
`severity` / `status` / `category` / `q` / `scanId` / `assigneeId` /
`unassigned` / `slaState` / `deleted`，外加一个开关 `facets`。

#### 3.12.1 为什么要这么做

在此之前 `stats` 只认 `scanId`，而它**自己另写了一遍筛选逻辑**。当时只有
一个共同参数，看起来"没重复多少"—— 而那正是最危险的时候：再加第二个
筛选参数，两份口径就会漂移，症状是**列表筛出来 3 条、角标写着 7 条**。
用户无法解释这个差值，只能不再相信角标，那这个功能就白做了。

现在两者编译筛选条件走**同一个** `_filter_conditions`，
"列表长度 ≠ stats.total"在结构上不可能出现（有测试盯着这个等式）。

#### 3.12.2 `facets` —— 角标要能当"切换目标"用

这是本功能最容易做错的一处。角标有两种可能的含义：

| 取值 | 含义 | 界面上 0 的 chip |
| --- | --- | --- |
| `facets=false`（默认） | 所有计数都在**当前筛选结果**之内 | 该禁用（结果里确实没有） |
| `facets=true` | 算某一维度时**忽略那一维度自己的条件** | 必须**仍可点** |

为什么需要后者：用户筛了「高危」，想改看「中危」时，若中危角标是 0
且按钮被禁用，他**根本切不过去** —— 必须先取消高危再选中危。
这不是"数字准不准"的问题，而是筛选器还能不能用的问题。

两条必须写死的边界：

1. **`total` 永远是"筛出来几条"**，不受 `facets` 影响。facet 只改角标，
   不改结果集。
2. **只忽略"自己那一维"**，其余条件照旧生效。搜索"登录"时角标就该只
   统计命中的那些 —— 否则角标会指着一批被搜索排除掉的条目，
   用户点进去发现数量对不上。

`facets=false`（默认）是为了让 Overview 与既有调用方**行为完全不变**：
它们要的是全量口径，不该因为这次改动而变。

> **★ 前端 Findings 页实际用的是本地 facet 计数，不是这个参数（v0.18 记）。**
>
> 两处原因，第二条是决定性的：
>
> 1. **界面筛选是多选的**（`severities: Set`，可同时勾「高危 + 中危」）。
>    后端的 `facets` 收逗号分隔的多值（见下），所以这条**不再**是障碍 ——
>    但走后端意味着每敲一个搜索字符就发一次请求。
> 2. **角标与表格必须必然一致**。走后端会出现"表格已经 3 条、
>    角标还是 7"的短暂错位（两个请求的到达顺序不保证），
>    而从已加载的列表算（`lib/finding-query.facetCounts`）零延迟，
>    且同一个数组算出来，不可能对不上。
>
> 所以 `?facets=true` 是给**服务端渲染或单值客户端**用的能力，
> 而 Findings 页那份实现在 `lib/finding-query.ts` —— 它也是前端
> **唯一**的计数实现（组件里不要再写 `findings.filter(...).length`）。
>
> 因此 Findings 页也**不再拉** `GET /findings/stats` 了：那个字段成了
> 没人读的死数据，而它每次加载还多发一个请求。`/findings/stats`
> 仍由 `/overview` 在服务端使用（§3.4.1 的跨页一致性讲的是那条路径）。

> **★ `severity` / `status` / `category` 支持逗号分隔的多值（v0.18）。**
>
> `?severity=high,low`。这是**向后兼容的加法**：单值行为不变，
> 而逗号在原取值集合里本来也不可能出现（枚举值无逗号），
> 所以不存在"同一个请求两种解释"。
>
> 它由**导出**逼出来：界面的筛选是多选的，而 `/findings/export`
> 必须能表达同一个筛选 —— 否则用户勾了两个严重度再导出，
> 要么报错、要么**悄悄只导出一个**，而他会把那份表发出去。
>
> ⚠️ 校验必须**逐个取值**做（`high,nonsense` → 400 并指出 `nonsense`）。
> 只校验整串的话，非法值会照样拼成 `IN ('nonsense')` 并静默筛出 0 条 ——
> 而"筛出 0 条"在界面上看起来像"确实没有"。

> **`byStatus` / `byCategory` 仍返回全部档位（含 0）**，而 `bySeverity`
> 仍是 `Partial`（为 0 的键不存在）—— 这个不对称是 v0.6 定下的，
> 本次不变。理由见 §3.4.2。

### 3.13 漏洞软删除（v0.18）

与**扫描模块完全同一套语义**（§3.3）：列表默认排除、回收站视图、
撤销窗口、永久删除要另外确认。

| 端点 | 说明 |
| --- | --- |
| `POST /findings/batch/delete` | 打 `deletedAt` 标记，**数据一条不丢** |
| `POST /findings/batch/restore` | 清除标记。**不动 `status`** |
| `DELETE /findings/{id}/purge` | **物理删除**。仅回收站内，且被报告引用时拒绝 |
| `GET /findings?deleted=true` | 只看回收站 |

响应与扫描模块**同形**：

```jsonc
{ "changed": 2, "skipped": 1, "findings": [ /* 仅含真正改了的 */ ] }
```

> **★ 软删除是默认、且必进统计排除。**
>
> `findings.deleted_at IS NULL` 是**所有查询的默认条件**（列表、
> 统计、分组、导出、Overview 全部）。漏一处就会出现"删了还在计数"，
> 而那是用户最不能接受的一种不一致。所以它写在 `_filter_conditions`
> 里 —— 那是唯一一处编译筛选条件的地方。
>
> **软删除不检查报告引用，这是有意的。** 软删除**不破坏**报告：
> 报告冻结的是 finding id 与当时的快照，被软删的漏洞仍然存在、
> 仍然能渲染。拦截在这里会让"这条是误报，我不想再看到它"变成做不到的
> 事，而那个需求是真实的。引用检查放在 `purge`（那才是真正会破坏的一步）。
>
> **恢复不动处置状态。** 漏洞在回收站里期间状态没有变过（谁也不会去改
> 一条已删的），所以恢复要还原成删除前的样子 —— 顺手改成 `new` 会抹掉
> 分诊成果，而那正是这个模块最值钱的东西。
>
> **永久删除有三条闸**：① 必须先软删除（`409 FINDING_NOT_IN_TRASH`）；
> ② 被报告引用时拒绝（`409 FINDING_REFERENCED_BY_REPORT`，并说出是哪几份）；
> ③ **附件要一起清掉** —— `ON DELETE CASCADE` 只删数据库行，
> **不会删磁盘上的文件**。
>
> **软删除不动附件。** 进回收站不是"销毁证据"，那是永久删除才做的事。
> 删掉附件会让"恢复"变得不完整（记录回来了、证据没了）。

新增审计动作：`finding.delete` / `finding.restore` / `finding.purge`。
软删除**也要留痕** —— 它把条目从默认视图里移走，事后必须能回答
"那条漏洞怎么不见了"（与 `scan.archive` 同一条理由）。

### 3.14 CSV 导出（`GET /findings/export`，v0.18）

```
GET /findings/export?severity=&status=&category=&q=&scanId=&assigneeId=&unassigned=&slaState=&deleted=
→ 200 text/csv; charset=utf-8
   Content-Disposition: attachment; filename="suvex-findings-20260925.csv"
```

> **★ 筛选参数与 `/findings` 完全一致，且走同一个 `list_findings`。**
>
> 这是这个端点最容易被做错的地方：如果导出忽略筛选，用户筛出 3 条高危、
> 点导出却拿到全部 200 条，而他会把那份表**发出去**。
> **错误的数据比没有数据更糟。**
>
> **为什么走后端而不是像扫描列表那样在前端生成。**
> `lib/scan-export.ts` 的选择是前端生成（"数据全在列表里了"）。
> 漏洞这里不成立：列表返回的是**摘要**（不含 `detail`），
> 而导出的价值有一半在"这条问题到底是什么"。前端要生成就得逐条
> 再取详情（N 个请求），后端一次查询就能给全。
>
> **带 UTF-8 BOM（`\ufeff`）。** 不带的话，简体中文 Windows 上的
> Excel 会按本地编码读，中文列名变成乱码 —— 而"导出给别人看"正是
> 这个功能的用途，打开是乱码等于没做。
>
> **值用枚举，不新造中文标签表。** 后端至今没有任何一处把
> `severity` / `status` 翻成中文（`GET /findings` 回的就是 `"high"`），
> 前端用 `SEVERITY_META` 自己渲染。在导出这里新造一份中文表就多了
> 一处会漂移的口径。列名保留中文（那是给人读表头的）。
>
> **不含 `detail.evidence`。** 证据是原始响应片段（可能几百行、
> 含换行与逗号），放进 CSV 会让每一行膨胀且极难阅读。
> CSV 回答的是"有哪些问题、各自多重、归谁、什么时候到期"。
>
> **文件名是 ASCII。** `Content-Disposition` 里放中文要按 RFC 5987
> 编码，不编码时某些客户端会乱码或截断。不值得为文件名引入
> 编码复杂度 —— 日期已经够区分。

### 3.15 漏洞附件（v0.18）

```jsonc
// POST /findings/{id}/attachments   ← ★ multipart/form-data，字段名 files（可多个）
// 响应 201
[
  {
    "id": "att_01J…",
    "findingId": "finding_01J…",
    "filename": "proof.png",       // 用户上传的原名（已消毒）
    "contentType": "image/png",
    "sizeBytes": 20480,
    "uploadedAt": "2026-09-25T16:00:00Z",
    "uploadedBy": "user_01J…"
  }
]
```

| 端点 | 说明 |
| --- | --- |
| `GET /findings/{id}/attachments` | 列表，**按上传时间正序** |
| `POST /findings/{id}/attachments` | 上传（可多个），上限见下 |
| `GET /findings/{id}/attachments/{aid}/content` | 下载内容 |
| `DELETE /findings/{id}/attachments/{aid}` | 删除（文件 + 元信息） |

> **★ 存储路径完全由服务端从 id 生成，与上传的文件名无关。**
>
> ```
> stored_path = <root>/<finding_id>/<attachment_id><ext>
> filename    = 用户上传的原名（只用于显示与下载）
> ```
>
> 这是本节最要紧的一条安全决定。用上传文件名拼路径会同时打开两个洞：
>
> 1. **路径穿越**：`../../etc/passwd` 会让写入落到附件根之外。
> 2. **覆写**：两个人上传 `screenshot.png`，后者**静默覆盖**前者 ——
>    而"证据被另一个人的截图替换了"是不可接受的。
>
> 从 id 生成把两个洞一起关掉。扩展名走**白名单**（`ALLOWED_EXTENSIONS`）
> 而不是黑名单 —— 黑名单永远漏（`.php5`、`.phtml`…），而这里的目标不是
> "拦住危险文件"（附件不会被服务端执行），而是"确保下载时类型可信"。
>
> **响应不含 `storedPath`。** 存储路径是服务端内部的事，
> 客户端没有任何用途需要知道它（要下载就走 content 端点）。
>
> **顺序规则：写的时候先文件后库，删的时候先库后文件。**
> 两个方向是同一条原则：**让用户可见的状态保持一致，宁可留孤儿文件**。
> 反过来的话，`store` 会留下"行指向不存在的文件"（点下载 500），
> `delete` 会留下"行还在、文件没了"（看起来像附件是空的）。
>
> **上限**：单文件 `SUPEX_ATTACHMENT_MAX_BYTES`（默认 10 MiB），
> 一次请求 `SUPEX_ATTACHMENT_MAX_COUNT`（默认 20）。
> ⚠️ 大小按**真实字节数**判，不是按 `Content-Length` ——
> 后者可以被客户端撒谎（分块传输），真正的闸门在服务端。
>
> **下载强制 `Content-Disposition: attachment`。** 这一条对
> `image/svg+xml` 尤其要紧：SVG 可以内嵌 `<script>`，
> 内联渲染等于让一个**上传的文件**在本站的原点上执行。
>
> **附件不能跨漏洞读。** `GET /findings/A/attachments/<B 的附件>` 返回 404。
> 现在只有单管理员、谈不上越权，但**接口的形状不该依赖这一点**。
>
> 新增审计动作：`finding.attach` / `finding.attach_delete`。
> 上传**把数据带进了系统**，而附件可能含敏感信息（内网截图、凭据抓包），
> "谁在什么时候往这条记录里放了什么"是事后必须能回答的问题。

#### 3.15.1 ⚠️ multipart 与 CSRF 防线（实现约束，前端必须知道）

`app/auth/deps.py` 的 CSRF 防线**拒绝一切表单类 Content-Type 的写请求**：

> 跨站 HTML 表单只能发出 `application/x-www-form-urlencoded`、
> `multipart/form-data`、`text/plain` 这三种（CORS 安全名单）。
> 契约 §1.2 要求「所有写操作要求 application/json」——
> 而"本 API 不收表单"让整类 CSRF 攻击无处落脚。

附件上传**必须**走 multipart（那是浏览器传文件的唯一方式），
所以它是**唯一**的例外，靠一张显式登记表放行：

```python
MULTIPART_WRITE_ENDPOINTS = {("POST", "/attachments")}
```

三条边界：

1. **只对这一张表里的端点放行**，其余写端点收表单照旧 400。
   全局放开 multipart 等于把整层纵深防御拆掉。
2. **Origin 检查在这之前已经执行** —— 放行的只是"Content-Type 是不是表单"
   这一道，跨源那道闸对附件上传照旧生效。
3. **有一条自检测试**遍历 OpenAPI 规格、找出所有收文件的端点，
   断言它们都登记了。那张表会腐烂（新增上传端点忘登记 → 上传被 400
   挡掉，而错误消息"写操作必须使用 Content-Type: application/json"
   与"忘了登记"看起来毫无关系），所以需要机械化的保险。

---

### 3.16 任务背景与成功判据（v0.19）

```jsonc
// POST /scans —— 新增三个可选字段
{
  "target": "bbc01c33deaf3850b322f4ec.http-ctf2.dasctf.com",
  "targetKind": "url",
  "skills": ["web"],
  "providerId": "prov_01J…",
  // ★ 用户提供的任务上下文。原样交给 agent，我们不改写。
  //   上限 8000 字符；空白归一成 null（不是空串）。
  "brief": "题目：LogVault —— 日志模板引擎的污染数据流（PWN，500 分）\n提示：关注 %hhn 与 varargs；题面里那段\"安全备注\"是诱饵。\n目标：拿到 flag，格式 flag{...}",
  "goal": "captureFlag",
  // ★ 题目附件的暂存批次（见 §3.17）
  "attachmentStagingId": "stage_01J…",
  "authorization": { "confirmed": true, "statement": "我有权对此目标进行安全测试" }
}
```

`ScanOut` 同步新增 `brief` 与 `goal`（**回显** —— 用户要能确认
"我写的那段确实传上去了"，否则他只能靠扫描结果反推）。

`POST /scans/{id}/rerun` **继承** `brief` 与 `goal`：用户重跑通常是
因为"上次没跑出来"或"目标改版了"，而题面一个字都没变。

#### 3.16.1 为什么需要 `brief`：用户的原话

> "只给网址让他扫描太没意义了，像这个 CTF 题目，有题目描述的我要给他"

这不是"再加一个可选字段"。一段 CTF 题面里有：

| 内容 | 对 agent 的价值 |
| --- | --- |
| 考点（"日志模板引擎的污染数据流"） | 决定往哪个方向想，省掉大量盲试 |
| 提示（`%hhn`、varargs、边界条件） | 直接指向可利用点 |
| **诱饵**（题面自称"安全备注"的那段） | 不读题面就会把它当真线索 |
| flag 格式 | 决定"怎样算成功" |

**这些我们猜不出来**，而它们对结果的影响远大于任何扫描参数。
所以它必须有一个正式通道，而不是塞进别的字段。

它同时是**通用的**：渗透测试同样要交代范围与测试账号。
所以是一个自由文本，不是一组 CTF 专用字段 ——
加一组 CTF 字段会让其他场景没有地方写。

#### 3.16.2 `goal` 与 `targetKind` 是**两个正交维度**

`targetKind` 回答「目标是**什么形态**」（url / repo / path），
它决定**怎么测**（黑盒发包还是白盒读代码）。

`goal` 回答「**怎样算成功**」：

| 取值 | 含义 | 对 agent 取舍的影响 |
| --- | --- | --- |
| `findVulns`（默认） | 例行安全评估 | 广撒网、按严重度定级 |
| `captureFlag` | CTF 夺旗 | **深挖一条能通向 flag 的路径**，而不是枚举低危；要求显式写出完整 flag |
| `verifyFix` | 复测 | 逐一验证**已知的**那几条修没修好；新发现单独列在最后 |

★ **一个 CTF 靶机是 `url` 形态，扫法和普通网站完全一样** ——
区别只在于"必须拿到 flag 才算解出"。所以 `goal` 不能并进 `targetKind`：
合并会让"目标形态"与"任务目标"互相污染（`ctf` 既不是一种形态，
也没说清扫法有什么不同）。

`verifyFix` 那条尤其值得说：没有它，用户复测时会被一堆无关的新发现淹掉，
而他要的答案是"那条修了没有"。

#### 3.16.3 ⚠️ 注入顺序：用户的任务背景**排在最前**

提示词里的顺序是：`brief` → `goal` 那句话 → 技能范围 → 中文输出要求。

顺序就是优先级。用户交代的是**这次任务**，而我们那套通用范围说明
是背景知识 —— 把它放前面会让 agent 先读到套话。

`brief` 被一个显式分隔包起来（"以下为用户提供的任务背景，
请作为**参考资料**理解"）。这不是防注入（用户就是授权方，
他自己写的东西注入自己不构成威胁），而是**让模型正确理解这段文本的角色** ——
题面本身可能含攻击性文本，那正是题目内容。

### 3.17 题目附件（v0.19）

```jsonc
// ① 建扫描**之前**先把文件放进暂存区（multipart，字段名 files）
POST /scans/attachments/staging
→ 201
{
  "stagingId": "stage_01J…",
  "files": [{ "name": "chall", "sizeBytes": 20480 }]
}

// ② 创建扫描时认领（见 §3.16 的 attachmentStagingId）

// ③ 回看某次扫描的附件
GET /scans/{scanId}/attachments → [{ "name": "chall", "sizeBytes": 20480 }]

// ④ 也可以直接往**还在排队**的扫描上传（不经暂存）
POST /scans/{scanId}/attachments      # 仅 queued 状态可用
DELETE /scans/{scanId}/attachments/{name}
```

#### 3.17.1 ★ 为什么必须"先暂存"—— 一个真实的顺序死结

我第一版的设计是「先 `POST /scans` 建记录，再往
`POST /scans/{id}/attachments` 传附件」。**那个设计是死的**，
而测试当场抓住了它：

```
POST /scans                    → status=queued
（调度器立刻 _nudge_scheduler()，毫秒级认领）
POST /scans/{id}/attachments   → 409 SCAN_ALREADY_STARTED
```

创建与启动之间的窗口是**毫秒级**的，所以"建完再传"在实践中
永远来不及 —— 于是附件功能等于不存在，**而它看起来完全正常**
（端点都在、单独测每一个都过）。

修法是把附件放在创建**之前**，这也更符合用户的实际动作顺序：
向导里先填目标、贴题面、**选附件**，最后才点「启动扫描」。

认领是**移动**而不是复制 —— 这让"一次暂存只能被认领一次"
由文件系统本身表达，不需要额外的状态标记。

**暂存 id 失效不能让创建失败**：附件丢了是次要问题，
"扫描建不出来"才是主要问题。所以认领不存在的批次返回空列表。

未被认领的暂存批次由 `sweep_staging` 按 2 小时清掉
（它挂在 `sweep_retention` 里跑）。

> 📌 **顺带修正一条既有的错误说法**：我一度在注释里写
> "`sweep_retention` 会按保留期清 `scan_root/<scan_id>`"，
> 而实测下来**它根本不删任何目录** —— 它只改数据库
> （打 `archived_at`、删回收站里过期的行）。
> 磁盘上的 `_scan_runs/<scan_id>/` 一直留着。
>
> 那意味着一个**既有的缺口**：扫描产物目录只增不减。
> 它属于运维层的问题，但值得写下 —— 因为它此前被那段错误的
> 注释"解释掉"了，看起来像已经处理过。

#### 3.17.2 与漏洞附件（§3.15）是**两套**规则

看起来像重复，但两边的取舍是相反的：

| | 漏洞附件 | 题目附件 |
| --- | --- | --- |
| 是什么 | 处置**证据**（截图、复测报告） | 题目**输入**（二进制、源码、字典） |
| 扩展名 | 白名单 | **不限制**（`.bin`、无扩展名都正常） |
| 谁读它 | 人在浏览器里看 | **机器**在沙箱里分析 |
| 文件名 | 只用于显示（存储名用 id） | **必须保留原名**（agent 靠名字认东西） |
| 大小上限 | 10 MiB | 50 MiB |

合用一个模块的话白名单会被迫取**两者中更宽的那个** ——
于是"处置证据"那边也收任意二进制，而那个白名单本来是为了
"下载时类型可信"。

那题目附件的路径安全靠什么？**文件名消毒**（去掉路径分隔符与控制字符）
+ 落点校验 + 同名时加 `-2` 后缀（**不覆盖**：覆盖掉的可能是这次分析
唯一正确的那个附件）。

⚠️ 题目附件**会被送进沙箱**，这是功能本身的要求
（把不信任的样本交给分析工具）。真实边界在沙箱隔离上
（`docs/security-checklist.md` 的 F-01/F-02），不在文件校验上。

#### 3.17.3 ⚠️ 这是 multipart 的第三个端点 —— 登记表已改成精确匹配

上传端点是 CSRF 防线的**显式例外**，靠 `MULTIPART_WRITE_ENDPOINTS`
放行。加暂存端点时它**真的踩坑了**：

那张表当时用**后缀匹配**（`endswith("/attachments")`），
而新路径 `/scans/attachments/staging` **不以 `/attachments` 结尾** ——
于是它没被放行，上传被 400 挡掉（"写操作必须使用 Content-Type:
application/json"），而那条消息与"忘了登记"看起来毫无关系。

已改成**精确匹配路由模板**，并记下一个反直觉的细节：

> ⚠️ 登记表里**不带 `/api/v1` 前缀**。前缀是
> `include_router(prefix=...)` 挂载时加的，而
> `request.scope["route"].path` 是子路由自己的模板
> （实测：`POST /api/v1/findings/<id>/attachments` 匹配到的是
> `/findings/{findingId}/attachments`）。按"完整对外路径"写会
> **一条都匹配不上**，症状是所有上传端点突然都 400。

### 3.18 题解报告（`GET /scans/{id}/writeup`，v0.20 补记）

CTF 夺旗任务的产物**不是**漏洞报告。这一节把它与 §3.5 的报告彻底分开。

```jsonc
// 200
{
  "markdown": "# …\n",        // 完整题解正文
  "filename": "题解-….md",     // 前端另存为时用，服务端定名（避免两边各起一套）
}
```

**为什么是服务端拼，而不是让 agent 直接写题解**

做不到。执行后端的报告标题（`# Security Penetration Test Report`）与**所有分节
标题都是写死的**，`finish_scan` 也只有那四个渗透口径的字段。所以我们把已有的
产物（任务背景 / flag / agent 的推理与工具调用 / 报告正文）**重新分节**成题解形状，
而不是伪造一个框架不支持的输出格式。

**三个前置条件，任一不满足都是 `409 WRITEUP_UNAVAILABLE`：**

| 条件 | 为什么要单独说一句人话 |
| --- | --- |
| `goal == captureFlag` | 渗透评估的产物是漏洞报告，不是解题过程 —— 这句话本身要能解释"为什么你没有题解" |
| 扫描已到终态 | 题解要等它跑完才有 |
| 报告文件确实在 | 产物目录可能被手工清理过，拼不出题解 |

> **★ 一个错误码而不是三个。** 补救动作都不是在界面上能做的（换判据重跑、
> 或者等它跑完），真正要区分的是**那句话**——而 `message` 本来就是写给用户
> 看的中文原话。拆三个码只会让前端写三段其实一样的分支。

> **⚠️ 报告只读一遍，然后传进去。** 不让拼装函数自己再读一次：两次读之间文件
> 可能被清理，于是"判断有没有"与"实际用来拼的那份"可以不是同一个东西 ——
> 而症状是返回一份**只有标题和 flag 的空题解**，看起来还像成功了。

#### 3.18.1 `Scan.flag` 是怎么来的（§2.1）

**它不是猜出来的，是从报告里按标记行取的。** 执行协议要求夺旗任务在报告里写出
一行 `FLAG: <值>`（没拿到就写 `FLAG: NOT_FOUND`），提取只认这一行。

> **⚠️ 占位符 `flag{...}` 必须被拒掉。** 一次失败的扫描曾被回填成 `flag{...}` ——
> 题面/报告里用省略号描述格式是极常见的写法，而它**长得就像个 flag**。
> 于是界面上出现了一面"解出来的旗"，值是三个点。现在判据是"花括号里是不是
> 全是标点"，命中即判为占位符、不写入。
>
> **并且宁可为空也不猜**：同一份报告里出现多个候选形状时，这一栏留 `null`。
> 一个错的 flag 比没有 flag 更糟 —— 前者会让人停止怀疑。

---

### 3.19 CTF2 平台对接（v0.21 补记）

> **★ 这一节是一次事后补齐，不是新决策。**
>
> 整个 `app/ctf2/` 模块（25 个端点、5 个枚举、24 个出入参、8 个迁移）
> 此前在契约里**零覆盖** —— 而契约是本项目的「唯一事实来源」，
> 前端按它接入。这意味着有一大块对外接口从未被写下来过。
>
> 补记的原因是 2026-10-10 做交付评估时量出来的：`TargetKind 契约缺
> artifact` 只是校验脚本恰好抓到的那一处，实际漂移远比它大。
> 详细的漂移清单与量化见变更表 v0.21。

**它解决什么**：CTF2 是一个 CTF 平台。这个模块让 Suvex 能**绑定平台
账号 → 同步赛事与题目 → 起题目环境 → 派 agent 解题 → 把 flag 提交回平台**。
与 §3.3 的扫描是两个并列的入口（见 §3.16.2 关于"判据"与"目标形态"的区分）。

#### 端点总表

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| GET | `/ctf2/accounts` | read | 账号绑定列表。**只返回掩码，没有 token** |
| POST | `/ctf2/accounts` | **admin** | 绑定账号（`201`）|
| DELETE | `/ctf2/accounts/{accountId}` | **admin** | 解绑（`204`）|
| GET | `/ctf2/settings` | **admin** | 解题策略配置 |
| PUT | `/ctf2/settings` | **admin** | 改配置 |
| GET | `/ctf2/competitions/remote` | **scan** | **平台侧**的赛事列表（不落库）|
| POST | `/ctf2/challenges/pull` | **scan** | 从平台拉一道题入库 |
| POST | `/ctf2/sync` | **scan** | 全量同步赛事与题目 |
| GET | `/ctf2/competitions` | read | 本地赛事列表 |
| GET | `/ctf2/challenges` | read | 本地题目列表 |
| GET | `/ctf2/challenges/{challengeId}` | read | 题目详情 |
| GET | `/ctf2/snapshot` | read | 一次拿到账号 + 赛事 + 题目 |
| GET | `/ctf2/queue` | read | 解题队列状态 |
| POST | `/ctf2/challenges/{challengeId}/solve` | **scan** | 开始解一道题 |
| PUT | `/ctf2/challenges/{challengeId}/priority` | **scan** | 改优先级 |
| POST | `/ctf2/priorities/by-score` | **scan** | 按分值批量排优先级 |
| POST | `/ctf2/challenges/{challengeId}/submit` | **scan** | 提交 flag |
| POST | `/ctf2/challenges/{challengeId}/cancel` | **scan** | 取消解题 |
| DELETE | `/ctf2/challenges/{challengeId}/environment` | **scan** | 销毁题目环境（`204`）|
| GET | `/ctf2/platforms` | read | 已接入的平台 |
| GET | `/ctf2/platforms/{platformId}` | read | 单个平台详情 |
| POST | `/ctf2/platforms/draft` | **admin** | 提交一个平台接入草稿（`202`）|
| POST | `/ctf2/platforms/{platformId}/activate` | **admin** | 启用某个平台 |
| POST | `/ctf2/platforms/use-builtin` | **admin** | 切回内置的 CTF2（`204`）|
| DELETE | `/ctf2/platforms/{platformId}` | **admin** | 删除平台（`204`）|
| GET | `/ctf2/challenges/{challengeId}/writeup` | read | **题解（Writeup）**（v0.36）|
| GET | `/ctf2/usage` | read | **token 消耗统计**（v0.36，可按题/赛事/时间筛）|
| GET | `/ctf2/challenges/{challengeId}/usage` | read | 单道题的逐次消耗明细（v0.36）|

> **鉴权分级的理由**：读操作是 `read`；**会对外发请求或消耗外部额度**的
> 是 `scan`（同步、拉题、解题、提交）；**改系统级配置与凭据**的是 `admin`
> （绑定账号、改提交策略、接入平台）。
>
> 这个分法与 §5 的思路一致：**判据是"这个操作会不会产生外部后果"**，
> 而不是"它看起来危不危险"。

#### 3.19.1 ★ `submitMode`：默认 `safe`，不是"更安全所以更好"

`PUT /ctf2/settings` 的 `submitMode` 决定机器能否**无人确认**地对外提交 flag。

| 取值 | 行为 |
| --- | --- |
| `safe`（**默认**） | agent 解出候选 flag 后停在 `candidate_ready`，**等人确认**才提交 |
| `auto` | 自动提交，不等人 |

> **为什么默认是 `safe`**：flag 提交有**不可撤销的外部后果** ——
> 错误提交消耗平台额度，而额度是有限的（每道题几次），
> 且一个 LLM 解出来的东西未必对。自动提交等于把"扣额度的动作"
> 交给一个会犯错的东西去做。
>
> 所以 `safe` 是默认，`auto` 要用户**显式打开**，而且它也可以
> 按题设置。这不是"保守"，是**承认自动化的边界**：
> "自动"不等于"无需人工确认"，前者是效率，后者是安全边界。

#### 3.19.2 ★ 提交前的前置校验

`POST /ctf2/challenges/{challengeId}/submit` 在真正发包**之前**会拦下几类
情况，命中时返回的 `status` 是 **`rejected`**（本地拦下，不消耗额度）：

| 条件 | 为什么拦在本地 |
| --- | --- |
| 超出该题错误提交上限（`remainingWrongSubmissions <= 0`） | 再发就是白扣一次额度 |
| 在 `safe` 模式下无人确认 | 见 §3.19.1 |
| flag 形状明显不可能是答案 | 省一次外部请求 |

> **⚠️ 它**不再**要求"答案长得像 flag"。** 这条在 v0.21 改过：
> 判据从"像不像 flag"改成"**是不是明显不可能**"。理由是 CTF 里有大量
> 非 `flag{...}` 形状的答案（一段口令、一个文件名、一串数字），
> 而按形状拒绝会把正确答案挡在门外 —— 那是**假阴性**，
> 而假阴性在这个场景里比假阳性贵得多（用户会以为 agent 没解出来）。
>
> 名字里的 `flag` 是历史遗留，它现在的处理对象是**任意答案**。

#### 3.19.3 平台接入（`/ctf2/platforms/*`，v0.25）

内置的 CTF2 排在 `GET /ctf2/platforms` 的**第一个** —— 它是默认的那个。
用户也可以接入别的平台，流程是"提草稿 → 系统生成适配器 → 启用"：

| 端点 | 语义 |
| --- | --- |
| `POST /platforms/draft` | 提交草稿（`202` —— 生成是异步的，见 `isBuilding`）|
| `POST /platforms/{id}/activate` | 启用；**同一时刻只有一个 `active`** |
| `POST /platforms/use-builtin` | 切回内置 CTF2（`204`）|
| `DELETE /platforms/{id}` | 删除 |

> **`202` 而不是 `201`**：草稿提交后系统要**生成**适配器（这是异步的，
> 可能要几十秒）。`201` 的语义是"资源已创建"，而那一刻它还在建。
> `202 Accepted` 才是诚实的：**受理了，还没做完**。
> 前端据此显示 `isBuilding`，而不是假装已经好了。

#### 3.19.4 与扫描模块的关系

**每一道 CTF2 解题任务都建一条真实的 `Scan`**（`goal = captureFlag`）。

> 为什么不是另一套表：`activity_events.scan_id` 是**非空外键** ——
> 事件流必须挂在某次扫描上。所以 CTF2 借扫描的壳，这样：
>
> * 事件流（§4）直接可用，不必为 CTF2 再写一套实时通道；
> * `scans.flag`（§3.18.1）直接可用，不必再存一遍；
> * 活动页、报告、题解这些既有能力全部复用。
>
> **代价要说清**：扫描列表里会出现 CTF2 的任务，而它们的目标
> 可能是一个平台内部的环境地址。`Ctf2Challenge.scanId` 是这条
> 关联的入口 —— 反过来，从扫描找不到"它是哪道题"（这是单向的）。
>
> ⚠️ 这类"壳扫描"**不能续跑**：它的"上次会话"根本不存在
> （strix 从没跑过它），续跑会走到一句与事实相反的"会话文件已不在"。
> 所以 `resume_scan` 会先拦下它，返回 `409 SCAN_IS_CTF2_SHELL`。

#### 3.19.5 ★ CTF2 的题解**不走** scans 的题解端点（v0.36 修）

**背景是一个真实的误导**：CTF2 的壳扫描在扫描列表里长得像一次普通扫描，
于是它也出现了「题解」按钮。点开得到：

> 这次扫描没有留下报告文件（产物目录可能被手工清理过），拼不出题解。

**那句话在误导用户。** 真相不是"文件被清理了"，而是
**CTF2 解题根本不走 strix** —— 它由 `app/ctf2/runner.py` 自己的
循环驱动，从不产生 `penetration_test_report.md`。所以 `scans/writeup.py`
按设计找不到东西，**永远**找不到。

修法：`GET /scans/{scanId}/writeup` 认出 `origin = "ctf2"` 的壳，
返回一句**指向正确入口**的话，而不是让用户去翻产物目录：

```
409 WRITEUP_UNAVAILABLE
"这是 CTF2 的解题任务，题解在题目卡片上（GET /ctf2/challenges/{id}/writeup），
 不走扫描报告。"
```

> **为什么返回 409 而不是内部转发**：转发会让"扫描的题解"这个端点
> 有两种完全不同的数据来源，而调用方无法预知自己拿到的是哪一种。
> 让错误信息指路更诚实 —— 与 §3.19.4 里 `SCAN_IS_CTF2_SHELL`
> 处理"壳不能续跑"是同一个手法（认出特例、明说原因、指对入口）。

**CTF2 自己的题解从哪来**：`SolveOutcome.transcript`（每轮的命令 + 输出摘要）
+ 题目信息 + flag + 轮数/耗时/token 统计，由 `ctf2/writeup.py` 拼装。

> **★ 题解里的 flag 不脱敏 —— 与"方法卡"（`ctf2_solve_notes`）恰好相反。**
>
> 这个差别必须写下来，否则后来者会"顺手统一"而把其中一边改坏：
>
> | | 方法卡（`ctf2_solve_notes`） | 题解（`ctf2_writeups`） |
> | --- | --- | --- |
> | 去哪 | **另一道题**的 prompt | 赛后提交给平台 / 给人看 |
> | 带 flag 的后果 | **泄漏**（B 题可能把 A 题的答案交上去） | 正确 —— flag 正是要展示的 |
> | 所以 | 白名单式脱敏 | **原样保留** |
>
> 判据是**这份内容的去向**，不是"flag 是不是秘密"。

**★ `GET /ctf2/challenges/{id}/writeup` 的 409 判据是"这道题跑过没有"**
（2026-10-15 修正两次，见下面两段"为什么"）：

| 成因 | 响应 | 话 |
| --- | --- | --- |
| 题不存在 | `404 NOT_FOUND` | —— |
| **从没跑过** | `409 WRITEUP_UNAVAILABLE` | 「还没有解过……点「解题」跑一次」 |
| **跑过但没留下题解行** | `409 WRITEUP_UNAVAILABLE` | 「这次解题没有生成题解（自 v0.36 起才产出）……重新解一次」 |

**"跑过"的判据是两条证据任一成立**（`service.has_solve_attempt`）：

1. `ctf2_solve_attempts` 里有没有这道题的行（v0.28 起的**受理快照**）；
2. `Ctf2Challenge.scan_id` 是不是非空（**壳 scan**，从第一版就有）。

> **为什么第一版（只看 `status == unsolved`）是错的**：`environment_ready` /
> `solving` / `candidate_ready` / `submitting` / `failed` **都不是** `unsolved`，
> 按状态二分会把它们全部归进第二档 —— 于是一道**正在跑**的题会被
> 报成"解题记录早于题解功能上线（v0.36）"。用户刚点完「解题」
> 却被告知"重新解一次"，这是本节开头那种误导的翻版。

> **★ 为什么第二版（只看 `ctf2_solve_attempts`）也错 —— 少了一条时间线**
> （2026-10-15 复核实测，Review 用全库 135 道逐题比出来的）：
>
> `ctf2_solve_attempts` 是迁移 `d5e8f1a37c92`（v0.28，10-11）才建的表。
> 在那之前解的题**没有 attempt 行**，但**有壳 scan**。
>
> 实测：**22 道**题有 `scan_id`、无 attempt 行、无题解，其中
> **20 道是 `solved` 且 `confirmed_flag` 非空**（`hashcat`、`Url编码`、
> `流量！鲨鱼！`、`[ACTF2020 新生赛]Exec` …）。它们的 `solved_at` 落在
> **09-30 ~ 10-08**，而有 attempt 的题最早是 **10-12** —— 时间上完全分界，
> 就是"表还不存在"那段时间解的。
>
> 第一版判据对这 20 道**平台判过对的题**回的是「这道题还没有解过」——
> **一句假话**。这与本节的立论（"别让错误信息误导用户"）直接冲突，
> 所以判据必须补上 `scan_id` 这条更早的证据线。
>
> 两条判据的覆盖对比（实测）：只查 attempt 误判 **22** 道；
> "attempt 或 scan_id" 误判 **0** 道。反向也安全：真没跑过的
> **104** 道（两者皆空）在新判据下仍判"没跑过"，不会多给入口。
> "跑过但没题解"的题从 **8** 道变成 **30** 道 —— 第二句话里
> 「可能那一次没有留下解题过程」对这批老题正是准确的措辞。

> **判据为什么不用状态**：状态会被后续流转改掉（`environment_ready`
> 是解题过程的主状态，几十分钟里都不是 `unsolved`），而快照与壳 scan
> 都是**落下来就不变**的事实。判"有没有发生过"，要用事实，不能用当前状态。

**★ 题解正文的版式（v0.38 重做）**

```
# CTF Writeup · {title}
> Flag / 结果 / 轮数 / 耗时          ← 一眼看到结论
## 一、题目信息                       ← 表格 + 题面原样
## 二、解题思路                       ← FINAL.summary（人话综述）
## 三、解题过程                       ← 关键步骤，完整命令
## 四、尝试与排除
   ### 试过的方向                    ← FINAL.tried
   ### 已排除的可能                  ← FINAL.ruled_out
## 五、结论                           ← flag / 候选 / 卡在哪
## 附录 A · 用到的工具
## 附录 B · 资源消耗
## 附录 C · 完整过程纪要             ← 原始流水挪到附录
```

> **★ v0.38 的根因不是排版，是内容被丢弃。**
> `runner.parse_final` 一直能解出完整的结论文档，但收尾时**只取了
> `candidates`**，`summary` / `tried` / `ruled_out` 解析完就丢掉了 ——
> `SolveOutcome` 当时根本没有对应字段。
>
> 实测（`[ACTF2020 新生赛]Upload`）：题解 5873 字符，而模型在 FINAL 里
> 写了「用响应长度做旁路：baseline=8628；上传 .php→8532…」这样的推理，
> 在题解里 grep「响应长度」「旁路」「黑名单」**全部 0 次命中**。
> 所以修的是**捕获**（`SolveOutcome` 加三字段 + `extract_final_analysis`），
> 不只是渲染。

> **★ `FINAL` 在 transcript 里有两种形态**（实测 61 份：裸行 35 /
> 带 `[turn N] assistant(...)` 前缀 6 / 两种都有 1 / 都没有 19）：
> 解析要**两路都覆盖**，只认一种会静默漏掉一批。
> 实测 42 份能解析出分析（summary 42 / tried 33 / ruled_out 31）。

> **★ 关键步骤里的 `|` 与标题层级是生成侧的契约**：
> 表格单元格里的裸 `|` 会被 GFM 拆列（要转义成 `\|`），
> 但**代码块里的 `|` 绝不能转义**（那是 shell 管道）；
> 版式**最深到 h3**（前端原来未映射 h4，会整片隐形 —— 新版式不依赖它）。

#### 3.19.6 ★ token 消耗统计（v0.36 新增）

**记什么**：每次 LLM 调用一行（`ctf2_solve_usages`），字段是接入点给的
`prompt_tokens` / `completion_tokens` / `total_tokens`。

> **★ 为什么是"每次调用一行"而不是在尝试上加累计列。**
>
> 需求要的是"细分到每一次任务的消耗"，而"每一次任务"有两种读法：
> 每次**解题尝试**、每次**LLM 调用**。独立表**同时满足两者**
> （按 attempt 聚合即得前者）；反过来只存累计列，明细就**永久丢失**了。
>
> 具体能多回答的问题：哪一轮最贵、是不是在兜圈子烧钱、
> 换模（v0.35 的 fallback）前后各花了多少 —— 这些都需要按行存。

> **★ 实测：这个网关的 `usage` 同时给两套口径，而其中一套是坏的。**
>
> 2026-10-14 在 Kali 上实测（默认 provider `gzd` / `deepseek-v4.1-flash`）：
>
> | 字段 | 简单问答 | 推理型问答 | 能用吗 |
> | --- | --- | --- | --- |
> | `prompt_tokens` | 37 | 75 | ✅ |
> | `completion_tokens` | 44 | 904 | ✅ |
> | `total_tokens` | 81 | 979 | ✅ |
> | `input_tokens` | 37 | 75 | ⚠️ 与 prompt 重复 |
> | **`output_tokens`** | **0** | **0** | ★ **恒为 0，不可用** |
> | `reasoning_tokens` | 0 | **0** | ⚠️ 实测不给，不可依赖 |
>
> 响应里同时有 `usage_semantic: "openai"` 与 `usage_source: "anthropic"`
> —— 这是**自建网关转译**的特征。若照 Anthropic 那套读
> `input_tokens` / `output_tokens`，会**静默少算全部输出**
> （而界面上显示 0 看起来像"没跑过"，比不显示更坏）。
>
> 另：推理型那次 `completion_tokens = 904` 而 `content` 只有 280 字 ——
> 说明**推理开销已计入 completion**，不必单加 `reasoning_tokens`。
> 仍然留一列备查（别的接入点可能会给）。

**`calls` 与 `recordedCalls` 分开**（见 §2.10 的说明）：前者永远准确，
后者是"接入点真给了 usage"的次数。两者不等时界面要**明说**，
不能让"没记录"伪装成"消耗 0"。

**不做 token → 金额的换算**：单价随模型、供应商、时段变化，
写死在代码里会误导。只给 token 数，换算由用户自己乘。

> **历史数据如实标注**：本功能上线**之前**的解题没有 usage 记录。
> 统计接口会带上 `since` 字段说明"从哪一刻开始有数据"，
> **不伪造历史**（同 §3.18 的"宁可少说，不说错的"）。

---

## 4. 实时事件流

> **本节按 SSE 写**（见 §0.1）。若最终选 WebSocket，本节整体替换，
> 但 §4.3 的事件负载结构**可以原样复用** —— 那是数据形状，与传输无关。

### 4.1 连接

```
GET /api/v1/scans/{scanId}/events      (Accept: text/event-stream)
```

也支持不带 scanId 的全局流（`/api/v1/events`），用于同时看多个扫描。

**可选订阅参数 `?lastEventId=<evt_id>`（v0.2 新增）**：

```
GET /api/v1/scans/{scanId}/events?lastEventId=evt_09
```

`EventSource` **只能在重连时**自动带 `Last-Event-ID` 请求头，首次连接带不了 ——
而首次连接恰恰是唯一有丢事件风险的时刻：契约 §4.4 要求前端
「先拉 `events/recent` 做 seed、再订阅」，这两步之间有一个几百毫秒的缝，
缝里产生的事件 seed 里没有、订阅也收不到。
把 seed 最后一条的 `id` 用这个参数带上来，缝就关掉了。

**不传游标时的行为：从当前末尾开始，不重放历史。**
从头发的话，一个跑了三小时的扫描会让每次打开页面都推几千帧，
而前端会全部渲染一遍。

补发的三种情况（服务端按此实现）：

| 情况                                     | 行为                                    |
| ---------------------------------------- | --------------------------------------- |
| 游标指向的事件还在                        | 重放它之后的全部事件                    |
| 游标指向的事件已被清理                    | 发 `event: resync` 帧，然后从末尾继续   |
| 游标之后积压超过 `SSE_REPLAY_MAX_EVENTS`  | 同上（对客户端而言这两件事是同一件事）  |

**订阅参数**：前端按 `scanId` 订阅。Scans 页的「查看活动」链接是
`/activity?scan=<id>`，ActivityPage 读它作为 `scanFilter`。

### 4.2 事件帧格式

```
id: evt_09
event: activity
data: {"id":"evt_09","scanId":"scan_01J8X2","agentId":"agent-03","agentPhase":"exploit","type":"poc_generated","summary":"...","detail":{...},"at":"2026-09-23T15:30:42.123Z"}

```

- **`id` 必须给** —— 它是 `Last-Event-ID` 补发的依据。
- `event:` 统一用 `activity`（也用于将来扩展 `agent_status` 等）。
- `data` 是 §2.5 的**完整事件**（含 `detail` 与 `agentPhase`）。

**心跳**：每 15–30 秒发一个注释行（`: keepalive`）防止中间代理断连。

### 4.3 事件类型与负载

见 §2.5（完整事件）与 §2.6（8 种 `detail` 载荷）。前端 `EventStream`/`EventRow`
已按 8 种类型渲染，**顺序与命名必须逐字一致**：

`tool_call` / `reasoning` / `discovery` / `poc_generated` / `verified` / `blocked` / `failed` / `completed`

**另需 `agent_status` 事件**（可选但建议）：名册上每个 agent 的状态
（`running|idle|blocked|done|failed`）会变，而完整事件里没有 agent 状态。

> 前端目前是**从事件流派生**名册统计的（`useActivityStream` 的 `agents` memo：
> 有事件进来就标 `running`，并累计 `eventCount`）。所以严格说**不是必需的** ——
> 但派生的代价是"agent 失败了但没有后续事件"这种情况无法表达。
> 建议加，前端按需消费。

#### 4.3.1 这些事件从哪来（v0.5 新增）

**这是任务 4 唯一真正新增的东西**：把外部执行工具（strix）产出的结构化事件
翻译成上面那 8 种。传输层没动 —— 见表后的说明。

⚠️ **官方文档写错了。** strix 文档说事件在 `strix_runs/<run>/events.jsonl`，
而在 `strix-agent` 1.6.2 的源码里全包搜索 `events.jsonl`，**零命中**。
那个文件不存在。真实产物（逐条来自源码）是：

| 文件 | 内容 | 依据 |
| --- | --- | --- |
| `strix_runs/<run>/run.json` | 运行记录 | `core/paths.py` 的 `RUN_RECORD_FILENAME` |
| `strix_runs/<run>/.state/agents.json` | agent 名册（`statuses`/`names`/`parent_of`/`errors`） | `interface/tui/live_view.py` 的 `hydrate_from_run_dir` |
| `strix_runs/<run>/.state/agents.db` | **SQLite，结构化事件流在这里** | `interface/viewer/transcript.py` 与 `interface/tui/history.py` |
| `strix_runs/<run>/vulnerabilities.json` | 漏洞列表 | 同上 |
| `strix_runs/<run>/strix.log` | 纯文本日志（**没有结构**） | `telemetry/logging.py` 的 `_FORMAT` |

`agents.db` 的表结构来自 openai-agents SDK：

```sql
CREATE TABLE agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,      -- ★ 这一列装的是 strix 的 agent_id
    message_data TEXT NOT NULL,    -- 一行一个 JSON
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

读它必须**只读打开**（`mode=ro`）—— 扫描进程可能正在另一个进程里写这个
WAL 库，读者绝不能加锁或改它。这是 strix 自己 `history.py` 的做法。

**语义映射表**（哪些确证、哪些启发式，逐条标）：

| 契约类型 | strix 来源 | 依据强度 |
| --- | --- | --- |
| `tool_call` | `function_call` + 结果成功 | ✅ 直接对应 |
| `failed` | 结果里 `success is False` | ✅ 直接对应 |
| `reasoning` | `tool_name == "think"` | ✅ **确证**：该工具签名是 `think(thought: str)`，docstring 写明是内心推理 |
| `reasoning` | `role == "assistant"` 的消息 | ⚠️ 语义近似，用 `detail.intent` 区分 `plan` / `narration` |
| `discovery` | `create_vulnerability_report` 成功 | ✅ 是"产出了一条漏洞"，⚠️ 载荷字段是启发式的 |

> **8 种里有 4 种在 strix 侧没有对应来源**，如实记：`poc_generated` /
> `verified` / `blocked` / `completed`。其中 `completed` 由**扫描生命周期**
> 发出（§3.3.1），不需要外部工具提供；另三种外部工具根本没有对应的结构化信号。
>
> ⚠️ `discovery` 的 `subject` / `evidence` / `severity` 是按"名字最可能对得上"
> 取的（`title` / `evidence` / `severity`），**需要一次真实扫描对着
> `vulnerabilities.json` 核对之后才算坐实**。取不到 `severity` 时
> **不放这个键**，而不是编一个默认值。
>
> ⚠️ `tool_call` 载荷里的 `exitCode` 与 `durationMs` **恒为 `null`**：
> 外部工具只给 `result.success` 布尔与调用区间，没有单次调用的退出码与耗时。
> 编一个 `0` 会让人以为那是真的进程退出码。

**事件是幂等导入的**：`activity_events` 上有 `(scan_id, source_row_id)`
的部分唯一索引，`source_row_id` 就是 `agent_messages.id`。
导入器每轮调度都跑一次（扫描还在写），靠这道索引去重 ——
没有它，活动流里每条事件会重复出现几十次。

> **关于传输层**：任务 4 要求用 WebSocket，而 §0.1 已裁决用 SSE
> 且任务 1 已经实现（含 `Last-Event-ID` 补发、`resync` 帧、心跳、
> 多条连接）。任务 4 的四条要求里，②③④ 都**已经由 SSE 满足**，
> 唯一缺的是①（捕获并结构化外部事件）。所以本次只补①，传输层不动。
> 决策记录见 `suvex-api/docs/adr-005-event-ingestion.md`。

### 4.4 断线重连与补发（**必须实现**）

`activity-mock.ts` 列出的三个真实复杂度，逐条对应：

1. **断线重连 + 补发漏掉的事件**
   - 客户端重连时带 `Last-Event-ID` 请求头（浏览器 `EventSource` 自动带）；
   - **服务端必须据此重放之后的事件**。若不实现，用户网络抖动一次就会丢一段历史。
   - 若客户端从 `Last-Event-ID` 之后的事件已被清理，服务端应发一个
     `event: resync` 帧告知"需要重新拉取"，让客户端回退到 `GET /scans/{id}/events?since=` 补历史。

2. **事件可能乱序到达**
   - 前端已按 `at` 排序（`useActivityStream` 的 `flush` 里 sort），
     所以后端**不需要保证顺序**，但**必须保证 `at` 准确**。
   - ⚠️ 不要用"发送时刻"当 `at` —— 那是推送时间，不是事件发生时间。
     补发时这一点尤其明显：3 分钟前的事件补发时 `at` 仍应是 3 分钟前。

3. **背压**
   - 前端已按 120ms 批量合并（`FLUSH_INTERVAL_MS`）。
   - 服务端也应做**服务端侧合并**：突发大量事件时不要逐条 flush 到 socket。
   - 建议：单连接每秒超过 ~200 条时合并成一批（但那需要新的事件形状，
     属于 v1 增强，v0 先不做）。

**`since` 游标**（补历史用）：

```
GET /api/v1/scans/{scanId}/events/recent?since=<ISO>&limit=200
```

返回 `{ "events": [...], "hasMore": false }`，前端在建立 SSE 之前先拉一次
作为 seed（对应 mock 的 `seedCount`）。

> 前端 mock 一次性同步产出 20 条 seed，真实实现应该是"先拉历史、再订阅"。
> 这个改动在 `ActivityPage` 的接入点注释里已经预留。

---

## 5. ★ 高风险操作：扫描授权确认

**这是本契约里唯一一个涉及法律责任的设计。** 合集明确要求：
"这个字段要落库留痕，不是前端摆设"。

### 5.1 为什么不能只在前端做

`POST /scans` 会**真的对目标发包** —— 端口扫描、目录枚举、漏洞利用尝试。
对未授权目标做这件事，在很多司法管辖区是**违法行为**。
所以"用户点过确认框"这件事必须：

1. **服务端强制校验** —— 前端可以绕过（改 JS、直接 curl）；
2. **落库留痕** —— 事后能证明"这个人在这个时间对某个目标声称过有授权"；
3. **与扫描记录绑定** —— 不是一条孤立的日志，而是那次扫描的一部分。

### 5.2 请求字段

```jsonc
"authorization": {
  "confirmed": true,
  "statement": "我有权对此目标进行安全测试",     // 原样回存用户同意的措辞
  "confirmedAt": "2026-09-23T15:30:42.123Z"    // 客户端时刻（服务端另记 receivedAt）
}
```

### 5.3 服务端校验规则

| 情况                                   | 响应                        |
| -------------------------------------- | --------------------------- |
| 缺 `authorization` 字段                | `403 TARGET_NOT_AUTHORIZED` |
| `confirmed != true`                    | `403 TARGET_NOT_AUTHORIZED` |
| `statement` 与约定的声明**不完全一致** | `400 VALIDATION_FAILED`     |
| 通过                                   | 落库，继续                  |

> 校验 `statement` 的措辞是刻意的：**防止前端"顺手勾一个默认值"**。
> 用户必须看到、并同意那**具体一句话**。这也让留痕有意义 ——
> 记录的是一条明确的声明，不是一个布尔。

### 5.4 落库要求

`scans` 表需含（或另建 `scan_authorizations` 表并外键关联）：

| 列                           | 说明                                                  |
| ---------------------------- | ----------------------------------------------------- |
| `authorized_by`              | **谁授权的**：会话 → 管理员账号 id；令牌 → `token:<tokenId>`（见下） |
| `authorization_statement`    | 用户同意的原文                                        |
| `authorization_confirmed_at` | 客户端声称的确认时刻                                  |
| `authorization_received_at`  | **服务端收到的时刻**（不可伪造）                      |
| `authorization_ip`           | 请求来源 IP                                           |
| `authorization_user_agent`   | 请求 UA                                               |

> **`authorized_by` 在单管理员系统里恒定，为什么还要存**：留痕要能被独立理解。
> 如果将来加第二个人（或加机器令牌签发者），这一列就是"谁授权的"，
> 而不是"某个人授权了"。**现在存的成本是 8 字节，将来补的成本是数据迁移。**
>
> ⚠️ **v0.10 修正（任务 8 发现）：机器令牌发起扫描时这一列原来是 `null`。**
> 原因很直接 —— 代码写的是 `identity.user_id`，而**令牌通道下 `user_id` 就是空**
> （令牌不绑定某个人，见 §1.2 的两条通道）。于是"哪个 CI 令牌授权对
> 这个目标发包"这件事**完全没有留痕**，而它恰恰是机器场景下最该回答的问题。
>
> 现在的写法：会话记 `user_id`，令牌记 `token:<tokenId>`。
> 前缀是必需的 —— 不加的话，"一个叫 `user_xxx` 的令牌"与
> "一个 id 为 `user_xxx` 的管理员"在这一列里分不开。

> 最后三项是**只有服务端能记录的**，也是留痕真正的价值所在 ——
> 前两项客户端可以伪造，服务端时刻和来源不能。

### 5.5 前端需要的改动（提示）

前端 `NewScanWizard` 当前**没有这个确认步骤**（第 4 步只有确认摘要 +
启动按钮）。接入时需要在第 4 步加一个必勾的复选框，未勾时
`LaunchButton` 的 `disabledReason` 返回"请先确认你有权测试该目标"。

> 这与既有的 `LaunchButton` 设计一致 —— 它已经强制要求
> **禁用时必须给出原因**，所以这个新约束能自然接进去。

---

## 6. 其他高风险 / 需注意的操作

除扫描外，以下操作也需明确鉴权级别与后果说明：

| 操作                                               | 级别    | 风险                   | 建议                                                                 |
| -------------------------------------------------- | ------- | ---------------------- | -------------------------------------------------------------------- |
| `POST /scans/{id}/rerun`                           | `scan`  | **同样会对目标发包**   | **同样要求 `authorization`**（重跑是一次新扫描）                     |
| `PUT /settings/sandbox`（`allowVolumeWrite=true`） | `admin` | 验证脚本可能改宿主文件 | 前端已有文案说明"仅在你确信目标代码可丢弃时开启"，沿用为准           |
| `POST /settings/tokens`（`scopes` 含 `admin`）     | `admin` | 授出管理权限           | 前端 `TOKEN_SCOPE_NOTE.admin` 已写"仅授予完全可信的集成"             |
| `POST /settings/tokens`（`expiresAt: null`）       | `admin` | 永不过期令牌           | 前端已把"永不过期"做成**显式选项**并标注风险，不要后端偷偷加默认过期 |
| `DELETE /providers/{id}`                           | `admin` | 用它的扫描会失败       | 有 running 扫描引用时返回 `409`，并说明是哪些扫描                    |

> **`rerun` 也要授权确认**这条容易被漏掉：重跑在语义上是一次新扫描，
> 目标可能已经变了（域名易主、IP 重分配）。不能因为"上次确认过"就免掉。

---

## 7. 接真实后端时前端要改的地方（汇编）

> **任务 7 已完成。** 这一节因此改了性质：从**待办清单**变成**对账表** ——
> 逐项记录当初计划怎么改、实际怎么改，以及**哪些没按计划做**。
> 偏离必须留在这里，否则下一个人会把这张表当成现状。

| 文件 | 计划 | 实际怎么做的 | 状态 |
| --- | --- | --- | --- |
| `lib/api.ts` | **新增**：统一 fetch 封装 | `apiRequest` + `apiText` + `ApiError` + `toApiError`。`credentials:'include'`、20s 超时（AbortController）、401 注册回调（登录/启动/`auth/me` 三条路径显式豁免）、错误信封 → `ApiError{code,status,fields}`。默认地址**跟随 `window.location.hostname`** —— 见下面那条 `SameSite` 的坑 | ✅ |
| `lib/api.ts` | `credentials: 'include'` | 已加。**另外发现**：`SameSite=Strict` 比的是 site 不是 origin，`localhost:5173 → 127.0.0.1:8000` 是**跨 site**，Cookie 会被丢掉。所以前端必须用 `127.0.0.1:5173` 打开 | ✅ |
| `App.tsx` | **新增**路由守卫 | `bootstrapAuth()` 探测 `/auth/state` → `RequireAuth` / `LoginRoute` / `SetupRoute`。**`/`（产品首页）保持公开** —— 这是对 §8 的收窄 | ✅ |
| `pages/LoginPage.tsx` / `SetupPage.tsx` | **新增**，复用 `Field` / `checkPassword` | 复用 `Field` / `TextInput` / `checkPassword`，**没有**重写校验。后端连不上时进入独立的 `offline` 阶段并**把后端地址写出来**（"重试"对"没启动服务"是没用的） | ✅ |
| `Sidebar` | 加登出入口 | 已加，与"收起导航"同区 | ✅ |
| `data/activity-mock.ts` | `createMockEventSource` → `createSseEventSource` | 已换并**删除 mock 文件**。⚠️ 踩到帧格式：后端发的是命名事件（`event: activity`），`es.onmessage` 收不到任何东西 | ✅ |
| `pages/ActivityPage.tsx` | SSE 源；先拉 `events/recent` 做 seed | 已接。种子只存在于**按扫描**的端点，所以全局流没有 replay —— 这一点写在 `activity-stream.ts` 顶部 | ✅ |
| `data/scans-store.ts` | CRUD → REST | 已接。`createScan` 新增**授权确认**入参（§5.5） | ✅ |
| `pages/ScansPage.tsx` | ★ 先做轮询 | 已接：有 `queued`/`running` 时按 2.5s 轮询 `GET /scans`，**全部到终态就停**（依赖是布尔 `hasActive`，不是列表引用） | ✅ |
| `data/findings-store.ts` | `setFindingStatus` → `POST /findings/status` | 已接。角标改读 `GET /findings/stats`（§3.4.2 的"唯一数据源"），本地计数函数删掉 | ✅ |
| `data/settings-store.ts` | 全部变更函数 → REST | 已接。`sandbox` / `notices` 后端仍未交付（§9），保持内存态并**显式标注** | ⚠️ 部分 |
| `components/settings/ProviderPanel.tsx` | 四点（掩码 / 空串表示不改 / 重置清空 / 409 提示） | 四点都做了，`secretPreview` 的二次处理已删 | ✅ |
| `data/providers.ts` | `useProviders()` 改读 API 投影 | 已接（`providers.ts` 原先的 `MOCK_PROVIDERS` 常量早已删除，投影源是 `settings-store`） | ✅ |
| `data/providers.ts` | 加 `protocol` 字段（`openai` / `anthropic`） | **未做**：后端 `ProviderOut` 里没有这个字段，前端加了也拿不到值。见下面的偏离清单 | ❌ 阻塞 |
| `data/overview-mock.ts` | `useOverviewData` → `GET /overview` | 已接并**删除 mock 文件**。整页读一个端点（同事务算齐），代价是这一页不再实时联动 | ✅ |
| `data/reports-store.ts` | 本地生成种子 → `GET /reports` | 已接并删除种子。**另加**：历史文档 `GET /reports/{id}/document`、草稿预览 `POST /reports/preview`（理由见偏离清单） | ✅ |
| `lib/report-api.ts` | `delay()` → `fetch`；PDF 仍 `browser-print` | Markdown 走 `GET /reports/{id}/download`（文件名取自 `Content-Disposition`），PDF 保持浏览器打印 | ✅ |
| `components/scans/NewScanWizard.tsx` | 加授权确认复选框（§5.5） | 已加，措辞常量 `SCAN_AUTHORIZATION_STATEMENT` 与后端**逐字一致**（跨仓库对账点） | ✅ |
| `scripts/report-parity.mts` | 改为对着 API 跑 | **未按原计划**：改成读一组**独立 fixture**。理由见偏离清单 | ⚠️ 偏离 |

### 三处偏离，逐条交代

**① `protocol` 字段没加 —— 而且不该由前端单方面加。**
契约 §3.2 要求它，但后端 `ProviderOut` 里没有这个字段（`app/providers/llm.py`
的注释也确认了"那是前端类型变更"）。前端加一个拿不到值的必填字段，只会让
每一个 provider 都带着 `protocol: undefined` 流进判断逻辑。**这一项要等后端字段。**
（现状不受影响：`protocol` 只在"自建网关转发 Anthropic 协议"时才影响探测 —— 那条路径当前不存在。）

**② 报告预览与导出从"前端 IR"改成了"服务端渲染"。**
原计划是"只把数据来源从 mock 换成 API，派生逻辑不动"。实际做不到，原因是硬的：
`GET /findings` 返回**摘要**（§3.4），**不含 `detail`** —— 前端拿不到
`description` / `evidence` / `reasoning` / `poc` / `remediation`。
症状一开始很隐蔽：预览照常渲染，标题/严重度/状态/时间全对，
**只有「问题描述」和「证据」两节是空的**，而"这条没有内容"和"内容没取到"
在屏幕上长得一模一样。
所以预览改走 `POST /reports/preview`、下载走 `GET /reports/{id}/download`、
历史预览走 `GET /reports/{id}/document` —— 三者同源，一致性反而更强了
（原来是"前端 IR vs 后端 markdown"跨端一致，现在是同一份服务端产物）。
前端的 `report-document.ts` / `report-markdown.ts` **保留**，作为 §3.5.1
对账脚本的参照实现。

**③ `report-parity.mts` 改成 fixture，而不是"对着 API 跑"。**
§3.5.1 的原话是"那个脚本应当改为对着 API 跑"。实际做的是把输入从
`findings-mock` / `scans-mock` 换成一个**独立 fixture 文件** ——
因为那两个 mock 源在任务 7 里被删了，脚本当场坏掉（`Cannot find module`）。
不改成"对着 API 跑"的理由：这个检查验的是**渲染器覆盖完整**
（IR 的每个字段是否都出现在 Markdown 里），那是纯函数的事，与数据从哪来无关；
而一旦依赖网络，它就不能在 CI 里离线跑，绿色也不代表渲染器是对的。
**真正的跨端一致性应该由后端那份 `verify_report_parity.py` 对着 API 验** ——
那是后端的活，记在这里以免遗漏。
顺带修掉一条**永远为真**的假检查（`!regex.test('') || true`），
它会给出绿色的 ✓，而真正的回归（表格因标题里的竖线错列）不会有人发现。

**设计原则（贯穿全部改动）**：`data/*.ts` 里的**派生函数与类型保持不变**，
只替换数据来源。这条原则**基本成立**，两处例外都在上面写了：
报告的文档构建（②）和 Overview 的指标派生（搬到了后端）。
两处的原因相同 —— 它们都**需要全量 detail**，而列表接口按契约只给摘要。

> **新增的认证代码同样遵守既有约定**：错误用 `SaveResult` 的 `{ status, error }`
> 形状渲染、按钮禁用必须给原因（`LaunchButton` 已经立了这个规矩）、
> 界面文案中文。**不要因为"这是登录页"就另立一套交互**。

---

## 8. 前端需要新增的工作（登录带来的）

§0.3 决定加登录后，**多出一块合集里没列出的前端任务**。列在这里以免遗漏：

| 项           | 文件 / 位置                   | 说明                                                                              |
| ------------ | ----------------------------- | --------------------------------------------------------------------------------- |
| 启动状态查询 | `main.tsx` 或 App 根          | 先调 `GET /auth/state`，决定显示初始化页 / 登录页 / 主界面                        |
| 初始化页     | `pages/SetupPage.tsx`（新增） | 只显示一次。复用 `checkPassword` 的强度条与逐条要求（**已有组件逻辑，不要重写**） |
| 登录页       | `pages/LoginPage.tsx`（新增） | 单口令框。复用 `Field` / `TextInput`                                              |
| 路由守卫     | `App.tsx`                     | 未登录一律重定向到登录页；`/auth/state` 未初始化则到 setup                        |
| 401 拦截     | `lib/api.ts`（新增）          | 任何请求收到 401 → 清本地态 + 跳登录页。**不能只在登录接口处理**                  |
| 登出入口     | `Sidebar` 底部                | 与现有"收起导航"同区                                                              |
| 凭据携带     | `lib/api.ts`                  | `credentials: 'include'`（Cookie 必须显式带上，否则跨端口不带）                   |

> ⚠️ **`credentials: 'include'` 是必须的**：开发态前端在 `:5173`、后端在 `:8000`，
> 是**跨源**请求。不显式声明的话浏览器不会带 Cookie，表现为"登录成功但下一个请求又是未登录"。
>
> 这也意味着后端要正确配置 CORS：`Access-Control-Allow-Credentials: true`
> 且 `Access-Control-Allow-Origin` **必须是具体源**（不能用 `*` —— 带凭据时
> 通配符被浏览器拒绝）。

> 💡 **新增页面同样遵守既有约定**（`design-system.md`）：
> 界面文案中文、用现有 token、不引入新的视觉语言。
> 登录页是这个产品给人的第一印象之一，但它**不需要** Hero ——
> 那会让"赶紧进去干活"的用户多等一屏。

---

## 9. 后端分期交付（v0.2 新增）

后端按任务分 8 期实现，**端点从一开始就全部注册**，未实现的返回
`501 NOT_IMPLEMENTED`，`message` 里写明计划在哪个任务交付。

| 状态 | 端点                                                                 | 计划任务 |
| ---- | -------------------------------------------------------------------- | -------- |
| ✅ 已实现 | `/health`、`/health/sandbox`、`/auth/*`（全部）、`/settings/password`、`/settings/tokens`（增删查）、`/scans/{id}/agents`、`/scans/{id}/events`、`/scans/{id}/events/recent`、`/events` | 任务 1 |
| ✅ 已实现 | `/providers/*`（全部六个端点，含加密存储与真实连通性探测）                                                          | 任务 2   |
| ✅ 已实现 | `/scans`（列表 / 新建 / 详情 / abort / rerun）、`/scans/queue`、`/scans/{id}/agents` —— 真的拉起外部工具执行        | 任务 3   |
| ✅ 已实现 | 外部工具的结构化事件导入（§4.3.1）—— 解析层 + 幂等落库 + 收尾补齐，走已有的 SSE 通道                              | 任务 4   |
| ✅ 已实现 | `/findings/*`（列表 / 详情 / 批量状态流转 / 统计）—— 含漏洞记录导入、时间线留痕、统计唯一数据源                   | 任务 5   |
| ✅ 已实现 | `/reports/*`（列表 / 生成 / 预览 / 详情 / 文档复现 / 下载 / 删除）—— IR 复刻前端、内容冻结、Markdown 导出；PDF 仍走浏览器打印 | 任务 6   |
| ✅ 已实现 | `/overview` —— 全部派生（统计走 `stats()` 唯一数据源、活动流投影、两个 `null` 语义守住）。**契约原本把它排在任务 6，而任务 6 的正式交付物是报告** —— 于是它的 501 消息「计划在任务 6 中交付」过期成了假话，且当时的验收脚本把这个 501 断言成 PASS。v0.8 一并修正 | 任务 6（补） |
| ⏳ 未实现 | `/settings`（快照 / 沙箱 / 通知）                                    | 任务 7   |

> **为什么注册而不是干脆不注册（让它们 404）**：404 的语义是「这个资源不存在」，
> 而真实情况是「端点还没做」—— 对调用方来说是两种完全不同的处理。
> 注册之后 OpenAPI 文档里能看到完整端点清单，前端联调不必靠读文档猜。
>
> ⚠️ **未实现的端点同样要求鉴权**（401 先于 501）。否则它会变成一个
> **免鉴权的存在性探测**：能区分"端点存在但没做"与"没有这个端点"。

**测试与验收**（后端仓库内）：

```bash
pytest                              # 118 个用例，对真实 PostgreSQL 跑
python scripts/verify_acceptance.py # 32 项端到端验收，含真实 SSE 长连接
```

---

## 附：契约自检记录

本文档的字段是从前端源码逐字提取的，**并且有机器化校验**（不靠人保证）：

```bash
npm run verify:contract   # 枚举取值 / 事件类型差异 / 状态流转表 ↔ 源码 对账
npm run verify:coverage   # 对照任务合集的需求清单，查端点是否覆盖齐
npm run verify:all        # 上面两个 + 报告预览导出一致性
```

**为什么要做成脚本而不是一次性检查**：

1. 契约是**手写**的，而它声明"字段从源码提取" —— 手抄就会有错；
2. 本项目已多次栽在"文档与实现漂移"上（品牌改名漏同步 token 前缀示例、
   动效 token 名与代码类名不一致导致动画静默失效）。这类问题的共同点是
   **不报错**，只是文档和代码说的不是一回事；
3. 这份契约要被后面 **8 个后端任务**依赖 —— 它错一个字段名，全部返工。
   而契约还会有增量修改（合集写明"发现遗漏先回来改这份文档"），
   所以"改完重新对账"必须是**一条命令**，不能靠记性。

**脚本本身也验证过**：故意把 `false_positive` 从契约里删掉后，
`verify:contract` 报出 4 处具体不一致并以退出码 1 结束 ——
一个不会失败的检查比没有检查更坏。

已逐项核对并纳入自动校验的源文件：
`data/scan.ts`、`data/finding.ts`、`data/findings.ts`、`data/activity.ts`、
`data/overview.ts`、`data/report.ts`、`data/settings.ts`、`data/providers.ts`、
`lib/finding-query.ts`、`lib/scan-query.ts`、`lib/report-build.ts`、
`lib/report-document.ts`、`lib/validate-target.ts`、`lib/report-api.ts`、
`hooks/useActivityStream.ts`、各 `*-store.ts`。

### 契约版本

| 版本 | 变更                                                                                                                             |
| ---- | -------------------------------------------------------------------------------------------------------------------------------- |
| v0.10 | **任务 8（安全加固）的两处契约修正与一处新增**：① §3.9 **新增审计日志**（`GET /audit` + 六个 `AuditAction` + 365 天保留策略）；② §3.5 `DELETE /reports/{id}` 的级别从 `read` 提升为 **`admin`** —— 原值与本契约 §1.2 对 `read`（「只读」）的定义自相矛盾，且它是该组唯一的销毁型操作；③ §5.4 `authorized_by` 明确令牌通道记 `token:<tokenId>`（原来是 `null`，机器发起扫描时不留人）。三项都是**先改契约再改代码** |
| v0   | 初稿。含三处待裁决项                                                                                                             |
| v0.1 | **三项决策落地**：SSE / 加登录（最小化）/ 字符串主键。新增 `/auth/state`、`/auth/setup`、CSRF 防护、登录带来的前端工作清单（§8） |
| v0.2 | **实现反馈的六处修订**（都在实现任务 1 时发现，按「先改文档再改代码」的规矩回改）：① 错误码表补 `405 METHOD_NOT_ALLOWED` / `501 NOT_IMPLEMENTED` / `403 CROSS_ORIGIN_BLOCKED`；② `logout` 的鉴权级别从 `read` 放宽为幂等（凭据过期时也要能清 Cookie）；③ §3.7 补 `/settings/tokens` 与 `/settings/password` 的请求体形状；④ §4.1 新增 `?lastEventId=` 订阅参数并写明「不传游标 ⇒ 从末尾开始」；⑤ §2.1 注明 `startedAt` 在 queued 时等于受理时刻；⑥ §2.4 注明 `Agent.id` 必须全局唯一（举例 `agent-03` 会误导）。新增 §9 分期交付状态表 |
| v0.3 | **任务 2（providers）落地，七处修订**：① §2.9 新增只读字段 `apiKeyMask`，并写明**为什么掩码不能放进 `apiKey`**（前端保存时会把 `account.apiKey` 原样回传，那会把真凭据替换成掩码 —— 静默数据损坏）；② §2.9 新增「凭据加密存储」一节（Fernet + 独立环境变量 `SUPEX_SECRET_KEY`，掩码另存一列使读路径不必解密）；③ §3.2 的 `POST`/`PATCH`/`default` 响应改为 `SaveResult` + `provider`；④ `verify` 响应补 `durationMs` / `endpoint` / `reply` / `provider`，并写明**失败也返回 200**；⑤ 新增 `PROVIDER_IS_DEFAULT` / `PROVIDER_IN_USE` / `PROVIDER_KEY_UNREADABLE` 三个错误码，并记下「默认不可删」的例外（最后一个）与**移除 `scans.provider_id` 外键**的理由；⑥ §3.2 写明探测协议按接入点判定、以及"本地 baseUrl 补 `/v1`"这条归一化；⑦ §7 补 provider 接线时前端要改的四处 |
| v0.4 | **任务 3（扫描生命周期）落地，五处修订**：① §2.1 新增 `runId`（外部工具侧的运行标识），并写明 `progress` 后端**不填**（不编造百分比）；② §2.1 的 `startedAt` 语义精确化：开跑时被改写为真实开跑时刻，`durationSec` 因此不含排队时间；③ 新增 §3.3.1 —— 执行模型（退出码↔状态映射、**退出码 2 是成功**、并发上限、`/scans/queue`、凭据只走环境变量、服务重启的孤儿处理）；④ §9 交付表更新；⑤ §7 补 Scans 页轮询接线 |
| v0.5 | **任务 4（事件流接入）落地**：新增 §4.3.1 —— 查证后确认**官方文档写的 `events.jsonl` 在 1.6.2 源码里不存在**，真实的结构化事件流在 `strix_runs/<run>/.state/agents.db`（SQLite）。逐条记下格式依据（表结构 / 列名含义 / 只读打开 / 三种 `message_data` 类型 / `call_id` 配对 / 内部轮前缀表）、**语义映射表**（确证与启发式分开标）、以及 8 种里有 4 种在外部工具侧没有对应来源这件事实。**传输层不动**：任务 4 的四条要求里 ②③④ 已由既有的 SSE 满足，决策记录见 ADR-005。§9 交付表更新 |
| v0.6 | **任务 5（漏洞持久化与状态流转）落地**：① 新增 §3.4.1 —— 明确 `GET /findings/stats` 是**统计的唯一数据源**，Overview 必须调它，并写清两处口径差异（`severityBreakdown` 含误报、两个 metric 排除误报）；② 新增 §3.4.2 —— `bySeverity` 是 `Partial`（为 0 时键不存在）而 `byStatus`/`byCategory` 不是，这个不一致是**有意的**并给出理由；③ §9 交付表更新；④ **顺带修正了任务 4 的一处错误**：读源码确认 `create_vulnerability_report` **没有 `severity` 参数**（它由 `cvss_breakdown` 经 CVSS3 算出），任务 4 里按"名字最可能对得上"猜的那个字段从来没命中过 —— 当时标的"待对账"兑现了 |
| v0.7 | **任务 6（报告生成）落地，四处修订**：① §3.5.1 补上**实测复核** —— 本机 Edge/Chrome 都在、无头打印实测成功（41KB PDF），所以"服务端 PDF 做不到"**不成立**；真正挡住它的是"独立渲染就意味着第二套版式"这条架构约束，并说清什么情况下该重新评估；② `POST /reports/preview` 与 `GET /reports/{id}/document` 的语义写明（前者 `provisional=true` 且 `code=""`，后者按冻结快照复现）；③ **明确"冻结"到底冻结了什么**：只冻结 `findingIds`（成员）、`position`（顺序）、`excludedCount`，而条目内的 `statusLabel` 反映**当前**状态 —— 这是与前端 `rebuildReportDocument` 一致的有意行为，不写清楚会被下一个人"修"掉；④ 新增对账脚本 `verify_report_parity.py`（契约 §3.5.1 原本就要求"那个脚本应当改为对着 API 跑"） |
| v0.8 | **`/overview` 交付 + 两处历史遗留的修正**（都是在写 `/overview` 的测试、回读验收脚本输出时对账发现的）：① **§2.5 与 §3.6 自相矛盾** —— §3.6 说 `activity` 是「投影形状，见 §2.5」，而 §2.5 说「不要后端发投影」。§2.5 的建议按此**收窄为「除 `/overview` 外」**，并写明选 §3.6 的依据（投影与完整事件的 `summary` 本就是同一字段，后端没多承担什么；而 `OverviewData` 是已定稿的前端类型）；② **`/overview` 的 501 消息过期成假话** —— 契约 §9 把它排在任务 6，任务 6 交付的却是报告，于是消息里「计划在任务 6 中交付」从此不成立，**而一个永远返回 501 的端点自己不会喊**；更糟的是验收脚本把这个 501 断言成了 `[PASS]`，把"承诺过期"洗成绿灯。已实现该端点，并把那条检查的目标换成确实未实现的 `/settings`；③ **`/findings/stats` 的 `bySeverity` 没满足它自己的 `Partial` 语义** —— 契约说"为 0 的档键不存在"，实际响应却是 `{"high": null, "medium": null, ...}`（`int \| None` 被 Pydantic 序列化成显式 null）。前端 `?? 0` 兼容所以一直没暴露，而**"两边都接受"正是它危险的地方**。修法是 `response_model_exclude_none=True`；④ 补 §3.6 的实现要点（数据来源表、平均耗时只算已完成、`severityBreakdown` 与 stats 口径不同是刻意的、`activity` 投影与 `subject` 为 null 的规则） |
| v0.9 | **任务 7（前端接入）完成，§7 从待办清单改成对账表**：① §7 逐项记录"计划 vs 实际"，并把**三处偏离**写明（`protocol` 字段因后端无此字段而阻塞；报告的预览与导出从"前端 IR"改为"服务端渲染"，因为 `GET /findings` 按 §3.4 只给摘要、**不含 detail**，前端构建出来的报告「问题描述」与「证据」两节是空的；`report-parity.mts` 改成独立 fixture 而非"对着 API 跑"，并修掉其中一条**永远为真**的假检查）。② 明确**"全站不再依赖 mock"有一处例外**：§9 里 `/settings` 快照仍未交付，所以沙箱与通知两块保持内存态，并在面板上显式标注。③ 记下两条只有真接口才暴露的坑：`SameSite=Strict` 比的是 **site** 而不是 origin（`localhost:5173 → 127.0.0.1:8000` 是跨 site，Cookie 会被丢掉），以及 SSE 的**命名事件**（`event: activity`，用 `onmessage` 收不到）。④ token 用量在状态条上如实标注"未接入"——契约里**没有**这个端点，而原来的 `184.3k / 1M · 18%` 是编的。<br>⑤ **`durationMs` 的类型与本文 §4.2 不符**：契约写明 `tool_call` 的 `exitCode` 与 `durationMs` **恒为 `null`**（外部工具不给单次调用的退出码与耗时），而前端 `EventData` 把它声明成了 `number`。mock 事件都带数字，所以一直没暴露；真实事件一进来，活动流里每条工具调用都渲染成 **"nullms"**（`null < 1000` 在 JS 里是 `true`），而 `exit —` 还被涂成红色（"非 0 即红"把**未知**当成了**失败**）。两侧都改成如实表达。<br>⑥ **顶部状态条在非设置页说反话**：直接落在总览时它写着「未配置 · 去「设置」添加模型服务」，而 provider 早就配好了 —— 因为 provider 只在 Settings 页被加载过。修法是 `Layout` 统一调 `ensureScansLoaded()` / `ensureSettingsLoaded()`（幂等）。**不能放在 `TopStatusBar` 里**：那会让公开的落地页发出未登录请求、401 后被踢到登录页，落地页就此不可访问。<br>⑦ **通知的"测试消息已送达"是编的**：`POST /settings/notify/test` 未交付，前端原来的实现按地址形态**猜测**结果并渲染一句绿色的「已送达 · 刚刚」。通知是告警通道 —— 给用户一个假的"通"，等于让他带着一条从未验证过的通道上线，而且他会因为见过那句绿字而更确信它没问题。改成**禁用按钮 + 说明原因**。<br>⑧ 落地页页脚原写「后端接口尚未接入」，已接入后那句话是假的，一并改掉。<br>⑨ **沙箱状态面板在说一个验证不了的断言**：它显示「Docker 沙箱 · 运行中 · 沙箱容器已就绪，可以接受扫描任务」，而那个值来自内存初值 —— 本机可能根本没有 Docker。SaveBar 上那句 `BACKEND_PENDING_NOTE`（"保存不会持久化"）**不能为它背书**：两件不同的事。未接入期间降级为「状态未知」并说明原因 |
| v0.14 | **任务 10（漏洞工作流）第一批：状态机扩展**：① §2.1/§2.3 的状态从 4 个扩到 6 个（新增 `retesting` / `closed`），并新增三条边 —— `fixed → retesting`（提交复测）、`fixed → closed`（**直连关闭**，保留它是因为需求原文写"如需要复测环节"，复测不该是唯一路径，强制经过会让明显修好的漏洞也要多走一次流转）、`closed → confirmed`（**可以重新打开**，没有回路的终态会逼用户把"重新打开"记成新记录而切断历史）；`fixed` 与 `retesting` 均**不能**直接判误报，必须先退回 `confirmed`。② §3.4 的 `POST /findings/status` 新增约束：`status=false_positive` 时 `note` **必填**（去空白后非空），否则 `400 VALIDATION_FAILED` —— 误报必须有理由，否则"规则误报率"只能计数、无法复盘是哪类规则在误报；批量场景填**一次**理由即应用到全部选中项，不要求逐条填（逐条会让批量判误报重新不可用，用户会改用「已确认」糊过去，反而丢掉这个信号）。③ **顺带修正报告模块的两处状态联动**（都由状态扩容暴露）：`reports/document.py` 的 `only_confirmed` 过滤原本是白名单 `status in (CONFIRMED, FIXED)`，会**静默漏掉** `retesting`/`closed` 这两档确认过的状态 —— 给客户的报告里少放两条真问题而没有人会当场发现，已改成**排除式**（只排除 `new` 与 `false_positive`），将来再加状态也不会漏；`summarize()` 的 `"fixed"` 原本只数 `FIXED`，而用户走完复测流程后状态会变成 `retesting`/`closed`，报告里的"已修复 N 条"会**随着流程推进而变小**（事情做得越多、修复数越少），已改为计入这三档。④ 前端同步 6 个状态（类型 / `FINDING_STATUS_LABEL` / `_ORDER` / `_TONE` / `TRANSITIONS` / `TRANSITION_LABEL` / `TRANSITION_INTENT`、徽标图标、详情页短名、排序权重、计数兜底），其中 `satisfies Record<FindingStatus, …>` 让编译器一次性揪出了全部 5 处遗漏 —— 这正是它存在的价值，靠人眼找必漏。⑤ ⚠️ **变更记录表的顺序是乱的**（本次顺带发现）：`v0.10` 那一行被插在了 `v0.1` **之前**（约 2097 行），而 `v0.11`–`v0.13` 只在正文各处有标注、从未作为表行补上。内容都在，只是这张表读起来不连续。留此说明，以免下一个人以为那几版不存在。 |
| v0.15 | **任务 10 第二批：指派负责人 + 按类型分组**：① 新增 **§3.10 处理人名单（`/members`）** —— 独立的一张业务名单，**不是登录账号**：本系统是单管理员的（`/auth/setup` 一次性初始化），"指派给谁"要回答的是"这条漏洞该由谁去修"，而那个人往往不登录这个系统；用名单表而不是自由文本，因为按负责人筛选会被错别字撕成好几组（`name` 上有唯一约束，重名 400）；成员**停用而非删除**，历史指派仍显示名字。读放宽到 `read`（漏洞列表上"负责人"那一列读的就是它），写是 `admin`。② 新增 **§3.10.1 `POST /findings/assign`** —— 批量指派，`assigneeId: null` 是**明确的操作**（取消指派）而不是"没传"；幂等跳过不写时间线；指向不存在的处理人 404。③ §3.4 的 `GET /findings` 新增两个筛选：`assigneeId`（这个人名下的）与 `unassigned`（还没人认领的）—— **它们是两种不同的查询**，用空串表达"未指派"会让空串成为特殊值而库里存的是 NULL。`FindingSummary` 同时带 `assigneeId` 与 `assigneeName`，名字在服务端批量装配（一次查询，不是 N+1），前端不必为渲染那一列再发一次名单请求。④ 新增 **§3.11 `GET /findings/groups`** —— 按**标题**分组（与扫描对比模块同一个判据，否则会出现"对比说这条消失了、分组视图里还在"）；组内最高严重度决定这一组的样子；带 `findingIds` 而不只是计数（展开时用它筛列表，按标题重查会多带出同名的其他条目）；筛选参数与 `/findings` 完全一致；聚合在 SQL 里做而不是取回再 groupby。⑤ §3.9 补记**留痕粒度的规则**：**时间线逐条**（批量改 3 条就有 3 行 `assigned`，各带那一条自己的历史）+ **审计每次操作一行**（一次批量 1 行，`detail.changedIds` 列全部 id）。两者不是替代关系 —— 审计若也逐条，一次 500 条的批量会产生 500 行，把真正的操作序列淹掉。⑥ `DELETE /members/{id}` 返回 `{"unassigned": N}` 而**不是 204**：删掉处理人会解除他名下所有漏洞的指派，而 204 没有响应体、前端拿不到那个数字 —— **实测时正是这么发生的**（删完之后那几条的负责人无声消失，页面上没有任何提示）。⑦ 新增审计动作 `finding.assign` / `member.create` / `member.update` / `member.delete`。⑧ 迁移 `822b63652b4e`：新建 `members` 表 + `findings.assignee_id` / `assigned_at`（`ON DELETE SET NULL`）+ **`ix_findings_assignee_id` 索引**（外键约束不会自动建索引，而"按负责人筛选"是这个功能的主要用途之一）。 |
| v0.16 | **任务 10 第三批：SLA 修复时限 + 手选漏洞出报告**：① 新增 **§3.7 的 `GET/PUT /settings/finding-sla`** —— 按严重度设修复天数（默认 critical 3 / high 7 / medium 30 / low 90 / info 0），`0` = **该档不设时限**（不是"立刻到期"：给低危套一个必然超期的假期限会让"超期"这个信号整体失效，一眼全是红的等于没人看）；`startFrom` 可配 `discovered` / `confirmed`（按确认时间起算对"确认得晚"的团队更公平，但会让人有动机拖着不确认 —— 两种都有道理，所以交给配置）；保存时**立刻重算**所有未终结漏洞（不重算的话用户改了天数回列表看到的还是旧倒计时，看起来像没保存成功）。② `FindingSummary` 新增 `dueAt` / `slaState` / `slaDaysLeft`，**三个都由后端判定**（前端拿 `dueAt` 和 `Date.now()` 自己比的话，两处独立判断会在刷新前后给出不同答案，而"临期"的判定窗口得在前后端各写一份）。`GET /findings` 新增 `slaState` 筛选，用 `sla_due_at` 的**区间条件**（能走索引，而不是全表取回来再过滤）。③ `findings.sla_due_at` **冗余存储** + `ix_findings_sla_due_at`：到期提醒要排序也要筛，现算的表达式用不上索引。三个重算时机（导入 / 状态流转 / 规则变更）都在服务端。④ 报告 scope 新增 **`kind="findings"`**（打通"漏洞发现"与"报告"：漏洞页勾选后直接带过去，不必在报告页重新勾一遍）。⚠️ 它与另外三种有一处结构差异：成员是**一串 id**，塞不进任何一列 —— 存在既有的 `report_findings` 关联表里，所以 `reports` 表**不加列**，只放开两条 CHECK 约束。⑤ **修复两处 v0.14 的遗留缺陷**（都由本轮实测暴露）：`StatusLiteral` 仍是 4 个值，而它是 `POST /findings/status` 的请求体类型 —— 「提交复测」「关闭」会被 pydantic 拦成 422；更严重的是**数据库上的 `ck_findings_status_valid` 也只允许 4 个值**，`closed` / `retesting` **根本存不进去**（撞 CHECK 约束，500）。两处都已修正（含迁移 `b1f7c0a25e38`）。这两处漏掉的原因是它们"长得不像状态机定义" —— `Literal` 是个类型别名、CHECK 在数据库里，而当时的对账脚本只比对了 `vocab` 与前端 `finding.ts`。⑥ 迁移 `6d1460fcd7b4`（SLA 表 + 列 + 索引）、`c8a41d6e9307`（report scope 两条约束放开）。⑦ **顺带修复测试基建的一处死锁**：`_clean_state` 用 `TRUNCATE ... CASCADE` 清表时，上一个用例的后台调度器线程可能还在写库，撞锁报 `DeadlockDetected` —— 而它只在全量跑（跑了六分半、前面有若干真扫描）时出现在**下一个用例的 setup 阶段**，单独跑永远通过。修法是清表前先等在途扫描落地（全量 842 passed / 0 error，代价是 6.5 分钟 → 11.7 分钟）。 |
| v0.17 | **任务 11：四项真实事故的修复**（触发点是一次**跑了 32 分 16 秒零产出**的扫描，用户只能看着转圈的图标）：① **§2.1 新增 `stalledAt` / `lastEventAt` + 卡死检测** —— 判据用**事件时间戳**（`_import_for` 每轮都导入外部工具新写出的事件，所以"事件停了"就等于"agent 确实没在产出"），不用 CPU（会误伤"在跑长命令但没输出"）也不用输出量（依赖落盘时机）。⚠️ **`--max-turns` 拦不住这类卡死**：回合数根本没在涨，时间却在走。默认 `scan_stall_action="flag"`（**只标记不终止** —— 启发式判断，而终止一个真扫描不可逆），标记**可自愈**（有新事件即清，否则一次误判会让界面永远挂着"疑似卡死"）。② **修复 `terminatedBy` 从来没有人写过值**：字段、契约、前端类型、UI 上那栏「终止方」全都在，而三条终止路径一条都没赋值 —— 于是每个值都是 `None`，界面永远显示"无法判定"。更糟的是**同一件事有两种措辞**：strix 日志写 `interrupted by the user`，而 Suvex 记 `服务停止，扫描已终止`，看起来像它自己挂的。三条路径现已分别写入 `user`（用户按终止）/ `system`（调度器回收孤儿、进程树终止）。③ **§2.8.1 目标可达性预检** —— 那次卡死的真正成因是**用户在目标里没写协议**，前端按既有规则补成 `https://`，而该主机只在 http 下有应答（http 0.06 秒 404 / https TLS 握手失败）。现在创建时会探测一次并把结论放进 `notice` 带回界面；**判定"通不通"的标准是有没有 HTTP 应答而不是状态码**（404/403 都算通，否则内网应用会大面积误报，而误报的代价是用户从此忽略这个提示）；**预检永不阻断创建**。⚠️ 探测结果存**新列 `probedTarget`**，绝不覆盖 `target` —— 第一版写进 `target` 被 `test_target_is_normalized_before_storage` 当场抓住（归一化是**剥掉**协议，写进去会让 `example.com` 与 `https://example.com` 又变成两条记录）。④ **要求 agent 用中文推理**：外部工具的推理过程一直是英文，而"它到底在想什么"正是用户要看的。`_instruction()` 现在明确要求中文输出推理与结论，**但命令行 / URL / 路径 / 代码 / payload 一律保持原样不翻译**（翻译它们会直接毁掉可执行性）。⑤ **顺带修复测试隔离的一处漏洞**：`_clean_state` 用 `Base.metadata.sorted_tables` 拼 TRUNCATE 清单，而它跑在 `client` fixture **之前** —— 那一刻 `app.main` 还没被导入，metadata 里只有恰好被直接 import 过的表，`admin_accounts` 因此漏在清理之外。症状极具迷惑性：**单独跑某个文件必错、全量跑永远通过**（全量跑时前面的文件早就把模型 import 齐了），报错却是下一个用例 setup 阶段的 `409 SETUP_ALREADY_DONE`。我最初照"是不是顺序污染"去查，方向完全错了。改成从 `pg_tables` 读清单（排除 `alembic_version`，清掉它会让下次 `upgrade head` 重建已存在的表）。修完全量 **889 passed / 0 failed / 0 error**。⑥ 迁移 `b39e890e4b53`（卡死检测三列）、`a1a0945e265f`（`probed_target`）。 |
| v0.18 | **任务 10 第四批（P6）：漏洞软删除 + CSV 导出 + 统计跟随筛选 + 附件**（漏洞工作流的原设计清单到此收尾）：① **§3.12 统计跟随筛选** —— `GET /findings/stats` 现在收**与 `/findings` 完全同一套**筛选参数。★ 改动的真正理由不是"加几个参数"，而是**它原本自己另写了一遍筛选逻辑**：当时只有一个共同参数（`scanId`），看起来"没重复多少"—— 而那正是最危险的时候，再加第二个参数两份口径就会漂移，症状是"列表筛出 3 条、角标写着 7 条"，而用户无法解释这个差值，只能不再相信角标。现在两者编译条件走**同一个** `_filter_conditions`，"列表长度 ≠ stats.total"在结构上不可能出现（有测试盯着这个等式）。② 同节新增 **`facets` 开关** —— 这是本功能最容易做错的一处：角标有两种含义，"`facets=false`（默认）只要当前筛选结果的计数"vs"`facets=true` 算某一维度时忽略那一维自己的条件"。**需要后者是因为筛选器否则会把自己锁死**：用户筛了「高危」想改看「中危」，若中危角标是 0 且按钮禁用，他根本切不过去 —— 必须先取消高危。这不是"数字准不准"，而是筛选器还能不能用。两条边界写死了：`total` 永远是"筛出来几条"（facet 只改角标不改结果集）、只忽略"自己那一维"（搜索"登录"时角标就该只统计命中的那些，否则角标会指着一批被搜索排除的条目）。默认 `false` 让 Overview 与既有调用方**行为完全不变**。③ **§3.13 漏洞软删除** —— 与扫描模块**完全同一套语义**（§3.3）：`batch/delete`、`batch/restore`、`/{id}/purge`、`?deleted=true`，响应同形 `{changed, skipped, findings}`。`deleted_at IS NULL` 是**所有查询的默认条件**（列表/统计/分组/导出/Overview），而且它写在唯一的筛选条件构造器里 —— 漏一处就会出现"删了还在计数"，那是用户最不能接受的不一致。三条刻意的决定：**软删除不检查报告引用**（软删除不破坏报告：冻结的是 id 与快照，被软删的漏洞仍能渲染；拦截在这里会让"这条是误报，我不想再看到它"变成做不到的事）；**恢复不动处置状态**（漏洞在回收站期间状态没变过，顺手改成 `new` 会抹掉分诊成果）；**软删除不动附件**（进回收站不是销毁证据）。永久删除三条闸：必须先软删除（`409 FINDING_NOT_IN_TRASH`）、被报告引用时拒绝（`409 FINDING_REFERENCED_BY_REPORT`）、**附件要一起清掉**（`ON DELETE CASCADE` 只删行、不删磁盘文件）。新增审计 `finding.delete`/`finding.restore`/`finding.purge`。④ **§3.14 CSV 导出** —— `GET /findings/export`，**筛选参数与 `/findings` 完全一致且走同一个 `list_findings`**：忽略筛选的话用户筛出 3 条高危、点导出拿到全部 200 条，而他会把那份表**发出去** —— 错误的数据比没有数据更糟。**走后端而不是像扫描列表那样前端生成**：列表返回的是摘要（不含 `detail`），前端要生成就得逐条再取详情（N 个请求）。带 **UTF-8 BOM**（不带的话简体中文 Windows 的 Excel 按本地编码读，中文列名乱码，而"导出给别人看"正是它的用途）。**值用枚举不新造中文标签表**：后端至今没有任何一处把 severity/status 翻成中文，新造一份就多一处会漂移的口径。⑤ **§3.15 附件** —— 增删查四个端点。★ **存储路径完全由服务端从 id 生成，与上传文件名无关**：用文件名拼路径会同时打开两个洞 —— 路径穿越（`../../etc/passwd`）与**互相覆写**（两人都传 `screenshot.png`，后者静默覆盖前者，而"证据被另一个人的截图替换了"不可接受）。扩展名走**白名单**不用黑名单（黑名单永远漏 `.php5`/`.phtml`，而目标不是"拦住危险文件"——附件不会被服务端执行——而是"确保下载时类型可信"）。**响应不含 `storedPath`**（客户端没有任何用途需要知道它）。两条顺序规则其实是同一条原则的两种应用：**写的时候先文件后库、删的时候先库后文件**，为的是"让用户可见的状态保持一致，宁可留孤儿文件"（反过来会得到"行指向不存在的文件"或"行还在、文件没了"）。大小按**真实字节数**判而不是 `Content-Length`（后者可以被客户端撒谎）。下载**强制 `Content-Disposition: attachment`**，这对 `image/svg+xml` 尤其要紧：SVG 可内嵌 `<script>`，内联渲染等于让一个上传的文件在本站原点上执行。附件不能跨漏洞读。新增审计 `finding.attach`/`finding.attach_delete`。⑥ ⚠️ **撞上一处真实的设计冲突并显式解决**（§3.15.1）：既有的 CSRF 防线**拒绝一切表单类 Content-Type 的写请求**（跨站表单是最经典的 CSRF 载体，而"本 API 不收表单"让整类攻击无处落脚），而附件上传**必须**走 multipart。修法是**显式登记表** `MULTIPART_WRITE_ENDPOINTS`，三条边界：只对表内端点放行（全局放开等于把整层纵深防御拆掉）、**Origin 检查在这之前已执行**（放行的只是 Content-Type 那一道，跨源保护照旧生效）、以及**配套一条自检测试**遍历 OpenAPI 规格找出所有收文件的端点并断言它们都登记了。最后那条才是真正的保险 —— 那张表会腐烂，而症状（上传被 400 挡掉，消息是"写操作必须使用 Content-Type: application/json"）与"忘登记"看起来毫无关系。我的自检测试第一版**遍历 `app.routes` 找 `APIRoute`**，在本项目的 FastAPI 版本上**一个都匹配不到**（14 个顶层路由里 11 个是 `_IncludedRouter`），失败的样子是"探测到 0 个上传端点"；改走 OpenAPI 规格后既正确又更稳（它是对外承诺的契约，不随内部路由对象结构变化）。⑦ 迁移 `99b1799eb3c6`（`findings.deleted_at` + `finding_attachments` 表 + 两个索引）。⚠️ `ix_findings_deleted_at` 是**手写的部分索引**（`WHERE deleted_at IS NOT NULL`）：autogenerate 认不全部分索引，而且给少数派（回收站条目）建索引才有价值 —— 给多数派建等于给几乎每行建条目。⑧ 配置新增 `SUPEX_ATTACHMENT_ROOT` / `SUPEX_ATTACHMENT_MAX_BYTES` / `SUPEX_ATTACHMENT_MAX_COUNT`；测试基建补上 `SUPEX_ATTACHMENT_ROOT` 指向临时目录（**少了它测试会把文件写进项目根的 `_uploads/`**，而"忘了设"的表现不是测试失败、是仓库里多出东西）。⑨ 新增 `tests/test_findings_p6.py`（37 条）。 ⑩ ⚠️ **实机验证时抓到两个只有真浏览器能暴露的缺陷**（类型检查与 lint 全绿，所以它们一路溜到了验收）：**其一，时间线图标表漏了 `assigned`** —— 那个 kind 是 v0.15（任务 10 P2 的指派）后端就在写的，而前端 `TIMELINE_ICON` 没跟上。后果不是"图标不显示"，而是 `TIMELINE_ICON[kind]` 得到 `undefined`、`<Icon />` 让 React 抛 "Element type is invalid"，**打开任何一条被指派过的漏洞整个后台白屏**（`#root` 被卸载成空的）。修法两道：补齐已知 kind，**且未知 kind 一律降级**成中性图标 —— 后者才是保险，将来后端再加 kind 时后果应该是"图标不太好认"而不是"后台打不开"。**其二，全站原本一个错误边界都没有** —— 这正是上一条能造成白屏的原因：渲染期异常无人接住，React 会卸载整棵组件树。已补**两层**：顶层（`main.tsx`，挡外壳自己崩，含路由/Layout/守卫）与页面层（`App.tsx`，挡某一个页面崩 —— 那时导航还在，用户能自己走开）。页面层刻意**不套 DOM 包装**，否则会掐断 `PageFallback` 依赖的高度链。两条都是"一个数据值的疏忽不该让整个产品不可用"。 ⑪ `_uploads/` 加进 `.gitignore`：附件是**用户上传的业务数据**（内网截图、抓包），与 `.env` 同一个性质 —— 真实值绝不入库。 |
| v0.19 | **任务 12：把"任务是什么"交给 agent**（触发点是一句用户原话：**「只给网址让他扫描太没意义了，像这个 CTF 题目，有题目描述的我要给他」**，同一次反馈里还有"活动页是空的"）：① **§3.16 新增 `brief`（任务背景）与 `goal`（成功判据）**。`brief` 是用户提供的自由文本，**原样**交给 agent、我们一个字不改写 —— CTF 题面里的考点、提示、**诱饵**、flag 格式，我们猜不出来，而它们对结果的影响远大于任何扫描参数。它同时是通用的（渗透测试也要交代范围与测试账号），所以不是一组 CTF 专用字段。`goal` 与 `targetKind` 是**两个正交维度**：前者决定"怎样算成功"，后者决定"目标是什么形态、怎么测" —— 一个 CTF 靶机是 `url` 形态、扫法和普通网站完全一样，所以 `goal` 不能并进 `targetKind`（合并会让"目标形态"与"任务目标"互相污染）。三个取值：`findVulns`（默认）/ `captureFlag`（深挖一条通向 flag 的路径，而不是枚举低危；要求显式写出完整 flag）/ `verifyFix`（逐一验证**已知的**那几条，新发现单独列在最后）。`rerun` 继承两者。② **注入顺序即优先级**：`brief` 排**最前**，然后才是判据、技能范围、中文输出要求 —— 用户交代的是"这次任务"，我们那套通用范围说明是背景知识。`brief` 被显式分隔包起来（"以下为用户提供的任务背景，请作为**参考资料**理解"），这不是防注入（用户就是授权方），而是让模型正确理解这段文本的角色 —— 题面本身可能含攻击性文本，那正是题目内容。③ **★ 指令从命令行改走文件（`--instruction-file`）** —— 这是一个**安全修复**，不只是绕开长度限制：原来指令拼在 `--instruction` 里，而 `brief` 会含测试账号（strix 官方文档自己举的例子就是 `admin:password123`），**拼进 argv 等于把凭据摊给同机所有用户与任何日志采集**（`ps aux` 一眼可见）。顺带解决转义 —— 题面里的引号、反引号、`$`、`%hhn` 在拼 argv 时会被静默改写。文件权限 `0o600`（Windows 上 POSIX 权限位不适用，如实标注而不是假装生效）。**不再保留 `--instruction` 那条路径**：两套并存意味着迟早有人往旧的那条里加东西。`LaunchSpec.summary()`（会进日志）**排除 `brief`** —— 日志的保留期与可达面都远大于数据库那一列。④ **§3.17 题目附件**，走 strix 的 `--workspace-file` 投进沙箱 `attachments/`。⚠️ 与漏洞附件（§3.15）是**两套规则**：那个是处置证据（人在浏览器里看，扩展名白名单、存储名用 id），这个是题目输入（**机器**在沙箱里分析，任意类型、无扩展名如 `chall` 也必须收、**必须保留原名**因为 agent 靠名字认东西）。安全靠文件名消毒 + 落点校验 + 同名加 `-2` 后缀（**不覆盖** —— 覆盖掉的可能是这次分析唯一正确的那个附件）。⑤ ⚠️ **一个被测试当场抓住的顺序死结**：我第一版设计是"先建扫描、再往 `/{id}/attachments` 传"，而扫描创建后**毫秒级**被调度器认领（`_nudge_scheduler`），所以那条路稳定 409 —— **附件功能等于不存在，而它看起来完全正常**（端点都在、单独测每一个都过）。改成**先暂存、创建时认领**（`POST /scans/attachments/staging` → `stagingId` → 放进创建的 `attachmentStagingId`），认领是**移动**所以"一次暂存只能被认领一次"由文件系统表达；暂存 id 失效**不让创建失败**（附件丢了是次要问题）。⑥ ⚠️ **multipart 登记表踩了自己预告过的坑**：上传端点是 CSRF 防线的显式例外，那张表原本用**后缀匹配**（`endswith("/attachments")`），而新路径 `/scans/attachments/staging` **不以 `/attachments` 结尾** —— 于是它没被放行，上传被 400 挡掉，而错误消息（"写操作必须使用 Content-Type: application/json"）与"忘了登记"看起来毫无关系。已改成**精确匹配路由模板**，并记下一个反直觉细节：登记表**不带 `/api/v1` 前缀**（前缀是 `include_router` 挂载时加的，`request.scope["route"].path` 是子路由自己的模板）—— 我第一版按完整对外路径写，结果**一条都匹配不上**，症状是所有上传端点突然都 400。⑦ **顺带修正一条既有的错误说法**：我一度在注释里写"`sweep_retention` 会按保留期清 `scan_root/<scan_id>`"，实测**它根本不删任何目录**（只改数据库）—— 于是暂存清理是**新加**的（`sweep_staging`，2 小时 TTL，挂在 `sweep_retention` 里），并且它必须精确匹配 `_staging` 那一级（写错一个 `**` 就会删掉用户真正在用的附件，有测试盯着）。同时把那个**既有缺口**写下来：扫描产物目录只增不减，此前被那句错误注释"解释掉"了。⑧ 前端新增向导第 4 步「任务背景」（判据选择 + 题面文本框 + 附件选择），并如实提示"含口令也可以写：它不会出现在进程列表里" —— 不告诉他这一点，他会自己删掉最关键的那一行。⑨ 迁移 `d7beeb9712a9`。⚠️ autogenerate 生成的那句**在非空表上会直接失败**（`goal` 非空却无 `server_default`），是手工改的：先带 default 加列、再摘掉 default（保留 default 会让"忘记传 goal 的插入"静默变成默认值）；CHECK 约束也手工补（autogenerate 不比对约束内容，本项目已踩过一次）。⑩ 配置无新增；新增 `tests/test_scan_brief_and_goal.py`（31 条）。 |

| v0.20 | **补记：把契约文档拉回代码现状**（触发点是 `npm run verify:contract` 报红：`TargetKind 契约缺: artifact`）。这一行同时记下一件更该被看见的事：**这份文档的变更表停在 v0.19（任务 12），而代码已经走到任务 20** —— 中间那批（CTF2 判题引擎、题目附件、夺旗目标、`service` 目标形态…）的实现都改了契约面，却没有回写这里。本次只补与当前改动直接相关的四项，其余待专项对账：① §2.1 `targetKind` 补 **`service`** 与 **`artifact`** —— `service` 其实早就实现了，只是因为校验脚本按**子串**在整个文档里找，而"service"这个词在别处出现过，于是它一直**没被报出来**（这也是该校验的已知弱点）；② §2.1 `skills` 补 **`protocol`**（唯一只适用 `service` 的技能，理由见 §2.1 表格上方那段）；③ §2.1 新增 **`Scan.flag`**，并新增 **§3.18.1** 写清它的来源与"占位符必须拒掉"这条判据；④ 新增 **§3.3.2 续跑** 与 **§3.18 题解报告** 两节 + 两个错误码（`SCAN_NOT_RESUMABLE` / `WRITEUP_UNAVAILABLE`）。四项都是**先改文档再改代码**的规矩在本轮的事后补齐，不是新决策 |

| v0.21 | **专项对账：把契约文档拉回代码现状（第二批）** —— 这是本项目**规模最大的一次文档补齐**，也是一次**事后追认**，不是新决策。触发点是 v0.20 那行只补了四处，而实际漂移远大于此：交付评估时逐项量化，发现整个 CTF2 模块（25 个端点、5 个枚举、24 个出入参、8 个迁移）**在契约里零覆盖**，而契约是本项目声明的「唯一事实来源」—— 也就是说有一大块对外接口从未被写下来过，前端却已经按它接完了。**本轮量化出的缺口与补齐情况**：① **新增 §2.10**（CTF2 字段对照表：5 个枚举 + 14 个出参形状）与 **§3.19**（CTF2 端点总表 25 个 + 提交策略 + 提交前校验 + 平台接入 + 与扫描模块的关系）。其中三条是**非显然的设计意图**，必须写下来：`Ctf2ChallengeStatus` 为什么不抄平台侧状态（会无法表达"平台说未解出、而我们正在跑"）；`rejected` 与 `wrong` 为什么必须分开（前者本地拦下不消耗额度、后者真发了请求消耗额度，合并会让"还剩几次"被白白扣掉）；`submitMode` 为什么默认 `safe`（flag 提交有**不可撤销的外部后果**，"自动"不等于"无需人工确认"）。② **§2.8 补 `service` 与 `artifact` 两类归一化与校验**（此前只有 url/repo/path 三行），并新增 **§2.8.0 粘贴识别**（`nc 10.0.0.5 9999` / `openssl s_client -connect …` 等命令形式直接识别 —— 这类题的题面给的就是一条命令，让用户自己抠 host 和 port 是把他当解析器用）与 **§2.8.2 TCP 预检为什么不写 `probedTarget`**（TCP 探测**只握手、连上后一个字节都不发**：CTF 靶机有状态，探测时发包可能消耗掉一次答题机会；而"没有协议前缀"这个概念，硬补 `http://` 等于编造一个不存在的层）。③ **§2.9 补执行三件套**（`maxConcurrentScans` / `scanMaxTurns` / `scanMode` + 只读的 `executionSource`），并写清一件**界面上看不出来的事**：沙箱配额四个字段里 **只有三个真生效**，`timeoutMinutes` 与 `allowVolumeWrite` 是"能点、能存、但不生效"的（执行后端全包没有扫描级墙钟超时，工作区挂载恒为读写）—— 这类字段比缺失的字段更糟，用户会以为设了就有效。同时记下**配额只接了扫描一条路**，CTF2 那条是任务 21 才接的。④ **§3.7 补两个漏记的端点**（`GET /settings/sandbox` 此前只记了 PUT；`GET/PUT /settings/finding-sla` 是 v0.16 就有的，一直没进表）。⑤ **§3.9 审计动作表从 8 个补到 31 个** —— 此前只列了任务 8 那一轮的六个加两个报告动作，而代码里已经有 31 个。补记时按"事后要能回答什么问题"分组，其中 `ctf2.settings_update` 单独点出：它记的是**谁把 `submitMode` 从 `safe` 改成 `auto`**，那是本系统里少数几个"一次点击改变外部后果边界"的操作。⑥ **错误码表补两个**（`SCAN_IS_CTF2_SHELL`、`SETUP_ALREADY_DONE`）。**⚠️ 同时记下两个必须被看见的问题**：**其一，变更记录与代码的任务编号体系对不上** —— 代码里同时存在两套编号（`alembic/versions/20260927_1200_*.py` 写「任务 17 = artifact」，而 `app/core/vocab.py:469` 写「v0.20（任务 17）：CTF2 平台对接」），CTF2 实际是**独立的版本线 v0.20–v0.27**、与任务编号并行，那一行注释是笔误；本轮按迁移文件与测试表格的证据（三处一致）认定「任务 17 = artifact」，**但这是推断而非坐实** —— 项目只有一次基线提交、无历史可查，所以如实记在这里而不是悄悄改掉。**其二，`ScanMode` 不在自动对账清单里** —— `suvex-web/scripts/contract-parity.mjs` 的 `ENUM_CHECKS` 没有它，所以它即使与代码漂移也**不会报红**；这与 v0.20 那次发现的"校验脚本按子串匹配，导致 `TargetKind` 缺 `service` 一直没报出来"是同一类弱点：**对账脚本只覆盖它被喂过的枚举**。⑦ **本轮明确记为"不改契约面"的两项**（免得后来者以为漏记）：任务 13 的 `EXPLORATION_PROTOCOL` 与任务 15 的 `TCP_TARGET_CLAUSE` 都是**纯提示词注入**，不进任何字段、不进命令行，只改变 agent 的过程 —— 前者是为了治"一轮没产出新信息的空转"（实测一次 CTF 扫描 61.5 分钟、166 次 LLM 请求，时长几乎全是 LLM 等待，溯源到子 agent 对未知 slot 布局做无假设穷举），后者是因为执行后端对"含点且不含斜杠"的输入一律判成 web 应用并补 `https://`，而 `--target` 没有任何写法能表达裸 TCP |

| v0.22 | **最后六个 501 清零 —— 设置页不再有死块**（触发点：交付评估时量化出"6 个端点返回 501，全部集中在通知与设置快照"，而那是企业用户打开设置页就会撞上的东西）。**做法与边界**：① **`GET /settings` 真的返回完整四块**（accounts / sandbox / notices / tokens）。它此前长期 501，理由是"缺三块却回 200 会让调用方以为拿全了" —— **那条判断至今成立，本轮没有推翻它**，推翻的是它的前提（四块现在都拼得出来）。⚠️ 规矩留着：只要还有一块拼不出来，这个端点就该继续 501；回一个缺字段的 200 比 501 糟得多。响应里**没有** `status` / `error` —— 它们是前端 `SettingsSnapshot` 上的本地加载状态，服务端下发等于替前端声明它自己的状态机。② **新增 `GET /settings/notices/rule` 与 `GET /settings/notices/channels`** 两个读端点（此前只有写，读只能靠 501 的快照）；渠道列表返回**裸数组**，与 `GET /settings/tokens` 同形状，前端两处共用同一个渲染组件。③ **规则与渠道保持两个端点、两个维度**：合成一个对象会让「邮件收全部、Slack 只收严重」这种运维常态**无处表达**。④ **四条实现约束**（都写进了 §3.7 的通知小节）：`verified` **不接受客户端传**（schema 里就没有这个字段，`StrictModel` 会 400 并点名）；★ **改 `target` 必须把 `verified` 打回 `False`**（它的含义是"**这个地址**验证过"，地址变了事实就不成立 —— 漏掉的症状是"显示已验证、真出事时一条都没发出去"），而改 `label`/`enabled` 不影响验证状态；**停用的渠道仍然列出**（停用是状态不是删除，隐藏它会让用户以为配置丢了；这与 tokens 的"已吊销不列出"**不同**，因为已吊销的令牌留着会造成"它还能用"的误解）；**`POST .../test` 本轮只做本地校验、不真的往外发** —— 真实发送需要 SMTP / Webhook / Slack 三套协议实现，是独立的一块工作，而**假装发了是禁止的**（会让界面显示已验证而实际发不出去），所以它校验地址形状并把结论如实写回 `verifyError`，形状判据**刻意宽松**（配置校验不是安全边界）。⑤ **`minSeverity` 走白名单校验**：DB 上有 CHECK 兜底，但那条会抛 Postgres 的约束违反错误，用户看不懂，所以路由层先校验并给中文原因。⑥ 新增 `tests/test_settings_notices.py`（16 条），其中三条专门盯"最容易漏"的地方：`verified` 传不进去、改地址会重置验证、改名字**不会**重置。**顺带**：反向端点对账脚本（v0.21 加的）自动发现端点总数从 103 变成 **105** 并确认契约已覆盖 —— 这正是它存在的意义（人工清单不会注意到新增了两个端点） |

| v0.24 | **多用户与两档角色**（触发点：交付评估时把"单管理员模型"列为缺口之一，用户选定方案 B —— 多用户单租户 + 管理员/只读两档）。**① 数据模型**：`admin_accounts` 加 `username`（NOT NULL + UNIQUE）与 `role`。表结构其实早就为这一刻留了余地（id、时间戳、sessions 关系都在，当时的注释就写着"将来真加第二个人时，改的是插入逻辑而不是表结构"）—— 但漏了两件事，因为单账号时代**它们不需要存在**：登录要用 `username` 区分是谁，权限分档要用 `role`。**② 迁移 `c1f5a83d2e07` 的关键是存量回填**：`username='admin'` + `role='admin'`。取值不是随意的 —— 回填成 `viewer` 会让升级后**没人能改设置**（一个自锁）。做法是先加可空列、回填、再设 NOT NULL（不能直接加 NOT NULL：Postgres 的检查发生在语句执行时，存量行会立刻违反）。**③ `role` 刻意不加 CHECK 约束**（与项目里其它枚举列不同）：它的取值集合将来最可能变（加一档"能扫描但不能改设置"很自然），而每加一档就要写一个迁移重建 CHECK。未知角色由 `scopes_for_role` 兜成**空集**（403）而不是默认给 `read` —— 安全的方向是"什么都不给"。**④ `require_admin_session`**：用户管理的四个端点不接受机器令牌。**这不是"更严一点"，而是一条明确的安全边界** —— 管用户是"对权限本身的操作"，令牌若能做这件事就能**自我放大**（创建一个新管理员、或改掉签发者的角色），而令牌是长期有效的（可设永不过期），泄漏一个就等于永久拿到用户管理权。**⑤ 角色变更立刻生效**：`resolve_identity` 每次请求都读一次账号（主键查询），而不是把角色缓存在会话里。这不是性能取舍，是安全要求 —— "我刚把他降成只读，他还在发起扫描"是一个真实的权限漏洞；缓存在会话里会让降级延迟到该会话过期（默认 12 小时）。**⑥ 三条防自锁规则**（删最后一个管理员 / 删自己 / 降级最后一个管理员），各带专门的错误码，因为前端要给出不同的补救动作。**⑦ 顺带修掉两处**：`read_me` 原来调 `get_account(db)` 取"库里第一个账号" —— 单管理员时代没问题，多用户之后会让**每个用户都看到同一个人的身份**（包括 `role`，而前端要据此决定显不显示管理入口）；`GET /providers` 从 `admin` 放宽到 `read` —— 它原来在矩阵里的论证（"返回部署配置的形状"）在单管理员时代无害，但引入 viewer 后变成真问题：前端顶部状态条读它显示"当前模型"，若要求 admin，viewer 登录后状态条会报错，而那个错误看起来像"系统坏了"。放宽是安全的：这个端点不返回任何明文凭据（`apiKey` 恒为空串，只有 `apiKeyMask`）。**⑧ 新增** `tests/test_users_and_roles.py`（23 条）与 `src/data/users-store.test.ts`（12 条），并做过**突变验证**：9 处关键防线逐个破坏、确认对应测试变红（其中 2 处第一轮**没变红**，暴露出"未知角色兜成 read"与"时序侧信道"两个测试盲区，已补）。**⑨ 契约**：新增 §2.11（用户与角色）+ 错误码 3 个 + 审计动作 3 个 + §3.1 端点表补 4 行 |
| v0.36 | **CTF2 题解产出 + token 消耗统计**（触发点：CTF2 题目卡上的「题解」入口点开得到「这次扫描没有留下报告文件」，而那句提示在**误导用户**）。**① 题解不走 scans 那条路**（新增 §3.19.5）：CTF2 解题由 `ctf2/runner.py` 自己的循环驱动、**从不产生 `penetration_test_report.md`**，所以扫描的题解端点对它**永远**找不到东西。修法是 `GET /scans/{id}/writeup` 认出 `origin="ctf2"` 的壳并返回 409 指路，同时新增 `GET /ctf2/challenges/{id}/writeup`。**② flag 不脱敏**（与「方法卡」恰好相反）：方法卡会注入**另一道题**的 prompt（带 flag 就是泄漏），题解是赛后**提交给平台**的（flag 正是要展示的）。两者共用 `key_steps_from_transcript`，所以给它加了 `sanitize` 参数，默认 `True`（偏向危险的那一侧：忘了传只是题解少了地址，反过来是方法卡泄漏靶机地址）。**③ token 统计**（新增 §3.19.6）：每次 LLM 调用一行（`ctf2_solve_usages`），不是"在尝试上加累计列" —— 累计列**永久丢失**"哪一轮最贵 / 换模前后各花多少"这类明细。**④ 实测发现该网关的 `usage` 同时给两套口径，而其中一套是坏的**：`output_tokens` 恒为 0（照 Anthropic 那套读会**静默少算全部输出**），所以只认 `prompt_tokens`/`completion_tokens`/`total_tokens`。**⑤ `calls` 与 `recordedCalls` 必须分开**：接入点没给 usage 时 token 记 0，而"真的是 0"与"没记录到"在界面上长得一样（同 §2.10 的 `hasKey`/`apiKey`、`candidateFlag`/`confirmedFlag` 那条纪律）。另加 `usage_reported` 列区分二者。**⑥ 不做 token → 金额换算**：单价随模型/供应商/时段变，写死会误导。**⑦ 历史如实标注**：本功能上线**之前**的解题没有 usage 记录，统计接口带 `since` 说明从哪一刻开始，**不伪造历史**。**⑧ 2026-10-15 修正三处**（本轮实现 + 复核发现）：`GET /ctf2/challenges/{id}/writeup` 的 409 判据从 `status == unsolved` 改为**受理快照存不存在**（旧写法会让一道**正在跑**的题被报成"解题记录早于 v0.36"，是假话）；§2.10 的三行字段表比实现短（`Ctf2Writeup` 缺 `turns`/`elapsedSec`/`updatedAt`、`Ctf2TokenUsage` 缺 `since`、`Ctf2UsageEntry` 缺 `reasoningTokens`）已补齐。**⑨ 新增测试** `tests/ctf2/test_writeup.py`（纯拼装、含"flag 不被脱敏"的回归钉子）、`test_usage_and_writeup.py`（落库与聚合口径）、`test_usage_endpoints.py`（端点字段名逐字核对 + 五个中间态的回归钉子） |
| v0.37 | **题解回填 + token 粒度**（触发点：用户截图那道 `[极客大挑战 2019]Havefun` 弹「这道题还没有题解」，而它 10-01 就已解出）。**① 回填 26 道**：`runner` 收尾时**无条件**调 `save_writeup`，但那一句是 v0.36 才加的 —— 所以之前解出的题**从来没有题解**（实测 `solved=26` 而 `writeups=1`）。修法是 `scripts/backfill_ctf2_writeups.py`：从 `_ctf2_solves/` 的 transcript 重建 outcome 并重跑 `build_writeup`。**26 道全部可回填**（transcript 26/26 都在），幂等。⚠️ **`turns` 不能靠"数 `[turn N]` 行"** —— 实测一个文件里那类行 210 行而真实轮数 76（一行能塞多轮），要取**最大 turn 号**。**② `byAttempt`**（见 §2.10）：`byChallenge` 按题聚合，而一道题可解多次（Upload 解过 4 次）。**③ 判据补 `scan_id`**：`has_solve_attempt` → `was_solve_started`，因为 `ctf2_solve_attempts` 是 v0.28 才建的表，只看它会让 **20 道平台判过对的题**被报"还没有解过"。 |
| v0.38 | **题解质量重做**（触发点：用户原话「解题wp写的太过简陋，要求规范一点」）。**★ 根因不是排版，是内容被丢弃**：`runner.parse_final` 一直能解出完整结论文档，但收尾只取了 `candidates`，`summary` / `tried` / `ruled_out` **解析完就丢掉了**（`SolveOutcome` 当时没有对应字段）。实测 `[ACTF2020 新生赛]Upload`：题解 5873 字符，而模型写了「用响应长度做旁路：baseline=8628；上传 .php→8532…」，在题解里 grep 那些关键词**全部 0 次命中**。**① 捕获三字段**：`SolveOutcome` 加 `summary`/`tried`/`ruled_out` + 新函数 `extract_final_analysis`（容错：缺字段/单字符串/数组混非字符串都不抛）。**② 版式重做**（见 §3.19.5）：五节正文 + 三个附录，头部一眼看到结论。**③ 修 160 字符截断**：`key_steps_from_transcript` 加 `clip` 参数（题解 800、方法卡仍 160）；⚠️ **判据始终按 160 那版算**，所以放宽它**不改变方法卡行为**。**④ 重生成 28 份历史题解**：81814 → **115829 字符**（+34KB 被丢弃的内容回来了），覆盖 summary 28 / tried 22 / ruled_out 19。**⑤ 表格 `\|` 转义**（生成侧责任：GFM 下单元格里的裸 `\|` 会拆列），但**代码块里的 `\|` 绝不转义**（shell 管道）。**⑥ 版式最深 h3** —— 明确告知渲染侧不依赖 h4。**⑦ 一处实测踩到的坑**：重生成脚本第一版传 `now=row.updated_at` 做幂等锚点，而 `updated_at` 带 `onupdate`、每次写库都被刷新 → **永远差两秒、永不幂等**；改用 `generated_at`（只在 INSERT 写）后，重跑第 2/3 次均为"重生成 0 / 内容未变 28"。 |
| v0.39 | **线上暴露面加固**（触发点：一份对 `http://suvex.asia` 的只读审计 F1–F8，全部实测复核属实）。**★ 根因是"开发服务器当生产用"**：隧道映射到 `127.0.0.1:5173`，而那是 `npx vite --host 0.0.0.0` —— 实测 `GET /@vite/client` 返回 **210,597 字节**、`/src/*.tsx` 直接返回 **TS 源码 + sourcemap**。**① 换生产构建**：`dist/`（6.0M / 149 assets / 无 .map）经 nginx 静态服务，`/api` 反代到 `127.0.0.1:8000`。⚠️ 静态根**不能**用 `/root/suvex/.../dist` —— nginx worker 以 `www-data` 跑而 `/root` 是 700，会**全部 403**（实测 `stat() failed (13: Permission denied)`）；正解是 bind mount 到 `/opt/suvex/suvex-web/dist`（**不能复制**，复制会在下次构建后静默变旧）。**② `Host` 必须 `$http_host`**（不是 `$host`/`$proxy_host`）：`enforce_csrf` 用 `request.url.netloc` 算 `own`，`$host` 会丢端口 → 所有写操作 403。**实测**：正确 Origin → 越过 CSRF 到达业务校验（400 字段校验）、恶意 Origin → **403 `CROSS_ORIGIN_BLOCKED`**。**③ SSE** `proxy_buffering off` + `gzip off`。**④ 三处暴露面收敛**（见 §3.8）：docs/openapi **默认关**、metrics **默认关**、sandbox 镜像名**默认空**。**⑤ F4**：OpenAPI 补 `securitySchemes`（原来 `null`）。**⑥ 显式 404 掉 dev server 路径**（`/@vite`、`/src/` 等）—— 否则会走 SPA 回退返回 200，让扫描器以为"这些路径存在"。**⑦ 一条"假防线"的发现**：通用模板里 `/metrics` 的 `allow 127.0.0.1` 在**隧道拓扑下无效**（frpc 从本机连入，`$remote_addr` 恒为 127.0.0.1）—— 那种"看起来有防线、实际没有"比不写更糟，所以本部署改用应用层默认关闭，并在配置里写明为什么。**⑧ 未做（等用户定）**：HTTPS（F2）、`_allowed_origins` 无条件信任 `X-Forwarded-Host`（反代侧已刻意不发该头以缩小伪造面）、SSRF 黑名单绕过、302 重定向绕过白名单。 |
| v0.40 | **CTF2 平台地址的两条绕过修复**（触发点：RedTeam 对 Suvex 自身做渗透，**两条都原型验证过、未消耗任何 CTF2 额度**；它们正是 v0.39 ⑧ 里记着"未做"的那两条）。**① SSRF：等价 IP 写法绕过**（发现 1）。原判据是**字符串比对** `parsed.host.lower() in BLOCKED_HOSTS`，而 `169.254.169.254` 有 `2852039166`（十进制）、`0xA9FEA9FE`（十六进制）、`0251.0376.0251.0376`（八进制点分）、`[::ffff:169.254.169.254]`（IPv4-mapped IPv6）这些 **`getaddrinfo` 全认、字符串一个都不等**的写法 —— 实测**全部放行**。一次 GET 就能换到云主机临时凭据，而"让服务端替你打一个 URL"正是这个功能的形状。修法是**两种判据并存**（不是替换）：域名仍走 `BLOCKED_HOSTS` 字符串比对（`metadata.google.internal` / `metadata.goog` 是**域名**，IP 归一化分支对它们返回 `None`，删了就把它们放过去了 —— RedTeam 第一版原型正栽在这里）；字面量 IP 先归一化（三条分支：`ipaddress.ip_address` → `int(h, 0)` → `socket.inet_aton`，外加剥 `[]`、剥尾点、`ipv4_mapped` 映射回 IPv4）再判**网段** `169.254.0.0/16` 与 `100.100.100.200/32`。判据实现搬到新模块 **`app/ctf2/hostguard.py`** —— 因为发现 2 的两个模块也要用它，而 `platforms → builder → generic → transport` 已成链，反向 import 会成环。（⚠️ 该模块在 **v0.41** 已整体移到 `app/core/hostguard.py`，因为 provider 那条路径也要用同一份判据 —— 见下。）**⛔ 刻意没有做成"禁止内网"**：比赛平台很可能就部署在内网，`192.168.*` / `10.*` / `172.18.0.1` / `127.0.0.1` **必须继续放行**（`app/ctf2/platforms.py` 的注释早就写明这条，见 §2.5）。**② 302 重定向绕过白名单**（发现 2）。`app/ctf2/adapters/transport.py` 与 `app/ctf2/adapters/probe.py` 都用了 `httpx.AsyncClient(..., follow_redirects=True)`，而 `assert_safe_base_url` **只校验用户填的那个 URL** —— 后续每一跳都不过校验。实测（本地靶子、走生产代码路径）：靶子 A `127.0.0.2:18080` 返回 `302 Location: http://127.0.0.1:18081/internal/secret`，httpx 跟过去，**把内网服务的响应体读了出来**。修法是 `follow_redirects=False` + **逐跳自己跟 + 禁跨 host**，新增 `app/ctf2/adapters/redirect.py`。★ 判据是**白名单**而不是"每跳过一遍元数据校验"：**"跳转目标必须与用户填的 host 相同"** —— 后者仍是黑名单思路，而黑名单正是发现 1 栽的地方。同 host 的 `http→https` **刻意放行**（不扩大攻击面，而真实平台常用它做规范化，禁掉会误伤）。自实现循环就必须复刻 httpx 的语义，否则会**悄悄改变行为**：`302`/`303`（及 `301`+`POST`）改 `GET` 并丢掉请求体，`307` 保留方法与 body，`Location` 可为相对地址。⚠️ `app/ctf2/client.py:539` 本来就是 `follow_redirects=False`（注释写着"重定向会把 Authorization 头带到另一个主机"），**没动它**。**③ 已知残余（如实记）**：`http://169.254.169.254.nip.io/` 这类**域名**写法仍可绕过（校验时它只是个字符串域名）—— 彻底堵住要在**连接层**校验解析后的 IP，已记进 `suvex-api/docs/security-checklist.md` 的 F-06。 |
| v0.41 | **上一轮修复的两处补漏**（触发点：Review 对 v0.40 做对抗审查，4 处发现里 **2 处确认要改、1 处判断被纠正、1 处是文档**）。**① 判据只有一份，两条路径都接上**（F-1，高）。上一轮修的是 **CTF2** 那条路径，而 **provider 那条原样还在**：`app/providers/llm.py` 用的仍是 `host in BLOCKED_HOSTS` 字符串比对，实测 `2852039166` / `0xA9FEA9FE` / `0251.0376.0251.0376` / `[::ffff:169.254.169.254]` / `169.254.169.254.` **五种写法全部放行**，外加 `100.100.100.200`（阿里云元数据）因为**清单里根本没有**而放行 —— 共 **6 条**。★ 根因不是"漏改一处调用点"，而是**判据有两份**：`llm.py` 那份比 CTF2 那份多 `169.254.170.2`（AWS ECS 任务元数据）、少 `100.100.100.200`，**同一类地址在一条路径上被拦、在另一条上放行**。修法：判据**只有一份**，落在 **`app/core/hostguard.py`**（`app/core/` 是叶子层，只 import 标准库；`app/ctf2/` 与 `app/providers/` **互不认识**，让一方 import 另一方是错的方向）—— 原 `app/ctf2/hostguard.py` 整体 `git mv` 过去，`llm.py` 自己那份清单删除，`platforms.BLOCKED_HOSTS` 保留为别名不破坏既有 import。⚠️ **`llm.py` 的第二层逻辑刻意没动**：它判"直接写内网 / 回环 IP"，受 `SUPEX_ALLOW_PRIVATE_LLM_ENDPOINTS` 控制（默认 `true`，因为本地 Ollama 就是 `http://127.0.0.1:11434`），与"元数据地址的等价写法"是**两件事** —— 合并会出事（开关默认是开的，合并等于默认配置下元数据地址就能打），所以加了 `test_the_two_guard_layers_stay_independent` 钉住它。**② 跳转后的 query 必须来自 `Location`**（F-3，中，真回归）。`request_same_host` 原来把 `send_kwargs`（含 `params`）原样透传到**每一跳**，于是每跳都重放第一跳的 query、把 `Location` 自带的 query **顶掉** —— 与它自己 docstring 承诺的"复刻 httpx 语义"不符。实测：原始 `params={page:1}`、`Location=/after?cursor=FROM_LOCATION&sig=abc` → 我们第 2 跳 `.../after?page=1`（丢了），httpx 原生 `.../after?cursor=FROM_LOCATION&sig=abc`。★ **触发面比看上去大**：`transport.request` 每次都传 `query = params + auth.query(key)`，而 **query-style 认证的 `auth.query()` 恒非空**（`spec.py:188` 是 `{name: prefix+key}`）→ **对 query-style 平台，每一次跳转都会丢 query**，症状是"某个平台跳转后同步不了"，看起来像平台的问题。修法与 httpx 的 `_build_redirect_request` 对齐（它构造下一跳时**根本没有 `params`**）：`params` 只在第一跳生效，之后 query 由 `Location` 决定。**③ 一处被纠正的判断**（F-4）：Review 报"畸形 `Location` 抛 `ExceptionGroup[OverflowError]`，调用点的 `except httpx.HTTPError` 接不住 → 500"，我第一版**在本机 Windows** 测到"httpx 返回 **502**、`anyio.connect_tcp` 抛 `OSError`、0 例 `ExceptionGroup`"，据此判它**不成立**并去纠正 Review。⚠️ **那个结论是错的、错因是测试环境**：在 **Kali（生产同款 Linux，同一 httpx 0.28.1）** 上，同一条路径冒出 **`ExceptionGroup[OverflowError]`** 且两条调用点都接不住 —— 已修，见 v0.43。**④ 文档**：`security-checklist.md` 的 F-06 拆成 **F-06（DNS rebinding）** 与 **F-07（通配 DNS 服务把内网 IP 编进域名）** —— 后者范围更大（`nip.io` / `sslip.io` / `traefik.me` 是**独立服务**不是变体，**任意内网地址**都能roup 的形状）同样 502；生产路径 `request_same_host` 跑 10 种畸形 Location，逃逸的**全部**是接得住的 `Ctf2Error` 或 `RemoteProtocolError`（`HTTPError` 子类），**0 例 `ExceptionGroup`**。**刻意不改代码**（没复现就放宽 `except` 会吞掉本该上抛的错误），只记进 F-08 并附逐层实测表。**④ 文档**：`security-checklist.md` 的 F-06 拆成 **F-06（DNS rebinding）** 与 **F-07（通配 DNS 服务把内网 IP 编进域名）** —— 后者范围更大（`nip.io` / `sslip.io` / `traefik.me` 是**独立服务**不是变体，**任意内网地址**都能这样藏，公网地址没有额外风险），原写法让人以为"窗口很窄"。 |
| v0.42 | **出站客户端一律不读环境变量代理**（触发点：RedTeam 抓出原清单漏了一处 —— 清单是搜 `httpx.AsyncClient(` 得到的，而 **`app/scans/reachability.py` 用的是模块级 `httpx.get(...)`**，按形态搜的方法在设计上就看不见它；**而漏的这处目标恰是最典型的用户可控输入**：`POST /scans` 的 `target`，`scan` scope 即可触发）。**问题**：`httpx` 的 `trust_env` **默认 `True`** —— 每个客户端都会读环境里的 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`（以及 `.netrc`）并把请求交给那个代理。实测（本机起真转发代理，判据是**代理收到的请求行**）：默认配置下代理收到 `GET http://rt-meta.test:8099/ HTTP/1.1`（真的改道），`trust_env=False` 则不经过。**修法**：9 处全部显式 `trust_env=False`，**但理由分两档**（只按"是不是 SSRF 面"筛会漏掉后三处）—— **A 档 6 处防 SSRF**：`ctf2/adapters/transport.py`、`ctf2/adapters/probe.py`、`providers/llm.py` 的 `probe()`、`ctf2/runner.py` 的 `LlmGateway.chat`、`ctf2/runner.py` `solve()` 里那个 `http`（只喂 `_tool_http_request`，已有 `guard_tool_url` + `assert_allowed_target` 白名单）、**`scans/reachability.py` 的 `_probe()`**；**B 档 3 处防凭据经代理泄漏**：`ctf2/client.py`（发 PAT）、`ctf2/attachments.py`（带 `Authorization` token）、`arena/client.py`（发 `X-Agent-AccessKey`；⚠️ `app/arena` 生产路径无调用者=死代码，优先级最低）。⚠️ **A 档 6 处 = RedTeam 说的"8 处"**（它把原清单 7 处 + 漏的 1 处 = 8，再加它自己找的 `providers/llm.py` 那处 sync 的才是本表 9 处；口径不一致是没对齐编号，以本表为准）。⚠️ **一处有意保留的取舍**：`providers/llm.py` 那处若用户在**公司代理后面**跑本地 Ollama，`trust_env=False` 会让探测连不上（不再走 `HTTP_PROXY`）—— 与 `SUPEX_ALLOW_PRIVATE_LLM_ENDPOINTS` 同类取舍，**已在代码注释写明**，正解是加显式 proxy 配置项传给 `proxy=`，而不是打开 `trust_env`（那等于把"谁来收这次请求"交给环境）。**防回归**：新增 `tests/test_outbound_trust_env.py`（11 例）—— 一条 **AST 扫全 `app/`**（要求每处出站客户端显式写 `trust_env`，将来新增第 10 个漏写会直接失败）、一条逐档逐文件取值断言、一条行为验证、一条按调用参数钉住 `_probe()`。⚠️ 取值走 AST 而非字符串：**第一版字符串断言被注释骗过了**（客户端上方都有解释 `trust_env=False` 的注释，删掉参数后字符串断言仍通过），反向验证时实测踩到，已改 `ast.literal_eval`。 |
| v0.43 | **畸形 `Location` 的异常逃逸修复（F-08），并撤回上一轮的错误判断**。Review 最初报"越界端口 `Location` 抛 `ExceptionGroup[OverflowError]`，调用点接不住 → 500"，我在 v0.41 里判它**不成立** —— **那个判断是错的，错因是我在错误的测试环境上取证**：同一份脚本、同一个 httpx 0.28.1，**本机 Windows** 上 `httpx.AsyncClient` 对 `http://127.0.0.1:99999/x` 返回 **502**（连异常都不抛）、`anyio.connect_tcp` 抛 `OSError`，而 **Kali（生产同款 Linux）** 上冒出 **`ExceptionGroup[OverflowError]`**。⚠️ 这个坑主管也踩过一次（他先据 Windows 结果否掉 Review，后在 Kali 上认错）—— **硬规矩：网络栈 / 异常类型 / 平台行为的验证一律在 Kali 做，本机 Windows 结论不可采信**。在 Kali 上铺开测 28 个畸形 `Location`，共找出 **9 条逃逸路径、两个异常族**：① **端口越界**（`:99999` / `:65536` / `:70000` / `:100000` / `:4294967296` / `https://…:99999` / `http://:99999`）→ `ExceptionGroup[OverflowError]`（anyio task group 包住 `socket.connect` 的 `OverflowError`）；② **协议异常**（`javascript:alert(1)` / `data:text/html,x`）→ **`httpx.InvalidURL`**（直接继承 `Exception`，**不是** `HTTPError` 子类）。两者调用点（`ctf2/adapters/transport.py`、`probe.py` 的 `except httpx.HTTPError`）都接不住 → 用户看到 500。**修法（选"在 `request_same_host` 里拦成 `Ctf2Error`"，不选"放宽 `except`"）**：`app/ctf2/adapters/redirect.py` 新增 `_check_port()`，在 `_next_url()`（Location 指过来的）与循环体（第一跳 URL 由 `base_url + path` 拼成，实测把 base_url 配成 `:99999` 同样逃逸）两处拦端口越界；并在 `client.request` 外包一层 —— ⚠️ **httpx 自己也会解析一次 `Location`**：`_send_handling_redirects` 里 `_build_redirect_request` 排在 `if follow_redirects:` **之前**，所以 `javascript:` 那类在 `client.request()` 内部就炸、我们的 `_next_url` 根本轮不到执行；包装里把 `InvalidURL` / `ExceptionGroup` 转成 `Ctf2Error`，**但只转"地址/网络层面"的叶子异常**（`OSError` / `ArithmeticError` / `ValueError` / httpx 的 URL 与传输异常），编程错误照旧上抛。**为什么这样选**：① 贴合本模块原有形状（"逐跳校验、不合格抛 `Ctf2Error`"），`probe.py` 已经会优雅跳过不合格端点；② 放宽 `except` 要在**每个**调用点各改一次，而"漏改某一处"正是这个缺陷的性质（本轮就漏了 `_probe()` 那处）；③ 放宽到 `BaseExceptionGroup` 会连带吞掉编程错误（task group 会把我们自己的 bug 一起包起来）。⚠️ **我在这批里犯的两个错，都记下来**：① 第一版协议判据写成 `scheme not in ("http","https")`，而**相对地址**（`/final`）的 `scheme` 是**空串**、那是合法的 → 实测 **6 个既有用例挂掉**；改成 `scheme and scheme not in (…)`。② 同样地，`if target.scheme and not target.host` 里的 `target.scheme and` 不能省（相对地址的 `host` 也是空串，要留给 `join` 补全），省掉会把 `/final` 拗成错误地址。**防回归**：`tests/ctf2/test_redirect_guard.py` 加 15 条畸形 `Location` 参数化用例 + 1 条**反向闸**（相对地址必须仍被跟随，防"把什么都拒掉"的实现）+ 2 条端口越界断言（`Location` 与 `base_url` 各一）。**逐层反向验证**：把两层护栏**都**去掉 → 9 条逃逸全部重现（`ExceptionGroup` ×7 + `InvalidURL` ×2）；装回 → **0 逃逸**、30 条 redirect 用例全绿。⚠️ 只去掉 `_check_port` 一层时仍是 0 逃逸（两层互为备份），所以反向验证必须**同时**去掉两层才有意义。 |
