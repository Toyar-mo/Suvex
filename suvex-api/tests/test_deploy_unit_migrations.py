"""systemd unit 的启动命令守卫：迁移必须真的在 ExecStart 里。

## 这个测试在防什么

`deploy/suvex-api.service:54` 曾写着「迁移在启动命令里（`alembic upgrade
head &&`），不在应用代码里」，而 `ExecStart` 里**只有 uvicorn**。那句注释
从 `8537e5b` 建立 unit 时就是错的，但没人发现，因为它读起来完全合理 ——
注释说了一件本该做的事，而代码没做。

后果不是"少跑一步"：

  · 新迁移静默不生效，服务照常启动、`is-active` 一直是绿的；
  · 旧表能读能写，所以冒烟测试全过；
  · 只有新功能第一次碰到新表时才炸，且报错是
    `relation "scan_schedules" does not exist` —— 看起来像 ORM 写错了。
  · 在 v0.85 上线后，调度器每 15 秒 tick 一次就抛一次，连刷一整天。

这类"注释与代码不一致"没法靠 review 抓（review 时读注释会以为它成立），
只能靠断言。所以这里直接解析 unit 文件，要求：

  1. `ExecStart` 里出现 `alembic` 与 `upgrade head`；
  2. `ExecStart` 里出现 `uvicorn`（防止有人把启动命令整个写错）；
  3. 用的连接符是 `&&` 而不是 `;` —— 迁移失败就不该起服务。

## 为什么不测"运行时真的迁移了"

那需要 systemd + postgres + 真实 unit，属于部署环境的事，单测跑不了。
这个测试守的是**静态契约**：配置文件里写了什么。运行时的正确性由
`install.sh --dry-run` 与真机验收覆盖。
"""

from __future__ import annotations

import re
from pathlib import Path

UNIT = Path(__file__).resolve().parents[1] / "deploy" / "suvex-api.service"


def _exec_start_text() -> str:
    """把 `ExecStart=` 到下一个「顶格 key=value」之间的内容拼成一行。

    systemd 的续行是行尾 `\\`，但真实文件里是 `ExecStart=/bin/sh -c '\\`
    这种形式 —— 单引号里还有多行。所以不能只按反斜杠续行解析，要按
    「顶格的非注释行开始新的指令」来切。
    """
    lines = UNIT.read_text(encoding="utf-8").splitlines()
    collected: list[str] = []
    in_exec = False
    for raw in lines:
        stripped = raw.strip()
        if not in_exec:
            if stripped.startswith("ExecStart="):
                in_exec = True
                collected.append(stripped[len("ExecStart=") :])
            continue
        # 已经开始收集：遇到顶格的 `Key=`（且不是注释/空行）就结束
        if raw and not raw[0].isspace() and "=" in raw and not stripped.startswith("#"):
            break
        collected.append(raw)
    return "\n".join(collected)


def test_exec_start_runs_migrations() -> None:
    """★ ExecStart 里必须有 alembic upgrade head。"""
    text = _exec_start_text()
    assert text, "没找到 ExecStart 指令 —— unit 文件结构变了？"
    assert "alembic" in text, (
        "ExecStart 里没有 alembic —— 迁移不会随服务执行。"
        "这正是 2026-10-25 修掉的那个缺陷（注释说有、代码没有）。"
    )
    assert "upgrade" in text and "head" in text, "ExecStart 里的 alembic 没到 upgrade head"


def test_exec_start_still_starts_uvicorn() -> None:
    """迁移之外，服务本体还得起得来 —— 防止补迁移时把启动命令改坏。"""
    text = _exec_start_text()
    assert "uvicorn" in text, "ExecStart 里没有 uvicorn —— 服务不会启动"
    assert "app.main:app" in text, "ExecStart 里的 uvicorn 没指向 app.main:app"


def test_migration_failure_prevents_start() -> None:
    """用 `&&` 连接，而不是 `;` —— 迁移失败就不该起服务。"""
    text = _exec_start_text()
    assert "alembic" in text
    # 找出 alembic 与 uvicorn 之间的连接符
    m = re.search(r"alembic.*?(&&|;)\s*.*?uvicorn", text, re.DOTALL)
    assert m, "没找到 alembic 与 uvicorn 的连接处"
    assert m.group(1) == "&&", (
        f"alembic 与 uvicorn 之间用的是 {m.group(1)!r} —— 迁移失败时服务仍会启动，"
        "半套表结构会在第一个业务请求上以「代码 bug」的样子炸掉。"
    )


def test_unit_uses_absolute_python_for_alembic() -> None:
    """用 venv 里的绝对 python 跑 alembic，不依赖 PATH。

    `install.sh` 用 `sudo -u suvex` 装 venv，服务的 PATH 里没有 `.venv/bin`，
    所以裸写 `alembic` 会在 systemd 下变成 `command not found`。
    """
    text = _exec_start_text()
    assert ".venv/bin/python" in text or ".venv/bin/alembic" in text, (
        "ExecStart 里调用 alembic 没有用 venv 里的绝对路径 —— "
        "systemd 的 PATH 不含 .venv/bin，会报 command not found"
    )
