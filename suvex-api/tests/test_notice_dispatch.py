"""通知投递（v0.89）—— 把「什么情况下发」与「怎么发」真的接上。

## 这个文件盯的是什么

v0.22 起 `NotifyChannel` / `NoticeRule` 只有 CRUD，`POST .../test` 会诚实
地回"真实发送尚未接入"。本轮把 webhook 与 slack 两条链路接上，所以这里
要证明的不是"代码里有 dispatch 这个函数"，而是**下面这几条行为**：

1. 规则说了算：关掉开关不发、够不上阈值不发、失败一定要发；
2. 真发：`httpx.Client.post` 确实被调用了一次，且载荷是按渠道类型造的；
3. 失败有原因：`ChannelResult.reason` 非空，并写回 `verify_error`；
4. 一条渠道挂了不影响其它渠道；
5. 停用的渠道不参与发送（但仍在台账里）；
6. SSRF 拦截是**独立的一条原因**，不混进"对端挂了"。

## 为什么全部打桩 `httpx.Client`

这些用例不该出网。`dispatch` 里 `httpx.Client` 是模块级名字，所以
`monkeypatch.setattr(dispatch.httpx, "Client", FakeClient)` 就能整条替换 ——
不需要起 mock server，也不需要真的连一台机器。

⚠️ 但**不能**连 `netguard` 一起打桩：SSRF 那条用例正是要验证
`netguard.BlockedAddressError` 被单独识别，打掉它等于把被测行为删了。
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import netguard
from app.settings import dispatch
from app.settings.models import NoticeRule

API = "/api/v1"


# ══════════════════════════════════════════════════════════
# 打桩：一个记录调用的假 Client
# ══════════════════════════════════════════════════════════


class _Recorder:
    """记录每一次 `post` 的 (url, json)，并可按需抛异常 / 返回状态码。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.raise_on_post: BaseException | None = None
        self.status_code = 200
        self.text = ""

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = self

        class _Client:
            def __init__(self, **kwargs: Any) -> None:
                # 记下构造参数：`trust_env=False` 与 guard transport 是安全约束，
                # 值得单独断言（见 test_client_is_built_without_env_proxy）。
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
        # `ensure_guarded` 检查的是真实 httpx client 的连接池结构，
        # 假 Client 没有 `_transport` / `_mounts`，直接置成空操作。
        # ⚠️ 只打桩 `ensure_guarded`，**不**打桩 `BlockedAddressError`。
        monkeypatch.setattr(dispatch.netguard, "ensure_guarded", lambda _client: None)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    rec.install(monkeypatch)
    return rec


def _payload(**overrides: Any) -> dispatch.NoticePayload:
    base = {
        "scan_id": "scan_1",
        "target": "https://example.com",
        "status": "completed",
        "summary": "扫描完成，用时 12 秒",
        "severity": "high",
        "duration_sec": 12,
        "critical_count": 0,
        "high_count": 2,
    }
    base.update(overrides)
    return dispatch.NoticePayload(**base)


def _rule(db_session: Any, **overrides: Any) -> NoticeRule:
    """把单例规则改成想要的样子。"""
    rule = db_session.get(NoticeRule, "singleton")
    if rule is None:
        rule = NoticeRule(id="singleton")
        db_session.add(rule)
    rule.min_severity = overrides.get("min_severity", "high")
    rule.notify_on_scan_complete = overrides.get("notify_on_scan_complete", True)
    rule.notify_on_critical_immediate = overrides.get("notify_on_critical_immediate", True)
    db_session.commit()
    db_session.refresh(rule)
    return rule


def _channel(db_session: Any, **overrides: Any) -> Any:
    from app.settings.models import NotifyChannel

    channel = NotifyChannel(
        id=overrides.get("id", "ch_1"),
        kind=overrides.get("kind", "webhook"),
        label=overrides.get("label", "安全组 Webhook"),
        target=overrides.get("target", "https://hooks.example.com/suvex"),
        enabled=overrides.get("enabled", True),
        verified=False,
    )
    db_session.add(channel)
    db_session.commit()
    db_session.refresh(channel)
    return channel


# ══════════════════════════════════════════════════════════
# 一、规则筛选
# ══════════════════════════════════════════════════════════


def test_scan_complete_switch_off_skips_notification(db_session: Any) -> None:
    """关掉「扫描完成后通知」就不发。"""
    rule = _rule(db_session, notify_on_scan_complete=False)
    ok, reason = dispatch.should_notify(rule, _payload())

    assert ok is False
    assert "扫描完成后通知" in reason


def test_severity_below_threshold_skips(db_session: Any) -> None:
    """本次最高 medium，规则要求 high → 不发，且原因说清两端。"""
    rule = _rule(db_session, min_severity="high")
    ok, reason = dispatch.should_notify(rule, _payload(severity="medium"))

    assert ok is False
    assert "medium" in reason and "high" in reason


def test_severity_at_threshold_passes(db_session: Any) -> None:
    """恰好等于阈值要发 —— 阈值是「至少这么严重」。"""
    rule = _rule(db_session, min_severity="high")
    ok, _reason = dispatch.should_notify(rule, _payload(severity="high"))

    assert ok is True


def test_failed_scan_notifies_even_when_complete_switch_off(db_session: Any) -> None:
    """★ 失败**不受**「完成后通知」约束。

    成功的结果躺在那儿等用户下次打开；失败意味着"你交代的事没做成"，
    而他可能正指望今晚有结果。`notify_on_scan_complete` 控制的是"每次扫完
    都吵你一下"，不是"出事也别吭声"。
    """
    rule = _rule(db_session, notify_on_scan_complete=False)
    ok, _reason = dispatch.should_notify(rule, _payload(status="failed", severity="info"))

    assert ok is True


def test_unknown_severity_is_not_notified(db_session: Any) -> None:
    """不认识的严重度**不放行** —— 少发一条比多发一条噪音代价小。"""
    rule = _rule(db_session, min_severity="info")
    ok, _reason = dispatch.should_notify(rule, _payload(severity="kaboom"))

    assert ok is False


# ══════════════════════════════════════════════════════════
# 二、真发
# ══════════════════════════════════════════════════════════


def test_webhook_posts_the_scan_payload(db_session: Any, recorder: _Recorder) -> None:
    """★ 核心用例：确实发了一次 POST，载荷是这次扫描。"""
    _rule(db_session, min_severity="high")
    _channel(db_session, kind="webhook", target="https://hooks.example.com/suvex")

    outcome = dispatch.dispatch(db_session, _payload())

    assert outcome.sent == 1
    assert outcome.failed == 0
    assert len(recorder.calls) == 1
    url, body = recorder.calls[0]
    assert url == "https://hooks.example.com/suvex"
    assert body["event"] == "scan.finished"
    assert body["scan_id"] == "scan_1"
    assert body["target"] == "https://example.com"
    assert body["high_count"] == 2


def test_slack_posts_a_text_field(db_session: Any, recorder: _Recorder) -> None:
    """Slack 的载荷是 `{"text": ...}`，不是 webhook 那个结构。"""
    _rule(db_session, min_severity="high")
    _channel(db_session, kind="slack", target="https://hooks.slack.com/services/x")

    dispatch.dispatch(db_session, _payload())

    _url, body = recorder.calls[0]
    assert set(body.keys()) == {"text"}
    assert "扫描完成" in body["text"]


def test_client_is_built_without_env_proxy(db_session: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """★ `trust_env=False` —— 出站请求不因环境变量里的代理改道。

    与 `providers/llm.py` 里那段是同一个理由：打开 `trust_env` 等于把
    "这次请求由谁收"交给环境，而这台机器上还有别的进程共享环境。
    """
    captured: dict[str, Any] = {}

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, url: str, json: Any = None) -> httpx.Response:
            return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(dispatch.httpx, "Client", _Client)
    monkeypatch.setattr(dispatch.netguard, "ensure_guarded", lambda _c: None)

    _rule(db_session, min_severity="high")
    _channel(db_session)
    dispatch.dispatch(db_session, _payload())

    assert captured.get("trust_env") is False
    assert isinstance(captured.get("transport"), netguard.GuardedHTTPTransport)


# ══════════════════════════════════════════════════════════
# 三、失败要带原因，且写回 verify_error
# ══════════════════════════════════════════════════════════


def test_http_error_is_recorded_with_reason(db_session: Any, recorder: _Recorder) -> None:
    """对端 500：不发成功，原因里带状态码，并写进 verify_error。"""
    _rule(db_session, min_severity="high")
    channel = _channel(db_session)
    recorder.status_code = 500
    recorder.text = '{"error": "boom"}'

    outcome = dispatch.dispatch(db_session, _payload())

    assert outcome.sent == 0
    assert outcome.failed == 1
    assert "HTTP 500" in outcome.results[0].reason
    db_session.refresh(channel)
    assert channel.verified is False
    assert "500" in (channel.verify_error or "")


def test_timeout_is_recorded(db_session: Any, recorder: _Recorder) -> None:
    """超时是**独立**的失败原因，不是笼统的"请求失败"。"""
    _rule(db_session, min_severity="high")
    _channel(db_session)
    recorder.raise_on_post = httpx.ReadTimeout("too slow")

    outcome = dispatch.dispatch(db_session, _payload())

    assert outcome.failed == 1
    assert "没有应答" in outcome.results[0].reason


def test_blocked_address_has_its_own_reason(db_session: Any, recorder: _Recorder) -> None:
    """★ SSRF 拦截要说"解析到内网"，不能混进"对端挂了"。

    混进通用错误里，用户会去查网络，而真正该看的是渠道配置。
    """
    _rule(db_session, min_severity="high")
    _channel(db_session, target="http://169.254.169.254/latest/meta-data/")
    recorder.raise_on_post = netguard.BlockedAddressError("blocked")

    outcome = dispatch.dispatch(db_session, _payload())

    assert outcome.failed == 1
    assert "内网" in outcome.results[0].reason


def test_success_clears_a_previous_failure(db_session: Any, recorder: _Recorder) -> None:
    """★ 发成功要把上次的红点擦掉 —— 否则修好了还一直红。"""
    from app.settings import service as settings_service

    _rule(db_session, min_severity="high")
    channel = _channel(db_session)
    settings_service.mark_channel_verified(db_session, channel.id, ok=False, error="上次失败了")

    outcome = dispatch.dispatch(db_session, _payload())

    assert outcome.sent == 1
    db_session.refresh(channel)
    assert channel.verified is True
    assert channel.verify_error is None


# ══════════════════════════════════════════════════════════
# 四、渠道筛选与隔离
# ══════════════════════════════════════════════════════════


def test_disabled_channel_is_not_used(db_session: Any, recorder: _Recorder) -> None:
    """停用的渠道不参与发送（但仍在 settings 的台账里，那是另一个列表）。"""
    _rule(db_session, min_severity="high")
    _channel(db_session, enabled=False)

    outcome = dispatch.dispatch(db_session, _payload())

    assert outcome.skipped is True
    assert "没有启用中的通知渠道" in outcome.skip_reason
    assert recorder.calls == []


def test_one_broken_channel_does_not_stop_others(db_session: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 不短路：Slack 挂了，webhook 仍然要收到。

    用户常常同时配两条（一个给人看、一个进工单系统）。
    """
    _rule(db_session, min_severity="high")
    _channel(db_session, id="ch_bad", kind="slack", target="https://hooks.slack.com/a")
    _channel(db_session, id="ch_good", kind="webhook", target="https://hooks.example.com/b")

    calls: list[str] = []

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, url: str, json: Any = None) -> httpx.Response:
            calls.append(url)
            if "slack" in url:
                raise httpx.ConnectError("refused")
            return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(dispatch.httpx, "Client", _Client)
    monkeypatch.setattr(dispatch.netguard, "ensure_guarded", lambda _c: None)

    outcome = dispatch.dispatch(db_session, _payload())

    assert len(calls) == 2, "两条渠道都要被尝试"
    assert outcome.sent == 1
    assert outcome.failed == 1


def test_three_channels_all_get_sent(db_session: Any, recorder: _Recorder) -> None:
    """★ 三条渠道全部发到，并且 `_post` 拿到的是**值对象**不是 ORM 实体。

    ## 这条用例守的是什么

    `dispatch()` 在循环前把渠道拍成 `_ChannelView` 快照。守的不变量是
    **`_post` 的入参类型**：它必须与事务无关。

    ⚠️ 曾经这里写的是另一个理由 ——「commit 之后剩下的 ORM 对象会过期，
    第二条起会失联」。**那个说法在本项目里是错的**：sessionmaker 是
    `expire_on_commit=False`（`app/core/db.py:147`），实测直接遍历 ORM
    实体的 SELECT 次数与快照版完全相同（各 6 次）。也就是说「直接用
    实体」在本项目跑得通，这条用例**抓不住**那个改法。

    所以断言放在真正可观测的地方：每个传给 `_post` 的对象都是
    `_ChannelView`，且不带 SQLAlchemy 实例状态。改回实体 → 这里立刻红。
    """
    _rule(db_session, min_severity="high")
    for index in (1, 2, 3):
        _channel(
            db_session,
            id=f"ch_{index}",
            kind="webhook",
            target=f"https://hooks.example.com/{index}",
        )

    seen: list[object] = []
    real_post = dispatch._post

    def _spy(channel: Any, payload: Any) -> Any:
        seen.append(channel)
        return real_post(channel, payload)

    with mock.patch.object(dispatch, "_post", _spy):
        outcome = dispatch.dispatch(db_session, _payload())

    assert outcome.sent == 3, f"三条都要发到，实际 {outcome.sent}"
    assert outcome.failed == 0
    assert len(recorder.calls) == 3
    assert [url for url, _body in recorder.calls] == [
        "https://hooks.example.com/1",
        "https://hooks.example.com/2",
        "https://hooks.example.com/3",
    ]

    # ★ 真正的不变量：入参全是值对象，且**没有** ORM 实例状态。
    assert len(seen) == 3
    for channel in seen:
        assert isinstance(channel, dispatch._ChannelView), (
            f"_post 拿到的是 {type(channel)!r}，必须是 _ChannelView 值对象"
        )
        assert not hasattr(channel, "_sa_instance_state"), "_post 收到了 ORM 实体：发送与事务被耦合在一起了"


def test_email_channel_is_reported_as_unsupported(db_session: Any, recorder: _Recorder) -> None:
    """邮件这一轮不发，而且**说清楚**为什么 —— 不假装发了。"""
    _rule(db_session, min_severity="high")
    _channel(db_session, kind="email", target="sec@example.com")

    outcome = dispatch.dispatch(db_session, _payload())

    assert outcome.failed == 1
    # 没真的出站：`_build_request` 就在这一层拒绝了。
    assert recorder.calls == []
    assert "邮件通道尚未接入" in outcome.results[0].reason


# ══════════════════════════════════════════════════════════
# 五、/test 端点：真的发了没有
# ══════════════════════════════════════════════════════════


def test_test_endpoint_reports_success_when_delivered(ready_client: TestClient, recorder: _Recorder) -> None:
    """★ v0.89 起 `/test` 会真的发；成功时 `verified` 变 True。"""
    created = ready_client.post(
        f"{API}/settings/notices/channels",
        json={
            "kind": "webhook",
            "label": "安全组 Webhook",
            "target": "https://hooks.example.com/suvex",
            "enabled": True,
        },
    )
    assert created.status_code == 201, created.text
    channel_id = created.json()["id"]

    response = ready_client.post(f"{API}/settings/notices/channels/{channel_id}/test")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verified"] is True, body
    assert not body["verifyError"]
    assert len(recorder.calls) == 1


def test_test_endpoint_reports_failure_with_reason(ready_client: TestClient, recorder: _Recorder) -> None:
    """发送失败时 `verified=False` 且**必须**给原因。"""
    created = ready_client.post(
        f"{API}/settings/notices/channels",
        json={
            "kind": "webhook",
            "label": "安全组 Webhook",
            "target": "https://hooks.example.com/suvex",
            "enabled": True,
        },
    )
    channel_id = created.json()["id"]
    recorder.status_code = 503

    response = ready_client.post(f"{API}/settings/notices/channels/{channel_id}/test")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verified"] is False
    assert "503" in body["verifyError"]


def test_test_endpoint_still_rejects_email(ready_client: TestClient) -> None:
    """邮件渠道仍然报"未接入"，且**不出站**。"""
    created = ready_client.post(
        f"{API}/settings/notices/channels",
        json={
            "kind": "email",
            "label": "安全组邮件",
            "target": "sec@example.com",
            "enabled": True,
        },
    )
    channel_id = created.json()["id"]

    response = ready_client.post(f"{API}/settings/notices/channels/{channel_id}/test")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verified"] is False
    assert "邮件通道尚未接入" in body["verifyError"]


def test_bad_target_shape_is_rejected_before_sending(ready_client: TestClient, recorder: _Recorder) -> None:
    """★ 形状不对**不发** —— 形状错的地址常常仍指向一台真实的外部主机。"""
    created = ready_client.post(
        f"{API}/settings/notices/channels",
        json={
            "kind": "webhook",
            "label": "坏地址",
            "target": "not-a-url",
            "enabled": True,
        },
    )
    channel_id = created.json()["id"]

    response = ready_client.post(f"{API}/settings/notices/channels/{channel_id}/test")

    body = response.json()
    assert body["verified"] is False
    assert recorder.calls == [], "形状不对不该出站"


# ══════════════════════════════════════════════════════════
# 六、纯函数：载荷形状
# ══════════════════════════════════════════════════════════


def test_webhook_body_keeps_snake_case() -> None:
    """★ 对外协议用 snake_case —— 它的消费者是别人的脚本，改名就断。"""
    body, _shape = dispatch.build_body("webhook", _payload())

    assert "scan_id" in body
    assert "scanId" not in body


def test_unknown_kind_raises() -> None:
    """未知渠道类型是**编程错误**，抛而不是静默返回空载荷。"""
    with pytest.raises(ValueError):
        dispatch.build_body("carrier-pigeon", _payload())


def test_slack_text_starts_with_the_conclusion() -> None:
    """Slack 预览截断很早，第一段就得是结论，不能是标题前缀。"""
    body, _shape = dispatch.build_body("slack", _payload())
    text = body["text"]

    assert text.startswith("https://example.com")
    assert "扫描完成" in text


# ══════════════════════════════════════════════════════════
# 七、`notify_on_critical_immediate` 的**消费端**（v0.90）
# ══════════════════════════════════════════════════════════
#
# 2026-10-26 审计发现：这个开关能被保存、能被回显，但**没有任何投递路径读它**。
# 设置页原本写着「不等扫描结束，边跑边发」，而实际投递只在扫描收尾发生一次。
#
# v0.90 把它接上了：`_import_for` 在**新建**出 critical 记录时入队一条
# `scan.critical`，`dispatch` 按 `payload.event` 分派到
# `should_notify_critical`。下面这些用例钉住三件事：
#
# 1. 开关真的说话（关掉就不发）；
# 2. 两个开关**互不蕴含**（关掉收尾回执不该连告警一起静音）；
# 3. 载荷的 `event` 真的进 webhook body（那是接收方脚本的分派键）。
#
# ⚠️ 历史的教训留在这里：这些断言原来是反的（断言"它没有作用"）。
#    反转它们的原因不是断言写错了，而是**行为变了** —— 一个钉住
#    "某功能没接上"的测试，唯一正确的处理就是在接上时反转它，
#    否则它会开始保护那个 bug。


def test_critical_immediate_off_means_no_alert(db_session: Any) -> None:
    """开关关掉时，途中告警不发 —— 且原因说得清是哪个开关。"""
    from app.settings import dispatch as d

    payload = _payload(severity="critical", status="running", event=d.EVENT_SCAN_CRITICAL)
    rule = _rule(db_session, notify_on_critical_immediate=False)

    should, reason = d.should_notify_critical(rule, payload)
    assert should is False
    assert "立即通知" in reason


def test_critical_immediate_on_means_alert_even_if_complete_off(db_session: Any) -> None:
    """两个开关互不蕴含：关掉「扫描结束时通知」不该连告警一起静音。

    ★ 这是这条路径最容易写错的地方 —— 顺手加一句
      `if not rule.notify_on_scan_complete: return False` 看着很自然
      （"都没开通知还发什么"），但关掉回执的**常见理由**恰恰是
      "别每次都吵我，只在真出事时叫我"。串起来就正好灭了他们唯一的告警。
    """
    from app.settings import dispatch as d

    payload = _payload(severity="critical", status="running", event=d.EVENT_SCAN_CRITICAL)
    rule = _rule(db_session, notify_on_critical_immediate=True, notify_on_scan_complete=False)

    should, _reason = d.should_notify_critical(rule, payload)
    assert should is True


def test_critical_alert_ignores_min_severity(db_session: Any) -> None:
    """`min_severity` 是**收尾**时的判断，不该影响途中告警。

    阈值最高只能选到 critical，而这条路上的事件本身已经是 critical ——
    再比一次不算错，但会让"阈值"看起来对这条路也有语义，那是误导。
    """
    from app.settings import dispatch as d

    payload = _payload(severity="critical", status="running", event=d.EVENT_SCAN_CRITICAL)
    # 阈值选到 critical（最严），告警仍然该发：它就是 critical。
    rule = _rule(db_session, min_severity="critical", notify_on_critical_immediate=True)

    should, _reason = d.should_notify_critical(rule, payload)
    assert should is True


def test_critical_path_rejects_non_critical_payload(db_session: Any) -> None:
    """走错门的载荷要被挡住，而不是发出去。

    `_import_for` 只在 `created_max_severity == "critical"` 时入队，
    所以正常情况下不会出现 non-critical 的 `scan.critical` 载荷。
    但"正常情况下不会"不是判据 —— 拼错一个字段名就会造出这种载荷，
    而它发出去会让接收方以为出了严重问题。
    """
    from app.settings import dispatch as d

    high = _payload(severity="high", status="running", event=d.EVENT_SCAN_CRITICAL)
    rule = _rule(db_session, notify_on_critical_immediate=True)

    should, reason = d.should_notify_critical(rule, high)
    assert should is False
    assert "critical" in reason


def test_finished_payload_is_not_judged_by_critical_rule(db_session: Any) -> None:
    """收尾载荷交给 `should_notify_critical` 一定被拒 —— 它走的是另一条路。"""
    from app.settings import dispatch as d

    finished = _payload(severity="critical", status="completed")
    rule = _rule(db_session, notify_on_critical_immediate=True)

    should, reason = d.should_notify_critical(rule, finished)
    assert should is False
    assert d.EVENT_SCAN_FINISHED in reason


def test_webhook_event_field_carries_the_dispatch_key() -> None:
    """`event` 是写给**别人的脚本**做 `if event == ...` 分派用的。

    所以它必须真的出现在 body 里，且两个路径取不同的值。
    """
    from app.settings import dispatch as d

    finished = _payload().as_webhook_body()
    critical = _payload(event=d.EVENT_SCAN_CRITICAL).as_webhook_body()

    assert finished["event"] == d.EVENT_SCAN_FINISHED
    assert critical["event"] == d.EVENT_SCAN_CRITICAL
    # 别的字段不该因为这个新字段而变形
    assert finished["scan_id"] == critical["scan_id"] == "scan_1"


def test_dispatch_routes_critical_payload_to_the_critical_rule(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dispatch()` 按 `payload.event` 选判断函数，而不是永远用 `should_notify`。

    ★ 用打桩断言"选了哪个判断函数"，而不是断言最终发出的字节 —
      因为 `should_notify` 在 `critical + completed` 上也会返回 True
      （阈值允许时），于是"选错函数"这件事在结果上**看不出来**。
    """
    from app.settings import dispatch as d

    used: list[str] = []

    def fake_finished(rule: Any, payload: Any) -> tuple[bool, str]:
        used.append("finished")
        return False, "stub"

    def fake_critical(rule: Any, payload: Any) -> tuple[bool, str]:
        used.append("critical")
        return False, "stub"

    monkeypatch.setattr(d, "should_notify", fake_finished)
    monkeypatch.setattr(d, "should_notify_critical", fake_critical)

    d.dispatch(db_session, _payload(event=d.EVENT_SCAN_FINISHED))
    d.dispatch(db_session, _payload(event=d.EVENT_SCAN_CRITICAL))

    assert used == ["finished", "critical"]
