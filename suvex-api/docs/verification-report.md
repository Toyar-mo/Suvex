# 验收记录

**实测输出**的存档，不是手写的结论。复现：

```powershell
$py = "E:\Program Files (x86)\suvex-api-venv\Scripts\python.exe"
& $py -m pytest
& $py scripts\verify_acceptance.py
& $py scripts\verify_report_parity.py
```

- 环境：本机 Windows，Python 3.12.14，PostgreSQL 16.4（服务 `suvexpg`，端口 55432）
- 测试对**真实 PostgreSQL** 跑，不是 SQLite
- 验收脚本会重建 `suvex_acceptance` 库、真的起 uvicorn 子进程，然后：
  开真实 SSE 长连接、起真本地假 LLM 服务让 provider 探测去打它、
  **真的拉起 strix 二进制**、并用替身后端（产出**真实结构**的外部产物）
  跑通扫描生命周期、事件导入、漏洞导入、报告生成与统计一致性
- ⚠️ 两处未验证，都因为本机没有 Docker：`docker compose up`、
  以及**一次真实扫描**（strix 报 `DOCKER NOT INSTALLED`）。
  详见 `README.md` 的「已知未验证项」

> 本轮（任务 6 + `/overview`）另有一处**需要说明的发现**：
> 写 `/overview` 的测试时对账发现 `/findings/stats` 的 `bySeverity`
> 没满足契约 §3.4.2 的 `Partial` 语义（为 0 的档发的是 `null` 而不是
> "键不存在"）。它被三处"兼容"盖住了一直没暴露，其中一处是
> **任务 5 的验收脚本把假值过滤掉之后再比较** —— 那条检查是空转的。
> 详见契约 §3.6 的「一处历史遗留的修正」与 `docs/adr-008-overview-derivation.md`。

> **前端接入阶段（任务 7）又抓到一处同类问题，性质更严重**：
> `Scan.findings` / `Scan.agent_count` 这两个**冗余列**写好了刷新函数
> （`refresh_scan_rollup()`）却**一个调用点都没有** —— 它们是死代码，
> 对结果毫无影响。于是建行时写下的 `{}` / `0` 永远留着，
> 扫描列表对每一次扫描都显示"未发现漏洞 / 0 个智能体"，
> 而漏洞页里明摆着有两条：**两个页面互相打脸**。
>
> 后端自己的测试抓不到它，因为工厂（`tests/factories.py` 的 `make_scan`）
> 为了形状好看**手写了一份合理的汇总** `{"high": 1}` / `agent_count=1` ——
> 断言看到的是工厂捏的值，不是刷新逻辑算的值。
> 抓到它的是前端：把 mock 换成真实接口之后，扫描列表上写着"未发现漏洞"，
> 而同一屏旁边的漏洞页写着两条。
>
> 修法见 §5。这一条值得记下来的教训是：**冗余列必须有一个"谁负责刷新"的
> 答案，且那个答案要在代码里看得见**（调用点），否则它一定会腐烂，
> 而且腐烂得悄无声息 —— 显示的是 0，不是报错。

---

## 1. pytest —— 任务 1 ~ 6 与 `/overview`

> ⚠️ 下面这段是**任务 6 那一轮**的原文（387 项）。
> 任务 7 收尾时补了 3 条回归（见 §5.6）之后是 **390 passed**，
> 复现：`& $py -m pytest -p no:logging --tb=no -rN`

```........................................................................ [ 18%]
........................................................................ [ 37%]
........................................................................ [ 55%]
........................................................................ [ 74%]
........................................................................ [ 93%]
...........................                                              [100%]
============================== warnings summary ===============================
E:\Program Files (x86)\suvex-api-venv\Lib\site-packages\fastapi\testclient.py:1
  E:\Program Files (x86)\suvex-api-venv\Lib\site-packages\fastapi\testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
387 passed, 1 warning in 97.99s (0:01:37)
```

---

## 2. 端到端验收

> ⚠️ 下面这段是**任务 6 那一轮**的原文（114 项）。
> 任务 7 收尾时补了 2 条对账（见 §5.6）之后是 **合计 116 项：通过 116，失败 0**。
> 新增的两条是：
>
> ```
> [PASS] ★★ 扫描列表的 findings 计数与漏洞页逐档相等（冗余列被刷新过）
> [PASS] ★★ 扫描列表的 agentCount 与名册一致（不是建行时的 0）
> ```

```Suvex 后端验收（任务 1）
  项目根：C:\Users\Administrator\Desktop\新项目\suvex-api
  验收库：postgresql+psycopg://suvex:suvex_dev_local@127.0.0.1:55432/suvex_acceptance

── 准备 ──
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 5912c13304a2, initial schema
INFO  [alembic.runtime.migration] Running upgrade 5912c13304a2 -> 54eaafabd74c, encrypt provider api keys
INFO  [alembic.runtime.migration] Running upgrade 54eaafabd74c -> 698537531e16, drop scans provider foreign key
INFO  [alembic.runtime.migration] Running upgrade 698537531e16 -> 0f52bf59196c, scan execution backend
INFO  [alembic.runtime.migration] Running upgrade 0f52bf59196c -> 442ee7749c6c, activity event source row
INFO  [alembic.runtime.migration] Running upgrade 442ee7749c6c -> 0da53d27a616, finding source ref and provenance
  [PASS] 验收库已重建并迁移到 head

── 结构检查 ──
  [PASS] Alembic 迁移已执行且停在 head
         数据库版本=0da53d27a616 head=0da53d27a616
  [PASS] 表结构完整（15 张表）
  [PASS] compose 定义了 db 与 api 两个服务
  [PASS] api 等待 db 健康（而不是仅「已启动」）
  [PASS] db 使用 postgres 镜像
         postgres:16-alpine
  [PASS] api 的数据库地址指向 compose 内部的服务名 db:5432
         postgresql+psycopg://suvex:${POSTGRES_PASSWORD:-suvex_dev_local}@db:5432/suvex
  [PASS] db 未映射到宿主端口（只在内网可达）
  [PASS] api 只绑到回环地址
         ['127.0.0.1:8000:8000']
  [PASS] Dockerfile 的启动命令里包含迁移（compose up 一步到位）
  [PASS] 镜像以非 root 用户运行
  [PASS] .dockerignore 排除了 .env（凭据不进镜像层）
  [PASS] db 口令可通过环境变量覆盖
  [PASS] api 接受了 SUPEX_SECRET_KEY（provider 凭据的加密密钥）
  [PASS] ★ 加密密钥**没有**写死的默认值（给默认值等于把加密密钥提交进仓库）
         ${SUPEX_SECRET_KEY:-}

── 启动 uvicorn（127.0.0.1:35795）──
  [PASS] uvicorn 启动并通过 /health

── 验收项 ──
  [PASS] 首次启动 /auth/state 报告「未初始化、未登录」
         {"initialized": false, "authenticated": false}
  [PASS] 弱口令初始化被拒（400 VALIDATION_FAILED）
         口令强度不足，需要：至少 12 个字符、同时包含大写与小写字母、包含数字。
  [PASS] 无凭据访问受保护端点返回 401
         请先登录。
  [PASS] 强口令初始化成功并下发会话 Cookie
         用户 id=user_01M39NYJMWJTXX0WZEK4T3Y8XQ
  [PASS] 会话 Cookie 带齐 HttpOnly / SameSite=Strict / Path / Max-Age
         HttpOnly; Max-Age=43200; Path=/; SameSite=strict
  [PASS] 带会话 Cookie 可访问 /auth/me，字段为 camelCase
         {"id":"user_01M39NYJMWJTXX0WZEK4T3Y8XQ","createdAt":"2026-09-24T12:23:52.788Z"}
  [PASS] 已初始化后 /auth/setup 彻底关闭（409）
         系统已完成初始化，无法再次设置口令。请直接登录。
  [PASS] 签发机器令牌：明文只出现一次，prefix = 明文前 8 位
         prefix=svx_gG59
  [PASS] 机器令牌可作为 Bearer 访问
         HTTP 200
  [PASS] 只读令牌发起扫描被拒（403 INSUFFICIENT_SCOPE）
         当前凭据权限不足：此操作需要「发起扫描」权限。
  [PASS] 无效令牌返回 401
         HTTP 401
  [PASS] 来自未授权源的 Cookie 写请求被拦（403 CROSS_ORIGIN_BLOCKED）
         该请求来自未授权的来源，已被拦截。若这是你自己部署的前端，请把它的地址加进 SUPEX_CORS_ORIGINS。
  [PASS] 未实现端点返回 501 并说明计划任务（目标换成了确实未实现的 /settings）
         HTTP 501 · 设置快照尚未实现（计划在后端任务 7 中交付）。
  [PASS] 登出后会话立即失效（204 → 401）
         logout=204 me=401
  [PASS] 事件流 seed（/events/recent）能读到真实事件
         HTTP 200
  [PASS] agent 名册可读
         HTTP 200
  [PASS] SSE 推送：连上之后新产生的事件被实时推给客户端
         期望事件 evt_01M39NYK5HMFPC6VSEAGXXYRG2，收到 25 行
  [PASS] SSE 心跳：空闲时发注释行防止中间代理断连
         X-Accel-Buffering=no
  [PASS] 新建 provider 成功（id 由服务端分配）
         id=prov_01M39NYT400VGP6DD3A9W6XBSF
  [PASS] 响应只回显掩码，apiKey 恒为空串
         apiKeyMask=sk-****4c1f
  [PASS] 数据库里存的是密文，不含明文
         密文前缀=gAAAAABqtRZgKpjp… 长度=140
  [PASS] 把掩码当值传回时，凭据保持不变（不会把掩码存成新 key）
         label=DeepSeek 生产
  [PASS] 探测成功并返回真实耗时（真的发了一次 HTTP 请求）
         status=ok durationMs=46 endpoint=http://127.0.0.1:39760/v1/chat/completions reply=pong
  [PASS] 探测结果写回了 verified
         verified=True verifyError=None
  [PASS] 探测失败返回 200 + status=failed + 可读中文原因
         HTTP 200 · 无法连接到接入点（[WinError 10061] 由于目标计算机积极拒绝，无法连接。）。请确认 baseUrl 与网络可达性。
  [PASS] 指向云厂商元数据地址的探测被拦截
         该地址是云厂商的实例元数据服务，本平台不会向它发起请求。请填写真正的模型接入点。
  [PASS] 任何时刻只有一个默认 provider
         默认=['prov_01M39NYT400VGP6DD3A9W6XBSF']
  [PASS] 删除仍有替补的默认 provider 被拒（409 PROVIDER_IS_DEFAULT）
         「DeepSeek 生产」是当前默认 provider，不能删除。请先把另一个 provider 设为默认。
  [PASS] 缺授权确认时拒绝创建（403 TARGET_NOT_AUTHORIZED）
         该目标尚未确认授权，无法发起扫描。
  [PASS] 授权措辞与约定不一致时拒绝（400 VALIDATION_FAILED）
         应为「我有权对此目标进行安全测试」
  [PASS] 创建成功：初始状态 queued，目标已归一化
         id=scan_01M39NYWT26SRY1E7XY30SBDWV target=api.example.com（提交的是 https://api.example.com/admin?x=1）
  [PASS] 状态机：queued → running → completed，且时间戳与耗时都落库
         finishedAt=2026-09-24T12:24:12.507Z durationSec=9 runId=fake-scan_01M39NYWT26SRY1E7XY30SBDWV
  [PASS] 读回了执行后端自己生成的 run 标识（任务要求 2）
         runId=fake-scan_01M39NYWT26SRY1E7XY30SBDWV
  [PASS] 状态变更写成了事件（活动流/SSE 能实时看到）
         事件类型=['reasoning', 'tool_call', 'failed', 'discovery', 'reasoning', 'completed']
  [PASS] ★ strix 的结构化事件被解析并落库（reasoning / tool_call / failed / discovery）
         从外部事件库导入并读回的类型=['completed', 'discovery', 'failed', 'reasoning', 'tool_call']
  [PASS] ★ 事件内容与外部工具的原文一致（可回放）
         think 工具的内容被翻成 reasoning(plan)：先做端口与服务识别，再决定打哪里。
  [PASS] 事件 id 唯一且顺序稳定（SSE 的 Last-Event-ID 补发依赖它）
         共 6 条，去重后 6 条
  [PASS] ★ 外部工具的 agent 被映射进本地名册（活动流左侧要用）
         名册=[('reporter', 'done')]
  [PASS] ★ strix 的漏洞记录被导入为结构化条目（字段对齐契约）
         导入 2 条：['medium', 'high']
  [PASS] 详情字段与契约 §2.2 逐字一致（含 poc / remediation）
         poc.language=python affected=['api.example.com']
  [PASS] ★ 证据强度轴（来自 agent）与处置状态轴（人的判断）分列，未合并
         verification=verified（来自 strix 的 confidence） status=new（外部工具不产生人的判断）
  [PASS] ★ 反证/置信度依据被折进 reasoning（没有丢掉）
         Set-Cookie 头里没有 SameSite。

【置信度依据（medium）】只在测试环境复现过

【反证】预发环境返回的头部带了 SameSite=Lax
  [PASS] ★ 批量状态流转成功（待确认 → 已确认）
         changed=1 skipped=0
  [PASS] ★ 变更记录了操作人与时间戳，时间线倒序（最新在前）
         最新一条：actor=user new→confirmed
  [PASS] ★ 幂等：重复标同一状态 → skipped，且不写时间线（避免噪音）
         changed=0 skipped=1 时间线条数=2（未增加）
  [PASS] ★ 非法流转被服务端拒绝（400 VALIDATION_FAILED）
         不能把「待确认」直接改成「已修复」。
  [PASS] ★★★ 统计数字与逐条列表**完全一致**（唯一数据源，两页不会对不上）
         统计 total=4 bySeverity={'high': 2, 'medium': 2} ｜ 列表数出来 4 条 {'medium': 2, 'high': 2}
  [PASS] ★ 每一档的统计数与按该档筛选出来的条数一致（逐档交叉验证）
         全部 2 档一致
  [PASS] ★ falsePositiveCount 单独返回（Overview 排除误报时必须说明排除了几条）
         falsePositiveCount=0
  [PASS] 终止运行中的扫描：状态置为 aborted，且**孙进程也被杀掉**（不留僵尸）
         HTTP 200 · 心跳文件已停止更新
  [PASS] 终止已完成的扫描被拒（409 SCAN_NOT_ABORTABLE）
         这次扫描已经是「已完成」，无法终止。
  [PASS] 重跑建了**新记录**，旧记录的历史原样保留
         新 id=scan_01M39NZ89Y03Q0Z5VRFCPFNDW8 旧 id=scan_01M39NYWT26SRY1E7XY30SBDWV（旧记录仍是 completed）
  [PASS] 并发上限生效：超出的扫描留在队列里，没有一起启动
         running=1 limit=1 queued=3 runner=fake

── 任务 6：报告 ──
  [PASS] ★ 草稿不显示编号（预览与下载的编号必然对得上）
         provisional=True code=''
  [PASS] ★ 预览**没有落库**（历史列表里没有这份草稿）
         当前历史 0 份：[]
  [PASS] ★ 预览报告带上了真实扫描数据（条目数与统计一致）
         条目 2 条 · 风险判断「本次范围内共发现 2 个安全问题，其中 1 个为…」· 范围「扫描任务「api.acme.internal」」
  [PASS] ★ 生成时分配报告编号（RPT-YYYYMMDD-XXXX，前端按此形状解析）
         code=RPT-20260924-B1K8
  [PASS] ★ 生成时冻结了范围与内容选项（含收录条目 id 与顺序）
         收录 2 条 · 排除 0 条 · format=markdown
  [PASS] ★ severityCounts 为 0 的档**键不存在**（Partial，前端按此渲染）
         severityCounts={'high': 1, 'medium': 1}（合计 2 = 收录 2）
  [PASS] ★★★ **预览与导出是同一份文档**（归一化编号/时间后逐字段相同）
         前端预览看到的排版与下载得到的文件一致
  [PASS] ★ 文档内条目顺序 = 冻结的 findingIds 顺序（严重度降序）
         顺序一致，共 2 条
  [PASS] ★ 复现时用的是**冻结的编号**，不是重新分配一个
         文档编号 RPT-20260924-B1K8 = 记录编号 RPT-20260924-B1K8
  [PASS] ★ 下载返回 Markdown 附件，**文件名用编号**（不是中文标题）
         text/markdown; charset=utf-8 · attachment; filename="RPT-20260924-B1K8.md"
  [PASS] ★ 下载内容完整：标题在、不是空壳
         1991 字节，145 行
  [PASS] ★ Markdown 五个部分齐全（摘要 / 总览 / 详情 / 附录 A·B·C）
         六个标题全部命中
  [PASS] ★ 收录的每一条漏洞都在正文里（编号 + 标题逐一命中）
         2 条全部出现
  [PASS] ★ 正文里写的是**真实编号**（不是草稿的「生成后分配」）
         正文含 RPT-20260924-B1K8
  [PASS] 附录 C 如实交代生成时用的选项（筛选方式与是否含 PoC）
         条目筛选=全部状态 · PoC=包含
  [PASS] ★★ includePoc 关掉只去掉脚本，**验证结论保留**（结论不因材料而消失）
         PoC python → 已去掉｜验证标签 已验证 PoC 保持不变
  [PASS] ★ onlyConfirmed 只留「已确认 + 已修复」（修过的仍算发生过，剔除会失真）
         全部 2 条 → 仅已确认 1 条｜留下的状态=['已确认']｜排除 1 条
  [PASS] ★ 被选项排除的条数被如实算出（非零，说明筛选真的发生了）
         范围内 2 条，收录 1 条，排除 1 条
  [PASS] ★★ 冻结的排除数**不随数据变化重算**，且如实写进下载的文件
         生成时排除 1 条 → 改动漏洞后复现仍是 1 条（重算会变成 0）
  [PASS] ★ 冻结的**收录集合**在数据变化后不变（三个月后回看内容一致）
         收录 1 条 → 复现 1 条
  [PASS] ★ 条目的**处置状态反映当前值**（与前端 rebuildReportDocument 一致）
         条目状态标签=已确认 ｜ 活数据=confirmed
  [PASS] ★ 三种范围都可用（按扫描 / 按目标 / 按时间区间）
         target → 200，range(from/to) → 200｜range 收录 8 条
  [PASS] ★ 区间范围的 `from` 键被正确接受（不是被 extra=forbid 拦下）
         范围描述「2020年1月1日 至 2030年12月31日」
  [PASS] ★ 范围是**真的在筛**（空区间得到 0 条，而不是把全部塞进来）
         2000 年那两天收录 0 条
  [PASS] ★ PDF 报告不返回 404，而是如实说明「由前端浏览器打印」
         HTTP 200 · X-Suvex-Note=pdf-via-browser-print
  [PASS] ★ 生成过的报告都进了历史列表
         历史 2 份：['RPT-20260924-KZC8', 'RPT-20260924-B1K8']
  [PASS] ★ 删除报告后：详情与下载都 404，历史列表里也没有了
         DELETE→204 详情→404 下载→404 列表仍在=False
  [PASS] ★★ 删除报告**没有**连带删掉漏洞（只删关联行，不删发现本身）
         删除前 8 条 → 删除后 8 条

── 全局统计（/overview）──
  [PASS] ★★ /overview 不再是 501 占位，且形状与前端 OverviewData 一致
         顶层键=['activity', 'hasAnyScan', 'metrics', 'severityBreakdown']
  [PASS] ★★★ Overview 的严重度分布与 /findings/stats **逐档相等**（同源，两页不会对不上）
         Overview={'critical': 0, 'high': 4, 'medium': 4, 'low': 0, 'info': 0} ｜ stats={'critical': 0, 'high': 4, 'medium': 4, 'low': 0, 'info': 0}
  [PASS] ★ bySeverity 的 Partial 语义正确：为 0 的档**键不存在**，不是 null
         没有显式 null 的档｜键=['high', 'medium']
  [PASS] ★ severityBreakdown 固定返回全部 5 档（与 stats 的 Partial 语义相反，是有意的）
         顺序 critical > high > medium > low > info
  [PASS] ★ activeScans / hasAnyScan 与 /scans 列表数出来的一致（同一张表，不会自相矛盾）
         activeScans=4 列表里 4 条｜hasAnyScan=True
  [PASS] ★ 平均耗时只按**已完成**扫描算（failed/aborted 的耗时是「多久放弃的」，不是一次要多久）
         接口=8 秒 · 列表算出=8 秒 · 已完成 2 次
  [PASS] ★★ 活动流是**投影形状**（扁平、有 subject、无 detail），且不含 reasoning / failed
         8 条｜类型=['blocked', 'completed', 'discovery', 'tool_call']｜字段=['agentId', 'agentPhase', 'at', 'id', 'scanId', 'subject', 'summary', 'type']
  [PASS] ★ 活动流最新在前
         首条=web_search {"query":"api.example.com
  [PASS] ★ falsePositiveCount 与统计接口一致（排除是可以的，悄悄排除不行）
         Overview=0 stats=0
  [PASS] 服务端日志里没有出现明文 API key（含 uvicorn 访问日志）
         检查了服务端 172 行输出
  [PASS] 服务端日志里没有未捕获异常

  已清理 1 个残留的扫描子进程

── 真 strix 集成 ──
  [PASS] 找到 strix 可执行文件
         E:\Program Files (x86)\strix-venv\Scripts\strix.exe
  [PASS] 命令拼装正确，且**凭据不在 argv 里**（只在环境变量里）
         E:\Program Files (x86)\strix-venv\Scripts\strix.exe --target http://127.0.0.1:1 --non-interactive --scan-mode quick --max-turns 1 --instruction 本次测试的侧重范围：Web 应用层漏洞（注入、XSS、越权、SSRF）。 范围之外的发现也请记录，但不要为此展开额外探索。
  [PASS] 环境变量注入正确（模型、关闭遥测）
         STRIX_LLM=deepseek/deepseek-chat STRIX_TELEMETRY=0
  [PASS] strix 子进程跑完并给出退出码
         exit=1 pid=31788
  [PASS] strix 报告 Docker 不可用（本机的预期结果）
         —— 这说明启动链路是通的，唯一缺的是 Docker 引擎。见 README「已知未验证项」。
  [PASS] 能读到子进程的日志尾部（失败原因会来自这里）
                                                                    | ⏎ |                                                                             | ⏎ |                                                                             | ⏎ +-----------------------------------------------------------------------------+

============================================================
合计 114 项：通过 114，失败 0

⚠️ 未验证项：`docker compose up --build` 未在本机执行。
   原因：本机没有 Docker 引擎（Windows 上未启用 WSL2 / 虚拟化平台，
   启用需要重启）。compose 文件与 Dockerfile 只做了静态校验，
   见上面「compose」那几条。
```

---

## 3. 报告两端常量对账（`verify_report_parity.py`）

对比**后端复刻的常量与措辞** vs **前端 TS 源码**（17 项）。

⚠️ 它证明的是"后端复刻的常量与前端的**源码**一致"，
**不**证明"两端对同一份数据产出的 IR 逐字节相同" ——
那需要把前端 `verify:report` 改成打后端 API（要先接上前端）。

```
合计 17 项：通过 17，失败 0
```

---

## 4. 前端契约校验（`suvex-web`）

```
npm run verify:contract   ✓ 契约与源码一致（含"完整事件 8 种 / 投影 6 种"的断言）
npm run verify:coverage   ✓ 需求清单全部覆盖，决策无矛盾
npm run verify:report     ✓ 预览与导出内容一致（含 5 个边界情形）
```


---

## 5. 前端接入（任务 7）暴露出的后端问题与修正

### 5.1 症状

前端把 mock 数据源全部换成真实接口之后，扫描列表上出现了自相矛盾的一屏：

```
扫描任务
全部 1 · 已完成 1
已完成   acme-web.internal   ...   未发现漏洞   5 秒
共 1 次扫描 · 累计发现 0 条漏洞
```

而同一时刻漏洞页写着「共 2 条漏洞 · 2 条待确认」。

### 5.2 根因

`GET /scans` 的 `findings` / `agentCount` 来自扫描行上的**冗余列**
（列表一屏几十条，逐条 count 会变成几十个查询 —— 契约 §3.2 把这两个字段
放进列表响应里，就得有人负责让它们是真的）。而这两列
**只在建行时被写过一次**：

    app/scans/service.py:266-267    agent_count=0, findings={}

刷新它们的 `refresh_scan_rollup()` 写好了、进了 `__all__`、注释也写了
"每次漏洞变动都要重新刷这两个数"，但**一个调用点都没有**：
`grep -r refresh_scan_rollup app/` 只命中「定义」与 `__all__`。

### 5.3 修正 —— 三个调用点，覆盖"谁改了数据"的全部入口

| 调用点 | 覆盖的场景 |
| --- | --- |
| `scans/scheduler.py` · `_import_for()` | 每轮导入之后：事件与漏洞都从这里进库 |
| `scans/service.py` · `finalize()` | 终态兜底：进程起不来的失败路径不经过导入 |
| `findings/service.py` · `change_status()` | 漏洞状态流转：`findings` 的口径**排除误报**，标一条误报就要重算 |

口径只有一处（`scans.service.findings_counts`），三个调用点共用它。
刷新失败的异常**单独吞掉**：汇总列是展示层的冗余，不能因为刷不动它
就让"扫描收尾"或"状态流转"报错（后者尤其糟 —— 用户会再点一次，
而那次因为幂等会返回 `changed: 0`）。

### 5.4 已经落库的旧数据不会自己变对

`_tmp/backfill_rollup.py` 回填一次（幂等）：

```powershell
& $py _tmp\backfill_rollup.py --check   # 先看会改什么，不写库
& $py _tmp\backfill_rollup.py           # 再改
```

```
共 1 条扫描
  scan_01M39YBAR5T5N6E7CADYA6SPW5 completed ({}, 0) → ({'high': 1, 'medium': 1}, 2)  ← 变了
修正了 1 条
```

### 5.5 端到端复验（真实后端）

新起一次扫描，完成后**不做任何人工干预**直接读列表：

```
created=scan_01M3A0S6...  initialFindings={}  initialAgents=0
status=completed  durationSec=6
  rollup 验证扫描:      status=completed  findings={"high":1,"medium":1}  agentCount=1
  acme-web 全量检测:    status=completed  findings={"high":1,"medium":1}  agentCount=2
```

前端扫描列表随之变成（同一份数据，浏览器里读出来的原文）：

```
已完成   rollup.example.com   ...   1 个智能体 · 2 项检测   [高危 1] [中危 1]   6 秒
已完成   acme-web.internal    ...   2 个智能体 · 3 项检测   [高危 1] [中危 1]   5 秒
共 2 次扫描 · 累计发现 4 条漏洞
```

### 5.6 补的检查

验收脚本（`verify_acceptance.py`）新增两条，都在假扫描跑完之后对账：

```
[PASS] ★★ 扫描列表的 findings 计数与漏洞页逐档相等（冗余列被刷新过）
[PASS] ★★ 扫描列表的 agentCount 与名册一致（不是建行时的 0）
```

pytest 新增三条回归（`tests/test_scans.py` 末节「汇总列」）：

- `test_finalize_refreshes_rollup_instead_of_keeping_creation_defaults`
- `test_rollup_counts_findings_by_severity_and_skips_false_positives`
- `test_marking_a_finding_as_false_positive_shrinks_the_scan_rollup`

⚠️ 这三条在**没有修正时会失败**：工厂故意写了一份"看起来合理"的
`{"high": 1}` / `agent_count=1`，断言看的就是它有没有被真值覆盖掉。
（工厂那份值正是后端自己抓不到这个 bug 的原因 ——
断言看到的是工厂捏的数，不是刷新逻辑算的数。）

### 5.7 顺带复验了实时通道

活动流是 SSE（ADR-002）。用一个真实的扫描确认浏览器端确实收到推送：

```
改前     全部活动 0 条 · 实时 0 条 · 还没有事件。
18 秒后  全部活动 6 条 · 实时 6 条
         推理 / 工具调用（exit —）/ 失败 / 发现 / 推理 / 完成
```

`exit —` 是 `exitCode` / `durationMs` 为 `null` 时的**正确**渲染
（契约 §4.2：外部工具不给这两个值时它们是 `null`，不是 0，
更不该显示成 "nullms" 或被当成失败染红）。

---

## 6. 任务 8：安全加固（前端接入之后的收尾轮）

任务 8 的交付物与逐项结论在 **`docs/security-checklist.md`**，
这里只留实测数字与三条命令。

```
& $py -m pytest -p no:logging --tb=line -rN
678 passed, 1 warning in 177.34s (0:02:57)

& $py scripts\verify_acceptance.py
合计 116 项：通过 116，失败 0

& $py scripts\verify_security.py          # 需后端已在跑
合计 24 项：通过 24，失败 0
```

**测试从 390 增到 678（+288）**：任务 8 新增的三个文件（281 项），
外加收尾时修探测缺陷补的 7 项（`tests/test_providers.py`）。

| 文件 | 项数 | 对应要求 |
| --- | --- | --- |
| `tests/test_authorization_matrix.py` | 239 | 要求 1（鉴权穷举） |
| `tests/test_audit.py` | 19 | 要求 2（审计日志） |
| `tests/test_security_hardening.py` | 23 | 要求 3 / 4 / 5 |

> **与 `verify_acceptance.py` 的分工**（这一点在 `verify_security.py` 的
> docstring 里也写了）：那个脚本自起后端、跑在独立验收库上、覆盖任务 1~7；
> 这个脚本要求后端**已经在跑** —— 它要验的正是"你现在部署的这一份"的
> 鉴权与留痕，另起一套环境就失去意义了。

### 收尾：真实配置暴露的探测缺陷（契约 §3.2 的 v0.11）

把真实模型配进平台之后，验证报「对方返回了成功状态，但响应里没有可识别的内容。
请确认 baseUrl 指向的是兼容的模型接口。」

**而用户的配置没有任何问题。** 对方的原始响应是：

```jsonc
{"choices":[{"message":{"role":"assistant",
  "content":"",                                   // ← 空
  "reasoning_content":"The user just said \"ping\". This"},
  "finish_reason":"length"}],
 "usage":{"completion_tokens":8,
  "completion_tokens_details":{"reasoning_tokens":8}}}  // 8 个预算全用在推理上
```

`deepseek-flash` 是**推理型模型**：产出分 `reasoning_content`（想的过程）与
`content`（最终回答）两段，而探测只给 **8** 个 token —— 预算在"想"的阶段就用完了。
实测同一个模型：

| `max_tokens` | `content` | `reasoning_tokens` | `finish_reason` |
| ------------ | --------- | ------------------ | --------------- |
| 8            | `""`      | 8                  | `length`        |
| 64           | `""`      | 64                 | `length`        |
| **256**      | `"pong 🏓 …"` | 36            | **`stop`**      |

两处修：① 判定接受 `reasoning_content`（光认 `content` 把"我们给的预算太小"
读成了"用户的配置有问题"）；② 默认预算 8 → **256**（否则用户看到的是一段
被截断的思考碎片）。

> **一句会把人带偏的假指控，代价比一个 bug 大。** 原文案叫用户去查 `baseUrl`，
> 而 `baseUrl` 错的话前面早就以 404 / 非 JSON 的形式失败了。
> 现在这条失败分支的文案**明确不许提 `baseUrl`**，并有测试钉住
> （`test_verify_treats_200_with_unparsable_body_as_failure`）。

⚠️ 顺带发现：`.env` 里也钉着 `SUPEX_LLM_PROBE_MAX_TOKENS=8`，
它**覆盖**代码默认值 —— 只改代码默认值在这一台机器上不生效。
已把 `.env` 一并改成 256，并加了一条测试断言 `.env.example`
与**字段默认值**一致（断言 field default 而不是 `Settings()`：
后者会读 `.env`，用它做断言等于"谁的 `.env` 写小了谁就把测试跑红"）。

### 本轮修掉的一个**偶发**验收失败（★ S-05）

第一次跑任务 8 的验收时，`★★★ Overview 的严重度分布与 /findings/stats
逐档相等` 失败过一次。原因不是同源不一致，而是**那条断言缺一个前提**：

`verify_overview` 紧跟在 `verify_scans` 之后，而后者会真的起扫描
（还有一个"终止运行中的扫描"的用例）。调度器在后台持续把外部的漏洞产物
导进库，那个导入与"读 `/overview`"和"读 `/findings/stats`"这两次 GET 之间
**没有任何同步** —— 恰好插在中间时两次读到不同快照。

修法是把它缺的前提做实（新增 `_quiesce()`：先等没有 queued/running 的扫描，
再等漏洞总数连续两次相同），**不是放松断言** —— 那会把最强的一条检查
变成一条弱检查。修完连跑两次都是 116/116。

> 顺带记一条因果：这个偶发是**修任务 8 时被抖出来的**。
> 每个写操作都加了审计（多一次 `commit`），timing 变了，
> 本来就存在的竞态才开始暴露。所以"改动之后偶发失败"未必是新 bug ——
> 但它一定值得查清，而不是重跑一次了事。

## 7. 任务 9：批量管理 / 归档删除 / 体验优化

### 7.1 后端新增（契约 v0.13）

| 端点 | 用途 |
| --- | --- |
| `POST /scans/batch/archive` | 批量归档 / 取消归档 |
| `POST /scans/batch/delete` | 批量软删除（进回收站） |
| `POST /scans/batch/restore` | 批量恢复 |
| `POST /scans/batch/rerun` | 批量重跑（**逐条授权 + 逐条审计**） |
| `GET /scans/compare?a=&b=` | 两次扫描的漏洞对比 |
| `DELETE /scans/{scanId}/purge` | 永久删除（**仅限回收站内**） |
| `GET/PUT /settings/scan-retention` | 自动归档天数 / 回收站保留天数 |
| `GET/PUT /settings/scan-views` | 我的视图（覆盖式全量保存） |

`scans` 表加 `archived_at` / `deleted_at` / `terminated_by`；`list_scans` 的可见性
变成**三种互斥模式**（默认 / `archived=true` / `deleted=true`），过滤放在服务端 ——
因为页面上的 tab 计数直接读返回的数组，客户端过滤会让计数与列表对不上。

两条边界值得单独记：

* **归档与删除是正交的**：归档不改变 `status`，"已归档"是一个独立维度，
  不是第六种状态。
* **永久删除必须先经过回收站，而且会被报告引用挡住**。
  `report_findings.finding_id → findings` 与 `findings.scan_id → scans` 都是
  `ON DELETE CASCADE`，直接物理删除一个被报告引用的扫描会把那份报告的内容
  一起掏空。所以 `purge_scan` 返回 409 并**指名**是哪份报告挡住了它，
  而 `sweep_retention` 作为后台流程静默跳过并计数（`keptBecauseReferenced`）。

### 7.2 真机验证抓到的三个前端缺陷

这三个都是**后端全绿、单测拦不住**的：它们的共同点是"客户端手里的数据与
服务端的关系"没写对，只有把页面跑起来、点下去、再回头核对后端才能看见。

#### （1）四个撤销的 `view` 参数两两传反

`runBatch` 的判据是：这次操作之后，记录**还在不在**传进去的这个视图里 ——
在，就乐观移除当前槽；不在（回来了），就等服务端再静默重拉。

撤销是"让记录**回到**视图"的动作，所以它的 `view` 必须指向**记录要去的地方**，
也就是与正向操作一致。页面里四处撤销恰好两两写反：

| 操作 | 原来传的 | 应该传 |
| --- | --- | --- |
| 归档后撤销 | `archived` | `default` |
| 取消归档后撤销 | `default` | `archived` |
| 删除后撤销 | `deleted` | `default` |
| 恢复后撤销 | `default` | `deleted` |

症状特别隐蔽：**不报错，后端也真的改对了**，只是本地不重拉 ——
那条记录就此从眼前消失，直到用户手动刷新。实测：10 条里归档 1 条 → 列表 9，
点「撤销」→ 后端已回到 10，界面仍是 9。

#### （2）一次批量操作会改变**两个**视图

修完（1）还剩一层更深的：`runBatch` 只处理了"用户正看着的这个视图"。
可归档一条记录，它**离开**默认视图的同时**进入**归档视图 ——
而归档槽对此一无所知，它还是上次拉下来的那一份。

症状：切到「已归档」发现刚归档的那条不在里面，切回默认它又在
（默认槽走过乐观更新），用户会认为归档丢数据了。

修法是在槽上引入 `stale` 标记与 `isViewFresh()`：

* `runBatch` 无论走哪个分支，都把**对侧视图**标脏（由 `ENTER_VIEW[kind]` 决定
  记录去了哪儿）；
* 页面加载视图前先问 `isViewFresh(view)`：新鲜 → silent（不闪骨架），
  脏了 → **非** silent（给骨架，而不是先端上一份已知过期的列表）。

选"标脏 + 下次进入时拉"而不是"当场把对侧也拉一遍"：用户可能根本不打开那个 tab，
当场拉就是白烧请求。

真机四个场景（10 条开发数据，每步都回读后端核对）：

| 场景 | 期望 | 实测 |
| --- | --- | --- |
| 归档 1 条 → 默认视图 | 9 | 9 |
| 紧接着切「已归档」 | 1 | 1 |
| 取消归档 → 归档视图 | 0 | 0 |
| 切回默认视图 | 10 | 10 |
| 删除 1 条 → 默认视图 | 9 | 9 |
| 切「回收站」 | 1 | 1 |
| 恢复 → 回收站 | 0 | 0 |
| 切回默认视图 | 10 | 10 |

最后一行就是（2）修复前卡在 9 的那条。

#### （3）快捷键处理器在 `target` 为 `document` 时抛异常

```ts
const target = event.target as HTMLElement | null
const tag = target?.tagName.toLowerCase()   // ← document 走到这里就炸
```

`?.` 只挡住了 `target` 为 null，挡不住「target 存在但没有 `tagName`」。
`document.tagName` 是 `undefined`，紧接着的 `.toLowerCase()` 抛 `TypeError`。

诚实地说：真实用户按键时 `target` 总是聚焦元素，所以**手工操作下不会出现**，
它是被自动化验证（向 `document` 派发合成 `keydown`）逼出来的。但仍然要修 ——
抛出发生在 keydown 处理器里，这一轮所有分支静默失效，且不冒泡到任何错误边界，
排查时看起来像"快捷键根本没绑上"。改成 `target instanceof HTMLElement` 守卫。

修完的实测链路（高亮 = 焦点行）：

| 动作 | 高亮行 | 已选 |
| --- | --- | --- |
| `Esc` | 0 | 0 |
| `j` `j` | 1 | 0 |
| `x` | 1 | 1 |
| `j` `x` | 1 | 2（「对比」按钮出现） |
| `Esc` | 0 | 0 |

### 7.3 前端校验

`typecheck` / `lint --max-warnings 0` / `verify:contract` / `verify:coverage` / `build`
全部通过。契约升到 **v0.13**，其中补记了一条跨客户端约定：`我的视图` 的 `query`
里键名（`status` / `q` / `sort` / `dir`）由客户端定义，**服务端只当不透明串存取、
不解析**；读取规则是「认不出的键忽略，认得出但值非法的键回退默认」，
这正是老视图在新增筛选维度后仍然可用的原因。同时写明**套用视图一律切回默认列表**，
因此 `query` 不含 `archived` / `deleted` 这类可见性参数。

### 7.4 一处顺手修掉的文案不一致

回收站与删除确认框里原来写死「30 天」，而回收站保留天数（`trashRetentionDays`）
是**可配置**的。用户改成 7 天，界面还在说 30 天 —— 一个会随时间必然变错的文案。
改法是**不写死数字**，把用户导向真正显示该数值的地方（「设置 → 扫描保留规则」）：
这里要以零耦合的方式永不失效，而精确数字属于设置页。
