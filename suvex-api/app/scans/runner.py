"""扫描执行后端：怎么把一次扫描真的跑起来。

## 为什么要有这一层抽象

三个理由，都不是"为了好看"：

1. **测试与 CI 绝不能跑真实渗透测试** —— 它们会向真实目标发包，那是违法的。
   所以必须有一个可替换的执行后端，而"可替换"这件事本身
   也让 strix 的升级不会绑死整个 API。
2. strix 是一个**外部 CLI**，它的参数、退出码、产物目录都是它的契约。
   把它们收在一个文件里，上游改了只改这里。
3. 进程管理（spawn / 存活判定 / 杀进程树）是**危险且容易写错**的一段，
   集中在一处才审得过来。

## 命令怎么拼（依据 docs.strix.ai 的 CLI Reference）

```
strix (--target <target> | --target-list <path>) [options]
```

| 我们的概念 | strix 侧 |
| ---------- | -------- |
| `Scan.target` | `--target` |
| `Scan.skills` | **没有对应参数** → 拼进 `--instruction` 的自然语言里 |
| 扫描深度 | `--scan-mode quick\\|standard\\|deep` |
| 回合上限 | `--max-turns`（默认 500，与 strix 一致） |
| 费用上限 | `--max-budget`（可选） |
| 无人值守 | `--non-interactive`（**必须**，否则退出码语义不同） |
| LLM 与凭据 | **环境变量** `STRIX_LLM` / `LLM_API_KEY` / `LLM_API_BASE` |

### ★ 凭据只走环境变量，绝不进 argv、也不写配置文件

两个都是刻意的：

* **不进 argv**：命令行参数在进程列表里对同机其他用户**可见**
  （`ps aux`、Windows 的任务管理器都能看到）。一个 API key 出现在那里
  等于泄漏给整台机器。
* **不写 `--config` 文件**：strix 支持 `--config /path/to/config.json`，
  而那个文件里要放 `LLM_API_KEY` —— 那就成了**磁盘上的第二份明文凭据**，
  与任务 2 的「密钥全链路不出现明文落盘」直接冲突。

环境变量是本进程私有的（Linux 上 `/proc/<pid>/environ` 只有同用户可读），
是这个场景下唯一合适的位置。

## 退出码（strix 的契约）

| 码 | 含义 |
| -- | ---- |
| 0  | 完成。（无人值守模式下 0 表示**没发现漏洞**） |
| 1  | 致命错误（含 **Docker 不可用**、缺环境变量、配置非法） |
| 2  | 发现了漏洞（仅无人值守模式） |

⚠️ **`2` 是成功，不是失败。** 把"发现了漏洞"当成执行失败，
是这一层最容易写错的一处：扫描确实跑完了，只是结果不干净。

## ⚠️⚠️ 但 `2` 不是 strix 独占的 —— argparse **用法错误也是 2**

这条是 v0.27 实测撞出来的，它比上面那条更容易骗人，因为它**看起来像成功**：

    strix: error: Invalid target 'ctf2:1cbe06be-…'
    usage: strix [-h] [-v] [--update] [-t TARGET] …

`argparse` 在用法错误时固定 `sys.exit(2)`，而 `EXIT_FINDINGS` **也是 2**。
于是 `service.finalize` 的 `exit_code in (EXIT_OK, 2)` 把一条**什么都没干**
的记录记成「已完成 · 0 秒」—— 用户看到成功，而目标一个字没被碰过。

**靠退出码本身分不开这两个语义**（同一个数字，同一个进程）。
所以判据必须来自**另一个维度**：`strix_runs/` 存不存在。

    `strix_runs/<run>/` 是 strix **真正跑起来之后**才建的目录
    （`detect_run_id` 读的正是它）。所以：

        没有 strix_runs  ⟺  strix 根本没开始跑  ⟹  那不可能是"发现了漏洞"

这条判据在真实数据上验过（2026-10-08，Kali 生产库，9 条 `exit_code=2`）：

| 组 | 条数 | 有 `strix_runs` |
| -- | ---- | --------------- |
| 耗时 ≤5 秒（CLI 用法错误） | 2 | **0** |
| 耗时 >5 秒（真跑过） | 7 | **7**（全部） |

两组完全分离，没有一条落在中间。判据见 `did_strix_run`。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.core.config import get_settings
from app.core.prompts import load_offensive_playbook
from app.core.vocab import SkillId

logger = logging.getLogger("suvex.scans")


def did_strix_run(run_dir: str | None) -> bool:
    """strix **真的启动过**吗 —— 靠 `strix_runs/` 目录判断。

    ★ 这解决的是退出码 `2` 的二义性（见模块 docstring 那段）：
    `2` 既可能是"发现了漏洞"，也可能是 argparse 的用法错误。

    ## 为什么用目录存在性，而不是"日志里有没有 usage 字样"

    日志匹配是**文本启发式**：strix 版本升级、报错措辞变化、或者
    agent 自己在报告里复述了一句 "usage:"，都会让它误判。
    而 `strix_runs/` 是 strix **自己**在真的进入工作流程后建的产物目录 ——
    它是**结构性事实**，不随文案变化。

    ⚠️ 判 `False` 只说明"没跑起来"，**不说明原因**。原因是另一件事
    （日志里有 argparse 那句），调用方要把两件事分开对待：
    前者决定终态，后者只用来写说明。

    ## 参数是 `str` 而不是 `Path`

    调用方（`scheduler._reap_finished`）手上是从数据库读出来的字符串，
    且它必须在**开写事务之前**调完（见 `_flag_of` 里那段关于
    `DeadlockDetected` 的说明）。让它自己 `Path(...)` 转换只会把
    "可能是 None"这个分支散到两处。
    """
    if not run_dir:
        return False
    try:
        return (Path(run_dir) / "strix_runs").is_dir()
    except OSError:
        # 路径非法 / 权限不足（它可能是 root 建的目录）—— 当成"没跑过"。
        # 这个方向的误判是**安全**的：它会把一条可疑记录记成 failed
        # 而不是 completed，而 failed 会让用户去看一眼。
        logger.warning("检查 strix_runs 失败，按未启动处理：%s", run_dir, exc_info=True)
        return False


def did_agent_run(run_dir: str | None) -> bool:
    """自研引擎**真的启动过**吗 —— 靠 `agent_run/run.json` 判断。

    与 `did_strix_run` 同构、同一个目的（消解退出码的二义性），只是
    判据换成自研引擎自己写的运行标识：

    - `agent_run/run.json` 由 `agent_runner._write_run_json` 在引擎
      **跑完一轮之后**写（无论成功失败都写，含 `tool_calls` / `turns`）。
    - 所以"文件在" = 引擎确实进了 solve 循环；"文件不在" = 它压根没起来
      （构造 SolveSpec 就失败、或容器没起来）。

    ## 为什么用 `run.json` 而不是"日志有没有错误字样"

    同 `did_strix_run`：文本启发式会随文案变化误判，而 `run.json` 是
    结构性事实。⚠️ 判 `False` 同样**只说明没跑起来，不说明原因**。
    """
    if not run_dir:
        return False
    try:
        return (Path(run_dir) / "agent_run" / "run.json").is_file()
    except OSError:
        logger.warning("检查 agent_run/run.json 失败，按未启动处理：%s", run_dir, exc_info=True)
        return False


def did_backend_run(run_dir: str | None, *, runner_name: str | None) -> bool:
    """按后端分派"真的启动过吗"。

    两个后端的判据不同（`strix_runs/` vs `agent_run/run.json`），
    调用方（`scheduler`）手上只有 `scan.runner`（启动时写入的后端名），
    所以在这里按名字分派，而不是让调度器自己 switch。

    认不出的后端名（`None` / `fake` / 未来新增）一律**按 True** 处理 ——
    这不是"放行"，而是"不拿一个我不认识的判据去否定一次记录"：
    `strix_ran` 这个参数本来的作用是**纠一条具体的谎报**（argparse 用法
    错误被当成发现了漏洞），对未知后端没有那个二义性，默认信任退出码。
    """
    if runner_name == "strix":
        return did_strix_run(run_dir)
    if runner_name == "agent":
        return did_agent_run(run_dir)
    return True

# 退出码的语义常量。**不要**在别处硬编码 2。
EXIT_OK = 0
EXIT_FATAL = 1
EXIT_FINDINGS = 2

# 技能 → 给 agent 的自然语言侧重。
#
# strix 的 CLI **没有**技能参数（它的 "Skills" 是另一回事：知识包，
# 由 `~/.strix` 或镜像提供）。所以"技能范围"只能落成 instruction 里的一句话。
# 这是如实映射，不是绕过：用户选的"web / api / 容器"确实会改变 agent 的侧重，
# 只是载体是提示词而不是开关。
SKILL_FOCUS: dict[str, str] = {
    SkillId.WEB: "Web 应用层漏洞（注入、XSS、越权、SSRF）",
    SkillId.API: "API 接口（认证、授权、参数校验、批量赋值）",
    SkillId.CONTAINER: "容器与编排配置（镜像、权限、挂载、逃逸面）",
    SkillId.CLOUD: "云配置与元数据（IAM、存储桶、实例角色）",
    SkillId.DEPENDENCIES: "依赖与供应链（已知漏洞版本、锁文件）",
    SkillId.SECRETS: "硬编码凭据与密钥泄漏",
    # ★ 唯一一个**面向裸 TCP 端口**的技能（任务 20）。
    #
    # 其余六个的侧重全是"Web 应用层"或"代码/配置文件里的东西"，
    # 而裸端口上两者都不存在 —— 这也是为什么当初没有技能能挂到
    # `service` 上、导致向导第 2 步一条死路。
    #
    # 措辞刻意写"协议"而不是"漏洞利用"：这类题的第一步永远是
    # **搞清楚对面说什么协议**（很多是自定义二进制协议），
    # 而不是上来就找溢出。把侧重写偏会让 agent 空转。
    SkillId.PROTOCOL: "裸 TCP 服务的协议分析与二进制交互（协议逆向、构造输入、边界与逻辑缺陷）",
}


# ══════════════════════════════════════════════════════════
# 成功判据 → 注入提示词的那句话（任务 12 阶段 5）
# ══════════════════════════════════════════════════════════
#
# ## 为什么值得为它单写一段
#
# 判据改变的是 agent 的**取舍**，而不只是结尾的报告格式：
#
# · 例行找漏洞时，"发现更多问题"是好事。
# · 夺旗时，**拿到 flag 才算完成** —— 深度利用一个点、把 flag 读出来，
#   比广撒网报二十个低危更有价值。而默认行为会去广撒网。
# · 复测时，目标是确认**已知那几条**修没修好。若不说清，
#   agent 会顺带报一堆新问题，把用户要的那个答案淹掉。
#
# 不说这三者的区别，用户就得靠"在题面里自己写清楚"来弥补 ——
# 而那正是把结构化的东西塞回自由文本，界面也没法据此做任何呈现。
GOAL_CLAUSE: dict[str, str] = {
    "findVulns": (
        "本次测试的目标是**例行安全评估**：尽可能找出真实存在的问题、"
        "按严重度定级、并给出可执行的修复建议。"
    ),
    "captureFlag": (
        "本次测试的目标是**夺旗（CTF）**：核心判据是**拿到 flag**。"
        "请优先深挖可以通向 flag 的路径，而不是广撒网枚举低危问题；"
        "找到 flag 后，在结论里**显式写出完整的 flag 字符串**，"
        # ★ 这一行的形状是**机读约定**，不是排版要求（任务 18）。
        #
        # 起因：原来只要求"在结论里显式写出"，而结论（executive_summary）
        # 在活动流里会被截断显示 —— flag 正好落在截断点之后，
        # 于是实测出现"agent 说拿到了、用户在界面上看不到"。
        #
        # 单独一行的 `FLAG: xxx` 让平台能**确定地**提取，而不是靠猜
        # （见 `scans/flag.py`：猜不出来就留空，所以"能确定"很重要）。
        "**并单独起一行、严格写成 `FLAG: <完整flag>` 的形状** —— "
        "平台按这一行自动提取并直接显示在界面上，不这么写就会被埋在正文里。"
        "同时说明它是从哪个入口、用什么 payload 拿到的。"
        "未能拿到时，**同样单独起一行写 `FLAG: NOT_FOUND`**，"
        "并在正文里如实说明卡在哪一步、还需要什么信息，不要编造。"
        # ── 报告版式：题解，不是渗透报告（任务 19）──────────
        #
        # ★ 为什么必须在这里说：strix 的 `finish_scan` 只有四个固定字段，
        #   而它的 docstring 明确写着 "for non-technical leadership. Risk…"
        #   —— 整套是**渗透口径**。不说的话 agent 会老老实实按那个口径写，
        #   于是拿到一份"严重度 / 业务影响 / 修复建议"的渗透报告，
        #   而用户要的是解题过程。
        #
        #   平台随后会把四节**重排成题解版式**（见 `scans/writeup.py`），
        #   所以这里只需要把**内容**写对口径，标题不用管。
        "★ 这道题请写成一份**题解（Writeup）**，不是渗透测试报告。"
        "调用 `finish_scan` 时，四个字段按下面的口径写："
        "`executive_summary` = 题目信息与结论（考的是什么、最后解出来没有、flag 是什么）；"
        "`methodology` = 解题思路（怎么想到这个方向的、试过哪些、怎么排除的）；"
        "`technical_analysis` = 完整解题过程（逐步展开，带关键命令、脚本与输出）；"
        "`recommendations` = 复盘（踩过的坑、同类题的技巧）。"
        "⚠️ **不要**写严重度分级、业务影响、修复建议、CVE/CWE 编号 —— "
        "那些是渗透报告的内容，出现在题解里就是跑题。"
    ),
    "verifyFix": (
        "本次测试的目标是**复测已知问题是否已修复**。"
        "请针对任务背景里列出的那些问题逐一验证（已修复 / 仍未修复 / 无法验证），"
        "并对每条给出你实际发过的请求与观察到的响应。"
        "顺带发现的新问题请单独列在最后，不要与复测结论混在一起。"
    ),
}


# ══════════════════════════════════════════════════════════
# 探索与止损协议（任务 13）
# ══════════════════════════════════════════════════════════
#
# ## 它要解决什么
#
# 实测一次 CTF 扫描：61.5 分钟、166 次 LLM 请求。拆开时间轴后，**全时长
# 都是 LLM 等待**（工具执行几乎不耗时），而其中相当一部分是**一轮没产出
# 新信息的空转**。溯源到两处浪费：
#
#   · 子 agent 对未知的 slot 布局做**无假设的穷举**（slot 5-12），
#     等 Root 发现并叫停时，那批枚举已经烧掉了；
#   · 目标已达成后，另一个 agent 仍在同强度探索，直到 Root 主动发现。
#
# ## ★ 两处与 strix 真实机制的出入（本段是修正版，理由在此）
#
# 协议原稿要求"每完成一个独立子步骤先查收邮箱"，并假设停止指令能抢占。
# 读 `strix-agent` 1.6.2 源码后确认，这两条在**本平台的运行方式下不成立**：
#
#   1. **没有非阻塞查收工具。** 全套工具里只有 `wait_for_agents`，而它会
#      **阻塞到有消息或超时**（默认 300s）—— 没有"看一眼有没有新消息"这种
#      调用。因此"每个子步骤查收一次"落不了地：agent 能查收的时刻，实际
#      只有它主动调 `wait_for_agents` 的那一次。
#   2. **非交互运行下消息不抢占。** `core/agents.py` 里 `send()` 的
#      `stream.cancel(mode="immediate")` 被 `interrupt_on_message` 守着，
#      而它等于 `interactive`（`core/execution.py` 里 `attach_runtime`）。
#      本平台传的是 `--non-interactive`，所以**停止指令不会打断当前 turn**。
#      实测残留可以印证：那次运行的 `.state/agents.json` 里 `pending_counts`
#      显示某个子 agent 有 3 条消息**直到扫描结束都没被消费**。
#
# 所以这里不改"该做什么"，只改"靠什么做到"：**把止损前移到自检与任务书
# 上，而不是指望运行中的消息往返**。这不削弱协议 —— 它把原本依赖一个
# 不存在机制的条款，换成在这个架构里真正会生效的写法。
EXPLORATION_PROTOCOL = """## 探索与止损协议（必须遵守）

**1. 假设先行。** 对任何未知参数空间（slot 编号、偏移量、路径、字段值）做第二次尝试之前，先写出假设：我认为答案在 [范围]，依据是 [源码线索 / 协议规范 / 已知常量 / 已排除的反例]。给不出依据时，只允许 1-2 次探测性尝试，**同一循环内不得连续枚举超过 4 个值**。每次探测后自问：这次拿到新信息了吗？没有就说明假设错了，换思路，而不是继续同类尝试。

**2. 止损线（硬性）。** 同一枚举循环内连续 3 次工具调用未产出新信息（返回值类型相同、无新线索）即判定该分支已死。此时必须：停止枚举；用 coverage 记 `needs_follow_up`（不是 `ruled_out`，除非确实证伪了某个具体假设）；转向其他线索或请求支援。

**3. 查收指令的正确方式。** 本环境**没有**"顺手看一眼邮箱"的工具，且停止类消息**不会**打断你正在跑的 turn。因此不要指望中途被叫停，**止损必须由你自己的自检完成**。`wait_for_agents` 只在确实在等别的 agent 回复时才调用（它会阻塞到有消息或超时），无人可等时不要调用它。

**4. 完工即上报。** 目标达成的**那一刻**就提交发现物与报告，不要等 Root 来发现 —— 你是最先知道自己做完了的人。收到"目标已达成"类消息后，在完成当前工具调用后立即收尾：把进度写进 coverage，然后结束当前分支，不要"跑完这一段再说"。

**5. 预算自检（每 10 轮或每个新阶段开始时）。** 问三件事：过去 10 轮是否产出了新的实质信息（没有则当前方向大概率错了）；距离轮数上限还剩多少（已用 70% 就转为"整理已知信息 + 提交阶段性结论"，而不是继续同强度尝试）；是否存在可以并行却正在串行等待的部分。

**6. 证据分级。** 用 coverage 记录时：`ruled_out` 必须附具体反证（测了什么、观察到什么、为什么排除）；`needs_follow_up` 是"有可能但没验证完"的**默认选项**。拿不准时宁可标 `needs_follow_up` —— 错误分级比标注不完整损失更大。

**7. 禁止事项。** 禁止无假设依据就写循环脚本做穷举；禁止收到停止类消息后"再跑完当前这一批"；禁止完成目标后不通知其他 agent 而坐等 Root 发现；禁止把 `ruled_out` 用在"感觉应该没问题"却没有具体反证的情况。

**8. 转达义务（Root 专用）。** 上面第 1-7 条同样约束你派出的每个子 agent。派活时把这份协议的要求写进子 agent 的任务书（`create_agent` 的 `task` 参数）—— 子 agent 看不到本段指令，只有你能传导它。

**9. 报告分节不要自带标题。** 调用 `finish_scan` 时，`executive_summary` / `methodology` / `technical_analysis` / `recommendations` 四个字段**只写正文，不要再写 `# Executive Summary` 这类小节标题** —— 渲染层会自动为每个字段加上对应标题，你再写一遍就会在成品里出现**连续两行同名标题**（实测已出现：「# Executive Summary」紧接一行「# Executive Summary」）。字段内部的子标题（`##` 及以下）照常使用。"""


# ══════════════════════════════════════════════════════════
# TCP 目标：后端把类型判错了，靠这段纠正（任务 15）
# ══════════════════════════════════════════════════════════
#
# ## 为什么非有它不可
#
# 执行后端（strix 1.6.2）的 `interface/utils.py: infer_target_type` 里有一条：
#
#     if "." in target and "/" not in target and not target.startswith("."):
#         return "web_application", {"target_url": f"https://{target}"}
#
# 于是 `host:9999` 被**归类成 HTTPS 网站**。它的类型系统里根本没有
# "原始 TCP 服务"这一类，`--target` 也没有任何写法能表达出来 ——
# 所以这不是"我们的映射没做对"，而是**下游缺一个类型**。
#
# 后果不是"不精确"，是**方向性错误**：agent 会对着一个裸 TCP 端口做
# TLS/HTTP 握手，然后拿到连接重置或超时；而它读到的目标描述明明写着
# "这是一个 HTTPS 网站"。这与本项目踩过的"https/http 搞反导致 32 分钟
# 白跑"是同一类事故 —— **工具说错了话，agent 信了**。
#
# 我们改不了它的分类，只能在自己的指令里**明确否定它**。措辞上必须
# 直说"那个标注是错的"，而不能委婉地说"注意这可能是 TCP"：
# 后者会被"系统提示词优先"这条规则压过去。
TCP_TARGET_CLAUSE = """## ⚠️ 目标是**原始 TCP 服务**，不是网站

目标 `host:port` 是一个**裸 TCP 端口**（Pwn / 网络 Misc / Crypto 预言机这类题目）。

**执行后端内部会把它标注成 `web_application` 并补上 `https://` —— 那个标注是错的，请忽略它。**

具体做法：

- **不要**用浏览器、HTTP 客户端或 `https://` 去访问它。那里没有 HTTP 层，
  发起 TLS/HTTP 只会得到连接重置或超时 —— 那**不是**"目标不可达"，
  而是协议用错了，别据此下"打不通"的结论。
- 用 `nc` / `ncat` / `socat` / `openssl s_client`，或 python 的 `socket`
  **直连该 host:port**，按题目要求收发**原始字节**。
  这类协议常是二进制的：读写一律用 `bytes`，不要先 decode 成字符串。
- 判断"通了"的依据是**你收到了预期的字节**，不是 HTTP 状态码 ——
  裸服务不会给你状态码。
- 题面若给了连接命令或协议说明，以它为准。附件（二进制、源码、字典）
  在沙箱工作区里，先 `ls` 找一下。"""


def _goal_clause(goal: str) -> str:
    """取判据对应的那句话。未知取值退回默认 —— 但**不静默**。

    ⚠️ 这里的 `get(goal, 默认)` 与"静默退化"不同：schema 层（`GoalLiteral`）
    已经挡住了非法取值，所以走到默认分支只可能是**数据库里有更旧/更新的
    枚举值**（迁移期间）。那时用默认判据比让扫描起不来合理。
    """
    clause = GOAL_CLAUSE.get(goal)
    if clause is None:
        logger.warning("未知的扫描判据 %r，按例行评估处理", goal)
        return GOAL_CLAUSE["findVulns"]
    return clause


def write_instruction_file(run_dir: Path, instruction: str) -> Path:
    """把提示词写到 `<run_dir>/instruction.txt`（任务 12 阶段 3）。

    ## 为什么要落盘而不是继续拼命令行

    两个理由，第二个是安全问题：

    1. **转义**：题面里的引号、反引号、`$`、`%hhn` 在拼 argv 时会被
       shell/子进程层改写。而那是**静默改写提示词** —— 出的错很难归因。
    2. **`ps aux` 里能看到全文**。用户会在任务背景里写测试账号
       （strix 官方文档自己举的例子就是 `admin:password123`），
       拼进 argv 等于把凭据摊给同机所有用户与任何日志采集。

    官方也推荐这个用法：`--instruction-file` 的说明写着
    "Use this option when you have lengthy or complex instructions"。

    ## 权限

    目录 `0o700`、文件 `0o600`：它可能含凭据，而同机其他用户
    没有理由读它。扫描进程以同一个用户跑，读得到。

    ⚠️ Windows 上 `chmod` 基本无效（POSIX 权限位不适用），
    那里靠的是用户目录本身的 ACL —— 如实记一笔，不假装它生效了。
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "instruction.txt"
    path.write_text(instruction, encoding="utf-8")
    if os.name == "posix":
        try:
            os.chmod(run_dir, 0o700)
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover - 权限设不上不该让扫描起不来
            logger.warning("设置 instruction.txt 权限失败：%s", path, exc_info=True)
    return path


def _workspace_files(spec: LaunchSpec) -> list[tuple[Path, str]]:
    """要投进沙箱工作区的文件（任务 12 阶段 4）。

    CTF 题给的附件（二进制、源码、字典）靠 `--workspace-file` 进沙箱。
    没有它，agent 只能靠题面**描述**猜附件里有什么 —— 而很多题的核心
    恰恰在附件里。

    返回 `[(本地路径, 沙箱内相对路径)]`。目录约定见
    `scan_upload_dir`；这里只负责列出**存在**的文件（上传目录可能为空，
    或者用户没传附件）。
    """
    directory = scan_upload_dir(spec.scan_id)
    if not directory.is_dir():
        return []
    files: list[tuple[Path, str]] = []
    for path in sorted(directory.iterdir()):
        # 跳过目录与隐藏文件（`.gitkeep` 之类不该进沙箱）
        if not path.is_file() or path.name.startswith("."):
            continue
        files.append((path, f"attachments/{path.name}"))
    return files


def scan_upload_dir(scan_id: str) -> Path:
    """一次扫描的附件目录。

    ★ 与扫描产物（`scan_root/<scan_id>/`）**分开**，刻意放在
    `scan_root/_uploads/<scan_id>/`。

    ⚠️ 理由要说准确 —— 我第一版写的理由是错的。当时我写
    "`sweep_retention` 会按保留期清 `scan_root/<scan_id>`，
    混在一起会被顺手清掉"，而**实测下来 `sweep_retention` 根本不删目录**：
    它只改数据库（打 `archived_at`、删回收站里过期的行），
    磁盘上的 `_scan_runs/<scan_id>/` 一直留着。

    那真正的理由是什么？两条，都与"人的手"有关而不是自动清理：

    1. **手工清理**。磁盘涨起来时，运维的自然动作是
       `rm -rf _scan_runs/<scan_id>` 或按日期清旧目录。附件如果躺在
       那个目录里，就会连着被删 —— 而题目附件在"同一道题重跑几遍"
       时还想留着。
    2. **将来若真的加上自动清理**，那一步是按"扫描产物"的语义写的，
       而附件的生命周期跟着**任务**走。分开之后，加清理的人不会
       顺手带走别的东西。

    📌 顺带记一条**既有的缺口**（不是本次引入的）：扫描产物目录
    目前**没有任何自动清理**，只增不减。真实部署下 `_scan_runs/`
    会无限增长。这属于运维层的问题，但它值得被写下来 ——
    因为它此前被那段错误的注释"解释掉"了，看起来像已经处理过。
    """
    return get_settings().scan_root / "_uploads" / scan_id


# `artifact` 模式的目标目录名（任务 17）。放在扫描产物目录下。
CHALLENGE_DIRNAME = "challenge"


def challenge_dir(scan_id: str) -> Path:
    """`artifact` 模式的目标目录：题目附件铺开之后的地方（任务 17）。

    ## 为什么不放 `_uploads/<scan_id>/`（那是附件原件的地方）

    两者的生命周期**不同**，混在一起就再也分不开了：

    | | `_uploads/<scan_id>/` | `<scan_id>/challenge/` |
    | --- | --- | --- |
    | 是什么 | 用户交上来的**原件** | 从原件**派生**出来的 |
    | 重跑同一道题 | 还要用（不该被清） | 重新生成即可 |
    | 丢了怎么办 | 用户得重新上传 | 从原件再解一次 |

    派生物放在**产物目录**里，意味着"手工清理一次扫描"
    （`rm -rf _scan_runs/<scan_id>`）会带走它、而原件留着 ——
    这正是我们要的。反过来放，那次 rm 就会顺手删掉用户的题目素材。

    ⚠️ 与 `scan_upload_dir` 的 docstring 里那条口径一致：分开是为了
    **人的手**，不是自动清理（`sweep_retention` 至今不删任何目录）。
    """
    return get_settings().scan_root / scan_id / CHALLENGE_DIRNAME


def remove_scan_artifacts(scan_id: str) -> bool:
    """删掉一次扫描的产物目录。返回是否真的删了东西。

    ## ★ 为什么必须有这个函数（2026-10-11 补）

    在这之前，**永久删除一次扫描只删数据库行，磁盘上的产物永远留着** ——
    `purge_scan` 走到 `db.delete(scan)` 就结束了。于是：

      · 用户在回收站里点"永久删除"，以为清掉了，磁盘上还躺着 5–7 MB；
      · 删得越多，孤儿目录越多，而且**再也没有任何东西引用它们** ——
        数据库里已经查不到那个 id 了。

    这正是"磁盘只增不减"那条缺口的**主要来源**（另一部分是有意的：
    归档的扫描要留着产物，因为归档是"收起来"不是"删掉"）。

    ## 只删 `scan_root/<scan_id>/`，不碰兄弟目录

    `scan_root/` 下面住着五类东西，**只有第一类归这里管**：

    | 路径 | 归谁管 |
    | --- | --- |
    | `<scan_id>/` | ★ 本函数（扫描产物） |
    | `_uploads/<scan_id>/` | 附件原件，跟**任务**走，重跑还要用 |
    | `_uploads/_staging/` | `uploads.sweep_staging` |
    | `_ctf2_attachments/<题目id>/` | CTF2 附件，跟**题目**走 |
    | `<transcript 目录>` | CTF2 对话记录 |

    ⚠️ 所以这里**按 id 拼路径**，而不是"遍历 `scan_root` 删掉匹配
    某些条件的目录" —— 后者一旦条件写错就会连附件一起删，而附件
    **删了就回不来**（用户得重新上传 / 重新下题）。按 id 拼路径
    从构造上就不可能碰到 `_uploads` 这些下划线开头的兄弟目录。

    ⚠️ `scan_id` 来自数据库主键（`scan_<ULID>`），不是用户输入。
    但仍然做一次形状校验：万一将来有人把 URL 参数传进来，
    `../` 会让这个函数删掉任意目录。
    """
    # 形状校验：只允许 `scan_` + 字母数字。ULID 是大写 base32。
    if not scan_id or not scan_id.startswith("scan_"):
        return False
    if not scan_id[5:].isalnum():
        return False

    root = get_settings().scan_root.resolve()
    target = (root / scan_id).resolve()
    # 双保险：解析之后必须仍在 scan_root 之下（挡 `..` 与符号链接）。
    if not target.is_relative_to(root) or target == root:
        return False
    if not target.is_dir():
        return False

    shutil.rmtree(target, ignore_errors=True)
    return not target.exists()


def sweep_orphan_artifacts(db) -> int:  # noqa: ANN001
    """删掉**数据库里已无记录**的扫描产物目录。返回删了几个。

    ## 为什么按"孤儿"而不是按"时间"

    按时间删（"30 天前的目录都删"）会误伤两种情况：

      1. **正在跑的扫描** —— 它的目录很新，但一个跑了很久的扫描
         （大目标、慢网络）目录也可能很旧，而它正在写；
      2. **归档的扫描** —— 归档是"收起来"，用户随时可能要看产物。

    而"数据库里已经没有这条记录了"是一个**确定的**判据：那条扫描
    要么被 `purge_scan` 永久删除了，要么数据库被从别处恢复过。
    无论哪种，磁盘上那份都已经是无人引用的孤儿 —— 删它是安全的。

    ⚠️ 只扫 `scan_*` 形状的目录，跳过全部下划线开头的兄弟目录
    （`_uploads` / `_ctf2_attachments` / 转录目录）。理由同
    `remove_scan_artifacts`：那些的生命周期不跟扫描走。
    """
    from sqlalchemy import select

    from app.scans.models import Scan

    root = get_settings().scan_root
    if not root.is_dir():
        return 0

    on_disk = [
        p.name
        for p in root.iterdir()
        if p.is_dir() and p.name.startswith("scan_") and p.name[5:].isalnum()
    ]
    if not on_disk:
        return 0

    # 一次查完，不要每个目录查一次库。
    known = set(
        db.execute(select(Scan.id).where(Scan.id.in_(on_disk))).scalars()
    )
    removed = 0
    for name in on_disk:
        if name in known:
            continue
        if remove_scan_artifacts(name):
            removed += 1
    if removed:
        logger.info("清掉了 %d 个已无数据库记录的扫描产物目录", removed)
    return removed


def sweep_orphan_ctf2_workspaces(db) -> int:  # noqa: ANN001
    """清掉**题目已不存在**的 CTF2 持久工作区。返回清了几个。

    ## 为什么必须有（v0.29）

    持久工作区（`scan_root/_ctf2_workspaces/<challenge_id>/`）是这轮
    新加的：它让"续解"能继承上次的产物。但它**天然是只增不减的** ——
    而"只增不减"正是我刚在 README 缺口清单里列出的那一条。

    加一个持久化目录却不给它清理策略，等于把那条缺口从扫描产物
    复制到了 CTF2 —— 所以清理函数和它一起加。

    ## 判据与附件同源：**题目还在不在**

    | 情况 | 处置 |
    | --- | --- |
    | 题目还在库里（含已解出、已归档） | **留着** —— 用户可能还要看产物 |
    | 题目被删了（`ctf2_challenges` 里查不到） | **删掉** —— 无人引用了 |

    ⚠️ **不按时间删**：一道解出后放着不动的题，产物正是最该留的
    （复盘、写 WP 都要它）。而"题目记录没了"是确定的判据 ——
    与 `sweep_orphan_artifacts` 选"孤儿"而不是"时间"是同一条理由。

    ⚠️ 只扫 `_ctf2_workspaces/<id>` 这一层，**不动** `_ctf2_attachments/`
    （附件跟题目走，且题目没了时会被同一批删掉 —— 但那由各自的
    清理负责，不要在这里顺手做，否则两个生命周期又被搅在一起）。
    """
    from sqlalchemy import select

    from app.kernel.models import KernelChallenge

    root = get_settings().scan_root / "_ctf2_workspaces"
    if not root.is_dir():
        return 0

    on_disk = [p.name for p in root.iterdir() if p.is_dir()]
    if not on_disk:
        return 0

    # 一次查完，不要每个目录查一次库。
    known = set(
        db.execute(
            select(KernelChallenge.id).where(KernelChallenge.id.in_(on_disk))
        ).scalars()
    )
    removed = 0
    for name in on_disk:
        if name in known:
            continue
        target = root / name
        # 双保险：解析之后必须仍在 root 之下（挡符号链接与 `..`）。
        try:
            if not target.resolve().is_relative_to(root.resolve()):
                continue
        except OSError:  # pragma: no cover - 并发删除
            continue
        shutil.rmtree(target, ignore_errors=True)
        removed += 1
    if removed:
        logger.info("清掉了 %d 个题目已不存在的 CTF2 工作区", removed)
    return removed


def sweep_retired_artifacts(db, *, retention_days: int) -> int:  # noqa: ANN001
    """清掉**已归档且过了产物保留期**的扫描产物目录。返回删了几个。

    ## ★★ 这条补的是 README 缺口清单「扫描产物目录只增不减」的**后半段**（2026-10-16）

    磁盘只增不减那件事有**两个**来源，而 2026-10-11 那次只修了前一个：

    | 来源 | 谁负责 | 状态 |
    | --- | --- | --- |
    | 数据库里**已无记录**的孤儿目录 | `sweep_orphan_artifacts` | ✅ 已修 |
    | 记录还在、但**早已归档**的目录 | ★ **本函数** | 本次补 |

    第二个来源才是主因：`purge_scan` 要用户手动去回收站点"永久删除"，
    而**归档是自动的**（`sweep_retention` 到点就打 `archived_at`）。
    于是真实部署下"归档的扫描越来越多、产物永远留着" ——
    按每天 20 次扫描估，README 里那个 43 GB/年 的量级就是这么来的。

    ## 为什么判据是"已归档 + 超期"，而不是别的

    三种写法都试过想过，只有这一种同时安全且有效：

    | 判据 | 问题 |
    | --- | --- |
    | 只按时间（"90 天前的目录都删"） | **误伤正在跑的扫描** —— 一个跑了很久的扫描目录也会很旧，而它正在写 |
    | 只按"记录在不在" | 那是 `sweep_orphan_artifacts` 干的事，对"记录还在"的目录无效 |
    | **已归档 + 超期** | ✅ 归档是用户/规则明确表达的"收起来"，再过保留期才删 |

    ## ★ 为什么**不看** `deleted_at`（回收站里的那些）

    回收站里的扫描由 `trash_retention_days` 管，到点 `sweep_retention` 会
    **物理删除整条记录**、`purge_scan` 那条路径会顺带删产物。所以回收站
    那条路已经闭环了，这里再管一次只会让"两个保留期谁说了算"变成一个
    没人能回答的问题。**本函数只管归档那一条。**

    ## ★★ 为什么不碰兄弟目录

    只按 `<scan_id>` 拼路径（复用 `remove_scan_artifacts` 的形状校验与
    "解析后仍在 scan_root 之下"的双保险），**不遍历** `scan_root`。
    后者一旦条件写错就会连 `_uploads/` 一起删 —— 而附件**删了就回不来**
    （用户得重新上传）。

    ⚠️ 特别注意 `challenge/` 目录（`artifact` 模式的派生物）：它住在
    `<scan_id>/challenge/`，**跟着产物一起删是对的**（它是派生物，
    从 `_uploads/` 的原件还能再生成）。而 `_uploads/<scan_id>/` 是
    **另一个路径**，按 id 拼路径从构造上就碰不到它。

    ## `retention_days <= 0` 表示关闭

    与那套配置里 `0 = 关闭该规则` 的约定一致。返回 0、什么都不做。
    """
    if retention_days <= 0:
        # 显式关闭。**不做**"0 当作立刻清理"那种解释 —— 那会让一个
        # 想关掉清理的人反而把所有归档产物一次删光。
        return 0

    from datetime import timedelta

    from sqlalchemy import select

    from app.core.timeutil import now_utc
    from app.scans.models import Scan

    root = get_settings().scan_root
    if not root.is_dir():
        return 0

    cutoff = now_utc() - timedelta(days=retention_days)

    # ★ 判据全在数据库这一侧：已归档 + 未进回收站 + 归档时刻早于截止。
    #
    # ⚠️ 用 `archived_at`（什么时候归档的）而不是 `finished_at`（什么时候
    #    跑完的）：保留期问的是"它被收起来多久了"。用 `finished_at` 的话，
    #    一条跑完很久、刚被手工归档的扫描会被**立刻**删掉产物 ——
    #    而用户刚点了"归档"，期待的是"收起来"，不是"删掉"。
    #
    # ⚠️ `deleted_at.is_(None)`：进了回收站的交给回收站那条路（见 docstring）。
    candidates = list(
        db.execute(
            select(Scan.id)
            .where(Scan.archived_at.is_not(None))
            .where(Scan.archived_at < cutoff)
            .where(Scan.deleted_at.is_(None))
        ).scalars()
    )
    if not candidates:
        return 0

    removed = 0
    for scan_id in candidates:
        # 复用 `remove_scan_artifacts`：它自带形状校验（必须是 `scan_` +
        # 字母数字）与"解析后仍在 scan_root 之下"的双保险。
        # 自己再写一遍路径拼装只会多一处可能漂移的实现。
        if remove_scan_artifacts(scan_id):
            removed += 1

    if removed:
        logger.info(
            "清掉了 %d 个已归档超过 %d 天的扫描产物目录", removed, retention_days
        )
    return removed


# ══════════════════════════════════════════════════════════
# ★ 子进程的环境变量**白名单**
# ══════════════════════════════════════════════════════════
#
# ## 为什么不能 `dict(os.environ)`
#
# 原来两个 runner 都是 `env = dict(os.environ)` —— 那等于把**整个后端进程的
# 环境**交给一个接下来要执行任意命令的进程。而本进程的环境里有：
#
# | 变量 | 泄漏后果 |
# | --- | --- |
# | `SUPEX_SECRET_KEY` | **最严重**：它能解开数据库里所有 provider 的 API key 密文 |
# | `SUPEX_DATABASE_URL` | 连接串含数据库口令 → 直连业务库（扫描、漏洞、**审计日志**） |
# | `SUPEX_*` 其余 | CORS、会话 Cookie 名、限流阈值…给攻击者画一张部署图 |
#
# strix 的威胁模型是"它会执行从目标上拿到的东西"（这正是要沙箱的原因），
# 所以不能假设它内部一定安全 —— 一个被诱导 `env` 一下的 agent，
# 就把 `SUPEX_SECRET_KEY` 读走了。而拿到那条密钥 + 一份库快照
# = 所有 provider 凭据失守。
#
# ## 为什么是白名单而不是黑名单
#
# 黑名单（`pop("SUPEX_SECRET_KEY")`）要求**每次**有人加一个 `SUPEX_` 变量时
# 都记得来加一行 —— 而漏掉的那次不会有任何症状。白名单反过来：
# 新变量默认**不传**，要传必须显式加进来。默认安全。
#
# ## 为什么仍然要传一部分
#
# strix 是普通 CLI，它需要 `PATH`（找 docker/nmap）、`HOME`（读
# `~/.strix` 配置与 git 凭据）、以及 Windows 上那几个 SystemRoot 之类
# 否则连进程都起不来。所以白名单在 Windows 与 POSIX 上取不同的集合。
_POSIX_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "USER", "SHELL"}
)
_WINDOWS_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR",
        "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
        "COMSPEC", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
        "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
        # strix 起 docker 客户端要走它自己的路径解析
        "PROGRAMFILES(X86)", "PROGRAMW6432",
        "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY",
        "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
    }
)


# ★ 这些**前缀**下的变量一律透传。
#
# 它们不是我们的机密，而是**运维配置**：strix 自己读它们来控制沙箱行为。
# 白名单如果只认固定名字，会把运维手段一起挡掉 —— 而其中几条
# （`STRIX_DOCKER_SANDBOX_NETWORK`、`STRIX_SANDBOX_*` 资源上限）
# 恰恰是**隔离与限流的手段**。挡掉它们等于"为了安全而削弱了安全"。
#
# 我第一版白名单就犯了这个错：只认固定名字，于是运维设置的
# `STRIX_SANDBOX_MEM_LIMIT` / `STRIX_RUNTIME_BACKEND` 到不了 strix。
# 这是"默认安全"与"可运维"之间真实存在的张力，解法是按前缀放行
# **属于那个工具的命名空间**，而不是逐个人肉列举。
_PASSTHROUGH_PREFIXES: tuple[str, ...] = ("STRIX_", "DOCKER_")


def _base_env() -> dict[str, str]:
    """只带**运行一个 CLI 所必需**的环境变量。见上面的说明。

    ⚠️ 用 `os.name` 判断而不是 `sys.platform.startswith("win")`：
    两者都行，但 `os.name` 是标准库里最直接的那个，且不依赖字符串前缀。
    """
    allowlist = _WINDOWS_ENV_ALLOWLIST if os.name == "nt" else _POSIX_ENV_ALLOWLIST
    # 大小写：Windows 的环境变量名不区分大小写，POSIX 区分。
    # 统一按大写比对，但**保留原始键名**回填 —— 直接用小写名回填会在
    # POSIX 上得到一组找不到 `PATH` 的变量（`path` 在 Linux 上无效）。
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in allowlist or upper.startswith(_PASSTHROUGH_PREFIXES):
            env[key] = value
    return env


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """启动一次扫描需要的全部输入。

    凭据在这一层是**明文**（strix 需要它），所以这个对象本身是敏感的：
    不要 `repr` 它、不要把它写进日志、不要存进数据库。
    """

    scan_id: str
    target: str
    target_kind: str
    skills: tuple[str, ...]
    run_dir: Path
    log_path: Path
    # LiteLLM 格式的模型名，如 `deepseek/deepseek-chat`
    llm_model: str
    # 明文凭据。本地模型可以为空串。
    llm_api_key: str = field(repr=False)
    llm_api_base: str = ""
    # ── 任务背景与成功判据（任务 12）────────────────────────
    #
    # ⚠️ 放在**最后**：dataclass 的规则是"带默认值的字段必须在无默认值
    #    的之后"，而把它们插在中间会逼着 `run_dir` / `llm_model`
    #    也变成有默认值 —— 那两个是**必填**的，给默认值等于
    #    把"忘了传"从编译期错误变成运行期的 `None`/空串。
    #
    # ⚠️ `brief` **可能含凭据**（用户会写"测试账号 admin:xxx"）。
    #    所以它：
    #      · 不进命令行（走 `--instruction-file`，见 `command()`）；
    #      · 不进 `summary()`；
    #      · 落盘到 `<run_dir>/instruction.txt`，权限与扫描产物一致。
    brief: str | None = None
    goal: str = "findVulns"

    # ── 执行参数（任务 13）────────────────────────────────
    #
    # ⚠️ 有默认值，且默认值等于 strix 自己的默认值（`standard` / 500）。
    #    这样测试与替身路径不必逐个补参数，而真实路径上
    #    `service.build_spec()` 每次都会显式填上生效值。
    #
    # ★ 为什么由 spec 携带而不是在 `command()` 里读 env：
    #   这两个值现在是**可被设置页覆盖**的，而读覆盖值需要数据库会话。
    #   `command()` 是纯拼装函数、拿不到会话 —— 硬塞一个进去会让
    #   "拼命令行"这个动作依赖数据库。见 `settings/service.py` 的说明。
    scan_mode: str = "standard"
    scan_max_turns: int = 500

    # ── 沙箱容器配额（任务 14）────────────────────────────
    #
    # ★ 前三项**真的会被 strix 用上**（走环境变量，见 `env()`）：
    #   `STRIX_SANDBOX_CPUS` / `STRIX_SANDBOX_MEM_LIMIT` /
    #   `STRIX_DOCKER_SANDBOX_NETWORK`。
    #
    # ★ 后两项（v0.64 起）在**自研引擎（agent 后端）**里是真生效的：
    #   · `timeout_seconds` —— 墙钟超时。自研引擎自己在 `_solve_with_stop`
    #     的轮询循环里按它设 deadline，超时取消引擎循环（strix 后端没有
    #     扫描级墙钟超时，那一侧不读这个值 —— 见下）。
    #   · `allow_volume_write` —— 持久工作区挂载读写开关。为真时
    #     `<run_dir>/agent_run/workspace` 以 `:rw` 挂进 `/work/persist`，
    #     为假时以 `:ro` 挂。关掉后 agent 仍可用 `/work` 这个 tmpfs，
    #     只是产出出不了容器、跨重启不保留（这不影响发现收集：发现是
    #     引擎在内存里 `outcome.findings` 交给 `_write_vulnerabilities` 写的，
    #     不经这个卷）。
    #
    # ⚠️ 残余口径：strix 后端**不读**这两项（strix 全包没有扫描级墙钟超时，
    #    工作区挂载恒为 `read_only: False`）。界面上这两项按「自研引擎生效」
    #    标注即可，别写成 strix 也吃。
    sandbox_cpus: int | None = None
    sandbox_mem_limit_mb: int | None = None
    sandbox_network_isolated: bool = False
    # ★ v0.64：墙钟超时（秒）。None = 不设上限（沿用引擎自己的回合上限）。
    #   自研引擎在 `_solve_with_stop` 轮询里按它设 deadline；strix 不读。
    timeout_seconds: int | None = None
    # ★ v0.64：持久工作区挂载读写开关。默认 True（保持历史行为：
    #   工作区以 `:rw` 挂载）。False 时以 `:ro` 挂 —— 见上方注释。
    allow_volume_write: bool = True

    # ── 续跑（任务 16）──────────────────────────────────
    #
    # 非空时**不传 `--target`**，改传 `--resume <run_id>` —— 两者互斥，
    # 见 `command()` 的说明。
    #
    # ★ 续跑能接回什么：root 与**每个未终结子 agent 的完整 LLM 历史与拓扑**，
    #   外加工作目录里的 `notes.json` / `threat_models.json` / `todos.json` /
    #   `coverage.json`。也就是说上一次跑出来的判断与线索都还在 ——
    #   对一次跑了十几分钟才倒下的扫描，这比从头再来省得多。
    resume_run_id: str | None = None

    # ── 越权与注入手法块（v0.42）──────────────────────────
    #
    # 打开时 `_instruction()` 会在末尾追加 `prompts/offensive_playbook.md`
    # 那一块手法矩阵。关着时**连那个文件都不读** —— 指令字符串与
    # 引入该开关之前逐字节相同。
    #
    # ⚠️ 那个文件不叫 `ctf2_*.md`，尽管它最早是给 CTF2 写的：现在
    #   解题与扫描**共用同一份**，路径常量在 `app/core/prompts.py`。
    #
    # ★ 为什么由 spec 携带而不是在 `_instruction()` 里直接读配置：
    #   `_instruction()` 是 `@staticmethod`，拿不到数据库会话。硬塞一个
    #   进去会让"拼指令"这个动作依赖数据库 —— 与 `scan_mode` /
    #   `scan_max_turns` 由 spec 携带是同一条理由（见上面那段的说明）。
    #
    # ★ 为什么默认 `False`：这是**行为开关**。默认开会让所有既有部署在
    #   升级后立刻拿到一套更激进的手法，而设置页上在此之前根本没有
    #   这个选项 —— 那等于替用户做了一个他没做过的决定。
    enable_offensive_prompt: bool = False

    # ★ v0.58：多智能体编排开关。由 `scans.service` 从 KernelConfig 读出并
    # 写进本字段（引擎侧独立线程、不碰库，只能读这份快照）。只对自研引擎
    # （agent 后端）有意义 —— `StrixRunner` 不读它。默认 `False`：默认关，
    # 由人显式打开（成本按子 agent 数量放大，见 KernelConfig 的说明）。
    multi_agent: bool = False

    def summary(self) -> str:
        """可以安全打进日志的摘要 —— **不含凭据，也不含 `brief`**。

        ⚠️ `brief` 必须排除：用户会在里面写测试账号与口令，
        而 `summary()` 的产物是**会进日志**的。日志的保留期与可达面
        都远大于数据库里的那一列（见 `core/logging.py` 的脱敏说明）。
        """
        return (
            f"scan={self.scan_id} target={self.target} model={self.llm_model} "
            f"base={self.llm_api_base or '(默认)'} key={'有' if self.llm_api_key else '无'} "
            f"brief={'有' if self.brief else '无'}"
        )


@dataclass(frozen=True, slots=True)
class LaunchResult:
    pid: int
    run_dir: Path
    log_path: Path
    # 完整的命令行，**不含凭据**（凭据只在 env 里），所以它可以进日志。
    command: tuple[str, ...]
    # ★ 进程句柄。调度器靠 `poll()` 判存活 —— 而 `poll()` 走的是**句柄**，
    # 不是 pid，所以没有"pid 被系统复用"那个竞态。
    # 用 pid 判存活只在句柄已丢的场合（服务重启后的孤儿回收）才不得已而为之。
    process: subprocess.Popen = field(repr=False)


class ScanRunner(Protocol):
    """执行后端。两个实现：`StrixRunner`（真跑）与 `FakeRunner`（替身）。"""

    name: str

    def ensure_ready(self) -> None:
        """启动前的自检。前置条件不满足时抛 `RunnerUnavailable`。"""

    def command(self, spec: LaunchSpec) -> list[str]: ...

    def env(self, spec: LaunchSpec) -> dict[str, str]: ...

    def launch(self, spec: LaunchSpec) -> LaunchResult: ...

    def detect_run_id(self, run_dir: Path) -> str | None:
        """读出外部工具自己生成的 run 标识。读不到返回 None。"""


class RunnerUnavailable(RuntimeError):
    """执行后端不可用（可执行文件找不到、依赖缺失）。"""


# ══════════════════════════════════════════════════════════
# 进程工具：所有与操作系统打交道的地方都在这里
# ══════════════════════════════════════════════════════════


def is_alive(pid: int | None) -> bool:
    """这个 pid 还活着吗。

    ⚠️ 用 pid 判存活**有天生的竞态**：进程退出后 pid 可能被系统复用，
    于是一个已经死掉的扫描会被判成"还在跑"。所以这里只在
    **进程组/子进程句柄已经丢失**的场合用它（比如服务重启后的孤儿回收），
    而正常路径上用的是 `Popen.poll()`（它靠句柄，没有这个问题）。
    """
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        # Windows 上没有 os.kill(pid, 0) 那种"探活"语义（它会真的发信号）。
        # `tasklist` 是标准做法，但它要起一个进程 —— 一次回收扫描里
        # 调用几十次是可以接受的（不是热路径）。
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=_no_window(),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        # 没找到时 tasklist 打印一句"信息: 没有运行的任务匹配指定标准。"
        return str(pid) in completed.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 存在但不是我们的（同机其他用户）。仍然算"活着"，但杀不掉 ——
        # 那种情况下我们连启动它的资格都没有，不该走到这里。
        return True
    # ★ 存在 ≠ 活着：僵尸进程也会让 os.kill(pid, 0) 成功（见 _is_zombie）。
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    """POSIX：这个 pid 是不是僵尸进程。

    ## 为什么判活必须单独认它（2026-10-11 在 Linux CI 上抓到）

    `os.kill(pid, 0)` 探的是"这个 pid 还在不在进程表里"，**不是**
    "它还在不在跑"。进程被 SIGKILL 之后不会立刻消失：要等父进程
    `wait()` 掉才被回收，在那之前它是僵尸（状态 `Z`），依然占着 pid、
    依然让 `os.kill(pid, 0)` 成功。

    于是"杀进程"这条路径会出现一个很反直觉的现象：**已经杀死了，
    判定却说它还活着**。具体表现是 `_wait_gone` 白等到超时、日志打出
    一句"没有退出，强杀"、最后返回失败 —— 而进程其实早就死了。

    ⚠️ Windows 上不存在这个问题：那边走 `tasklist` 分支，进程一退出
    就从任务列表里消失，没有僵尸这个中间态。所以这条只在 POSIX 上
    才暴露 —— 也就是"本地（Windows）全绿、CI（Linux）红"的典型差异。

    ⚠️ 没有 `/proc` 的系统（macOS）不适用，返回 False 即"按原逻辑判"。
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
    except OSError:
        return False
    # 格式：`pid (comm) state ...`。comm 里可能有空格甚至括号
    # （进程名由别人控制），所以从**最后一个** `)` 之后切开取状态字段，
    # 不能用 split() 直接按位置取 —— 那样进程名里带一个空格就会错位。
    _, _, rest = data.rpartition(b")")
    fields = rest.split()
    return bool(fields) and fields[0] == b"Z"


def is_terminable(process: object | None) -> bool:
    """这个执行句柄能不能用 `terminate_tree(process.pid)` 杀。

    ## 为什么需要这一层

    `LaunchResult.process` 对 strix 后端是一个 `subprocess.Popen`（有真实
    的 OS pid），对自研引擎后端是一个 `_TaskHandle`（`pid` 是**线程标识**，
    不是 OS pid）。

    拿线程标识去 `os.kill(pid, 0)` / `killpg` 会探到**不相干的进程**，
    极端情况下会把别人的进程杀掉 —— 这是本后端引入的一个真实危险，
    必须在这里一次性挡住，而不是指望每个调用点自己记得判断。

    判据：`Popen` 没有 `terminable` 属性 → 默认 `True`（它是真的进程）；
    `_TaskHandle.terminable` 是属性且为 `False` → 返回 `False`。
    """
    if process is None:
        return False
    # `getattr(..., True)` 的 True 是给 Popen 用的：它没有这个属性，
    # 而 Popen 显然可终止。只有显式声明 `terminable = False` 的才挡。
    return bool(getattr(process, "terminable", True))


def _no_window() -> int:
    """Windows 上不给子进程弹控制台窗口。其他平台返回 0（无效果）。"""
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def popen_kwargs() -> dict:
    """spawn 的公共参数。

    * POSIX：`start_new_session=True` 让子进程成为**新进程组的组长**，
      这样 `killpg` 能一次干掉它和它的全部后代。
    * Windows：`CREATE_NEW_PROCESS_GROUP` + `CREATE_NO_WINDOW`，
      杀树靠 `taskkill /T`（它按父子关系走，不需要进程组）。
    """
    if os.name == "nt":
        return {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | _no_window(),
        }
    return {"start_new_session": True}


def terminate_tree(pid: int, *, grace_seconds: float = 10.0, log=print) -> bool:  # noqa: ANN001
    """终止一个进程**及其全部后代**。返回是否确认已经退出。

    ★ 「不留僵尸进程」这件事的实现在这里，而它比看起来难：

    * 直接 `Popen.terminate()` **只杀直接子进程**。strix 自己会起
      docker 客户端与一堆工具进程 —— 那些孙子进程会活下来，
      而且它们的父进程没了之后会被 init/systemd 收养，从此**再也没人认领**。
    * 所以必须按**树**杀：Windows 用 `taskkill /T /F`，
      POSIX 用进程组（`killpg`）。`/T` 与 `start_new_session` 就是为这个准备的。

    顺序也不能反：先温和（terminate / SIGTERM），给它机会清理沙箱容器，
    超时才强杀（`/F` / SIGKILL）。直接强杀会留下**启动到一半的容器** ——
    那正是"僵尸"的另一种形态，而且比进程更难清理。
    """
    if not is_alive(pid):
        return True

    if os.name == "nt":
        # ★ Windows 上直接 `/T /F`，不做"先温和后强杀"。
        #
        # 一开始写的是"先 `taskkill /T`（不带 /F）给它机会清理沙箱容器，
        # 超时再 /F"。**实测证明那是错的**：不带 /F 时 taskkill 发的是
        # 一个关闭请求，控制台程序未必响应；更要紧的是，
        # 直接子进程一退，`taskkill /T` 就报告成功，
        # 而**孙进程还活着继续写心跳** —— 测试当场抓到（`test_aborting_a_running_scan_kills_the_whole_process_tree`）。
        #
        # 而"给它机会清理容器"这个理由本身也站不住：Docker 容器是
        # dockerd 的子进程，不是 strix 的子孙，`/T` 从来就管不到它们。
        # 容器清理要靠 strix 自己或 `docker` 命令，不是靠温和地杀它。
        _run_quiet(["taskkill", "/PID", str(pid), "/T", "/F"], log=log)
        return _wait_gone(pid, min(grace_seconds, 10.0))

    # POSIX：整个进程组一起。
    import signal

    try:
        group = os.getpgid(pid)
    except ProcessLookupError:
        return True
    try:
        os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        return True
    if _wait_gone(pid, grace_seconds):
        return True
    log(f"进程 {pid} 在 {grace_seconds:g} 秒内没有退出，强杀")
    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        return True
    return _wait_gone(pid, 10.0)


def _run_quiet(command: list[str], *, log=print) -> None:  # noqa: ANN001
    try:
        subprocess.run(
            command, capture_output=True, timeout=30, creationflags=_no_window()
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # 杀不掉要出声 —— 静默失败会留下真的僵尸，而没人知道。
        log(f"执行 {command[0]} 失败：{exc}")


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(0.2)
    return not is_alive(pid)


# ══════════════════════════════════════════════════════════
# 续跑的前提（任务 16）
# ══════════════════════════════════════════════════════════


def resume_state_dir(run_dir: Path, run_id: str) -> Path:
    """strix 续跑所需状态的所在目录：`strix_runs/<run_id>/`。"""
    return run_dir / "strix_runs" / run_id


def can_resume(run_dir: str | Path | None, run_id: str | None) -> bool:
    """这次扫描**能不能**续跑。不能就返回 False。

    ## strix 的两个硬前提（源码 `interface/cli_args.py: _load_resume_state`）

    1. `strix_runs/<run_id>/run.json` —— 它从这里读回 `targets_info`
       与原始指令（"picks up where the prior run left off, **including the
       instruction**"）；
    2. `strix_runs/<run_id>/.state/agents.json` —— 会话与 agent 拓扑。

    ## 为什么必须在**启动前**判，而不是让它自己去失败

    少了任何一个，`--resume` **不会**优雅退回全新扫描，而是直接
    `parser.error` 退出：

        --resume X: missing .../agents.json. The run was never started ...
        there's nothing to resume from.

    那会变成一次"启动即失败"的扫描 —— 用户看到的是又一条失败记录，
    而不是"这次续不了，要不要重新跑"。所以判定要前置到**能给出
    解释的时刻**（点续跑按钮时），而不是留给子进程去撞。
    """
    if not run_dir or not run_id:
        return False
    base = resume_state_dir(Path(run_dir), run_id)
    return (base / "run.json").is_file() and (base / ".state" / "agents.json").is_file()


# ══════════════════════════════════════════════════════════
# 真 strix
# ══════════════════════════════════════════════════════════


class StrixRunner:
    """真的拉起 strix CLI。"""

    name = "strix"

    def ensure_ready(self) -> None:
        """启动前确认 strix 可执行文件在。

        **只检查它在不在，不检查 Docker。**
        为什么：Docker 不可用是 strix 自己能检测并报出来的（退出码 1），
        而那是一条**有日志、有退出码、可追溯**的失败路径。
        我们自己提前探一次 Docker，就会多一条与 strix 判断不一致的可能 ——
        而"我们以为能跑、strix 说不能跑"比"直接让 strix 去跑并失败"更难解释。
        """
        cfg = get_settings()
        if not self._resolve(cfg.strix_command):
            raise RunnerUnavailable(
                f"找不到 strix 可执行文件（SUPEX_STRIX_COMMAND={cfg.strix_command!r}）。\n"
                "安装方式见 README 的「strix 环境」一节；"
                "本机建议装在独立 venv 里，与 API 的依赖隔离。"
            )

    @staticmethod
    def _resolve(command: str) -> str | None:
        """把配置的 strix 命令解析成可执行路径。

        支持两种写法：PATH 上的命令名（`strix`），或绝对路径
        （本机用的是 `E:\\Program Files (x86)\\strix-venv\\Scripts\\strix.exe`）。
        `shutil.which` 对绝对路径同样有效，所以一个分支就够了。
        """
        return shutil.which(command)

    def command(self, spec: LaunchSpec) -> list[str]:
        cfg = get_settings()
        resolved = self._resolve(cfg.strix_command) or cfg.strix_command
        argv = [resolved]

        # ── 目标：新建 还是 续跑（任务 16）────────────────
        #
        # ★ 两者**互斥** —— 这不是我们的取舍，是 strix 的硬约束，原话：
        #
        #     Cannot combine --resume with --target/--target-list.
        #     --resume picks up where the prior run left off, including the
        #     instruction.
        #
        #   续跑时它会自己从 `run.json` 读回 `targets_info`。再给一个
        #   `--target` 是自相矛盾的（以哪个为准？），所以这里用 if/else
        #   而不是把两个参数叠上去 —— 叠上去 strix 会直接报错退出。
        if spec.resume_run_id:
            argv += ["--resume", spec.resume_run_id]
        else:
            argv += ["--target", spec.target]

        argv += [
            # 必须：无人值守模式的退出码才有区分度
            # （交互模式永远退 0，那样我们分不出成功与致命错误）。
            "--non-interactive",
            # ★ 扫描深度与回合上限取自 `spec`（任务 13），而不是直接读 env。
            #
            # `spec` 由 `service.build_spec()` 拼，那里有数据库会话，
            # 读到的是**设置页覆盖后的生效值**。`command()` 这一层没有会话，
            # 也不该为了两个参数去开一个 —— 它是个纯粹的命令行拼装函数。
            "--scan-mode",
            spec.scan_mode,
            "--max-turns",
            str(spec.scan_max_turns),
        ]
        if cfg.scan_max_budget_usd is not None:
            argv += ["--max-budget", str(cfg.scan_max_budget_usd)]

        # ── 工作区文件（任务 12 阶段 4）──────────────────
        #
        # CTF 题给的附件（二进制、源码、字典）走这个参数进沙箱，
        # 官方原话是 "Place a file from this machine into the sandbox
        # workspace before the scan starts"。没有它，agent 只能靠
        # 题面**描述**去猜附件里有什么 —— 而很多题的核心就在附件里。
        for path, dest in _workspace_files(spec):
            argv += ["--workspace-file", f"{path}:{dest}" if dest else str(path)]

        # ── 指令走**文件**，不走命令行（任务 12 阶段 3）────
        #
        # ★ 这是一个安全修复，不只是"绕开长度限制"。
        #
        # 原来指令拼在 `--instruction` 里，后果是：
        #   1. **`ps aux` 里能看到全文** —— 任何同机用户、任何日志采集
        #      都会看到。而 `brief` 里用户会写测试账号与口令
        #      （strix 官方文档自己举的例子就是
        #      "Use the following credentials: admin:password123"），
        #      那等于把凭据摊在进程列表里。
        #   2. **转义** —— 题面里的引号、反引号、`$`、`%hhn`
        #      会在拼 argv 时出问题，而那种错误是静默改写提示词。
        #
        # 写进 `<run_dir>/instruction.txt` 之后两个问题一起消失。
        # ⚠️ **不再保留 `--instruction` 那条路径**：两套并存意味着
        #    迟早有人往旧的那条里加东西，而它是不安全的。
        instruction = self._instruction(spec)
        if instruction:
            path = write_instruction_file(spec.run_dir, instruction)
            argv += ["--instruction-file", str(path)]
        return argv

    @staticmethod
    def _instruction(spec: LaunchSpec) -> str:
        """把**任务背景**与技能范围拼成提示词。

        ## ★ 顺序就是优先级：用户的任务背景放最前

        这不是排版偏好。实测的抱怨是「只给网址让他扫描太没意义了」——
        而一段 CTF 题面里的信息量（考点、提示、**诱饵**、flag 格式）
        远大于我们能猜的。把用户的背景放在第一段，agent 读到的第一件事
        就是"这道题在考什么"，而不是我们那套通用范围说明。

        我们自己的口径要求（中文输出、授权声明）放后面 ——
        它们仍然会被读到，但不该盖过用户对**这次任务**的交代。

        ## 授权声明保留

        它在 agent 面对可疑行为时提供上下文（这是渗透测试，不是入侵）。
        这是既有的设计，与任务背景不冲突。
        """
        parts: list[str] = []

        # ① 用户的任务背景（任务 12）。**最前**，见 docstring。
        #
        # ⚠️ 用明确的分隔与标题包起来，而不是把用户文本直接倒进去：
        # 题面里可能有"忽略之前的指令"这类句子（CTF 题面本身就可能
        # 就是攻击性文本，那正是题目内容）。标清"以下是用户提供的
        # 任务背景"能让模型把它当**资料**读，而不是当系统指令读。
        # 这不是防注入（用户就是授权方，他自己写的东西注入自己
        # 不构成威胁），而是**让模型正确理解这段文本的角色**。
        if spec.brief:
            parts.append(
                "===== 以下为用户提供的任务背景，请作为这次测试的**参考资料**理解 =====\n"
                + spec.brief.strip()
                + "\n===== 任务背景结束 ====="
            )

        # ② 成功判据（任务 12 阶段 5）。它改变 agent 的**取舍**。
        parts.append(_goal_clause(spec.goal))

        # ③ 探索与止损协议（任务 13）。它改变 agent 的**过程**：什么时候
        #    该继续深挖、什么时候该承认这条线索死了。放在判据之后 ——
        #    先讲清"要什么"，再讲"怎么走"。
        parts.append(EXPLORATION_PROTOCOL)

        # ④ 技能范围。没有技能参数可用（见模块 docstring），
        #    所以这是唯一能表达它的地方。
        focus = [SKILL_FOCUS[skill] for skill in spec.skills if skill in SKILL_FOCUS]
        if focus:
            parts.append("本次测试的侧重范围：" + "；".join(focus) + "。")
            parts.append("范围之外的发现也请记录，但不要为此展开额外探索。")
        if spec.target_kind == "path":
            parts.append("目标是一个本地代码目录，属于白盒测试。")

        # ★ TCP 目标：纠正执行后端那句**错误的类型标注**（任务 15）。
        #
        # 放在这一段（而不是并进用户题面）有理由：它描述的是"目标是什么形态"，
        # 与上面的技能范围属于同一层信息；而且它是**我们的口径**，
        # 不该混进"用户提供的资料"里 —— 那样模型会把它当参考资料读，
        # 而不是当必须遵守的纠正。
        if spec.target_kind == "service":
            parts.append(TCP_TARGET_CLAUSE)

        # ★ 越权与注入手法矩阵（v0.42）。
        #
        # ## 为什么放在这里（而不是跟着 `EXPLORATION_PROTOCOL`）
        #
        # 协议那一段讲的是**过程**：什么时候继续深挖、什么时候承认线索死了。
        # 这一段讲的是**具体手法**：手上有回显就先打什么、过滤器在挡的时候
        # 怎么换形。它是"怎么走"的下一层，所以排在协议的后面。
        #
        # 而它又排在"要求中文输出"**之前** —— 那句话是对**输出形式**的要求，
        # 应当是全篇最后一句话（离生成最近），不该被一大段 payload 隔开。
        #
        # ## 为什么关着时连文件都不读
        #
        # 与 `ctf2` 侧同一条纪律：关着的部署里那个文件可能根本不存在，
        # 而"关着 = 完全没有影响"要能在行为上成立，不只是看上去成立。
        if spec.enable_offensive_prompt:
            parts.append(load_offensive_playbook())

        # ★ 要求中文输出（任务 11）。
        #
        # 实测时活动页的"推理"全是英文，用户看不懂那些内容 ——
        # 而这套界面是中文的，那种混搭让人以为"是不是坏了"。
        #
        # 说清楚能改什么、不能改什么：strix 自己的系统提示词与工具描述
        # 都是英文的，我们**只能通过 `--instruction` 影响模型的输出倾向**，
        # 改不了它内部的思考语言。所以这句话是"尽量"，不是保证。
        #
        # 措辞上区分两类文本：自由论述（推理/结论）要中文，
        # 而命令、路径、payload 这类**必须保持原样** —— 把
        # `ffuf -w ... -u ...` 翻译成中文会让命令直接跑不起来。
        parts.append(
            "请用中文输出你的推理过程与结论、以及对发现的描述。"
            "但**命令行、URL、路径、代码、payload 一律保持原样**，不要翻译。"
        )

        # ★ 分隔符从 `" "` 改成 `"\n\n"`（任务 12）。
        #
        # 任务背景是**多行**的（题面、附件清单、测试账号），用空格拼
        # 会把它们揉成一整段 —— 而段落边界正是模型区分
        # "这是题面 / 这是我们的口径要求"的依据。
        return "\n\n".join(parts)

    def env(self, spec: LaunchSpec) -> dict[str, str]:
        cfg = get_settings()
        env = _base_env()
        env["STRIX_LLM"] = spec.llm_model
        if spec.llm_api_key:
            env["LLM_API_KEY"] = spec.llm_api_key
        else:
            # 本地模型不需要 key。**显式删掉**继承来的那个 ——
            # 否则宿主机 shell 里恰好导出的一个 LLM_API_KEY 会被悄悄用上，
            # 而用户以为自己选的是本地模型。
            env.pop("LLM_API_KEY", None)
        if spec.llm_api_base:
            env["LLM_API_BASE"] = spec.llm_api_base
        else:
            env.pop("LLM_API_BASE", None)

        # 遥测默认关闭（上游默认是开）。理由是产品取向而不是洁癖：
        # 本平台跑的是**客户目标的渗透测试**，把目标与用量上报给第三方，
        # 是一件需要用户明确同意才能做的事。想开就显式设 true。
        env["STRIX_TELEMETRY"] = "1" if cfg.strix_telemetry else "0"

        # ── 沙箱容器配额（任务 14）────────────────────────
        #
        # ★ strix 读的是这三个环境变量（源码 `runtime/docker_client.py`
        #   的 `_apply_resource_limits` / `_apply_sandbox_network`）——
        #   也就是说，配了**真的会生效**，不是记一笔。
        #
        # ⚠️ 未设/空值时 strix 保持 docker 默认（**无上限**），
        #    所以这里只在有值时才设，不替它编一个默认。
        if spec.sandbox_cpus is not None:
            env["STRIX_SANDBOX_CPUS"] = str(spec.sandbox_cpus)
        if spec.sandbox_mem_limit_mb is not None:
            # strix 的 `mem_limit` 直接交给 docker，吃的是 "4096m" 这种串。
            env["STRIX_SANDBOX_MEM_LIMIT"] = f"{spec.sandbox_mem_limit_mb}m"

        # 沙箱所在的 docker 网络。
        #
        # ★ 优先级：**"禁止出网"压过配置的网络名**。
        #   两者都设时不能让网络名赢 —— 那会让一个明确勾了"禁止外网"的
        #   用户得到一个能出网的沙箱，而他以为自己已经收紧了。
        #   `none` 是 docker 内置的"无网络"网络，容器仍然起得来。
        if spec.sandbox_network_isolated:
            env["STRIX_DOCKER_SANDBOX_NETWORK"] = "none"
        elif cfg.sandbox_network.strip():
            # 空则不动 —— 保持 strix 的默认行为，不替用户做决定
            # （默认 bridge 是多数本地场景要的）。
            env["STRIX_DOCKER_SANDBOX_NETWORK"] = cfg.sandbox_network.strip()

        # 让子进程的输出不缓冲 —— 否则日志要等扫描结束才一次出现，
        # 而"扫描卡住了"的时候你正需要看它最后一行。
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def launch(self, spec: LaunchSpec) -> LaunchResult:
        spec.run_dir.mkdir(parents=True, exist_ok=True)
        log_handle = open(spec.log_path, "ab", buffering=0)  # noqa: SIM115 - 交给子进程持有
        try:
            process = subprocess.Popen(  # noqa: S603 - 参数是列表，没有 shell
                self.command(spec),
                cwd=str(spec.run_dir),
                env=self.env(spec),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **popen_kwargs(),
            )
        except OSError as exc:
            log_handle.close()
            raise RunnerUnavailable(f"启动 strix 失败：{exc}") from exc
        # 父进程关掉自己这一份写端 —— 子进程仍然持有它。
        # 不关的话，日志文件会一直被父进程占着，而 Windows 上
        # 那会让"扫描结束后归档日志"这类操作失败。
        log_handle.close()

        return LaunchResult(
            pid=process.pid,
            run_dir=spec.run_dir,
            log_path=spec.log_path,
            command=tuple(self.command(spec)),
            process=process,
        )

    def detect_run_id(self, run_dir: Path) -> str | None:
        """读 strix 生成的 run 标识。

        依据 docs.strix.ai 的 Configuration 页：本地遥测写在
        `strix_runs/<run_name>/events.jsonl`（相对**工作目录**）。
        所以 run 标识就是 `strix_runs/` 下的那个目录名。

        取**最新的**那个。正常情况下一次扫描只会产生一个；
        取最新是防御性的 —— 万一目录里残留了上一次的东西，
        至少拿到的是这一次的。
        """
        root = run_dir / "strix_runs"
        if not root.is_dir():
            return None
        candidates = [entry for entry in root.iterdir() if entry.is_dir()]
        if not candidates:
            return None
        newest = max(candidates, key=lambda path: path.stat().st_mtime)
        return newest.name


# ══════════════════════════════════════════════════════════
# 替身：测试与 CI 用
# ══════════════════════════════════════════════════════════

# 替身脚本。
#
# 它做三件事，分别验证三条不同的链路：
#
#   1. **故意起一个孙进程**持续写心跳文件 —— 那是"杀进程树"唯一能被
#      机器验证的方式：只杀直接子进程的实现会让孙进程活下来继续写心跳，
#      而测试会当场看见。
#
#   2. **产出真实结构的 strix 产物** —— `strix_runs/<run>/.state/agents.db`
#      （按 openai-agents SDK 的建表语句）、`agents.json`、`run.json`。
#      于是"把 strix 事件导进本地库"这条链路可以端到端验证，
#      而不是只能对着伪造的库手工调解析函数。
#
#   3. **把契约 §2.5 的 8 种事件类型尽量都跑出来** —— 三层 agent 树
#      （root → recon / exploit / verify）、一个**真的失败了的** agent
#      （`errors` 里有值 ⇒ 我们这边强制落 `FAILED`）、以及 5 档 severity
#      各一条。一个"只有成功路径"的替身测不出失败路径：那些分支会一直
#      躺在代码里没人走，直到线上第一次真失败。
#
# ⚠️ 这里**曾经**写的是 `strix_runs/<run>/events.jsonl` ——
# 那是照着 strix 官方文档写的，而文档是错的：`strix-agent` 1.6.2 的源码里
# 全包搜索 `events.jsonl` 零命中。真正的结构化事件流在 `agents.db` 里。
# 这个改动就是"先查源码再写代码"的结果。
#
# ⚠️ 产物里的每个字段都要**照着 strix 源码**写，不能照着"看起来合理"写。
# 这里踩过两次：`create_vulnerability_report` 的参数里根本没有 `severity`
# （它由 `cvss_breakdown` 算出来），而 `poc_script_code` 是**包在 markdown
# 围栏里**存的（`report/writer.py`）。两处都曾按印象写错，而错的产物会让
# 解析层的测试"通过"，因为测试与替身用的是同一个错误假设。
_FAKE_SCRIPT = r'''
import json, os, sqlite3, subprocess, sys, time
from pathlib import Path

run_dir = Path(sys.argv[1])
duration = float(os.environ.get("FAKE_STRIX_SECONDS", "3"))
exit_code = int(os.environ.get("FAKE_STRIX_EXIT", "0"))
make_run = os.environ.get("FAKE_STRIX_RUN_ID", "")
spawn_grandchild = os.environ.get("FAKE_STRIX_GRANDCHILD", "0") == "1"
emit_events = os.environ.get("FAKE_STRIX_EVENTS", "0") == "1"

print(f"[fake-strix] 启动 target={os.environ.get('FAKE_STRIX_TARGET','')}", flush=True)
print(f"[fake-strix] STRIX_LLM={os.environ.get('STRIX_LLM','')}", flush=True)
print(f"[fake-strix] LLM_API_KEY={'<已设置>' if os.environ.get('LLM_API_KEY') else '<未设置>'}", flush=True)

child = None
if spawn_grandchild:
    heartbeat = run_dir / "grandchild.heartbeat"
    code = (
        "import sys, time\n"
        "p = sys.argv[1]\n"
        "while True:\n"
        "    open(p, 'w').write(str(time.time()))\n"
        "    time.sleep(0.2)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code, str(heartbeat)])
    print(f"[fake-strix] 孙进程 pid={child.pid}", flush=True)

if make_run:
    run_root = run_dir / "strix_runs" / make_run
    state = run_root / ".state"
    state.mkdir(parents=True, exist_ok=True)

    # 1) run.json —— 照 core/paths.py 的 RUN_RECORD_FILENAME 与
    #    interface/viewer/transcript.py 读的字段。
    (run_root / "run.json").write_text(json.dumps({
        "run_name": make_run,
        "status": "running",
        "start_time": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "targets_info": [{"original": os.environ.get("FAKE_STRIX_TARGET", "")}],
        "user_instruction": "fake run",
    }), encoding="utf-8")

    # 2) agents.json —— 照 live_view.hydrate_from_run_dir 读的四个字典。
    #
    #    ★ 三层 agent 树：root 是编排者，它下面挂 recon / exploit / verify
    #    三个各司其职的子 agent（角色靠 `strix_events._ROLE_BY_NAME` 按名字
    #    子串推断），另有一个**真的失败了的** agent —— 它是 `agent-recon`
    #    的下级，用来走"名册里 `errors` 有值 ⇒ 我们这边强制落 FAILED"
    #    那条分支。没有失败 agent 的替身测不出那条分支：它会一直躺在代码里
    #    没人走，直到线上第一次真失败。
    #
    #    ★ 名册写**两遍**：先按"刚开跑"写一次，跑完再按"收尾"覆盖一次。
    #    导入器每一轮都重读名册刷新 agent 状态（`importer._ensure_agent`），
    #    只写一遍的话那条刷新路径永远只看到同一个值 —— 等于没测。
    agent_names = {
        "agent-root": "root",
        "agent-recon": "recon",
        "agent-exploit": "exploit",
        "agent-verify": "verify",
        "agent-failed": "recon-crawler",
    }
    agent_parent_of = {
        "agent-recon": "agent-root",
        "agent-exploit": "agent-root",
        "agent-verify": "agent-root",
        "agent-failed": "agent-recon",
    }

    def write_roster(statuses, errors):
        (state / "agents.json").write_text(json.dumps({
            "statuses": statuses,
            "names": agent_names,
            "parent_of": agent_parent_of,
            "errors": errors,
        }, ensure_ascii=False), encoding="utf-8")

    write_roster({agent_id: "running" for agent_id in agent_names}, {})

    # 3) agents.db —— **建表语句逐字照抄** openai-agents SDK 的
    #    sqlite_session.py，因为那是 strix 实际会用到的结构。
    #    列名 `session_id` 里装的是 strix 的 agent_id（见 history.py 的查询：
    #    它把 session_id 读出来当 agent_id 用）。
    if emit_events:
        conn = sqlite3.connect(state / "agents.db")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            " session_id TEXT PRIMARY KEY,"
            " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            " updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_messages ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " session_id TEXT NOT NULL,"
            " message_data TEXT NOT NULL,"
            " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            " FOREIGN KEY (session_id) REFERENCES sessions (session_id) ON DELETE CASCADE)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_messages_session_id"
            " ON agent_messages (session_id, id)"
        )

        def put(session, payload):
            conn.execute(
                "INSERT INTO agent_messages (session_id, message_data) VALUES (?, ?)",
                (session, json.dumps(payload, ensure_ascii=False)),
            )

        def call(agent, call_id, name, args):
            put(agent, {"type": "function_call", "call_id": call_id, "name": name,
                        "arguments": json.dumps(args, ensure_ascii=False)})

        def result(agent, call_id, payload):
            put(agent, {"type": "function_call_output", "call_id": call_id,
                        "output": payload if isinstance(payload, str)
                        else json.dumps(payload, ensure_ascii=False)})

        # ── agent-root：编排者。第一个 user 轮是"它的任务"，会被丢掉 ──
        put("agent-root", {"role": "user", "content": "对 api.example.com 做一次安全测试"})
        # think 工具 → 应翻成 reasoning(intent=plan)
        call("agent-root", "c1", "think", {"thought": "先做端口与服务识别，再决定打哪里。"})
        result("agent-root", "c1", {"success": True, "message": "Thought recorded"})
        # 一次成功的工具调用 → tool_call
        call("agent-root", "c2", "web_search", {"query": "api.example.com 已知漏洞"})
        result("agent-root", "c2", {"success": True, "results": [{"title": "X"}]})
        # 一条 assistant 发言 → reasoning(intent=narration)
        put("agent-root", {"role": "assistant", "content": "已完成初步侦察，进入利用阶段。"})

        # ── agent-recon：子 agent。它的消息行证明"名册里有两个以上 agent"
        #    这条路径真的被走过（旧替身只有 root 有消息行，子 agent 是摆设）──
        put("agent-recon", {"role": "user", "content": "侦察子任务：枚举端点与版本。"})
        call("agent-recon", "c5", "web_get_contents", {"url": "https://api.example.com/"})
        result("agent-recon", "c5", {"success": True, "content": "<html>…</html>"})

        # ── agent-exploit：利用阶段 ──
        put("agent-exploit", {"role": "user", "content": "利用子任务：验证未授权读取。"})
        # 一次失败的工具调用 → failed
        call("agent-exploit", "c3", "repeat_request", {"request_id": "r1"})
        result("agent-exploit", "c3", {"success": False, "error": "请求重放被目标拒绝"})
        # 产出漏洞报告 → discovery
        #
        # ⚠️ 参数里**没有** `severity`：真实工具的签名里没有这个参数
        # （严重度由 `cvss_breakdown` 经 CVSS3 算出来），结果里才有。
        # 旧替身在这里写了一个 `severity: "high"`，于是解析层那个
        # "读 args['severity']" 的错误分支看起来是通的 —— 替身与解析层
        # 共享同一个错误假设，测试就永远发现不了它。
        call("agent-exploit", "c4", "create_vulnerability_report", {
            "title": "调试接口未授权可读环境变量",
            "description": "GET /debug/env 未做鉴权即可读取进程环境变量。",
            "evidence": "HTTP/1.1 200 OK\nAWS_KEY=***",
            "confidence": "high",
            "cwe": "CWE-200",
        })
        result("agent-exploit", "c4", {"success": True, "report_id": "vuln-0001",
                                       "severity": "high", "cvss_score": 7.5})

        # ── agent-verify：复核阶段。这两条让 `poc_generated` 与 `verified`
        #    第一次有了真实来源（契约的 8 种事件类型里，它们此前
        #    "在 strix 侧没有对应来源"，只能靠扫描生命周期兜）──
        #
        #    ⚠️ **参数必须按真 schema 造，是扁平的**：`update_vulnerability_report`
        #    的 `params_json_schema` 里 `required = ['report_id','update_reason']`，
        #    顶层 26 个 properties，`poc_script_code` / `confidence` / `target`
        #    都在顶层，**没有 `fields`**（那是 `_do_update` 的内部形参）。
        #    旧替身包了一层 `fields` —— 而解析层那时也读 `fields`，两边
        #    **共享同一个错误假设**，于是那两个分支在真实扫描里从没命中过，
        #    测试却一直是绿的。这正是"替身与解析层同源"最危险的地方。
        put("agent-verify", {"role": "user", "content": "复核子任务：为已确认的发现补 PoC。"})
        # 给已有发现补上验证脚本 → poc_generated
        # （脚本**包在 markdown 围栏里**，照 `report/writer.py` 的存法）
        call("agent-verify", "c6", "update_vulnerability_report", {
            "report_id": "vuln-0001",
            "update_reason": "补上可复现的验证脚本，证据从「疑似」升级为「可复现」。",
            "poc_script_code": "```python\nimport requests\n\nr = requests.get('https://api.example.com/debug/env', timeout=10)\nprint(r.status_code, len(r.text))\n```",
            "target": "api.example.com",
        })
        result("agent-verify", "c6", {"success": True, "action": "updated",
                                      "message": "Report 'vuln-0001' now carries your revision. Do not file it again.",
                                      "report_id": "vuln-0001",
                                      "updated_fields": ["poc_script_code", "target"],
                                      "severity": "high", "cvss_score": 7.5})
        # 复核后把置信度提到 high → verified
        call("agent-verify", "c7", "update_vulnerability_report", {
            "report_id": "vuln-0002",
            "update_reason": "在预发环境复现了同一问题，置信度提到高。",
            "confidence": "high",
        })
        result("agent-verify", "c7", {"success": True, "action": "updated",
                                      "report_id": "vuln-0002",
                                      "updated_fields": ["confidence"],
                                      "severity": "medium", "cvss_score": 4.3})
        # 一次"只改措辞"的 update → 走 `tool_call` 兜底（**不是** poc_generated）。
        # 留它是为了钉住兜底那条路：`update_` 不等于"产出了 PoC"或"已验证"，
        # 把它翻成那两种是编造，而编造的事件在活动流里看起来和真的一样。
        call("agent-verify", "c8", "update_vulnerability_report", {
            "report_id": "vuln-0003",
            "update_reason": "只调整措辞，不改结论。",
            "remediation_steps": "换一种说法。",
        })
        result("agent-verify", "c8", {"success": True, "action": "updated",
                                      "report_id": "vuln-0003",
                                      "updated_fields": ["remediation_steps"],
                                      "severity": "low", "cvss_score": 3.1})
        # 交还控制权等人 → ⛔ **不再是 `blocked`**。
        # 旧替身调的是 `wait_for_user`，而 1.6.2 里**没有这个工具**：
        # 真名是 `respond_to_user`（`strix/tools/respond/tool.py:17`），
        # 且它在 `agents/factory.py:688-690` 受 `if interactive:` 门控，
        # 而本项目传 `--non-interactive` —— 双重不可达。所以契约的 `blocked`
        # 在 strix 侧**没有来源**，这里只喂一个真形状的调用（成功返回是
        # JSON 字符串），它会走 `tool_call` 兜底。
        call("agent-verify", "c9", "respond_to_user", {
            "message": "需要人工确认后才能继续：目标上还挂着测试账号，是否清理？",
        })
        result("agent-verify", "c9", json.dumps({
            "success": False,
            "error": ("No user is attached to an autonomous run. Keep working, and call "
                      "finish_scan (root) or agent_finish (subagent) when the task is done."),
        }, ensure_ascii=False))

        # ── agent-failed：真的失败了的子 agent。它的失败在名册的 `errors`
        #    里，而"名册里 errors 有值 ⇒ 我们这边强制落 FAILED"那条分支
        #    此前**没有任何测试走过** ──
        put("agent-failed", {"role": "user", "content": "爬取子任务：抓取站点地图。"})
        call("agent-failed", "c9", "view_sitemap_entry", {"url": "https://api.example.com/sitemap.xml"})
        result("agent-failed", "c9", {"success": False, "error": "目标在 8443 端口重置了连接"})

        conn.commit()
        conn.close()

    # 4) vulnerabilities.json —— 字段名与"有才写"的规则都照
    #    `report/state.py` 的 `add_vulnerability_report`，
    #    写入点照 `report/writer.py`（它在 run 目录根下）。
    #
    #    ★ **5 档 severity 各一条**。旧替身只有 high / high / medium，
    #    于是 `low` 与 `info` 这两档在整条链路上从没被真实数据走过 ——
    #    汇总列的 `bySeverity` 是 Partial（为 0 的档键不存在），
    #    少两档就永远看不出"键该不该出现"这件事到底对不对。
    #
    #    ★ `poc_script_code` **包在 markdown 围栏里**（照 `report/writer.py`
    #    的存法：agent 生成的代码字段带语言标签的围栏）。旧替身存的是裸代码，
    #    形状与真实产物不符 —— 而落库那侧要靠围栏取语言。
    #
    #    ★ `vuln-0005` 故意**没有** `severity` 字段：真实产物里也可能缺，
    #    而落库侧对"认不出的严重度"有明确口径（落 `info` 并记 warning）。
    if emit_events:
        (run_root / "vulnerabilities.json").write_text(json.dumps([
            {
                "id": "vuln-0001",
                "title": "调试接口未授权可读环境变量",
                "severity": "high",
                "timestamp": "2026-09-24 10:00:00 UTC",
                "description": "GET /debug/env 未做鉴权即可读取进程环境变量。",
                "target": os.environ.get("FAKE_STRIX_TARGET", ""),
                "technical_analysis": "路由注册在鉴权中间件之前。",
                "evidence": "HTTP/1.1 200 OK\nAWS_SECRET_ACCESS_KEY=***",
                "poc_script_code": "```python\nimport requests\n\nr = requests.get('https://x/debug/env', timeout=10)\nprint(r.status_code, len(r.text))\n```",
                "remediation_steps": "把该路由挂到鉴权中间件之后。",
                "confidence": "high",
                "cwe": "CWE-200",
                "cvss": 7.5,
            },
            {
                "id": "vuln-0002",
                "title": "会话 Cookie 缺少 SameSite 属性",
                "severity": "medium",
                "timestamp": "2026-09-24 10:00:05 UTC",
                "description": "登录响应里的会话 Cookie 未设置 SameSite。",
                "technical_analysis": "Set-Cookie 头里没有 SameSite。",
                "confidence": "medium",
                "confidence_rationale": "只在测试环境复现过",
                "counterevidence": "预发环境返回的头部带了 SameSite=Lax",
                "cwe": "CWE-1275",
            },
            {
                "id": "vuln-0003",
                "title": "管理后台使用出厂默认口令",
                "severity": "critical",
                "timestamp": "2026-09-24 10:00:10 UTC",
                "description": "管理后台仍可用 admin/admin 登录。",
                "technical_analysis": "初始化脚本在首次启动时写入固定口令且不强制修改。",
                "evidence": "POST /admin/login\\nHTTP/1.1 302 Found\\nLocation: /admin/dashboard",
                "remediation_steps": "首次登录强制改密，并移除初始化脚本里的固定口令。",
                "confidence": "high",
                "cwe": "CWE-798",
                "cvss": 9.8,
            },
            {
                "id": "vuln-0004",
                "title": "响应头缺少 Content-Security-Policy",
                "severity": "low",
                "timestamp": "2026-09-24 10:00:15 UTC",
                "description": "所有页面响应都没有 CSP 头。",
                "technical_analysis": "反向代理层未注入 CSP。",
                "confidence": "medium",
                "cwe": "CWE-693",
                "cvss": 3.1,
            },
            {
                "id": "vuln-0005",
                "title": "错误页回显了内部路径",
                "severity": "info",
                "timestamp": "2026-09-24 10:00:20 UTC",
                "description": "404 页面回显了容器内的绝对路径。",
                "technical_analysis": "默认错误页未定制。",
                "confidence": "low",
                "cwe": "CWE-209",
            },
        ], ensure_ascii=False), encoding="utf-8")
        print("[fake-strix] 写入了 5 条 vulnerabilities.json（5 档 severity 各一条）", flush=True)

    # 5) 收尾时再写一遍名册：状态从 running 变成终态，并且
    #    `agent-failed` 在 `errors` 里留下一句话。
    #    导入器每一轮都重读名册刷新 agent 状态，所以第二遍才让
    #    "状态会变"与"失败 agent 落 FAILED"这两条路径真的被走到。
    if emit_events:
        write_roster(
            {
                "agent-root": "completed",
                "agent-recon": "completed",
                "agent-exploit": "completed",
                # ⚠️ 真值只能是 `core/agents.py` 的
                # `Status = Literal["running","waiting","completed","stopped",
                #                    "crashed","failed","budget_paused"]` 里那几个。
                # 旧替身在这里写 `blocked` —— 那个状态在 1.6.2 里**不存在**
                # （全包搜 `"blocked"` 零命中），于是 `_STATUS_MAP` 里那条
                # `blocked` 分支只有替身能走到，真实扫描永远走不到：
                # 又一处"替身与解析层共享同一个错误假设"。
                # `waiting` 是真值（`park_waiting` 写的那个），且与
                # `completed` / `failed` 都不同 —— 它把"非终态非失败 → IDLE"
                # 这条映射真的验到了。
                "agent-verify": "waiting",
                "agent-failed": "failed",
            },
            {"agent-failed": "目标在 8443 端口重置了连接"},
        )

    print(f"[fake-strix] run={make_run}", flush=True)

try:
    time.sleep(duration)
finally:
    if child is not None:
        child.terminate()

print(f"[fake-strix] 退出码 {exit_code}", flush=True)
sys.exit(exit_code)
'''


class FakeRunner:
    """替身执行后端。

    它**不是 mock**：它同样是一个真的子进程（还带一个真的孙进程），
    只是那个进程不做任何真实攻击。所以队列、进程监督、
    杀进程树、退出码解释这些逻辑全都是真的在跑。
    """

    name = "fake"

    def ensure_ready(self) -> None:
        return None

    def command(self, spec: LaunchSpec) -> list[str]:
        script = spec.run_dir / "_fake_strix.py"
        return [sys.executable, str(script), str(spec.run_dir)]

    def env(self, spec: LaunchSpec) -> dict[str, str]:
        cfg = get_settings()
        env = _base_env()
        env["FAKE_STRIX_SECONDS"] = str(cfg.fake_scan_seconds)
        env["FAKE_STRIX_EXIT"] = str(cfg.fake_scan_exit_code)
        env["FAKE_STRIX_RUN_ID"] = cfg.fake_scan_run_id or f"fake-{spec.scan_id}"
        env["FAKE_STRIX_TARGET"] = spec.target
        env["FAKE_STRIX_GRANDCHILD"] = "1" if cfg.fake_scan_grandchild else "0"
        # 是否产出模拟的 strix 产物（agents.db / agents.json / run.json）。
        # 关掉它时扫描目录里什么都没有 —— 用来验证"没有产物时导入器不炸"。
        env["FAKE_STRIX_EVENTS"] = "1" if cfg.fake_scan_events else "0"
        # 真的把这两个透出去 —— 于是"凭据走 env 而不进 argv"这件事
        # 在替身上也能被验证（测试断言 argv 里没有它）。
        env["STRIX_LLM"] = spec.llm_model
        if spec.llm_api_key:
            env["LLM_API_KEY"] = spec.llm_api_key
        return env

    def launch(self, spec: LaunchSpec) -> LaunchResult:
        spec.run_dir.mkdir(parents=True, exist_ok=True)
        (spec.run_dir / "_fake_strix.py").write_text(_FAKE_SCRIPT, encoding="utf-8")

        log_handle = open(spec.log_path, "ab", buffering=0)  # noqa: SIM115
        try:
            process = subprocess.Popen(  # noqa: S603
                self.command(spec),
                cwd=str(spec.run_dir),
                env=self.env(spec),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **popen_kwargs(),
            )
        except OSError as exc:
            log_handle.close()
            raise RunnerUnavailable(f"启动替身进程失败：{exc}") from exc
        log_handle.close()

        return LaunchResult(
            pid=process.pid,
            run_dir=spec.run_dir,
            log_path=spec.log_path,
            command=tuple(self.command(spec)),
            process=process,
        )

    def detect_run_id(self, run_dir: Path) -> str | None:
        return StrixRunner().detect_run_id(run_dir)


_RUNNERS: dict[str, type] = {"strix": StrixRunner, "fake": FakeRunner}


def _register_optional_runners() -> None:
    """把**可选后端**登记进 `_RUNNERS`，惰性且只做一次。

    ## 为什么不能写在模块级那两个字典字面量里

    `agent_runner` 在模块顶层 `from app.scans.runner import LaunchResult,
    LaunchSpec, RunnerUnavailable` —— 而本模块如果在顶层反过来 import 它，
    就成环了（`scans.runner` → `scans.agent_runner` → `scans.runner`）。
    所以只能在这里延迟 import。

    ## 为什么失败不能抛（重要）

    `agent_runner` 会 import `app.kernel.runner`，那一串依赖（openai /
    httpx / …）在**只想用 strix 后端的部署**里可能压根没装全。那种部署
    照样要能跑 —— 所以这里**吞掉 import 异常**，只记一条 debug。
    真到用户选了 `agent` 后端而它没登记时，`get_runner` 会抛
    `RunnerUnavailable` 并列出可用项，那时才是有意义的报错时机。
    """
    if "agent" in _RUNNERS:
        return
    try:
        from app.scans.agent_runner import RUNNER_NAME, AgentRunner
    except Exception:  # noqa: BLE001 - 见上面「为什么失败不能抛」
        logger.debug("自研引擎后端不可用（import 失败），仅 strix/fake 可选", exc_info=True)
        return
    _RUNNERS[RUNNER_NAME] = AgentRunner


def get_runner() -> ScanRunner:
    """按配置返回执行后端。

    延迟构造而不是模块级单例：测试里要能改 `SUPEX_SCAN_RUNNER` 之后立刻生效，
    而模块级常量一旦导入就固定了 —— 那种测试会永远跑在 strix 后端上，
    也就是**真的去发起攻击**。
    """
    _register_optional_runners()
    cfg = get_settings()
    runner_cls = _RUNNERS.get(cfg.scan_runner)
    if runner_cls is None:
        raise RunnerUnavailable(
            f"未知的扫描执行后端：{cfg.scan_runner!r}（可选：{'、'.join(_RUNNERS)}）"
        )
    return runner_cls()
