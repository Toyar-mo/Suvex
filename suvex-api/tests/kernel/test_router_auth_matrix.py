"""CTF2 端点的鉴权矩阵（仿 `tests/test_authorization_matrix.py`）。

## ★ 为什么需要**第二个**矩阵文件，而不是往原来那个里加几行

那个文件的三张表是**全站唯一**的分类权威，它有
`test_every_endpoint_is_classified`：任何新端点不在三张表里就红。

那是对的，但它有一个真实的代价：加一个领域就要去改一个
近 500 行的、与这个领域无关的文件。所以这里的做法是：

* 本文件维护 CTF2 的**子集**（三张表，逐条带理由）；
* `test_the_two_matrices_agree` 断言**两份表在 CTF2 端点上完全一致** ——
  于是既有的全量分类测试不会红，而本文件的表也不会与它漂移。

⚠️ **仍然必须**把 CTF2 的端点加进 `tests/test_authorization_matrix.py`
的三张表（那是全站权威）。`test_the_two_matrices_agree` 失败时会
把差集原样打出来，那几行就是可以逐条抄过去的内容。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

# ══════════════════════════════════════════════════════════
# 三张表（CTF2 子集）
# ══════════════════════════════════════════════════════════

# 不需要凭据的 CTF2 端点 —— **一个都没有**。
#
# ★ 空集是结论，不是"还没填"。CTF2 的每一个端点要么读绑定信息、
#   要么动平台资源，没有一条是登录前必须能调的（不像 `/auth/state`
#   那样卡在启动流程上）。把它写成空集而不是删掉这个表，
#   是为了让 `test_the_two_matrices_agree` 有一个明确的对照面。
PUBLIC: frozenset[tuple[str, str]] = frozenset()

# 需要 `read`：只读，且**不含凭据**。
READ_ONLY: frozenset[tuple[str, str]] = frozenset(
    {
        # 账号列表 —— 只有掩码（`tokenMask`），没有 token。
        # 它不需要 admin 的理由与 `GET /scans` 一样：看列表是日常动作。
        ("GET", "/api/v1/ctf2/accounts"),
        ("GET", "/api/v1/ctf2/competitions"),
        ("GET", "/api/v1/ctf2/challenges"),
        ("GET", "/api/v1/ctf2/challenges/{challengeId}"),
        ("GET", "/api/v1/ctf2/snapshot"),
        # 队列状态：与 `GET /scans/queue` 同级（它回答"为什么排在队列里"）
        ("GET", "/api/v1/ctf2/queue"),
        # 平台列表与详情：只读**本地数据库**（不打平台）—— 与
        # `GET /ctf2/accounts` 同级。生成过程的状态也在详情里，前端
        # 轮询它，所以它必须是 read 级（否则只读令牌看不了进度）。
        ("GET", "/api/v1/ctf2/platforms"),
        ("GET", "/api/v1/ctf2/platforms/{platformId}"),
        # ── 题解与 token 统计（v0.36）──────────────────────
        #
        # 三个都是**纯读本地库**：题解是解题收尾时落库的 markdown，
        # 统计是对 `ctf2_solve_usages` 求和。都不打平台、不含凭据。
        #
        # ⚠️ 题解里**有 flag 原文**（它是赛后提交给人看的）——
        #    但这仍然够 read 级：flag 本来就在 `GET /ctf2/challenges`
        #    的 `confirmedFlag` 里（同一份数据、同一个级别）。
        #    需要拔高的是"提交"（对外动作 + 消耗不可再生额度），不是"看"。
        ("GET", "/api/v1/ctf2/challenges/{challengeId}/writeup"),
        ("GET", "/api/v1/ctf2/challenges/{challengeId}/usage"),
        ("GET", "/api/v1/ctf2/usage"),
        # ── 处置建议工单（v0.41）──────────────────────────
        #
        # 与题解同一级别、同一理由：它是解题收尾时落盘的 markdown，
        # **纯读本地文件**，不打平台、不含凭据。
        #
        # ⚠️ 它比题解**更不敏感**：工单是"要去做什么"的清单，
        #    不含 flag、不含攻击载荷原文（`workorder.py` 只收四个
        #    文本字段，`detail_evidence`/`poc_code` 一概不碰）。
        ("GET", "/api/v1/ctf2/challenges/{challengeId}/workorder"),
    }
)

# 需要 `scan` / `admin`：**只读令牌必须 403**。
SCOPE_GATED: dict[tuple[str, str], str] = {
    # ── admin：凭据与安全策略 ──────────────────────────
    #
    # 写凭据 = 与 `POST /providers` 同级。给更低级别的话，
    # 一个能发起扫描的机器令牌就能**替换平台的 PAT** ——
    # 那等于让它能拿别人的账号去提交 flag。
    ("POST", "/api/v1/ctf2/accounts"): "admin",
    # 销毁凭据。
    ("DELETE", "/api/v1/ctf2/accounts/{accountId}"): "admin",
    # ★ 提交策略是**安全策略**，不是界面偏好。
    #
    # `submitMode=auto` 意味着机器会在**无人确认**的情况下对外提交 flag，
    # 而每次错误提交都消耗一个不可再生的额度。改它的人必须是有权改配置的人。
    ("GET", "/api/v1/ctf2/settings"): "admin",
    ("PUT", "/api/v1/ctf2/settings"): "admin",
    # ── scan：对外动作 / 改数据 ─────────────────────────
    #
    # 同步会改数据（写库）且发出去一串外部请求。给 read 级的话，
    # 一个只读令牌就能反复触发同步 —— 那是一条"用我们的凭据刷平台接口"的路。
    ("POST", "/api/v1/ctf2/sync"): "scan",
    # ★ v0.43 应急响应：建一个本地复盘任务。
    #
    # 为什么是 scan 而不是 admin：它不写凭据、不改安全策略
    # （`/ctf2/settings` 那种才是 admin）。它做的事与"起一次扫描"
    # 同类 —— 创建一个会消耗 LLM 额度、会起沙箱容器的任务。
    # 按 `POST /scans` 的同级处理。
    #
    # 为什么不是 read：它不是只读。建完之后这道题就在队列里可以被
    # 反复开跑，一个只读令牌能靠反复调用把 LLM 额度刷光。
    ("POST", "/api/v1/ctf2/ir/tasks"): "scan",
    # 按链接拉单题：写库 + 打平台接口。
    ("POST", "/api/v1/ctf2/challenges/pull"): "scan",
    # ★ 列平台赛事**只读**，但仍然 scan 级 —— 它每次都真的打一次平台接口。
    ("GET", "/api/v1/ctf2/competitions/remote"): "scan",
    # 起环境 + 调度 LLM 解题：**对外发包**（这正是 scan scope 存在的理由）。
    ("POST", "/api/v1/ctf2/challenges/{challengeId}/solve"): "scan",
    # ★★ 提交 flag：**对外发出一个不可撤销的动作**，且有配额后果。
    #     `read` 的定义是只读 —— 一个只读令牌能替用户扣掉答题机会，
    #     显然不成立。
    ("POST", "/api/v1/ctf2/challenges/{challengeId}/submit"): "scan",
    ("POST", "/api/v1/ctf2/challenges/{challengeId}/cancel"): "scan",
    # 释放平台环境：改状态 + 调平台接口。给 read 级的话，
    # 只读令牌就能把别人正在解题的环境停掉 —— 那会让解题在半路失败。
    ("DELETE", "/api/v1/ctf2/challenges/{challengeId}/environment"): "scan",
    # ── v0.23 调度优先级 ───────────────────────────────
    #
    # ★ 归 `scan` 而不归 `admin`：它改的是**队列顺序**，不改任何安全策略，
    #   也不对外发动作。判据与 `cancel`（改状态、不发包、可撤销）同类。
    #
    # ★★ 也**不**归 HIGH_RISK：那份名单的判据是"不可撤销 / 对外发动作 /
    #    销毁凭据"。改一个排序权重三条都不沾 —— 再调一次就回去了，
    #    而且中断不了任何已经在跑的题（见 `service.set_challenge_priority`
    #    的说明）。把它算成高风险会让"比赛现场快速调优先级"这个
    #    被规则明确允许的动作变得碍手碍脚。
    ("PUT", "/api/v1/ctf2/challenges/{challengeId}/priority"): "scan",
    ("POST", "/api/v1/ctf2/priorities/by-score"): "scan",
    # ── 平台接入：**admin**（v0.25）─────────────────────
    #
    # ★ 归 admin 而不是 scan，理由与「绑定账号」「改提交策略」同一类：
    #   这几个端点收的是"**服务端要去打哪个地址**"。
    #
    #   `draft` 会让服务端真的请求用户填的 baseUrl（内网地址**不拦**，
    #   因为比赛平台常常就在内网）；`activate` 会把凭据写进账号表，
    #   并把"以后的题目从哪来"整个换掉。
    #
    #   这与"开始解题"（scan 级）不是一回事 —— 那个是把**已有的题**
    #   丢给沙箱，这个是让服务端发起一次到任意主机的请求。
    ("POST", "/api/v1/ctf2/platforms/draft"): "admin",
    ("POST", "/api/v1/ctf2/platforms/{platformId}/activate"): "admin",
    ("POST", "/api/v1/ctf2/platforms/use-builtin"): "admin",
    ("DELETE", "/api/v1/ctf2/platforms/{platformId}"): "admin",
}

# 高风险操作单独点名。
#
# 判据与全站那份一致：**不可撤销 / 对外发动作 / 销毁凭据**。
HIGH_RISK: list[tuple[str, str]] = [
    ("POST", "/api/v1/ctf2/challenges/{challengeId}/submit"),
    # 让服务端去请求一个**用户给的地址**（对外发动作），以及把凭据写进
    # 账号表并把数据来源整个换掉。
    ("POST", "/api/v1/ctf2/platforms/draft"),
    ("POST", "/api/v1/ctf2/platforms/{platformId}/activate"),
    # 删掉一份配置是不可撤销的（要重新生成得再花几分钟和几次模型调用）。
    ("DELETE", "/api/v1/ctf2/platforms/{platformId}"),
    ("POST", "/api/v1/ctf2/challenges/{challengeId}/solve"),
    ("DELETE", "/api/v1/ctf2/accounts/{accountId}"),
    ("PUT", "/api/v1/ctf2/settings"),
]

FAKE = {"challengeId": "ctf2chal_nonexistent", "accountId": "ctf2acc_nonexistent"}

_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_PREFIX = "/api/v1/ctf2"


def _fill(path: str) -> str:
    out = path
    for key, value in FAKE.items():
        out = out.replace("{" + key + "}", value)
    return out


def _declared_ctf2_paths() -> set[tuple[str, str]]:
    """从 OpenAPI 里捞出**全部** CTF2 端点。

    ⚠️ 只看 `/ctf2` 前缀的那些。全站分类属于
    `tests/test_authorization_matrix.py`，这里不做（也不该做）——
    那个文件有自己的枚举器与自检。
    """
    found: set[tuple[str, str]] = set()
    for path, methods in create_app().openapi()["paths"].items():
        if not path.startswith(_PREFIX):
            continue
        for method in methods:
            if method.upper() in _METHODS:
                found.add((method.upper(), path))
    return found


def _classified() -> set[tuple[str, str]]:
    return set(PUBLIC) | set(READ_ONLY) | set(SCOPE_GATED)


# ══════════════════════════════════════════════════════════
# 分类完整性
# ══════════════════════════════════════════════════════════


def test_every_ctf2_endpoint_is_classified() -> None:
    """★ 每一个 CTF2 端点都必须在本文件的三张表之一里。

    它挡的是"新加端点忘了声明鉴权级别" —— 而那种遗漏的症状是
    端点悄悄变成公开的（FastAPI 的默认是"没有依赖 = 不鉴权"），
    不会有别的测试报出来。
    """
    declared = _declared_ctf2_paths()
    assert declared, "没有找到任何 CTF2 端点 —— 路由没注册，或前缀写错了"

    unclassified = declared - _classified()
    assert not unclassified, (
        "以下 CTF2 端点没有声明鉴权级别，请加进本文件的 "
        f"PUBLIC / READ_ONLY / SCOPE_GATED：\n  {sorted(unclassified)}"
    )
    stale = _classified() - declared
    assert not stale, f"以下端点已不存在，请从表里删掉：\n  {sorted(stale)}"


def test_the_two_matrices_agree() -> None:
    """★ 本文件的 CTF2 子集必须与**全站那份**逐条一致。

    两份表的存在是有理由的（见模块 docstring），但两份表**必然漂移** ——
    除非有东西盯着。这条就是那个东西。

    ⚠️ 失败时的报错要能直接照着改：所以它把差集原样打出来。
    """
    from tests.test_authorization_matrix import (
        READ_ONLY as MAIN_READ_ONLY,
    )
    from tests.test_authorization_matrix import (
        SCOPE_GATED as MAIN_SCOPE_GATED,
    )

    main_read = {item for item in MAIN_READ_ONLY if item[1].startswith(_PREFIX)}
    main_gated = {item for item in MAIN_SCOPE_GATED if item[1].startswith(_PREFIX)}

    assert main_read == set(READ_ONLY), (
        "CTF2 的只读端点在两份表里不一致。\n"
        f"  只在 tests/test_authorization_matrix.py 里：{sorted(main_read - set(READ_ONLY))}\n"
        f"  只在本文件里：{sorted(set(READ_ONLY) - main_read)}\n"
        "请把两边改成一致（全站那份是权威）。"
    )
    assert main_gated == set(SCOPE_GATED), (
        "CTF2 的受限端点在两份表里不一致。\n"
        f"  只在 tests/test_authorization_matrix.py 里：{sorted(main_gated - set(SCOPE_GATED))}\n"
        f"  只在本文件里：{sorted(set(SCOPE_GATED) - main_gated)}"
    )


def test_the_tables_do_not_overlap() -> None:
    assert not (set(PUBLIC) & set(READ_ONLY))
    assert not (set(PUBLIC) & set(SCOPE_GATED))
    assert not (set(READ_ONLY) & set(SCOPE_GATED))


def test_high_risk_operations_are_scope_gated() -> None:
    """高风险操作必须在 `SCOPE_GATED`。

    单独一条而不是靠上面那条：如果某天有人把 `submit` 误加进
    `READ_ONLY`，`test_every_ctf2_endpoint_is_classified` 照样通过
    （它在某张表里），只有这一条会红。
    """
    for item in HIGH_RISK:
        assert item in SCOPE_GATED, f"{item} 是高风险操作，必须在 SCOPE_GATED 里"


def test_binding_and_policy_are_admin_not_scan() -> None:
    """★ 单独钉住两条：**凭据**与**安全策略**是 admin 级。

    它们最容易被人降级成 scan —— 理由是"CTF2 的使用者就是能发起扫描的人"。
    而那个理由不成立：
      · 写凭据 = 能替换平台账号（与 `POST /providers` 同级）；
      · 改提交策略 = 能决定机器是否在无人确认时对外提交（改安全边界）。
    这两件事都不是"发起一次扫描"的延伸。
    """
    for item in (
        ("POST", "/api/v1/ctf2/accounts"),
        ("DELETE", "/api/v1/ctf2/accounts/{accountId}"),
        ("PUT", "/api/v1/ctf2/settings"),
    ):
        assert SCOPE_GATED.get(item) == "admin", f"{item} 必须是 admin 级"


# ══════════════════════════════════════════════════════════
# 无凭据 → 401
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(("method", "path"), sorted(set(READ_ONLY) | set(SCOPE_GATED)))
def test_ctf2_endpoints_reject_anonymous(
    client: TestClient, method: str, path: str
) -> None:
    """★ 无凭据 → **401**（不是 403、不是 404）。

    必须是 401 而不是别的：鉴权依赖在端点函数体之前执行，所以
    "参数不存在"也轮不到 404。用 403 的话前端会走"权限不足"的分支，
    而用户其实只是没登录 —— 他会去找管理员要权限，方向完全错。
    """
    response = client.request(method, _fill(path))
    assert response.status_code == 401, (
        f"{method} {path} → {response.status_code}（无凭据必须 401）\n{response.text[:300]}"
    )
    body = response.json()
    assert body["error"]["code"] == "UNAUTHENTICATED"


# ══════════════════════════════════════════════════════════
# 错误 token → 401
# ══════════════════════════════════════════════════════════

BAD_TOKENS: list[tuple[str, str]] = [
    ("空串", "Bearer "),
    ("形状不对", "Bearer not-a-suvex-token"),
    ("前缀对但不存在", "Bearer svx_thisTokenDoesNotExistAtAll123456"),
    ("别的方案", "Basic YWRtaW46YWRtaW4="),
]


@pytest.mark.parametrize(("label", "header"), BAD_TOKENS, ids=[x[0] for x in BAD_TOKENS])
@pytest.mark.parametrize(("method", "path"), sorted(SCOPE_GATED))
def test_bad_tokens_are_rejected(
    ready_client: TestClient, method: str, path: str, label: str, header: str
) -> None:
    """形状不对 / 不存在的令牌 → 401，在**每一个**受限端点上跑一遍。

    不只挑一个代表性的：一旦有人给某个端点换成手写鉴权，就只有那个
    端点会漏 —— 而"401 是依赖注入统一做的"这个前提值得被验证一次。
    """
    ready_client.cookies.clear()
    response = ready_client.request(method, _fill(path), headers={"Authorization": header})
    assert response.status_code == 401, (
        f"{method} {path} 在 {label} 时返回了 {response.status_code}，应为 401"
    )
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


# ══════════════════════════════════════════════════════════
# 只读令牌：403 与 能读
# ══════════════════════════════════════════════════════════


def _read_only_token(ready_client: TestClient) -> dict[str, str]:
    """造一个只有 `read` 的机器令牌。"""
    created = ready_client.post(
        "/api/v1/settings/tokens",
        json={"label": "CTF2 只读脚本", "scopes": ["read"], "expiresAt": None},
    )
    assert created.status_code == 201, created.text
    return {"Authorization": f"Bearer {created.json()['plaintext']}"}


def test_read_only_token_is_denied_on_gated_endpoints(ready_client: TestClient) -> None:
    """★ **已认证但权限不足 = 403**，不是 401。

    这个区分不是审美：401 会让前端的 `apiRequest` 走"跳登录页"那条路，
    而用户其实登录着 —— 于是他被踢出登录页，再登录一次，还是 403。
    403 才是"你需要更高权限"的正确表达。
    """
    headers = _read_only_token(ready_client)
    ready_client.cookies.clear()

    for method, path in sorted(SCOPE_GATED):
        response = ready_client.request(method, _fill(path), headers=headers)
        assert response.status_code == 403, (
            f"{method} {path} 用只读令牌得到了 {response.status_code}，应为 403\n"
            f"{response.text[:200]}"
        )
        error = response.json()["error"]
        assert error["code"] == "INSUFFICIENT_SCOPE"
        # 文案要说清**需要什么权限**，否则用户不知道该申请哪个
        assert error["message"]


def test_read_only_token_can_still_read(ready_client: TestClient) -> None:
    """反面：只读令牌**读得动**。

    少了这一条，上面那条 `assert 403` 可以被"所有请求都 403"满足 ——
    一个把 read 级端点也一起拒掉的实现会全绿。

    ⚠️ 这些端点打在**空库**上：所以它们要么 200（空列表），
    要么 404（假 id 不存在）。401/403 都不许出现。
    """
    headers = _read_only_token(ready_client)
    ready_client.cookies.clear()

    for method, path in sorted(READ_ONLY):
        response = ready_client.request(method, _fill(path), headers=headers)
        assert response.status_code not in (401, 403), (
            f"{method} {path} 用只读令牌被拒了（{response.status_code}），"
            "read 级端点不该要求更高权限"
        )


def test_snapshot_never_leaks_the_token(ready_client: TestClient) -> None:
    """★ 只读的快照里**不能**出现凭据。

    这条测的是"读接口不回填凭据"这条纪律。`KernelAccountOut` 里根本
    没有 token 字段（只有掩码），所以它是结构上成立的 —— 但
    "结构上成立"值得有一条断言盯着：有人为了"方便调试"加一个
    `token` 字段，不会有任何别的东西报错。
    """
    response = ready_client.get("/api/v1/ctf2/snapshot")
    assert response.status_code == 200, response.text
    body = response.json()

    account = body.get("account")
    if account is None:
        # 空库：没有绑定。那么它必须是 **null**，而不是一个带空 token 的对象。
        assert account is None
        return
    assert "token" not in account
    assert set(account) >= {"tokenMask", "hasToken"}
