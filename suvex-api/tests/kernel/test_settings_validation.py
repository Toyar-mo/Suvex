"""`PUT /ctf2/settings` 的校验行为：漏传一项必须**报错**，不能静默补默认值。

## 这个文件守的是什么

`PUT /ctf2/settings` 是**全量提交**语义 —— `update_ctf2_settings` 逐字段
赋值，请求体里没有的字段按后端默认值落库。所以"给字段一个默认值"在这里
不是无害的便利，而是**静默关掉一个已经开着的功能的唯一途径**：

```
用户打开场景包开关（用的可能是 API，也可能是将来某个还没写的界面）
  ↓
某次保存改的是别的项，payload 里没带那一个开关
  ↓
后端按 default=False 落库 → 开关被关掉
```

而界面上看不出发生过什么：那一次保存返回 200、页面正常刷新、用户以为
自己只改了那一项。这类缺陷不会自己暴露 —— 要等到某道 `agentsec-*` 题
解题时的提示词少了一整块纪律，而那时代码已经跑完了。

## 为什么断言"必填"而不是"默认值取哪个更安全"

判据不是"哪个值安全"，而是**服务端能不能区分"调用方没传"和"调用方传了
false"**。区分不了，给默认值就是在替调用方做决定。`false` 只在"用户从没
打开过这个功能"时才是安全的；而服务端无法知道现在是哪种情况。

⚠️ 所以本文件里的用例**不检查默认值等于什么**，而是检查"漏传"这件事本身
被拒绝了 —— 那是唯一能区分两种意图的方式。

## 两处都要拦：HTTP 入口与 service 入口

`KernelSettingsIn` 的必填字段只拦得住打 HTTP 的那一侧。`tests/` 里大量
helper 与运维脚本是**直接调 `service.update_ctf2_settings`** 的，那条路径
不经过 pydantic。如果只改 schema，同一个缺陷会从另一个入口原样复现 ——
所以 `update_ctf2_settings` 的两个开关形参也去掉了默认值，改成必填
关键字参数（Python 在调用时当场 `TypeError`）。两处各有自己的用例。

## ⚠️ 状态码是 400 不是 422

本项目的 `app/core/errors.py` 把 FastAPI 的 `RequestValidationError`
统一翻译成 `400 VALIDATION_FAILED`。照 FastAPI 默认行为写 422 会让用例
永远红，而失败原因看起来像"字段没校验"。同理，报错详情里出现的是
**蛇形**字段名（pydantic 校验的是**模型字段**，`alias_generator` 只作用于
序列化/反序列化的键名），断言驼峰会永远红。
"""

from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.kernel import service

API = "/api/v1"

#: 两个场景包开关。它们被单独拎出来是因为**只有它们**曾经带默认值
#: （`fallbackProviderId` 也带，但它是可空字段，`None` 与"不传"等价，
#: 不构成替调用方做决定 —— 见 `KernelSettingsIn` 的说明）。
SCENARIO_SWITCHES = ("enableAgentsecScenarios", "enableIrScenarios")


def _settings_body(**overrides: object) -> dict[str, object]:
    """拼一个**完整**的 `PUT /ctf2/settings` 请求体。

    ⚠️ 字段全部必填，所以这里必须给全 —— 这正是这个 helper 的用途：
    用例只在它之上做"删掉某一项"或"改某一项"，不重复抄十个字段。

    ⚠️ 两个开关这里写成 `False` 而不是省略，因为省略本身就是被测的
    那个错误。要测省略，用例自己 `pop`。
    """
    body: dict[str, object] = {
        "submitMode": "safe",
        "maxWrongSubmissions": 3,
        "defaultSandboxProfile": "ctf2-sandbox",
        "maxConcurrentSolves": 3,
        "solvePollSeconds": 5.0,
        "autoSyncSeconds": 300,
        "autoPriorityNew": True,
        "autoStartNew": False,
        "maxTurns": 40,
        "fallbackProviderId": None,
        "enableAgentsecScenarios": False,
        "enableIrScenarios": False,
    }
    body.update(overrides)
    return body


def _restore(ready_client: TestClient) -> None:
    """把配置还原成"两个开关都关"，免得污染同一会话里后面的用例。"""
    ready_client.put(f"{API}/ctf2/settings", json=_settings_body())


# ══════════════════════════════════════════════════════════
# HTTP 入口：漏传 → 400，不是 200
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize("missing", SCENARIO_SWITCHES)
def test_missing_switch_is_rejected_not_silently_defaulted(
    ready_client: TestClient, missing: str
) -> None:
    """★★ 请求体漏掉一个开关 → **400 拒绝**。

    这是本文件的核心。若实现给它加了默认值，这条会变成 200 ——
    而后果是"用户打开过的开关被下一次无关的保存清掉，界面上看不出"。

    两个开关各跑一遍（而不是只测一个）：它们是**两种独立功能**，
    共用一个会被写成"只给其中一个加默认值"的半修，而那种半修恰好
    会让其中一族悄悄失效。
    """
    body = _settings_body()
    body.pop(missing)
    response = ready_client.put(f"{API}/ctf2/settings", json=body)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    # ⚠️ 蛇形名 —— 见模块 docstring 最后一段。
    snake = "enable_agentsec_scenarios" if missing == "enableAgentsecScenarios" else "enable_ir_scenarios"
    assert snake in response.text, "报错必须点名缺的是哪一项，否则调用方只能猜"


def test_both_switches_missing_is_also_rejected(ready_client: TestClient) -> None:
    """两个都漏 → 同样被拒（这条防"只把其中一个改成必填"的半修）。"""
    body = _settings_body()
    for name in SCENARIO_SWITCHES:
        body.pop(name)
    response = ready_client.put(f"{API}/ctf2/settings", json=body)
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("value", [True, False])
def test_explicit_value_still_round_trips(
    ready_client: TestClient, value: bool
) -> None:
    """★★ 显式传值照常写入 —— 两种值都要能存。

    ⚠️ `True` 与 `False` 都跑，是因为"拒绝漏传"很容易被实现成
    "拒绝这一项"（比如把字段写成必填但后端忽略它）。只测 `True`
    发现不了"存不进去 false"，而那意味着**开关永远关不掉**。
    """
    try:
        saved = ready_client.put(
            f"{API}/ctf2/settings",
            json=_settings_body(
                enableAgentsecScenarios=value, enableIrScenarios=value
            ),
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["enableAgentsecScenarios"] is value
        assert saved.json()["enableIrScenarios"] is value

        reread = ready_client.get(f"{API}/ctf2/settings")
        assert reread.status_code == 200
        assert reread.json()["enableAgentsecScenarios"] is value, "读回来的值必须与写进去的一致"
    finally:
        _restore(ready_client)


def test_the_two_switches_do_not_flip_each_other(ready_client: TestClient) -> None:
    """★★ 两个开关各自独立 —— 打开一个不会带翻另一个。

    这不是"顺手多测一条"：两族分类（`agentsec-*` / `ir-*`）的校验与
    提示词注入是**分别**由这两个开关门控的。若实现把它们接到同一列
    （或同一个默认值来源），症状是"打开 IR 之后 agentsec 的分类也能过
    校验，但它的提示词块不注入" —— 分类合法、纪律没注入，且没有任何
    报错。
    """
    try:
        first = ready_client.put(
            f"{API}/ctf2/settings",
            json=_settings_body(enableAgentsecScenarios=True, enableIrScenarios=False),
        )
        assert first.status_code == 200, first.text
        assert first.json()["enableAgentsecScenarios"] is True
        assert first.json()["enableIrScenarios"] is False, "打开 agentsec 不该带翻 ir"

        second = ready_client.put(
            f"{API}/ctf2/settings",
            json=_settings_body(enableAgentsecScenarios=True, enableIrScenarios=True),
        )
        assert second.status_code == 200, second.text
        assert second.json()["enableAgentsecScenarios"] is True, "打开 ir 不该带翻 agentsec"
        assert second.json()["enableIrScenarios"] is True
    finally:
        _restore(ready_client)


# ══════════════════════════════════════════════════════════
# service 入口：漏传 → 当场 TypeError
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize("missing", ["enable_agentsec_scenarios", "enable_ir_scenarios"])
def test_service_function_requires_both_switches(
    db_session: Session, missing: str
) -> None:
    """★★ 直接调 service 函数时漏传 → 立刻 `TypeError`。

    为什么这条不能省：`KernelSettingsIn` 只拦得住打 HTTP 的调用方。
    `tests/` 里的 helper 和运维脚本是直接调这个函数的，那条路径
    不经过 pydantic —— 只改 schema 的话，同一个缺陷会从另一个入口
    原样复现。

    ⚠️ 断言的是 `TypeError` 而**不是**"落库成了 False"：Python 在
    调用时就拒绝，比"写进库再回头发现值不对"早得多，也不会留下
    半截写入。这是把"配置类端点漏传一项是调用方 bug"那条纪律从
    运行时前移到调用点。
    """
    row = service.get_ctf2_config(db_session)
    kwargs: dict[str, object] = {
        "submit_mode": row.submit_mode,
        "max_wrong_submissions": row.max_wrong_submissions,
        "default_sandbox_profile": row.default_sandbox_profile,
        "max_concurrent_solves": row.max_concurrent_solves,
        "solve_poll_seconds": row.solve_poll_seconds,
        "auto_sync_seconds": row.auto_sync_seconds,
        "auto_priority_new": row.auto_priority_new,
        "auto_start_new": row.auto_start_new,
        "max_turns": row.max_turns,
        "fallback_provider_id": row.fallback_provider_id,
        "enable_agentsec_scenarios": row.enable_agentsec_scenarios,
        "enable_ir_scenarios": row.enable_ir_scenarios,
    }
    kwargs.pop(missing)
    with pytest.raises(TypeError, match=missing):
        service.update_ctf2_settings(db_session, **kwargs)  # type: ignore[arg-type]


def test_service_function_has_no_default_for_the_switches() -> None:
    """★★ 反射检查：两个形参在签名里**没有默认值**。

    与上面那条互为冗余，但冗余是有意的 —— 上面那条验行为，这条验
    契约，且它的失败信息直接指出"谁又给这两个参数加了默认值"，
    而行为用例失败时看到的是一个 `TypeError` 没被抛出来。
    """
    signature = inspect.signature(service.update_ctf2_settings)
    for name in ("enable_agentsec_scenarios", "enable_ir_scenarios"):
        param = signature.parameters[name]
        assert param.default is inspect.Parameter.empty, (
            f"`{name}` 又带默认值了 —— 那会让漏传它的调用点静默改配置"
        )


def test_service_function_keeps_non_switch_defaults() -> None:
    """`fallback_provider_id` 仍可省略 —— 别把这次改动扩大到它身上。

    它是**可空**字段：`None` 与"不传"语义完全等价（都表示"不启用换模"），
    所以默认值不构成"替调用方做决定"。去掉它的默认值会动到 v0.35 那个
    功能的契约，与本单无关。
    """
    param = inspect.signature(service.update_ctf2_settings).parameters[
        "fallback_provider_id"
    ]
    assert param.default is None
