"""CSRF 防护（契约 §1.2 的必配项）。

契约 §0.3 论证过为什么必须加鉴权，而**决定性的是 CSRF**：
一个无鉴权的 JSON API 如果监听在 localhost/内网，用户浏览器访问的
**任意网页**都能向它发请求 —— 用户打开那个页面，他的机器就替攻击者
发起了一次针对任意目标的攻击扫描。受害者（目标方）看到的来源是**用户的 IP**。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

ALLOWED_ORIGIN = "http://localhost:5173"
FOREIGN_ORIGIN = "https://evil.example"

PASSWORD_BODY = {"current": "SuvexDevPass2026", "next": "Whatever2026Pass"}


def test_cookie_write_from_foreign_origin_is_blocked(ready_client: TestClient) -> None:
    """带 Cookie 的写请求来自未授权源 → 403 `CROSS_ORIGIN_BLOCKED`。

    这是 SameSite=Strict 之外的第二道。为什么还要第二道：
    `SameSite` 是浏览器行为，而浏览器行为有历史包袱 ——
    新注册域名有默认值差异、`localhost` 有特殊放宽。
    服务端自己判一次 `Origin` 的成本是几微秒，
    而这把安全边界从「依赖浏览器实现正确」变成了「依赖我们自己的一行判断」。
    """
    response = ready_client.post(
        "/api/v1/settings/password",
        json=PASSWORD_BODY,
        headers={"Origin": FOREIGN_ORIGIN},
    )

    assert response.status_code == 403, response.text
    error = response.json()["error"]
    assert error["code"] == "CROSS_ORIGIN_BLOCKED"
    assert "来源" in error["message"]


def test_cookie_write_from_allowed_origin_passes(ready_client: TestClient) -> None:
    """来自配置里允许的源 → 放行（这里最终 200，说明 CSRF 那道过了）。"""
    response = ready_client.post(
        "/api/v1/settings/password",
        json=PASSWORD_BODY,
        headers={"Origin": ALLOWED_ORIGIN},
    )

    assert response.status_code == 200, response.text


def test_cookie_write_from_same_origin_passes(ready_client: TestClient) -> None:
    """同源请求必须是放行的。

    生产环境常把前端与 API 放在同一个域名下，此时 `Origin` 等于 API 自己的
    地址，而它不会出现在 `SUPEX_CORS_ORIGINS`（那是给开发态跨源用的）。
    同源请求按定义不是 CSRF —— 不放行的话生产环境会全站写操作 403。
    """
    response = ready_client.post(
        "/api/v1/settings/password",
        json=PASSWORD_BODY,
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 200, response.text


def test_form_encoded_write_is_rejected(ready_client: TestClient) -> None:
    """跨站 HTML 表单**只能**发出三种 Content-Type，本 API 一个都不收。

    契约 §1.2 的「所有写操作要求 Content-Type: application/json」——
    这条就是它的可执行版本。少了它，一个 `<form method=post>` 就能
    在没有 CORS 预检的情况下打到写端点（表单提交是"简单请求"，
    不触发预检，所以 Origin 检查是唯一能拦它的依据，而有些老浏览器的
    表单提交不带 Origin）。
    """
    response = ready_client.post(
        "/api/v1/settings/password",
        content="current=x&next=y",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_bearer_token_is_not_subject_to_csrf_checks(ready_client: TestClient) -> None:
    """机器令牌不受 CSRF 约束 —— 它本来就不该受。

    CSRF 的定义是"借用户的浏览器"：浏览器会自动带上 Cookie，
    但**不会**自动带上 `Authorization` 头。所以拿 Bearer 的请求
    无论来自哪个源都不构成 CSRF，而给它加 Origin 检查只会让
    "在别的域名下跑 CI"这种正常用法莫名失败。
    """
    created = ready_client.post(
        "/api/v1/settings/tokens",
        json={"label": "CI", "scopes": ["admin"], "expiresAt": None},
    ).json()
    ready_client.cookies.clear()

    response = ready_client.post(
        "/api/v1/settings/password",
        json={"current": "SuvexDevPass2026", "next": "Whatever2026Pass"},
        headers={
            "Authorization": f"Bearer {created['plaintext']}",
            "Origin": FOREIGN_ORIGIN,
        },
    )

    assert response.status_code == 200, response.text


def test_read_requests_are_not_csrf_checked(ready_client: TestClient) -> None:
    """GET 不做 Origin 检查。

    读操作不改变状态，而跨源读取本来就被 CORS 挡住（浏览器不让 JS
    读到响应）。给 GET 加 Origin 检查只会挡住「从别的页面链过来看文档」
    这种无害场景。
    """
    response = ready_client.get("/api/v1/auth/me", headers={"Origin": FOREIGN_ORIGIN})

    assert response.status_code == 200


# ══════════════════════════════════════════════════════════
# 伪造 X-Forwarded-Host 不能把自己变成"同源"（v0.44，发现 4）
# ══════════════════════════════════════════════════════════
#
# 原实现把 `X-Forwarded-Host` 的值**无条件**加进允许集合，于是任何客户端
# 都能用它把自己的源变成"自己的源"—— 而 CSRF 这道防线的全部价值就是
# 「攻击者的源**不在**集合里」。实战复现（2026-10-16，真实 `_allowed_origins`，
# 不打桩）：开 `--proxy-headers --forwarded-allow-ips=127.0.0.1` 时，
# 带 `Host: suvex.asia` + `X-Forwarded-Proto: https` + `X-Forwarded-Host:
# evil.example`，`allowed` 集合里真的出现了 `https://evil.example`。
#
# 修法是**直接删掉那一段**（论证见 `_allowed_origins` 的 docstring）：
# 两个部署里的 nginx 都刻意不发这个头，所以正常流量下它恒空、
# **只有攻击者能让它有值**。


def test_spoofed_forwarded_host_cannot_whitelist_the_attacker(ready_client: TestClient) -> None:
    """★★ 伪造 `X-Forwarded-Host` 不再能把攻击者的源加进允许集合。

    ## 这条是发现 4 的直接回归

    攻击者要的三个头如下 —— 它们**全都是请求头**，也就是说
    任何能发请求的东西都能带（不需要控制任何反代）：

        Host: suvex.asia
        X-Forwarded-Proto: https
        X-Forwarded-Host: evil.example

    修复前 `_allowed_origins` 会算出 `https://evil.example` 并放行后面
    那个 `Origin: https://evil.example` 的写请求。修复后这必须仍然是 403。

    ⚠️ 断言的是**403 本身**而不是"集合里没有那个值"：这条测试走的是
    真实 HTTP 栈（`ready_client`），验的是"攻击最终不成立"，
    而不是某个内部函数返回了什么。
    """
    response = ready_client.post(
        "/api/v1/settings/password",
        json=PASSWORD_BODY,
        headers={
            "Origin": FOREIGN_ORIGIN,
            "X-Forwarded-Host": "evil.example",
            "X-Forwarded-Proto": "https",
        },
    )

    assert response.status_code == 403, (
        "伪造 `X-Forwarded-Host` 把自己变成'同源'了 —— `_allowed_origins` "
        "又去读那个可伪造的头了。任何客户端都能带这个头，"
        "而 CSRF 的价值就是'攻击者的源不在集合里'。\n"
        f"响应：{response.text}"
    )
    assert response.json()["error"]["code"] == "CROSS_ORIGIN_BLOCKED"


def test_spoofed_forwarded_host_cannot_forge_the_scheme_either(
    ready_client: TestClient,
) -> None:
    """★★ 伪造的那个头**碰巧等于真实 `Host`** 时，也不许改变 scheme 判定。

    ## 为什么单独钉这一条

    它是"读这个头"与"不读这个头"**唯一会分出胜负**的场景，
    而那条分界线正是修复的关键：

    本轮请求真实的 own origin 是 `http://testserver`（测试用的 http）。
    下面这个 `Origin: https://testserver` 与它**不同源**（scheme 不同），
    所以必须 403。而原实现会把 `X-Forwarded-Host: testserver` 采信、
    按 `X-Forwarded-Proto: https` 拼出 `https://testserver`
    **加进允许集合**，于是这个请求被放行。

    也就是说：只要这个头参与判据，攻击者就能用它**自己决定
    允许集合里出现什么**（换成 `evil.example` 就是第一条测试那个场景，
    换成与 `Host` 相同的值就是这一条）。两种用法都要堵。

    ⚠️ 这条同时防住一种"打补丁式"的回归：把无条件读改成
    "读，但只在与 `Host` 一致时采用"。那样看着安全，实则这第二种用法
    仍然成立 —— 而它有实际影响（scheme 被攻击者决定）。
    """
    response = ready_client.post(
        "/api/v1/settings/password",
        json=PASSWORD_BODY,
        headers={
            "Origin": "https://testserver",
            "X-Forwarded-Host": "testserver",
            "X-Forwarded-Proto": "https",
        },
    )

    assert response.status_code == 403, (
        "伪造 `X-Forwarded-Host` 把 own origin 的 scheme 改成了 https —— "
        "说明这个头仍然参与判据。它是请求头，任何客户端都能带，"
        "所以它参与判据就等于攻击者能自己决定允许集合的内容。\n"
        f"响应：{response.text}"
    )
    assert response.json()["error"]["code"] == "CROSS_ORIGIN_BLOCKED"


def test_forwarded_proto_alone_cannot_widen_the_allowed_set(ready_client: TestClient) -> None:
    """只带 `X-Forwarded-Proto`（不带 `X-Forwarded-Host`）同样无效。

    ## 如实说明这条测试的能力边界

    修复前的判据是"`X-Forwarded-Host` **有值才**采用 `X-Forwarded-Proto`"
    （原代码里 proto 的读取在 `if forwarded:` 里面），所以**单独**一个
    proto 在旧实现下也是无效的 —— 也就是说这条测试**不是**修复的
    反向验证（实测：把旧实现加回去，它仍然绿）。

    那它钉什么？钉**修完之后也不能有第二个头参与进来**。理由是修复后
    最容易被想到的"改进"就是接一个 `X-Forwarded-Proto` 进来"支持 https 反代"
    （比如用它决定 own origin 的 scheme）—— 而那是同一个错误：
    它同样是请求头、同样可伪造。真实的 https 判定由 ASGI 层做
    （`--proxy-headers` 按对端是否可信改写 scheme），业务代码不碰。

    ⚠️ `Forwarded`（RFC 7239 那个标准头）也一起带上：它是"看起来更正规"
    的同一个头，若将来有人换成解析它，这条会同时变红。
    """
    response = ready_client.post(
        "/api/v1/settings/password",
        json=PASSWORD_BODY,
        headers={
            "Origin": FOREIGN_ORIGIN,
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Port": "443",
            "Forwarded": "host=evil.example;proto=https",
        },
    )

    assert response.status_code == 403, (
        "`X-Forwarded-Proto` / `X-Forwarded-Port` / `Forwarded` 里的某一个"
        "把攻击者的源放行了 —— 这些**全都是请求头**，全部可伪造。"
    )


def test_configured_origin_still_passes_after_the_removal(ready_client: TestClient) -> None:
    """★ 反向验证：删掉那段之后，**正当**放行路径必须还在。

    只断言"攻击被挡住"是不够的 —— 一个恒返回 403 的实现也能过那几条。
    所以这里同时确认两条正当路径仍然通：

      · 配置里登记的源（`SUPEX_CORS_ORIGINS`，开发态跨源）；
      · 本请求自己的源（生产环境前后端同域时的 `Origin`）。

    （同源那条另有 `test_cookie_write_from_same_origin_passes` 单独守着。）
    """
    configured = ready_client.post(
        "/api/v1/settings/password",
        json=PASSWORD_BODY,
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert configured.status_code == 200, (
        "配置里登记的源被挡住了 —— 删 `X-Forwarded-Host` 那段时误伤了正当路径"
    )
