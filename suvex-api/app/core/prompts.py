"""提示词文件里那些**被两条路径共用**的部分。

## 为什么需要这个模块（而不是各自读各自的）

`prompts/` 下的文件绝大多数只有一个读者：

| 文件 | 唯一的读者 |
| ---- | ---------- |
| `ctf2_system.md` | `app/kernel/runner.py` |
| `ctf2_agentsec.md` | `app/kernel/runner.py` |
| `ctf2_ir.md` | `app/kernel/runner.py` |
| `platform_spec.md` | `app/kernel/adapters/builder.py` |

所以它们的路径常量就写在各读者自己文件里，没必要集中。

`offensive_playbook.md` 是**第一个例外**：解题（`app/kernel/runner.py`）
与渗透扫描（`app/scans/runner.py`）两条路径都要追加它。而这两个模块
**互相不能顶层 import** —— 它们已经在多个地方用函数内 import 互相引用
（`app/kernel/runner.py` 用 `app.scans.runner._base_env`，
`app/scans/runner.py` 用 `app.kernel.models.Ctf2Challenge`），
再加一条顶层边就会撞成环。

## 所以路径常量放这里，而不是复制两份

复制两份的代价不是"多写一行"，而是**改文件名时要记得改两处**。
本项目反复记录过这一类失效的形态：两处口径分叉之后，改了一处的那次
提交看起来完全正常，症状要等到有人真的打开开关才出现。
`app/core/` 是两条业务路径都能依赖、且不产生兄弟耦合的唯一位置。

## 为什么不在这里抛领域异常

这个模块只负责"把文件读成字符串"。读不到时它让 `OSError` 直接冒出来，
由各自调用方翻成自己的错误类型（`Ctf2Error` / `RunnerUnavailable`）——
那些异常的措辞要贴合各自的失效后果，在这里统一反而说不准。
"""

from __future__ import annotations

from app.core.config import PROJECT_ROOT

PROMPTS_DIR = PROJECT_ROOT / "prompts"

#: 越权与注入手法块（v0.42）。**解题与渗透扫描共用同一份。**
#:
#: ## ⚠️ 它不叫 `ctf2_*.md`，这不是笔误
#:
#: 同目录其它文件都带 `ctf2_` 前缀，因为它们只服务解题。这一份同时服务
#: `app/scans/runner.py`，挂 `ctf2_` 前缀会让下一个人在扫描侧看到它时
#: 以为拿错了文件，进而"顺手"复制一份到 `scans/` 下 —— 那就正好制造出
#: 两份会各自漂移的手法矩阵。
OFFENSIVE_PLAYBOOK_PATH = PROMPTS_DIR / "offensive_playbook.md"


def load_offensive_playbook() -> str:
    """读手法块正文。

    ⚠️ 读不到时**不兜底成空串**，让 `OSError` 冒出来。

    与 `ctf2/runner.py` 里那几个 `load_*_prompt` 同一条纪律：兜底成空串
    会让"文件没进部署包"完全无声 —— 开关打开了、设置页显示已启用、
    agent 拿到的却还是原提示词，而产出质量的差别要人去比对才能发现。
    """
    return OFFENSIVE_PLAYBOOK_PATH.read_text(encoding="utf-8")


__all__ = [
    "OFFENSIVE_PLAYBOOK_PATH",
    "PROMPTS_DIR",
    "load_offensive_playbook",
]
