"""v1.61：CTF 求解路径的**墙钟预算**（`wall_clock_seconds`）。

## 这个文件守的是什么

在这之前，CTF 解题循环的 `SolveSpec.wall_clock_seconds` **恒为 `None`**
（只有渗透路径 `app/scans/agent_runner.py` 会赋值）。后果不是"少个优化"：
主循环里那套墙钟三档催促（`WALL_WARN_BANDS` = 60/80/92%，是整套催促机制
里**唯一**的按时间轴维度）因此一次都没触发过，只剩"回合催"在兜底。
而回合催治不了"一轮花很久"的题 —— 真库实测有题目跑满 80 轮、单次求解
墙钟烧到 9.5 小时，中间回合比一直停在低位，催促全程沉默。

所以这里守三件事：
1. 配置层：`wall_clock_seconds` 能存、能读、能校验区间、`None` 表示"自动"；
2. 推导层：`default_ctf2_wall_clock_seconds` = `max_turns × 45` 夹 `[MIN, MAX]`；
3. 装配层：`build_spec`（`SolveSpec`）真正把非 None 的墙钟传下去，且
   主循环开跑前打 `wall_clock_started_at` —— 否则前面两项都是白搭，
   而这正是"防护写了、没接上电"的老病（对照 v0.48 CPU/内存上限漏赋值）。

## 为什么 `None` 是"自动"而不是"关闭"

与 `fallbackProviderId`（NULL = 关闭）**语义相反**。这里 NULL 表示"让系统
按回合预算自动给一个合理墙钟"。理由：零配置就修好病态长跑，且自动值随
`max_turns` 联动，不会出现"改了回合忘了改墙钟"的错位。这一点单独用
`test_none_means_auto_not_off` 钉死，因为它是本功能最容易被误读的契约。
"""

from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.vocab import (
    DEFAULT_CTF2_SECONDS_PER_TURN,
    MAX_CTF2_WALL_CLOCK_SECONDS,
    MIN_CTF2_WALL_CLOCK_SECONDS,
    default_ctf2_wall_clock_seconds,
)
from app.kernel import service

API = "/api/v1"


def _settings_body(**overrides: object) -> dict[str, object]:
    """拼一个完整的 `PUT /ctf2/settings` 请求体（字段全必填）。"""
    body: dict[str, object] = {
        "submitMode": "safe",
        "maxWrongSubmissions": 3,
        "defaultSandboxProfile": "suvex-sandbox",
        "maxConcurrentSolves": 3,
        "solvePollSeconds": 5.0,
        "autoSyncSeconds": 300,
        "autoPriorityNew": True,
        "autoStartNew": False,
        "maxTurns": 40,
        "fallbackProviderId": None,
        "enableAgentsecScenarios": False,
        "enableIrScenarios": False,
        "enableMultiAgent": False,
    }
    body.update(overrides)
    return body


def _restore(ready_client: TestClient) -> None:
    """还原成默认（墙钟自动、开关全关），免得污染后面的用例。"""
    ready_client.put(f"{API}/kernel/settings", json=_settings_body())


# ══════════════════════════════════════════════════════════
# 推导层：default_ctf2_wall_clock_seconds
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("turns", "expected"),
    [
        # 80 轮 × 45s = 3600（一小时）—— 常见默认值。
        (80, 80 * DEFAULT_CTF2_SECONDS_PER_TURN),
        # 难题提到 200 轮 → 9000s，但**被 90 分钟的成本闸夹住**。
        # 2026-11-08 改：上界从 24 小时收紧到 90 分钟 —— 500 轮 × 45s
        # 推出来是 6.25 小时，一道题能把并发槽占一整天，而调研里没有
        # 一家这么做（Muteki 每题 4000s、Naptime 16–32 步）。
        (200, 90 * 60),
    ],
)
def test_derivation_scales_with_turns(turns: int, expected: int) -> None:
    """★★ 墙钟随回合预算联动 —— 这是"选定自动推导而非固定值"的核心理由。"""
    assert default_ctf2_wall_clock_seconds(turns) == expected


def test_derivation_clamps_to_min() -> None:
    """极小回合数被抬到下界（否则墙钟会小到一开跑就催收尾）。"""
    # 5 轮 × 45 = 225 > 120，正常不触下界；用 1 轮逼出夹取。
    assert default_ctf2_wall_clock_seconds(1) == MIN_CTF2_WALL_CLOCK_SECONDS


def test_derivation_clamps_to_max() -> None:
    """极大回合数被压到上界（防止手滑填出荒谬墙钟，非成本闸）。"""
    assert (
        default_ctf2_wall_clock_seconds(10_000) == MAX_CTF2_WALL_CLOCK_SECONDS
    )


def test_derivation_never_below_min_even_at_zero() -> None:
    """回合 0 也不返回 0 —— 0 秒墙钟等于"立刻收尾"，明显是配置错误。"""
    assert default_ctf2_wall_clock_seconds(0) == MIN_CTF2_WALL_CLOCK_SECONDS


# ══════════════════════════════════════════════════════════
# 配置层 HTTP：存 / 读 / 校验
# ══════════════════════════════════════════════════════════


def test_none_means_auto_not_off(ready_client: TestClient) -> None:
    """★★ `wallClockSeconds=None` 往返后仍是 None（= 自动推导）。

    这是本功能**最容易被误读**的契约：`None` 不是"关闭墙钟"，而是
    "按回合自动给"。若某次重构把它当成"关闭"（像 fallback 那样），
    症状是"默认全都不催了"，而代码上看起来只是"可空字段"。
    """
    try:
        saved = ready_client.put(
            f"{API}/kernel/settings", json=_settings_body(wallClockSeconds=None)
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["wallClockSeconds"] is None

        reread = ready_client.get(f"{API}/kernel/settings")
        assert reread.status_code == 200
        assert reread.json()["wallClockSeconds"] is None, (
            "读回来的必须是 None（自动），不能变成某个具体数"
        )
    finally:
        _restore(ready_client)


def test_explicit_value_round_trips(ready_client: TestClient) -> None:
    """显式给了数就存进去、读回来一致。"""
    try:
        saved = ready_client.put(
            f"{API}/kernel/settings", json=_settings_body(wallClockSeconds=1800)
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["wallClockSeconds"] == 1800

        reread = ready_client.get(f"{API}/kernel/settings")
        assert reread.json()["wallClockSeconds"] == 1800
    finally:
        _restore(ready_client)


@pytest.mark.parametrize("bad", [MIN_CTF2_WALL_CLOCK_SECONDS - 1, MAX_CTF2_WALL_CLOCK_SECONDS + 1])
def test_out_of_range_is_rejected(ready_client: TestClient, bad: int) -> None:
    """越界的墙钟 → 400 拒绝，**不静默夹取**。

    静默夹取的症状：用户填 120、保存成功、实际跑 3600，他会以为是 bug 再改
    一次 —— 与 `_assert_max_turns` 同一条纪律。
    """
    response = ready_client.put(
        f"{API}/kernel/settings", json=_settings_body(wallClockSeconds=bad)
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_omitted_field_is_auto_not_error(ready_client: TestClient) -> None:
    """★ 与两个场景开关**相反**：漏传 `wallClockSeconds` 不报错，落成 None（自动）。

    它带默认值是有意的 —— 与 `fallbackProviderId` 同类可空字段：`None` 与
    "不传"语义等价（都=自动），不构成"替调用方做决定"。这条用例把这个
    "为什么它和开关待遇不同"的契约钉死，防止未来有人照抄开关的"必填"改法。
    """
    body = _settings_body()
    # `_settings_body()` 本就不含 `wallClockSeconds`（它是可空可选字段，
    # 默认不写）；用 pop 的默认值形式保证这里真的"漏传"而非报 KeyError。
    body.pop("wallClockSeconds", None)
    try:
        saved = ready_client.put(f"{API}/kernel/settings", json=body)
        assert saved.status_code == 200, saved.text
        assert saved.json()["wallClockSeconds"] is None
    finally:
        _restore(ready_client)


# ══════════════════════════════════════════════════════════
# service 入口：可空、有默认
# ══════════════════════════════════════════════════════════


def test_service_default_is_none() -> None:
    """`update_ctf2_settings` 的 `wall_clock_seconds` 有默认 `None`。

    与两个场景开关（无默认、必填）**不同**：它是可空字段，`None`=自动，
    与"不传"等价。这条防止有人把它误改成必填（那会强迫每个调用点都传值，
    而"自动"本该是零配置默认）。
    """
    param = inspect.signature(service.update_ctf2_settings).parameters[
        "wall_clock_seconds"
    ]
    assert param.default is None


def test_service_persists_value(db_session: Session) -> None:
    """服务层直接写序列：设了值 → 读回来是它；设 None → 读回 None。"""
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
        "enable_multi_agent": row.enable_multi_agent,
    }
    try:
        service.update_ctf2_settings(db_session, wall_clock_seconds=2400, **kwargs)
        assert service.get_ctf2_config(db_session).wall_clock_seconds == 2400

        service.update_ctf2_settings(db_session, wall_clock_seconds=None, **kwargs)
        assert service.get_ctf2_config(db_session).wall_clock_seconds is None
    finally:
        service.update_ctf2_settings(db_session, wall_clock_seconds=None, **kwargs)


def test_effective_settings_carry_wall_clock(db_session: Session) -> None:
    """`effective_ctf2_settings` 把墙钟带进 `KernelSettings`（供 build_spec 读）。"""
    settings = service.effective_ctf2_settings(db_session)
    # 默认（未显式设）应当是 None = 自动。
    assert hasattr(settings, "wall_clock_seconds")
    assert settings.wall_clock_seconds is None


# ══════════════════════════════════════════════════════════
# 装配层：build_spec 真把墙钟接到 SolveSpec
# ══════════════════════════════════════════════════════════


def test_build_spec_sets_wall_clock_from_settings(
    db_session: Session, ctf2_challenge, ctf2_provider
) -> None:
    """★★ 装配层铁证：`build_spec` 产出的 `SolveSpec.wall_clock_seconds` 非 None。

    这条是整个改动"接上电"的判据。前面推导/配置全对，但只要 `build_spec`
    没把它塞进 `SolveSpec`，主循环的 `_wall_clock_band` 仍返回 None，
    三档催促照样沉默 —— 正是"防护写了、没接上电"的老病。所以这里不看
    每个中间值，只问最终结论：**装配出来的 spec 带不带墙钟**。

    `ctf2_challenge`/`ctf2_provider` 是 conftest 里的既有夹具
    （一道道地的挑战 + 一个可用 provider），`build_spec` 自己从库里挑
    provider，不需要我们手造。
    """
    from app.kernel import runner as runner_mod

    spec = runner_mod.build_spec(db_session, ctf2_challenge)
    assert spec.wall_clock_seconds is not None, (
        "build_spec 必须给 SolveSpec 填 wall_clock_seconds，否则主循环墙钟催促全程沉默"
    )
    # 默认应是 45s × 回合预算（未显式配置时）。
    assert spec.wall_clock_seconds == default_ctf2_wall_clock_seconds(spec.max_turns)

    # 起点尚未打（那是 solve_in_sandbox 开跑时的事）。
    assert spec.wall_clock_started_at is None
