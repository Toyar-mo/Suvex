"""CTF2 借来的壳不能被当成扫描操作 —— 以及 2 号退出码的二义性。

## 这次修的是什么（2026-10-08 用户实测）

用户对扫描列表里那条 `[CTF2] WEB5[中等]` 点了「重跑」，然后问
「点了重跑为什么没有开始解」。完整链条：

    rerun_scan 忠实复制 target `ctf2:1cbe06be-…`
    → strix 收到后当场拒绝（Invalid target）
    → argparse 用法错误，退出码 2
    → 而 EXIT_FINDINGS **也是 2**，被记成「已完成 · 0 秒」
    → CTF2 侧零动作，题目状态一个字没变

他点了两次，于是有两条这样的假记录。三处证据（全部实测）：

| 证据 | 值 |
| --- | --- |
| `authorized_by` | `user_01M39V17…`（**不是** `ctf2`）→ 不是 CTF2 建的 |
| `run_dir/scan.log` | `strix: error: Invalid target 'ctf2:…'` |
| `exit_code` | `2` → 被记成 `completed` |

本文件测的四个修复：

1. `scans` 侧拒绝在 CTF2 的壳上重跑 / 续跑 / 终止（`require_launchable_scan`）；
2. `exit_code == 2` 必须**同时**满足"strix 真的跑过"才算成功；
3. `did_strix_run` 的判据本身（`strix_runs/` 存在性）；
4. 扫描并发名额不把 CTF2 的壳算进来（`_not_ctf2_shell`）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.core.errors import ErrorCode
from app.core.timeutil import now_utc
from app.core.vocab import SCAN_ORIGIN_CTF2
from app.scans import runner, scheduler, service
from app.scans.models import Scan

# Markdown 加粗语法：`**某段话**`。中间不许跨行，也不许吞掉空字符串
# （`****` 不是加粗，是四个星号）。
_MARKDOWN_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")


def _shell(db: Session, *, tmp_path: Path, sname: str = "scan_shell") -> Scan:
    """造一条 CTF2 借来挂事件流的壳（`origin='ctf2'`）。

    关键特征照抄生产里那两条：`target` 是 `ctf2:<remote_id>`、
    状态 `running`、`authorized_by` 是**用户 id**（重跑那两条就是这样）。
    """
    from app.core.ids import new_id
    from app.core.timeutil import now_utc

    run_dir = tmp_path / sname
    run_dir.mkdir(parents=True, exist_ok=True)
    scan = Scan(
        id=new_id("scan"),
        name="[CTF2] WEB5[中等]",
        target="ctf2:1cbe06be-3fa2-4a0f-a298-75f8e2172538",
        target_kind="service",
        status="running",
        skills=[],
        goal="captureFlag",
        provider_id="prov_x",
        started_at=now_utc(),
        authorized_by="user_01M39V17Z1FCWVTDR3KW9ABYQX",
        origin=SCAN_ORIGIN_CTF2,
        run_dir=str(run_dir),
    )
    db.add(scan)
    db.commit()
    return scan


# ══════════════════════════════════════════════════════════
# 1. 三个入口拒绝 CTF2 的壳
# ══════════════════════════════════════════════════════════


def test_rerun_refuses_ctf2_shell(db_session: Session, tmp_path: Path) -> None:
    """★★ 重跑 CTF2 的壳必须**明确拒绝**，而不是"跑一次然后假成功"。

    这是那次事故的**入口**：`rerun_scan` 忠实复制了 target，
    然后 strix 拒收它、argparse 退 2、被记成「已完成」。

    ⚠️ 拒绝信息里必须有**替代动作**（去 CTF2 页面点解题）——
    只说"不能这么做"会让用户站在一个刚刚还能点的按钮前面不知所措。
    """
    from app.core.errors import ApiError

    shell = _shell(db_session, tmp_path=tmp_path)
    with pytest.raises(ApiError) as exc:
        service.rerun_scan(
            db_session,
            shell.id,
            _AuthPayload(),
            identity=_identity(),
            ip="10.0.0.1",
            user_agent="pytest",
        )
    assert exc.value.code == ErrorCode.SCAN_IS_CTF2_SHELL
    # 替代动作必须出现在消息里 —— 否则这句话不可行动
    assert "CTF2" in exc.value.message


def test_resume_refuses_ctf2_shell(db_session: Session, tmp_path: Path) -> None:
    """续跑同样要拦。

    它的"上次会话"**根本不存在**（strix 从没跑过），而这条路径最终会
    走到 `can_resume` 报一句"会话文件已不在" —— 那句**与事实相反**
    （事实是压根没跑过）。
    """
    from app.core.errors import ApiError

    shell = _shell(db_session, tmp_path=tmp_path, sname="scan_shell2")
    # 让"状态可续"这一关先过掉，逼出壳判定。
    #
    # ⚠️ `finished_at` 必须一起写：`scans` 上有 CHECK
    #   `ck_scans_finished_at_matches_terminal_status` —— 终态与
    #   "结束时间非空"必须同时成立。只改 status 会被数据库当场拒掉
    #   （我第一次就漏了它，测试以 CheckViolation 红）。
    shell.status = "failed"
    shell.finished_at = now_utc()
    db_session.commit()

    with pytest.raises(ApiError) as exc:
        service.resume_scan(
            db_session,
            shell.id,
            _AuthPayload(),
            identity=_identity(),
            ip="10.0.0.1",
            user_agent="pytest",
        )
    assert exc.value.code == ErrorCode.SCAN_IS_CTF2_SHELL


def test_abort_refuses_ctf2_shell(db_session: Session, tmp_path: Path) -> None:
    """终止也要拦 —— 否则会产生**与事实相反**的一对记录。

    扫描侧写 `aborted` + "已由用户终止"，而 CTF2 的 worker **还在跑**
    （它是线程，杀不掉）。同一个东西在两个页面上有两种说法。
    """
    from app.core.errors import ApiError

    shell = _shell(db_session, tmp_path=tmp_path, sname="scan_shell3")
    with pytest.raises(ApiError) as exc:
        service.abort_scan(db_session, shell.id)
    assert exc.value.code == ErrorCode.SCAN_IS_CTF2_SHELL


def test_normal_scan_is_still_rerunnable(
    db_session: Session, tmp_path: Path, provider_id: str
) -> None:
    """★ 反向：**正常扫描一个字都不能被影响**。

    这道断言是防"守卫写宽了"的 —— 一个把所有扫描都拒掉的实现
    能让上面三条全绿，而系统彻底废掉。

    ⚠️ 用真实的 `provider_id` fixture（走 HTTP 建一个默认 provider）：
    `rerun_scan` 会调 `_require_provider`，而那个检查是**真的查库**。
    我自己编的 `prov_x` 会被它正确地拒掉 —— 第一次跑时就是这条红。
    """
    shell = _shell(db_session, tmp_path=tmp_path, sname="scan_normal")
    shell.origin = None
    shell.target = "https://example.com"
    shell.authorized_by = "user_x"
    shell.provider_id = provider_id
    db_session.commit()

    # 不抛错就是通过 —— 它会真的建出一条新记录
    created = service.rerun_scan(
        db_session,
        shell.id,
        _AuthPayload(),
        identity=_identity(),
        ip="10.0.0.1",
        user_agent="pytest",
    )
    assert created.id != shell.id
    assert created.origin is None


# ══════════════════════════════════════════════════════════
# 2. did_strix_run 的判据
# ══════════════════════════════════════════════════════════


def test_did_strix_run_false_without_strix_runs(tmp_path: Path) -> None:
    """没有 `strix_runs/` = strix 没启动过。

    这正是那次假完成的形态：run_dir 里只有 `instruction.txt` 与
    1003 字节的 `scan.log`（内容就是 argparse 的用法错误）。
    """
    run_dir = tmp_path / "no_runs"
    run_dir.mkdir()
    (run_dir / "scan.log").write_text("usage: strix ...\n", encoding="utf-8")
    assert runner.did_strix_run(str(run_dir)) is False


def test_did_strix_run_true_with_strix_runs(tmp_path: Path) -> None:
    """有 `strix_runs/<run>/` = 真跑过（那目录是 strix 自己建的）。"""
    run_dir = tmp_path / "has_runs"
    (run_dir / "strix_runs" / "some-run_b59b").mkdir(parents=True)
    assert runner.did_strix_run(str(run_dir)) is True


def test_did_strix_run_handles_missing_dir() -> None:
    """路径为空或不存在都返回 False —— **不抛异常**。

    它跑在调度器的收尾路径上，那里任何异常都会让一条扫描永远停在
    `running`。而这个方向的误判（当真没跑过）是安全的：
    结果是 failed，用户会去看一眼。
    """
    assert runner.did_strix_run(None) is False
    assert runner.did_strix_run("") is False
    assert runner.did_strix_run("/nonexistent/path/xyz") is False


# ══════════════════════════════════════════════════════════
# 3. 退出码 2 的二义性（★ 本次最核心的修复）
# ══════════════════════════════════════════════════════════


def _finalize(db: Session, scan: Scan, *, exit_code: int | None, strix_ran: bool):
    """把一条 running 扫描交给 `finalize`，返回它的最终状态。"""
    service.finalize(db, scan, exit_code=exit_code, strix_ran=strix_ran)
    db.refresh(scan)
    return scan.status


def _running_scan(db: Session, tmp_path: Path, *, sname: str = "sc") -> Scan:
    from app.core.ids import new_id
    from app.core.timeutil import now_utc

    scan = Scan(
        id=new_id("scan"),
        name="t",
        target="https://example.com",
        target_kind="url",
        status="running",
        skills=[],
        goal="findVulns",
        provider_id="prov_x",
        started_at=now_utc(),
        authorized_by="user_x",
        run_dir=str(tmp_path / sname),
    )
    db.add(scan)
    db.commit()
    return scan


def test_exit_code_2_without_run_is_failed(db_session: Session, tmp_path: Path) -> None:
    """★★ **本次事故的回归测试**：退出码 2 + 没跑过 = 失败。

    这条断言就是"点了重跑却什么都没发生，界面还说已完成"那个 bug 的
    反向锁。它必须红着才能说明修复有效 —— 修复前这里是 `completed`。
    """
    scan = _running_scan(db_session, tmp_path)
    assert scan.exit_code is None
    status = _finalize(db_session, scan, exit_code=2, strix_ran=False)
    assert status == "failed", "argparse 的用法错误被谎报成了成功"
    assert scan.exit_code == 2
    # 失败原因必须**可行动**：说清"没对目标做任何事"，而不是贴一屏参数表
    assert scan.failure_reason is not None
    assert "没能启动" in scan.failure_reason
    assert "没有对目标做任何事" in scan.failure_reason


def test_failure_reason_has_no_markdown_emphasis(db_session: Session, tmp_path: Path) -> None:
    """★ 纯文本通道里不能出现 Markdown 加粗语法。

    ## 为什么需要这条

    `failure_reason` 有四个消费方，**四个都不渲染 Markdown**：
      ① 扫描列表 `ScanList.tsx:866` —— `<p>{scan.failureReason}</p>`
      ② 事件流失败事件的「错误」字段 —— `EventRow.tsx:391` 用 `CodeBlock`（`<pre>`）
      ③ CSV 导出 `scan-export.ts:47` —— 直接进单元格
      ④ `GET /scans/{id}` 响应体 —— 任何外部调用方

    曾经这里写的是两个字面星号包住「没有对目标做任何事」，用户看到的是：
    「这次扫描**没有对目标做任何事**。」—— 强调没起作用，反而像排版事故。

    ## 为什么这条容易复发

    写这段文案的人（和读这段代码的人）看的是**源码**，源码里星号确实起到了
    强调作用 —— 因为编辑器注释和 IDE 提示会渲染它。而**用户看的是渲染结果**，
    那条链路上没有任何一层做 Markdown 解析。

    也就是说：**写的时候有效果，跑起来没有** —— 这正是最难自查的一类缺陷。
    同类风险：`**` `__` `` ` `` `#` 开头的行（会被当成标题）、`- ` 开头（列表）。

    断言范围刻意只要「不存在未被解释的 `**` 成对出现」，避免误伤
    日志正文里本来就有的星号（比如 glob 模式 `**/*.py`）。
    """
    scan = _running_scan(db_session, tmp_path)
    _finalize(db_session, scan, exit_code=2, strix_ran=False)
    reason = scan.failure_reason
    assert reason is not None

    # 形如 `**某段话**` 的成对星号 = Markdown 加粗语法，纯文本通道里不该出现。
    unexpected = _MARKDOWN_BOLD.findall(reason)
    assert not unexpected, (
        f"失败原因里出现了 Markdown 加粗语法 {unexpected!r}，"
        f"但 failure_reason 的四个消费方都不渲染 Markdown —— "
        f"用户会看到字面星号。要强调请换措辞，别借排版语法。"
    )


def test_exit_code_2_with_run_is_completed(db_session: Session, tmp_path: Path) -> None:
    """★ 反向：真跑过 + 退出码 2 = 成功（发现了漏洞）。

    与上一条配对，防"把所有 2 都判成失败" —— 那会让每一次真的发现了
    漏洞的扫描都显示成失败，是比原 bug 更糟的倒退。
    """
    scan = _running_scan(db_session, tmp_path, sname="sc2")
    status = _finalize(db_session, scan, exit_code=2, strix_ran=True)
    assert status == "completed"


def test_exit_code_0_is_completed_regardless(db_session: Session, tmp_path: Path) -> None:
    """退出码 0 就是成功，与 `strix_ran` 无关 —— 它的语义没有二义性。"""
    scan = _running_scan(db_session, tmp_path, sname="sc3")
    assert _finalize(db_session, scan, exit_code=0, strix_ran=False) == "completed"


def test_other_exit_codes_are_failed(db_session: Session, tmp_path: Path) -> None:
    """1（致命错误）等其它码仍然是失败 —— 本次改动不该碰到它们。"""
    scan = _running_scan(db_session, tmp_path, sname="sc4")
    assert _finalize(db_session, scan, exit_code=1, strix_ran=True) == "failed"


def test_argparse_error_is_extracted_from_log(db_session: Session, tmp_path: Path) -> None:
    """★ 用法错误那句话要被提到失败原因的最前面。

    那种日志只有 1003 字节、主体是一屏 `usage:` 参数表 ——
    原样贴上去会把**真正要做的事**（改 target）淹掉。
    """
    scan = _running_scan(db_session, tmp_path, sname="sc5")
    run_dir = Path(scan.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "scan.log").write_text(
        "usage: strix [-h] [-v] [--update] [-t TARGET]\n"
        "strix: error: Invalid target 'ctf2:1cbe06be-…': Invalid target\n"
        "Target must be one of:\n- A valid URL (http:// or https://)\n",
        encoding="utf-8",
    )
    scan.log_path = str(run_dir / "scan.log")
    db_session.commit()

    _finalize(db_session, scan, exit_code=2, strix_ran=False)
    db_session.refresh(scan)
    reason = scan.failure_reason or ""
    # 人话在最前，且带上了 strix 自己那句原文
    assert reason.startswith("strix 没能启动")
    assert "Invalid target" in reason


# ══════════════════════════════════════════════════════════
# 4. 并发名额不把 CTF2 的壳算进来
# ══════════════════════════════════════════════════════════


def test_not_ctf2_shell_excludes_shells_only(db_session: Session, tmp_path: Path) -> None:
    """`_not_ctf2_shell()` 只排除 `origin='ctf2'`，不放走别人。

    ★ 为什么它重要：扫描调度器数并发名额用的是
    `COUNT(Scan WHERE status='running')`。CTF2 每解一道题都借一条
    `running` 的 scan，于是**每解一道题，扫描就少一个名额** ——
    那道题短则几十秒、长则几十分钟。设成 3 时一道题在解就只剩 2 个
    扫描位，而界面上没有任何地方解释这件事。

    判据必须用 `origin` 而不是"查关联表"：重跑建出的那条新壳
    **没有 challenge 指向它**（那正是本次事故的产生方式）。
    """
    from sqlalchemy import func, select

    shell = _shell(db_session, tmp_path=tmp_path, sname="q_shell")
    normal = _running_scan(db_session, tmp_path, sname="q_normal")

    counted = int(
        db_session.execute(
            select(func.count())
            .select_from(Scan)
            .where(Scan.status == "running")
            .where(scheduler._not_ctf2_shell())
        ).scalar_one()
    )
    assert counted == 1
    total = int(
        db_session.execute(
            select(func.count()).select_from(Scan).where(Scan.status == "running")
        ).scalar_one()
    )
    assert total == 2, "前置数据没造对：应该有两条 running"
    assert shell.id != normal.id


# ── 小工具：授权载荷与身份 ──────────────────────────────
#
# `rerun_scan` / `resume_scan` 都要求授权确认（`check_authorization`），
# 所以测试必须送一份**逐字正确**的声明。措辞从配置里读，不硬编码 ——
# 硬编码的话改配置会让这些测试莫名其妙地红。


class _Auth:
    """授权声明的替身。

    ⚠️ 字段名是 **`confirmedAt`**（camelCase），照抄 API 的入参形状 ——
    `check_authorization` 直接把 `auth.confirmedAt` 写进留痕列。
    我第一次只写了 `confirmed`，于是以 `AttributeError` 红。
    """

    def __init__(self, statement: str) -> None:
        self.confirmed = True
        self.statement = statement
        self.confirmedAt = now_utc()


class _AuthPayload:
    def __init__(self, *, name: str | None = None) -> None:
        from app.core.config import get_settings

        self.name = name
        self.target = None
        self.authorization = _Auth(get_settings().scan_authorization_statement)


def _identity():
    """一个管理员会话身份。

    走**真实的** `Identity`（`app.auth.deps`）而不是打桩：
    `rerun_scan` 会读 `identity.user_id` 写进留痕列，
    而"哪个字段是 id"这件事正是我在本文件里踩过的坑
    （它叫 `user_id`，不是 `id`，且 `kind` / `label` / `scopes` 是必填）。
    """
    from app.auth.deps import Identity

    return Identity(
        kind="session",
        label="session:test",
        scopes=frozenset({"read", "scan", "admin"}),
        user_id="user_test_admin",
    )
