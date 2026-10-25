"""★ 任务 8 要求 1：鉴权的**穷举**验证。

> 遍历契约里所有标注需要鉴权的接口，写一遍自动化测试确认无 token 或
> 错误 token 时全部正确拒绝，尤其是"启动扫描""终止扫描""删除报告"
> 这类高风险操作。

## 为什么这张表要**从 OpenAPI 反查**，而不是手写

`tests/test_protected_endpoints.py` 已经把"无凭据 → 401"写成了一张参数化表，
那张表是**手抄**的。手抄有一个必然的失效模式：**新加端点时忘了补一行**，
而症状是"那个端点悄悄变成公开的"—— 没有任何测试会红。

这里的做法反过来：先把 OpenAPI 里的**全部**端点列出来，
再断言"每一个都在我们显式声明的三张表之一里"。于是：

* 加了一个端点却忘了声明它的鉴权级别 → `test_every_endpoint_is_classified` **红**；
* 声明了 `public` 但其实需要凭据 → `test_public_endpoints_are_really_public` **红**；
* 声明了需要凭据但无凭据却通过了 → `test_all_protected_endpoints_reject_anonymous` **红**。

## 三张表分别是什么

| 表 | 含义 |
| --- | --- |
| `PUBLIC` | 契约 §1.2 定为 `public` 的（登录前必须能调） |
| `READ_ONLY` | 需要 `read`（会话或任意令牌都能过） |
| `SCOPE_GATED` | 需要 `scan` / `admin` —— **只读令牌必须被拒** |

`SCOPE_GATED` 里的每一条都要能回答"为什么它比 read 高"，
所以那个 dict 里带一句话的理由。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

# ══════════════════════════════════════════════════════════
# 三张表
# ══════════════════════════════════════════════════════════

# 契约 §1.2：登录前必须能调用，否则启动流程死锁在登录页上。
PUBLIC: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/v1/health"),
        ("GET", "/api/v1/health/sandbox"),
        # ★ v0.23：指标端点是 **public** 的。
        #
        #   与 `/health` 同一条理由：抓取方是 Prometheus，它**没有我们的
        #   凭据**。给它加鉴权等于让监控系统必须先登录才能取指标 ——
        #   而"监控取不到数据"这件事往往在出事时才发现。
        #
        #   代价是"任何人都能看到请求量"。那个代价是可接受的：
        #   它**只回数字，不含任何业务数据内容**（见 `core/metrics.py`），
        #   而且路由标签用的是**模板**（`/scans/{scanId}`）而不是实际路径，
        #   所以它也泄露不了"有哪些扫描 id"。
        #
        #   不想公开的话，在反代上按网段限制，或配
        #   `SUPEX_METRICS_ENABLED=false` 关掉 —— 两种做法都比
        #   "给指标端点加个静态 token"好（那会被写进抓取配置，
        #   而那份配置往往比指标本身更容易泄漏）。
        ("GET", "/api/v1/metrics"),
        ("GET", "/api/v1/auth/state"),
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/setup"),
        # 登出是幂等的：凭据过期时也要能清 Cookie（契约 §3.1 的 v0.2 放宽）。
        ("POST", "/api/v1/auth/logout"),
        # OpenAPI 文档本身。**它必须公开**，否则前端拿不到接口定义；
        # 而它不泄露任何业务数据（只有字段形状）。
        #
        # ⚠️ 这三个**不在 `app.openapi()["paths"]` 里** —— 它们是 FastAPI
        # 自己注册的文档路由，不参与 schema 生成。所以下面枚举端点时
        # 必须从 `app.routes` 走，只看 OpenAPI 会漏掉它们，
        # 而"漏掉"在这里的表现是"一个公开端点没被任何测试覆盖"。
        # 其中 oauth2-redirect 挂在根上（不带 api 前缀），这是 FastAPI
        # 的既定行为，不是我们的配置错误。
        ("GET", "/api/v1/openapi.json"),
        ("GET", "/api/v1/docs"),
        ("GET", "/docs/oauth2-redirect"),
    }
)

# 需要 `read`：会话或任何令牌都能过。
READ_ONLY: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/v1/auth/me"),
        ("GET", "/api/v1/scans"),
        ("GET", "/api/v1/scans/queue"),
        # 对比是**只读**：它只读两条扫描的漏洞标题做集合差，不改任何状态。
        ("GET", "/api/v1/scans/compare"),
        ("GET", "/api/v1/scans/{scanId}"),
        ("GET", "/api/v1/scans/{scanId}/agents"),
        ("GET", "/api/v1/scans/{scanId}/events"),
        ("GET", "/api/v1/scans/{scanId}/events/recent"),
        # ★ v0.57：线索回显是**只读** —— 只是把"我给过哪些线索"读回来。
        #   写入那条（POST /hints）在 SCOPE_GATED 里，因为它改变智能体的行为。
        ("GET", "/api/v1/scans/{scanId}/hints"),
        ("GET", "/api/v1/events"),
        ("GET", "/api/v1/findings"),
        ("GET", "/api/v1/findings/stats"),
        # ★ v0.15：分组是**只读**聚合 —— 它只是把同一批数据换个切法。
        # 要求更高会让"只有 read 的机器令牌"在分组视图上直接失败，
        # 而它本来就有权看这些漏洞。
        ("GET", "/api/v1/findings/groups"),
        # ★ v0.18：导出与附件读取都是**只读** —— 它们不改任何状态。
        #   导出尤其容易被误标成更高一级（"它把数据带出系统了"），
        #   但 `read` 的语义就是"能读到就能导出"：能看见那份列表的人
        #   本来也能一条条抄走，拦住导出只是增加麻烦，不增加安全。
        ("GET", "/api/v1/findings/export"),
        # v0.70：SARIF 导出与 CSV 导出同为**只读**、同级 read。
        ("GET", "/api/v1/findings/export/sarif"),
        # ★ 任务 12：任务附件是**只读**的列表查询
        ("GET", "/api/v1/scans/{scanId}/attachments"),
        ("GET", "/api/v1/findings/{findingId}/attachments"),
        ("GET", "/api/v1/findings/{findingId}/attachments/{attachmentId}/content"),
        ("GET", "/api/v1/findings/{findingId}"),
        # 名单的**读**放宽到 read：漏洞列表上"负责人"那一列读的就是它。
        # 若要求 admin，一个只有 read 的令牌拉列表时会解析不出负责人名字。
        ("GET", "/api/v1/members"),
        ("GET", "/api/v1/reports"),
        ("POST", "/api/v1/reports"),
        ("POST", "/api/v1/reports/preview"),
        ("GET", "/api/v1/reports/{reportId}"),
        ("GET", "/api/v1/reports/{reportId}/document"),
        ("GET", "/api/v1/reports/{reportId}/download"),
        ("GET", "/api/v1/overview"),
        # ★ v0.24：provider **列表**从 admin 放宽到 read。
        #
        #   这不是顺手放宽，而是"只读角色能用的前提"：前端顶部状态条
        #   要显示"当前模型 XXX"，读的就是这个端点（`Layout` 的
        #   `ensureSettingsLoaded()`）。若它要求 admin，viewer 登录后
        #   会看到状态条报错 —— 而那个错误看起来像"系统坏了"。
        #
        #   放宽是安全的：这个端点**不返回任何明文凭据**
        #   （`apiKey` 恒为空串，只有 `apiKeyMask`，见 ADR-003）。
        #   写操作（新建 / 改 / 删 / 验证 / 设默认）仍然是 admin。
        ("GET", "/api/v1/providers"),
        # ★ v0.20：CTF2 的**只读**端点。
        #
        # 它们与扫描列表同级：看有哪些题、环境起来没有、队列排到哪了。
        # `GET /ctf2/accounts` 也在 read 里 —— 它只返回掩码
        # （`tokenMask`），没有凭据本身。
        #
        # ⚠️ 这几行必须与 `tests/kernel/test_router_auth_matrix.py` 的
        # `READ_ONLY` 逐条一致，那条对账测试会盯着。
        ("GET", "/api/v1/kernel/accounts"),
        ("GET", "/api/v1/kernel/competitions"),
        ("GET", "/api/v1/kernel/challenges"),
        ("GET", "/api/v1/kernel/challenges/{challengeId}"),
        ("GET", "/api/v1/kernel/snapshot"),
        ("GET", "/api/v1/kernel/queue"),
        # ⚠️ 与 `tests/kernel/test_router_auth_matrix.py` 的同一段必须一致
        #    （`test_the_two_matrices_agree` 会盯着）。
        ("GET", "/api/v1/kernel/platforms"),
        ("GET", "/api/v1/kernel/platforms/{platformId}"),
        # ── 题解与 token 统计（v0.36）──────────────────────
        #
        # 三个都是**纯读本地库**：题解是解题收尾时落库的 markdown，
        # 统计是对 `ctf2_solve_usages` 求和。都不打平台、不含凭据。
        #
        # ⚠️ 题解里有 flag 原文（赛后提交给人看的）—— 仍是 read 级：
        #    flag 本来就在 `GET /ctf2/challenges` 的 `confirmedFlag` 里。
        #    需要拔高的是"提交"（对外动作 + 消耗额度），不是"看"。
        ("GET", "/api/v1/kernel/challenges/{challengeId}/writeup"),
        ("GET", "/api/v1/kernel/challenges/{challengeId}/usage"),
        ("GET", "/api/v1/kernel/usage"),
        # 处置建议工单（v0.41）：与题解同级 —— 纯读本地落盘的 markdown，
        # 不打平台、不含凭据；且比题解更不敏感（无 flag、无载荷原文）。
        ("GET", "/api/v1/kernel/challenges/{challengeId}/workorder"),
        # ── 补一条**既有的漏项**（v0.20 顺手补，与 CTF2 无关）──
        #
        # `GET /scans/{scanId}/writeup` 是任务 19（题解）加的端点，
        # 但它**从来没进过这三张表** —— 于是这条分类完整性测试一直是红的。
        #
        # 它显然是 `read`：只读地取一次已完成的夺旗扫描的题解，
        # 不改任何状态。（它读的东西本来就是 `GET /scans/{id}` 的产物。）
        ("GET", "/api/v1/scans/{scanId}/writeup"),
        # ── 工具启用开关：读（v0.82）──────────────────────
        #
        # ★ 为什么是 **read** 而不是 admin：它回的是"哪些工具可用 + 当前
        #   开关值"这份静态清单。不含凭据、不打外部接口、不改安全策略。
        #
        # ⚠️ 与 `GET /kernel/settings`（admin）刻意不同级：后者回的是
        #   `submitMode` —— 那个值决定"机器会不会在无人确认下对外提交"，
        #   属于安全策略。而"哪个工具被关掉了"只是能力清单，看见它
        #   不构成任何后果。**判据是"看见这个值有没有后果"，不是"是不是
        #   内核配置"** —— 否则会把一堆只读清单都拔高到 admin，
        #   而拔高 admin 的副作用是只读账号连页面都打不开。
        ("GET", "/api/v1/kernel/tools"),
        # ── 技能库：读（v0.88）──────────────────────────────────
        #
        # 列表与详情都是**静态文本清单**（名字 + 描述 + 自定义技能的正文），
        # 不含凭据、不改状态、不触发执行 → read 级够用。
        #
        # ⚠️ 详情端点回的是"自定义技能的正文"—— 那是人写的操作手册，
        # 不是凭据。看得见它不构成后果：能看见的人本来也能在页面上读到。
        ("GET", "/api/v1/kernel/skills"),
        ("GET", "/api/v1/kernel/skills/usage"),
        ("GET", "/api/v1/kernel/skills/{name}"),
        # ⚠️ v0.90 加了附件端点后忘了登记，被 `test_every_endpoint_is_classified`
        # 抓住（它扫真实的 OpenAPI 路径全集，不靠人记）。**这条测试的价值就在
        # 这里：新端点漏登记时它红，而不是等上线后靠人工审计发现。**
        #
        # 为什么是 read：`GET /skills/{name}/files` 回的是**技能包附件正文**
        # （`scripts/probe.sh` 这类随技能一起分发的小脚本），与
        # `GET /skills/{name}` 回正文同性质 —— 都是人写的内容、不是凭据、
        # 不改状态、不触发执行。能读详情的人本来就能读附件。
        #
        # 为什么**不是** admin：它对 admin 没有额外暴露面。
        # 但它也**不能是 public** —— 附件里可能有内部脚本路径与用法，
        # 未登录不该看到。
        ("GET", "/api/v1/kernel/skills/{name}/files"),
        # ── 资产总览：读（v0.85）────────────────────────────────
        #
        # ★ 为什么是 **read**：它把已有的扫描记录与漏洞记录按目标聚合
        #   成一份台账（每个目标扫过几次、上次拿到结论是什么时候、
        #   还有几个没处置的高危）。**不返回任何新信息** —— 里面每个
        #   数字，能读 `GET /scans` 和 `GET /findings` 的人本来就能自己
        #   数出来。给更高一级的后果是：只读角色打开"资产总览"整页 403，
        #   而那一页的用途恰恰是"给人看现在该关注什么"。
        ("GET", "/api/v1/assets"),
        # ── 定时复查：读（v0.85）────────────────────────────────
        #
        # 与 `GET /scans` 同级 —— 它返回的就是"排了哪些计划、下次什么时候跑"。
        # 不含凭据（provider 只给 id 和 label），不改状态。
        #
        # ⚠️ 写操作（新建 / 改 / 启停 / 删除）全部在 SCOPE_GATED 里，
        #   见那一侧的理由：一条计划会让系统**在无人值守时对真实目标发包**。
        ("GET", "/api/v1/schedules"),
        # ── 对话页：读（v0.93）──────────────────────────────────
        #
        # ★ 这 7 条**全部是 read 级**，包括看起来"会做事"的那三个写路径
        #   （POST 建会话 / PATCH 改名 / DELETE 删 / POST 发消息）。理由是
        #   对话的两层分工：
        #
        #   * **端点层**只做"会话记录的增删改查" —— 建一条会话、追加一条
        #     消息、改个标题。这些都是**用户自己的笔记本**，和
        #     `POST /reports` 那种"对外产生副作用"的写入不是一回事。
        #   * **工具层**才是真正会碰外部世界的地方（`create_scan` 发包、
        #     `delete_skill` 删文件），而每个工具的 `scope` 在
        #     `chat/prompts.py::ChatToolSpec.scope` 里单独声明，
        #     由 `engine._execute_tool` 在执行前**逐次**校验。
        #
        #   这不是设计洁癖，是实测过的：只读账号（zzviewer88）在真机上
        #   说"帮我建一个扫描任务"，`create_scan` 卡在工具层被拒，
        #   模型回复"当前账号没有 scan 权限…我这边没有绕过的方式"，
        #   扫描没有排上。若把端点拔高到 scan，只读用户连"我上次扫了什么"
        #   都问不了 —— 而那个查询只需要 list_scans（read 级工具）。
        #
        # ⚠️ **判据是"这个端点本身有没有后果"，不是"它背后能做什么"。**
        #   后者会把整个对话页拔到 scan/admin，让只读角色完全用不了它。
        ("GET", "/api/v1/chat/sessions"),
        ("POST", "/api/v1/chat/sessions"),
        ("GET", "/api/v1/chat/sessions/{session_id}"),
        ("PATCH", "/api/v1/chat/sessions/{session_id}"),
        # 删除是自己笔记本里撕掉一页。归属校验（只能删自己的）在
        # `service` 层按 owner 做，与 `scan` scope 无关。
        ("DELETE", "/api/v1/chat/sessions/{session_id}"),
        # 发一条消息：真正做事的是它背后的工具，逐次受 scope 管。
        ("POST", "/api/v1/chat/sessions/{session_id}/messages"),
        # 工具清单是静态元数据（名字 + 说明 + 所需 scope），
        # 与 `GET /kernel/tools` 同性质 —— 是能力清单，不是凭据。
        ("GET", "/api/v1/chat/tools"),
    }
)

# 需要 `scan` / `admin`：**只读令牌必须 403**。
# 值 = 需要哪个 scope；用它同时做"理由"的载体。
SCOPE_GATED: dict[tuple[str, str], str] = {
    # 会对真实目标发包 —— 这正是 `scan` scope 存在的理由
    ("POST", "/api/v1/scans"): "scan",
    ("POST", "/api/v1/scans/{scanId}/abort"): "scan",
    ("POST", "/api/v1/scans/{scanId}/rerun"): "scan",
    # 续跑（任务 16）：与重跑同级 —— 它同样会继续对真实目标发包。
    ("POST", "/api/v1/scans/{scanId}/resume"): "scan",
    # ★ v0.57：运行中补线索。`scan` 级而不是 read —— 它改变智能体接下来
    #   会做什么（把一句话送进下一轮上下文，可能扭转整个方向）。
    #   给 read 的话，只读令牌就能操纵一次正在跑的扫描的决策。
    ("POST", "/api/v1/scans/{scanId}/hints"): "scan",
    # ★ v0.13：批量归档 / 软删除 / 恢复 / 重跑 / 永久删除。
    # 它们**改变数据**，而 `read` 的定义是"只读" ——
    # 给更低级别的话，一个只读令牌就能把整页扫描归档或删进回收站，
    # 那是**掩盖痕迹**，比读到数据严重得多。
    ("POST", "/api/v1/scans/batch/archive"): "scan",
    ("POST", "/api/v1/scans/batch/delete"): "scan",
    ("POST", "/api/v1/scans/batch/restore"): "scan",
    ("POST", "/api/v1/scans/batch/rerun"): "scan",
    # 永久删除是**不可逆**的，与 scan 级一致（能发包的人才有权销毁记录）
    ("DELETE", "/api/v1/scans/{scanId}/purge"): "scan",
    # 改变数据。给它更低级别的话，只读令牌就能把一堆漏洞全标成误报 ——
    # 那是**掩盖问题**，比读到数据严重得多。
    ("POST", "/api/v1/findings/status"): "scan",
    # ★ v0.15：批量指派负责人。同样是**改变数据** ——
    # 给更低级别的话，只读令牌就能把"谁负责什么"整个重排，
    # 而责任归属被改掉比读到数据严重得多（没人知道该找谁了）。
    ("POST", "/api/v1/findings/assign"): "scan",
    # ★ v0.18：软删除 / 恢复 / 永久删除 / 附件增删。
    #
    # 软删除是 scan 级而不是 read：它把条目从默认视图里移走。
    # 给更低级别的话，一个只读令牌就能让一批漏洞"消失" ——
    # 而那是**掩盖问题**，与 `POST /findings/status` 同一条理由。
    ("POST", "/api/v1/findings/batch/delete"): "scan",
    ("POST", "/api/v1/findings/batch/restore"): "scan",
    # 永久删除**不可逆**，与 `DELETE /scans/{id}/purge` 同一条理由
    ("DELETE", "/api/v1/findings/{findingId}/purge"): "scan",
    # 附件上传/删除改变数据，且**把内容带进/带出系统** ——
    # 上传可能含敏感信息（内网截图、凭据抓包），删除是不可逆的。
    ("POST", "/api/v1/findings/{findingId}/attachments"): "scan",
    # ★ 任务 12：题目附件上传/删除。也是 scan 级 ——
    # 它把文件送进那个**会执行任意命令**的沙箱，比读数据严重得多。
    ("POST", "/api/v1/scans/{scanId}/attachments"): "scan",
    # ★ 任务 12：题目附件的**暂存**端点。也在建扫描之前就能调，
    # 所以它同样要 scan 级 —— 它往磁盘写文件、且那些文件会被送进沙箱。
    ("POST", "/api/v1/scans/attachments/staging"): "scan",
    ("DELETE", "/api/v1/scans/{scanId}/attachments/{name}"): "scan",
    ("DELETE", "/api/v1/findings/{findingId}/attachments/{attachmentId}"): "scan",
    # 改设置、管 provider、管令牌、改密码
    #
    # ⚠️ **`GET /providers` 在 v0.24 从 admin 移到了 `READ_ONLY`。**
    #
    #    它原来在这里，论证是"返回的是部署配置的形状（有哪些供应商、
    #    哪个模型、baseUrl、密钥掩码末四位），对能读扫描结果的人来说
    #    是额外的信息面"。那条论证在**单管理员**时代是无害的 ——
    #    反正只有一个人。
    #
    #    而引入 `viewer` 之后它变成了一个真问题：前端顶部状态条
    #    要显示"当前模型 XXX"，读的就是这个端点。若它要求 admin，
    #    viewer 登录后会看到状态条报错 —— 而那个错误看起来像
    #    "系统坏了"，不像权限问题。
    #
    #    重新权衡后的判断：**掩码与模型名不是机密**（它们不包含任何
    #    可用凭据，`apiKey` 恒为空串），而"只读角色能正常使用界面"
    #    是硬需求。所以放宽。
    ("POST", "/api/v1/providers"): "admin",
    ("PATCH", "/api/v1/providers/{providerId}"): "admin",
    ("DELETE", "/api/v1/providers/{providerId}"): "admin",
    ("POST", "/api/v1/providers/{providerId}/verify"): "admin",
    ("POST", "/api/v1/providers/{providerId}/default"): "admin",
    # ★ v0.24：用户管理。这一组是**对权限本身的操作**。
    #
    #   ⚠️ 它们不仅要求 admin scope，还要求**必须是登录会话**
    #   （`require_admin_session`）—— 机器令牌一律拒绝。
    #   理由：令牌若能管用户，它就能自我放大（创建一个新管理员、
    #   或改掉签发者的角色），而令牌是长期有效的，泄漏一个
    #   就等于永久拿到用户管理权。
    #
    #   这一点**矩阵表达不了**（它只有"需要哪个 scope"这一维），
    #   所以真正的守卫在 `tests/test_users_and_roles.py` 的
    #   `test_machine_token_cannot_manage_users`。
    ("GET", "/api/v1/auth/users"): "admin",
    ("POST", "/api/v1/auth/users"): "admin",
    ("PATCH", "/api/v1/auth/users/{userId}"): "admin",
    ("DELETE", "/api/v1/auth/users/{userId}"): "admin",
    ("GET", "/api/v1/settings"): "admin",
    # ★ v0.24：通知的**读**端点保持 `admin`（与 `GET /providers` 相反）。
    #
    #   我一度把它们标成 `read`，而矩阵立刻报红 —— 因为实现用的是
    #   `require_admin`。**矩阵是对的，我的声明是错的。**
    #
    #   重新想清楚两者的差别：
    #
    #   | 端点 | 返回什么 | 能放宽吗 |
    #   | --- | --- | --- |
    #   | `GET /providers` | 模型名 + **掩码**（不可用的字符串） | 能 |
    #   | `GET /settings/notices/channels` | 渠道地址 —— **webhook URL 本身就是凭据** | **不能** |
    #
    #   一个 Slack Webhook URL 拿到就能往那个群发消息，它是**可用的**凭据
    #   （与 provider 的掩码不同 —— 掩码只是末四位）。所以渠道列表
    #   连看都要 admin。
    #
    #   代价是 `viewer` 打开设置页时通知那块会 403。这是可接受的：
    #   设置页对 viewer 本来就基本是只读展示，而通知配置属于
    #   "管理动作"的一部分。**前端要据此隐藏那一块**（而不是显示一个错误）。
    ("GET", "/api/v1/settings/notices/rule"): "admin",
    ("GET", "/api/v1/settings/notices/channels"): "admin",
    # 沙箱与执行配置（任务 13）。并发上限决定"同时有几个进程在对真实目标
    # 发包"，回合上限与扫描深度决定费用 —— 都是 admin 级，不是 read。
    ("GET", "/api/v1/settings/sandbox"): "admin",
    ("PUT", "/api/v1/settings/sandbox"): "admin",
    ("PUT", "/api/v1/settings/notices/rule"): "admin",
    ("POST", "/api/v1/settings/notices/channels"): "admin",
    ("PATCH", "/api/v1/settings/notices/channels/{channelId}"): "admin",
    ("DELETE", "/api/v1/settings/notices/channels/{channelId}"): "admin",
    ("POST", "/api/v1/settings/notices/channels/{channelId}/test"): "admin",
    ("GET", "/api/v1/settings/tokens"): "admin",    ("POST", "/api/v1/settings/tokens"): "admin",
    ("DELETE", "/api/v1/settings/tokens/{tokenId}"): "admin",
    ("POST", "/api/v1/settings/password"): "admin",
    # 扫描保留规则决定"记录什么时候被永久删掉"，是销毁策略 —— admin 级。
    ("GET", "/api/v1/settings/scan-retention"): "admin",
    ("PUT", "/api/v1/settings/scan-retention"): "admin",
    # 「我的视图」是个人资产，但按项目惯例设置类端点一律 admin。
    ("GET", "/api/v1/settings/scan-views"): "admin",
    ("PUT", "/api/v1/settings/scan-views"): "admin",
    # ★ v0.16：SLA 修复时限是**部署策略**（"高危几天算超期"），
    # 与保留规则同级 —— 改它会影响所有人的到期判断，所以是 admin。
    ("GET", "/api/v1/settings/finding-sla"): "admin",
    ("PUT", "/api/v1/settings/finding-sla"): "admin",
    # ★ v0.15：处理人名单的**写**是 admin。
    # 名单决定"漏洞归谁"，而"能改名单"等于"能重塑责任归属" ——
    # 这是配置级权限，不是扫描级。
    ("POST", "/api/v1/members"): "admin",
    ("PATCH", "/api/v1/members/{memberId}"): "admin",
    ("DELETE", "/api/v1/members/{memberId}"): "admin",
    # 审计日志：它记的是"谁做了什么"，给 read 级等于把所有操作者的
    # 来源 IP 与 UA 暴露给任何一个只读令牌。
    ("GET", "/api/v1/audit"): "admin",
    # ★ v0.10 提升：删除报告是**销毁型**且不可逆的操作（编号是客户对账的东西），
    # 而契约 §1.2 给 `read` 的定义是「只读」。原来它是 `read` ——
    # 一个只用来跑报表的令牌能把交付物删干净。见契约 §3.5 的 v0.10 修正。
    ("DELETE", "/api/v1/reports/{reportId}"): "admin",
    # ★ v0.20：CTF2 领域。
    #
    # ── admin：凭据与安全策略 ──
    # 绑定/解绑写的是**凭据**，与 `POST /providers` 同级：给更低级别的话，
    # 一个能发起扫描的机器令牌就能替换平台的 PAT —— 那等于让它能拿
    # 别人的账号去提交 flag。
    ("POST", "/api/v1/kernel/accounts"): "admin",
    ("DELETE", "/api/v1/kernel/accounts/{accountId}"): "admin",
    # `submitMode=auto` 意味着机器会在**无人确认**的情况下对外提交 flag，
    # 而每次错误提交都消耗一个不可再生的额度。这是安全策略，不是界面偏好。
    ("GET", "/api/v1/kernel/settings"): "admin",
    ("PUT", "/api/v1/kernel/settings"): "admin",
    # ── scan：对外动作 / 改数据 ──
    # 同步会改数据（写库）且发出去一串外部请求 —— 给 read 级的话，
    # 一个只读令牌就能反复触发同步，那是一条"用我们的凭据刷平台接口"的路。
    ("POST", "/api/v1/kernel/sync"): "scan",
    # ★ v0.43 应急响应：建一个本地复盘任务。与 `POST /scans` 同级 ——
    # 它会消耗 LLM 额度、会起沙箱容器，所以不能给 read。
    # 同一判据在 `tests/kernel/test_router_auth_matrix.py` 里也有一份。
    ("POST", "/api/v1/kernel/ir/tasks"): "scan",
    # 按链接拉单题：写库 + 打平台接口，与 sync 同级。
    ("POST", "/api/v1/kernel/challenges/pull"): "scan",
    # ★ 这个端点**只读**，但仍然定 scan 级 —— 因为它每次都真的打一次
    # 平台接口（列赛事）。给 read 级的话，一个只读令牌就能反复触发
    # 外部请求，与 `/kernel/sync` 是同一条理由。
    ("GET", "/api/v1/kernel/competitions/remote"): "scan",
    # 起环境 + 调度解题：**对外发包**（`scan` scope 存在的理由）。
    ("POST", "/api/v1/kernel/challenges/{challengeId}/solve"): "scan",
    # ★★ 提交 flag：**对外发出一个不可撤销的动作**，且有配额后果。
    # `read` 的定义是只读 —— 一个只读令牌能替用户扣掉答题机会，显然不成立。
    ("POST", "/api/v1/kernel/challenges/{challengeId}/submit"): "scan",
    ("POST", "/api/v1/kernel/challenges/{challengeId}/cancel"): "scan",
    # 释放平台环境：改状态 + 调平台接口。给 read 级的话，只读令牌就能把
    # 别人正在解题的环境停掉 —— 那会让解题在半路失败。
    ("DELETE", "/api/v1/kernel/challenges/{challengeId}/environment"): "scan",
    # ── v0.23 调度优先级 ───────────────────────────────
    # 改的是**队列顺序**：不改安全策略、不对外发动作、可撤销。
    # 与 `cancel`（改状态、不发包）同类，所以是 scan 而不是 admin。
    # 详见 `tests/kernel/test_router_auth_matrix.py` 里那段判据。
    ("PUT", "/api/v1/kernel/challenges/{challengeId}/priority"): "scan",
    ("POST", "/api/v1/kernel/priorities/by-score"): "scan",
    # ── 平台接入：admin（v0.25）────────────────────────
    # 判据详见 `tests/kernel/test_router_auth_matrix.py` 的同名段落：
    # 这几个端点让服务端去打**用户给的地址**、并把凭据写进账号表。
    ("POST", "/api/v1/kernel/platforms/draft"): "admin",
    ("POST", "/api/v1/kernel/platforms/{platformId}/activate"): "admin",
    ("POST", "/api/v1/kernel/platforms/use-builtin"): "admin",
    ("DELETE", "/api/v1/kernel/platforms/{platformId}"): "admin",
    # ── v0.81 MCP 连接 ────────────────────────────────
    #
    # ★ 三条全部 **admin**，判据不是"read 够不够看一眼"，而是
    #   "这个动作能不能改变智能体的对外能力边界"：
    #
    #   · `PUT` 写的是 `command` —— 一个**会被 exec 的可执行文件路径**，
    #     外加可能含 token 的 `env`。能改它的人就能让智能体拉起任意进程。
    #     这与 `POST /providers`（写凭据）同级，不能给 scan 级。
    #   · `POST /test` 不写库，但它**真的 exec 一次**用户给的 command
    #     并握手。给 read 级的话，一个只读令牌就能拿服务端当任意命令的
    #     执行器用（`command` 由请求体决定）—— 那是一条 RCE 通道，
    #     不是"读一下配置"。这条最容易看错成只读，因为它叫 test。
    #   · `GET` 只读，但它回的是"智能体能拉起哪些外部进程"的完整清单
    #     （含 command 与 args）。这与 `GET /kernel/settings` 同级
    #     ——后者回的是提交策略，同样 admin。
    #
    # ⚠️ 同一判据在 `tests/kernel/test_router_auth_matrix.py` 里也有一份。
    ("GET", "/api/v1/kernel/mcp-servers"): "admin",
    ("PUT", "/api/v1/kernel/mcp-servers"): "admin",
    ("POST", "/api/v1/kernel/mcp-servers/test"): "admin",
    # ── v0.82 工具启用开关：写 ───────────────────────────
    # 改的是智能体**能力边界**。关掉 `run_shell` 会让之后每一次任务的
    # 产出都明显变差，而现场看起来像"模型变笨了"而不是"配置被改过"。
    # 与 `PUT /kernel/settings`（改提交策略）同类 —— 一次点击改变系统行为。
    ("PUT", "/api/v1/kernel/tools"): "admin",
    # ── v0.88 技能库：写 ────────────────────────────────
    #
    # ★ 归 admin 的理由与工具开关**不同**，更强：
    #   改工具改的是"能不能用某个能力"；改技能改的是**模型读到的文本**。
    #   技能正文会进模型上下文，它教模型"怎么做事" —— 包括该守哪些纪律。
    #   所以一段文字就能让模型绕过某条纪律（"这次不用附对照实验"），
    #   而那在事后**完全看不出来**：技能正文不出现在任何执行日志里。
    #
    #   这与 `PUT /kernel/settings`（改提交策略）同级 —— 都是"一次点击
    #   改变系统行为，而后果不在当场显现"。
    ("POST", "/api/v1/kernel/skills"): "admin",
    ("PUT", "/api/v1/kernel/skills/{name}"): "admin",
    ("DELETE", "/api/v1/kernel/skills/{name}"): "admin",
    # ── v0.85 定时复查：写 ──────────────────────────────
    #
    # ★ 这一组全部归 `scan`，而且**新建比改更该拦**。
    #
    #   一条定时计划的本性是「在**没有人看着**的时候，由系统自己向真实
    #   目标发包」。它绕过了两道人眼：① 建扫描时用户要勾"我有权测试"
    #   那句话，而计划只在建的那一刻确认一次；② 之后每次触发都没人
    #   当场知道（通知是事后）。
    #
    #   所以：`read` 绝对不够 —— 一个只读账号能排一条"每 5 分钟扫一次
    #   内网核心系统"的计划，然后什么都不用做，系统自己替它攻击。
    #   这与 `POST /scans/{id}/hints`（改变正在跑的扫描的决策，也是 scan）
    #   同一量级，但**危害更高**，因为它是持续且无人值守的。
    #
    #   「改启停」也是写：用户以为只是"暂停一下"，实际上把一条休眠的计划
    #   唤醒到"下一分钟就对目标发包"。这一条最容易看错成低危。
    ("POST", "/api/v1/schedules"): "scan",
    ("PATCH", "/api/v1/schedules/{schedule_id}"): "scan",
    ("POST", "/api/v1/schedules/{schedule_id}/enabled"): "scan",
    ("DELETE", "/api/v1/schedules/{schedule_id}"): "scan",
}

# 高风险操作单独点名（任务 8 要求 1 的原话）。
HIGH_RISK: list[tuple[str, str]] = [
    ("POST", "/api/v1/scans"),
    ("POST", "/api/v1/scans/{scanId}/abort"),
    ("POST", "/api/v1/scans/{scanId}/rerun"),
    ("POST", "/api/v1/scans/{scanId}/resume"),
    ("DELETE", "/api/v1/reports/{reportId}"),
    ("POST", "/api/v1/findings/status"),
]

# ★ SSE 端点：**响应不会结束**，所以不能用 `client.request()` 去读它。
#
# 这不是"这两个端点没法测"，而是"要用另一种方式测"：
# 它们的鉴权同样在依赖注入里（`require_read`），所以
# `test_all_protected_endpoints_reject_anonymous` 对它们是**有效**的
# （401 在开始推流之前就返回了）。受限的是"已登录后能读到内容"那一条 ——
# 那需要流式读取，属于 SSE 专项测试（`tests/test_events.py`）的范围。
STREAMING_PATHS: frozenset[str] = frozenset(
    {"/api/v1/events", "/api/v1/scans/{scanId}/events"}
)

# 路径参数换成必然不存在的 id：401/403 在**参数校验之前**判定，
# 所以值是多少不影响这几条测试 —— 但用一个明显假的 id
# 能让"这条请求本来会 404"这件事在日志里一眼可读。
FAKE = {"scanId": "scan_nonexistent", "findingId": "finding_nonexistent",
        "reportId": "rpt_nonexistent", "providerId": "prov_nonexistent",
        "tokenId": "tok_nonexistent", "channelId": "ch_nonexistent",
        "name": "nonexistent_skill"}


def _fill(path: str) -> str:
    out = path
    for key, value in FAKE.items():
        out = out.replace("{" + key + "}", value)
    return out


# 这些路由由 Starlette/FastAPI 自动加，不属于业务端点。
# 不忽略的话，框架升级带来的新内部路由会让分类检查变红，
# 而那种红是噪音 —— 它会训练人"看到这条红了就往白名单里加"，
# 那正好毁掉这条测试的价值。
_INTERNAL_PATH_PREFIXES = ("/static",)

_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})


def _declared_paths() -> set[tuple[str, str]]:
    """捞出**全部**端点（方法 + 路径模板）。

    ⚠️ **必须同时从两个来源枚举，任何一个都不完整：**

    | 来源 | 有 | 缺 |
    | --- | --- | --- |
    | `app.openapi()["paths"]` | 全部业务端点（已带 `/api/v1` 前缀） | FastAPI 自己注册的文档路由 |
    | `app.routes` | 文档路由（`/docs`、`/openapi.json`、`/docs/oauth2-redirect`） | 业务端点 |

    为什么 `app.routes` 里没有业务端点：FastAPI 0.141 的 `include_router`
    注册的是一个**惰性包装**（`_IncludedRouter`，9 个，每个路由一个）——
    它既不暴露 `.path` 也不暴露 `.routes`，真正的端点在它的闭包里。
    所以 `app.routes` 只剩 3 条文档路由。

    只用 OpenAPI 会漏掉文档路由，而"漏掉"的表现是
    **一个公开端点没被任何测试覆盖** —— 正是这套测试要防的那件事。
    """
    found: set[tuple[str, str]] = set()

    # 来源一：业务端点
    for path, methods in create_app().openapi()["paths"].items():
        for method in methods:
            if method.upper() in _METHODS:
                found.add((method.upper(), path))

    # 来源二：框架自己注册的路由（文档）
    for route in create_app().routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or not path or path.startswith(_INTERNAL_PATH_PREFIXES):
            continue
        for method in methods:
            if method.upper() in _METHODS:
                found.add((method.upper(), path))

    # 自检：枚举器坏掉时（比如上游改了 include_router 的实现）会返回很少几条，
    # 那种情况下 `stale` 会红，但报错信息会指向"表过期了"，
    # 而真正的原因在枚举器。这条断言把方向指对。
    assert len(found) >= 35, (
        f"端点枚举只找到 {len(found)} 条，明显偏少 —— 枚举器坏了，"
        "不是分类表过期了。请检查 _declared_paths 的两个来源。"
    )
    return found


def _classified_paths() -> set[tuple[str, str]]:
    return set(PUBLIC) | set(READ_ONLY) | set(SCOPE_GATED)


# ══════════════════════════════════════════════════════════
# 分类完整性
# ══════════════════════════════════════════════════════════


def test_every_endpoint_is_classified() -> None:
    """★ 每一个端点都必须在三张表之一里。

    **这是这套测试里最重要的一条。** 它挡的是"新端点忘了声明鉴权级别" ——
    而那种遗漏的症状是端点悄悄变成公开的，不会有别的测试报出来。
    """
    declared = _declared_paths()
    classified = _classified_paths()

    unclassified = declared - classified
    assert not unclassified, (
        "以下端点没有声明鉴权级别。请把它们加进本文件的 PUBLIC / READ_ONLY / "
        f"SCOPE_GATED 之一：\n  {sorted(unclassified)}"
    )

    stale = classified - declared
    assert not stale, (
        f"以下端点已经不存在了，请从本文件的表里删掉：\n  {sorted(stale)}"
    )


def test_the_three_tables_do_not_overlap() -> None:
    """一个端点只能有一个级别 —— 重叠说明有人复制粘贴时忘了删旧的。"""
    assert not (PUBLIC & set(READ_ONLY))
    assert not (PUBLIC & set(SCOPE_GATED))
    assert not (set(READ_ONLY) & set(SCOPE_GATED))


def test_high_risk_operations_are_scope_gated() -> None:
    """任务 8 点名的高风险操作**必须**在 `SCOPE_GATED` 里。

    单独一条而不是靠上面那条：如果某天有人把 `POST /scans` 误加进
    `READ_ONLY`，`test_every_endpoint_is_classified` 照样通过
    （它在某张表里），只有这一条会红。
    """
    for item in HIGH_RISK:
        assert item in SCOPE_GATED, f"{item} 是高风险操作，必须在 SCOPE_GATED 里"


# ══════════════════════════════════════════════════════════
# 无凭据 → 401
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(("method", "path"), sorted(set(READ_ONLY) | set(SCOPE_GATED)))
def test_all_protected_endpoints_reject_anonymous(
    client: TestClient, method: str, path: str
) -> None:
    """★ 无凭据 → **401**，统一错误信封。

    状态码必须是 401 而不是 501/404/403：鉴权依赖在端点函数体之前执行，
    所以"未实现的端点"（`/settings` 的那些 501）同样是先 401 ——
    否则未实现的端点会变成**免鉴权的存在性探测**。
    """
    response = client.request(method, _fill(path))

    assert response.status_code == 401, (
        f"{method} {path} → {response.status_code}（无凭据必须 401）\n{response.text[:300]}"
    )
    body = response.json()
    assert body["error"]["code"] == "UNAUTHENTICATED"
    assert body["error"]["message"] == "请先登录。"


@pytest.mark.parametrize(("method", "path"), sorted(PUBLIC))
def test_public_endpoints_are_really_public(client: TestClient, method: str, path: str) -> None:
    """`public` 端点**不能**返回 401。

    它们的存在理由就是"登录前要能调用"：`/auth/state` 决定显示初始化页
    还是登录页，`/health` 的调用方是编排器。任何一条变成 401，
    启动流程就死锁在登录页上。
    """
    response = client.request(method, _fill(path), json={} if method == "POST" else None)

    assert response.status_code != 401, (
        f"{method} {path} 不该要求凭据（返回了 401）"
    )


# ══════════════════════════════════════════════════════════
# 错误 token → 401
# ══════════════════════════════════════════════════════════

BAD_TOKENS: list[tuple[str, str]] = [
    ("空串", "Bearer "),
    ("只有 Bearer 没有值", "Bearer"),
    ("形状不对", "Bearer not-a-suvex-token"),
    ("前缀对但不存在", "Bearer svx_thisTokenDoesNotExistAtAll123456"),
    ("只有前缀", "Bearer svx_"),
    ("别的方案", "Basic YWRtaW46YWRtaW4="),
    ("没有方案名", "svx_looksLikeATokenButHasNoScheme"),
    ("前缀截断", "Bearer sv"),
]


@pytest.mark.parametrize(("label", "header"), BAD_TOKENS, ids=[x[0] for x in BAD_TOKENS])
@pytest.mark.parametrize(
    ("method", "path"),
    sorted(SCOPE_GATED),
)
def test_bad_tokens_are_rejected_on_every_gated_endpoint(
    ready_client: TestClient, method: str, path: str, label: str, header: str
) -> None:
    """★ 形状不对 / 不存在的令牌 → 401。

    在**每一个** scope 受限端点上跑一遍，而不是只挑一个代表性的：
    "401 是依赖注入统一做的"这个前提值得被验证一次，
    因为一旦有人给某个端点换成手写鉴权，就只有那个端点会漏。

     同时也覆盖了"错误 token 不能当匿名通过"这条 ——
    401 与 403 的区分在这里很关键（见下一条测试）。
    """
    ready_client.cookies.clear()
    response = ready_client.request(method, _fill(path), headers={"Authorization": header})

    assert response.status_code == 401, (
        f"{method} {path} 在 {label} 时返回了 {response.status_code}，应为 401"
    )
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_scope_denial_is_403_not_401(ready_client: TestClient) -> None:
    """★ **已认证但权限不足 = 403**，不是 401。

    这个区分不是审美：401 会让前端的 `apiRequest` 走"跳登录页"那条路，
    而用户其实登录着 —— 于是他被莫名其妙踢出登录页，再登录一次，
    还是 403。403 才是"你需要更高权限"的正确表达。
    """
    created = ready_client.post(
        "/api/v1/settings/tokens",
        json={"label": "只读脚本", "scopes": ["read"], "expiresAt": None},
    )
    assert created.status_code == 201, created.text
    token = created.json()["plaintext"]
    ready_client.cookies.clear()
    headers = {"Authorization": f"Bearer {token}"}

    for method, path in sorted(SCOPE_GATED):
        response = ready_client.request(method, _fill(path), headers=headers)
        assert response.status_code == 403, (
            f"{method} {path} 用只读令牌得到了 {response.status_code}，应为 403\n"
            f"{response.text[:200]}"
        )
        error = response.json()["error"]
        assert error["code"] == "INSUFFICIENT_SCOPE"
        # 文案要说明**需要什么权限**，否则用户不知道该申请哪个
        assert error["message"], "message 不能为空"


def test_read_only_token_can_still_read(ready_client: TestClient) -> None:
    """反面：只读令牌**读得动**。

    少了这一条，上面那条 `assert 403` 可以被"所有请求都 403"满足 ——
    一个把 read 级端点也一起拒掉的实现会全绿。
    """
    created = ready_client.post(
        "/api/v1/settings/tokens",
        json={"label": "只读脚本", "scopes": ["read"], "expiresAt": None},
    )
    token = created.json()["plaintext"]
    ready_client.cookies.clear()

    for method, path in sorted(READ_ONLY):
        if path in STREAMING_PATHS:
            # 见 STREAMING_PATHS 的说明：这里读它会挂住（流不会结束）。
            continue
        response = ready_client.request(
            method, _fill(path), headers={"Authorization": f"Bearer {token}"}
        )
        # 404 是允许的（那个假 id 确实不存在）—— 它证明鉴权过了。
        # 401/403 都不允许。
        assert response.status_code not in (401, 403), (
            f"{method} {path} 用只读令牌被拒了（{response.status_code}），"
            "read 级端点不该要求更高权限"
        )
