"""单题 worker：起环境 → 沙箱内解题 → 产出候选 flag → 提交。

## ★ 三个架构决定，都在这里定死

### 1. 解题**循环跑在宿主进程**，只有工具**执行**落在沙箱里

任务原文是「沙箱内 LLM 解题循环」。实现上循环在宿主、执行在沙箱，
理由是**凭据**：

逆向题的解题过程就是"运行一个未知二进制"，而那个二进制可以读
`/proc/self/environ`、可以 `env`、可以把整个进程环境外带出去。
把 LLM API key 放进沙箱环境变量，等于把一把能烧钱的钥匙交给
一个**我们主动引进来的**不可信程序。而 litellm 网关的凭据本来
就活在宿主（`providers` 领域），没必要为了字面一致把它搬进沙箱。

隔离的实质一条没丢：**解题产生的所有副作用（执行、写文件、样本运行）
全部发生在沙箱容器内**，决策只是发生在外面。这与 strix 的做法
（决策在宿主、工具在沙箱）也是同构的。

### 2. 工具协议用**文本 JSON**，不用原生的 `tools` / `tool_calls`

三个理由，第二条是决定性的：

1. **一套协议覆盖所有 provider**。原生 tools 的格式在 OpenAI 兼容
   与 Anthropic Messages 之间**不一样**，写两套适配意味着两条路径
   都要测，而其中一条永远缺少真实环境的验证。
2. **自建网关/中转对 tools 的支持参差不齐**。不支持时的失败形态是
   "模型收到一堆不认识的参数然后胡说"，而不是一个干净的 400 ——
   那是本项目反复吃过亏的"工具说错话、模型信了"那一类。
3. 这个循环是**我们自己的**，协议由我们定，不需要与任何 SDK 对齐。

代价是解析脆弱 → 所以格式错误会**显式喂回给模型**让它重试
（见 `_format_reminder`），而不是静默地当成一次空回合。

### 3. 工具集**严格三个**：读附件、发 HTTP、沙箱内执行命令

这不是"暂时只有三个"，而是**能力边界**：

| 工具 | 在哪执行 | 为什么 |
| --- | --- | --- |
| `read_attachment` | 宿主（读已投进沙箱的那份） | 给 agent 一个**不用起 shell** 就能看题面的路径 |
| `http_request` | **宿主** | 见下 |
| `run_shell` | **沙箱容器内** | 真正的分析工作 |

`http_request` 在宿主执行是刻意的：**容器内的网络请求我们拦不住**
（agent 可以 `python -c 'import socket...'` 绕过任何包装）。
把 HTTP 请求收到宿主来，白名单（`guard_tool_url`）才是**能生效的**。

⚠️ 但这只是第一层。容器内的 `run_shell` 仍然可以发起任意连接 ——
所以第二层是 Docker 网络：默认 `--network none`（题目环境通过
宿主侧的 HTTP 工具访问），需要原始 TCP 的题型才放开。
两层的关系写在 `guard_tool_url` 与 `SandboxContainer._argv` 的注释里。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import PROJECT_ROOT, get_settings
from app.core.db import session_scope
from app.core.ids import new_id
from app.core.text import strip_nul
from app.core.timeutil import now_utc
from app.core.vocab import (
    DEFAULT_CTF2_MAX_TURNS,
    DEFAULT_CTF2_MAX_WRONG_SUBMISSIONS,
    Ctf2ChallengeStatus,
    Ctf2SandboxProfile,
    Ctf2SubmissionStatus,
    Ctf2SubmitMode,
)
from app.ctf2 import events as ctf2_events
from app.ctf2.client import (
    CTF2_ERROR_ALREADY_SOLVED,
    Ctf2EnvironmentTimeout,
    Ctf2Error,
    Ctf2ResponseError,
    assert_allowed_target,
)
from app.ctf2.models import (
    CTF2_OFFICIAL_HOSTS,
    Ctf2Challenge,
    Ctf2Environment,
    Ctf2SolveAttempt,
    Ctf2Submission,
)

logger = logging.getLogger("suvex.ctf2")

# ══════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════

# 镜像名。**与 `Ctf2SandboxProfile` 的取值逐字一致** ——
# 这里不再做一层"逻辑名 → 镜像名"的映射，因为两层映射会让
# "这个字段到底存的是什么"变成一个每次都要查的问题
# （与 vocab 里那条注释同一条理由）。
IMAGE_BASE = Ctf2SandboxProfile.BASE.value
IMAGE_REVERSE = Ctf2SandboxProfile.REVERSE.value

# CTF2 解题沙箱容器名的前缀。
#
# ★ 它不只是命名习惯 —— `sweep_orphan_sandboxes` **靠它来认领孤儿**
#   （启动时把带这个前缀的容器全删掉）。所以：
#   · 改这里也要保证别的东西不占用这个前缀；
#   · 容器名必须**永远**以它开头，否则泄漏的容器会攒下来没人清。
SANDBOX_NAME_PREFIX = "ctf2-"

# 沙箱所在的 Docker 网络。**独立网络而不是默认 bridge**：
# 默认 bridge 上的容器能互相看见，而"同时跑两道题"意味着
# A 题的样本能扫到 B 题的环境 —— 那是题目之间的越界。
CTF2_NETWORK = "suvex-ctf2"

# 宿主只读资源的容器内路径。与 Dockerfile 的 VOLUME 一致。
HOST_RESOURCES_PATH = "/opt/host-resources"
HOST_RESOURCES_ENV = "SUPEX_CTF2_HOST_RESOURCES"

# 沙箱内的工作目录（Dockerfile 里已建好且 chown 给 solver）。
WORK_DIR = "/work"
ATTACHMENT_DIR = f"{WORK_DIR}/attachments"
# ★ 跨容器存活的工作区在容器里的挂载点（v0.29）。
#
# 它是 `/work` 的**子目录**而不是 `/work` 本身 —— 理由见 `_argv` 里
# 那段说明（直接替换 /work 会让 attachments 子挂载失效）。
#
# 放在 /work 之下的好处是模型**不需要知道它存在**：它在 /work 里
# 正常工作，只是把值得留下的东西放到这个子目录。提示词里会说明。
PERSIST_DIR = f"{WORK_DIR}/persist"

# 单个 shell 命令的默认限时（秒）。300 秒是"足够跑一次爆破之外的
# 任何分析"的量级；再长会让一次卡死的命令拖掉整道题的预算。
DEFAULT_SHELL_TIMEOUT = 300

# ── 解题循环的预算 ────────────────────────────────────────
#
# ## ★ 40 → 80（v0.26）：因为旧值**根本没用满**
#
# 原来这里是 40，而 `MAX_HISTORY_TURNS` 是 24。两个数字放在一起才看得出
# 真正的问题：**第 24 轮之后模型就开始遗忘**（历史被裁成"题目 + 最近 24 轮"），
# 于是后 16 轮它在反复做已经做过的事，最后落一句"回合用尽"。
#
# 实测（2026-10-01，用户截图）：`[FlareOn3]GUI` 与 `WEB5[中等]` 两道题
# 都是「回合用尽（40 轮）仍未给出结论」。用户的第一反应是"40 轮太少" ——
# 对，但**只把 40 调大不会让它们解出来**，只会多绕 40 轮。
#
# 所以这一版做了三件事，缺一不可：
#   ① 轮数 40 → 80；
#   ② **动作纪要**（见 `_digest_message`）：被裁掉的历史不再凭空消失，
#      而是压成一行一条的"做过什么"，一直留在上下文里；
#   ③ **收尾提醒**（见 `_final_stretch_note`）：只剩几轮时把模型从
#      "继续探索"拉回"给结论"。
#
# 没有 ② 的话 ① 只是烧更多的钱；没有 ① 的话 ② 省下的上下文换不来
# 更多有效回合。三个一起才成立。
DEFAULT_MAX_TURNS = DEFAULT_CTF2_MAX_TURNS

#: 动作纪要保留多少条。
#:
#: ★ 这是"遗忘"的解药：每轮压成一行（`[t3] run_shell: unzip … → 输出…`），
#: 于是**再久远的探索也不会从上下文里消失**，只是变得简略。
#:
#: 120 行 × 约 160 字符 ≈ 19000 字符 —— 比 `MAX_TOOL_OUTPUT_CHARS` 大，
#: 但它换来的是"模型知道自己做过什么"，而那正是它不空转的前提。
MAX_DIGEST_LINES = 120

#: 还剩几轮时开始催收尾。
FINAL_STRETCH_TURNS = 5

# 「平台说这题已经被本队伍解出了」时给用户看的那句话。
#
# ★ 它必须同时回答三件事，否则用户会以为是我们坏了：
#   ① 发生了什么（平台侧已解出，不是我们失败）；
#   ② 为什么 suvex 不继续（我们不知道 flag，硬解会白烧一整轮 LLM，
#      而提交时平台只会回 ALREADY_SOLVED）；
#   ③ 用户能做什么。
#
# ⚠️ ③ 只写**真实存在**的动作。初稿里写了"可以用手动标记把它记进来"，
#    而全项目**没有**这个端点（`router.py` 里只有求解/提交/取消/释放）
#    —— 一句写着不存在功能的提示，比不提示更伤：用户会去找那个按钮。
#
# 实测出处：2026-10-01，`[HCTF 2018]WarmUp` 平台 `is_solved=true`
# 而本地是 `unsolved`（队友在网页端交的）；同一天向已解出的 hashcat
# 重复提交正确 flag，平台回 HTTP 400 `ALREADY_SOLVED`。
PLATFORM_ALREADY_SOLVED_MESSAGE = (
    "平台显示这道题已被本队伍解出（通常是队友在网页端提交的）。"
    "suvex 拿不到那个 flag，而再提交一次会被平台以 ALREADY_SOLVED 拒绝，"
    "所以这里不再重复解题，以免白跑一整轮 LLM。"
    "答案请在 CTF2 平台上查看；这道题在 suvex 里会一直显示为未解出 —— "
    "我们不会凭空编一个 flag 填进已解出的位置。"
)

# 单次 LLM 调用的超时。解题回合要模型想清楚，比探测宽松得多。
DEFAULT_LLM_TIMEOUT = 180.0
# ── LLM 调用的**连接层**重试 ──────────────────────────────
#
# 覆盖的是"抖了一下"这一类错误，4xx 与真正的协议错误不重试 ——
# 后者重试多少次都一样，只会把"key 配错了"拖成几十秒的等待。
#
# ⚠️ 为什么值得单独重试：一次瞬时抖动就能让**整道题**作废 ——
# 实测（2026-09-30）一个已跑到第 28 轮、正在跑 john 的解题过程，
# 因为一次 `ConnectError` 全部丢掉，而当时链路是通的（401 / 0.14s）。
# 重试成本是几百毫秒，不重试的代价是几十轮工具调用白跑。
#
# ## ★ `ReadError` 必须在这张表里（2026-10-12 实测补）
#
# 原先这里只有 `ConnectError` / `ConnectTimeout` / `ReadTimeout`，
# 而 `ReadError` 落到下面的 `except httpx.HTTPError` 分支被**直接抛出**
# —— 于是**一次读中断就结束整道题**。
#
# 实测（2026-10-12，`[FlareOn3]GUI`）：模型已经用 `monodis` 反汇编出
# 完整 IL、识别出这是 ConfuserEx 混淆、正在逐段分析方法表 ——
# 第 14 轮一次 `ReadError` 让这一切作废。日志原话：
#
#     CTF2 解题结束：turns=14 candidates=0 error=调用 LLM 失败（ReadError）：
#
# 而它**远没有跑满 80 轮预算**。对比上一次同一道题的失败
# （"回合用尽（80 轮）"）—— 那是能力问题，这次是**基础设施问题**。
#
# 语义上 `ReadError` 比 `ConnectError` **更该重试**：
#   · `ConnectError`   = 连不上 → 可能是地址/端口配错（重试无用）；
#   · `ReadError`      = 连上了、读响应时断了 → 服务端抖动或本地模型
#     过载（重试往往就好）。本地部署的模型（如 `10.39.16.9`）在长推理
#     下过载尤其常见，而这类部署正是本项目的典型用法。
#
# 所以把 `ReadError` 一并纳入重试列表。
_LLM_RETRY_ATTEMPTS = 4
_LLM_RETRY_BACKOFF = 1.5  # 秒，线性退避：1.5s / 3.0s / 4.5s
#: 值得重试的连接层异常。**加新成员时要问一句"重试是否可能成功"** ——
#: 这个元组是唯一的判据，改它等于改"哪些失败会作废整道题"。
_LLM_RETRYABLE = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.ReadError,  # ★ 2026-10-12 补，见上面那段实测
)
# 喂给模型的工具输出上限。**必须有**：一次 `cat` 一个 ELF
# 会把上下文瞬间吃满，而后果是后续所有回合都在"遗忘"。
MAX_TOOL_OUTPUT_CHARS = 8000
# 对话历史里保留的最近回合数（更早的做摘要/裁剪）。
MAX_HISTORY_TURNS = 24


# ══════════════════════════════════════════════════════════
# 镜像选择
# ══════════════════════════════════════════════════════════

# 会被判成"逆向"的分类值。**精确匹配**，见下面 `profile_for_category`。
_REVERSE_CATEGORIES = frozenset(
    {
        "reverse",
        "reversing",
        "rev",
        "re",
        "逆向",
        "逆向工程",
        "反汇编",
    }
)


def profile_for_category(category: str | None) -> str:
    """题目分类 → 沙箱镜像。

    ## ★ 必须**精确匹配**，不能子串匹配

    这不是风格问题，是一个会稳定出错的坑：`forensics`（取证）
    里含子串 `re`，而 `forensics` 与 `reverse` 恰好是**两个不同的镜像**。
    用 `"re" in category` 判断的话，所有取证题都会被送进逆向镜像 ——
    而症状不是报错，是"取证题也能跑"（因为 re 镜像是 base 的超集），
    于是**没人会发现**，直到某天有人为了缩小镜像把 re 里的东西删了。

    ## 认不出来时返回 base

    这是安全的方向：base 是超集里较小的那个，而**两个镜像都能跑
    非逆向题**（re 只是多了工具）。所以"猜错成 base"的代价是
    "某个逆向工具不在"，而那是**可见的失败**（命令 not found），
    比"猜错成 re"带来的一堆无用攻击面要好。
    """
    normalized = (category or "").strip().lower()
    if normalized in _REVERSE_CATEGORIES:
        return Ctf2SandboxProfile.REVERSE.value
    return Ctf2SandboxProfile.BASE.value


def image_for_profile(profile: str) -> str:
    """沙箱 profile → 镜像名。未知取值退回 base，并且**出声**。

    ⚠️ 这里的 `get(profile, BASE)` 与"静默退化"不同：schema 层已经
    挡住了非法取值，走到默认分支只可能是**数据库里有更旧/更新的枚举值**
    （迁移期间）。那时用 base 比让整道题起不来合理 —— 与
    `scans.runner._goal_clause` 的取舍完全一致。
    """
    if profile == Ctf2SandboxProfile.REVERSE.value:
        return IMAGE_REVERSE
    if profile != Ctf2SandboxProfile.BASE.value:
        logger.warning("未知的沙箱 profile %r，按 base 处理", profile)
    return IMAGE_BASE


# ══════════════════════════════════════════════════════════
# ★ 网络白名单 —— 独立的小函数，因为它是最容易被改坏的一处
# ══════════════════════════════════════════════════════════
#
# ## 为什么它值得单独一个函数，而不是写在工具分发里
#
# 读这段代码的人最可能做的事是"顺手让 agent 能访问一个地址"：
# 某个题要给一个 CDN、某个提示指向一个文档站、某个附件在别的域名上。
# 每一次这样的放宽都让这条边界松一点，而**没有一次会报错** ——
# 白名单宽了不会失败，只会安静地不再拦任何东西。
#
# 所以它被独立出来、带这段说明、并且有专门的测试
# （`test_guard_rejects_out_of_scope_urls`）。改它必须改测试，
# 而测试会强迫改它的人读一遍"允许的只有两类"。
#
# ## 允许的两类，以及为什么只有两类
#
# 1. **CTF2 官方域名** —— 平台的附件、API；
# 2. **本题的 access_url** —— 题目环境。
#
# ⛔ 不放行宿主机内网、不放行 `host.docker.internal`、
# 不放行任意公网地址。理由：题目**内容**是参赛者与出题人可控的
# （题面本身就可能写"请访问 http://内网地址/xx"），
# 而 agent 会照做。拿题目内容当白名单来源等于没有白名单。


def allowed_hosts(access_urls: Sequence[str]) -> frozenset[str]:
    """本题允许访问的主机名集合。

    ⚠️ **只从数据库里的 `access_urls` 取** —— 绝不要从题目文本、
    题面附件、或 agent 的输出里取。见上面那段说明。
    """
    hosts: set[str] = set()
    for url in access_urls or ():
        candidate = url if "://" in url else f"http://{url}"
        host = (urlsplit(candidate).hostname or "").strip().lower()
        if host:
            hosts.add(host)
    return frozenset(hosts)


def guard_tool_url(url: str, *, access_urls: Sequence[str]) -> None:
    """`http_request` 工具发请求前的**唯一**校验点。越界直接抛。

    抛而不是"返回一句拒绝"是刻意的：拒绝理由要**回到模型**（它会据此
    换思路），而抛异常让调用点无法忽略它 —— 一个返回 `None` 的校验
    会让"忘了判返回值"变成一次真实的越界请求，而那种 bug 不会报错。

    ## 为什么这里再叫一次 `client.assert_allowed_target`

    同一份规则（官方域名 + 本题地址）在客户端那一层已经有一份实现。
    这里**复用**而不是重写：两份白名单实现必然漂移，而漂移的方向
    通常是"这一份记得加、那一份忘了"。
    """
    try:
        assert_allowed_target(url, allowed_extra=tuple(allowed_hosts(access_urls)))
    except Ctf2Error as exc:
        # 包成同一个领域异常，但消息要能直接喂回模型 ——
        # 它需要知道"为什么不行"才能换路子，而不是反复试同一个地址。
        raise Ctf2Error(
            f"该地址不在允许范围内：{exc.message} 只允许访问本题的环境地址"
            f"与 CTF2 官方域名（{', '.join(CTF2_OFFICIAL_HOSTS)}）。"
        ) from exc


# ══════════════════════════════════════════════════════════
# Docker 子进程
# ══════════════════════════════════════════════════════════

# Windows 上不给子进程弹控制台窗口。与 `scans/runner.py` 的
# `_no_window()` 同一个值 —— 这里是本地一份小常量而不是 import
# 那个私有函数，因为它是**一个常量**，复制它的漂移风险为零，
# 而跨模块 import 私有名会把两个模块的耦合做实。
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class Ctf2SandboxError(Ctf2Error):
    """沙箱（Docker）层面的失败。"""


@dataclass(frozen=True, slots=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def combined(self, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
        """给模型看的输出：stdout 优先，stderr 附在后面，截断。"""
        parts = []
        if self.stdout.strip():
            parts.append(self.stdout)
        if self.stderr.strip():
            parts.append("[stderr]\n" + self.stderr)
        text = "\n".join(parts) if parts else "(无输出)"
        return _clip(text, limit)


def _base_env() -> dict[str, str]:
    """跑 docker 客户端所需的环境变量（白名单）。

    ★ **复用 `scans/runner.py` 的 `_base_env()`**，这一点是刻意的，
    值得解释为什么打破"不 import 私有名"的惯例：

    那个函数是**环境变量白名单的唯一实现处**，而它防的是一个具体的、
    严重的事故：把后端进程的环境整个交给一个接下来要执行任意命令的
    子进程 —— 其中 `SUPEX_SECRET_KEY` 能解开数据库里所有 provider 的
    API key 密文。白名单要是复制成两份，**漂移的那一次不会有任何症状**，
    只会静默地多传一个变量出去。

    换句话说：这里 import 私有名，比复制一份白名单更安全。
    """
    from app.scans.runner import _base_env as scans_base_env

    return scans_base_env()


def _run(argv: list[str], *, timeout: float) -> CommandResult:
    """跑一条命令。**不走 shell**（argv 是列表），异常不往外抛。

    ⚠️ 不往外抛不是"吞异常"：Docker 不可用与"命令返回非零"对调用方
    是同一件事（都是"这一步没成功"），而把它们分成两种异常会让
    调用点写两份处理。真正的诊断信息在 `stderr` 里，它会进日志与
    （截断后）进模型的上下文。

    超时用 `subprocess` 自己的 `timeout`：它杀掉的是 **docker 客户端**，
    而容器里的进程会继续跑 —— 这正是 shell 工具要在容器**内部**
    再套一层 `timeout` 的原因（见 `SandboxContainer.shell`）。
    """
    try:
        completed = subprocess.run(  # noqa: S603 - argv 是列表，没有 shell
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=_base_env(),
            creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        return CommandResult(127, "", f"找不到可执行文件：{argv[0]}")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"命令超时（{timeout:g} 秒）：{' '.join(argv[:3])} …")
    except OSError as exc:
        return CommandResult(126, "", f"无法执行 {argv[0]}：{exc}")
    return CommandResult(completed.returncode, completed.stdout or "", completed.stderr or "")


def sweep_orphan_sandboxes() -> int:
    """清掉**上一次运行**遗留的 CTF2 沙箱容器。返回清了几个。

    ## ★★ 为什么需要它：重启服务必然泄漏容器

    worker 是一个**线程**，而 Python 没有安全的杀线程手段（见
    `ctf2/scheduler.py` 的 docstring）。`systemctl restart` 让进程退出时，
    正在跑的线程被连带杀死 —— `finally` 里的 `sandbox.destroy()`
    **根本没机会执行**。

    实测（2026-10-01）：我在一轮部署里重启了几次服务，`docker ps`
    就留下了两个 `ctf2-*` 容器，而数据库里的环境记录已经是 `stopped`、
    题目也已经 `solved` —— **两边说法不一致，且没有任何界面能看出这件事**。

    调度器停止时确实会打一行 warning（"请检查 `docker ps`"），
    但那是**日志里的提醒**，而没有人会为了重启去看日志。比赛现场
    "调个并发就要重启"会反复踩它，几个小时的比赛下来足以把内存吃光。

    ## 为什么在**启动时**清，而不是停止时

    停止时清不掉：进程正在退出，而泄漏的原因恰恰是"那个线程没机会
    跑完自己的清理"。**启动时是可靠时机** —— 新进程里不存在任何
    "属于它的合法容器"（这一秒之前还没有 worker 跑起来），所以
    凡是叫 `ctf2-` 前缀的全是上一次的遗留。

    ⚠️ 前缀是**判据的全部**。所以容器的命名前缀不能被别的东西复用
    （`ctf2-{challenge_id[-12:]}-{随机}`，见 `_run_worker`）。

    ## 失败不阻断启动

    Docker 不可用、命令超时、权限不足 —— 任何一种都只是"这次没清成"，
    而**启动本身不该因此失败**（那会让一次清理失败升级成服务起不来）。
    所以只记 warning 并返回 0。
    """
    # ★ 锚定前缀（2026-10-10 修）。
    #
    # Docker 的 `--filter name=` 是**子串/正则匹配**，不是前缀匹配 ——
    # 而本函数的整个安全论证建立在"凡是叫 `ctf2-` 开头的都是遗留"上
    # （见 docstring：「前缀是判据的全部」）。
    # 不锚定的话，一个叫 `my-ctf2-helper` 的容器也会被 `docker rm -f` 删掉。
    #
    # 当前没有别的容器名含 `ctf2-`，所以这是**潜在**风险而非现网故障。
    # 但那条论证目前不成立，而"论证与实现不符"正是最该修的一类 ——
    # 下一个读到 docstring 的人会以为这里已经防住了。
    #
    # 同一个文件里的 `ensure_ctf2_network` 用的就是 `name=^{...}$` ——
    # 作者显然知道可以锚定，只是这里漏了。
    listed = _run(
        ["docker", "ps", "-aq", "--filter", f"name=^{SANDBOX_NAME_PREFIX}"],
        timeout=30,
    )
    if not listed.ok:
        logger.warning("清理遗留 CTF2 沙箱时无法列出容器：%s", listed.stderr.strip())
        return 0
    ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if not ids:
        return 0
    removed = _run(["docker", "rm", "-f", *ids], timeout=120)
    if not removed.ok:
        # 部分删掉也算成功了一半，但这里如实报"这次没清干净"。
        logger.warning("清理遗留 CTF2 沙箱失败：%s", removed.stderr.strip())
        return 0
    logger.warning(
        "清理了 %d 个上一次运行遗留的 CTF2 沙箱容器（重启服务时正在跑的 "
        "worker 线程杀不掉，它们的清理代码没机会执行）",
        len(ids),
    )
    return len(ids)


def ensure_ctf2_network() -> bool:
    """保证 `CTF2_NETWORK` 存在。已存在就什么都不做。返回是否可用。

    ## ★★ 为什么必须有这个函数（2026-10-01 实测）

    `CTF2_NETWORK` 原来**只被引用、从来没人创建**（`docker run --network`
    那两处）。而 Docker 不会替你建一个不存在的网络，它的报错是：

        failed to set up container networking: network suvex-ctf2 not found

    这条错误出现在**解题沙箱启动**那一步，用户看到的是"启动解题沙箱失败"，
    而真正的原因（一个本该由我们建的网络不存在）完全没出现在界面上。
    实测：整道题两次轮询全部倒在这一句上，`未解出`。

    ## 为什么不是"退回默认 bridge"就完事

    独立网络是**安全设计**，不是命名习惯（见 `CTF2_NETWORK` 的说明）：
    默认 bridge 上的容器能互相看见，而"同时跑两道题"意味着 A 题的样本
    能扫到 B 题的环境 —— 那是题目之间的越界。所以这里要的是**把网络建出来**，
    不是把隔离降级掉。

    ## 为什么幂等（先查再建）

    `docker network create` 对同名网络会报错（`already exists`），
    而调用点（服务启动）每次重启都会跑一遍。所以必须先查。
    查与建之间有理论竞态（另一个进程同时建），但那只会让 `create` 失败，
    而失败后网络其实已经存在 —— 所以**失败也要复查一次**，
    复查到了就算成功。

    ## 失败不阻断启动

    与 `sweep_orphan_sandboxes` 同一条理由：一次准备失败不该升级成
    服务起不来。返回 `False` 只记 warning，真正的失败会在起容器时
    以原来的方式暴露出来（那时报错至少有了上下文）。
    """
    listed = _run(["docker", "network", "ls", "--filter", f"name=^{CTF2_NETWORK}$", "--format", "{{.Name}}"], timeout=30)
    if listed.ok and CTF2_NETWORK in listed.stdout.split():
        return True

    created = _run(["docker", "network", "create", CTF2_NETWORK], timeout=60)
    if created.ok:
        logger.info("已创建 CTF2 沙箱网络 %s（解题容器靠它与其他题目隔离）", CTF2_NETWORK)
        return True

    # ★ 竞态复查：`create` 失败未必是坏事 —— 可能是另一个进程刚建好。
    #   不复查的话，这里会为一件**已经成立**的事记一条 warning。
    again = _run(["docker", "network", "ls", "--filter", f"name=^{CTF2_NETWORK}$", "--format", "{{.Name}}"], timeout=30)
    if again.ok and CTF2_NETWORK in again.stdout.split():
        return True

    logger.warning(
        "无法创建 CTF2 沙箱网络 %s，解题沙箱将起不来：%s",
        CTF2_NETWORK,
        (created.stderr.strip() or "未知原因"),
    )
    return False


def ensure_docker_available() -> None:
    """起容器前的自检。

    ⚠️ 只检查 `docker version` 能跑通，**不检查镜像在不在**。
    镜像缺失是一条有明确报错的路径（`docker run` 会说
    "Unable to find image ... locally" 或拉取失败），我们自己提前查一次
    只会多出"我们以为没有、其实有"的可能 —— 与 strix runner 那条
    "只检查可执行文件在不在，不检查 Docker"是同一条理由。
    """
    result = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=20)
    if not result.ok:
        tail = result.stderr.strip().splitlines()
        raise Ctf2SandboxError(
            f"Docker 不可用，无法启动解题沙箱。（{tail[-1] if tail else '未知原因'}）"
        )


class SandboxContainer:
    """一个题目沙箱容器。

    ## 生命周期

    ```
    start()  ──  docker run -d  ──►  exec()/shell() 多次  ──►  destroy()
    ```

    `destroy()` 必须在 **`try/finally`** 里调用（见 `_run_worker`）：
    一个没被销毁的容器会一直占着 CPU/内存/磁盘，而**没有任何界面能看到它**
    —— 只有 `docker ps` 能看到。连续跑几十道题之后，宿主会被
    一堆没人认领的容器拖垮，而排查者完全想不到去看 docker。
    """

    def __init__(
        self,
        *,
        name: str,
        image: str,
        network_isolated: bool = True,
        cpus: int | None = None,
        memory_mb: int | None = None,
        host_resources: Path | None = None,
        attachments_dir: Path | None = None,
        workspace_dir: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.image = image
        self.network_isolated = network_isolated
        self.cpus = cpus
        self.memory_mb = memory_mb
        self.host_resources = host_resources
        # ★ 题目附件目录：**挂载**进容器（`-v …:ro`），不用 `docker cp`。
        #
        # 为什么不能 cp：容器是 `--read-only` 起的，而 `docker cp` 要往
        # rootfs 的临时层写字节 —— 直接失败：
        #   "Error response from daemon: container rootfs is marked read-only"
        #
        # 挂载还顺带贴合语义：附件是只读材料。
        self.attachments_dir = attachments_dir
        # ★★ 持久工作区（v0.29，2026-10-12 补）—— **读写**挂载的宿主目录。
        #
        # ## 它解决什么
        #
        # `/work` 是 tmpfs，容器一停内容全没（见 `_argv` 里那段说明）。
        # 而"续解"（用户点"接着上次跑"）只从 transcript 还原
        # **命令 + 输出前 140 字** —— 于是：
        #
        #   · 解压出来的题目文件没了；
        #   · 反编译产物、解密结果、爆破出的口令没了；
        #   · 模型自写的脚本（C# harness、Python 分析器）没了。
        #
        # 实测（2026-10-12，`[FlareOn3]GUI`）：模型跑满 78 轮，写了 12 个
        # C# harness、patch 过 CLR 元数据、还原出内嵌程序集 —— 这些**全部
        # 随容器消失**。续解时它得重新解压、重新反编译，只是不用重新
        # "决定跑哪条命令"。
        #
        # ## 边界（这条比功能本身重要）
        #
        # 挂载点是**读写**的，而容器里跑的是题目给的不可信内容。
        # 所以：
        #   · 路径由**服务端**按 challenge_id 生成，不含任何题目可控的字符串；
        #   · 目录在宿主上位于 `scan_root/_ctf2_workspaces/`（与产物同级，
        #     便于按保留期一起清）；
        #   · ⚠️ 它**不挂载宿主任何敏感路径** —— 就是一个空的临时目录。
        #
        # 代价如实记：容器内被攻下之后，它能往这个目录写东西，而那些
        # 字节会留在宿主上。这是"保留现场"换来的必然代价，所以目录
        # 里**只该有解题产物**，且要能整目录删掉（见 `sweep`）。
        self.workspace_dir = workspace_dir
        # ⚠️ `extra_env` **绝不允许**承载凭据。它存在的唯一用途是
        # 传一些无害的开关（比如 `PWNLIB_NOTERM=1`）。
        # 类型上没法强制这一点，所以这条规矩写在这里 + 有测试盯着
        # （`test_extra_env_must_not_carry_credentials`）。
        self.extra_env = extra_env or {}
        self._started = False
        self.container_id: str | None = None

    def _argv(self) -> list[str]:
        """构造 `docker run` 的参数。

        ## 逐个参数的"为什么"

        | 参数 | 挡什么 |
        | --- | --- |
        | `--rm` | 容器退出即删。配合 `destroy()` 是双保险（进程崩了也不会留残骸） |
        | `--name` | 固定名字，让 `docker rm -f` 在拿不到 id 时仍然可用 |
        | `--network none` | ★ 默认**完全无网络**。题目环境走宿主侧 HTTP 工具 |
        | `--read-only` | 根文件系统只读 → 被攻下的进程无法持久化 |
        | `--tmpfs /work` | /work 可写但在内存里，容器一停就消失 |
        | `--cap-drop ALL` | 丢掉全部 Linux capabilities |
        | `--security-opt no-new-privileges` | 挡 setuid 提权 |
        | `--pids-limit` | 挡 fork 炸弹（一次 `:(){ :|:& };:` 能把宿主拖死） |
        | `--memory` / `--cpus` | 配额。LLM 让它跑 `hashcat` 会吃满机器 |
        | `-v ...:ro` | ★ 只读挂宿主资源，绝不含可执行目录 |

        ⛔ **绝不**出现的三个：`--privileged`、
        `-v /var/run/docker.sock:...`（挂了等于容器能控制宿主 Docker）、
        `--network host`。
        这三条中任何一条都会让上面全部努力归零，所以它们被写在这张表里
        而不是留给"常识"。
        """
        argv = [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            self.name,
            # ★ 默认完全无网络。
            #
            # ⚠️ **这是本模块最重要的一个安全参数**，也是最容易被
            # "为了方便"改掉的一个。它成立的**前提**是：agent 的所有
            # HTTP 交互都走宿主侧的 `http_request` 工具（那里有白名单），
            # 而不是容器内直接连。
            #
            # 需要原始 TCP 的题型（pwn / 网络 misc）必须放开 —— 那时
            # 白名单就只剩**应用层**那一层，这是如实记录的残余风险，
            # 见 `guard_tool_url` 的说明。
            "--network",
            CTF2_NETWORK if not self.network_isolated else "none",
            "--read-only",
            # /work 可写但在内存里：容器一停，题目样本与产出全部消失。
            # `uid/gid` 必须给：tmpfs 默认属主是 root，而容器里是非 root
            # （Dockerfile 里 `USER solver`），一写就 Permission denied。
            "--tmpfs",
            f"{WORK_DIR}:rw,exec,size=512m,uid=1000,gid=1000",
            # ★ /tmp 也要给（2026-10-10 补）。
            #
            # ## 为什么它和 /work 是两件事
            #
            # 容器是 `--read-only` 启动的，所以除了显式 tmpfs 之外**处处只读**。
            # 而相当多的工具默认往 /tmp 或 $TMPDIR 写：
            #   · `cmake` / `gcc` 的中间产物；
            #   · `john` 的 pot 文件；
            #   · `hashcat` 的 session / restore 文件。
            #
            # 不给的失败形态是某个工具报 `Permission denied` —— 那看起来像
            # **镜像里缺东西**，而不是"挂载少给了一个目录"，排查方向会跑偏。
            #
            # ⚠️ 这与安全无关：`exec` 已经在 /work 上放开了，
            #    多一个可写 tmpfs 不增加攻击面（本来就是一次性的、容器停即消失）。
            #    它是**可用性**修复。
            "--tmpfs",
            "/tmp:rw,exec,size=256m,uid=1000,gid=1000",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "512",
        ]
        if self.memory_mb:
            argv += ["--memory", f"{self.memory_mb}m"]
        if self.cpus:
            argv += ["--cpus", str(self.cpus)]
        # 只读挂宿主资源（已定设计决策 2）。
        #
        # ⚠️ 只在宿主路径**存在**时挂：挂一个不存在的路径会创建空目录，
        # 而"资源目录是空的"与"资源没配"在容器里长得一样，
        # 排查时会往"是不是 mount 没生效"的方向跑。
        if self.host_resources is not None and self.host_resources.is_dir():
            argv += ["-v", f"{self.host_resources}:{HOST_RESOURCES_PATH}:ro"]
        # 题目附件（只读）。与上面同一条理由：只在目录**存在**时挂 ——
        # 挂一个不存在的路径，Docker 会替我们建一个空目录，而"空目录"
        # 与"没挂上"在容器里长得一模一样。
        #
        # ⚠️ 它位于 `/work` 这个 tmpfs **之下**。Docker 按路径深度先后
        #    挂载（`/work` 先、`/work/attachments` 后），所以子挂载不会
        #    被父 tmpfs 冲掉 —— 这也正是 Dockerfile 里那句
        #    `mkdir -p /work/attachments` 在运行时失效的原因。
        if self.attachments_dir is not None and self.attachments_dir.is_dir():
            argv += ["-v", f"{self.attachments_dir}:{ATTACHMENT_DIR}:ro"]
        # ★★ 持久工作区（读写）。见 `workspace_dir` 的说明。
        #
        # ⚠️ 挂到 `/work/persist`（**不是** `/work` 本身）：
        #    `/work` 是 tmpfs，直接挂载会把它整个换掉 —— 而那会连带
        #    让 `/work/attachments` 那个子挂载失效（Docker 按路径深度
        #    挂载，父层被替换后子层的可见性不再可靠）。
        #    挂一个**子目录**则两者互不干扰：tmpfs 提供可写的工作区，
        #    这个子目录提供跨容器存活的那部分。
        #
        # ⚠️ 目录不存在时**不挂**（与 host_resources / attachments 同一条
        #    理由）：挂一个不存在的宿主路径，Docker 会替我们建一个
        #    空目录 —— 而"空目录"与"没挂上"在容器里长得一模一样。
        #    这里由调用方先 mkdir（见 `_workspace_dir`）。
        if self.workspace_dir is not None and self.workspace_dir.is_dir():
            argv += ["-v", f"{self.workspace_dir}:{PERSIST_DIR}:rw"]
        for key, value in self.extra_env.items():
            argv += ["-e", f"{key}={value}"]
        argv += [self.image, "tail", "-f", "/dev/null"]
        return argv

    def start(self) -> None:
        ensure_docker_available()
        # ★ 起容器前再保证一次网络（幂等，已存在时是一次 `docker network ls`）。
        #
        # 光靠"服务启动时建一次"挡不住这件事：网络是外部状态，
        # 服务跑着的时候它可能被 `docker network prune` 或人手删掉 ——
        # 而那时解题会退回同一个看不懂的
        # `network suvex-ctf2 not found`。代价是一次查询，换掉一整类误报。
        #
        # 只在**要联网**时查：`network_isolated` 的容器走 `--network none`，
        # 与这个网络无关，没必要为它付这次查询。
        if not self.network_isolated:
            ensure_ctf2_network()
        result = _run(self._argv(), timeout=120)
        if not result.ok:
            raise Ctf2SandboxError(
                f"启动解题沙箱失败：{result.stderr.strip() or result.stdout.strip() or '未知原因'}"
            )
        self.container_id = result.stdout.strip()[:64] or None
        self._started = True
        logger.info(
            "CTF2 沙箱已启动 name=%s image=%s network=%s",
            self.name,
            self.image,
            "none" if self.network_isolated else CTF2_NETWORK,
        )

    # ── 在容器里做事 ──────────────────────────────────────

    def exec(self, argv: list[str], *, timeout: float = 120) -> CommandResult:
        """在容器里跑一条命令（不经 shell）。"""
        return _run(["docker", "exec", "-i", self.name, *argv], timeout=timeout)

    def shell(self, command: str, *, timeout: float = DEFAULT_SHELL_TIMEOUT) -> CommandResult:
        """在容器里跑一条 shell 命令。**这是 `run_shell` 工具的实现。**

        ## 命令内容不做任何转义或净化 —— 这是正确的

        agent 要执行的命令就是**题目的解法**（`python3 solve.py`、
        `r2 -A ./chall`、`gdb -batch ...`）。对它做"净化"只会
        把题做不出来，而不会提高一点安全性 —— 安全性来自容器隔离，
        不来自命令字符串检查（这条与 `scans/uploads.py` 里那句
        "真实边界在沙箱隔离上，不在文件校验上"是同一条原则）。

        ## ★ 但**限时**必须在容器内部做

        `subprocess` 的 timeout 杀的是 `docker exec` 这个**客户端进程**，
        而容器里的命令会继续跑（Docker 不会因为 exec 的客户端断开就
        杀掉目标进程）。后果是：一个 `while true; do ...; done`
        会永远占着沙箱的 CPU，而宿主侧看起来"那次调用已经超时结束了"。

        所以叠一层容器内的 `timeout --signal=KILL`。
        `--signal=KILL` 而不是默认的 TERM：一个正在跑未知样本的进程
        完全可能捕获/忽略 TERM（那正是逆向题里样本的常见行为），
        而这里没有任何需要优雅退出的东西（容器马上会被销毁）。

        ## `bash -lc` 的 `-l`

        ⚠️ **这一段原来的说法是错的，已按实测改正。**

        原文写的是"`-l` 让 `/etc/profile` 生效，于是镜像里装的 venv 会在
        PATH 上；不加的话 `python3` 走的是系统 python3（没有 pwntools）"。
        实测结果**正好相反**：

        ```
        非 login：PATH=/opt/venv/bin:…                           → python3 = venv 的 → import pwn ✓
        login   ：PATH=/home/solver/.local/bin:/usr/local/sbin:… → python3 = /usr/bin/python3
                                                                 → ModuleNotFoundError: No module named 'pwn'
        ```

        kali-rolling 的 `/etc/profile` **重置** PATH，把 Dockerfile 里
        `ENV PATH="$VIRTUAL_ENV/bin:$PATH"` 挤了出去。也就是说：如果只靠
        `-l`，解题时 `python3 solve.py` 会因为 import 不到 pwntools 而失败 ——
        而那看起来像"镜像里没装这个包"，方向完全错。

        ★ 两处一起修（刻意的第二处表达）：
        · 镜像侧：Dockerfile 往 `/etc/profile.d/00-venv.sh` 写了 PATH，
          于是**任何** login shell 都能看到 venv；
        · 这里：保留 `-l`（它仍然有用 —— `/etc/profile` 会设好 locale
          与其它工具的环境），但**不再假定 PATH 由它保证**。
        """
        return self.exec(
            ["timeout", "--signal=KILL", str(int(timeout)), "bash", "-lc", command],
            timeout=timeout + 30,
        )

    def copy_in(self, host_path: Path, container_path: str) -> CommandResult:
        """把宿主上的文件送进容器（题目附件）。

        ## ★ 必须先建**父目录**：`--tmpfs /work` 把镜像里那层盖掉了

        Dockerfile 里确实有 `mkdir -p /work/attachments`，但容器是用
        `--tmpfs /work` 起的（见本类 docstring 的参数表）—— tmpfs 是
        一次**新的挂载**，镜像里那一层目录在运行时**不存在**。

        而 `docker cp` 到不存在的父目录会直接失败。失败本身还好，
        真正致命的是它以前**没人看**（调用方忽略返回值）。

        实测后果（2026-09-29，hashcat 题）：模型在容器里
        `ls /work/attachments` 得到 "No such file or directory"，
        于是退化成用 `read_attachment` **一次 32 字节**地啃那个
        122KB 的 zip，40 轮耗尽仍未收敛 —— 日志上看起来像"模型太笨"，
        而错在这一行。
        """
        parent = container_path.rsplit("/", 1)[0] or "/"
        if parent != "/":
            self.shell(f"mkdir -p {parent}")
        return _run(
            ["docker", "cp", str(host_path), f"{self.name}:{container_path}"], timeout=120
        )

    def destroy(self) -> None:
        """销毁容器。**必须幂等**（`finally` 里会调，而它可能已经死了）。

        `-f` 而不是温和的 `stop`：容器里可能跑着一个故意忽略 TERM 的
        样本（逆向题常态），而 `stop` 会等 10 秒超时再杀 —— 那个
        10 秒乘以几十道题就是几分钟的纯等待，而没有任何收益：
        容器里没有任何需要优雅关闭的状态。
        """
        if not self._started and not self.name:
            return
        result = _run(["docker", "rm", "-f", self.name], timeout=60)
        self._started = False
        # 容器已经不存在（被别人清了、或 --rm 已经生效）不算错误。
        # 把它当错误会让 finally 里的销毁动作在正常路径上也抛异常，
        # 而那会**盖住真正的异常** —— 于是用户看到的是"销毁失败"，
        # 而真实原因是模型跑挂了。
        if not result.ok and "No such container" not in (result.stderr or ""):
            logger.warning("销毁 CTF2 沙箱 %s 时出错：%s", self.name, result.stderr.strip())


# ══════════════════════════════════════════════════════════
# LLM 网关
# ══════════════════════════════════════════════════════════
#
# ★ 走**现有** provider 配置（已定设计决策 3：不新接模型）。
#
# 凭据在宿主内存里、只进请求头、不进容器的环境变量 —— 见模块 docstring
# 第 1 条。这里复用 `providers/llm.py` 的协议判定（`is_anthropic` /
# `build_endpoint` / `resolve_base_url`），所以"本地模型补 /v1"、
# "Anthropic 走 Messages API"这些既有规则不会在这里分叉出第二份。


@dataclass(frozen=True, slots=True)
class LlmReply:
    text: str
    # 推理型模型会把产出分成 reasoning + content；两个都可能是我们
    # 需要的那段（见 providers/llm.py 里那段实测表）。
    source: str
    raw: dict[str, Any] = field(default_factory=dict)


class LlmGateway:
    """解题循环用的对话客户端。**非流式**。"""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        label: str = "",
        kind: str = "cloud",
        timeout: float = DEFAULT_LLM_TIMEOUT,
        max_tokens: int = 4096,
    ) -> None:
        from app.providers.llm import build_endpoint, is_anthropic, resolve_base_url

        resolved = resolve_base_url(base_url, label)
        if not resolved:
            raise Ctf2Error(
                "没有可用的 LLM 接入点：provider 的 baseUrl 为空，"
                "且无法从名称推断官方默认地址。请先在设置里配置 provider。"
            )
        endpoint = resolved if "://" in resolved else f"https://{resolved}"
        self.anthropic = is_anthropic(endpoint)
        self.url = build_endpoint(endpoint, anthropic=self.anthropic, local=kind == "local")
        self.model = model
        # ⚠️ 这个类**故意没有 `__repr__`**：默认的 object repr 只给地址，
        # 而任何自定义 repr 都有把 `_api_key` 带进去的风险。
        # 需要打日志时请用 `self.url` 与 `self.model`。
        self._api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens

    def _headers(self) -> dict[str, str]:
        from app.providers.llm import ANTHROPIC_VERSION

        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if not self._api_key:
            return headers
        if self.anthropic:
            headers["x-api-key"] = self._api_key
            headers["anthropic-version"] = ANTHROPIC_VERSION
        else:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _body(self, messages: list[dict[str, str]], *, system: str) -> dict[str, Any]:
        if self.anthropic:
            # Messages API 的 system 是**顶层字段**，不是一条 message。
            # 放进 messages 会得到 400。
            return {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "system": system,
                "messages": messages,
            }
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "system", "content": system}, *messages],
        }

    async def chat(self, messages: list[dict[str, str]], *, system: str) -> LlmReply:
        from app.core.crypto import redact
        from app.providers.llm import _extract_reply  # noqa: PLC2701 - 见下

        # ⚠️ 这里 import 了 `providers/llm.py` 的私有 `_extract_reply`。
        #
        # 与 `_base_env` 那次 import 同一个理由，但要说准确：它编码的是
        # **实测出来的**一条知识（推理型模型的 `reasoning_content`、
        # Anthropic 的 content blocks 形状）。复制一份的话，上游哪天
        # 补一种新形状，这里不会跟着改，而症状是"某个模型永远产出空"——
        # 那正是上游当初踩过、并在注释里详细记下来的那个坑。
        #
        # ── ★ 瞬时网络错误必须重试，否则一次抖动就毁掉整道题 ──
        #
        # 实测（2026-09-30）：一次 `ConnectError: All connection attempts
        # failed` 让一个已经跑到第 28 轮、正在跑 john 的解题过程**整个作废**
        # （`error=调用 LLM 失败`）。而当时 Kali 到 `api.deepseek.com`
        # 是通的（401 / 0.14s）—— 纯粹是一次抖动。
        #
        # 代价极不对称：重试的成本是几百毫秒，丢掉的是几十轮已经做出来的
        # 工作（那些工具调用真的在沙箱里跑过，重新来过要再花几分钟）。
        #
        # ⚠️ 只重试**连接层**错误（连不上、超时、读中断）。
        #    4xx 不重试 —— 那是请求本身有问题，重试多少次都一样，
        #    而且会把一个"key 配错了"拖成几十秒的等待。
        last_error: Exception | None = None
        response: httpx.Response | None = None
        for attempt in range(_LLM_RETRY_ATTEMPTS):
            if attempt:
                await asyncio.sleep(_LLM_RETRY_BACKOFF * attempt)
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self.timeout)
                ) as client:
                    response = await client.post(
                        self.url,
                        json=self._body(messages, system=system),
                        headers=self._headers(),
                    )
                break
            except _LLM_RETRYABLE as exc:
                last_error = exc
                logger.warning(
                    "调用 LLM 连接失败（%s，第 %d/%d 次）：%s",
                    type(exc).__name__,
                    attempt + 1,
                    _LLM_RETRY_ATTEMPTS,
                    redact(str(exc))[:120],
                )
            except httpx.HTTPError as exc:
                # 其他协议层错误（畸形响应、写中断等）不重试：
                # 它们不像"抖了一下"，重试通常只是把同一个错误再撞一遍。
                raise Ctf2Error(
                    f"调用 LLM 失败（{type(exc).__name__}）：{redact(str(exc))[:200]}"
                ) from exc
        if response is None:
            raise Ctf2Error(
                f"调用 LLM 失败（{type(last_error).__name__}）："
                f"{redact(str(last_error))[:200]}（已重试 {_LLM_RETRY_ATTEMPTS} 次）"
            ) from last_error

        try:
            parsed = response.json()
        except ValueError:
            parsed = None

        if response.status_code >= 400:
            raise Ctf2Error(
                f"LLM 拒绝了请求（HTTP {response.status_code}）："
                f"{redact(str(parsed))[:200] if parsed else redact(response.text)[:200]}"
            )

        text, source = _extract_reply(self.anthropic, parsed)
        return LlmReply(text=text, source=source, raw=parsed if isinstance(parsed, dict) else {})


# ══════════════════════════════════════════════════════════
# 系统提示词
# ══════════════════════════════════════════════════════════

PROMPT_PATH = PROJECT_ROOT / "prompts" / "ctf2_system.md"


def load_system_prompt() -> str:
    """读 solver 的系统提示词。

    ⚠️ 文件不在时**抛错**，不用内置兜底。

    兜底听起来友好，但它会让"提示词文件漏进部署包"这件事
    **完全无声**：系统照常跑，只是 agent 少了那份纪律（不越界、
    不爆破、不编造），而产出的质量下降没有任何信号。
    抛错会让部署当场发现。
    """
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise Ctf2Error(
            f"读不到解题提示词 {PROMPT_PATH}。它必须随部署一起分发 —— "
            "缺了它 agent 的行为纪律（不越界、不爆破、不编造）就没了约束。"
        ) from exc


# ══════════════════════════════════════════════════════════
# flag 校验与去重
# ══════════════════════════════════════════════════════════

# flag 的形状。与 `scans/flag.py` 的 `_FLAG_TOKEN` 是**两个不同的用途**，
# 所以各写一份（那份是"从自由文本里捞候选"，这份是"校验一个已捞到的候选"）：
#
#   · 前缀：字母开头，最多 32 个字母数字下划线（`flag` / `DASCTF` / `HTB`）；
#   · 正文：1~200 个字符，**不含花括号**（嵌套花括号基本只出现在注入尝试
#     或模型胡说里）。
#
# ★★ 正文里**允许空格** —— 这一条是被一次真实事故改回来的。
#
# 实测（2026-10-01，BUUCTF「Url编码」）：模型完整解出了题、给出
# `flag{and 1=1}`（confidence: high），而**这一行正则把答案丢了**。
# 旧写法是 `[^{}\s]{1,200}`，那个 `\s` 把空格排除在外，于是这个真 flag
# 被判"形状不合法"，候选被 `extract_candidates` 静默丢弃（只记一条
# info 日志），题目最后以 `failed` 收场、**一次提交都没发出去**。
#
# 它正是下面 `validate_flag` 的 docstring 自己警告过的失败模式：
# 本地拦下一个真 flag，用户完全没有补救路径 —— 系统连试都没试。
# 而这道题的 flag 正文里就是有一个空格。
#
# 所以判据收紧到只针对**花括号**：嵌套花括号确实可疑（正常 flag 不会有），
# 而空格是题目内容的一部分，不是异常信号。
#
# ⚠️ 前缀**不写死**成 `flag`：CTF2 上是 DASCTF 系列，真题 flag 前缀
# 可能是 `DASCTF{...}`。写死会让所有真题被判为格式错误 ——
# 而那是最坏的一种失败（真实的 flag 被自己的校验挡在门外）。
#
# ⚠️ 前端 `src/data/ctf2.ts` 的 `FLAG_PATTERN` 与这一行**逐字一致**，
# 由 `scripts/verify_ctf2_parity.py` 做机器比对。
FLAG_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}\{[^{}]{1,200}\}$")

# ★★ v0.21：`FLAG_PATTERN` 的**语义降级了**。
#
# 它现在只是一个**分类器** —— 回答「这个字符串**看起来像** flag 吗」，
# **不再决定一个答案能不能提交**。
#
# 为什么要拆：把"识别形状"和"决定收不收"绑在同一个正则上，在 11 月比赛的
# 规则面前直接失效。赛事明确写了提交物可以是
#
#     flag、**命令行、文件名、时间戳、IP信息**等
#
# 而旧实现要求答案必须匹配 `前缀{正文}` —— 于是一条正确的命令行、一个 IP、
# 一个时间戳，**在本地就被判成"格式不合法"丢掉了**，用户完全没有补救路径。
# （同类的 bug 已经真实发生过一次：`flag{and 1=1}` 被空格规则拒掉，见上。）
#
# 现在的分工：
#   · 能不能提交 → `validate_answer`（判据刻意很松，只拦明显不可能的）
#   · 像不像 flag → 本正则，留给展示、日志与告警

# 占位符正文：只有省略号/星号/下划线/问号/连字符/点。
# 判据与理由照搬 `scans/flag.py` —— 那条是**回填时真实撞到**的
# （模板里的 `flag{...}` 被当成"拿到了 flag"存进库）。
#
# ⚠️ 它**只在"答案长得像 flag"时才适用**（见 `validate_answer`）——
#    否则 `...` 这种答案会被误判成占位符而丢掉。
_PLACEHOLDER_BODY = re.compile(r"^[\s.。*＊_?？\-—]+$")

# 答案的长度上限。
#
# 平台 submit 请求体里 `flag` 字段的 `maxLength` 是 4096（见 OpenAPI）。
# 我们收在 1000 —— 远低于平台上限，但足够容纳任何真实答案：一条命令行、
# 一个路径、一个时间戳、一个 IP、一个 flag，都在 200 字符以内。
# 留 5 倍余量是为了不误伤长哈希或长文件名。
MAX_ANSWER_LENGTH = 1000


class AnswerFormatError(Ctf2Error):
    """一个**答案**不合法：空的、超长的、或纯占位符。

    ⚠️ v0.21 起它**不再**表示"flag 形状不对" —— 形状不再是拒绝的理由。
    见 `validate_answer`。
    """


# 旧名，保留以便既有的 `except FlagFormatError` 继续工作。
# 两者是**同一个类**（不是继承），所以 `except FlagFormatError` 能捕获
# 新代码抛出的 `AnswerFormatError`，反之亦然。
FlagFormatError = AnswerFormatError


def _safe_text(text: str | None, limit: int = 500) -> str:
    """写进 `last_error` 这类**文本列**之前必须过一遍。

    与 `events._emit` 是同一个理由（见那里的实测记录）：PostgreSQL 的
    `text` 存不了 NUL 字节，插进去是硬错误，而那个错误会**掀掉整个
    worker** —— 一道本来解得下去的题，因为一句错误信息写不进去而崩。

    ⚠️ `last_error` 拿到的经常是**模型与工具的原文**：
    `outcome.error`、worker 的异常字符串。而工具输出里出现 NUL 是常态
    （解二进制附件、`strings` 打出来的东西、被 `utf-16le` 解过的文件）
    —— 2026-10-01 那次崩溃就是 `run_shell` 的 stdout 带 NUL。
    """
    return strip_nul(text or "")[:limit]


def normalize_flag(value: str) -> str:
    """去掉首尾空白、包裹引号/反引号，以及 NUL 字节。

    模型经常把答案用反引号或引号包起来（Markdown 习惯），
    而那会让一次**正确的**提交被平台判错 —— 白白扣一次额度。

    ★ NUL 也要在这里删掉：PostgreSQL 的 text 存不了它，插进去是硬错误。
    放在这里是因为本函数是答案的**唯一入口**（候选、确认、提交三条路径
    都过它），见 `app/core/text.py`。

    ⚠️ 名字里的 `flag` 是历史遗留 —— v0.21 起它的处理对象是**任意答案**
    （命令行、IP、时间戳都走它）。改名会波及太多调用点，而这个函数的
    行为本来就与"是不是 flag"无关，所以留着名字、改掉注释。
    """
    text = strip_nul(value or "").strip()
    for quote in ("`", "'", '"'):
        if len(text) >= 2 and text.startswith(quote) and text.endswith(quote):
            text = text[1:-1].strip()
    return text


def validate_answer(value: str) -> str:
    """校验并归一化一个**答案**。不合法就抛 `AnswerFormatError`。

    ## ★★ v0.21：判据从"像不像 flag"改成"是不是明显不可能"

    旧实现（`validate_flag`）要求答案匹配 `FLAG_PATTERN`（`前缀{正文}`）。
    那在 11 月比赛的规则面前**直接失效** —— 赛事明确写了提交物可以是

        flag、**命令行、文件名、时间戳、IP信息**等

    于是 `cat /flag`、`192.168.1.1`、`2026-11-15T13:04:19Z` 这些**正确答案**
    会在本地被判"格式不合法"丢掉，而用户完全没有补救路径：系统连试都没试。

    所以现在只拦**明显不可能是答案**的三种：

    | 拦什么 | 为什么 |
    | --- | --- |
    | 空 / 纯空白 | 平台会回参数错误，白费一次机会 |
    | 超过 `MAX_ANSWER_LENGTH` | 远超平台上限，且几乎不可能是答案 |
    | **像 flag 但正文是占位符**（`flag{...}`） | 那是模板，不是答案 |

    ⚠️ 第三条**只在答案看起来像 flag 时才适用**。对 `...` 这类本来就合法的
       答案（题目可能就要一个省略号），套占位符规则会把它误杀 ——
       这正是"形状判据"会错杀的同一类错误。

    ## 为什么"形状不对"不再是拒绝理由

    本地校验的**唯一目的**是省下不可再生的错误提交额度。而"形状不像 flag"
    根本不等于"答案错了" —— 比赛明确允许命令行、IP、时间戳。

    两种错法的代价完全不对称：

    * 错杀一个真答案 → **这题永远拿不到分**，没有补救路径；
    * 漏放一个垃圾串 → 损失**一次额度**，且平台会给出明确判定。

    所以判据应当松到"只拦明显不可能的"。方向永远是这个：
    **宁可放过，不可错杀。**
    """
    text = normalize_flag(value)
    if not text:
        raise AnswerFormatError("答案为空。")
    if len(text) > MAX_ANSWER_LENGTH:
        raise AnswerFormatError(f"答案太长（{len(text)} 字符，上限 {MAX_ANSWER_LENGTH}）。")
    # 占位符检查**只对"像 flag 的"生效** —— 见 docstring 里那条警告。
    if FLAG_PATTERN.match(text):
        body_start = text.find("{")
        if _PLACEHOLDER_BODY.match(text[body_start + 1 : -1]):
            raise AnswerFormatError(
                f"这个答案只有占位符正文（{text[:60]!r}），它不可能是真答案。"
            )
    return text


def validate_flag(value: str) -> str:
    """`validate_answer` 的旧名。

    ⚠️ **语义已经变了**：它不再要求答案长得像 flag。留着这个名字是因为
    调用点很多（`service.submit_challenge`、`router`），而一次纯改名会让
    diff 淹没真正的行为改动 —— 那种改动最容易在评审时被漏掉。
    """
    return validate_answer(value)


def flag_fingerprint(value: str) -> str:
    """flag 的指纹，用于去重与日志。

    用 sha256 而不是直接比对原文：日志里打指纹是安全的，打 flag 不是。
    """
    return hashlib.sha256(normalize_flag(value).encode("utf-8")).hexdigest()


def already_submitted(db: Session, *, challenge_id: str, flag: str) -> bool:
    """这个 flag 是不是已经提交过（或正在提交）。

    ## 为什么要连 `pending` 一起算进去

    `pending` 意味着"请求已经发出去了，但还没拿到判定"（见
    `Ctf2SubmissionStatus` 的说明）。它**可能已经到达平台**。
    把它排除在外的话，模型重新提出同一个 flag 时会再发一次 ——
    如果第一次其实成功了，第二次就是对同一个 flag 的重复提交，
    而很多平台把重复提交计为错误。宁可漏掉一次重试机会，
    也不要冒这个险。

    ⚠️ `rejected` **不算**（它根本没发出去），所以不在这里的列表里。
    """
    rows = db.execute(
        select(Ctf2Submission.id)
        .where(
            Ctf2Submission.challenge_id == challenge_id,
            Ctf2Submission.flag == normalize_flag(flag),
            Ctf2Submission.status.in_(
                (
                    Ctf2SubmissionStatus.CORRECT.value,
                    Ctf2SubmissionStatus.WRONG.value,
                    Ctf2SubmissionStatus.PENDING.value,
                )
            ),
        )
        .limit(1)
    ).first()
    return rows is not None


# ══════════════════════════════════════════════════════════
# 解题的输入输出
# ══════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class Candidate:
    """一个候选 flag 及其证据。"""

    flag: str
    evidence: str
    # 模型自己的把握程度（自由文本）。**不参与任何判断**，
    # 只展示给人看 —— 把它当判据会让"模型说 high"变成一次自动提交的理由。
    confidence: str = ""


@dataclass(slots=True)
class SolveSpec:
    """一次解题的完整输入。

    凭据在这一层是**明文**（要发给 LLM），所以这个对象是敏感的：
    不 `repr` 它、不写进日志、不落库 —— 与 `scans.runner.LaunchSpec`
    同一条纪律。
    """

    challenge_id: str
    title: str
    category: str
    description: str
    profile: str
    access_urls: tuple[str, ...]
    attachments: tuple[Path, ...]
    # ── LLM ──
    llm_model: str
    llm_api_key: str = field(repr=False)
    llm_api_base: str = ""
    llm_label: str = ""
    llm_kind: str = "cloud"
    # ── 策略 ──
    mode: str = Ctf2SubmitMode.SAFE.value
    max_turns: int = DEFAULT_MAX_TURNS
    max_wrong_submissions: int = DEFAULT_CTF2_MAX_WRONG_SUBMISSIONS
    # ★ 默认**隔离**（容器无网络）。只有需要原始 TCP 的题型才放开，
    # 见 `needs_raw_network`。
    network_isolated: bool = True
    # ★ 平台给的**靶机接入类型**（`http` / `nc` …）。空串表示平台没说。
    #
    # `build_spec` 时还不知道它（要等环境起来），所以由 `_run_worker`
    # 在 `start_environment_for` 之后补上 —— 它同时决定两件事：
    #   ① 沙箱要不要放开网络（见那里的说明）；
    #   ② 提示词要不要告诉模型"别只试 http_request"。
    access_type: str = ""
    cpus: int | None = None
    memory_mb: int | None = None
    # ★ v0.26「续解」：上一次已经做过的动作（纪要行）。
    #
    # ## 为什么它比"加轮数"更值钱
    #
    # 实测（2026-10-01，`[FlareOn3]GUI`）：一道 .NET 逆向题跑满 40 轮、
    # 发出 **37 条各不相同的命令**（一路分析到 `decryptBuffer` /
    # `DecompressBuffer` 的资源流结构），最后因为预算耗尽而停。
    #
    # 那 37 条命令的发现是真金白银换来的，而点"再解一次"会让模型
    # 从零开始 —— 把同样的事再做一遍。顺带一提：这次实测也**推翻**了
    # "跑满轮数是因为失忆或兜圈子"的猜测（37 条命令全不相同），
    # 真实原因是这类题本身就需要更多次迭代。
    #
    # 所以这里给的不是"更多轮数"，而是**接着上次的起点**。
    resume_digest: tuple[str, ...] = ()

    # ★ v0.28：用户补充的线索（"考点在反调试"、"libc 是 2.27"）。
    #
    # ## 为什么它必须进来（2026-10-11 修）
    #
    # `POST /solve` 的 schema 一直有这个字段（`schemas.Ctf2SolveIn.brief`），
    # 注释写着"传给 agent 的额外提示"，而它**一个字都没到过模型那里**：
    # router 没转交 → service 没字段 → 这里也没有位置。
    #
    # 后果是用户以为自己在帮 agent，实际什么都没发生 —— 而且**没有任何
    # 反馈**告诉他这一点。这与 `scans.brief` 形成了刺眼的不一致：
    # 那边是真的通的（进指令文件、进 prompt）。
    brief: str = ""

    # ★★ v0.32：同类题的方法卡（**已脱敏**，由调用方召回后传入）。
    #
    # ## 为什么是"传进来"而不是在这里查库
    #
    # `_initial_prompt` 是个**纯函数**（只读 spec），而查库要 session。
    # 让它碰库会把"组装 prompt"与"访问数据库"耦合起来 —— 而
    # `solve_in_sandbox` 那条路径**按设计不碰 DB**（见它的 docstring）。
    #
    # 所以召回发生在 `build_spec`（那里有 session），结果放进 spec。
    #
    # ⚠️ 这些内容**必须已经脱敏** —— 它们会被拼进 prompt，而卡片来自
    #    别的题。脱敏在写入时做（`notes.sanitize_for_reuse`），
    #    这里不再过滤一遍：两处都做会让"谁负责"变得含混。
    notes_text: str = ""

    def summary(self) -> str:
        """可以安全打进日志的摘要 —— **不含凭据**。"""
        return (
            f"challenge={self.challenge_id} profile={self.profile} "
            f"model={self.llm_model} key={'有' if self.llm_api_key else '无'} "
            f"mode={self.mode} urls={len(self.access_urls)}"
        )


@dataclass(slots=True)
class SolveOutcome:
    candidates: list[Candidate] = field(default_factory=list)
    turns: int = 0
    submitted: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    error: str | None = None
    transcript: str = ""

    #: 这次解题**有没有真的跑过工具并拿到输出**（v0.31）。
    #:
    #: ## 为什么需要这个信号
    #:
    #: `auto` 模式原来只看"没交过"和"还有额度"就提交，**完全不看证据**。
    #: 而 `evidence` 是模型自己写的字符串 —— 于是"凭空编一个 flag +
    #: 编一段 evidence"是一条真实可达的路径，代价是不可再生的额度。
    #:
    #: 这个字段提供一道**廉价且不会误伤**的闸：如果整场解题一次工具都
    #: 没跑（纯靠"想"给出答案），那它极可能是编的。而正常解题**必然**
    #: 跑过工具（看附件、解压、分析），所以这道闸几乎不会挡住真候选。
    #:
    #: ⚠️ 它**不是**"证据校验"——真正的校验（evidence 与工具产物一致）
    #:    需要自然语言匹配，而那本身不可靠。这里只做最粗的一层。
    saw_tool_output: bool = False


def needs_raw_network(category: str | None) -> bool:
    """这道题是否需要**容器内**的原始网络。

    pwn / 网络类题要直连 TCP 服务，所以不能把容器关进 `none`。
    其余题型（web 走 HTTP 工具、crypto/取证/逆向全在本地文件上）
    一律用最严的隔离。

    ⚠️ 认不出来时返回 False（= 隔离）。这是安全方向：猜错的代价是
    "某个 TCP 题连不上"（可见的失败，agent 会报告连接被拒），
    而反向猜错的代价是"一个本可以完全断网的容器被放开了网络"。
    """
    normalized = (category or "").strip().lower()
    return normalized in {
        "pwn",
        "pwning",
        "exploit",
        "network",
        "net",
        "misc-net",
        "pwnable",
    }


# ══════════════════════════════════════════════════════════
# 三个工具
# ══════════════════════════════════════════════════════════

TOOL_NAMES = ("read_attachment", "http_request", "run_shell")


async def _tool_read_attachment(args: dict[str, Any], *, spec: SolveSpec) -> str:
    """读附件（宿主侧读那份已下载的副本）。

    ⚠️ 路径做**白名单式**校验而不是"过滤 `..`"：只允许读
    `attachments/` 下、且文件名里没有分隔符的条目。
    黑名单式的 `..` 过滤在本项目已经栽过（附件删除那次的路径穿越），
    正确的形状是"先确定合法集合，再判在不在里面"。
    """
    name = str(args.get("path") or args.get("name") or "").strip()
    if not name:
        return "参数错误：需要 `path`（附件文件名）。"
    if "/" in name or "\\" in name or name.startswith("."):
        return (
            f"拒绝：`{name}` 不是合法的附件文件名。只允许附件目录下的"
            "单个文件名（不含路径分隔符）。"
        )

    matches = [p for p in spec.attachments if p.name == name]
    if not matches:
        listing = ", ".join(p.name for p in spec.attachments) or "（无附件）"
        return f"没有名为 `{name}` 的附件。可用附件：{listing}"

    path = matches[0]
    offset = int(args.get("offset") or 0)
    limit = min(int(args.get("limit") or 4000), MAX_TOOL_OUTPUT_CHARS)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return f"读取失败：{exc}"

    chunk = raw[offset : offset + limit]
    # 二进制不做 utf-8 解码（会得到一堆替换字符，对解题毫无帮助），
    # 改成给头部十六进制 + 一句明确的指引。这条指引很重要：
    # 不告诉模型"用 run_shell 分析"，它会反复试图 read_attachment
    # 一个 ELF，浪费掉整个回合预算。
    if b"\x00" in chunk[:1024]:
        head = chunk[:256].hex(" ")
        return (
            f"`{name}` 是二进制（{len(raw)} 字节，偏移 {offset} 起 {len(chunk)} 字节）。"
            f"这里是前 256 字节的十六进制：\n{head}\n"
            "要分析它请用 run_shell（file / strings / r2 / binwalk 等）。"
        )
    return _clip(chunk.decode("utf-8", errors="replace"), limit)


async def _tool_http_request(
    args: dict[str, Any], *, spec: SolveSpec, http: httpx.AsyncClient
) -> str:
    """发一次 HTTP 请求（**宿主侧**，所以白名单拦得住）。

    见模块 docstring 第 3 条：容器内的请求拦不住，所以这个能力
    必须由宿主提供。
    """
    url = str(args.get("url") or "").strip()
    if not url:
        return "参数错误：需要 `url`。"
    if "://" not in url:
        url = "http://" + url

    # ★ 唯一校验点。见 `guard_tool_url` 的说明。
    guard_tool_url(url, access_urls=spec.access_urls)

    method = str(args.get("method") or "GET").upper()
    if method not in ("GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"):
        return f"不支持的 HTTP 方法：{method}"

    headers = args.get("headers") if isinstance(args.get("headers"), dict) else None
    body = args.get("body")

    # ★ 流式读 + 上限（2026-10-10 修）。
    #
    # ## 原来用 `response.text` 的问题
    #
    # `_clip()` 只截断**显示**，不限制**读取** —— `response.text` 会把整个
    # 响应体先读进宿主内存。而白名单里的主机是**题目环境**，内容由出题人
    # 可控：一个返回几十 GB 的端点就能把宿主打爆（OOM），
    # 而宿主上跑着 API、调度器和所有正在解题的 worker。
    #
    # ## 为什么是"读够就停"而不是"读完再截"
    #
    # 模型能看到的只有 `MAX_TOOL_OUTPUT_CHARS` 那么多，读更多纯属浪费 ——
    # 而且正是这部分浪费构成了攻击面。
    #
    # ⚠️ 上限取 `MAX_TOOL_OUTPUT_CHARS * 4`：留一点余量给"按字符截断 vs
    #    按字节读取"的差异（UTF-8 中文一字三字节），但远小于任何能造成
    #    OOM 的量级。读满即停，并如实告诉模型"被截断了"——
    #    静默截断会让它以为"响应就这么长"，从而得出错误结论。
    _READ_CAP = MAX_TOOL_OUTPUT_CHARS * 4
    try:
        async with http.stream(
            method,
            url,
            headers={str(k): str(v) for k, v in (headers or {}).items()},
            content=str(body).encode() if body else None,
        ) as response:
            chunks: list[bytes] = []
            total = 0
            truncated = False
            async for piece in response.aiter_bytes():
                chunks.append(piece)
                total += len(piece)
                if total >= _READ_CAP:
                    truncated = True
                    break
            status_code = response.status_code
    except httpx.HTTPError as exc:
        return f"请求失败（{type(exc).__name__}）：{exc}"

    text = b"".join(chunks).decode("utf-8", errors="replace")
    head = f"HTTP {status_code}\n"
    if truncated:
        head += (
            f"（响应体超过 {_READ_CAP // 1024} KB，只读了前 {_READ_CAP // 1024} KB —— "
            "需要更多内容请用 run_shell 里的 curl/head 分段取，或缩小请求范围）\n"
        )
    return head + _clip(text, MAX_TOOL_OUTPUT_CHARS)


async def _tool_run_shell(args: dict[str, Any], *, sandbox: SandboxContainer) -> str:
    """在沙箱容器里执行命令。**这是解题的主力工具。**"""
    command = str(args.get("command") or "").strip()
    if not command:
        return "参数错误：需要 `command`。"
    timeout = int(args.get("timeout_seconds") or DEFAULT_SHELL_TIMEOUT)
    # 夹在合理区间里：一个 `timeout_seconds: 100000` 会让这道题
    # 永远不返回，而调度器只能看到"还在跑"。
    timeout = max(5, min(timeout, DEFAULT_SHELL_TIMEOUT))
    result = sandbox.shell(command, timeout=timeout)
    if result.exit_code == 124:
        return f"命令超时（{timeout} 秒）已被容器内 timeout 终止。\n{result.combined()}"
    return result.combined()


async def dispatch_tool(
    name: str,
    args: dict[str, Any],
    *,
    spec: SolveSpec,
    sandbox: SandboxContainer,
    http: httpx.AsyncClient,
) -> str:
    """工具分发。**未知工具返回一句话，不抛异常。**

    不抛是因为模型会拿它当反馈继续走；抛异常会让整个循环中断，
    而"模型用错了一次工具名"不该终止一次解题。
    """
    try:
        if name == "read_attachment":
            return await _tool_read_attachment(args, spec=spec)
        if name == "http_request":
            return await _tool_http_request(args, spec=spec, http=http)
        if name == "run_shell":
            return await _tool_run_shell(args, sandbox=sandbox)
    except Ctf2Error as exc:
        # 白名单拒绝走这里 —— 消息**必须回到模型**，它才知道换路子。
        return f"拒绝执行：{exc.message}"
    except Exception as exc:  # noqa: BLE001 - 工具不该让循环崩掉
        logger.exception("CTF2 工具 %s 执行失败", name)
        return f"工具执行出错（{type(exc).__name__}）：{exc}"
    return f"未知工具 `{name}`。可用工具只有：{', '.join(TOOL_NAMES)}。"


# ══════════════════════════════════════════════════════════
# 输出解析（ACTION / FINAL 文本协议）
# ══════════════════════════════════════════════════════════

# 只匹配**标记本身**，不试图连 JSON 一起正则掉。
#
# ⚠️ 我第一版写的是 `ACTION\s*[:：]\s*(\{.*\})`，它有一个真实的失败：
# 模型很爱把 JSON 包在 ```json 围栏里，而那时 `:` 与 `{` 之间隔着
# "```json" —— 正则要求"只有空白"，于是**整条动作被读成 None**，
# 而症状是"模型明明给了 ACTION，循环却当它没说话、反复喂格式提醒"。
# 实测撞到过（`test_parse_action_tolerates_a_code_fence`）。
_MARKER = re.compile(r"^\s*(ACTION|FINAL)\s*[:：]", re.MULTILINE)


def _extract_json_object(text: str, start: int) -> str | None:
    """从 `text[start]`（应该是 `{`）开始，切出**配对完整**的那一段。

    ## ★ 为什么不能简单地"数花括号"或者"取到最后一个 `}`"

    因为 **flag 本身就含花括号**：`evidence` 里几乎一定会出现
    `flag{...}` 这样的字符串，而 `summary` 里也可能引用它。

    * "取到最后一个 `}`" → 会把结论之后的解释文字一起吞进来，JSON 解析失败；
    * "数花括号但不认字符串" → `flag{x}` 里的 `{` 与 `}` 各算一次，
      深度看起来是平衡的，但那纯属巧合 —— 只要正文里有一个**单独的**
      花括号（`flag{a{b}`、或者一句 "用 `}` 结束"），计数就错位，
      而错位的结果是切出一个**看起来合法但内容错位**的片段。

    所以必须**跳过字符串字面量**：进到 `"` 里之后，花括号一律不算数，
    直到遇到未转义的收尾引号。转义（`\\"`）也要认 ——
    模型的 evidence 里出现转义引号是很常见的。
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _parse_json_block(text: str, marker: str) -> dict[str, Any] | None:
    """从 `marker`（`ACTION` / `FINAL`）那一行之后取出 JSON 对象。

    **认不出就返回 None**，不猜。容错两条（都是实测会遇到的模型习惯）：

    · 外面裹了 ```json 代码围栏（先找 `{`，围栏自然被跳过）；
    · JSON 后面跟着解释文字（靠 `_extract_json_object` 配对切出第一段）。
    """
    if not text:
        return None
    for match in _MARKER.finditer(text):
        if match.group(1) != marker:
            continue
        tail = text[match.end() :]
        start = tail.find("{")
        if start < 0:
            continue
        raw = _extract_json_object(tail, start)
        if raw is None:
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            # 这一段不是合法 JSON（比如模型写了个"示意"的骨架）。
            # 继续找下一个标记 —— 而不是放弃整条消息：
            # 模型有时会在解释里先提一句 ACTION 的**形状**，
            # 再给真正的那一条。
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def parse_action(text: str) -> tuple[str, dict[str, Any]] | None:
    """`ACTION: {...}` → `(工具名, 参数)`。认不出返回 None。"""
    payload = _parse_json_block(text, "ACTION")
    if not payload:
        return None
    name = str(payload.get("tool") or "").strip()
    args = payload.get("args")
    if not name:
        return None
    return name, (args if isinstance(args, dict) else {})


def parse_final(text: str) -> dict[str, Any] | None:
    """`FINAL: {...}` → 结论文档。认不出返回 None。"""
    return _parse_json_block(text, "FINAL")


def extract_candidates(payload: dict[str, Any] | None) -> list[Candidate]:
    """从结论文档里取候选 flag。**逐条校验，坏的就丢并记下**。

    单个候选形状不对不该让整份结论作废 —— 模型经常给三个候选，
    其中一个是"我没找到"的占位。丢掉坏的那条、保留好的，
    比全丢更接近我们要的结果。

    ⚠️ 但**去重必须在归一化之后做**：同一行写两次、
    或一个带反引号一个不带，是同一件事。

    ⚠️ 需要知道"**为什么**被丢掉"时用 `extract_candidates_with_reasons`。
    """
    accepted, _rejected = extract_candidates_with_reasons(payload)
    return accepted


def extract_candidates_with_reasons(
    payload: dict[str, Any] | None,
) -> tuple[list[Candidate], list[str]]:
    """同 `extract_candidates`，但**同时返回被拒的原因**。

    ## ★ 为什么必须能把原因带出去

    实测（2026-10-01，BUUCTF「Url编码」）：模型解出了题、给出
    `flag{and 1=1}`（confidence: high），而它被格式校验丢弃 ——
    原因**只进了一句 `logger.info`**，界面上看到的是：
    状态 `failed`、原因「环境已释放。」

    也就是说：一个**本来能修**的失败（把空格加进允许字符集），
    在用户眼里成了一个**毫无线索**的失败。用户唯一能做的就是再点一次
    "再解一次"，然后拿到同样的结果。

    所以把原因返回给调用方，让它进 `SolveOutcome.error` →
    `_record_outcome` → `challenge.last_error` → 界面。
    """
    if not payload:
        return [], []
    raw = payload.get("candidates")
    if not isinstance(raw, list):
        # 容错：有些模型直接给 `{"flag": "..."}`（一个候选）。
        single = payload.get("flag")
        raw = [{"flag": single, "evidence": payload.get("evidence", "")}] if single else []

    out: list[Candidate] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            item = {"flag": item}
        if not isinstance(item, dict):
            continue
        raw_flag = str(item.get("flag") or item.get("answer") or "")
        try:
            # ⚠️ v0.21：这里**不再**要求答案像 flag（见 `validate_answer`）。
            #    旧实现要求匹配 `FLAG_PATTERN`，于是命令行 / IP / 时间戳
            #    这些比赛明确允许的答案会被本地丢掉 —— 而这题就再也拿不到分了。
            flag = validate_answer(raw_flag)
        except AnswerFormatError as exc:
            logger.info("丢弃一个不合法的候选答案：%s", exc.message)
            # 原因里带上**原样的候选**（截断）：用户要看到的是
            # "系统拒了哪个字符串、为什么"，否则无从判断该不该改校验。
            rejected.append(f"{raw_flag[:60]!r} —— {exc.message}")
            continue
        key = flag_fingerprint(flag)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            Candidate(
                flag=flag,
                evidence=_clip(str(item.get("evidence") or ""), 4000),
                confidence=str(item.get("confidence") or ""),
            )
        )
    return out, rejected


def _format_reminder() -> str:
    """模型没按格式输出时喂回去的那句话。

    ★ 必须**具体**（给出正确的样子），不能只写"格式错误"。
    实测里模型对"格式错误"的常见反应是把同一段话再说一遍 ——
    因为它不知道自己差在哪。给一个可照抄的模板，命中率高得多。
    """
    return (
        "你这一轮的输出没有包含可识别的动作或结论。请**严格**用下面两种格式之一：\n"
        "要调用工具时，单独一行写：\n"
        'ACTION: {"tool": "run_shell", "args": {"command": "ls -la /work"}}\n'
        f"（可用工具只有 {' / '.join(TOOL_NAMES)}）\n"
        "要结束并给出结论时，单独一行写：\n"
        'FINAL: {"candidates": [{"answer": "题目要的那个字符串", '
        '"evidence": "怎么拿到的"}], '
        '"summary": "一句话结论", "tried": ["试过什么"], "ruled_out": ["排除了什么"]}\n'
        # ⚠️ 这里必须写"答案"而不是"flag"：题目要的可能是命令行 / 文件名 /
        #    时间戳 / IP。提醒语里若只提 flag，模型会把找到的命令行丢掉。
        "`answer` 是**题目要提交的那个字符串**（flag、命令行、文件名、"
        "时间戳、IP 都可能），原样写进去。\n"
        "没有拿到答案时，candidates 写成空数组 —— **不要编造**一个。"
    )


# ══════════════════════════════════════════════════════════
# 解题循环
# ══════════════════════════════════════════════════════════


def _initial_prompt(spec: SolveSpec) -> str:
    """第一轮的用户消息：题目信息 + 环境信息 + 附件清单。"""
    lines = [
        "## 题目",
        f"- 标题：{spec.title}",
        f"- 分类：{spec.category or '（未标注）'}",
    ]
    if spec.description:
        lines += ["", "### 题面", spec.description.strip()]
    lines += ["", "## 环境"]
    if spec.access_urls:
        lines.append("- 可访问的地址（**只有这些**允许发请求）：")
        lines += [f"  - {url}" for url in spec.access_urls]
        # ★ 靶机怎么接，**平台是明确告诉我们的**（`access_type`），
        #   这里必须转达。
        #
        # ⚠️ 不转达的后果：`ctf2_system.md` 里写着"沙箱默认无网络"，
        #   于是模型对一台**裸 TCP** 靶机只会干试 `http_request`
        #   （而那个工具只讲 HTTP），然后在容器里连 `nc` 都不去试 ——
        #   一道本来能解的题被自己的提示词堵死。
        if spec.access_type and spec.access_type.lower() != "http":
            lines.append(
                f"- ⚠️ 这台靶机是 **{spec.access_type}** 类型（**不是 HTTP**）："
                "用 `run_shell` 里的 `nc <host> <port>` 或 "
                '`python3 -c "import socket; …"` 直连它。'
                "沙箱网络**已为这道题放开**，不要只试 `http_request`。"
            )
    else:
        lines.append("- 本题**没有**可访问的网络地址，全部信息在附件里。")
    lines.append("")
    if spec.attachments:
        lines.append("- 附件（用 read_attachment 读，或直接 `ls /work/attachments`）：")
        lines += [f"  - {p.name}（{p.stat().st_size} 字节）" for p in spec.attachments]
    else:
        lines.append("- 本题没有附件。")
    # ★★ v0.29：持久工作区。
    #
    # ## 为什么必须显式告诉模型
    #
    # `/work` 是 tmpfs，容器一停内容全没。而"续解"只从 transcript 还原
    # 命令与输出前 140 字 —— 所以解压产物、反编译结果、爆破出的口令、
    # 以及它自己写的分析脚本**全部消失**。实测（2026-10-12，
    # `[FlareOn3]GUI`）：模型跑满 78 轮、写了 12 个 C# harness、
    # patch 过 CLR 元数据，而这些在下次续解时一个都不在。
    #
    # 光加挂载是不够的：**模型不知道有这个目录就不会用**。
    # 所以这里明确说清"放什么、为什么放"。
    lines += [
        "",
        f"## 持久区：`{PERSIST_DIR}`（★ 重要的东西放这里）",
        "",
        "`/work` 是**内存盘，容器一停就全没了**。而这个子目录是"
        "**跨运行保留**的 —— 下次接着做这道题时它还在。",
        "",
        "**值得放进去的**（下次能省掉重做）：",
        "- 解压出来的题目文件（省掉重新解压）；",
        "- 反编译/反汇编产物（`objdump` / `monodis` / `r2` 的输出）；",
        "- 你写的分析脚本与程序（C# harness、Python 脚本）；",
        "- 已经解出的中间结果（爆破出的口令、解密出的密钥、提取的配置）。",
        "",
        "**不要放**：临时文件、跑一次就丢的中间物 —— 那只会让下次的"
        "目录更难读。",
    ]
    # ⚠️ 这一段**只在续解时**加。
    #
    # 一开始写成无条件的，结果全新解题的 prompt 里也出现了"续解"二字
    # —— 而 `test_the_initial_prompt_announces_a_resume` 正是断言
    # "全新解题时不该出现'续解'"。那条断言是对的：对一次全新的解题说
    # "如果这是续解"是纯噪音，还会让模型去找一个不存在的"上次"。
    if spec.resume_digest:
        lines += [
            "",
            f"⚠️ 先 `ls -la {PERSIST_DIR}` 看上次留了什么，"
            "**不要重做已经做过的事**。",
        ]
    # ★ v0.32：同类题的方法卡。
    #
    # ## 位置：在"现在开始"**之前**、"持久区"**之后**
    #
    # 理由：它是**参考材料**（别的题的经验），而"现在开始"是行动指令。
    # 放在指令之后会让模型先看指令再看到材料，顺序反了 —— 它会带着
    # "开始动手"的心态读到一堆"别的题怎么做"，更容易照搬。
    #
    # ⚠️ 内容已经脱敏（写入时做的，见 `notes.sanitize_for_reuse`）。
    #    这里只是拼接，**不再过滤** —— 两处都做会让"谁负责"含混。
    if spec.notes_text.strip():
        lines.append(spec.notes_text)
    lines += [
        "",
        "## 现在开始",
        "先看清楚手上有什么（附件类型、服务响应），再决定方向。",
        f"你最多有 {spec.max_turns} 轮。每轮只做一件事，然后按协议输出。",
    ]
    # ★ v0.28：人工补充的线索。
    #
    # ## 位置很讲究：放在"现在开始"**之后**、"续解提示"**之前**
    #
    # 因为它既不是题目的一部分（那是上面的题面），也不是"接着上次走"
    # 的指令（那是下面的续解段）。它是一个**独立的输入源**：
    # 出题人没写、但用户知道的信息。
    #
    # ⚠️ 措辞里明确它是"人工补充"而不是"题目要求"：不这么写的话，
    #    模型会把它当成题面的一部分，而在冲突时（提示说"libc 是 2.27"
    #    而实际探出来是别的版本）倾向于相信它 —— 那会把一个"线索"
    #    变成一条"错误的先验"。所以下面那句"与实测冲突时以实测为准"
    #    是这条提示的一部分，不是客套。
    if spec.brief.strip():
        lines += [
            "",
            "## 人工补充的线索（不是题面，是出题人之外的人给的提示）",
            "",
            spec.brief.strip(),
            "",
            "⚠️ 把它当作**方向性提示**，不是必须成立的事实。"
            "如果你实测的结果与它冲突，**以实测为准**，"
            "并在 `ruled_out` 里写清「提示说 X、实测是 Y」。",
        ]
    if spec.resume_digest:
        lines += [
            "",
            "## ⚠️ 这是**续解**，不是从头开始",
            f"你上一次在这道题上已经做过 {len(spec.resume_digest)} 步，"
            "最后因为回合预算耗尽而停下 —— 上面那份「已经执行过的动作」就是那次的全部过程。",
            "",
            "接着上次的进度做：",
            "- 已经跑过的命令**不要重跑**，它的结论就在纪要里；",
            "- 先判断上次分析到哪一步、还缺哪一环，从那里继续；",
            "- 如果上次的方向明显走不通，**说清为什么**再换 —— "
            "但不要无理由地把思路整个推倒重来（那等于把上次的预算白烧了）。",
        ]
    return "\n".join(lines)


def _digest_message(lines: list[str]) -> dict[str, str]:
    """把"做过什么"压成一条消息。

    ★ 末尾那句"不要重复执行"是这份纪要**一半的价值**：光给一张清单还不够，
    模型很容易把它当成"背景资料"，然后继续跑同一条命令。明确说出
    "跑过的别再跑"，空转才会真的减少 —— 而空转正是"跑满 40 轮却
    没有结论"的主要成因。
    """
    return {
        "role": "user",
        "content": (
            "## 已经执行过的动作（自动纪要，按时间顺序）\n\n"
            + "\n".join(lines)
            + "\n\n⚠️ 每行的输出都被截断了，只保留开头。"
            "**不要重复执行已经跑过的命令** —— 它的结论就在上面。"
            "需要更细的信息时，换一条更精确的命令，而不是把原来那条再跑一遍。"
        ),
    }


def _final_stretch_note(remaining: int) -> str:
    """只剩几轮时的收尾提醒。

    ★ 措辞刻意**不鼓励猜**：在「自动提交」模式下，一个猜出来的 flag 会被
    直接提交并**消耗一次错误机会** —— 那是不可再生资源（见
    `DEFAULT_CTF2_MAX_WRONG_SUBMISSIONS`）。所以这里要的是
    "给出有依据的候选，或者把卡点说清楚"，而不是"随便交一个"。
    """
    return (
        f"⚠️ 只剩 {remaining} 轮了，请开始收口：\n"
        "- 已经有依据充分的答案 → 用 FINAL 给出候选；\n"
        "- 还没拿到答案 → 用 FINAL 说清：试过什么、结果如何、卡在哪一步、"
        "还差什么信息。**这比再开一个新方向有用得多** —— 那份说明会留在"
        "解题过程里，人和下一轮都能接着用；\n"
        "- 不要在最后几轮开新的探索方向：它大概率跑不完，"
        "而你会因此连「目前已经知道什么」都没说清。"
    )


def _clip(text: str, limit: int) -> str:
    """截断长文本，并在末尾**明确说明被截断了**。

    静默截断是本项目吃过亏的一类：模型看到的是"命令输出就这些"，
    于是它基于不完整的信息下结论。一句话的提示就能避免。
    """
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原长 {len(text)} 字符；用更精确的命令缩小范围）"


async def solve_in_sandbox(
    spec: SolveSpec,
    *,
    sandbox: SandboxContainer,
    gateway: LlmGateway,
    system_prompt: str,
    transcript: list[str] | None = None,
    on_progress: Callable[[str, str, dict[str, str]], None] | None = None,
) -> SolveOutcome:
    """在已启动的沙箱里跑解题循环，直到产出候选或回合用尽。

    ⚠️ 这个函数**不碰数据库**（它是 async，而本项目的 Session 是同步的，
    见 `run_worker` 的三段式）。

    ## ★ `on_progress`：把过程**送出去**，而不是只留在本地 log 里

    实测（2026-09-30）：一道题被完整解出，而活动流里只有 5 行状态、
    没有任何过程 —— 用户的原话是"没过程解题过程就这样？"，而
    `ChallengeCard` 上那个「查看解题过程」的链接正指向活动流。

    根因就是这里：下面那三行 `log.append(...)` 记录了完整的推理、
    工具调用与输出，但**只进内存**，最后拼成字符串写进一个磁盘文件。
    活动流（前端唯一能看过程的地方）从头到尾不知道它们存在。

    这个回调就是那三者与事件流之间的桥：
    `(kind, summary, payload)` → worker 侧落成 `reasoning` / `tool_call`
    事件。`kind` 是 `"reasoning"` 或 `"tool_call"`，`payload` 是
    对应事件的 `detail` 骨架（见 `events.emit_reasoning` /
    `emit_tool_call` 的形状）。**用回调而不是直接发事件**，是因为
    本函数按设计不碰 DB（见上面的 ⚠️）—— 会话是同步的，在这里用
    会阻塞事件循环。

    ⚠️ 回调**不允许抛异常**：它由 worker 包成"写不进就记 warning"。
    一次事件写失败不该让已经跑了 30 轮的解题循环停下 ——
    过程是给人看的，结果才是要交的。
    """
    log = transcript if transcript is not None else []
    # ★ 上下文分成三段（v0.26）—— 见 `DEFAULT_MAX_TURNS` 那段说明：
    #
    #   base   —— 题目信息。**永不裁剪**（模型忘了题目就彻底跑偏）
    #   digest —— 动作纪要。"我做过什么"的极简版，**不会丢失**
    #             （只按条数上限往前丢，而每条只有一行）
    #   recent —— 最近 N 条完整消息（推理 + 工具输出）
    #
    # 拆成三段，而不是继续用"砍掉最早的"：**砍掉就等于遗忘**，
    # 而模型遗忘的表现是重新跑一遍已经跑过的命令 —— 在日志上看起来
    # 像"模型太笨"，实际是我们把它的记忆删了。
    base_message: dict[str, str] = {"role": "user", "content": _initial_prompt(spec)}
    recent: list[dict[str, str]] = []
    # ★ 续解时，纪要从**上一次的动作**开始 —— 那是这次运行唯一"继承"的东西。
    digest: list[str] = list(spec.resume_digest)

    def _messages() -> list[dict[str, str]]:
        """组装这一轮要发给模型的消息。

        ⚠️ 每次**新建列表**，而不是就地改一个共享的 `messages`：
        裁剪必须与发出去的内容严格一致，而复用同一个列表对象会让
        "裁掉了"和"模型收到的"变成两件事。
        """
        head = [base_message]
        if digest:
            head.append(_digest_message(digest))
        return [*head, *recent]

    outcome = SolveOutcome()

    def notify(kind: str, summary: str, payload: dict[str, str]) -> None:
        """把一条过程送给回调（有回调时）。**吞掉回调的异常。**"""
        if on_progress is None:
            return
        try:
            on_progress(kind, summary, payload)
        except Exception:  # noqa: BLE001 - 过程不该影响解题结果
            logger.warning("CTF2 过程事件发送失败（不影响解题）", exc_info=True)

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as http:
        for turn in range(1, spec.max_turns + 1):
            outcome.turns = turn
            # ★ 收尾提醒：只剩 `FINAL_STRETCH_TURNS` 轮时提醒一次。
            #
            # 动机来自实测：没有它的时候，模型会一路探索到最后一轮，
            # 然后得到"回合用尽" —— 而它其实**早就掌握足够信息**了，
            # 只是一直在找"更确定"的证据。用 `==` 保证只加一次，
            # 否则最后五轮每轮都在重复同一句话，反而挤掉有用的内容。
            remaining = spec.max_turns - turn
            if remaining == FINAL_STRETCH_TURNS:
                recent.append({"role": "user", "content": _final_stretch_note(remaining)})
            try:
                reply = await gateway.chat(_messages(), system=system_prompt)
            except Ctf2Error as exc:
                # LLM 层面的失败不重试：这一层上面没有"退避"的语义
                # （重复问同一个问题不会得到不同答案），而回合预算要
                # 留给真正在做事的回合。
                outcome.error = exc.message
                log.append(f"[turn {turn}] LLM 失败：{exc.message}")
                outcome.transcript = "\n\n".join(log)
                return outcome

            text = reply.text or ""
            log.append(f"[turn {turn}] assistant({reply.source}): {_clip(text, 4000)}")
            recent.append({"role": "assistant", "content": text})

            # ① 结论？
            final = parse_final(text)
            if final is not None:
                outcome.candidates, rejected = extract_candidates_with_reasons(final)
                outcome.transcript = "\n\n".join(log)
                if not outcome.candidates:
                    if rejected:
                        # ★★ 有结论、但候选**全被本地校验挡下** —— 这必须说出来。
                        #
                        # 实测（2026-10-01，BUUCTF「Url编码」）：模型给出
                        # `flag{and 1=1}`（confidence: high），被那个排除
                        # 空格的 `\s` 判为"形状不合法"而丢弃。原因当时只进了
                        # 一句 logger.info —— 界面上是 `failed` + 「环境已释放。」，
                        # 用户完全无从下手。
                        #
                        # 写进 `outcome.error` 之后，它会经 `_record_outcome`
                        # 落到 `challenge.last_error`，最终显示在题目卡片上。
                        # 把一个"查不出原因的失败"变成一句**可操作**的话。
                        outcome.error = (
                            "模型给出了结论，但候选 flag 被本地格式校验拒绝（未提交）："
                            + "；".join(rejected[:3])
                        )
                        log.append(f"[turn {turn}] {outcome.error}")
                    logger.info("CTF2 解题结束但无有效候选：%s", spec.challenge_id)
                return outcome

            # ② 动作？
            action = parse_action(text)
            if action is None:
                # 没有动作也没有结论：这一轮是纯思考/追问，把它当推理发出去。
                # ⚠️ 只发**有实义**的文本（空串或极短的寒暄不值得进活动流，
                #    而那些行会把真正有信息量的推理淹掉）。
                if len(text.strip()) >= 20:
                    notify("reasoning", _clip(text.strip(), 200), {"text": text.strip()})
                recent.append({"role": "user", "content": _format_reminder()})
                continue

            name, args = action
            command = str(args.get("command") or json.dumps(args, ensure_ascii=False))
            log.append(
                f"[turn {turn}] tool {name} {_clip(json.dumps(args, ensure_ascii=False), 500)}"
            )
            result = await dispatch_tool(name, args, spec=spec, sandbox=sandbox, http=http)
            log.append(f"[turn {turn}] result: {_clip(result, 2000)}")
            recent.append({"role": "user", "content": f"工具 `{name}` 的输出：\n{result}"})
            # ★ 记录"这次解题真的跑过工具"（v0.31）—— 供 auto 模式的
            #   证据门禁判断"答案是跑出来的还是想出来的"。
            #   见 `SolveOutcome.saw_tool_output`。
            outcome.saw_tool_output = True

            # ★ 把这一轮的过程送出去。
            #
            # 顺序刻意是"先说后做"：模型在 `ACTION:` 之前写的那段话
            # （"The shell is failing, let me retry" / "Cracked! Password = 9919"）
            # 是**为什么这么做的解释**，先于那条命令出现才读得通。
            narration = _narration_before_action(text)
            if narration:
                notify("reasoning", _clip(narration, 200), {"text": narration})
            notify(
                "tool_call",
                _clip(f"{name} {command}", 200),
                {"tool": name, "command": command, "output": result},
            )

            # ── 动作纪要：这一轮做过什么 ────────────────────
            #
            # ★ 见 `DEFAULT_MAX_TURNS` 那段说明。这一行是**永久保留**的
            # （直到超过 `MAX_DIGEST_LINES`），而完整的对话内容会被裁掉。
            #
            # 一行里放三样东西：轮次、命令、输出开头 —— 足够让模型认出
            # "这件事我做过了"，又不至于把上下文撑爆。
            digest.append(
                f"[t{turn}] {name}: {_clip(command, 110)} → {_clip(result, 140)}"
            )
            if len(digest) > MAX_DIGEST_LINES:
                del digest[: len(digest) - MAX_DIGEST_LINES]

            # 完整内容只留最近 N 条。
            #
            # ⚠️ 这里曾经把裁掉的部分**直接丢弃** —— 于是第 24 轮之后
            #    模型开始重复劳动，而日志上看起来像"模型太笨"。
            #    现在丢掉的内容已经在 `digest` 里留了一行，所以"忘"
            #    变成了"记得一个简略版"。
            if len(recent) > MAX_HISTORY_TURNS * 2:
                del recent[: len(recent) - MAX_HISTORY_TURNS * 2]

    outcome.error = f"回合用尽（{spec.max_turns} 轮）仍未给出结论。"
    outcome.transcript = "\n\n".join(log)
    return outcome


def _narration_before_action(text: str) -> str:
    """取出 `ACTION:` 之前那段"为什么这么做"的话。

    模型的一轮输出通常是这个形状：

        我需要先解开这个 zip。它看起来是个加密的 Office 文档。

        ACTION: {"tool": "run_shell", "args": {...}}

    后半段是动作（单独发成 `tool_call` 事件），前半段是理由 ——
    只发动作的话，活动流会变成一串没有上下文的命令，
    而"为什么跑这条"恰恰是排查时最需要的东西。

    ⚠️ 去掉代码围栏：模型常把 ACTION 包在 ``` 里，直接切会让
    理由的尾部带一串 ```，读起来像渲染坏了。

    ⚠️ `marker >= 0` 而不是 `> 0`：模型有时**一上来就写 ACTION**
    （没有铺垫），那时 `find` 返回 0 —— 用 `> 0` 会落到 else 分支，
    把整段（含 `ACTION: {...}`）当成"推理"发出去。实测就是这样：
    活动流里出现过一条以 `ACTION: {"tool": "run_shell"...` 开头的
    reasoning 事件，读起来像渲染坏了。
    """
    marker = text.find("ACTION:")
    head = text[:marker] if marker >= 0 else text
    head = head.replace("```", "").strip()
    return head if len(head) >= 20 else ""


# ══════════════════════════════════════════════════════════
# 提交（safe / auto 两条路径）
# ══════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class SubmitDecision:
    """一次候选 flag 该怎么处理。**纯函数算出来的**，便于测试。"""

    action: str  # "submit" | "hold" | "reject"
    reason: str = ""


def decide_submission(
    *,
    mode: str,
    already: bool,
    remaining_wrong: int,
    evidence: str = "",
    saw_tool_output: bool = False,
) -> SubmitDecision:
    """候选 flag 的几条去向。

    ## 顺序是有讲究的：先去重、再看额度、再看证据、最后看模式

    每一层都挡掉一类真实的浪费：

    1. **已提交过** → `reject`。重复提交在多数平台**计为错误**
       （有些直接判违规），所以它比"浪费一次额度"更严重。
    2. **额度用尽** → `reject`。额度是**不可再生**的，而"再试一次"
       的期望收益随剩余次数下降 —— 到 0 时必须停。这条**优先于模式**：
       即便 `auto` 模式也受它约束（已定设计决策 4 的原文）。
    3. **★ 证据不足** → `hold`（v0.31 补）。只对 `auto` 生效，见下。
    4. **safe 模式** → `hold`。产出的候选**落库但不提交**，
       等人在界面上点一下（`POST /ctf2/challenges/{id}/submit`）。

    ⚠️ `hold` 不是"失败"，它是 safe 模式的**正常终点**。把它做成
    错误会让界面上显示一片红，而实际上系统工作得完全正确。

    ## ★★ 为什么需要"证据门禁"（2026-10-12 补）

    在这之前，`auto` 模式的判据只有"没交过"和"还有额度"两条 ——
    **完全不看证据**。而 `evidence` 字段是模型自己写的字符串
    （`extract_candidates_with_reasons` 只是 `_clip` 一下原样收下），
    `confidence` 更是明确"不参与任何判断"。

    于是有一条真实可达的路径：**模型凭空编一个 flag、再编一段
    evidence，auto 模式就把它提交出去** —— 而错误提交消耗的是
    **不可再生**的额度，烧完这道题就没机会了。

    这道闸要挡的正是它。判据是**两条独立的弱信号**（都满足才放行）：

      · `evidence` 非空 —— 模型至少说了"怎么拿到的"；
      · `saw_tool_output` —— 这次解题里**真的跑过工具并拿到输出**。

    为什么是两条而不是更强的一致性校验（比如"evidence 里必须引用
    某条真实工具输出"）：那种校验需要把 evidence 与工具产物做匹配，
    而自然语言的匹配本身就不可靠 —— 误判的代价是"真候选被挡住"，
    比"编造的候选被提交"更常见。**先做一道廉价且不会误伤的闸**，
    把最坏情况（完全没跑工具、纯编造）挡掉。

    ## 为什么只对 auto 生效

    `safe` 模式的候选本来就等人工确认 —— 而人会看 evidence。
    对 safe 加这道闸只会让"候选落库"这一步也变复杂，收益为零。
    """
    if already:
        return SubmitDecision("reject", "这个 flag 已经提交过（或正在提交中），不重复提交。")
    if remaining_wrong <= 0:
        return SubmitDecision(
            "reject",
            "该题的错误提交次数已用尽。继续提交只会浪费平台的额度 —— "
            "请人工核对候选 flag 后再决定。",
        )
    if mode != Ctf2SubmitMode.AUTO.value:
        return SubmitDecision("hold", "safe 模式：候选 flag 需要人工确认后才提交。")
    # ── 以下是 auto 独有的门禁 ────────────────────────────
    #
    # ⚠️ 顺序在模式判断**之后**：safe 模式不受影响（见 docstring）。
    if not saw_tool_output:
        return SubmitDecision(
            "hold",
            "auto 模式下证据不足：这次解题**没有跑过任何工具**就给出了答案。"
            "候选已保留，请人工核对后再决定是否提交。",
        )
    if not evidence.strip():
        return SubmitDecision(
            "hold",
            "auto 模式下证据不足：候选没有给出 `evidence`（怎么拿到的）。"
            "候选已保留，请人工核对后再决定是否提交。",
        )
    return SubmitDecision("submit")


async def submit_candidate(
    *,
    challenge_id: str,
    flag: str,
    mode: str,
    client_factory,  # noqa: ANN001 - 返回 (client, remote_ids)
) -> tuple[str, str]:
    """提交一个 flag。返回 `(结果状态, 说明)`。

    ## ★ 三段式：sync 落库 → async 提交 → sync 回写

    `Ctf2Submission` 的行**必须在发出请求之前**就落库（`pending`）。
    不这么做的话，一个在"请求已发出、响应还没回来"时崩溃的进程
    会留下**没有任何记录**的提交 —— 而那次提交很可能已经到达平台
    （对的记为解出、错的扣了额度）。事后对账时，我们连"发过这一个"
    都不知道，用户看到的额度减少将无法解释。

    所以顺序是硬的：**先写 pending，再发请求，最后回写结果**。
    代价是崩溃时会留下 `pending` 的行，而那正是它存在的意义 ——
    它是一张"需要人工或对账去确认"的欠条，不是脏数据。

    ## ★ 为什么这个函数被两条路径共用

    `_handle_candidates` 的 auto 路径与 `service.confirm_submit`
    的人工路径**都调它**，而不是各写一遍。理由：上面那个顺序加上
    去重与额度两处判断，一共三个容易做错且互相耦合的点。
    抄一份的后果是两条路径在任何一次修改后行为不同 ——
    而"自动提交"与"手动提交"行为不同，是最不该发生的事。

    ## 为什么用 `session_scope()` 而不是注入会话工厂

    本函数是 async，而 Session 是同步的。它只在**两段同步代码**里
    用会话（提交前后的落库），中间那段 await 完全不碰数据库 ——
    所以"用哪个会话"没有第二个答案，注入一个工厂只会多一层间接。
    """
    submission_id = new_id("ctf2sub")
    with session_scope() as db:
        db.add(
            Ctf2Submission(
                id=submission_id,
                challenge_id=challenge_id,
                flag=normalize_flag(flag),
                status=Ctf2SubmissionStatus.PENDING.value,
                mode=mode,
                submitted_at=now_utc(),
            )
        )
        # 题目状态推进到"正在提交"：界面要能显示它，否则用户会重复点。
        #
        # ★★ 但**已解出的题不能推进** —— 这是实测撞出来的。
        #
        # 数据库上有一条 `ck_ctf2_challenges_confirmed_only_when_solved`：
        # `confirmed_flag IS NULL OR status = 'solved'`。而一道已解出的题
        # 必然带着 `confirmed_flag`，把它改成 `submitting` 就同时违反了
        # 那条约束与 `solved_implies_flag` 的意图，于是得到 500：
        #
        #     CheckViolation: new row ... violates check constraint
        #     "ck_ctf2_challenges_confirmed_only_when_solved"
        #
        # 实测路径（2026-10-01，拿一道已解出的题做端到端探测时撞上）：
        # 想验证"非 flag 形态的答案能不能走到平台"，随手挑了 hashcat
        # （已解出），结果提交还没发出去就先崩在写库上。
        #
        # 什么时候会真的走到这里：**再次提交一道已解出的题**。
        # 产品路径本该拦住它（`_assert_resolvable` 对已解出抛 409），
        # 但 `submit_challenge` 只调了 `_assert_submission_supported`，
        # **没有**调 `_assert_resolvable` —— 所以那条防线是漏的。
        #
        # 这里兜住它：已解出的题保持 `solved` 不动。提交本身仍然发出去
        # （用户可能就是想再确认一次答案），平台会回 `ALREADY_SOLVED`，
        # 而那正是 `already_solved` 分支要处理的情形。
        challenge = db.get(Ctf2Challenge, challenge_id)
        if challenge is not None and challenge.status != Ctf2ChallengeStatus.SOLVED.value:
            challenge.status = Ctf2ChallengeStatus.SUBMITTING.value
        db.commit()

    # ── 发请求（async） ──────────────────────────────────
    status_value = Ctf2SubmissionStatus.PENDING.value
    message = ""
    receipt: str | None = None
    already_solved = False

    # ★★ 先把客户端建出来，**再进 try**（2026-10-10 修）。
    #
    # ## 为什么必须挪出来
    #
    # `client_factory()` 在"账号不存在 / 凭据解不开"时抛的是
    # **`ApiError`**（见 `service.build_client_sync`），而 `ApiError` 与
    # `Ctf2Error` **没有继承关系** —— 下面那个 `except Ctf2Error` 捕不到它。
    # 于是异常穿透本函数，而上面**已经写下了一条 `pending` 提交行**。
    #
    # ## 那条假 `pending` 的代价比"一次报错"大得多
    #
    # `pending` 的语义是"请求已发出、结果未知"（见
    # `vocab.Ctf2SubmissionStatus.PENDING`）—— 那是**一张要人工对账的欠条**。
    # 但事实上请求**根本没发出去**。
    #
    # 更贵的是它的副作用：`already_submitted()` 把 `pending` 计入"已提交"，
    # 于是**同一个 flag 以后永远不会被再次提交** —— 一道本来能解的题，
    # 因为一次凭据抖动而永久失去那个候选。
    #
    # ## 挪出来之后的行为
    #
    # 建不出客户端 = 请求**确定没发出去**，所以那条 `pending` 要改写成
    # `failed`（而不是留着当欠条）。这与 `pending` 的定义严格一致：
    # 只有"真的发出去了、结果不知道"才配叫 pending。
    try:
        client, remote_ids = client_factory()
    except Exception as exc:  # noqa: BLE001 - 见上：任何建客户端失败都算"没发出去"
        reason = f"无法建立与 CTF2 的连接：{type(exc).__name__}: {exc}"
        logger.warning("CTF2 提交未能发出（%s）：%s", challenge_id, reason)
        # 与下面那条正常回写走**同一条路**（同一个 `_advance_challenge`），
        # 不另写一套恢复逻辑 —— 两套状态恢复必然漂移。
        with session_scope() as db:
            row = db.get(Ctf2Submission, submission_id)
            if row is not None:
                row.status = Ctf2SubmissionStatus.FAILED.value
                row.local_error = _safe_text(reason)
            challenge = db.get(Ctf2Challenge, challenge_id)
            if challenge is not None:
                _advance_challenge(
                    challenge, Ctf2SubmissionStatus.FAILED.value, flag, already_solved=False
                )
            db.commit()
        return Ctf2SubmissionStatus.FAILED.value, reason

    try:
        async with client:
            payload = await client.submit_flag(
                practice_id=str(remote_ids.get("competition_remote_id") or ""),
                challenge_id=str(remote_ids.get("remote_id") or ""),
                flag=normalize_flag(flag),
                sub_flag_id=remote_ids.get("sub_flag_id"),
            )
        status_value, message, receipt = interpret_submit(payload)
    except Ctf2Error as exc:
        # 传输层失败 → `failed`（**不是** `wrong`，不消耗额度）。
        # 这个区分见 `Ctf2SubmissionStatus` 的说明：把失败记成错误提交
        # 会让"还剩几次"莫名其妙地变少。
        status_value = Ctf2SubmissionStatus.FAILED.value
        message = exc.message
        # ★★ 但 `ALREADY_SOLVED` 不是"失败"，是"这题已经不用做了"。
        #
        # 实测（2026-10-01）：向一道**已经解出**的题（hashcat）重复提交
        # 一个**正确**的 flag，平台回
        #
        #     HTTP 400 {"error": {"code": "ALREADY_SOLVED",
        #                         "key": "errors.submission.already_solved"}}
        #
        # 它的状态码是 400，和"参数写错了"一模一样 —— 只按状态码分类，
        # 这个响应会走上面那条通用分支，把一道**已经解出的题**打成
        # `failed`，界面上显示"失败"。用户看到的是"suvex 把这题搞失败了"，
        # 而事实是这题**早就解出来了**。所以要按机器码把它分出来。
        #
        # ⚠️ 这里**不**把题目记成 `solved`：平台只说"已经解出"，它并
        #   没有认可**我们这次交的这个 flag**。凭空把 `confirmed_flag`
        #   写成某个值，等于在"平台认过的答案"这一列里放一个没被认过的
        #   字符串 —— 那正是 `models` 把候选与确认拆成两列要防的事。
        if isinstance(exc, Ctf2ResponseError) and exc.code == CTF2_ERROR_ALREADY_SOLVED:
            already_solved = True
            message = PLATFORM_ALREADY_SOLVED_MESSAGE

    # ── 回写（sync） ────────────────────────────────────
    with session_scope() as db:
        row = db.get(Ctf2Submission, submission_id)
        if row is not None:
            row.status = status_value
            row.remote_message = message or None
            row.remote_submission_id = receipt
            if status_value == Ctf2SubmissionStatus.FAILED.value:
                row.local_error = message
        challenge = db.get(Ctf2Challenge, challenge_id)
        if challenge is not None:
            _advance_challenge(challenge, status_value, flag, already_solved=already_solved)
        db.commit()

    return status_value, message


def interpret_submit(payload: dict[str, Any]) -> tuple[str, str, str | None]:
    """平台响应 → `(状态, 说明, 回执id)`。

    ## ★ 判"对"需要**两个**信号：明确的成功标志 + 一个回执 id

    只凭 `correct: true` 就标 `correct` 是危险的：我们没法区分
    "平台真的判对了"与"某个中间层返回了一个形状相似的默认值"。
    而回执 id 是**平台侧的凭据** —— 有它才说明这次提交真的进了
    平台的记录（`Ctf2Submission` 上那条 `correct_implies_receipt`
    约束守着这条不变量）。

    拿不到回执 id 时退回 `pending` 并记下原因：这是**诚实**的处理，
    而不是"少解析一层"。用户看到一条待确认的记录，
    比看到"已解出"而下次同步又变回未解出要好得多。
    """
    if not isinstance(payload, dict):
        return Ctf2SubmissionStatus.PENDING.value, "平台返回了无法解析的响应。", None

    receipt = None
    for key in ("id", "submission_id", "submissionId"):
        value = payload.get(key)
        if value is not None and str(value).strip():
            receipt = str(value).strip()
            break

    message = str(payload.get("message") or payload.get("detail") or "")[:500]

    correct = payload.get("correct")
    if correct is None:
        correct = payload.get("is_correct")
    if correct is None:
        # ★ CTF2 官方的字段名是 `accepted`，不是 `correct` / `is_correct`。
        #
        # 实测（2026-09-29，真实平台，`practice=BUUCTF` 的 hashcat 题，
        # 故意提交一个错的 flag）：
        #   {"accepted": false, "attempt": 1, "earned_points": 0,
        #    "is_solved": false, "points": 0, "solved_sub_flag_count": 0,
        #    "submission_id": "b3a0790e-…", "total_sub_flag_count": 1}
        # 响应里**没有** `correct` / `is_correct` / `status` ——
        # 只认旧字段名的话，连一次正确的提交也会掉到下面那个
        # `status` 兜底分支，被 `"" in (...)` 判成 False → 记成 wrong。
        #
        # `accepted` 与 `is_solved` 不是同一件事：套题
        # （`total_sub_flag_count` > 1）里单个子 flag 命中时
        # `accepted=true` 而 `is_solved` 仍是 false。所以判定用
        # `accepted`（这一次提交平台收不收），`is_solved` 只作佐证。
        accepted = payload.get("accepted")
        if isinstance(accepted, bool):
            correct = accepted
        elif isinstance(payload.get("is_solved"), bool):
            correct = payload["is_solved"]
    if correct is None:
        # 有些实现用 status 字符串。
        correct = str(payload.get("status") or "").strip().lower() in (
            "correct",
            "accepted",
            "ok",
        )

    if correct is True or correct == 1:
        if receipt:
            return (
                Ctf2SubmissionStatus.CORRECT.value,
                message or "平台判定 flag 正确。",
                receipt,
            )
        return (
            Ctf2SubmissionStatus.PENDING.value,
            "平台表示正确，但没有给出提交回执 id，无法确认这次提交真的入账。",
            None,
        )
    if correct is False or correct == 0:
        return (
            Ctf2SubmissionStatus.WRONG.value,
            message or "平台判定 flag 不正确。",
            receipt,
        )
    return Ctf2SubmissionStatus.PENDING.value, message or "平台未给出明确判定。", receipt


def _advance_challenge(
    challenge: Ctf2Challenge, status_value: str, flag: str, *, already_solved: bool = False
) -> None:
    """按提交结果推进题目状态。

    ## ★ 判定一出，候选就清空（与拆分前的行为相反）

    旧行为是 `wrong` 时把 flag 留在题目行上，理由是"别丢掉我们最后
    试的是哪一个"。那个诉求本身成立，但它现在有**更准的出处**：
    `ctf2_submissions` 里那条提交记录带着 flag、判定、时间与回执 ——
    而题目行上留一个字符串会在下一次提交时变成过期信息。

    留着它的真实代价是：这是一列**看起来像答案**的东西。safe 模式下
    用户看到候选 A → 提交 → 判错 → 状态回到未解出，而 A 还在那里。
    于是"这道题没解出，但界面上有一个 flag"。

    清空之后语义变得单一：
    · `candidate_flag` 有值 ⟺ 有一个等着人确认的候选；
    · `confirmed_flag` 有值 ⟺ 平台判过对。

    ⚠️ 与 `models` 里的两条 CHECK 一起看 —— 它们在数据层保证
    "两列不会同时有值"，也就是"解出之后候选不会还挂着"。
    """
    if already_solved:
        # ★★ 平台说这题**本队伍已经解出** —— 这不是"我们失败了"。
        #
        # 走默认的 `failed` 分支会写出 `status=failed` +
        # `last_error="提交请求失败（未到达平台）。"`，而后半句在这里
        # 是**假的**：请求明明到了平台，平台还回了一个明确的机器码。
        # 于是一道已经解出的题在界面上显示"失败"，原因还写错了。
        #
        # 所以这里显式回到 `unsolved`：
        # · 不写 `failed` —— 我们没失败，只是**没得做**；
        # · 不写 `solved` —— 平台没认可我们交的这个 flag（见
        #   `submit_candidate` 里的说明）；
        # · **不动 `candidate_flag`** —— 那个候选还没被判定过，
        #   它不是"错的"。清掉它等于让用户白跑一轮解题才能再拿到它。
        #
        # ⚠️ 但**已解出的题也不能降级**：它带着我们自己的 `confirmed_flag`，
        #    改成 `unsolved` 会撞 `ck_ctf2_challenges_confirmed_only_when_solved`
        #    （`confirmed_flag IS NULL OR status='solved'`），得到 500。
        #    真实会走到这里的形状：拿一道**自己解出过**的题再次提交，
        #    平台回 `ALREADY_SOLVED` —— 那时它本来就该保持 `solved`。
        #    （这个坑与 `submit_candidate` 里那处是同一个，两处一起修。）
        if challenge.status == Ctf2ChallengeStatus.SOLVED.value:
            return
        challenge.status = Ctf2ChallengeStatus.UNSOLVED.value
        challenge.last_error = PLATFORM_ALREADY_SOLVED_MESSAGE
        return

    if status_value == Ctf2SubmissionStatus.CORRECT.value:
        challenge.status = Ctf2ChallengeStatus.SOLVED.value
        challenge.confirmed_flag = normalize_flag(flag)
        challenge.candidate_flag = None
        challenge.solved_at = now_utc()
        challenge.last_error = None
    elif status_value == Ctf2SubmissionStatus.WRONG.value:
        # ★ 只有真正被平台判错的才计数（见 vocab.CTF2_CREDITED_WRONG_STATUSES）。
        challenge.wrong_submission_count = int(challenge.wrong_submission_count or 0) + 1
        challenge.candidate_flag = None
        if challenge.remaining_wrong_submissions <= 0:
            challenge.status = Ctf2ChallengeStatus.FAILED.value
            challenge.last_error = "错误提交次数已用尽。"
        else:
            challenge.status = Ctf2ChallengeStatus.SOLVING.value
    elif status_value == Ctf2SubmissionStatus.FAILED.value:
        # ⚠️ 请求没到达平台（`failed`）时**候选也不清**：那个候选还没被
        # 判定过，它不是"错的"。清掉它等于让用户重跑一次解题才能再交。
        challenge.status = Ctf2ChallengeStatus.FAILED.value
        challenge.last_error = "提交请求失败（未到达平台）。"


# ══════════════════════════════════════════════════════════
# worker 入口
# ══════════════════════════════════════════════════════════


def build_spec(
    db: Session,
    challenge: Ctf2Challenge,
    *,
    mode: str | None = None,
    resume: bool = False,
    attempt_id: str | None = None,
) -> SolveSpec:
    """从数据库行拼出 `SolveSpec`。**唯一**读凭据的地方。

    凭据通过 `providers.service.decrypt_provider_key` 拿（复用既有的
    "明文只在一处出现"那条纪律）；额度默认值来自 CTF2 自己的配置行
    （`service.effective_ctf2_settings`），而不是环境变量 ——
    见 `settings.models.Ctf2Config` 的说明。

    ## ★★ `attempt_id`：策略以快照为准（v0.28 修）

    这是那条时序漏洞的修复点。在这之前，`mode` / 回合预算都在**这里**
    现读 `effective_ctf2_settings(db)` —— 也就是"worker 真正开跑的
    那一刻"的全局设置，而它与"用户点解题的那一刻"之间隔着一段排队
    时间。后果见 `service.start_solve` 的说明（用户选了 safe 却被
    auto 提交）。

    现在的优先级是**快照 > 全局**：

      · 有 `attempt_id` → 读 `Ctf2SolveAttempt`，用它里面的 mode /
        回合预算 / brief / 模型。**不再读全局设置**（那正是修复的意义）；
      · 没有（测试直接调、或历史任务）→ 退回读全局，行为与修复前一致。

    ⚠️ 保留全局兜底不是为了"两条路并存"，而是因为 `build_spec` 是个
       公开函数（测试与脚本会直接调它）。让它在没有快照时直接报错，
       会把"兼容旧调用"变成"所有旧调用都炸"。
    """
    from app.ctf2 import service as ctf2_service
    from app.providers.models import ProviderAccount
    from app.providers.service import decrypt_provider_key

    # 沙箱配额与 `scans` 侧读的是**同一行配置**（见下面 `sandbox_row` 的说明）。
    # 局部导入与上面几个一致：这个模块被 scheduler 的线程反复调用，
    # 而 settings 领域只在真正启动一次解题时才需要。
    from app.settings import service as settings_service

    # ── 策略快照（有就用它，没有才读全局）────────────────────
    attempt: Ctf2SolveAttempt | None = None
    if attempt_id:
        attempt = db.get(Ctf2SolveAttempt, attempt_id)
        if attempt is None:
            # 拿不到就退回旧行为，但**必须出声**：这意味着入队与启动
            # 之间快照被删了（或者 id 传错了），而"静默退回读全局"
            # 正是这次要消灭的那类问题。
            logger.warning(
                "找不到解题策略快照 %s（题目 %s），退回读全局设置 —— "
                "这次解题的策略可能不是受理时定下的那个",
                attempt_id,
                challenge.id,
            )

    provider = (
        db.execute(
            select(ProviderAccount).order_by(
                ProviderAccount.is_default.desc(), ProviderAccount.created_at
            )
        )
        .scalars()
        .first()
    )
    if provider is None:
        raise Ctf2Error(
            "还没有配置 LLM provider，无法解题。请先在设置页添加一个 provider。"
        )

    environment = (
        db.execute(
            select(Ctf2Environment)
            .where(Ctf2Environment.challenge_id == challenge.id)
            .order_by(Ctf2Environment.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    access_urls = tuple(environment.access_urls or []) if environment else ()
    settings_row = ctf2_service.effective_ctf2_settings(db)

    if attempt is not None:
        # ★ 快照优先。三个值全部来自它，**一个字都不读全局设置**。
        resolved_mode = attempt.mode
        resolved_turns = int(attempt.max_turns)
        resolved_brief = attempt.brief
        # 续解标志同样以快照为准：它也是"这一次尝试"的输入，
        # 而落库之后不怕服务重启（内存里的 `_resume` 会丢）。
        resume = attempt.resume
        # 快照里有模型就用它（provider 配置可能已经被改过）。
        if attempt.llm_model:
            provider = _provider_from_snapshot(db, attempt, fallback=provider)
    else:
        resolved_mode = mode or settings_row.submit_mode
        resolved_turns = int(settings_row.max_turns or DEFAULT_CTF2_MAX_TURNS)
        resolved_brief = None

    # ★ 沙箱配额（2026-10-10 补）。
    #
    # ## 这里原来漏了，而后果不是"少个优化"
    #
    # `SolveSpec` 声明了 `cpus` / `memory_mb`，`_run_worker` 忠实地传给
    # `SandboxContainer`，`_argv` 也忠实地在非空时拼 `--memory` / `--cpus`
    # —— **四个环节三个对，唯独源头没赋值**。于是默认值 `None` 一路传到
    # `_argv` 的 `if` 里恒为假：**CTF2 的解题容器从来没有 CPU 与内存上限**。
    #
    # 而 `_argv` 的参数表里白纸黑字写着这两个挡的是
    # 「LLM 让它跑 `hashcat` 会吃满机器」—— 防护写了，没接上电。
    # `--pids-limit 512` 只挡 fork 炸弹，挡不住单进程吃满内存或占满所有核。
    #
    # ## 为什么走 `read_sandbox_config` 而不是在这里给个默认值
    #
    # 因为设置页里**已经有**这一组配置（`SandboxConfig.cpu_limit` /
    # `memory_limit_mb`），而 `scans` 侧就是从这里读的
    # （见 `scans/service.py` 的 `sandbox_cpus=...`）。
    # 在这里硬编码默认值会让"设置页改了配额"对 CTF2 无效 ——
    # 又是一次"改了 A 不动 B"，而那正是这次修复要消灭的东西。
    #
    # ⚠️ 没有配置行时留 `None`（= 不下发参数 = Docker 默认不限额）。
    #    这与 `scans` 侧的处理一致：**不替用户编一个他没设过的上限**。
    sandbox_row = settings_service.read_sandbox_config(db)
    sandbox_cpus = sandbox_row.cpu_limit if sandbox_row else None
    sandbox_memory_mb = sandbox_row.memory_limit_mb if sandbox_row else None

    # ★ 续解：从上次的过程文件里还原"做过什么"。
    #
    # ⚠️ 只在**明确要求**时做，不自动 —— 用户点"再解一次"时可能恰恰是想
    #    换个思路重来，而"接着上次走"是另一个意图。两种都是真实需求，
    #    所以让它成为一个显式选择，而不是替他决定。
    resume_digest: list[str] = []
    if resume:
        path = latest_transcript(challenge.id)
        if path is None:
            logger.info("要求续解，但找不到上次的解题过程（这次从头开始）")
        else:
            try:
                resume_digest = digest_from_transcript(
                    path.read_text(encoding="utf-8", errors="replace")
                )
            except OSError as exc:
                # 读失败**不影响解题**（只是少了一点上下文），所以不抛。
                logger.warning("读取上次解题过程失败（这次从头开始）：%s", exc)
            else:
                logger.info("续解：从上一次的过程里还原了 %d 步", len(resume_digest))

    return SolveSpec(
        challenge_id=challenge.id,
        title=challenge.title,
        category=challenge.category or "",
        description=challenge.description or "",
        profile=challenge.sandbox_profile or Ctf2SandboxProfile.BASE.value,
        access_urls=access_urls,
        attachments=tuple(_attachment_paths(challenge)),
        llm_model=provider.model,
        llm_api_key=decrypt_provider_key(provider),
        llm_api_base=provider.base_url or "",
        llm_label=provider.label,
        llm_kind=provider.kind,
        mode=resolved_mode,
        max_wrong_submissions=int(
            challenge.max_wrong_submissions or DEFAULT_CTF2_MAX_WRONG_SUBMISSIONS
        ),
        network_isolated=not needs_raw_network(challenge.category),
        resume_digest=tuple(resume_digest),
        max_turns=resolved_turns,
        # ★ v0.28：人工线索。`resolved_brief` 来自快照（有快照时）
        #   或 None（没快照的旧路径）—— 后者与修复前的行为一致。
        brief=resolved_brief or "",
        # ★★ v0.32：召回同类题的方法卡。
        #
        # ## 为什么召回放在这里（`build_spec`）而不是 `_initial_prompt`
        #
        # `_initial_prompt` 是纯函数（只读 spec），而查库要 session ——
        # 让它碰库会把"组装 prompt"与"访问数据库"耦合起来。而
        # `build_spec` 本来就有 session，且它正是"把库里的东西变成
        # 一次解题输入"的那一层。
        #
        # ⚠️ 召回失败**不影响解题**：没有卡片只是少了参考，
        #    而"因为查不到卡片就不解题"明显更糟。
        notes_text=_recall_notes_text(db, challenge),
        # 沙箱配额（见上面那段说明：这四个参数原来漏了，容器一直没有上限）
        cpus=sandbox_cpus,
        memory_mb=sandbox_memory_mb,
    )


def _recall_notes_text(db: Session, challenge: Ctf2Challenge) -> str:
    """召回同类题的方法卡并渲染成 prompt 片段。查不到就返回空串。

    ## 为什么吞异常

    召回是**锦上添花**：没有卡片只是少了参考，而"因为查不到卡片就不
    解题"明显更糟。所以任何失败（表不存在、查询出错）都退化成空串。

    ## 为什么在这里打印一条 info

    模型看到卡片与没看到卡片，解题路径会明显不同 —— 所以"这次到底
    召回了没有"是排查时该能一眼看到的事。没有这条日志，复盘时只能
    从 prompt 里翻。
    """
    try:
        from app.ctf2 import notes as notes_mod

        rows = notes_mod.notes_for_challenge(db, challenge)
        if not rows:
            return ""
        text = notes_mod.render_notes_for_prompt(rows)
        logger.info(
            "为 %s 召回了 %d 张同类题方法卡（category=%s）",
            challenge.id,
            len(rows),
            challenge.category or "未分类",
        )
        return text
    except Exception:  # noqa: BLE001 - 召回失败不该拦住解题
        logger.warning("召回同类题方法卡失败（本次不带参考）", exc_info=True)
        return ""


def _provider_from_snapshot(
    db: Session, attempt: Ctf2SolveAttempt, *, fallback
):  # noqa: ANN001, ANN201
    """按快照里记的 `provider_id` 取回 provider；取不到就用 `fallback`。

    ## 为什么按 id 取、而不是"按默认规则重新选一条"

    `build_spec` 选 provider 的规则是 `is_default DESC, created_at ASC`
    的第一条。如果快照只记了模型名而这里重新按规则选，那么"受理之后
    有人把另一个 provider 设成默认"会让这次尝试用上**另一个** provider
    —— 模型名可能相同、但 base_url 与 key 不同，于是行为与受理时不一致。

    所以按 id 精确取回。

    ⚠️ provider 被删掉时退回 `fallback`（并按默认规则重选一条）：
        "受理时那个 provider 已经没了"是一个真实情况（用户会删掉再建），
        而这时**让整道题失败**比"换一条能用的接着跑"更糟 —— 后者至少
        还有机会解出来，而前者确定解不出来。这个取舍与
        `_resolve_provider` 系列函数一致。
    """
    from app.providers.models import ProviderAccount

    if not attempt.provider_id:
        return fallback
    row = db.get(ProviderAccount, attempt.provider_id)
    if row is None:
        logger.warning(
            "快照 %s 记的 provider %s 已不存在，改用当前默认 provider",
            attempt.id,
            attempt.provider_id,
        )
        return fallback
    return row


def _host_resources_path() -> Path | None:
    """宿主只读资源的路径（环境变量指定）。没有就返回 None。

    ⚠️ 路径由**运维**通过环境变量给，不由题目给 —— 题目内容可控，
    拿它当挂载来源等于让出题人决定往容器里挂什么。
    """
    raw = (os.environ.get(HOST_RESOURCES_ENV) or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None


def _attachment_dir(challenge_id: str) -> Path:
    """题目的附件目录。

    ⚠️ 它**可能不存在**（这题没有附件，或者附件还没下载），调用方
    必须按"存在才挂 / 才读"处理 —— 见 `_argv` 与 `_attachment_paths`。
    """
    return get_settings().scan_root / "_ctf2_attachments" / challenge_id


def _workspace_dir(challenge_id: str, *, create: bool = False) -> Path | None:
    """跨容器存活的持久工作区（宿主侧路径）。v0.29 补。

    ## 它和 `_attachment_dir` 的区别

    | | `_ctf2_attachments/` | `_ctf2_workspaces/` |
    | --- | --- | --- |
    | 谁写 | **服务端**（同步时下载） | **容器里的模型** |
    | 挂载 | 只读（`:ro`） | **读写（`:rw`）** |
    | 内容 | 题目给的原件 | 解题产物（解压/反编译/脚本） |
    | 生命周期 | 跟着**题目** | 跟着**这道题的多次尝试** |

    ## 为什么 `create` 是显式开关

    读路径（比如列出已有产物）**不该**顺手建目录 —— 那会让"这道题从没
    跑过"与"跑过但没留下东西"变成同一个状态。只有真正要起容器时才
    `create=True`。

    ## ⚠️ 路径安全

    `challenge_id` 来自数据库主键（`ctf2chal_<ULID>`），不是用户输入。
    但仍然做一次形状校验 —— 这个目录会被**读写挂载**进容器，
    而路径拼接一旦能被影响就是"任意宿主目录暴露给不可信内容"。
    """
    if not challenge_id or "/" in challenge_id or "\\" in challenge_id or ".." in challenge_id:
        logger.warning("拒绝为可疑的 challenge_id 建工作区：%r", challenge_id)
        return None
    path = get_settings().scan_root / "_ctf2_workspaces" / challenge_id
    if create:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # 建不出来**不影响解题**（只是没有持久区），所以不抛。
            logger.warning("创建工作区失败（本次解题没有持久区）：%s", exc)
            return None
    return path if path.is_dir() else None


def _attachment_paths(challenge: Ctf2Challenge) -> list[Path]:
    """题目附件的**本地已下载副本**。

    ⚠️ 附件下载由**拉题/同步动作**负责（`service.sync` 与
    `service.pull_challenge` 都调 `attachments.download_all`），
    这里只读它落盘的结果。把下载放在解题路径上会让一次解题依赖网络，
    而下载失败时 agent 会对着一个空目录分析半天 —— 那是纯粹的浪费。

    ## ★ 必须排除指纹 sidecar（2026-10-12）

    附件下载完会在旁边写一个 `<name>.sha256`（见
    `attachments._write_fingerprint`）。它是**给同步逻辑用的**，
    不是题目材料 —— 不过滤的话：

      · 它会出现在给模型的"附件清单"里（`- foo.zip.sha256（65 字节）`），
        而模型会去 `cat` 它、试图从里面找线索；
      · `read_attachment` 也会把它当附件读。

    症状是"模型在分析一个只有哈希值的文件"，而它看起来像是题目给的。
    """
    directory = _attachment_dir(challenge.id)
    if not directory.is_dir():
        return []
    # 后缀常量从 `attachments` 取（不在这里重写一份）—— 两处各写一份
    # 就会漂移，而漂移的症状是"指纹文件又混进附件清单了"。
    from app.ctf2.attachments import FINGERPRINT_SUFFIX

    return [
        p
        for p in sorted(directory.iterdir())
        # 只排除我们自己的 sidecar —— 用**精确后缀**而不是"含 .sha256"：
        # 题目真给一个叫 `hashes.sha256.txt` 的文件时，那条规则会误伤它。
        if p.is_file() and not p.name.endswith(FINGERPRINT_SUFFIX)
    ]


#: 过程文件的目录名。`_save_transcript` 写它、`latest_transcript` 读它 ——
#: 两边共用一个常量，免得改名时只改一处。
TRANSCRIPT_DIR = "_ctf2_solves"

# 从过程文件里还原动作用的正则。
#
# ⚠️ 它认的是 `solve_in_sandbox` 写进 `log` 的那两种行（见那里的
#    `log.append`）—— 换句话说这是**我们自己的格式**，不是猜别人的。
#    但改了日志格式就要同步改这里，所以两边各留一条注释互相指路。
_TRANSCRIPT_TOOL = re.compile(r"^\[turn (\d+)\] tool (\w+) (.*)$")
_TRANSCRIPT_RESULT = re.compile(r"^\[turn (\d+)\] result: (.+)$")


def _one_line(text: str, limit: int) -> str:
    """压成单行并截断。**不加"已截断"提示** —— 纪要是索引，不是正文。"""
    return " ".join(text.split())[:limit]


def latest_transcript(challenge_id: str) -> Path | None:
    """这道题最近一次的解题过程文件。没有就返回 `None`。"""
    root = get_settings().scan_root / TRANSCRIPT_DIR
    try:
        files = sorted(root.glob(f"{challenge_id}-*.txt"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    return files[-1] if files else None


def digest_from_transcript(text: str, *, limit: int = MAX_DIGEST_LINES) -> list[str]:
    """把上一次的解题过程，还原成"动作纪要"。

    ## 为什么是"还原成纪要"，而不是把整个过程塞回上下文

    一份过程文件动辄 60–115 KB，而其中**绝大部分是工具的原始输出**。
    直接塞回去会把预算吃光，然后模型又开始"忘记题目要求" ——
    那就把"续解"变成了"更糟的重来"。

    值得留下的只有一行：**跑过什么命令、它的输出开头是什么**。
    那恰好是 `digest` 的形状，所以续解能直接复用同一套机制，
    而不是新发明一条"恢复上下文"的路径。

    ⚠️ 工具的原始输出在落盘时已经被 `_clip(…, 2000)` 截过一次，这里
       再截到 140 —— 两层的理由不同：第一层是"别让一个文件撑爆日志"，
       第二层是"纪要只用来认出这件事做过"。
    """
    tools: dict[int, str] = {}
    results: dict[int, str] = {}
    for line in text.splitlines():
        match = _TRANSCRIPT_TOOL.match(line)
        if match:
            tools[int(match.group(1))] = f"{match.group(2)} {match.group(3)}"
            continue
        match = _TRANSCRIPT_RESULT.match(line)
        if match:
            results[int(match.group(1))] = match.group(2)

    lines: list[str] = []
    for turn in sorted(tools):
        lines.append(
            f"[t{turn}] {_one_line(tools[turn], 110)} → {_one_line(results.get(turn, ''), 140)}"
        )
    # 只保留最后 `limit` 条：太久远的动作对"接下来怎么做"帮助有限，
    # 而最近的几步才决定当前方向。
    return lines[-limit:]


def _save_transcript(challenge_id: str, outcome: SolveOutcome) -> Path | None:
    """把解题过程写到文件。返回路径；没内容或写失败时返回 `None`。

    ## 为什么必须落盘

    `SolveOutcome.transcript` 一直是**被填充**的（`solve_in_sandbox`
    内部就把整个过程 join 好了，见它的 1346 行附近），但 `_run_worker`
    拿到之后只用了 `status` / `candidates`，**过程本身被丢掉了**。

    后果是解题失败时日志里只剩一句"无有效候选"，而"模型到底做了什么"
    完全不可见 —— 实测里这直接把排查拖成黑盒：看得见 `turns=18`，
    看不见它有没有找到附件、跑到哪一步停、最后为什么没给出 flag。

    ⚠️ 落盘失败**不影响**解题结果（它只是诊断产物），所以吞 `OSError`。
    ⚠️ 目录与附件**分开**：附件跟着题目走（重跑还要用），过程日志是
       一次性的，攒多了要能整目录删掉。
    """
    if not outcome.transcript:
        return None
    try:
        root = get_settings().scan_root / TRANSCRIPT_DIR
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{challenge_id}-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        path.write_text(outcome.transcript, encoding="utf-8")
        return path
    except OSError as exc:
        logger.warning("解题过程落盘失败（不影响解题）：%s", exc)
        return None


def run_worker(
    challenge_id: str, *, resume: bool = False, attempt_id: str | None = None
) -> None:
    """**worker 线程的入口**（同步）。调度器调它。

    ## 三段式的分工（这是本模块最容易写错的地方）

    ```
    ① sync : 读库 → SolveSpec（含明文凭据）
    ② async: 起环境、起沙箱、解题、提交 —— 只在这两处短暂碰库
    ③ sync : 把结果写回库
    ```

    为什么不混在一起：本项目的 SQLAlchemy 用的是**同步** Session，
    它在 event loop 里被 await 包着调用会**阻塞整个循环**。这里的
    循环是 worker **自己的**（`asyncio.run` 在它自己的线程里），
    所以短暂的同步 DB 操作没有其他人受害 —— 但那个边界必须写在
    明处，否则有人会把它搬到请求路径上（那里会卡住所有并发请求）。

    ## `attempt_id`：这次按什么策略跑（v0.28）

    受理时落下的策略快照 id。传进来之后，**mode / brief / 回合预算 /
    模型全部以快照为准**，不再读当时的全局设置 —— 见
    `service.start_solve` 里那段"为什么策略必须快照"的说明。

    ⚠️ 它可以是 `None`（测试直接调、或历史任务）：那时退回读全局
    设置的旧行为。这是**兼容性兜底**，不是推荐路径。
    """
    try:
        asyncio.run(_run_worker(challenge_id, resume=resume, attempt_id=attempt_id))
    except Exception as exc:  # noqa: BLE001 - worker 绝不能把异常抛进调度线程
        logger.exception("CTF2 解题 worker 崩溃：%s", challenge_id)
        try:
            with session_scope() as db:
                challenge = db.get(Ctf2Challenge, challenge_id)
                if challenge is not None and challenge.status in (
                    Ctf2ChallengeStatus.SOLVING.value,
                    # ★ 加上 environment_ready（2026-10-10 修）。
                    #
                    # 与 `_record_outcome` 是同一个洞的另一半：
                    # worker 起来之后主状态就是它，而不是 `solving`。
                    # 漏了它，崩溃时这道题会**永久留在"环境已就绪"**——
                    # 而 `finally` 里的 `release_environment` 已经把环境
                    # 停掉、`access_url` 清空，两边说法不一致且看不出来。
                    Ctf2ChallengeStatus.ENVIRONMENT_READY.value,
                    Ctf2ChallengeStatus.SUBMITTING.value,
                ):
                    challenge.status = Ctf2ChallengeStatus.FAILED.value
                    challenge.last_error = _safe_text(
                        f"worker 异常：{type(exc).__name__}: {exc}"
                    )
        except Exception:  # noqa: BLE001
            logger.exception("回写失败状态时又出错了：%s", challenge_id)


def _has_correct_submission(db: Session, challenge_id: str) -> bool:
    """这题在我们这边**有没有一条被平台判对的提交**。

    ★ 它是「平台说已解出」与「我们解的」之间的唯一判据，用来区分两种情况：

    | 平台 | 本地有 correct | 含义 | 该怎么办 |
    | --- | --- | --- | --- |
    | 已解出 | 有 | 是我们解的 | 照常解 —— 用户可能是**重置后想重跑验证** |
    | 已解出 | 没有 | 队友在网页端解的 | 别解 —— 我们拿不到 flag，白烧一轮 |

    历史提交是**权威且不会过期**的：一次 `correct` 一旦落库，这题就永远
    算"我们解出过"。所以这里查提交表而不是题目行的 `status` ——
    `status` 会被重置、会被取消打回，而提交记录不会。

    ⚠️ 与 `already_submitted` 的区别：那个问的是"**这个 flag** 交过没有"
    （防重复提交同一个值），这个只问"这题我们解出过没有"。
    """
    return (
        db.execute(
            select(Ctf2Submission.id)
            .where(
                Ctf2Submission.challenge_id == challenge_id,
                Ctf2Submission.status == Ctf2SubmissionStatus.CORRECT.value,
            )
            .limit(1)
        ).scalar()
        is not None
    )


async def _run_worker(
    challenge_id: str, *, resume: bool = False, attempt_id: str | None = None
) -> None:
    """worker 的 async 主体。见 `run_worker` 的三段式说明。"""
    from app.ctf2 import service as ctf2_service

    # ── ① 同步段：准备 ────────────────────────────────────
    with session_scope() as db:
        challenge = db.get(Ctf2Challenge, challenge_id)
        if challenge is None:
            logger.warning("要解的题不存在：%s", challenge_id)
            return
        # ★ 防御：已解出的题**直接跳过**，不要让它跑到 0.5 秒后撞上
        #   `ck_ctf2_challenges_confirmed_only_when_solved`。
        #
        # 产品路径本来是拦住的（`service._assert_resolvable` 对 `solved`
        # 抛 409 `CTF2_ALREADY_SOLVED`），但 `run_worker` 是个**公开入口**
        # —— 调度器直接调它，脚本也能直接调。绕过 service 层时，撞上的是：
        #
        #   CheckViolation: new row ... violates check constraint
        #   "ck_ctf2_challenges_confirmed_only_when_solved"
        #
        # 那条约束本身是**对的**（已解出的题必然带着 `confirmed_flag`，
        # 而解题第一步要把状态改成 `solving`，两者不能并存）。问题在于
        # 它的**表达**完全不像给调用方看的话 —— 读起来像"字段写错了"，
        # 而不是"这题已经解过了"。
        #
        # 实测（2026-10-01）：脚本直接重解一道已解出的题，拿到的就是
        # 上面那条看不懂的约束错误。
        if challenge.status == Ctf2ChallengeStatus.SOLVED.value:
            logger.info("这道题已经解出，跳过解题：%s", challenge_id)
            return
        try:
            spec = build_spec(db, challenge, resume=resume, attempt_id=attempt_id)
        except Ctf2Error as exc:
            challenge.status = Ctf2ChallengeStatus.FAILED.value
            challenge.last_error = _safe_text(exc.message)
            ctf2_events.emit_abandoned(db, challenge, reason=exc.message)
            db.commit()
            return

    # ── ★★ 开跑前先问平台一句：这题本队伍是不是**已经被别人**解出了 ──
    #
    # ⚠️ 位置很讲究：必须在 `build_client_sync()` **之前**。
    #    放到它后面，下面那个 `return` 会把已经建好的 httpx client
    #    漏掉 —— 它由本函数的 `finally` 负责关闭，而 `finally` 在更下面
    #    的 `try` 上。这个函数一道题只跑一次，泄漏一个 client 不会立刻
    #    出事，但会随解题次数线性累积。
    #    （`platform_reports_solved` 自己建、自己关，所以这里没有负担。）
    #
    # 为什么是**现问**而不是靠"同步时落库的布尔值"：同步是周期性的，而
    # "队友刚在网页端交了"可以发生在两次同步之间 —— 落库的值会过期，
    # 而过期的安全判断等于没有。这一次查询的代价是一个请求，换来的是
    # "永远不会在一道已经解出的题上白烧一整轮 LLM"。
    #
    # ★ 判据是「平台说已解出」**且**「我们这边没有一条 correct 提交」。
    #   两个条件缺一不可：
    #   · 只看平台 → 会把「重置回未解出、想重跑一遍验证」这条合法流程
    #     永久锁死（那正是 `models` 里说的"存着 flag 但状态退回 unsolved
    #     是合法的"）。我们自己解出的题，平台当然说已解出。
    #   · 只看本地 → 就是修复前的行为：队友解的题我们一无所知。
    #
    # ⚠️ 探测失败**不阻断**解题（fail-open）：它是安全网，不是必经之路。
    #    做成硬门槛的话，一次网络抖动就会让所有题都点不动 —— 那比
    #    "偶尔白跑一轮"糟得多。真正的兜底在提交那一步（见
    #    `submit_candidate` 对 `ALREADY_SOLVED` 的处理）。
    try:
        reports_solved = await ctf2_service.platform_reports_solved(challenge_id)
    except Ctf2Error as exc:
        logger.warning(
            "开跑前的平台状态探测失败（不阻断解题）：%s → %s", challenge_id, exc
        )
        reports_solved = False

    if reports_solved:
        with session_scope() as db:
            blocked = db.get(Ctf2Challenge, challenge_id)
            if blocked is not None and not _has_correct_submission(db, challenge_id):
                # ★ 回到 `unsolved` 而**不是** `failed`：这不是我们的失败。
                #   写成 `failed` 会让一道**已经解出的题**在界面上显示
                #   "失败" —— 那正是最容易误导用户的地方。
                blocked.status = Ctf2ChallengeStatus.UNSOLVED.value
                blocked.last_error = PLATFORM_ALREADY_SOLVED_MESSAGE
                ctf2_events.emit_abandoned(
                    db, blocked, reason=PLATFORM_ALREADY_SOLVED_MESSAGE
                )
                db.commit()
                logger.info(
                    "平台显示这题已被本队伍解出（且不是我们解的），跳过解题：%s",
                    challenge_id,
                )
                return

    client = ctf2_service.build_client_sync()
    # ⚠️ 这个 client 的生命周期**只归本函数管**（下面的 `finally` 里
    #    `release_environment` 要用它发 DELETE）。
    #
    # 原文写的是"它会被 `start_environment_for` / `submit_candidate`
    # 里的 `async with` 关掉，所以这里不再单独 aclose" —— **那段是错的**，
    # 而且错的方向很危险：它把一个 bug 描述成了设计。
    #
    # 事实是：`submit_candidate` 确实用 `async with`，但它应该关的是
    # **自己 factory 造出来的**那个客户端。当 factory 交出共享实例时
    # （见 `_handle_candidates` 里的说明），提交一成功就把这个 client
    # 关了，后面的收尾再碰它就是
    # `RuntimeError: Cannot send a request, as the client has been closed.`
    #
    # 现在 factory 改成每次新建，所以这里可以放心地让共享 client
    # 活满整个 worker —— 它由本函数负责，不假手他人。
    sandbox: SandboxContainer | None = None
    outcome = SolveOutcome()

    try:
        started = await ctf2_service.start_environment_for(client, challenge_id)
        # access_urls 是环境才拿到的东西，spec 里那份是空的 ——
        # 补上它，否则 http_request 的白名单里一个地址都没有。
        spec.access_urls = tuple(started.access_urls) or spec.access_urls

        # ★★ 用**平台给的接入类型**纠正网络隔离。
        #
        # `build_spec` 里的 `network_isolated=not needs_raw_network(category)`
        # 是**按题型猜**的：pwn/network 放开，其余一律断网。而平台其实
        # 直接告诉我们这个靶机怎么接 —— `access_type`。
        #
        # 两者不一致时**以平台为准**。典型的错配：
        #
        #   一道 `category=MISC` 的题，靶机却是裸 TCP（`access_type=nc`）。
        #   按题型猜 → 容器 `--network none` → 模型在容器里 `nc`/socket
        #   连不上，而 `http_request` 工具只讲 HTTP，也用不上 ——
        #   **这道题在我们的沙箱里根本无法解**，而失败看起来像模型能力问题。
        #
        # ⚠️ 只在平台**明确说是非 http** 时才放开。平台没给 `access_type`
        #    （老版本/字段缺失）时保持原判 —— 那是安全方向：
        #   猜错的代价是"某道 TCP 题连不上"（可见的失败），
        #   而反向猜错的代价是"一个本可完全断网的容器被放开了网络"。
        if started.access_type and started.access_type.lower() != "http":
            if spec.network_isolated:
                logger.info(
                    "CTF2 靶机接入类型是 %s（非 http），放开沙箱网络：%s",
                    started.access_type,
                    challenge_id,
                )
            spec.network_isolated = False
        # 转达给模型（提示词里要说明"别只试 http_request"）——
        # 见 `_initial_prompt` 的"## 环境"那一段。
        spec.access_type = started.access_type

        sandbox = SandboxContainer(
            # ★ 后缀必须是**随机的**，不能用时间戳取模。
            #
            # 原来写的是 `int(time.time()) % 100000` —— 同一秒内起两次就会
            # 撞名，而 `docker run --name` 撞名是**硬失败**。用户在前端
            # 事件流上看到的那条"启动解题沙箱失败：Docker 容器名冲突"
            # 就是它（2026-09-30 实测），排查时很容易误以为问题出在 Docker。
            #
            # `os.urandom` 不引入新依赖（`os` 已导入），8 个十六进制字符
            # 的碰撞概率可以忽略。
            name=f"{SANDBOX_NAME_PREFIX}{challenge_id[-12:]}-{os.urandom(4).hex()}",
            image=image_for_profile(spec.profile),
            network_isolated=spec.network_isolated,
            cpus=spec.cpus,
            memory_mb=spec.memory_mb,
            host_resources=_host_resources_path(),
            # ★ 附件走**挂载**，不走 `docker cp` —— 见 `SandboxContainer`
            #   的 `attachments_dir` 与 `copy_in` 的说明。
            attachments_dir=_attachment_dir(challenge_id),
            # ★★ 持久工作区（v0.29）：解压/反编译/脚本等产物写在这里，
            #    容器销毁后仍然活着，下次续解能直接用。
            #    `create=True` —— 这是唯一该建目录的地方（见 `_workspace_dir`）。
            workspace_dir=_workspace_dir(challenge_id, create=True),
            # ⚠️ 这里只放**无害的开关**。凭据绝不进容器 —— 见模块 docstring。
            extra_env={"PWNLIB_NOTERM": "1"},
        )
        sandbox.start()

        gateway = LlmGateway(
            model=spec.llm_model,
            api_key=spec.llm_api_key,
            base_url=spec.llm_api_base,
            label=spec.llm_label,
            kind=spec.llm_kind,
        )

        # ★ 过程事件：**每一条单独事务**落库。
        #
        # 为什么不攒到最后一起写：
        #
        # 1. 活动流的意义就是"**边跑边看**"。攒到结尾写的话，用户在解题的
        #    二十几分钟里看到的仍然只有"环境已就绪" —— 而那正是这次要修
        #    的那个抱怨（"没过程解题过程就这样？"）。
        # 2. 解题可能跑很久、也可能被中断 —— 攒着的过程会随进程一起丢，
        #    而它恰恰是失败时最需要的东西（bug 6 就是靠 transcript 挖出来的）。
        #
        # 代价是每轮多两次 commit。相对一次 100ms 级的 LLM 调用可以忽略。
        #
        # ⚠️ 这里再包一层 try：`solve_in_sandbox` 那一侧也包了，但两处
        #    失败的原因不同（那边是回调本身抛，这边是写库失败）。
        #    两层都守，才真的做到"过程不影响结果"。
        def _on_progress(kind: str, summary: str, payload: dict[str, str]) -> None:
            try:
                with session_scope() as db:
                    challenge = db.get(Ctf2Challenge, challenge_id)
                    if challenge is None:
                        return
                    ctf2_events.emit_progress(
                        db, challenge, kind=kind, summary=summary, payload=payload
                    )
                    db.commit()
            except Exception:  # noqa: BLE001 - 过程写不进去不该毁掉解题
                logger.warning(
                    "CTF2 过程事件落库失败（不影响解题）：%s", challenge_id, exc_info=True
                )

        outcome = await solve_in_sandbox(
            spec,
            sandbox=sandbox,
            gateway=gateway,
            system_prompt=load_system_prompt(),
            on_progress=_on_progress,
        )

        await _handle_candidates(challenge_id, spec, outcome)
    except Ctf2EnvironmentTimeout as exc:
        outcome.error = exc.message
        with session_scope() as db:
            challenge = db.get(Ctf2Challenge, challenge_id)
            if challenge is not None:
                challenge.status = Ctf2ChallengeStatus.FAILED.value
                challenge.last_error = _safe_text(exc.message)
        logger.warning("CTF2 环境启动超时：%s（%d 次轮询）", challenge_id, exc.polls)
    except Ctf2Error as exc:
        outcome.error = exc.message
        with session_scope() as db:
            challenge = db.get(Ctf2Challenge, challenge_id)
            if challenge is not None and challenge.status not in (
                Ctf2ChallengeStatus.CANDIDATE_READY.value,
                Ctf2ChallengeStatus.SOLVED.value,
            ):
                challenge.status = Ctf2ChallengeStatus.FAILED.value
                challenge.last_error = _safe_text(exc.message)
        logger.warning("CTF2 解题失败：%s → %s", challenge_id, exc.message)
    finally:
        # ★ 解题过程落盘 —— 放在 finally 里，**异常路径上也要留下现场**。
        #   这段内容是排查"为什么没解出"的唯一线索，见 `_save_transcript`。
        transcript_path = _save_transcript(challenge_id, outcome)
        if transcript_path is not None:
            logger.info("解题过程已保存：%s", transcript_path)

        # ★★ 环境用完**必销毁**，这一条没有例外（含异常路径）。
        #
        # 两层都要清：
        #   ① 本地沙箱容器 —— 不清会留下没人认领的容器，把宿主拖垮；
        #   ② 平台环境 —— 平台的资源是有租期的，不清会一直占着。
        #
        # 用 finally 而不是在每个 except 里各写一遍：后者只要有一处
        # 漏掉，就会在**那条**异常路径上稳定泄漏。而泄漏是不可见的
        # （没有界面显示"有几个容器没清"），所以没人会发现。
        if sandbox is not None:
            sandbox.destroy()
        # ⚠️ `mark_failed=False`：这里是 worker 的收尾，**状态由下面的
        #    `_record_outcome` 按解题结果决定**。
        #
        #    用默认的 True 会在 `_record_outcome` 之前先把题写成
        #    `failed` + "环境已释放。"，而那句话什么也没解释 ——
        #    真实原因（比如"候选被本地格式校验丢弃"）就此丢失。
        #    实测事故见 `service.release_environment` 的 docstring。
        await ctf2_service.release_environment(client, challenge_id, mark_failed=False)

        # ★ 关掉共享 client（2026-10-10 补）。
        #
        # ## 为什么必须有这一行
        #
        # 本函数上面那段注释把"谁负责关这个 client"讲得很细，结论是
        # **「它由本函数负责，不假手他人」** —— 但原来 `finally` 里没有
        # 任何 `aclose()`，全文也没有第二处。也就是说：承诺写了，没落实。
        #
        # ## 影响面如实说明（不要夸大）
        #
        # CPython 的引用计数**通常**会在协程帧释放时回收它、底层 socket
        # 随之关闭，所以这**不是**必然的 fd 泄漏。但：
        #   · 那依赖 GC 时机，属于"依赖实现细节"；
        #   · 会在 `asyncio.run` 已关闭事件循环之后再走清理路径，
        #     留下 `ResourceWarning`；
        #   · 每道题一个连接池，几十道题下来值得把它变成确定的。
        #
        # ⚠️ 顺序不能反：`release_environment` 还要用它发 DELETE 请求。
        #    放在它之前会把那次请求变成"客户端已关闭"的错误。
        # ⚠️ 用 try 包住：关连接失败不该盖住上面刚做完的收尾。
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 - 见上：收尾绝不抛
            logger.debug("关闭 CTF2 共享 client 时出错（忽略）", exc_info=True)

    # ── ③ 同步段：落库 ────────────────────────────────────
    with session_scope() as db:
        challenge = db.get(Ctf2Challenge, challenge_id)
        if challenge is not None:
            _record_outcome(db, challenge, outcome)
            # ★ 再把解题 scan 收尾，且**必须用 `_record_outcome` 之后的
            #   最终状态**来映射（解出 → completed，其余 → failed）。
            #   顺序反了会把一道刚解出的题记成一条 failed 的扫描。
            #
            #   少了这一步的症状（用户截图实测）：题早解完了，扫描列表
            #   却一直显示"运行中 · 0 个智能体 · ⚠ 疑似卡死"——
            #   见 `events.finalize_solve_scan` 的说明。
            try:
                ctf2_events.finalize_solve_scan(db, challenge)
            except Exception:  # noqa: BLE001 - 收尾失败不该盖住解题结果
                logger.warning(
                    "CTF2 解题 scan 收尾失败（不影响解题结果）：%s",
                    challenge_id,
                    exc_info=True,
                )
        db.commit()
    logger.info(
        "CTF2 解题结束：%s turns=%d candidates=%d error=%s",
        challenge_id,
        outcome.turns,
        len(outcome.candidates),
        outcome.error or "无",
    )


async def _handle_candidates(
    challenge_id: str,
    spec: SolveSpec,
    outcome: SolveOutcome,
) -> None:
    """把候选 flag 按 safe / auto 两条路径处理。

    ## ⚠️ 这里**不再接收**外层的 client

    以前签名里有个 `client: Ctf2Client`，并且被塞进 `client_factory`
    交给 `submit_candidate` —— 而那个函数会对它 `async with`（即 aclose），
    等于**替调用方关掉共享资源**。实测后果：提交成功后收尾阶段
    `release_environment` 复用同一个 client 直接炸
    （`RuntimeError: Cannot send a request, as the client has been closed.`），
    而那次提交**其实是成功的**。

    现在每次提交都新建客户端，共享 client 的生命周期只归 `_run_worker`。
    参数随之删掉 —— 留着它只会让"谁负责关"继续含混。
    """
    from app.ctf2 import service as ctf2_service

    if not outcome.candidates:
        return

    for candidate in outcome.candidates:
        # 同步段：读状态 + 决定 + （safe 时）落候选
        with session_scope() as db:
            challenge = db.get(Ctf2Challenge, challenge_id)
            if challenge is None:
                return
            if challenge.status not in (
                Ctf2ChallengeStatus.SOLVING.value,
                Ctf2ChallengeStatus.ENVIRONMENT_READY.value,
                Ctf2ChallengeStatus.CANDIDATE_READY.value,
            ):
                # 用户在这中间取消了 —— 不落候选（见 service.cancel_challenge
                # 里那段"取消杀不掉线程，但那一轮不该产出"的说明）。
                logger.info("CTF2 解题已被取消，丢弃候选：%s", challenge_id)
                return
            already = already_submitted(db, challenge_id=challenge_id, flag=candidate.flag)
            decision = decide_submission(
                mode=spec.mode,
                already=already,
                remaining_wrong=challenge.remaining_wrong_submissions,
                # ★ v0.31 证据门禁：只对 auto 生效。
                #   两条独立的弱信号 —— 有证据文本、且真的跑过工具。
                #   见 `decide_submission` 里那段"为什么需要证据门禁"。
                evidence=candidate.evidence,
                saw_tool_output=outcome.saw_tool_output,
            )
            challenge.candidate_count = int(challenge.candidate_count or 0) + 1
            if decision.action == "hold":
                # ★ safe 模式的落点：候选**落库但不提交**。
                #   写的是 `candidate_flag` 而不是 `confirmed_flag` ——
                #   这两列的区别正是"它猜的"与"平台认的"（见 models）。
                challenge.candidate_flag = candidate.flag
                challenge.status = Ctf2ChallengeStatus.CANDIDATE_READY.value
                ctf2_events.emit_candidate_ready(db, challenge, candidate=candidate)
            elif decision.action == "reject":
                outcome.rejected.append(f"{candidate.flag}：{decision.reason}")
            db.commit()

        if decision.action != "submit":
            continue

        remote_ids = _remote_ids(challenge_id)
        status_value, message = await submit_candidate(
            challenge_id=challenge_id,
            flag=candidate.flag,
            mode=spec.mode,
            # ⚠️ 用默认参数把 `remote_ids` **绑进闭包**，而不是让 lambda
            # 去捕获外层变量。
            #
            # 当前写法其实不会出错（lambda 在同一个迭代里立刻被 await 调用），
            # 但那条依赖很脆：将来有人把提交改成并发（`asyncio.gather`），
            # 所有 lambda 就会一起读到**最后一次**迭代的 remote_ids ——
            # 于是"把 A 题的 flag 提交到 B 题"，而那是消耗额度的对外动作。
            # 绑一次的成本是零，把那个可能性彻底去掉。
            #
            # ── ★ factory 必须**新建**客户端，不能交出手上这个 ──
            #
            # 原来这里返回的是 `(client, bound)` —— 外层那个**共享**的
            # 客户端。而 `submit_candidate` 内部写的是 `async with client:`，
            # 于是它 **aclose 掉了这个共享实例**。
            #
            # 实测后果（2026-09-30，一次真实的端到端）：题目**已经解出、
            # flag 已被平台判定正确**（`status=correct`），随后收尾阶段
            # `release_environment` 复用同一个 client 时炸了：
            #
            #     RuntimeError: Cannot send a request, as the client
            #                   has been closed.
            #
            # 而这个 `RuntimeError` 不是 `Ctf2Error`，穿透了收尾函数的
            # except，把 worker 整个崩掉 —— 用户看到"解题失败"，而实际
            # 结果早已落库。一个"提交成功却报失败"的假象。
            #
            # `ctf2_service.build_client_sync()` 每次调用都建新的，
            # 由 `submit_candidate` 自己关，共享 client 的生命周期
            # 就只归 `_run_worker` 管。
            client_factory=lambda bound=remote_ids: (
                ctf2_service.build_client_sync(),
                bound,
            ),
        )
        if status_value == Ctf2SubmissionStatus.CORRECT.value:
            outcome.submitted.append(candidate.flag)
            with session_scope() as db:
                challenge = db.get(Ctf2Challenge, challenge_id)
                if challenge is not None:
                    ctf2_events.emit_submitted_ok(db, challenge, flag=candidate.flag)
                db.commit()
        elif status_value == Ctf2SubmissionStatus.WRONG.value:
            outcome.rejected.append(f"{candidate.flag}：平台判错（{message}）")
            with session_scope() as db:
                challenge = db.get(Ctf2Challenge, challenge_id)
                if challenge is not None:
                    ctf2_events.emit_submitted_failed(
                        db, challenge, flag=candidate.flag, reason=message
                    )
                db.commit()
        else:
            outcome.rejected.append(f"{candidate.flag}：未确认（{message}）")
            logger.warning("CTF2 提交未确认：%s → %s", challenge_id, message)


def _remote_ids(challenge_id: str) -> dict[str, Any]:
    """取提交需要的远端 id（competition 的 remote_id、challenge 的 remote_id）。"""
    from app.ctf2.models import Ctf2Competition

    with session_scope() as db:
        challenge = db.get(Ctf2Challenge, challenge_id)
        if challenge is None:
            return {}
        competition = db.get(Ctf2Competition, challenge.competition_id)
        return {
            "competition_remote_id": competition.remote_id if competition else "",
            "remote_id": challenge.remote_id,
            "sub_flag_id": challenge.sub_flag_id,
        }


def _record_outcome(db: Session, challenge: Ctf2Challenge, outcome: SolveOutcome) -> None:
    """把解题结果写回题目行。**不覆盖已经成功的状态。**

    ## ★ 收尾前先确认一次"这题是不是已经解出了"

    实测（2026-09-30）踩到的坑：模型**独立地从零解出了题**
    （`office2john` → `hashcat` 爆出 `9919` → `msoffcrypto-tool` 解密 →
    在 app.xml / slide7.xml 两处读到 `okYOUWIN`），产出了 `candidates=1`、
    `error=无`。但因为同一个 flag 已有一条 `correct` 提交记录，去重逻辑
    判为"不重复提交"（**那个判断本身是对的**，重复提交会白白扣额度），
    于是它没有走提交路径 —— 而收尾时题目状态被降级成了 `failed`。

    **结果就是：一道已经解出的题，在界面上显示为失败。**

    所以这里必须回头查一次库：只要存在一条 `correct` 的提交，题目就是
    已解出 —— 与"这一轮有没有新提交"无关。一轮重复解出恰恰是最该
    被当成好消息的情形，不该反而把状态打下去。
    """
    solved = (
        db.execute(
            select(Ctf2Submission.flag).where(
                Ctf2Submission.challenge_id == challenge.id,
                Ctf2Submission.status == Ctf2SubmissionStatus.CORRECT.value,
            )
        )
        .scalars()
        .first()
    )
    if solved is not None:
        # 已经解出：状态与 flag 都要对。
        #
        # ⚠️ `confirmed_flag` **必须一并写上**，不能只改 status：
        #    数据库上有一条 `ck_ctf2_challenges_solved_implies_flag`
        #    约束（`solved` ⟹ 有 `confirmed_flag`），只改状态会撞它：
        #
        #        CheckViolation: new row ... violates check constraint
        #        "ck_ctf2_challenges_solved_implies_flag"
        #
        #    而且这条约束是对的：一个"已解出但没有 flag"的行，界面上
        #    没法回答"答案是什么"。所以这里从那条 correct 提交记录里
        #    把 flag 取回来 —— 那正是平台认过的那个值。
        #
        #    本轮的候选**不覆盖**它：候选是"模型猜的"，`confirmed_flag`
        #    是"平台认的"，这两列的区别见 models 里的说明。
        challenge.status = Ctf2ChallengeStatus.SOLVED.value
        challenge.confirmed_flag = challenge.confirmed_flag or solved
        challenge.last_error = None
        return

    if outcome.error and challenge.status not in (
        Ctf2ChallengeStatus.CANDIDATE_READY.value,
        Ctf2ChallengeStatus.SOLVED.value,
    ):
        challenge.status = Ctf2ChallengeStatus.FAILED.value
        challenge.last_error = _safe_text(outcome.error)
        ctf2_events.emit_abandoned(db, challenge, reason=outcome.error)
    elif challenge.status not in (
        Ctf2ChallengeStatus.CANDIDATE_READY.value,
        Ctf2ChallengeStatus.SOLVED.value,
        Ctf2ChallengeStatus.FAILED.value,
        Ctf2ChallengeStatus.SUBMITTING.value,
    ):
        # 没出错、也没产出候选 —— 回到"未解出"，让人可以再点一次。
        #
        # ★★ 判据从 `== SOLVING` 改成"不在终态/候选态里"（2026-10-10 修）。
        #
        # 原来写的是 `elif challenge.status == Ctf2ChallengeStatus.SOLVING.value`，
        # 而 **worker 跑起来之后主状态是 `environment_ready`**
        # （见 `service.py` 里 `challenge.status = ENVIRONMENT_READY`，
        # 以及 `scheduler.recover_orphans` docstring 里那句
        # "它是解题过程的主状态，不是瞬间中间态"）——
        # 于是那个分支**几乎永远不命中**。
        #
        # 后果很具体，而且是**很常见的路径**：模型照 `_final_stretch_note`
        # 的要求把 candidates 写成空数组时，`outcome.error` 保持 `None`
        # （没有候选 ≠ 出错），于是两个分支都不进 → 题目**永久留在
        # `environment_ready`**。而 `finally` 里的 `release_environment`
        # 已经把环境行标成 `stopped`、`access_url` 清成了 NULL ——
        # 数据库说"环境已就绪"，实际环境早没了，且界面上看不出来。
        #
        # 这与 `sweep_orphan_sandboxes` docstring 里记的那次事故同一种形状：
        # 两边说法不一致，而没有任何界面能发现。
        #
        # ⚠️ 用**排除法**而不是穷举"该收尾的状态"：将来加新状态时，
        #    排除法默认把它算作"需要收尾"，而穷举法默认漏掉它 ——
        #    后者正是这次 bug 的成因。
        challenge.status = Ctf2ChallengeStatus.UNSOLVED.value
        challenge.last_error = "本次未产出候选 flag。"

    # ★★ v0.32：把这次解题提炼成**方法卡**（供同类题复用）。
    #
    # ## 位置在最后：它要读"最终状态"
    #
    # 上面几个分支刚定完 `challenge.status`，而卡片里的 `solved`
    # 就取自它 —— 放在前面会拿到过期的值（那时状态还是 `solving`）。
    #
    # ## 为什么它不影响解题结果
    #
    # 写卡失败**只记日志**（见 `learn_from_solve`）：解题已经结束，
    # 结果是既成事实。让"提炼知识"的失败把一次成功的解题变成报错，
    # 是本末倒置。
    #
    # ## ⚠️ 它写的是**脱敏后**的内容
    #
    # 见 `notes.build_note_fields` —— flag 绝不进卡片，因为卡片会被
    # 注入到**另一道题**的 prompt 里。
    try:
        from app.ctf2 import service as ctf2_service

        ctf2_service.learn_from_solve(db, challenge, outcome)
    except Exception:  # noqa: BLE001 - 提炼知识失败不该影响解题结果
        logger.warning("写解题方法卡失败（不影响解题结果）", exc_info=True)


__all__ = [
    "ATTACHMENT_DIR",
    "Candidate",
    "CommandResult",
    "Ctf2SandboxError",
    "DEFAULT_MAX_TURNS",
    "FINAL_STRETCH_TURNS",
    "MAX_DIGEST_LINES",
    "FLAG_PATTERN",
    "MAX_ANSWER_LENGTH",
    "AnswerFormatError",
    "FlagFormatError",
    "IMAGE_BASE",
    "IMAGE_REVERSE",
    "SANDBOX_NAME_PREFIX",
    "sweep_orphan_sandboxes",
    "ensure_ctf2_network",
    "LlmGateway",
    "LlmReply",
    "SandboxContainer",
    "SolveOutcome",
    "SolveSpec",
    "SubmitDecision",
    "TOOL_NAMES",
    "already_submitted",
    "allowed_hosts",
    "build_spec",
    "decide_submission",
    "dispatch_tool",
    "ensure_docker_available",
    "extract_candidates",
    "flag_fingerprint",
    "guard_tool_url",
    "image_for_profile",
    "interpret_submit",
    "load_system_prompt",
    "needs_raw_network",
    "normalize_flag",
    "parse_action",
    "parse_final",
    "profile_for_category",
    "run_worker",
    "solve_in_sandbox",
    "submit_candidate",
    "validate_flag",
    "validate_answer",
]
