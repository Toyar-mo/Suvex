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
        # ★ v0.20：CTF2 的**只读**端点。
        #
        # 它们与扫描列表同级：看有哪些题、环境起来没有、队列排到哪了。
        # `GET /ctf2/accounts` 也在 read 里 —— 它只返回掩码
        # （`tokenMask`），没有凭据本身。
        #
        # ⚠️ 这几行必须与 `tests/ctf2/test_router_auth_matrix.py` 的
        # `READ_ONLY` 逐条一致，那条对账测试会盯着。
        ("GET", "/api/v1/ctf2/accounts"),
        ("GET", "/api/v1/ctf2/competitions"),
        ("GET", "/api/v1/ctf2/challenges"),
        ("GET", "/api/v1/ctf2/challenges/{challengeId}"),
        ("GET", "/api/v1/ctf2/snapshot"),
        ("GET", "/api/v1/ctf2/queue"),
        # ⚠️ 与 `tests/ctf2/test_router_auth_matrix.py` 的同一段必须一致
        #    （`test_the_two_matrices_agree` 会盯着）。
        ("GET", "/api/v1/ctf2/platforms"),
        ("GET", "/api/v1/ctf2/platforms/{platformId}"),
        # ── 补一条**既有的漏项**（v0.20 顺手补，与 CTF2 无关）──
        #
        # `GET /scans/{scanId}/writeup` 是任务 19（题解）加的端点，
        # 但它**从来没进过这三张表** —— 于是这条分类完整性测试一直是红的。
        #
        # 它显然是 `read`：只读地取一次已完成的夺旗扫描的题解，
        # 不改任何状态。（它读的东西本来就是 `GET /scans/{id}` 的产物。）
        ("GET", "/api/v1/scans/{scanId}/writeup"),
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
    # ⚠️ **`GET /providers` 也是 `admin`**，不是 `read`。
    # 契约 §3.2 整节标的是 `admin`，依据是它返回的是**部署配置的形状**
    # （有哪些供应商、用的哪个模型、baseUrl、密钥掩码的末四位）。
    # 掩码本身不是机密，但"你用了哪家、哪个模型、key 末四位"对
    # 一个已经能读扫描结果的人来说是额外的信息面，而对配置管理界面来说
    # 它本来就属于 admin。前端的设置页与状态条都走会话，不受影响。
    ("GET", "/api/v1/providers"): "admin",
    ("POST", "/api/v1/providers"): "admin",
    ("PATCH", "/api/v1/providers/{providerId}"): "admin",
    ("DELETE", "/api/v1/providers/{providerId}"): "admin",
    ("POST", "/api/v1/providers/{providerId}/verify"): "admin",
    ("POST", "/api/v1/providers/{providerId}/default"): "admin",
    ("GET", "/api/v1/settings"): "admin",
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
    ("POST", "/api/v1/ctf2/accounts"): "admin",
    ("DELETE", "/api/v1/ctf2/accounts/{accountId}"): "admin",
    # `submitMode=auto` 意味着机器会在**无人确认**的情况下对外提交 flag，
    # 而每次错误提交都消耗一个不可再生的额度。这是安全策略，不是界面偏好。
    ("GET", "/api/v1/ctf2/settings"): "admin",
    ("PUT", "/api/v1/ctf2/settings"): "admin",
    # ── scan：对外动作 / 改数据 ──
    # 同步会改数据（写库）且发出去一串外部请求 —— 给 read 级的话，
    # 一个只读令牌就能反复触发同步，那是一条"用我们的凭据刷平台接口"的路。
    ("POST", "/api/v1/ctf2/sync"): "scan",
    # 按链接拉单题：写库 + 打平台接口，与 sync 同级。
    ("POST", "/api/v1/ctf2/challenges/pull"): "scan",
    # ★ 这个端点**只读**，但仍然定 scan 级 —— 因为它每次都真的打一次
    # 平台接口（列赛事）。给 read 级的话，一个只读令牌就能反复触发
    # 外部请求，与 `/ctf2/sync` 是同一条理由。
    ("GET", "/api/v1/ctf2/competitions/remote"): "scan",
    # 起环境 + 调度解题：**对外发包**（`scan` scope 存在的理由）。
    ("POST", "/api/v1/ctf2/challenges/{challengeId}/solve"): "scan",
    # ★★ 提交 flag：**对外发出一个不可撤销的动作**，且有配额后果。
    # `read` 的定义是只读 —— 一个只读令牌能替用户扣掉答题机会，显然不成立。
    ("POST", "/api/v1/ctf2/challenges/{challengeId}/submit"): "scan",
    ("POST", "/api/v1/ctf2/challenges/{challengeId}/cancel"): "scan",
    # 释放平台环境：改状态 + 调平台接口。给 read 级的话，只读令牌就能把
    # 别人正在解题的环境停掉 —— 那会让解题在半路失败。
    ("DELETE", "/api/v1/ctf2/challenges/{challengeId}/environment"): "scan",
    # ── v0.23 调度优先级 ───────────────────────────────
    # 改的是**队列顺序**：不改安全策略、不对外发动作、可撤销。
    # 与 `cancel`（改状态、不发包）同类，所以是 scan 而不是 admin。
    # 详见 `tests/ctf2/test_router_auth_matrix.py` 里那段判据。
    ("PUT", "/api/v1/ctf2/challenges/{challengeId}/priority"): "scan",
    ("POST", "/api/v1/ctf2/priorities/by-score"): "scan",
    # ── 平台接入：admin（v0.25）────────────────────────
    # 判据详见 `tests/ctf2/test_router_auth_matrix.py` 的同名段落：
    # 这几个端点让服务端去打**用户给的地址**、并把凭据写进账号表。
    ("POST", "/api/v1/ctf2/platforms/draft"): "admin",
    ("POST", "/api/v1/ctf2/platforms/{platformId}/activate"): "admin",
    ("POST", "/api/v1/ctf2/platforms/use-builtin"): "admin",
    ("DELETE", "/api/v1/ctf2/platforms/{platformId}"): "admin",
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
        "tokenId": "tok_nonexistent", "channelId": "ch_nonexistent"}


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
