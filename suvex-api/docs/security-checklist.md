# 上线前安全检查清单（任务 8）

> 这份文档是**任务 8 的交付物**，不是设计稿。
> 每一项都写：**结论**（通过 / 不通过 / 待办）、**证据**（怎么验的、在哪能看到）、
> 以及没过的项**怎么改**。
>
> 验收标准（任务原话）：
> 「`/docs/security-checklist.md` 全部项通过或有明确的整改计划；
> 高风险操作的鉴权和审计留痕都能在真实请求中验证到。」
>
> **一句话结论**：七项里 **6 项通过**、**1 项部分通过**（Docker 不可用导致
> 容器级隔离无法实测，有整改计划与可执行的配置项）。
> 过程中发现并修复了 **4 个真实缺陷**（下表标 ★），其中两个是我自己引入的。

---

## 0. 总览

| # | 要求 | 结论 | 关键证据 |
| --- | --- | --- | --- |
| 1 | 鉴权检查（遍历契约里需鉴权的接口） | ✅ **通过** | `tests/test_authorization_matrix.py`（239 项） |
| 2 | 审计日志（独立表 + 更长保留策略） | ✅ **通过** | `audit_log` 表 + `GET /audit`；`tests/test_audit.py`（19 项）+ 真实请求 24 项 |
| 3 | 授权确认字段后端真实校验并落库 | ✅ **通过** | `tests/test_security_hardening.py`；六列留痕逐一断言 |
| 4 | 密钥 / 敏感信息脱敏 | ✅ **通过** | 脱敏正则 + 子进程环境白名单；`tests/test_secret_hygiene.py` + 新增 23 项 |
| 5 | 限流（扫描创建 + 登录） | ✅ **通过** | `app/core/ratelimit.py` + 两个限流器；阈值可配 |
| 6 | 沙箱与主库 / 后端网络隔离 | ⚠️ **部分通过** | compose 层面已隔离；**容器级无法实测（本机无 Docker）**。见 F-01 / F-02 |
| 7 | 产出本清单 | ✅ **通过** | 本文件 |

复现命令：

```powershell
$py = "E:\Program Files (x86)\suvex-api-venv\Scripts\python.exe"
cd <suvex-api>
& $py -m pytest -p no:logging --tb=short          # 全套（含本任务的 281 项新增）
& $py scripts\verify_acceptance.py                 # 端到端验收
& $py scripts\verify_security.py                   # 任务 8 的真实请求验证（需后端在跑）
```

---

## 1. 发现的真实缺陷（★ 逐条交代）

| ID | 缺陷 | 严重度 | 状态 |
| --- | --- | --- | --- |
| ★ S-01 | **strix 子进程继承整个后端环境**，含 `SUPEX_SECRET_KEY`（能解开所有 provider 密钥）与 `SUPEX_DATABASE_URL`（含库口令） | **高** | 已修 |
| ★ S-02 | **机器令牌发起扫描时 `authorized_by` 为 `null`** —— "哪个 CI 令牌授权对目标发包"完全没留痕 | 中 | 已修 |
| ★ S-03 | **`DELETE /reports/{id}` 是 `read` 级** —— 与契约 §1.2 对 `read`（「只读」）的定义自相矛盾，且是唯一不可逆的销毁操作 | 中 | 已修（契约 v0.10 → `admin`） |
| ★ S-04 | **我自己的环境白名单把 `STRIX_*` 运维配置一起挡掉了** —— 其中含 `STRIX_DOCKER_SANDBOX_NETWORK` 与 `STRIX_SANDBOX_*` 资源上限，即**隔离手段本身** | 中 | 已修（按命名空间放行） |
| ★ S-05 | **验收脚本里最强的那条一致性断言是偶发的** —— `/overview` 与 `/findings/stats` 的比较会撞上后台的扫描导入，两次读到不同快照 | 中 | 已修（比较前先 `_quiesce`） |

> **S-05 值得单独说一句**，因为它是"**修 bug 时把另一个 bug 抖出来了**"的典型：
> 我给每个写操作加了审计（每次都 `commit`），于是 timing 变了，
> 那条本来就有竞态的断言开始偶发失败。修法**不是放松断言**
> （那会把一条最强的检查变成一条弱检查），而是把它缺的前提做实 ——
> `_quiesce()` 先等没有 queued/running 的扫描，再等漏洞总数连续两次相同。
> 修完连跑两次都是 116/116。

另外订正了一处**文档与实现不一致**（不影响运行）：

* `GET /providers` 当**初**的实现是 `admin`（当时的契约 §3.2 也是这样写的），
  而我第一版测试表把它当成 `read`。当时是**测试表**错了，实现是对的 ——
  已按实现订正，并在测试里写明它为什么是 `admin`。

  > ⚠️ **v0.24 起这一条又变了**（上面这段保留为当时那次订正的存档）。
  > `GET /providers` 从 `admin` **有意**放宽到 `read`，理由见
  > `app/providers/router.py` 的 `list_providers` docstring：
  > 前端状态条要显示"当前模型 XXX"，读的就是这个端点；若要求 `admin`，
  > 一个 `viewer` 登录后状态条会报错，而那个错误看起来像"系统坏了"，
  > 不像权限问题。
  >
  > 放宽的前提是该端点**不返回任何明文凭据**：`apiKey` 恒为空串，
  > 只有掩码 `apiKeyMask`（见 ADR-003）。
  >
  > **写操作（新建 / 改 / 删 / 验证 / 设默认）仍然是 `admin`** ——
  > "能不能看到"与"能不能改"是两件事，这条线画在后者上。
  > 例外是那些**内容本身就是秘密**的东西（用户列表、审计日志），
  > 它们连看都要 `admin`。
  >
  > 契约的角色表与 `tests/test_authorization_matrix.py` 的 `READ_ONLY`
  > 分类均已同步到这一状态。

> **这四条都不是靠读代码读出来的**，而是靠"把断言写成可执行的"逼出来的：
> S-01 来自"子进程环境里到底有什么"这条断言，S-02 来自"令牌通道也要留痕"，
> S-03 来自"端点分类表必须自洽"，S-04 来自"白名单不能把运维手段也挡掉"。
> 这也是这一轮最值得记的经验：**安全检查的产出是断言，不是结论。**

---

## 2. 逐项明细

### 要求 1：鉴权检查 ✅

**要求原话**：遍历 `/docs/api-contract.md` 里所有标注需要鉴权的接口，写一遍自动化测试
确认无 token 或错误 token 时全部正确拒绝，尤其是"启动扫描""终止扫描""删除报告"这类高风险操作。

**做法**：`tests/test_authorization_matrix.py`。关键设计是**端点表不是手抄的** ——
先把全部端点（`app.openapi()["paths"]` ∪ `app.routes`）枚举出来，
再断言每一个都在三张显式表（`PUBLIC` / `READ_ONLY` / `SCOPE_GATED`）之一里。

> **为什么必须这样**：手抄的表有一个必然的失效模式 ——
> **新加端点时忘了补一行**，而症状是"那个端点悄悄变成公开的"。
> 没有任何测试会红。用枚举 + 完整性断言，这条路径就堵死了。

| 检查 | 覆盖 |
| --- | --- |
| 每个端点都被分类 | 全部 **50** 条（含 3 条文档路由）—— `PUBLIC` 9 + `READ_ONLY` 18 + `SCOPE_GATED` 23 |
| 无凭据 → 401 | `READ_ONLY`(18) + `SCOPE_GATED`(23) = **41** 条 |
| 错误 token → 401 | **8 种**坏凭据 × `SCOPE_GATED` 全部 **23** 条 = **184** 条 |
| 权限不足 → **403**（不是 401） | `SCOPE_GATED` 全部 **23** 条 × 只读令牌 |
| 只读令牌仍读得动 | `READ_ONLY` 全部 **18** 条（防止"全都拒绝"也能全绿） |
| 高风险操作必须受限 | `POST /scans`、`/abort`、`/rerun`、`POST /findings/status`、`DELETE /reports/{id}` 显式断言 |
| `public` 端点不要求凭据 | 9 条（含登出幂等、文档路由） |

**真实请求验证**（`scripts/verify_security.py` 的输出，节选）：

```
[PASS] 无凭据 POST /scans → 401 UNAUTHENTICATED
[PASS] 无凭据 POST /scans/scan_x/abort → 401 UNAUTHENTICATED
[PASS] 无凭据 DELETE /reports/rpt_x → 401 UNAUTHENTICATED
[PASS] 错误凭据 Bearer not-a-token… → 401
[PASS] 错误凭据 Bearer svx_nonexistent12… → 401
[PASS] 错误凭据 Basic eA==… → 401
```

**结论**：通过。**239 项**参数化用例。

---

### 要求 2：审计日志 ✅

**要求原话**：谁在什么时间对什么目标发起了扫描、谁变更了漏洞状态、谁下载了报告 ——
这些操作要有独立的审计日志表，不能只靠应用日志（应用日志会被日志轮转清理，
审计日志需要更长的保留策略）。

**做了什么**

| 层 | 文件 | 说明 |
| --- | --- | --- |
| 表 | `app/audit/models.py` + 迁移 `74442d700ddc` | `audit_log`，11 列，4 个索引 + 1 个 CHECK |
| 服务 | `app/audit/service.py` | `record()` 唯一写入口；`purge_expired()` 唯一的删除路径 |
| 接口 | `app/audit/router.py` | `GET /audit`（`admin`，游标翻页，**只读**） |
| 契约 | `docs/api-contract.md` §3.9 | 先改契约再改代码 |

**为什么必须是独立的表**（而不是复用 `activity_events`）：
后者的保留期跟着扫描走（可随扫描一起清理），而审计记录必须在
**被引用的对象删掉之后仍然存在**。合并的直接后果是"清理扫描历史时
把审计一起删了"—— 而那正是出事之后要找的东西。所以 `audit_log.target`
不设外键，`test_audit_survives_deleting_the_scan_it_references` 钉住了这一点。

**六个动作**（都写进契约 §3.9）：`scan.create` / `scan.rerun` / `scan.abort` /
`finding.status_change` / `report.download` / `report.delete`。

**保留策略**：`AUDIT_RETENTION_DAYS = 365`（应用日志按天/周轮转，审计留一年），
启动时跑一次 `purge_expired()`，并且**在 API 响应里暴露 `retentionDays`** ——
让"这些记录留多久"是可见的，而不是只写在文档里。

**只追加**：

* `POST/PUT/PATCH/DELETE /audit` → **405**（真实请求已验证）；
* 服务层不导出任何"改/删一条"的函数（`test_audit_module_exposes_no_update_or_delete_entry_point`）；
* 模型上**没有** `updated_at`。

**真实请求验证**（节选）：

```
[PASS] 留痕：scan.create（谁对什么目标发起扫描）
[PASS] 留痕：scan.abort（谁终止了扫描）
[PASS] 留痕：finding.status_change（谁变更了漏洞状态）
[PASS] 留痕：report.download（谁下载了报告）
[PASS] 保留策略在 API 里可见（365 天）    retentionDays=365
[PASS] 每条留痕都能回答「谁 / 何时 / 做了什么」    缺字段的：[]
[PASS] 审计内容里没有凭据                命中：[]
[PASS] POST/PUT/PATCH/DELETE /audit → 405（只允许 GET）
```

**如实记两条限制**：

1. **失败的尝试不写审计**（契约 §3.9 有说明）。这是有意的取舍：
   被拒的请求已由 access log 覆盖，而审计表要留一年 ——
   把每次口令爆破都永久存进去，等于给了攻击者一个**写满磁盘**的入口。
   要改成全记，得先加来源维度的写入限流。
2. **能改数据库的人仍能改审计**。本平台是单机自托管，库口令就在同一个
   compose 文件里 —— "防管理员篡改"不是应用层能解决的问题。
   真需要要用外部只增日志收集，超出本轮范围（记为 **F-03**）。

**结论**：通过（19 项单元 + 24 项真实请求）。

---

### 要求 3：授权确认字段后端真实校验并落库 ✅

**要求原话**：授权确认字段（任务 0/3 里提到的"我有权对此目标测试"）要在后端做实际校验，
不能只是前端 UI 上的一个勾选框 —— 检查提交时这个字段是否真的被后端校验和落库，
而不是静默忽略。

**核实结果**：**本来就是真做的**（任务 3 落地），本轮补的是**边界覆盖**与一个真 bug。

| 送法 | 期望 | 实测 |
| --- | --- | --- |
| 完全漏掉 `authorization` | `403 TARGET_NOT_AUTHORIZED` | ✅ |
| `confirmed: false` | `403 TARGET_NOT_AUTHORIZED` | ✅ |
| 措辞不符 | `400 VALIDATION_FAILED` + `details["authorization.statement"]` | ✅ |
| 措辞**多一个句号** | `400`（逐字比对，不是"包含"） | ✅ |
| 通过 | 六列留痕全非空 | ✅ |

> **为什么"漏掉"是 403 而"措辞不对"是 400**：前者是**授权缺失**（用户没做那个动作），
> 后者是**内容不合法**（做了，但送来的东西不对）。前端的处理不同 ——
> 前者要引导去勾选，后者要提示重新确认。用错一个码，界面就指错方向。

**★ S-02 修复**：`authorized_by` 原来写的是 `identity.user_id`，
而**令牌通道下它是 `None`**（令牌不绑定某个人）。于是"哪个 CI 令牌授权
对目标发包"完全没有留痕 —— 而这恰恰是机器场景下最该回答的问题。
现在：会话记 `user_id`，令牌记 `token:<tokenId>`（前缀是必需的：
不加的话，一个 id 叫 `user_x` 的令牌与一个 id 为 `user_x` 的管理员在这一列里分不开）。
契约 §5.4 已补记这一修正。

**结论**：通过。

---

### 要求 4：密钥 / 敏感信息脱敏 ✅

**要求原话**：确认所有中间件、错误堆栈、第三方监控上报，
都不会把 LLM API Key、JWT、数据库连接串打印出来。

#### 4.1 子进程环境（★ S-01，本轮最重要的修复）

**原来**：两个 runner 都是 `env = dict(os.environ)` —— 把**整个后端进程的环境**
交给一个接下来要执行任意命令的进程。里面有：

| 变量 | 泄漏后果 |
| --- | --- |
| `SUPEX_SECRET_KEY` | **能解开库里所有 provider 的 API key** |
| `SUPEX_DATABASE_URL` | 含库口令 → 直连业务库（含审计日志本身） |
| `SUPEX_*` 其余 | CORS、Cookie 名、限流阈值 —— 一张部署图 |

strix 的威胁模型就是"它会执行从目标上拿到的东西"（这正是要沙箱的原因），
所以不能假设它内部安全。一个被诱导执行 `env` 的 agent 就把密钥读走了。

**现在**：白名单（`app/scans/runner.py` 的 `_base_env()`）。
**为什么是白名单不是黑名单**：黑名单要求每次有人加一个 `SUPEX_` 变量时
都记得来加一行，而漏掉那次不会有任何症状；白名单默认安全。

**★ S-04（修 S-01 时引入又修掉的）**：第一版白名单只认固定名字，
把运维设的 `STRIX_SANDBOX_MEM_LIMIT` / `STRIX_RUNTIME_BACKEND` /
`STRIX_DOCKER_SANDBOX_NETWORK` 一起挡掉了 —— 而那几条**正是隔离与限流的手段**。
边界应当是**命名空间**：`STRIX_*` / `DOCKER_*` 属于那个工具的配置，放行；
`SUPEX_*` 是我们的机密，一律不放。两种行为都有测试钉住。

#### 4.2 日志与堆栈

| 面 | 结论 | 证据 |
| --- | --- | --- |
| 请求体日志中间件 | **不存在**（唯一的 access log 只记「方法 路径 状态码」） | `app/core/logging.py` 的审计结论；`test_plaintext_key_never_reaches_the_logging_module` 在 `Logger._log` 上抓原始消息 |
| 脱敏兜底 | `RedactingFormatter`（格式化**之后**脱敏 → 覆盖 traceback）+ `RedactingFilter`（管 uvicorn 等自带 handler 的 logger） | 同文件 |
| 错误堆栈 | 只进服务端日志；响应里**连异常类名都不给** | `test_unhandled_exception_does_not_leak_internals` 断言响应里没有 `RuntimeError` / `Traceback` / `service.py` / 连接串 |
| 校验错误回声 | 不回显用户输入（契约 §1.3 第 3 条） | `test_validation_error_does_not_echo_user_input` |
| 落库 | 只存密文 + 掩码，明文一个字都不在；模型上**没有** `api_key` 属性 | `tests/test_secret_hygiene.py` |
| 对方服务回声我们的 key | `verify_error` 过脱敏后才落库 | `test_provider_error_text_is_redacted` |

#### 4.3 脱敏正则（本轮补齐）

新增三类（原来只有 `sk-…` 与 `svx_…`）：

```
postgresql+psycopg://suvex:****@127.0.0.1:55432/suvex     ← 连接串口令
jwt eyJhbGci****                                          ← JWT
Authorization: Bearer ********                            ← Bearer 头
```

**为什么连接串那条是任务 8 点名的重点**：SQLAlchemy 连库失败的异常里
会把**整个 URL 原样带上**（含口令），而它出现在最容易被打印的地方。
配套的"不误伤"测试也在（正常文本、无口令的连接串不许被动）。

#### 4.4 第三方监控上报

**结论：本平台没有接任何第三方监控 / 错误上报 SDK**（没有 Sentry / DataDog /
OpenTelemetry exporter）。所以这一条目前**没有可泄漏的面**。

顺带核对了唯一一处"会出网"的地方：strix 的遥测默认**关**
（`STRIX_TELEMETRY=0`，上游默认是开）。理由不只是安全 ——
本平台跑的是**客户目标的渗透测试**，把目标地址与用量上报第三方
需要用户明确同意。`test_subprocess_still_gets_what_it_needs_to_run` 钉住了它。

> **如果将来接入监控上报**：先看 `app/core/logging.py` 的说明 ——
> 脱敏是**兜底**，不是"可以放心上报"的许可证。上报 SDK 通常直接
> 序列化异常与局部变量，那条路径**不经过**我们的 Formatter。
> 正确做法是在 SDK 的 `before_send` 里再过一遍 `crypto.redact`。

**结论**：通过（本任务新增 23 项 + 既有 `test_secret_hygiene.py` 11 项）。

---

### 要求 5：限流 ✅

**要求原话**：扫描创建接口、登录接口要有基本的限流，防止被脚本刷。

| 接口 | 阈值（可配） | 依据 |
| --- | --- | --- |
| `POST /auth/login`（+`/auth/setup` 共用一个桶） | 5 / 分钟，15 / 小时 | 口令是本系统唯一凭据，不限流等于允许**在线爆破** |
| `POST /scans`、`POST /scans/{id}/rerun`（共用一个桶） | 30 / 分钟，200 / 天 | 防**脚本刷**：见下 |

**为什么扫描创建也要限流（它本来就有并发上限）**：
`max_concurrent_scans` 管的是"同时在跑几个"，超出的会**排队** ——
所以脚本疯狂提交不会被拒绝，它会把队列堆到几千条，
每一条都带着一份授权声明进库。那不是"慢"，那是
**把队列变成一个持久化的攻击排程**。而且每次创建都要 fork 一个子进程
（并拉起沙箱容器），是实打实的开销。

**阈值为什么比登录松得多**：两者的正确阈值不同 —— 登录防爆破（严格），
创建防刷（够松才不会误伤"一口气建 5 次扫描"）。

**实现要点**：

* 滑动窗口（不是固定分钟窗口 —— 后者在边界上允许 2 倍突发）；
* key = **`身份 + IP` 组合**，不是只按 IP（反代后所有人共用一个桶
  → 一个人刷满其他人全被误伤），也不是只按身份（泄漏的令牌换个来源照样刷）；
* 超限 → `429` + `Retry-After`（CORS 的 `expose_headers` 专门为它放行）；
* **限流检查在落库之前** —— 被限流的请求不留任何痕迹（连一条 `queued` 都不该有），
  否则用户会看到一条永远跑不起来的扫描记录，而界面上没有任何解释。

**测试**：`test_scan_creation_is_rate_limited`、`test_rate_limited_request_leaves_no_scan_behind`、
`test_scan_rate_limit_is_per_credential_not_global`、`test_login_rate_limit_still_works`。

**如实记一条限制**：限流器是**进程内**的（滑动窗口 + 锁），
多 worker / 横向扩展时会按进程各算一份。单机 Compose 形态下够用
（`app/core/ratelimit.py` 的模块 docstring 记了这条代价与换 Redis 的时机）。

**结论**：通过。

---

### 要求 6：环境隔离 ⚠️ 部分通过

**要求原话**：确认 strix 执行沙箱和主业务数据库 / 后端服务网络隔离，
沙箱容器不应该能访问到不相关的内部服务。

#### 已确认（静态核查，有据可查）

| 项 | 结论 | 证据 |
| --- | --- | --- |
| 数据库不对宿主暴露 | ✅ | `docker-compose.yml` 的 `db` 只有 `expose: 5432`，**没有** `ports:` —— 局域网内任何机器都连不上 |
| API 只绑回环 | ✅ | `ports: - "127.0.0.1:8000:8000"` —— 它会对任意目标发包，暴露出去等于给别人一个现成的攻击跳板 |
| 库在后端自己的 compose 网络上 | ✅ | 服务名 `db`，与沙箱不在同一个网络 |
| 后端机密不进扫描子进程 | ✅ | 见要求 4.1（★ S-01） |
| 不向沙箱 bind mount 宿主目录 | ✅ | 全仓库 `grep bind_mount` **零命中** —— strix 支持它，我们没用 |
| 沙箱不带 `privileged` | ✅ | strix 只加 `NET_ADMIN` / `NET_RAW`（`nmap -sS` 需要），未用 privileged 模式 |
| 沙箱资源上限 | ✅（可配） | strix 默认**开**日志轮转（`STRIX_SANDBOX_LOG_MAX_SIZE=50m×3`）；内存/CPU/PID 上限可用 `STRIX_SANDBOX_*` 打开（经 `_PASSTHROUGH_PREFIXES` 透传） |

#### ⚠️ F-01：沙箱能回连宿主机（strix 自己的注入行为）

**事实**：strix 在创建沙箱容器时**主动**加入
`extra_hosts["host.docker.internal"] = "host-gateway"`。

> 证据：`strix_agent 1.6.2` 的 `strix/runtime/docker_client.py`
> 文件头第 3 条 delta："Add `host.docker.internal` → host-gateway to
> `extra_hosts` so the agent can reach host-served apps."

**后果**：沙箱内的代码可以访问**宿主机上监听的服务**。在 Docker Desktop
（Windows/macOS）上，`host.docker.internal` 能到达宿主**回环地址上**的端口 ——
本机开发形态下那就是 API（`127.0.0.1:8000`）与开发库（`127.0.0.1:55432`）。
生产 compose 形态下开发库没有暴露，但 API 仍在宿主回环上。

**为什么不能直接改掉**：这是 strix 的实现，没有开关能去掉它 ——
而且它有正当用途（扫描"宿主上跑着的应用"是这个工具的常见场景）。

**整改计划（按性价比排序）**

1. **把沙箱放进专用网络**（已实现配置项）：
   设 `SUPEX_SANDBOX_NETWORK=<name>` → 透传为 strix 的
   `STRIX_DOCKER_SANDBOX_NETWORK`。建议建成 `internal: true` 的网络，
   并且**不加入**业务网络。这样沙箱既不在默认 bridge 上，也不与业务容器同网。
2. **生产不要把 API 绑在宿主回环上**：改用反向代理 + 一个独立网络，
   让 API 只对反代可见。或在 API 容器上限制来源。
3. **宿主防火墙**：只放行扫描目标所需的出站，拒绝对宿主内部网段的入站。
4. 长期：让 strix 支持"沙箱网络策略"（需要上游改动，记为 **F-02**）。

#### ⚠️ F-02：容器级验证**未完成**（本机没有 Docker）

**没做到什么**：上表中"沙箱容器实际拿到了哪些 capability / 挂在哪个网络 /
能不能真的连到宿主机"这几点，**全部是读 strix 源码与实际编排文件得出的**，
没有在真实 Docker 上跑过一次。

**原因**：本机没有 Docker 引擎（Windows 上未启用 WSL2 / 虚拟化平台，
启用需要重启）。这也是任务 3 以来一直挂着的同一条限制。

**装好 Docker 后要做的三件事**（这是本项的验收补完）：

1. 跑一次真实扫描，确认 `strix_runs/<runId>/` 出现、状态走到 `completed`；
2. 在沙箱容器里执行
   `getent hosts host.docker.internal && curl -sS -m 2 http://host.docker.internal:8000/api/v1/health`
   —— 记录**能不能连通**。这是 F-01 的实测，也是整改前后的对照基线；
3. 确认 `docker inspect` 的 `NetworkMode` 等于配置的 `SUPEX_SANDBOX_NETWORK`
   （证明配置项真的生效），且 `CapAdd` 仅含 `NET_ADMIN` / `NET_RAW`。

**结论**：⚠️ **部分通过**。配置与代码层面该做的都做了（且修掉了一个真泄漏），
但容器级结论**待 Docker 环境补验**。整改计划明确、可执行、有基线。

---

### 要求 7：产出本清单 ✅

本文件。位置：`suvex-api/docs/security-checklist.md`。

> **为什么放在 `suvex-api/docs/` 而不是 `suvex-web/docs/`**：
> 七项全部是**后端与部署**的事（鉴权、审计、脱敏、限流、沙箱隔离）。
> 前端的接口契约仍在 `suvex-web/docs/api-contract.md`，
> 本轮对契约的两处修改（§3.5 的 `DELETE` 级别、§5.4 的 `authorized_by`）
> 都在那份文档里做了 v0.10 记录。

---

## 2.5 已知边界（**不是缺陷，但评审一定会问**）

这一节写的是"防护到什么程度"的**准确边界**。它们的共同点是：
代码里已经写清楚了，但此前只存在于注释中 —— 而企业安全评审
问的正是这些，答案不该靠读源码才能拿到。

### SSRF：挡的是"直接写内网地址"，不是完整防御

**位置**：`app/providers/llm.py`（`_assert_url_allowed`）、`app/ctf2/platforms.py`
（判据实现在 `app/ctf2/hostguard.py`）

| 项 | 状态 |
| --- | --- |
| 云元数据地址（`metadata.google.internal`、`metadata.goog`） | ✅ 已挡（字符串判据） |
| 元数据地址的**等价写法**（`2852039166` / `0xA9FEA9FE` / `0251.0376.0251.0376` / `[::ffff:169.254.169.254]`） | ✅ 已挡（归一化后判网段，见下） |
| 直接写内网 / 回环 IP 作为 `baseUrl` | ✅ 已挡（受 `SUPEX_ALLOW_PRIVATE_PROVIDER_URL` 控制） |
| **主机名解析后再判内网**（DNS rebinding） | ❌ **不做** |
| 平台请求跟随重定向后的二次校验 | ✅ **已做**（逐跳校验 + 禁跨 host，见下） |

> **2026-10-15 修了两条（RedTeam 发现的 1、2）**：
>
> ① **等价写法绕过**。原判据是字符串比对 `parsed.host.lower() in BLOCKED_HOSTS`，
> 而 `169.254.169.254` 有 `2852039166`、`0xA9FEA9FE`、`0251.0376.0251.0376`、
> `[::ffff:169.254.169.254]` 这些 `getaddrinfo` 全认、字符串一个都不等的写法 ——
> **它们原先全部放行**。现在两条判据**并存**：域名仍走字符串比对（否则
> `metadata.google.internal` 会被放过），字面量 IP 先归一化再判网段
> （`169.254.0.0/16` 与 `100.100.100.200/32`）。
>
> ② **重定向绕过**。平台探测/同步原先 `follow_redirects=True`，而校验只发生在
> 用户填 URL 的那一刻 —— 后续跳转从不过校验。实测能把内网服务的响应体读出来。
> 现在改成逐跳自己跟，判据是**白名单**："跳转目标必须与用户填的 host 相同"
> （同 host 的 `http→https` 刻意放行）。这比"每跳再查一次元数据"更严 ——
> 后者仍是黑名单思路，而黑名单正是 ① 栽的地方。
>
> ⚠️ **仍然不做"禁止内网"**：比赛平台很可能就部署在内网，
> `192.168.*` / `10.*` / `172.18.0.1` / `127.0.0.1` **必须继续放行**。
> 上面两条修的都是"元数据地址的变形"，不是把范围扩大成内网全禁。

> **为什么不做 DNS 解析后校验**：那需要真正解析一次，而**解析本身
> 就可能被指向内网**（TOCTOU）—— 解析出公网 IP、请求时却解析到
> 内网，这个窗口关不掉。要真正解决得靠网络层（出口代理 / egress
> 策略），而不是应用层。
> ⚠️ 已知残余：`http://169.254.169.254.nip.io/` 这类**域名**写法仍可绕过
> （校验时它只是个字符串域名）—— 见 F-06。
>
> **风险为什么可接受**：能改 provider 配置的是**管理员**，而管理员
> 本来就有权让服务器访问内网 —— 那正是这个平台的用途。所以这条
> 护栏防的是"误配"与"明显不该碰的地址"，不是防管理员。
> 代码里原话：**"这是一道便宜的护栏，不是完整的 SSRF 防御。"**

### 扫描目标：内网地址**故意不拦**

**位置**：`app/scans/targets.py`、`app/ctf2/router.py`

扫描目标允许内网 IP、非标准端口、`localhost`。这是**刻意的**：
安全测试工具的使用场景就是内网资产，比赛平台也常常部署在内网
（`192.168.x.x` / `10.x.x.x`）—— 拦掉等于把主要用途废掉。

**代价要说清**：这意味着"谁能发起扫描"就是一道**关键权限边界**。
当前由 `scan` scope 控制（见要求 1 的端点分类表），且每次创建
扫描都要填写授权声明（`SUPEX_SCAN_AUTHORIZATION_STATEMENT`）。

### 沙箱网络：不回连宿主机

**位置**：`app/ctf2/runner.py`

CTF2 沙箱**不放行**宿主机内网、不放行 `host.docker.internal` ——
与"扫描目标允许内网"相反。理由：沙箱里跑的是**题目附件里的
不可信代码**，它不该有能力回连宿主机。

> 这两条看似矛盾（一个允许内网、一个禁止），实际是**两个不同
> 信任级别**：扫描目标是用户明确指定的，沙箱代码是题目给的。

---

## 3. 待办汇总（按优先级）

| ID | 待办 | 为什么现在没做 | 怎么验收 |
| --- | --- | --- | --- |
| F-01 | 沙箱 `host-gateway` 回连宿主机的风险收窄 | strix 的固有行为，无开关；已提供网络配置项 + 生产建议 | 装好 Docker 后按要求 6 的三步实测 |
| F-02 | 容器级隔离实测（capability / 网络 / 宿主机可达性） | **本机没有 Docker 引擎** | 同上 |
| F-03 | 审计日志防篡改（外部只增收集） | 单机自托管下"防管理员"不是应用层能解决的；需要外部 sink | 审计同时推到只增不改的远端，且本地被改后能对账出来 |
| F-04 | 限流跨进程共享 | 单机单进程形态下不需要；引 Redis 会让 compose 从 2 个服务变 3 个 | 多 worker 部署前换成 Redis 或网关限流（调用点只有 `Limiter.check`） |
| F-05 | 失败的鉴权尝试是否也进审计 | 会变成"攻击者可写满磁盘"的入口，需先加写入限流 | 加来源维度写入限流后再决定 |
| F-06 | SSRF 的 DNS-rebinding 窗口（见 §2.5）。**含** `169.254.169.254.nip.io` 这类"域名指向元数据地址"的写法 —— 2026-10-15 修了等价 IP 写法与重定向绕过，但**域名这一条仍然放行** | 应用层关不掉这个窗口（校验时它只是个字符串域名），要靠出口代理 / egress 策略，或在**连接层**校验解析后的 IP（httpx 的 transport 钩子 / 自定义 resolver） | 部署时加出口白名单，或确认"管理员可信"这一前提成立；彻底堵住需在连接层校验解析结果 |

---

## 4. 本轮改了哪些文件

**新增**

| 文件 | 说明 |
| --- | --- |
| `app/audit/__init__.py` / `models.py` / `schemas.py` / `service.py` / `router.py` | 审计日志（表 / 出参 / 读写 / `GET /audit`） |
| `alembic/versions/20260924_1550_74442d700ddc_audit_log.py` | `audit_log` 建表 |
| `tests/test_authorization_matrix.py` | 要求 1：239 项鉴权穷举 |
| `tests/test_audit.py` | 要求 2：19 项审计日志 |
| `tests/test_security_hardening.py` | 要求 3/4/5：23 项 |
| `scripts/verify_security.py` | 真实请求验证（验收用）：24 项，含鉴权、审计留痕、只追加 |
| `docs/security-checklist.md` | 本文件 |

**改动（要点）**

| 文件 | 改动 |
| --- | --- |
| `app/scans/runner.py` | 子进程环境改白名单（★ S-01）+ `STRIX_*`/`DOCKER_*` 命名空间透传（★ S-04）+ 传递沙箱网络 |
| `app/scans/service.py` | `authorized_by` 覆盖令牌通道（★ S-02）；新增扫描创建限流器 |
| `app/scans/router.py` | 创建/重跑限流；创建/重跑/终止三处审计埋点 |
| `app/findings/router.py` | 状态流转审计埋点（仅 `changed ≥ 1` 时写） |
| `app/reports/router.py` | 下载/删除审计埋点；**`DELETE` 提升为 `admin`**（★ S-03） |
| `app/core/crypto.py` | `redact()` 补连接串口令 / JWT / Bearer |
| `app/core/config.py` | `scan_create_limit_per_minute` / `_day`、`sandbox_network` |
| `app/core/vocab.py` | `AuditAction` + `AUDIT_ACTION_LABELS` |
| `app/main.py` | 挂 `audit` 路由；启动时跑保留期清理 |
| `app/models.py` | 注册 `AuditLog` |
| `docker-compose.yml` | `SUPEX_SANDBOX_NETWORK` 透传 + 说明 |
| `tests/conftest.py` | 每个用例前重置扫描限流器 |
| `scripts/verify_acceptance.py` | 新增 `_quiesce()`：一致性比较前先等系统静下来（★ S-05） |
| `suvex-web/docs/api-contract.md` | §3.9 审计日志（新增）；§3.5 `DELETE` 级别（v0.10）；§5.4 `authorized_by`（v0.10） |
