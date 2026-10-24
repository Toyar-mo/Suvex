"""通知与设置快照（v0.22）—— 本模块最后六个 501 的验收。

## 为什么单独一个文件

这六个端点是本项目**最后一批未实现**的接口，长期返回 501。
它们此前不实现是有理由的（`GET /settings` 缺三块却回 200 会让调用方
以为拿全了），所以"改成实现了"必须能被验证 —— 而不是只看代码里
`pending()` 没了。

## 这套测试盯的四件事

1. **真的不再是 501** —— 逐条打，断言 200/201/204；
2. **规则与渠道是两个维度** —— 渠道里没有阈值、规则里没有地址；
3. **`verified` 是事实不是声明** —— 客户端传不进去；
4. **改地址会把验证打回 `False`** —— 这是最容易漏的一处，
   漏了会让用户以为新地址也验证过了。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.settings import dispatch

API = "/api/v1"


# ══════════════════════════════════════════════════════════
# 出站打桩（v0.89）
# ══════════════════════════════════════════════════════════
#
# `/test` 从 v0.89 起会**真的发**（见 `settings/router.py` 里那段说明），
# 所以这个文件里凡是走到 `/test` 的用例都会做出站请求。
#
# ⚠️ 必须打桩，两个理由：
#   1. 测试不该依赖网络，更不该真的去敲 `hooks.example.com`；
#   2. `tests/conftest.py` 的出站拦截（`guarded_getaddrinfo`）会把这次
#      请求变成 `EgressBlocked` 抛进被测代码里 —— 那看起来像产品缺陷，
#      实际是测试环境在拦外网。
#
# 打的是 `dispatch.httpx.Client`（模块级名字），所以不需要起 mock server。


class _FakeRecorder:
    """记录每次 `post` 的 (url, body)，并可按需返回状态码 / 抛异常。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.status_code: int = 200
        self.text: str = ""
        self.raise_on_post: BaseException | None = None


@pytest.fixture
def outbound(monkeypatch: pytest.MonkeyPatch) -> _FakeRecorder:
    """把 dispatch 的 HTTP 客户端换成记录器，默认回 200。"""
    recorder = _FakeRecorder()

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, url: str, json: Any = None) -> httpx.Response:
            if recorder.raise_on_post is not None:
                raise recorder.raise_on_post
            recorder.calls.append((url, json))
            return httpx.Response(
                recorder.status_code,
                text=recorder.text,
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(dispatch.httpx, "Client", _Client)
    # 假 client 没有真实连接池，`ensure_guarded` 的结构检查会失败 ——
    # 这里置成空操作。`BlockedAddressError` **不打桩**：SSRF 那条行为的
    # 验证在 `tests/test_notice_dispatch.py` 里，那里看的就是它。
    monkeypatch.setattr(dispatch.netguard, "ensure_guarded", lambda _client: None)
    return recorder


def _create(client: TestClient, **overrides) -> dict:
    payload = {
        "kind": "webhook",
        "label": "安全组 Webhook",
        "target": "https://hooks.example.com/suvex",
        "enabled": True,
    }
    payload.update(overrides)
    response = client.post(f"{API}/settings/notices/channels", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# ══════════════════════════════════════════════════════════
# 一、六个端点都不再是 501
# ══════════════════════════════════════════════════════════


def test_settings_snapshot_is_no_longer_501(ready_client: TestClient) -> None:
    """★ `GET /settings` 返回**完整四块**，而不是 501 或半成品。

    断言四块都在：accounts / sandbox / notices / tokens。
    只断言 200 是不够的 —— 一个 `{}` 也是 200，
    而那正是这个端点当初拒绝实现的原因。
    """
    response = ready_client.get(f"{API}/settings")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"accounts", "sandbox", "notices", "tokens"}, f"快照的块不对：{sorted(body)}"
    # notices 里必须**同时**有规则与渠道 —— 少一块就是半成品
    assert set(body["notices"]) == {"rule", "channels"}


def test_snapshot_has_no_local_state_fields(ready_client: TestClient) -> None:
    """`status` / `error` **不能**出现在响应里。

    它们是前端 `SettingsSnapshot` 上的**本地加载状态**，描述的是
    "这次请求进行得怎么样"，不是服务端数据。服务端下发它们等于
    替前端声明它自己的状态机。
    """
    body = ready_client.get(f"{API}/settings").json()

    assert "status" not in body
    assert "error" not in body


def test_notice_rule_is_no_longer_501(ready_client: TestClient) -> None:
    response = ready_client.get(f"{API}/settings/notices/rule")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["minSeverity"] == "high"  # 默认值
    assert body["notifyOnScanComplete"] is True
    assert body["notifyOnCriticalImmediate"] is True


def test_channels_list_is_no_longer_501(ready_client: TestClient) -> None:
    """空表也是 200（`[]`），不是 501 —— "还没配渠道"是正常状态。"""
    response = ready_client.get(f"{API}/settings/notices/channels")

    assert response.status_code == 200, response.text
    assert response.json() == []


def test_channel_crud_is_no_longer_501(ready_client: TestClient) -> None:
    """增 / 改 / 删三个端点连起来走一遍。"""
    created = _create(ready_client)
    assert created["id"].startswith("ntc")

    patched = ready_client.patch(f"{API}/settings/notices/channels/{created['id']}", json={"label": "改个名"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["label"] == "改个名"

    deleted = ready_client.delete(f"{API}/settings/notices/channels/{created['id']}")
    assert deleted.status_code == 204, deleted.text
    assert ready_client.get(f"{API}/settings/notices/channels").json() == []


def test_channel_test_endpoint_is_no_longer_501(ready_client: TestClient, outbound: _FakeRecorder) -> None:
    """★ v0.89 起这里**真的发**，成功就会亮绿勾。

    v0.22 那一版断言的是"不能报成功，因为真实发送还没接"。现在接上了，
    所以断言反过来了 —— 但**前提是这次发送真的发生**：`outbound.calls`
    必须有一条，否则"verified=True"就只是一个没被验证过的声明。
    """
    channel = _create(ready_client)

    response = ready_client.post(f"{API}/settings/notices/channels/{channel['id']}/test")

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(outbound.calls) == 1, "没有发出请求 —— 那 verified=True 就是假的"
    assert outbound.calls[0][0] == "https://hooks.example.com/suvex"
    assert body["verified"] is True
    assert not body["verifyError"]


def test_channel_test_endpoint_reports_failure_not_success(
    ready_client: TestClient, outbound: _FakeRecorder
) -> None:
    """对端挂了必须报**失败**并给原因 —— 这是绿色勾的本分。"""
    channel = _create(ready_client)
    outbound.status_code = 500
    outbound.text = "boom"

    body = ready_client.post(f"{API}/settings/notices/channels/{channel['id']}/test").json()

    assert body["verified"] is False
    assert body["verifyError"], "失败却没给原因 —— 用户无法据此修复"
    assert "500" in body["verifyError"]


# ══════════════════════════════════════════════════════════
# 二、规则与渠道是两个维度
# ══════════════════════════════════════════════════════════


def test_rule_and_channel_are_separate_dimensions(ready_client: TestClient) -> None:
    """★ 渠道里没有阈值、规则里没有地址。

    这条防的是"把两个维度合成一个对象"——那会让
    「邮件收全部、Slack 只收严重」这种真实需求**无处表达**。
    """
    channel = _create(ready_client)

    # 渠道：有地址，没有阈值
    assert "target" in channel
    assert "minSeverity" not in channel

    # 规则：有阈值，没有地址
    rule = ready_client.get(f"{API}/settings/notices/rule").json()
    assert "minSeverity" in rule
    assert "target" not in rule


def test_rule_can_be_updated(ready_client: TestClient) -> None:
    response = ready_client.put(
        f"{API}/settings/notices/rule",
        json={
            "minSeverity": "critical",
            "notifyOnScanComplete": False,
            "notifyOnCriticalImmediate": True,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["minSeverity"] == "critical"
    assert body["notifyOnScanComplete"] is False

    # 再读一次确认真的落库了（而不是只在响应里改了）
    assert ready_client.get(f"{API}/settings/notices/rule").json()["minSeverity"] == "critical"


def test_rule_rejects_an_unknown_severity(ready_client: TestClient) -> None:
    """非法严重度走 `400 VALIDATION_FAILED`，且**给中文原因**。

    DB 上还有 CHECK 兜底，但那条会抛出 Postgres 的约束违反错误 ——
    用户看不懂。所以路由层先校验。
    """
    response = ready_client.put(
        f"{API}/settings/notices/rule",
        json={
            "minSeverity": "catastrophic",
            "notifyOnScanComplete": True,
            "notifyOnCriticalImmediate": True,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


# ══════════════════════════════════════════════════════════
# 三、verified 是事实，不是声明
# ══════════════════════════════════════════════════════════


def test_client_cannot_claim_verified(ready_client: TestClient) -> None:
    """★ 客户端**根本传不进** `verified` —— schema 里没有这个字段。

    "已验证"是服务端真的发过一次测试通知的**事实**，不是用户的声明。
    让请求体能带它，界面上那个绿勾就变得毫无意义 ——
    而用户会以为通知链路是通的。

    ⚠️ 我第一版断言的是"传了会被**忽略**"（201 + verified=False），
    而实际行为更严格：`StrictModel` 直接 **400 拒绝**未知字段。

    这个行为**更好**，所以改测试而不是改代码 —— 理由：
    "静默忽略"会让调用方以为自己设置成功了（而它没有），
    而 400 当场告诉他这个字段不存在。后者是诚实的失败。
    """
    response = ready_client.post(
        f"{API}/settings/notices/channels",
        json={
            "kind": "webhook",
            "label": "安全组 Webhook",
            "target": "https://hooks.example.com/suvex",
            "enabled": True,
            "verified": True,
            "verifyError": "假装通过",
        },
    )

    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    # 错误里要点名是哪个字段不认识 —— 否则调用方不知道该删什么
    assert "verified" in error["details"]


def test_changing_target_resets_verified(
    ready_client: TestClient, db_session: Session, outbound: _FakeRecorder
) -> None:
    """★★ 改地址把 `verified` 打回 `False`。

    这是本模块最容易漏的一处：`verified` 的含义是"**这个地址**
    发过一次测试并成功了"。地址变了，那个事实不再成立。

    漏掉的症状是"配好了、显示已验证、但真出事时一条通知都没发出去"——
    而那时已经太晚了。

    ⚠️ v0.89 起 `/test` 会真的发，所以要打桩（`outbound`）。这里**不用**
    它的返回值，只是避免这次调用去敲真实主机 —— 本用例要验的是
    PATCH 那一步的重置，不是发送。
    """
    channel = _create(ready_client)
    # 先把它标成已验证（直接改库，模拟"之前验证通过过"）
    from app.settings import service as settings_service

    settings_service.mark_channel_verified(db_session, channel["id"], ok=True)

    # 改地址
    patched = ready_client.patch(
        f"{API}/settings/notices/channels/{channel['id']}",
        json={"target": "https://hooks.example.com/another"},
    )

    assert patched.status_code == 200, patched.text
    assert patched.json()["verified"] is False, "改了地址却还显示已验证"


def test_changing_label_keeps_verified(ready_client: TestClient, db_session: Session) -> None:
    """改名字**不**影响验证状态 —— 地址没变，事实依然成立。

    这一条是上一条的对照：如果实现写成"任何 PATCH 都重置 verified"，
    用户改个错别字就要重新验证一次，那是另一种坏。
    """
    channel = _create(ready_client)
    from app.settings import service as settings_service

    settings_service.mark_channel_verified(db_session, channel["id"], ok=True)

    patched = ready_client.patch(f"{API}/settings/notices/channels/{channel['id']}", json={"label": "换个名"})

    assert patched.json()["verified"] is True


# ══════════════════════════════════════════════════════════
# 四、校验与边界
# ══════════════════════════════════════════════════════════


def test_unknown_channel_kind_is_rejected(ready_client: TestClient) -> None:
    response = ready_client.post(
        f"{API}/settings/notices/channels",
        json={"kind": "carrier-pigeon", "label": "信鸽", "target": "x"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_test_endpoint_reports_a_bad_webhook_shape(ready_client: TestClient, outbound: _FakeRecorder) -> None:
    """地址形状不对时，原因要说清是**形状**问题。

    这与"发送失败"是两种不同的原因，混成一句话会让用户改了半天地址
    却发现不是那个问题。

    ★ 而且形状不对时**不该出站**：形状错的地址常常仍指向一台真实外部
    主机，发出去既浪费一次请求，又可能把那台主机卷进我们的错误排查里。
    """
    channel = _create(ready_client, target="hooks.example.com/no-scheme")

    body = ready_client.post(f"{API}/settings/notices/channels/{channel['id']}/test").json()

    assert body["verified"] is False
    assert "http://" in body["verifyError"]
    assert outbound.calls == [], "形状不对却仍然发了请求"


def test_disabled_channel_is_still_listed(ready_client: TestClient) -> None:
    """停用的渠道**仍然列出** —— 停用是状态，不是删除。

    隐藏它会让用户以为配置丢了，然后重新配一个。
    （与 `GET /settings/tokens` 的"已吊销不列出"不同：那里的令牌
    已彻底失效，留着只会造成"它还能用"的误解。）
    """
    channel = _create(ready_client, enabled=False)

    listed = ready_client.get(f"{API}/settings/notices/channels").json()

    assert [c["id"] for c in listed] == [channel["id"]]
    assert listed[0]["enabled"] is False


def test_deleting_an_unknown_channel_is_404(ready_client: TestClient) -> None:
    """路径里的 id 不存在 = 资源不存在 = 404（不是 400）。"""
    response = ready_client.delete(f"{API}/settings/notices/channels/ntc_nope")

    assert response.status_code == 404
