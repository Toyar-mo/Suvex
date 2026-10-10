"""`settings` 领域的读写逻辑。

## 为什么要有这一层

`sandbox_config` 是一张**单例表**。"没有就建一条"这件事如果散落在路由与
调度器两处各自实现，迟早会有一处忘了建、或者两处建出两行 —— 而单例表一旦
出现两行，行为就变成"看哪个先返回"，不可复现。取/建只有一个版本。

## ★ 覆盖优先级：DB 覆盖 env，env 是兜底

执行类参数（并发 / 回合上限 / 扫描深度）在两个地方都有值：

* `app/core/config.py` 的 env 默认值 —— **部署基线**。CI、迁移、
  "用户从没打开过设置页"的实例，走的都是它；
* `sandbox_config` 的三列 —— 用户在设置页改过的值。

规则：**DB 有值就用 DB，为 `NULL` 就退回 env。**

`NULL` 在这里不是"没配置"，而是一个明确的语义：**用户没覆盖过**。
所以那三列刻意可空 —— 见 `settings/models.py` 里的说明。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.vocab import ScanMode, values
from app.settings.models import SINGLETON_ID, Ctf2Config, SandboxConfig

# ── 执行参数的合法区间 ────────────────────────────────────
#
# 下限取 1 而不是 0：并发 0 等于"永远不启动任何扫描"，
# 那不是配置而是把自己锁在门外。
MIN_CONCURRENT_SCANS = 1
MAX_CONCURRENT_SCANS = 8
# 单个 agent 的回合上限。strix 自己的默认值是 500；给它一个上限是因为
# "无限回合"等于"费用不可控"，而这是**会对真实目标发包**的进程。
MIN_SCAN_MAX_TURNS = 10
MAX_SCAN_MAX_TURNS = 2000


@dataclass(frozen=True, slots=True)
class ExecutionSettings:
    """当前**实际生效**的执行参数（DB 覆盖后的结果）。

    单独一个类型而不是直接返回 `Settings`：调用方（调度器、runner）
    只该看到"生效值"，不该知道"这个值是从 DB 还是 env 来的"。
    把来源暴露出去，迟早会有人在调用点上再判一次优先级 —— 那就是第二份口径。
    """

    max_concurrent_scans: int
    scan_max_turns: int
    scan_mode: str


def get_sandbox_config(db: Session) -> SandboxConfig:
    """读沙箱配置，没有就建一条默认的。"""
    config = db.get(SandboxConfig, SINGLETON_ID)
    if config is None:
        config = SandboxConfig(id=SINGLETON_ID)
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def read_sandbox_config(db: Session) -> SandboxConfig | None:
    """只读读沙箱配置。**没有就返回 None，不建行。**

    ★ 与 `get_sandbox_config()` 的分工要说清楚：

    * 设置页 / `PUT` 走 `get_sandbox_config()` —— 那里"读"本身就要有东西可编辑，
      建行是合理的；
    * **启动扫描**走这个 —— 它只想拿当前配额。让他因为跑了一次扫描
      就凭空建出一行配置，会让"这一行是什么时候建的、按哪版默认值建的"
      变得不可知（与 `settings/models.py` 里那条"不用数据库默认值"的
      理由是同一条）。

    没有行时返回 None，调用方据此**不下发任何配额环境变量** ——
    那正是 strix 的默认行为（docker 不限），也就是用户从未配置过的真实语义。
    """
    return db.get(SandboxConfig, SINGLETON_ID)


def effective_execution_settings(db: Session | None = None) -> ExecutionSettings:
    """算出生效的执行参数。**只读，不建行。**

    ## 为什么不建行

    调度器**每 2 秒**调一次它。为一个读操作写库既没必要，也会让
    "只是跑了一轮调度"变成一次数据库写入 —— 而那张表的 `updated_at`
    是用来回答"用户什么时候改过配置"的，不该被调度器反复刷新。

    所以这里用 `db.get()`（没有就返回 None），而不是 `get_sandbox_config()`。
    """
    env = get_settings()
    row = db.get(SandboxConfig, SINGLETON_ID) if db is not None else None

    # ⚠️ 用 `is not None` 而不是 `or`：后者会把合法的 `0` 也当成"没设"。
    #    当前 `0` 被下面的区间校验挡在外面，但那是另一处的规则 ——
    #    依赖它来保证这里的正确性，等于让两条规则互相拴死。
    concurrent = (
        row.max_concurrent_scans
        if row is not None and row.max_concurrent_scans is not None
        else env.max_concurrent_scans
    )
    max_turns = (
        row.scan_max_turns
        if row is not None and row.scan_max_turns is not None
        else env.scan_max_turns
    )
    mode = row.scan_mode if row is not None and row.scan_mode else env.scan_mode

    return ExecutionSettings(
        max_concurrent_scans=concurrent,
        scan_max_turns=max_turns,
        scan_mode=mode,
    )


def validate_execution_settings(
    *,
    max_concurrent_scans: int,
    scan_max_turns: int,
    scan_mode: str,
) -> None:
    """校验执行参数。不合法就抛 422，**不静默截断**。

    不截断的理由：把用户填的 `999` 悄悄改成 `8` 并保存成功，他下一次
    看到的是一个自己没设过的值 —— 而"我明明填了 999"这件事没有任何痕迹。
    宁可当次报错说清范围。
    """
    if not (MIN_CONCURRENT_SCANS <= max_concurrent_scans <= MAX_CONCURRENT_SCANS):
        raise ApiError.validation(
            f"同时运行的扫描数应在 {MIN_CONCURRENT_SCANS}-{MAX_CONCURRENT_SCANS} 之间",
            {
                "maxConcurrentScans": (
                    f"应在 {MIN_CONCURRENT_SCANS}-{MAX_CONCURRENT_SCANS} 之间，"
                    f"收到 {max_concurrent_scans}"
                )
            },
        )
    if not (MIN_SCAN_MAX_TURNS <= scan_max_turns <= MAX_SCAN_MAX_TURNS):
        raise ApiError.validation(
            f"回合上限应在 {MIN_SCAN_MAX_TURNS}-{MAX_SCAN_MAX_TURNS} 之间",
            {
                "scanMaxTurns": (
                    f"应在 {MIN_SCAN_MAX_TURNS}-{MAX_SCAN_MAX_TURNS} 之间，"
                    f"收到 {scan_max_turns}"
                )
            },
        )
    if scan_mode not in values(ScanMode):
        raise ApiError.validation(
            f"扫描深度取值不合法：{scan_mode!r}",
            {"scanMode": f"应为 {'、'.join(values(ScanMode))} 之一"},
        )


# ══════════════════════════════════════════════════════════
# CTF2 解题策略（v0.20）
# ══════════════════════════════════════════════════════════
#
# ⚠️ 这里**只放读取**。写入（`update_ctf2_settings`）在 `app/ctf2/service.py` ——
#    因为校验规则（"什么算合法的提交策略"）是 CTF2 的领域知识，
#    放进 settings 会让这个模块开始理解 CTF2 的语义。
#
# 而这个读函数必须在这里，因为设置页装配时要读它，而 settings
# 不该 import ctf2 的业务模块（那会形成一个反向依赖）。


def read_ctf2_config(db: Session) -> Ctf2Config | None:
    """读 CTF2 配置行。**没有就返回 None，不建行。**

    与 `read_sandbox_config` 同一个模式：为一个读操作写库既没必要，
    也会让 `updated_at`（回答"用户什么时候改过"）被反复刷新。
    没有行时调用方用代码常量兜底（见 `ctf2.service.effective_ctf2_settings`）。
    """
    return db.get(Ctf2Config, SINGLETON_ID)
