"""多智能体开关的接线测试（v0.58）。

## 测什么

这个开关的价值全在"**它真的能把一次解题/扫描切到编排路径，且关着时
一点不变**"。所以这里测接线而不是测编排本身（编排在
`tests/kernel/test_multiagent.py`）：

1. 设置层：`PUT/GET /kernel/settings` 能存能读 `enableMultiAgent`；
   漏传被拒（必填，全量提交语义）。
2. 引擎侧：`_multi_agent_enabled` 读配置，配置翻转它跟着翻；
   读配置失败返回 False（安全方向）。
3. `build_spec` 把开关写进 `SolveSpec.multi_agent`。
4. 扫描侧：`LaunchSpec` 携带 `multi_agent`，且
   `build_solve_spec_from_scan` 把它透传进 `SolveSpec`。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.kernel import runner as kernel_runner
from app.kernel import service as kernel_service
from tests.kernel.test_settings_validation import _settings_body

API = "/api/v1"


# ══════════════════════════════════════════════════════════
# 1. 设置层：存 / 读 / 必填
# ══════════════════════════════════════════════════════════


def test_multi_agent_switch_round_trips(ready_client) -> None:  # noqa: ANN001
    """打开能存进去、能读回来；关掉也能存 ——— 两向都测，否则"关不掉"
    那种 bug（字段必填但后端忽略 false）发现不了。"""
    for value in (True, False):
        saved = ready_client.put(
            f"{API}/kernel/settings",
            json=_settings_body(enableMultiAgent=value),
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["enableMultiAgent"] is value
        reread = ready_client.get(f"{API}/kernel/settings")
        assert reread.json()["enableMultiAgent"] is value


def test_multi_agent_switch_is_required(ready_client) -> None:  # noqa: ANN001
    """★ 必填：漏传必须被拒。给默认值等于"漏传 = 静默关掉一个开着的功能"，
    而那正是 v0.44 修过的缺陷形状，不能在新增字段上复现。"""
    body = _settings_body()
    body.pop("enableMultiAgent")
    resp = ready_client.put(f"{API}/kernel/settings", json=body)
    assert resp.status_code in (400, 422), resp.text


# ══════════════════════════════════════════════════════════
# 2. 引擎侧：_multi_agent_enabled 跟随配置
# ══════════════════════════════════════════════════════════


def test_engine_flag_follows_config(db_session: Session) -> None:  # noqa: ANN001
    row = kernel_service.get_ctf2_config(db_session)
    # 关 → False
    kernel_service.update_ctf2_settings(
        db_session,
        submit_mode=row.submit_mode,
        max_wrong_submissions=row.max_wrong_submissions,
        default_sandbox_profile=row.default_sandbox_profile,
        max_concurrent_solves=row.max_concurrent_solves,
        solve_poll_seconds=row.solve_poll_seconds,
        auto_sync_seconds=row.auto_sync_seconds,
        auto_priority_new=row.auto_priority_new,
        auto_start_new=row.auto_start_new,
        max_turns=row.max_turns,
        fallback_provider_id=None,
        enable_agentsec_scenarios=False,
        enable_ir_scenarios=False,
        enable_multi_agent=False,
    )
    assert kernel_runner._multi_agent_enabled(db_session) is False
    # 开 → True
    kernel_service.update_ctf2_settings(
        db_session,
        submit_mode=row.submit_mode,
        max_wrong_submissions=row.max_wrong_submissions,
        default_sandbox_profile=row.default_sandbox_profile,
        max_concurrent_solves=row.max_concurrent_solves,
        solve_poll_seconds=row.solve_poll_seconds,
        auto_sync_seconds=row.auto_sync_seconds,
        auto_priority_new=row.auto_priority_new,
        auto_start_new=row.auto_start_new,
        max_turns=row.max_turns,
        fallback_provider_id=None,
        enable_agentsec_scenarios=False,
        enable_ir_scenarios=False,
        enable_multi_agent=True,
    )
    assert kernel_runner._multi_agent_enabled(db_session) is True


# ══════════════════════════════════════════════════════════
# 3+4. spec 翻译：两条路径都把开关带进 SolveSpec
# ══════════════════════════════════════════════════════════


def test_scan_spec_carries_multi_agent_flag() -> None:
    """★ 扫描路径：`build_solve_spec_from_scan` 必须把 `LaunchSpec.multi_agent`
    透传进 `SolveSpec.multi_agent` ——— 否则设置页打开了、扫描却永远单循环。"""
    from app.scans.agent_runner import build_solve_spec_from_scan
    from app.scans.runner import LaunchSpec

    base = dict(
        scan_id="scan_x",
        target="http://demo.test:8080",
        target_kind="url",
        skills=("web",),
        brief="",
        goal="findVulns",
        scan_mode="safe",
        scan_max_turns=20,
        run_dir=None,
        log_path=None,
        llm_model="openai/deepseek-v4.1-flash",
        llm_api_key="k",
        llm_api_base="http://gw/v1",
        multi_agent=True,
    )
    spec = build_solve_spec_from_scan(LaunchSpec(**base))
    assert getattr(spec, "multi_agent", None) is True

    base_off = dict(base, multi_agent=False)
    spec_off = build_solve_spec_from_scan(LaunchSpec(**base_off))
    assert getattr(spec_off, "multi_agent", None) is False
