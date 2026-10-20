"""多用户与两档角色（v0.24）。

## 为什么单独一个文件

权限模型的漏判是**最不该靠人工 review** 的东西 —— 它不会报错、
不会崩、界面上一切正常，只是某个人多做了他不该做的事。

而这套测试要盯的是三件互相独立的事：

1. **`viewer` 真的只能读** —— 不能扫描（那是"让机器对外发包"）、
   不能改设置、不能管用户；
2. **★ 用户管理只能由登录的管理员做** —— 机器令牌一律拒绝。
   这条不是"更严一点"，而是一条明确的安全边界：令牌若能管用户，
   它就能**自我放大**（创建一个新管理员、或改掉签发者的角色），
   而令牌是长期有效的，泄漏一个就等于永久拿到用户管理权；
3. **不会把自己锁在门外** —— 不能删最后一个管理员、不能删自己、
   不能把自己降级。这三个都是"通过界面走进去的死胡同"，
   只能改库才能救回来。

## 与 `test_authorization_matrix.py` 的分工

那个文件是**矩阵式**的（逐端点 × 逐凭据），保证"每个端点都有明确的
鉴权要求、没有漏标的"。这里补的是**角色语义** ——
矩阵只能证明"端点要 read"，证明不了"viewer 做不了这件事"。
"""

from __future__ import annotations

import contextlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import service as auth_service
from tests.conftest import GOOD_PASSWORD, GOOD_USERNAME, VIEWER_USERNAME

API = "/api/v1"


# ══════════════════════════════════════════════════════════
# 一、只读角色真的只能读
# ══════════════════════════════════════════════════════════


def test_viewer_can_read(viewer_client: TestClient) -> None:
    """只读角色能看 —— 这是它的全部用途，先确认它**没被过度限制**。

    ⚠️ 这一条不能省：一个"什么都拒绝"的权限模型也能让所有
    "不能做 X" 的断言通过。所以必须同时证明"能做的确实能做"。
    """
    assert viewer_client.get(f"{API}/scans").status_code == 200
    assert viewer_client.get(f"{API}/findings").status_code == 200
    assert viewer_client.get(f"{API}/overview").status_code == 200
    assert viewer_client.get(f"{API}/reports").status_code == 200
    assert viewer_client.get(f"{API}/auth/me").status_code == 200
    # ★ provider 列表也要能读 —— 顶部状态条要用它显示"当前模型"
    assert viewer_client.get(f"{API}/providers").status_code == 200


def test_viewer_cannot_scan(viewer_client: TestClient) -> None:
    """★ **不能发起扫描** —— 这是权限模型里唯一真正重要的那条线。

    这个平台的危害是"替攻击者发包"，所以"能不能让机器对外动手"
    就是权限的分界线。viewer 越不过它。
    """
    response = viewer_client.post(
        f"{API}/scans",
        json={
            "name": "viewer 不该能建的扫描",
            "target": "example.com",
            "targetKind": "url",
            "providerId": "prov_x",
            "skills": ["web"],
            "authorization": {"confirmed": True, "statement": "我有权对此目标进行安全测试"},
        },
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "INSUFFICIENT_SCOPE"


def test_viewer_cannot_change_settings(viewer_client: TestClient) -> None:
    """不能改设置 —— 包括沙箱、执行三件套、通知、保留规则。"""
    assert viewer_client.put(
        f"{API}/settings/sandbox",
        json={
            "image": "x",
            "cpuLimit": 1,
            "memoryLimitMb": 512,
            "timeoutMinutes": 10,
            "networkIsolated": False,
            "allowVolumeWrite": False,
            "maxConcurrentScans": 1,
            "scanMaxTurns": 100,
            "scanMode": "quick",
            # ★ v0.42：`SandboxIn` 的字段**全部必填**（配置类端点的纪律）。
            # 少发这一项会让下面的断言变成 422 而不是 403 —— 而 422 也是
            # "非管理员"，但**理由完全不同**（字段缺失 vs 权限不足）。
            # 这条用例要验的是后者，所以必须把字段补齐。
            "enableOffensivePrompt": False,
        },
    ).status_code == 403
    assert viewer_client.put(
        f"{API}/settings/notices/rule",
        json={
            "minSeverity": "high",
            "notifyOnScanComplete": True,
            "notifyOnCriticalImmediate": True,
        },
    ).status_code == 403
    assert viewer_client.put(
        f"{API}/settings/scan-retention",
        json={"archiveAfterDays": 1, "trashRetentionDays": 1},
    ).status_code == 403


def test_viewer_cannot_manage_tokens(viewer_client: TestClient) -> None:
    """不能管令牌 —— 否则他能给自己签一个 `admin` scope 的令牌。

    这是"自我提权"最直接的一条路径：viewer 若能创建令牌，
    他就能创建一个带 `scan`（甚至 `admin`）scope 的令牌，
    然后用那个令牌做他被禁止的事。
    """
    assert viewer_client.get(f"{API}/settings/tokens").status_code == 403
    assert viewer_client.post(
        f"{API}/settings/tokens",
        json={"label": "提权试试", "scopes": ["admin"], "expiresAt": None},
    ).status_code == 403


def test_viewer_cannot_read_audit_log(viewer_client: TestClient) -> None:
    """★ 不能看审计日志 —— 这是"内容本身就是秘密"的那一类。

    前面那条原则是"能不能看与能不能改是两件事"，而审计日志是例外：
    它记录的是**谁在什么时候做了什么**，那是权限与行为信息本身。
    """
    assert viewer_client.get(f"{API}/audit").status_code == 403


def test_viewer_cannot_see_user_list(viewer_client: TestClient) -> None:
    """不能看用户列表 —— 对一个会对目标发包的平台，账号列表是爆破的起点。"""
    assert viewer_client.get(f"{API}/auth/users").status_code == 403


# ══════════════════════════════════════════════════════════
# 二、★ 用户管理只能由登录的管理员做
# ══════════════════════════════════════════════════════════


def test_machine_token_cannot_manage_users(
    ready_client: TestClient, db_session: Session
) -> None:
    """★★ **一个带 `admin` scope 的机器令牌也不能管用户。**

    这是本模块最要紧的一条断言，也是"令牌不能自我放大"的落地。

    ## 为什么它比"更严一点"重要得多

    令牌能改设置、能发起扫描，那些都在"这个令牌被授权做的事"范围内。
    但让它能**创建一个新的管理员**、或**改掉自己签发者的角色**，
    意味着令牌的权限可以自我放大 —— 而令牌可以设成永不过期，
    泄漏一个就等于永久拿到用户管理权。

    所以这条线画在"**对权限本身的操作**"上，而不是"危害大小"上。

    ## 怎么造这个令牌

    用管理员的会话创建（`POST /settings/tokens`，scope 含 admin），
    然后用 `Authorization: Bearer` 打用户管理端点 —— 必须是 403。
    """
    created = ready_client.post(
        f"{API}/settings/tokens",
        json={"label": "CI 管理令牌", "scopes": ["read", "scan", "admin"], "expiresAt": None},
    )
    assert created.status_code == 201, created.text
    plaintext = created.json()["plaintext"]

    headers = {"Authorization": f"Bearer {plaintext}"}

    # 它能做别的管理员操作（证明它**确实有** admin scope）——
    # ⚠️ 这一步不能省：少了它，一个"scope 根本没生效"的 bug
    #    也会让下面的 403 通过，而那条测试就变成了空转。
    assert ready_client.get(f"{API}/settings/sandbox").status_code == 200
    assert ready_client.get(
        f"{API}/settings/sandbox", headers=headers
    ).status_code == 200, "这个令牌应当有 admin scope —— 否则下面的 403 说明不了问题"

    # ★ 但用户管理一律拒绝
    assert ready_client.get(f"{API}/auth/users", headers=headers).status_code == 403
    assert ready_client.post(
        f"{API}/auth/users",
        headers=headers,
        json={"username": "by-token", "password": "TokenMadeThis2026", "role": "admin"},
    ).status_code == 403


def test_admin_can_manage_users(ready_client: TestClient) -> None:
    """管理员本人能加人、改角色、删人。"""
    created = ready_client.post(
        f"{API}/auth/users",
        json={"username": "temp-user", "password": "TempUserPass2026", "role": "viewer"},
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]
    assert created.json()["username"] == "temp-user"
    assert created.json()["role"] == "viewer"

    listed = ready_client.get(f"{API}/auth/users").json()["users"]
    assert {u["username"] for u in listed} == {GOOD_USERNAME, "temp-user"}

    promoted = ready_client.patch(f"{API}/auth/users/{user_id}", json={"role": "admin"})
    assert promoted.status_code == 200
    assert promoted.json()["role"] == "admin"

    assert ready_client.delete(f"{API}/auth/users/{user_id}").status_code == 204
    assert len(ready_client.get(f"{API}/auth/users").json()["users"]) == 1


def test_username_is_normalized(ready_client: TestClient) -> None:
    """用户名**大小写不敏感**，且存的是归一化后的值。

    用户不该因为记错大小写而登不进去，而"Alice"与"alice"是两个账号
    只会带来困惑。所以存归一化值 —— 让同一个东西只有一种表示。
    """
    created = ready_client.post(
        f"{API}/auth/users",
        json={"username": "  MiXeD-Case  ", "password": "MixedCasePass2026", "role": "viewer"},
    )

    assert created.status_code == 201, created.text
    assert created.json()["username"] == "mixed-case"

    # 用任意大小写都能登录
    ready_client.post(f"{API}/auth/logout")
    assert ready_client.post(
        f"{API}/auth/login",
        json={"username": "MIXED-CASE", "password": "MixedCasePass2026"},
    ).status_code == 200


# ══════════════════════════════════════════════════════════
# ★ 用户名的字符集（真机实测发现的真问题）
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("username", "label"),
    [
        ("zhang san", "普通空格"),
        ("zhang\tsan", "Tab"),
        ("zhang\u3000san", "全角空格"),
        ("zhang\xa0san", "不换行空格（NBSP）"),
    ],
)
def test_username_with_inner_whitespace_is_rejected(
    ready_client: TestClient, username: str, label: str
) -> None:
    """★★ **用户名里不能有中间空白**（v0.24 修的真问题）。

    ## 这是怎么发现的

    前端子代理在报告里如实写了"用户名的字符集、长度、是否允许空格
    我没验证后端代码"。于是我真机打了一遍，发现这些都**被接受了**：

        'zhang san'      → 存为 'zhang san'
        'zhang\\tsan'     → 存为 'zhang\\tsan'
        'zhang\\u3000san' → 全角空格
        'zhang\\xa0san'   → 不换行空格

    因为第一版只做了 `strip().lower()`，而 **`strip()` 只去首尾**。

    ## 为什么这是真问题（不是洁癖）

    后果是**看起来一样的两个名字进不去**：

      · 用户注册 `zhang san`（打了一个空格），登录时输 `zhangsan` ——
        两个名字在界面上视觉上几乎一样，而系统说"用户名或口令不正确"；
      · 更糟的是 `zhang\\tsan` 与 `zhang san`：**在输入框里看不出区别**，
        而它们是两个不同的账号。

    而用户没有任何办法自查 —— 他能看到的两个名字长得一样。

    ## 判据是白名单

    `^[\\w.-]+$`（Unicode 模式）。白名单而不是黑名单：
    Unicode 里的空白字符有十几种（还有各种不可见字符），
    黑名单永远漏，而白名单漏掉的代价只是"用户被要求换个名字"。
    """
    response = ready_client.post(
        f"{API}/auth/users",
        json={"username": username, "password": "WhitespacePass2026", "role": "viewer"},
    )

    assert response.status_code == 400, f"{label} 被接受了：{response.text}"
    assert "username" in response.json()["error"]["details"]


@pytest.mark.parametrize(
    "username",
    ["zhang", "ZHANG", "张三", "a.b-c_d", "user_01", "李.四"],
)
def test_legal_usernames_are_accepted(ready_client: TestClient, username: str) -> None:
    """★ 合法形式都要能过 —— 这是上一条的对照。

    少了它，"收紧校验"可能被实现成"什么都不让用"，
    而那是一个功能缺失（用户建不了账号）。

    允许的：字母（含中文）、数字、下划线、点、连字符。
    """
    response = ready_client.post(
        f"{API}/auth/users",
        json={"username": username, "password": "LegalNamePass2026", "role": "viewer"},
    )

    assert response.status_code == 201, f"{username!r} 被拒了：{response.text}"
    assert response.json()["username"] == username.strip().lower()


def test_overlong_username_is_rejected(ready_client: TestClient) -> None:
    response = ready_client.post(
        f"{API}/auth/users",
        json={"username": "x" * 65, "password": "TooLongNamePass2026", "role": "viewer"},
    )

    assert response.status_code == 400
    assert "长度" in response.json()["error"]["details"]["username"]


def test_login_with_a_whitespace_username_says_nothing_extra(
    ready_client: TestClient,
) -> None:
    """★ 登录时输一个**格式不合法**的用户名，必须按"用户不存在"处理。

    不能报"用户名格式不对" —— 那会泄露"这个格式不对"这个信息，
    而攻击者能据此判断哪些输入值得试（与"不区分用户不存在与口令错"
    是同一条理由）。

    判据：与"用户名合法但不存在"**返回完全相同的响应**。
    """
    ready_client.post(f"{API}/auth/logout")

    illegal = ready_client.post(
        f"{API}/auth/login",
        json={"username": "zhang san", "password": "WhateverPass2026"},
    )
    legal_missing = ready_client.post(
        f"{API}/auth/login",
        json={"username": "no-such-user", "password": "WhateverPass2026"},
    )

    assert illegal.status_code == 401
    assert illegal.json() == legal_missing.json(), (
        "格式不合法的用户名与不存在的用户名返回了不同响应 —— 那是可枚举的信号"
    )


def test_duplicate_username_is_rejected(ready_client: TestClient) -> None:
    """重名被拒 —— 大小写不同也算重名（因为归一化了）。"""
    first = ready_client.post(
        f"{API}/auth/users",
        json={"username": "dup", "password": "DuplicatePass2026", "role": "viewer"},
    )
    assert first.status_code == 201

    again = ready_client.post(
        f"{API}/auth/users",
        json={"username": "DUP", "password": "AnotherPass2026x", "role": "viewer"},
    )

    assert again.status_code == 409


def test_role_is_required_when_creating_a_user(ready_client: TestClient) -> None:
    """★ `role` **必填** —— 没有默认值。

    给一个 `role="admin"` 的默认值会让"忘了传角色"静默地造出一个管理员，
    而管理员能做的第一件事就是发起扫描（对外发包）。
    参数缺失应该在调用处就报错，而不是在数据库里变成一个提权。
    """
    response = ready_client.post(
        f"{API}/auth/users",
        json={"username": "no-role", "password": "NoRoleGivenPass2026"},
    )

    assert response.status_code == 400
    assert "role" in response.json()["error"]["details"]


def test_unknown_role_is_rejected(ready_client: TestClient) -> None:
    """角色值走白名单。"""
    response = ready_client.post(
        f"{API}/auth/users",
        json={"username": "weird", "password": "WeirdRolePass2026", "role": "superuser"},
    )

    assert response.status_code == 400


def test_unknown_role_in_db_grants_nothing(
    ready_client: TestClient, db_session: Session
) -> None:
    """★★ 库里出现**未知角色**时，那个账号**什么都做不了**（403），而不是降级成 read。

    ## 这条是突变验证补出来的

    我在 `scopes_for_role` 的注释里写了"未知角色返回空集，安全的方向是
    什么都不给"，但**没有任何测试守着它** —— 把返回值改成 `{"read"}`
    时全部测试仍然是绿的。

    而这类场景是真实存在的：手工改库、或者将来加了新角色但忘了
    更新映射表、或者迁移中途。那时的差别是：
      · 返回空集 → 那个人的每个请求都 403，**立刻被发现并排查**；
      · 返回 read → 他能读到全部扫描结果与漏洞细节，**静默地**。
    对一个存着客户内网信息的平台，后者是数据泄漏。

    ## 怎么造这个场景

    直接改库把角色写成一个不存在的值 —— 那是唯一能造出它的方式
    （API 层有白名单拦着，而正因为有它拦着，这条路才更需要测试）。
    """
    created = ready_client.post(
        f"{API}/auth/users",
        json={"username": "ghost-role", "password": "GhostRolePass2026", "role": "viewer"},
    ).json()

    # 直接改库，绕开 API 的白名单
    account = auth_service.get_account_by_id(db_session, created["id"])
    assert account is not None
    account.role = "superuser"  # 一个我们不认识的角色
    db_session.commit()

    from fastapi.testclient import TestClient as _TestClient

    from app.main import app

    with _TestClient(app) as ghost:
        assert ghost.post(
            f"{API}/auth/login",
            json={"username": "ghost-role", "password": "GhostRolePass2026"},
        ).status_code == 200, "登录本身应当成功 —— 角色不影响能不能登录"

        # ★ 但什么都读不到
        assert ghost.get(f"{API}/scans").status_code == 403, (
            "未知角色拿到了 read 权限 —— 应该什么都不给，让它立刻暴露"
        )
        assert ghost.get(f"{API}/findings").status_code == 403
        assert ghost.get(f"{API}/auth/me").status_code == 403


# ══════════════════════════════════════════════════════════
# 三、不会把自己锁在门外
# ══════════════════════════════════════════════════════════


def test_cannot_demote_the_last_admin(ready_client: TestClient, db_session: Session) -> None:
    """★ 不能把**最后一个管理员**降级。

    那会让系统再也没有人能改设置，而恢复只能直接改库 ——
    一个通过界面走进去的死胡同。
    """
    admin = auth_service.get_account_by_username(db_session, GOOD_USERNAME)
    assert admin is not None

    response = ready_client.patch(f"{API}/auth/users/{admin.id}", json={"role": "viewer"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LAST_ADMIN"

    # 而且**真的没改**：降级失败后他的权限应当原样
    assert ready_client.get(f"{API}/settings/sandbox").status_code == 200


def test_can_demote_an_admin_when_another_exists(ready_client: TestClient) -> None:
    """有两个管理员时**可以**降级其中一个 —— 上一条的对照。

    少了这条，"不能降级"可能被实现成"永远不能降级"，
    而那是一个功能缺失（用户没法把权限收回来）。
    """
    second = ready_client.post(
        f"{API}/auth/users",
        json={"username": "second-admin", "password": "SecondAdminPass2026", "role": "admin"},
    ).json()

    response = ready_client.patch(f"{API}/auth/users/{second['id']}", json={"role": "viewer"})

    assert response.status_code == 200
    assert response.json()["role"] == "viewer"


def test_cannot_delete_the_last_admin(ready_client: TestClient, db_session: Session) -> None:
    """不能删最后一个管理员。"""
    admin = auth_service.get_account_by_username(db_session, GOOD_USERNAME)
    assert admin is not None

    response = ready_client.delete(f"{API}/auth/users/{admin.id}")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CANNOT_DELETE_SELF"


def test_cannot_delete_yourself(ready_client: TestClient, db_session: Session) -> None:
    """★ 不能删自己 —— 即使还有别的管理员。

    删完立刻掉线，而如果同时是最后一个管理员就再也进不来了。
    所以"不能删自己"是一条**独立**的规则，不是"最后一个管理员"的特例。
    """
    # 先加一个管理员，这样"不能删自己"就不是因为"最后一个"而触发的
    ready_client.post(
        f"{API}/auth/users",
        json={"username": "another-admin", "password": "AnotherAdminPass26", "role": "admin"},
    )
    me = auth_service.get_account_by_username(db_session, GOOD_USERNAME)
    assert me is not None

    response = ready_client.delete(f"{API}/auth/users/{me.id}")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CANNOT_DELETE_SELF"


# ══════════════════════════════════════════════════════════
# 四、角色变更立刻生效
# ══════════════════════════════════════════════════════════


def test_role_change_takes_effect_immediately(
    ready_client: TestClient, viewer_client: TestClient, db_session: Session
) -> None:
    """★★ **降级立刻生效，不用等会话过期。**

    这不是性能取舍，是安全要求：把某人降成只读之后，他**下一个请求**
    就该被拦住。"我刚把他降成只读，他还在发起扫描"是一个真实的权限漏洞。

    实现方式是 `resolve_identity` 每次请求都读一次账号（主键查询，
    微秒级）。代价极小，换来的是"权限变更立刻生效"。

    ⚠️ 对照：如果角色被缓存在会话行或 Cookie 里，这条测试会失败 ——
    而降级要等到会话过期（默认 12 小时）才生效。
    """
    # 先确认 viewer 现在确实被拦
    assert viewer_client.post(
        f"{API}/settings/tokens",
        json={"label": "x", "scopes": ["read"], "expiresAt": None},
    ).status_code == 403

    # 管理员把他升成 admin
    viewer = auth_service.get_account_by_username(db_session, VIEWER_USERNAME)
    assert viewer is not None
    assert ready_client.patch(
        f"{API}/auth/users/{viewer.id}", json={"role": "admin"}
    ).status_code == 200

    # ★ **同一个会话、下一个请求**就该有 admin 权限 —— 不需要重新登录
    assert viewer_client.get(f"{API}/settings/tokens").status_code == 200, (
        "角色变更没有立刻生效 —— 是不是把角色缓存在会话里了？"
    )


def test_downgrade_blocks_the_next_request(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ 反向：**降级也立刻生效**（这一条比升级更要紧）。

    升级晚一点生效只是"暂时少一点权限"，而降级晚一点生效是
    "本该被拦住的人还在动手" —— 后者是安全问题。

    ⚠️ 要用**同一个会话**验证。重新登录会掩盖缓存问题
    （重新登录本来就会重读角色）。
    """
    from fastapi.testclient import TestClient as _TestClient

    from app.main import app

    ready_client.post(
        f"{API}/auth/users",
        json={"username": "soon-demoted", "password": "SoonDemotedPass2026", "role": "admin"},
    )

    with _TestClient(app) as target:
        assert target.post(
            f"{API}/auth/login",
            json={"username": "soon-demoted", "password": "SoonDemotedPass2026"},
        ).status_code == 200
        # 现在他确实是管理员
        assert target.get(f"{API}/settings/tokens").status_code == 200

        # 降级
        account = auth_service.get_account_by_username(db_session, "soon-demoted")
        assert account is not None
        assert ready_client.patch(
            f"{API}/auth/users/{account.id}", json={"role": "viewer"}
        ).status_code == 200

        # ★ 同一个会话的下一个请求就该被拦
        assert target.get(f"{API}/settings/tokens").status_code == 403, (
            "降级没有立刻生效 —— 这正是「我刚把他降成只读，他还在动手」"
        )


# ══════════════════════════════════════════════════════════
# 五、初始化
# ══════════════════════════════════════════════════════════


def test_setup_creates_an_admin_with_the_given_username(client: TestClient) -> None:
    """初始化时指定的用户名与角色。

    第一个账号**必须是管理员** —— 否则刚装好的系统没人能改设置。
    """
    response = client.post(
        f"{API}/auth/setup",
        json={"username": "first-admin", "password": GOOD_PASSWORD},
    )

    assert response.status_code == 201, response.text
    body = response.json()["user"]
    assert body["username"] == "first-admin"
    assert body["role"] == "admin"


def test_setup_rejects_an_empty_username(client: TestClient) -> None:
    response = client.post(f"{API}/auth/setup", json={"username": "   ", "password": GOOD_PASSWORD})

    assert response.status_code == 400


def test_viewer_password_still_needs_strength(ready_client: TestClient) -> None:
    """给 viewer 建号时**口令强度一样要查**。

    容易漏的一处：只读账号的口令听起来"不那么要紧"，但它一样能
    读到全部扫描结果、漏洞细节与报告 —— 那些是**客户的内网信息**。
    """
    response = ready_client.post(
        f"{API}/auth/users",
        json={"username": "weak-viewer", "password": "123", "role": "viewer"},
    )

    assert response.status_code == 400
    assert "password" in response.json()["error"]["details"]


# ══════════════════════════════════════════════════════════
# 六、★ 时序侧信道（突变验证补出来的盲区）
# ══════════════════════════════════════════════════════════


def test_login_timing_does_not_reveal_whether_a_username_exists(
    ready_client: TestClient, db_session: Session
) -> None:
    """★★ **用户不存在时也要跑一次完整的 argon2**，否则耗时能枚举账号。

    ## 这条是突变验证补出来的

    `service.authenticate` 里有一段写得很郑重的代码：

        if account is None:
            # 用固定假哈希跑一次 verify，让耗时与"账号存在但口令错"一致
            verify_password(_DUMMY_HASH, password)
            raise ApiError.unauthenticated(...)

    而**没有任何测试守着它** —— 把那行删掉，`test_auth_flow` 里那条
    "不区分失败原因"仍然是绿的，因为它只比对**文案**。

    ## 为什么文案相同还不够

    文案是给"看响应的人"的防线，耗时是给"量时间的人"的防线。
    攻击者不需要读响应体，他只需要量两次请求各花多久：

      · 用户名不存在 → 跳过 argon2 → **几毫秒**
      · 用户名存在、口令错 → 跑完整 argon2 → **约 44 毫秒**

    实测（本机）：argon2 单次校验中位数 **44.1 毫秒**，抖动极小
    （42.9–44.4ms）；而纯字符串比较是 **0.2 微秒** —— 差 22 万倍。
    所以跳过 argon2 是一个**一眼可辨**的时序差异，量几次就能
    确定哪些用户名是真的。而对一个**会对目标发包**的平台，
    拿到一个有效用户名就是爆破的一半。

    ## 判据：直接量 service 层，不走 HTTP

    ⚠️ 我第一版用 `TestClient` 打 HTTP 再比耗时，**那条测试是 flaky 的**：
    HTTP 往返本身有几十毫秒的抖动，把 44ms 的信号淹掉了
    （实测两组比值偶尔超过 3 倍容差，而那不代表防线失效）。

    改成直接调 `service.authenticate` —— 被测的东西是**同一条代码路径**
    （HTTP 层只是转发），但排除了网络与序列化噪声。
    这是"测行为而不是测传输"的一个具体例子。

    ## 容差为什么给 3 倍

    防线生效时两组的真实差别只有几毫秒（都是 44ms 左右），
    所以正常情况比值接近 1.0。而防线失效时是 44ms vs 0.2µs ——
    比值是**几个数量级**。3 倍这个阈值落在两者之间很远的地方，
    既不会被负载波动误伤，也能抓住任何"少跑了一次 argon2"的实现。
    """
    import statistics
    import time

    from app.auth import service as auth_service

    def measure(username: str, rounds: int = 5) -> float:
        samples = []
        for _ in range(rounds):
            started = time.perf_counter()
            with contextlib.suppress(Exception):
                auth_service.authenticate(db_session, username, "DefinitelyWrongPass1")
            # 预期就是认证失败（这里只关心耗时），所以吞掉异常。
            samples.append(time.perf_counter() - started)
        return statistics.median(samples)

    existing = measure(GOOD_USERNAME)
    missing = measure("no-such-user-at-all")

    # 存在用户这条**必须**跑了 argon2（本机约 44ms）。
    # 阈值给 5ms：远低于真实的 44ms，又高于"根本没跑"的量级。
    assert existing > 0.005, (
        f"存在用户的校验只花了 {existing * 1000:.2f}ms —— "
        "argon2 似乎没跑（环境异常，不是本测试要验的东西）"
    )

    ratio = max(existing, missing) / max(min(existing, missing), 1e-9)
    assert ratio < 3.0, (
        f"两组耗时差了 {ratio:.1f} 倍"
        f"（存在 {existing * 1000:.1f}ms / 不存在 {missing * 1000:.1f}ms）"
        " —— 用户不存在时没有跑 argon2，可以用耗时枚举账号"
    )
