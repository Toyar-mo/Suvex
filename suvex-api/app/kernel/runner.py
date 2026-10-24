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

### 3. 工具集**严格四个**：读附件、发 HTTP、沙箱内执行命令、拉技能速查

这不是"暂时只有几个"，而是**能力边界**：

| 工具 | 在哪执行 | 为什么 |
| --- | --- | --- |
| `read_attachment` | 宿主（读已投进沙箱的那份） | 给 agent 一个**不用起 shell** 就能看题面的路径 |
| `http_request` | **宿主** | 见下 |
| `run_shell` | **沙箱容器内** | 真正的分析工作 |
| `load_skill` | 宿主（读随代码分发的 markdown） | 把「某类漏洞怎么验 / 某工具怎么安全调」按需查出来，不塞满提示词 |

`http_request` 在宿主执行是刻意的：**容器内的网络请求我们拦不住**
（agent 可以 `python -c 'import socket...'` 绕过任何包装）。
把 HTTP 请求收到宿主来，白名单（`guard_tool_url`）才是**能生效的**。

⚠️ 但这只是第一层。容器内的 `run_shell` 仍然可以发起任意连接 ——
所以第二层是 Docker 网络，按题型分配：pwn / 网络类直接放开（要连 TCP）；
crypto / 取证 / 逆向用最严的 `--network none`；
**web 自 2026-10-18 起也放开** —— 容器里的 `gobuster` / `ffuf` 才能做目录
探测，光靠 `http_request` 一次一个 URL 扫不出没人告诉你的路径。放开之后
的边界由 iptables 白名单收窄到 dasctf 靶站网段（判据见 `needs_raw_network`）。
两层的关系写在 `guard_tool_url` 与 `SandboxContainer._argv` 的注释里。
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
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
from app.core.prompts import load_offensive_playbook
from app.core.text import strip_nul
from app.core.timeutil import now_utc
from app.core.vocab import (
    DEFAULT_CTF2_MAX_TURNS,
    DEFAULT_CTF2_MAX_WRONG_SUBMISSIONS,
    IR_CATEGORY_PREFIX,
    KernelChallengeStatus,
    KernelCompetitionKind,
    KernelSandboxProfile,
    KernelSubmissionStatus,
    KernelSubmitMode,
)
from app.kernel import events as ctf2_events
from app.kernel import mcp as mcp_mod
from app.kernel import skills as skills_mod
from app.kernel import tools as tools_mod
from app.kernel.client import (
    CTF2_ERROR_ALREADY_SOLVED,
    KernelEnvironmentTimeout,
    KernelError,
    KernelResponseError,
    assert_allowed_target,
)

# ★ v0.41：攻防知识互查。顶层 import 是安全的 —— 那个模块顶层只依赖
# 标准库，`findings` 的 import 发生在**函数内**（见 `recall_for_asset`），
# 所以不会与本文件撞环。
from app.kernel.history import recall_for_challenge
from app.kernel.models import (
    CTF2_OFFICIAL_HOSTS,
    KernelChallenge,
    KernelCompetition,
    KernelEnvironment,
    KernelSolveAttempt,
    KernelSubmission,
)

logger = logging.getLogger("suvex.ctf2")

# ══════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════

# 镜像名。**与 `KernelSandboxProfile` 的取值逐字一致** ——
# 这里不再做一层"逻辑名 → 镜像名"的映射，因为两层映射会让
# "这个字段到底存的是什么"变成一个每次都要查的问题
# （与 vocab 里那条注释同一条理由）。
# ## 为什么这两个可以被环境变量覆盖
#
# 镜像名是**部署事实**，不是代码常量：`docker/README.md` 里写着重建后
# 老 tag 会被覆盖，而"某个提交对应哪个镜像"在压测/对照里必须能钉住。
# 没有这个入口时，想拿 `suvex-sandbox:pre-gems` 跑一遍"补强前"基线，
# 只能改代码再改回来 —— 那是**一次改动两处**，中间任何一次忘改都会
# 让对照变成自己跟自己比。
#
# ⚠️ 只在**进程启动时**读一次（`import` 时）。不改 `image_for_profile`
#   的语义：它仍然只是 profile → 镜像名，覆盖发生在更早的一层。
#   默认值就是枚举值，所以不设这两个变量时行为与以前**逐字节一致**。
IMAGE_BASE = os.environ.get("CTF2_IMAGE_BASE") or KernelSandboxProfile.BASE.value
IMAGE_REVERSE = os.environ.get("CTF2_IMAGE_REVERSE") or KernelSandboxProfile.REVERSE.value
# ★ v0.41：应急响应镜像。与上面两个**完全同构**，包括"不设变量时
# 行为逐字节一致"这条 —— 默认值就是枚举值。
#
# ⚠️ 本机没有 docker，这个镜像从未被构建过。所以它在这里的存在只为
#    一件事：让 `profile_for_category` 有东西可返回、让镜像名可注入。
#    "镜像里到底有没有那些命令"由 `scripts/verify_sandbox_tools.py`
#    的能力契约回答（静态），**不是**由这一行回答。
IMAGE_IR = os.environ.get("CTF2_IMAGE_IR") or KernelSandboxProfile.IR.value

# CTF2 解题沙箱容器名的前缀。
#
# ★ 它不只是命名习惯 —— `sweep_orphan_sandboxes` **靠它来认领孤儿**
#   （启动时把带这个前缀的容器全删掉）。所以：
#   · 改这里也要保证别的东西不占用这个前缀；
#   · 容器名必须**永远**以它开头，否则泄漏的容器会攒下来没人清。
SANDBOX_NAME_PREFIX = "ctf2-"

# 沙箱所在的 Docker 网络。
#
# ## ★★ "独立网络"解决了什么、**没有**解决什么（2026-10-16 实测改正）
#
# 这里原来的说法是**错的**，原文是：
#
#     默认 bridge 上的容器能互相看见，而"同时跑两道题"意味着
#     A 题的样本能扫到 B 题的环境 —— 那是题目之间的越界。
#
# 实测证伪：**用户自定义 bridge 同样允许同网络容器互通**
# （`enable_icc` 默认 true），而且比默认 bridge **更宽松** ——
# 它还额外给容器做**按名字的 DNS 解析**。实测在真实的
# `suvex-ctf2` 上起两个容器，A 直接读到了 B 的响应：
#
#     A→B 按名字:  DNS 解析成功
#     A→B 按 IP 打: GOT:SECRET-FROM-B      ★ 越界
#
# 所以「换成自定义网络」本身**没有**解决它声称要解决的问题。
# 题目之间的隔离**另有一层机制**：见 `NETWORK_ICC_OPTION`。
#
# ## 自定义网络真正买到的那个东西（别删）
#
# 是**把沙箱与平台自己的容器隔开**。实测：
#
#     `suvex-db`（postgres:16）在**默认 bridge** 172.17.0.0/16 上，
#     沙箱在 `suvex-ctf2` 172.18.0.0/16 上。
#
#     从默认 bridge 上的容器连 `172.17.0.2:5432` → **CONNECTED**
#     从 `suvex-ctf2` 上的容器连同一个地址     → **REFUSED**
#
# 也就是说：沙箱里跑的是出题人给的东西，而它**够不到数据库**。
# 这一条价值是真的 —— 改动这里要连它一起考虑。
CTF2_NETWORK = "suvex-ctf2"

#: 单循环（非多智能体）时事件归属用的固定 agent 标识。
#: 与 `app/scans/importer.py` 的 `_AGENT_SELF_REF` 同值 —— 那边是兜底默认值，
#: 这里是主动写进 payload；两处一致，导入器就不必按引擎分叉。
_AGENT_SELF_REF = "agent-engine"

#: 渗透评估专用网桥（2026-10-22）。
#:
#: 与 `CTF2_NETWORK`（`suvex-ctf2`，靠 iptables 白名单收窄到 CTF 靶站）
#: 和默认 bridge（`172.17.0.0/16`，上面躺着平台数据库 `suvex-db`）**都隔离**。
#:
#: 它有默认 bridge 那套 NAT 出站规则 → 容器内 nmap/ffuf/gobuster/sqlmap
#: 能连任意授权目标与公网；但连不到 `suvex-db`（实测 `172.17.0.2:5432`
#: 返回 000），也连不到 ctf2 靶站。判据与实测见
#: `SandboxContainer.__init__` 里 `network_name` 的注释。
#:
#: 由 `ensure_pentest_network()` 惰性创建（`docker network create`），
#: 幂等。手动等价命令：`docker network create suvex-pentest`。
PENTEST_NETWORK = "suvex-pentest"

#: 关掉"同网络容器互通"的那个 Docker 网络选项。
#:
#: ## ★★ 为什么用这个，而不是"每道题一个网络"
#:
#: 发现 3 要的结果是"题目之间互不可见"。最直觉的修法是给每道题建一个
#: 独立网络，而**实测下来那条路不能走**：
#:
#: · Docker 的地址池**有硬上限**。本机（Docker 28.5.2，`daemon.json`
#:   未配 `default-address-pools`）实测连续创建第 **29** 个网络就失败：
#:
#:       Error response from daemon: all predefined address pools
#:       have been fully subnetted
#:
#:   （`bridge` 与 `suvex-ctf2` 已各占一个 /16，所以新增预算只有 **28** 个。）
#: · 而"每道题一个网络"意味着**网络会累积** —— `--rm` 只管容器、不管网络。
#:   进程被杀时（`sweep_orphan_sandboxes` 存在的**全部理由**就是这个）
#:   网络会留下来，而 28 个的预算一旦占满，**后面每一道题都建不出网络**、
#:   直接起不了沙箱。那是"用一个可用性事故换一个隔离收益"。
#: · 磁盘也紧张（Kali 实测只剩 2.8G / 97%）。
#:
#: 而 ICC 关掉之后，**同一个网络**上的容器之间**内核级**互不可达，
#: 且**不产生任何需要清理的新对象**。实测两者对照（同一镜像、
#: 同一台机器、只有一个变量）：
#:
#: | 判据 | ICC 默认 | ICC=false |
#: | --- | --- | --- |
#: | A→B 容器 IP 直连 | **读到 B 的响应** ★越界 | **BLOCKED** ✓ |
#: | 出站 8.8.8.8:53 | OPEN | **OPEN** ✓（NAT 走网关，不受影响） |
#: | 出站 ctf2.dasctf.com:443 | OPEN | **OPEN** ✓ |
#: | 到宿主网关 8000/8080 | OPEN | OPEN |
#: | 到 `suvex-db` 5432 | CLOSED | CLOSED |
#: | 名字能否解析 | 能 | 能（但连不上） |
#:
#: 机制（`iptables -S` 实测，两个网络只差这一条规则）：
#:
#:     -A DOCKER-FORWARD -i br-<icc=false 的桥> -o <同一个桥> -j DROP
#:
#: 所以它是**内核层的 DROP**，不是"我们少给了一个域名"那种表面文章。
#:
#: ## 如实记录的残余差异（ICC=false 比不上"每题一网"的地方）
#:
#: 1. **名字仍然能解析**：另一个容器的名字（存在性）是个能被探测的
#:    信号，只是连不上。容器名带 8 位随机十六进制，猜中要靠穷举。
#: 2. **发布到宿主的端口仍能从网关 hairpin 进**：实测 A 经网关连
#:    同网络邻居 `-p` 发布的端口是 **OPEN**。**当前不构成风险** ——
#:    沙箱的 `_argv()` 里**没有任何 `-p`**（见那张参数表），
#:    所以没有可 hairpin 的目标。将来若给沙箱加端口发布，这条要重看。
NETWORK_ICC_OPTION = "com.docker.network.bridge.enable_icc"

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
#:
#: ⚠️ 保留这个常量只为向后兼容（旧测试引用它）。v0.70 起真正的收尾提醒由
#: `_warn_bands()` 按**梯度**触发，见 `TURN_WARN_BANDS`。它的值（5）恰好等于
#: 旧行为"只剩 5 轮提醒一次"的起点，所以语义没有突变。
FINAL_STRETCH_TURNS = 5

#: 收尾催促的**梯度**（学 strix `core/hooks.py` 的 `_TURN_WARN_BANDS`）。
#:
#: ## 为什么从"单档"改成"三档"
#:
#: 旧实现只在"剩 5 轮"时提醒一次。实测问题（2026-10-22 一次渗透扫描被 60 分钟
#: 墙钟截断）：模型在**还有一半预算**时就开始大举铺开新方向，等触发那唯一一次
#: 提醒，它已经开了三条没跑完的线 —— 收尾提醒来得太晚、太突然，模型只能硬停。
#:
#: strix 的做法是**分三档、越接近越急**：70% 时"开始规划收尾，别再开大线"，
#: 85% 时"优先收口，只关必要的"，95% 时"立刻停止一切、马上交结论"。同样一句
#: 话分三档说，模型有足够轮次把已在手上的证据整理成结论，而不是被一刀切。
#:
#: ## 判据是"已用比例"，不是"剩几轮"
#:
#: 用比例而不是绝对轮数，是为了让 quick(40)/standard(80)/deep(150) 三种深度
#: 天花板统一适用 —— 80 轮的 70% 是 56 轮，150 轮的 70% 是 105 轮，各自相称。
TURN_WARN_BANDS: tuple[float, ...] = (0.70, 0.85, 0.95)

#: 成本梯度（学 strix 的 cost budget）。**默认关**（`None`）。
#:
#: 为什么默认关：我们是自建接入点、按 token 计费，没有可靠统一的美元定价表
#: （不同 provider 价格差异大，且会漂移）。填错价格会把"省钱"变成"提前瞎收工"，
#: 那比不设更糟。开启时由调用方给 **token 总量上限**（不是美元），避免引入定价表。
#: 判据与 `TURN_WARN_BANDS` 同一套比例语义（已用 token / 上限）。
COST_WARN_BANDS: tuple[float, ...] = (0.70, 0.85, 0.95)

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
#
# ## ★ `RemoteProtocolError` 同样必须在这张表里（2026-10-17 实测补）
#
# 症状：`Server disconnected without sending a response`。
#
# 实测（2026-10-17，`[BJDCTF 2nd]文件探测`）：模型已经跑到第 39 轮、
# 花了 1611 秒（27 分钟）在逐个探测站点的静态目录 —— 一次
# `RemoteProtocolError` 让这一切作废。日志原话：
#
#     CTF2 解题结束：turns=39 candidates=0
#     error=调用 LLM 失败（RemoteProtocolError）：Server disconnected without sending a response
#
# 它和 `ReadError` 属于同一类：连接建立了、请求也发出去了，服务端在
# 把回应写完之前把连接关了。区别只在"断在哪一步"——`ReadError` 是读
# 响应体时断，`RemoteProtocolError` 是连状态行都没读全。两者都是
# 服务端抖动/过载，**重试大概率成功**。
#
# 反例也记一下，免得以后有人把"协议错误一律重试"当教条：
# `httpx.UnsupportedProtocol`（URL 写错）、`httpx.LocalProtocolError`
# （我们自己的请求体有问题）重试多少次都一样，**不该**进来。
_LLM_RETRY_ATTEMPTS = 4
_LLM_RETRY_BACKOFF = 1.5  # 秒，线性退避：1.5s / 3.0s / 4.5s
#: 值得重试的连接层异常。**加新成员时要问一句"重试是否可能成功"** ——
#: 这个元组是唯一的判据，改它等于改"哪些失败会作废整道题"。
_LLM_RETRYABLE = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.ReadError,  # ★ 2026-10-12 补，见上面那段实测
    httpx.RemoteProtocolError,  # ★ 2026-10-17 补，见上面那段实测
)
# 喂给模型的工具输出上限。**必须有**：一次 `cat` 一个 ELF
# 会把上下文瞬间吃满，而后果是后续所有回合都在"遗忘"。
MAX_TOOL_OUTPUT_CHARS = 8000
# 对话历史里保留的最近回合数（更早的做摘要/裁剪）。
MAX_HISTORY_TURNS = 24

#: ★ v0.42：一轮回复里最多执行几个 `ACTION`。
#:
#: ## 为什么要有批量
#:
#: 实测：66 份解题过程里 **271 轮（14.2%）** 一轮写了 ≥2 个 `ACTION`
#: （极端一轮 23 个）。而旧版 `parse_action` 只解第一个、其余**静默丢弃**
#: —— 模型以为三条命令都跑了，实际只跑了一条，然后基于"三条的结果"
#: 推理下一步。于是同一件事既是**吞吐浪费**（80 轮 = 最多 80 条命令），
#: 又是**错误的认知前提**（它会以为自己已经看到过某些输出）。
#:
#: 改成批量执行是**顺着模型已经在做的事**，不是逼它换习惯。
#:
#: ## 为什么封顶 5
#:
#: 一轮里的动作是**串行**执行的（沙箱有状态，`cd` / 写文件 / 起后台进程
#: 都要求顺序），而 `run_shell` 单条最长 300 秒。不封顶的话，一句
#: "我来把这几件事都做了"可以把整道题的墙钟时间吃光。
MAX_ACTIONS_PER_TURN = 5

#: ★ v0.42：一轮批量动作喂回模型的**输出总预算**（字符）。
#:
#: 单条仍是 `MAX_TOOL_OUTPUT_CHARS`（8000），但 5 条 × 8000 = 40000 字符
#: 进上下文，比一整个 `MAX_HISTORY_TURNS` 的历史还大 —— 那就从"省轮次"
#: 变成了"撑爆上下文"，得不偿失。批量时按条数平分这个预算，
#: 见 `_per_action_budget`。
MAX_TURN_OUTPUT_CHARS = 16000

#: ★ v0.43：一次解题里最多**重建**几次沙箱容器。
#:
#: ## 为什么要有重建这件事
#:
#: 容器会因为**与解题无关的原因**消失：服务重启时 `main.lifespan` 里的
#: `sweep_orphan_sandboxes()`、人手 `docker rm`、宿主清理脚本。原来的处置
#: 是"立刻中止，本题算失败"（`sandbox.died` 那条）—— 但那时模型可能已经
#: 跑了几十轮、把题目的结构摸清了，全部作废。
#:
#: 实测代价（2026-10-17「粗心的开发人员」）：跑到 turn 42、40 分钟，下一
#: 条命令报 `No such container` 就直接放弃。而这道题的工作区里真正值钱的
#: 东西（笔记、脚本、已解出的中间结果）按系统提示的要求放在 `/work/persist`
#: —— 那是**宿主目录的读写挂载**，重建容器后还在。
#:
#: ## 为什么是 2 次而不是无限
#:
#: 重建是"外部故障"的兜底，不是常态。真出问题时（Docker 挂了、磁盘满了）
#: 无限重建会变成每轮都撞一次墙、白烧 token —— 与 v0.41 那条"容器一死就
#: 停"要防的浪费是同一种。2 次给的是"偶发一次 + 一次重试"的余量。
MAX_SANDBOX_REVIVALS = 2


# ══════════════════════════════════════════════════════════
# 镜像选择
# ══════════════════════════════════════════════════════════

# 会被判成"逆向"的分类值。**精确匹配**，见下面 `profile_for_category`。
#
# ⚠️ `pwn` **不在**这里，而且是刻意的：pwn 题的通用工具（`checksec` /
# `ROPgadget` / `cyclic` / `fmtstr_payload`）全部随 pwntools 装在 base 里，
# 它不需要逆向镜像那一大坨（`radare2` + `mono` + `angr`，1.35 GB）。
#
# 但 pwn **确实**需要 `gdb`（`gdb.attach(io)` 是调 exploit 的主要入口），
# 而 base 原本没有 —— 所以补的是 base 的 Dockerfile（加 `gdb`，几 MB），
# 而不是把 pwn 整类推到 4.18 GB 的逆向镜像去。实测见
# `docker/suvex-sandbox/Dockerfile` 里 `gdb` 那一条的注释。
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

    ## ★ v0.41 起有三个镜像，判定顺序本身有意义

    `ir-*` 判在 `_REVERSE_CATEGORIES` **之前**：两者今天没有交集，
    但 `ir-*` 是**我们自己定义**的、名字可控的集合；而
    `_REVERSE_CATEGORIES` 里混着平台可能传过来的别名（`rev` / `re` /
    `逆向`）。先判我们自己那批，意味着"将来平台恰好传了个含 `re`
    的别名"时，不会把一道 IR 题推进逆向镜像 —— 那正是上面那段
    警告的同一种事故。

    ## 认不出来时返回 base

    这是安全的方向：base 是超集里较小的那个，而**三个镜像都能跑
    非专属题**（re / ir 只是多了工具）。所以"猜错成 base"的代价是
    "某个专属工具不在"，而那是**可见的失败**（命令 not found），
    比"猜错成 re/ir"带来的一堆无用攻击面要好。
    """
    normalized = (category or "").strip().lower()
    # ★ 用前缀判，不用"在 IR_CATEGORIES 里"判：与 agentsec-* 同一条
    # 纪律（见 `vocab.IR_CATEGORY_PREFIX` 的说明）—— 前缀让"这批分类"
    # 成为一个可判定的集合，而不是一张要跟着改的名单。
    if normalized.startswith(IR_CATEGORY_PREFIX):
        return KernelSandboxProfile.IR.value
    if normalized in _REVERSE_CATEGORIES:
        return KernelSandboxProfile.REVERSE.value
    return KernelSandboxProfile.BASE.value


def image_for_profile(profile: str) -> str:
    """沙箱 profile → 镜像名。未知取值退回 base，并且**出声**。

    ⚠️ 这里的 `get(profile, BASE)` 与"静默退化"不同：schema 层已经
    挡住了非法取值，走到默认分支只可能是**数据库里有更旧/更新的枚举值**
    （迁移期间）。那时用 base 比让整道题起不来合理 —— 与
    `scans.runner._goal_clause` 的取舍完全一致。

    ★ v0.65：先 `normalize_sandbox_profile` 把历史镜像名（`ctf2-sandbox*`）
      映射成新名再判。这样迁移还没跑到的旧行、或回滚期间的旧值，也能
      起对镜像，而不是掉进"未知 → base"分支（那会让 IR 题起个没有 tshark
      的 base 镜像，模型看到的是"我没有工具"而不是"镜像选错了"）。
    """
    from app.core.vocab import normalize_sandbox_profile

    profile = normalize_sandbox_profile(profile)
    if profile == KernelSandboxProfile.REVERSE.value:
        return IMAGE_REVERSE
    if profile == KernelSandboxProfile.IR.value:
        return IMAGE_IR
    if profile != KernelSandboxProfile.BASE.value:
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
    except KernelError as exc:
        # 包成同一个领域异常，但消息要能直接喂回模型 ——
        # 它需要知道"为什么不行"才能换路子，而不是反复试同一个地址。
        raise KernelError(
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


# Docker 在**容器已经不存在**时给的固定说法（`docker exec` / `docker cp`
# 都这么报）。拼写照抄 daemon 的原话，不要"整理"成更像英文的句子 ——
# 我们靠子串匹配它，改一个字母就永远匹配不上。
#
# ⚠️ 用它的是 `SandboxContainer.exec` 的 `died` 标记。`destroy()` 里
#    也有一处同样的匹配，但**故意不复用这个常量**：那里的语义是
#    "容器已经没了，销毁算成功"，与这里有独立演化的可能（比如哪天
#    daemon 改文案，两处要分别判断要不要放宽）。复制一个字符串的
#    漂移风险，小于把两个判据耦合起来的风险。
_CONTAINER_GONE_MARKER = "No such container"


class KernelSandboxError(KernelError):
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
        return _clip_both_ends(text, limit)


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
    # 同一条纪律在别处也有：`_network_icc_disabled` 判"网络配置对不对"
    # 时按**名字**查（`docker network inspect suvex-ctf2`），
    # 而 `docker network create` 也是名字精确匹配 —— 都不经过子串匹配。
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


def _network_icc_disabled() -> bool | None:
    """`CTF2_NETWORK` 当前有没有关掉容器互通。网络不在时返回 `None`。

    ## ★ 为什么不能用 `docker network ls` 判断

    网络**存在**与网络**配置对**是两件事。原来的实现只查"在不在"，
    于是"网络在、但 `enable_icc` 是默认的 true"会被当成准备好了 ——
    而那种状态下题目之间**是互通的**（见 `NETWORK_ICC_OPTION` 的实测表）。
    只查存在性等于把这个安全属性交给"网络当初是谁建的"决定。

    判据取 `{{index .Options "…"}}`：
      · 网络存在且选项是 `false` → `False`
      · 网络存在但没有这个选项（默认 true）→ `True`
      · 网络不存在 → `None`（`inspect` 退出码 1）
    """
    result = _run(
        [
            "docker",
            "network",
            "inspect",
            CTF2_NETWORK,
            "--format",
            f'{{{{index .Options "{NETWORK_ICC_OPTION}"}}}}',
        ],
        timeout=30,
    )
    if not result.ok:
        return None
    value = result.stdout.strip()
    # 选项缺席时模板渲染成空串 —— 那正是 Docker 默认的 `true`。
    return value != "false"


def ensure_ctf2_network() -> bool:
    """保证 `CTF2_NETWORK` 存在**且题目之间互不可见**。返回是否可用。

    ## ★★ 为什么必须有这个函数（2026-10-01 实测）

    `CTF2_NETWORK` 原来**只被引用、从来没人创建**（`docker run --network`
    那两处）。而 Docker 不会替你建一个不存在的网络，它的报错是：

        failed to set up container networking: network suvex-ctf2 not found

    这条错误出现在**解题沙箱启动**那一步，用户看到的是"启动解题沙箱失败"，
    而真正的原因（一个本该由我们建的网络不存在）完全没出现在界面上。
    实测：整道题两次轮询全部倒在这一句上，`未解出`。

    ## ★★ 2026-10-16（发现 3）：光"建出来"不够，还要**关掉容器互通**

    原来这个函数的 docstring 说"独立网络 = 容器互不可见"，**那是错的**：
    用户自定义 bridge 与默认 bridge 一样允许同网络容器互通，还多一层
    按名字的 DNS。实测在真实的 `suvex-ctf2` 上，A 题容器读到了 B 题
    容器的响应体。理由与实测表见 `NETWORK_ICC_OPTION`。

    所以这里做两件事：**建的时候带上 ICC=false**，以及
    **已经存在的网络若没关 ICC，就地迁移**（否则"网络早就建好了"
    这个最常见的情况会一直是通的，而函数返回 True 像是没问题）。

    ## 迁移为什么是"删了重建"，以及它安全在哪

    Docker 的 `enable_icc` 是网络**创建时**的属性，没有"改现有网络"的
    API。所以迁移 = 删掉 + 用新选项重建。

    安全前提是**这一刻网络上没有容器**。这在本函数被调用的两个时机
    都成立：

      · `main.lifespan` 启动时 —— 紧接在 `sweep_orphan_sandboxes()`
        **之后**（见 `main.py`）。那一步已经把上一次运行遗留的
        `ctf2-*` 容器全删了，所以这一刻网络上没有我们的容器；
      · `SandboxContainer.start()` 起容器前 —— 此时**本容器还没起**。

    真出现"网络上有容器"时 `docker network rm` 会失败
    （`network … has active endpoints`），这时**不重建、只告警**：
    宁可这一轮先退回"互通"（并留下一条明确的 warning），
    也不能为了改配置去删一个还有容器的网络。

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
    icc_disabled = _network_icc_disabled()

    if icc_disabled is False:
        # 已经是对的形状 —— 最常见的情况，一次 inspect 就结束。
        return True

    if icc_disabled is True:
        # ★ 网络在，但题目之间是**互通**的 —— 迁移它。
        removed = _run(["docker", "network", "rm", CTF2_NETWORK], timeout=60)
        if not removed.ok:
            # 有容器挂着（或别的删除失败）：**不硬删**，如实告警后返回 True。
            # 返回 True 而不是 False：网络确实可用、沙箱起得来，
            # 这轮的问题是"隔离没生效"，那不是"起不来"。
            logger.warning(
                "CTF2 沙箱网络 %s 的容器互通没关掉，而它上面还有容器，"
                "本轮无法迁移 —— **同时跑的两道题之间此刻是互通的**。"
                "下一次服务重启（此时容器已被 sweep 清空）会自动修好。原因：%s",
                CTF2_NETWORK,
                removed.stderr.strip() or "未知原因",
            )
            return True
        logger.warning(
            "已删除并重建 CTF2 沙箱网络 %s：原来没关容器互通，"
            "同网络的两道题能互相扫到（发现 3）",
            CTF2_NETWORK,
        )

    created = _run(
        [
            "docker",
            "network",
            "create",
            "-o",
            f"{NETWORK_ICC_OPTION}=false",
            CTF2_NETWORK,
        ],
        timeout=60,
    )
    if created.ok:
        logger.info(
            "已创建 CTF2 沙箱网络 %s（容器互通已关：题目之间互不可见）",
            CTF2_NETWORK,
        )
        return True

    # ★ 竞态复查：`create` 失败未必是坏事 —— 可能是另一个进程刚建好。
    #   不复查的话，这里会为一件**已经成立**的事记一条 warning。
    #   ⚠️ 复查的是**配置**而不只是存在性：另一个进程刚建的那个
    #      未必是带 ICC=false 的。
    if _network_icc_disabled() is False:
        return True

    logger.warning(
        "无法创建 CTF2 沙箱网络 %s，解题沙箱将起不来：%s",
        CTF2_NETWORK,
        (created.stderr.strip() or "未知原因"),
    )
    return False


def ensure_pentest_network() -> bool:
    """确保渗透评估专用网桥 `suvex-pentest` 存在（幂等）。

    ## 与 `ensure_ctf2_network` 的关系

    两份、不共用一个网桥。理由在 `PENTEST_NETWORK` 与
    `SandboxContainer.network_name` 的注释里：CTF 靶站白名单和任意
    授权目标是两种出站策略，混在一个网桥上要么把 CTF 靶站暴露给
    渗透容器、要么把渗透目标挡在白名单外。

    ## 为什么也要 ICC=false

    同一次运行可能并发多个渗透任务，彼此不该互通（A 目标里读到的
    凭证不该流向 B 目标）。与 CTF 侧同一理由。

    ## 为什么不迁移（不像 `ensure_ctf2_network` 那样删了重建）

    `suvex-pentest` 是本轮新引入的网络，历史部署上不存在"没关 ICC 的
    旧版本"。所以这里只做"查不到就建"，不做迁移 —— 少一条删网络的
    代码路径就少一个误删在跑容器的机会。真遇到 ICC 没关（人为建的），
    后果是并发渗透任务互通，属于可接受的残余风险，记在注释里。

    失败不阻断：返回 False 只记 warning，真正失败会在起容器时暴露。
    """
    inspected = _run(
        ["docker", "network", "inspect", PENTEST_NETWORK],
        timeout=30,
    )
    if inspected.ok:
        return True

    created = _run(
        [
            "docker",
            "network",
            "create",
            "-o",
            f"{NETWORK_ICC_OPTION}=false",
            PENTEST_NETWORK,
        ],
        timeout=60,
    )
    if created.ok:
        logger.info("已创建渗透评估网桥 %s（出站 NAT 正常，与平台库/CTF 靶站隔离）", PENTEST_NETWORK)
        return True

    # 竞态复查：create 失败可能因为另一个进程刚建好。
    if _run(["docker", "network", "inspect", PENTEST_NETWORK], timeout=30).ok:
        return True

    logger.warning(
        "无法创建渗透评估网桥 %s，渗透沙箱将起不来：%s",
        PENTEST_NETWORK,
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
        raise KernelSandboxError(
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
        network_name: str | None = None,
        cpus: int | None = None,
        memory_mb: int | None = None,
        host_resources: Path | None = None,
        attachments_dir: Path | None = None,
        workspace_dir: Path | None = None,
        extra_env: dict[str, str] | None = None,
        volume_write: bool = True,
    ) -> None:
        self.name = name
        self.image = image
        self.network_isolated = network_isolated
        # ★★ 渗透评估专用网桥（2026-10-22 加）。
        #
        # ## 为什么 CTF 那套 egress 白名单不能拿来跑渗透
        #
        # CTF 解题是打**固定靶站**，所以 `suvex-ctf2` 的 iptables
        # `DOCKER-USER` 白名单收窄到几个已知靶站网段是合理的（见
        # `deploy/ctf2-egress-rules.sh`）。
        #
        # 但**渗透评估的目标是任意授权目标**。把渗透也塞进 `suvex-ctf2`，
        # 就变成"每换一个目标就要人工加一条 iptables"，而且忘加的症状是
        # 容器内 98 个工具全部超时、96% 的动作退化成宿主侧 http_request
        # 逐个猜 URL —— 这个坑真踩过（打 192.168.1.121，curl 000/5s）。
        #
        # ## 那能不能直接用默认 bridge？
        #
        # **不能。** 实测：平台数据库 `suvex-db`(postgres:16) 就在默认
        # bridge `172.17.0.0/16` 上，从默认 bridge 的容器连
        # `172.17.0.2:5432` 是 **CONNECTED**（见上文 156 行注释的实测）。
        # 渗透目标往往就是内网 IP，一旦容器在默认 bridge 上、LLM 又被
        # 指挥去"顺手扫一下同网段"，它能直接摸到平台库。
        #
        # ## 结论：专用网桥 `suvex-pentest`
        #
        # 它与 `suvex-ctf2`、默认 bridge 都隔离，默认 bridge 那套 NAT
        # 出站规则让它能连任意目标与公网，但连不到 db、连不到 ctf2 靶站。
        # 实测：目标 200 / db 000 / 公网 200。
        #
        # `network_name` 为 None 时退回老行为（CTF2_NETWORK / none），
        # 所以 ctf2 侧零改动。
        self.network_name = network_name
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
        # ★ v0.64：持久工作区挂载读写开关（设置页「允许写入挂载卷」）。
        #   True（默认，历史行为）→ 工作区以 `:rw` 挂进 `/work/persist`；
        #   False → 以 `:ro` 挂。关掉后 agent 仍可用 `/work` 这个 tmpfs
        #   干活，只是产出出不了容器、跨重启不保留。见 `_argv` 里的挂载行。
        self.volume_write = volume_write
        # ⚠️ `extra_env` **绝不允许**承载凭据。它存在的唯一用途是
        # 传一些无害的开关（比如 `PWNLIB_NOTERM=1`）。
        # 类型上没法强制这一点，所以这条规矩写在这里 + 有测试盯着
        # （`test_extra_env_must_not_carry_credentials`）。
        self.extra_env = extra_env or {}
        self._started = False
        self.container_id: str | None = None
        # ★★ 容器是否已被外部移除（v0.41）。
        #
        # ## 为什么需要这个标志：实测白烧了 39 轮
        #
        # 2026-10-16「被偷走的文件」（MISC）那次，第 24 轮的工具调用返回
        # `Error response from daemon: No such container: ctf2-DQDE6CBNWR1T-e30ff750`
        # —— 容器被 Docker 侧带走了（`--rm` 生效，服务没有重启、也没有任何
        # sweep 记录）。此后每一条命令都返回同一个错，而模型没有任何判据
        # 知道"这不是暂时的"，于是它用 `echo hello` / `echo x1` /
        # `echo probe_final` 一路探测到第 63 轮才收尾 —— 39 轮、约 62 万
        # token 换来的信息量是零。
        #
        # ⚠️ 判据**只认 `No such container` 这个精确子串**。不要顺手把
        #    `_run` 的 127 分支（"找不到可执行文件：docker"）也算进来：
        #    那是"Docker 没装/没在 PATH 上"，是宿主环境坏了，与"这个容器
        #    死了"是两件事，混在一起会让前者伪装成后者。
        #
        # ⚠️ 这里**只标记，不重建**。
        #
        #    依据分两半，性质不同，别混着引用：
        #
        #    - **代码事实**：`--tmpfs /work` 让 /work 在内存里（见 `_argv`
        #      的参数表），容器一停就消失；而 `{PERSIST_DIR}`（=
        #      `/work/persist`，见 `:222`）是宿主 `workspace_dir` 的
        #      **bind mount**（`_argv` 里那句 `-v ...:rw`），**重建后还在**。
        #      所以"重建后全丢"这个说法是错的 —— 丢的是 /work 这一层，
        #      持久区的内容会活下来。
        #    - **推测**：死因未知（`_run_worker` 的清理路径与宿主机日志都
        #      查不出是谁带走的），在不知道死因的情况下重建，很可能立刻再
        #      死一次，等于把一次白烧换成两次。**这条是推断，不是实测**。
        #
        #    真要做重建，正确的前置是先把死因查出来（那时才知道该不该重建、
        #    要不要换参数），而不是先写重建再指望它活。
        self.died = False

    def _network_arg(self) -> str:
        """决定 `docker run --network` 取什么值。

        三态，优先级从高到低：

        1. `network_isolated=True` → `"none"`（最严，本地题/运维强制隔离）；
        2. `network_name` 非空 → 用那个网桥（渗透评估传 `suvex-pentest`）；
        3. 否则 → `CTF2_NETWORK`（`suvex-ctf2`，CTF 靶站，靠 iptables 白名单收窄）。

        第 1 条排在 `network_name` 前面是**故意的**：即使渗透场景传了网桥名，
        只要上层判定要隔离（本地 repo/path 目标），就仍走 `none`。隔离永远是
        更安全的那一档，不该被"我传了个网桥名"覆盖。
        """
        if self.network_isolated:
            return "none"
        return self.network_name or CTF2_NETWORK

    def _argv(self) -> list[str]:
        """构造 `docker run` 的参数。

        ## 逐个参数的"为什么"

        | 参数 | 挡什么 |
        | --- | --- |
        | `--rm` | 容器退出即删。配合 `destroy()` 是双保险（进程崩了也不会留残骸） |
        | `--name` | 固定名字，让 `docker rm -f` 在拿不到 id 时仍然可用 |
        | `--network none` | ★ crypto/取证/逆向**完全无网络**；web 与 pwn 走 `suvex-ctf2`，边界靠 iptables 白名单 |
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
            self._network_arg(),
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
            # ★ v0.64：读写开关。`allowVolumeWrite=False` 时挂 `:ro` ——
            #   设置页那一项从"写了不生效"变成真有行为。禁用时容器内
            #   `/work/persist` 只读，agent 的写只能落在 `/work` 这个
            #   tmpfs（容器停即消失），不影响内存里的发现收集。
            _mode = "rw" if self.volume_write else "ro"
            argv += ["-v", f"{self.workspace_dir}:{PERSIST_DIR}:{_mode}"]
        for key, value in self.extra_env.items():
            argv += ["-e", f"{key}={value}"]
        argv += [self.image, "tail", "-f", "/dev/null"]
        return argv

    def start(self) -> None:
        ensure_docker_available()
        # ★ 起容器前再保证一次网络（幂等）。
        #
        # 光靠"服务启动时建一次"挡不住这件事：网络是外部状态，
        # 服务跑着的时候它可能被 `docker network prune` 或人手删掉 ——
        # 而那时解题会退回同一个看不懂的
        # `network suvex-ctf2 not found`。代价是一次查询，换掉一整类误报。
        #
        # ⚠️ 这一步**不只是查存在性**：它会连带把"网络在、
        #    但容器互通没关"就地迁移掉（见 `ensure_ctf2_network`）。
        #    迁移要求这一刻网络上没有容器 —— 而这里**本容器还没起**，
        #    所以在起容器前调用是安全的时机。**别把它挪到 `_run` 之后。**
        #
        # 只在**要联网**时查：`network_isolated` 的容器走 `--network none`，
        # 与这个网络无关，没必要为它付这次查询。
        #
        # 渗透评估走的是**另一个**网桥（`PENTEST_NETWORK`），它的准备也
        # 是"起来之前先确保存在"。按 `network_name` 分派，别混着查
        # `ensure_ctf2_network`（那个会去建 `suvex-ctf2` 靶站网络，
        # 渗透根本不用它）。
        if not self.network_isolated:
            if self.network_name:
                ensure_pentest_network()
            else:
                ensure_ctf2_network()
        result = _run(self._argv(), timeout=120)
        if not result.ok:
            raise KernelSandboxError(
                f"启动解题沙箱失败：{result.stderr.strip() or result.stdout.strip() or '未知原因'}"
            )
        self.container_id = result.stdout.strip()[:64] or None
        # ★★ `docker run` 成功**不等于**容器还活着（v0.41）。
        #
        # ## 为什么必须再验一次：实测 `docker run -d --rm` 会撒谎
        #
        # 容器**秒退**时（镜像入口一跑就结束、或起来立刻被什么带走），
        # `docker run -d` 仍然返回**退出码 0**，stdout 仍然是**完整的 64 位
        # 容器 ID**。Kali 上用 `_argv()` 的原参数、只把入口换成 `/bin/true`
        # 实测：
        #
        #     docker run 退出码 = 0
        #     stdout = bcff6db0...8fb3b56（长度 64）
        #     2 秒后 docker ps --filter name=…  →  空
        #     docker exec …  →  Error response from daemon: No such container
        #
        # 所以"`result.ok` 为真就认为起好了"是错的 —— 那只是"Docker 接受了
        # 这个请求"，不是"这个容器在跑"。
        #
        # ## 代价对比：这条为什么要加
        #
        # 真实解题过程里，四份"容器不见了"的现场有**三份是 turn 1 就死**
        # （`ctf2chal_01M516FTQ64CBZ1W44TDWPBC0M-20261016-041138.txt` 12 轮、
        # `-050638.txt` 22 轮、`ctf2chal_01M3PYYJVPR6SGQ14RPF0NEVT2-20260929-130842.txt`
        # 27 轮）。那种情况下 `self.died` 那条检测**一次都救不回来**：它要等到
        # 下一次工具调用才置位，而那时模型已经拿 12~27 轮去撞同一堵墙。
        # 一次 `docker inspect`（~50ms）换掉这一类全废。
        #
        # ⚠️ 验活**按容器 ID 做**，不按名字：名字是外部可占用的资源，
        #    9-29 那次 `Conflict. The container name "/ctf2-Q14RPF0NEVT2-1603"
        #    is already in use` 就是实例 —— 按名字查有可能查到**另一个**容器。
        self._verify_running()
        self._started = True
        # ★ v0.43：新容器 = 新的生命。
        #
        # `died` 是**上一个**容器留下的结论，重建成功后必须清掉 —— 否则
        # 解题循环下一轮开头读到 `died=True`，会立刻判定"沙箱又没了"，
        # 把刚刚重建好的这次机会白白丢掉（而且会连按 `MAX_SANDBOX_REVIVALS`
        # 次，每次都在容器还活着的情况下报"重建失败"）。
        #
        # ⚠️ 位置：放在 `_verify_running()` **之后**。验活失败会抛异常，
        #    此时重建并没有成功，`died` 必须保持 True。
        self.died = False
        logger.info(
            "CTF2 沙箱已启动 name=%s image=%s network=%s id=%s",
            self.name,
            self.image,
            "none" if self.network_isolated else CTF2_NETWORK,
            self.container_id,
        )

    def _verify_running(self) -> None:
        """`docker run` 之后确认容器**真的在跑**；不在就把现场信息带出来。

        失败时抛 `KernelSandboxError`，消息里带上 `docker inspect` 的
        `.State.Status` / `.ExitCode` / `.OOMKilled` / `.Error` —— 排查
        "容器为什么秒退"时，这四个字段就是全部线索（OOM 与入口崩掉
        在外部看起来完全一样）。
        """
        target = self.container_id or self.name
        probe = _run(
            [
                "docker",
                "inspect",
                target,
                "--format",
                "{{.State.Running}}|{{.State.Status}}|{{.State.ExitCode}}"
                "|{{.State.OOMKilled}}|{{.State.Error}}",
            ],
            timeout=20,
        )
        if not probe.ok:
            # 容器已经不存在了 —— 正是 `docker run -d --rm` 秒退的形态：
            # run 给了 ID，容器却在下一瞬间被删掉（`--rm` 生效）。
            raise KernelSandboxError(
                "启动解题沙箱失败：docker run 报告成功"
                f"（ID {self.container_id or '(无)'}），但容器随即消失，"
                "docker inspect 取不到它。"
                f"（{probe.stderr.strip() or probe.stdout.strip() or '未知原因'}）"
            )
        parts = (probe.stdout or "").strip().split("|", 4)
        while len(parts) < 5:
            parts.append("")
        running, status, exit_code, oom_killed, state_error = (p.strip() for p in parts)
        if running.lower() == "true":
            return
        raise KernelSandboxError(
            "启动解题沙箱失败：容器起来了但没在跑（可能秒退）。"
            f"name={self.name} id={self.container_id or '(无)'} "
            f"status={status or '(空)'} exit={exit_code or '(空)'} "
            f"oom={oom_killed or '(空)'} error={state_error or '(空)'}"
        )

    # ── 在容器里做事 ──────────────────────────────────────

    def exec(self, argv: list[str], *, timeout: float = 120) -> CommandResult:
        """在容器里跑一条命令（不经 shell）。

        ## ★ 顺带记下"容器没了"（v0.41）

        判据只看 stderr 里那个精确子串 —— 见 `self.died` 的说明。

        ⚠️ 覆盖范围**只有 `exec` 与 `shell` 两条路径**（`shell` 是
        `run_shell` 工具的实现，也是解题的主路径）。

        `copy_in` 的 `docker cp`（见本类的 `copy_in`）**仍直连 `_run`，绕开
        这里** —— 所以容器没了时那条 `docker cp` 的 `No such container`
        不会置 `died`。这一处**故意不补**：`copy_in` 在全仓**零生产调用点**
        （只有本文件的定义与 `_run_worker` 里一句引用它的注释），为它加一条
        检测等于给未来的读者一个"这里很重要"的假信号。真要它覆盖，主循环
        下一轮开头那次 `shell` / 工具调用会兜住。

        ## `-e TERM=dumb`：为了不往 observation 里塞两行噪音

        容器里没有 `TERM`，于是 pwntools 一 import 就让 `curses.setupterm()`
        去找 terminfo，找不到就往 **stderr** 打：

            Warning: _curses.error: setupterm: could not find terminfo database
            Terminal features will not be available.  Consider setting TERM ...

        而 `CommandResult.combined()` 把 stderr 以 `[stderr]` 附在 observation
        后面给模型看 —— 结果是一道 pwn 题里，模型每跑一次
        `python3 -c "from pwn import *"` 就多读两行与解题无关的话。
        实测（2026-10-19，重建后的 base 镜像）确认设 `TERM` 即消失。

        设 `dumb` 而不是 `xterm`：容器本来就没有终端能力，`dumb` 是 POSIX
        表达这件事的标准值，也不会诱导子进程去发 ANSI 控制序列。

        **为什么在调用侧设、不在镜像里 `ENV`**：镜像里设会连带影响
        `docker run -it` 的手动调试（`dumb` 下 vim / less 都没法用），
        而"以非交互方式调用"是**调用方**的事实，不是镜像的属性。
        """
        result = _run(
            ["docker", "exec", "-i", "-e", "TERM=dumb", self.name, *argv], timeout=timeout
        )
        if not result.ok and _CONTAINER_GONE_MARKER in (result.stderr or ""):
            if not self.died:
                logger.warning(
                    "CTF2 沙箱容器已不存在（被外部移除）name=%s —— 本轮之后会中止解题",
                    self.name,
                )
            self.died = True
        return result

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
class TokenUsage:
    """一次 LLM 调用的 token 用量（v0.36）。

    ## ★ 为什么单独一个类型，而不是把四个 int 塞进 `LlmReply`

    它要**跨模块传递**（runner → service → 数据库），而一个具名类型
    让"这四个数字是一组、要么都有要么都没有"这件事在类型上就成立。
    四个裸 int 会让调用方很容易只传其中两个。

    ## ★★ `reported` 这一位是必须的（别删）

    接入点**可能不给 `usage`**（第三方代理、某些本地模型）。那时
    token 全是 0 —— 而"真的是 0 token"与"没记录到"在界面上长得一样。

    所以 `reported=False` 明确表示"这四个 0 不代表没消耗"。
    与 `providers` 的 `hasKey`/`apiKey` 分两列是同一条纪律。

    ## 实测的字段口径（2026-10-14）

    | 字段 | 简单问答 | 推理型 | 取吗 |
    | --- | --- | --- | --- |
    | `prompt_tokens` | 37 | 75 | ✅ |
    | `completion_tokens` | 44 | 904 | ✅ |
    | `total_tokens` | 81 | 979 | ✅ |
    | `input_tokens` | 37 | 75 | ❌ 与 prompt 重复 |
    | **`output_tokens`** | **0** | **0** | ★ **恒为 0** |
    | `reasoning_tokens` | 0 | **0** | ⚠️ 实测不给 |

    ⚠️ 照 Anthropic 那套读 `input_tokens`/`output_tokens` 会
    **静默少算全部输出**（响应里两套口径同时存在，是自建网关转译的特征）。

    ⚠️⚠️ `reasoning_tokens` 那个 0 **不能当成"没有推理"的证据**：这
    个 0 只说明网关没上报这个字段，不说明模型没花推理。实测在同一份
    响应里 `message.reasoning_content` 有 6412 字符，而
    `completion_tokens_details.reasoning_tokens` 仍是 0 —— 网关把推理
    内容透传了、把计量丢了。要判"推理占了多少"，只能看
    `completion_tokens`（它**包含**推理部分），不能看这个细分字段。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    #: 接入点到底给没给 usage。False 时上面四个 0 **不代表没消耗**。
    reported: bool = False

    @property
    def billable_total(self) -> int:
        """用于聚合的总量。

        ⚠️ 优先用接入点给的 `total_tokens`；它缺失（= 0）而分项有值时
        退回 `prompt + completion`。这样"只给分项不给总量"的接入点
        也能统计对，而不是显示 0。
        """
        if self.total_tokens:
            return self.total_tokens
        return self.prompt_tokens + self.completion_tokens


def parse_usage(body: object) -> TokenUsage:
    """从 LLM 响应体里解出 token 用量（v0.36）。

    ## ★ 只认 `prompt_tokens` / `completion_tokens` / `total_tokens`

    实测（2026-10-14，Kali 上的默认 provider）发现那个网关**同时给两套口径**：

        prompt_tokens=75  completion_tokens=904  total_tokens=979
        input_tokens=75   output_tokens=0        ← ★ 恒为 0

    响应里带 `usage_semantic: "openai"` 与 `usage_source: "anthropic"`
    —— 这是自建网关转译的特征。若照 Anthropic 那套读
    `input_tokens`/`output_tokens`，会**静默少算全部输出**
    （而界面上显示 0 看起来像"没跑过"，比不显示更坏）。

    所以这里**只认 OpenAI 口径**，且**不做兼容猜测** ——
    猜错的代价是数字悄悄变错，而那比"显示未记录"糟得多。

    ## 缺失时返回 `reported=False`，而不是抛

    调用方（解题循环）不该因为"接入点没给 usage"而中断一道跑了
    三十轮的题。所以这里退化成 `TokenUsage(reported=False)`，
    由上层如实落库（见 `KernelSolveUsage.usage_reported`）。

    ## 防御性取值

    `usage` 可能是 `None`、不是 dict、或值是字符串/负数 —— 都当作
    "没给"。**不抛异常**：这是统计，不是解题的必经之路。
    """

    def _int(value: object) -> int:
        """取一个非负整数。取不到（None/非数字/负数）就当 0。"""
        if isinstance(value, bool):  # bool 是 int 的子类，必须先挡掉
            return 0
        if isinstance(value, int):
            return value if value >= 0 else 0
        if isinstance(value, float) and value >= 0:
            return int(value)
        return 0

    if not isinstance(body, dict):
        return TokenUsage()
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return TokenUsage()

    prompt = _int(usage.get("prompt_tokens"))
    completion = _int(usage.get("completion_tokens"))
    total = _int(usage.get("total_tokens"))

    # 三个都是 0 时视为"没给" —— 真实调用不可能三个都为 0
    # （哪怕空回复也至少有几个 prompt token）。
    #
    # ⚠️ 这个判据是刻意的：`reported=True` 而全是 0 会让界面显示
    #    "消耗 0"，那与"没记录"混淆 —— 正是这一列要消除的歧义。
    if prompt == 0 and completion == 0 and total == 0:
        return TokenUsage()

    # reasoning_tokens 藏在 completion_tokens_details 里。
    # ⚠️ 实测该接入点恒给 0，所以它**只备查、不参与统计**。
    details = usage.get("completion_tokens_details")
    reasoning = 0
    if isinstance(details, dict):
        reasoning = _int(details.get("reasoning_tokens"))

    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        reasoning_tokens=reasoning,
        reported=True,
    )


def _finish_reason(body: object) -> str:
    """从 LLM 响应体里解出「接入点为什么停下来」（v0.41.2）。

    ## 为什么这里就地取，而不复用 `providers/llm.py`

    那个模块是**探测用**的：它关心的是 content / reasoning 的形状（见
    `_extract_reply` 那几段实测表），入口与字段口径都不同。把解题侧的
    "是不是被截断了"塞进去，会让它多背一层语义，而那一层与它自己的
    职责无关。所以 token 用量（`parse_usage`）之后，这里再就地取一次。

    ## 两套口径都要认

    - OpenAI：`{"choices": [{"finish_reason": "length" | "stop" | ...}]}`
    - Anthropic：`{"stop_reason": "max_tokens" | "end_turn" | ...}`

    ⚠️ 两者的"被截断"值**名字不同**（`length` / `max_tokens`），只认一个
    就会漏掉另一家 —— 而这个函数存在的全部意义就是"别漏"。

    ## 取不到就返回空串

    `body` 可能是 `None`、不是 dict、`choices` 不是列表、元素不是 dict ——
    都当作"接入点没给"。**不抛**：这是旁路信息，不该让一道已经跑了
    几十轮的题因为解不出一个标记而中断。
    """

    def _get(mapping: object, key: str) -> str:
        if not isinstance(mapping, dict):
            return ""
        value = mapping.get(key)
        return value if isinstance(value, str) else ""

    if not isinstance(body, dict):
        return ""

    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            reason = _get(first, "finish_reason")
            if reason:
                return reason

    return _get(body, "stop_reason")


@dataclass(frozen=True, slots=True)
class LlmReply:
    text: str
    # 推理型模型会把产出分成 reasoning + content；两个都可能是我们
    # 需要的那段（见 providers/llm.py 里那段实测表）。
    source: str
    raw: dict[str, Any] = field(default_factory=dict)
    # ★ v0.36：这次调用的 token 用量。**默认是"未记录"**（`reported=False`），
    #   所以老的构造点（测试里手写的 `LlmReply(...)`）不会因此报错，
    #   而它们会如实落成"没记录到 usage"。
    usage: TokenUsage = field(default_factory=TokenUsage)
    # ★ v0.41.2：接入点为什么停下来 —— 只关心一件事：**输出是不是被切了**。
    #
    # OpenAI 口径给 `choices[0].finish_reason`，截断时是 `"length"`；
    # Anthropic 口径给 `stop_reason`，截断时是 `"max_tokens"`。两个都要认。
    #
    # ⚠️ 默认 `""`（未知）：老的构造点、以及"接入点没给这个字段"的响应
    #    都落到这里。**未知一律当作没被截断** —— 宁可漏报，也不要在正常
    #    回复上说"你被切断了"（那会让模型无缘无故去压缩它的推理）。
    finish_reason: str = ""


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
            raise KernelError(
                "没有可用的 LLM 接入点：provider 的 baseUrl 为空，"
                "且无法从名称推断官方默认地址。请先在设置里配置 provider。"
            )
        endpoint = resolved if "://" in resolved else f"https://{resolved}"
        self.anthropic = is_anthropic(endpoint)
        self.url = build_endpoint(endpoint, anthropic=self.anthropic, local=kind == "local")
        self.model = model
        # ★ v0.36 补：`label` 此前只用来推断地址（`resolve_base_url`），
        #   **没有存下来**。而 token 统计要按接入点聚合
        #   （"换模前后各花多少"），所以必须留住它。
        #
        #   ⚠️ 存的是**标签**（用户起的显示名，如 "gzd"），不是 id ——
        #      与 `KernelSolveUsage.provider_label` 一致。
        self.label = label
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

    def failover(self, other: FallbackLlm) -> LlmGateway:
        """换到另一个接入点，返回**新的** gateway（v0.35）。

        ⚠️ 返回新对象而不是就地改 `self`：`self` 是上一段成功历史对应的
        gateway，就地改会让"前 30 轮是谁跑的"变得不可考。解题循环持的是
        `gateway` 变量，换模就是重新赋值它 —— 语义清楚，也便于测试。

        复用 `__init__` 的地址归一化（`build_endpoint` / `is_anthropic`），
        所以"本地模型补 `/v1`""Anthropic 走 Messages API"这些既有规则
        不会在换模这条路径上分叉出第二份。
        """
        return LlmGateway(
            model=other.model,
            api_key=other.api_key,
            base_url=other.base_url,
            label=other.label,
            kind=other.kind,
            timeout=self.timeout,
            max_tokens=self.max_tokens,
        )

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
                # ★ `trust_env=False` —— 出站 SSRF 面。这个网关连着管理员填的
                #   provider `baseUrl`（`LlmGateway` 由 `build_endpoint` 推出
                #   URL），而 httpx 的 `trust_env` 默认 `True`：它会读环境里的
                #   `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` 把请求发给那个
                #   代理（实测默认配置下请求真的路由到了代理，`False` 不经过）。
                #   这里同时带 `Authorization` 凭据，所以走代理还多一层
                #   "把 LLM key 交给代理"的风险。
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self.timeout), trust_env=False
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
                raise KernelError(
                    f"调用 LLM 失败（{type(exc).__name__}）：{redact(str(exc))[:200]}"
                ) from exc
        if response is None:
            raise KernelError(
                f"调用 LLM 失败（{type(last_error).__name__}）："
                f"{redact(str(last_error))[:200]}（已重试 {_LLM_RETRY_ATTEMPTS} 次）"
            ) from last_error

        try:
            parsed = response.json()
        except ValueError:
            parsed = None

        if response.status_code >= 400:
            raise KernelError(
                f"LLM 拒绝了请求（HTTP {response.status_code}）："
                f"{redact(str(parsed))[:200] if parsed else redact(response.text)[:200]}"
            )

        text, source = _extract_reply(self.anthropic, parsed)
        # ★ v0.36：顺手把 token 用量解出来带上。**不在这里落库** ——
        #   本函数按设计不碰 DB（见 `solve_in_sandbox` 的说明），
        #   由解题循环收进 `SolveOutcome`，收尾时统一写。
        return LlmReply(
            text=text,
            source=source,
            raw=parsed if isinstance(parsed, dict) else {},
            usage=parse_usage(parsed),
            finish_reason=_finish_reason(parsed),
        )


# ══════════════════════════════════════════════════════════
# 系统提示词
# ══════════════════════════════════════════════════════════

PROMPT_PATH = PROJECT_ROOT / "prompts" / "kernel_system.md"

#: 智能体安全评测块（v0.40）。**只在开关打开且本题属于 `agentsec-*` 时追加。**
#:
#: ## ⚠️ 注意不要和 `adapters/builder.py` 的 `load_system_prompt` 搞混
#:
#: 那边有一个同名函数，但它读的是 `prompts/platform_spec.md` ——
#: **平台接入**（给人看的接入方案）用的，与解题无关。
#: 两者的"system prompt"只是撞名，不是同一份东西，不该同步。
#:
#: ## ★ 为什么是独立文件而不是拼在本模块的字符串常量里
#:
#: 与 `kernel_system.md` 同一条理由：提示词是**要改的东西**，改它的人可能
#: 不写 Python。放进 `.md` 之后，措辞调整就是一次纯文本改动，
#: 不需要碰代码、也不会误碰旁边的逻辑。
#:
#: ## ★ 为什么它是**追加**而不是另一份完整的系统提示词
#:
#: 因为 `kernel_system.md` 里的纪律（不越界、不爆破、不编造、输出协议、
#: 回合预算）**对智能体安全题一样适用** —— 那四类题更容易越界，不是更容易。
#: 换成另一份提示词等于把那批纪律丢掉，而丢掉的恰好是最该在的时候。
#:
#: 所以注入方式是：原提示词**一个字不动**，块追加在**末尾**。
PROMPT_AGENTSEC_PATH = PROJECT_ROOT / "prompts" / "kernel_agentsec.md"

#: 应急响应 / 根因复盘块（v0.41）。**只在开关打开且本题属于 `ir-*` 时追加。**
#:
#: 注入方式与 `PROMPT_AGENTSEC_PATH` **完全同构**（追加不替换、关着不读、
#: 关闭时逐字节相同），理由见上面那段说明 —— 那三条不是 agentsec 专属的
#: 取舍，是"往系统提示词里加东西"这件事本身的三条纪律。
#:
#: ## ★ 它和 agentsec 块可能**同时**出现在一道题里吗
#:
#: 不会。`is_agentsec_category` 与 `is_ir_category` 判的是两个互斥的
#: 前缀（`agentsec-` / `ir-`），而 `category` 只有一个值。真出现了
#: 两个前缀都匹配的取值，那意味着有人在分类名里拼了两段 ——
#: 校验层（`validate_note_category`）会先拒掉，轮不到这里。
PROMPT_IR_PATH = PROJECT_ROOT / "prompts" / "kernel_ir.md"

#: 渗透评估块（v0.47）。**只在开关打开且本题属于 `pentest-*` 时追加。**
#:
#: 注入方式与 `PROMPT_AGENTSEC_PATH` / `PROMPT_IR_PATH` **完全同构**
#: （追加不替换、关着不读、关闭时逐字节相同）。
#:
#: ## ★ 它与前两个块的区别：判据不同，输出字段也不同
#:
#: `kernel_agentsec.md` 判"某个考察点成不成立"，`kernel_ir.md` 判"入侵时间线"，
#: 而这一份判"我亲眼验证过哪些安全发现"。三者共用 `confirmed` /
#: `ruled_out` / `needs_follow_up` 三档纪律，但**交付物形状不同**：
#: 前两个走 `candidates`（flag 形态），这一份走 `findings`（结构化发现）。
#: 所以 `SolveOutcome` 上多了一个 `findings` 字段（原样来自 `FINAL.findings`）。
PROMPT_PENTEST_PATH = PROJECT_ROOT / "prompts" / "kernel_pentest.md"

#: 越权与注入手法块（v0.42）。**解题与渗透扫描共用同一份**，
#: 所以路径常量不在这里定义 —— 见 `app/core/prompts.py` 的说明
#: （那份 docstring 记的正是"为什么这一个不做成两个模块各自的常量"）。
#:
#: ## 与上面三个块的区别：它**不按分类**判
#:
#: `kernel_agentsec.md` / `kernel_ir.md` 是"某一类题该按另一套纪律做"，
#: 所以要先判分类。这一份是"同一件事做得更全"—— 打 Web 的手法是
#: 打 Web 的手法，与这道题是 `web` 还是 `misc` 无关。所以它的门槛
#: 只有开关本身，**外加一条排除**：`agentsec-*` 与 `ir-*` 那七类不吃它
#: （见 `build_system_prompt` 里那段说明）。


def load_system_prompt() -> str:
    """读 solver 的系统提示词。

    ⚠️ 文件不在时**抛错**，不用内置兜底。

    兜底听起来友好，但它会让"提示词文件漏进部署包"这件事
    **完全无声**：系统照常跑，只是 agent 少了那份纪律（不越界、
    不爆破、不编造），而产出的质量下降没有任何信号。
    抛错会让部署当场发现。
    """
    try:
        raw = PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise KernelError(
            f"读不到解题提示词 {PROMPT_PATH}。它必须随部署一起分发 —— "
            "缺了它 agent 的行为纪律（不越界、不爆破、不编造）就没了约束。"
        ) from exc
    # 把技能包清单注入占位符。清单只列 name + 一句话描述（全文靠 load_skill
    # 按需拉）。技能目录缺失时清单为空串 —— 这时提示词里那段 `load_skill`
    # 说明仍保留（工具照样可用，只是无内容可拉），不会因缺目录而崩。
    if "{{SKILL_INDEX}}" in raw:
        index = skills_mod.render_index() or "（本次部署没有附带技能包。）"
        raw = raw.replace("{{SKILL_INDEX}}", index)
    return raw


def load_agentsec_prompt() -> str:
    """读智能体安全评测块（v0.40）。

    ⚠️ 与 `load_system_prompt` 同一条纪律：**文件不在就抛错**。

    这个文件只在开关打开且本题属于 `agentsec-*` 时才被读。所以
    "缺文件"这件事不会在普通比赛里暴露 —— 它会在**第一次真的跑智能体
    安全题**时暴露。而那已经是最坏的时机：用户明明打开了开关、
    期待追加那段评测纪律，结果拿到的是原提示词 —— 模型会照攻击模式
    去"拿 flag"，而那四类题里没有 flag。

    若不抛错而是回退成空串，失效完全无声（题目照跑、结论全错）。
    所以抛错。
    """
    try:
        return PROMPT_AGENTSEC_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise KernelError(
            f"读不到智能体安全评测提示词 {PROMPT_AGENTSEC_PATH}。"
            "它必须随部署一起分发 —— 缺了它这四类任务会按攻击模式跑，"
            "而它们要的是「判定考察点是否成立」。"
        ) from exc


def load_ir_prompt() -> str:
    """读应急响应块（v0.41）。

    ⚠️ 与 `load_system_prompt` 同一条纪律：**文件不在就抛错**。

    这个文件只在开关打开且本题属于 `ir-*` 时才被读。所以"缺文件"
    不会在普通比赛里暴露 —— 它会在**第一次真的跑应急响应任务**时暴露，
    而那已经是最坏的时机：用户以为在做取证复盘，模型拿到的却是
    "怎么攻破系统"的攻击纪律，于是它会去扫端口、去爆破，而证据
    摆在那儿一条没看。

    若不抛错而是回退成空串，失效完全无声（任务照跑、结论全错）。
    """
    try:
        return PROMPT_IR_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise KernelError(
            f"读不到应急响应提示词 {PROMPT_IR_PATH}。"
            "它必须随部署一起分发 —— 缺了它这类任务会按攻击模式跑，"
            "而它们要的是「复盘过去发生了什么」。"
        ) from exc


def load_pentest_prompt() -> str:
    """读渗透评估块（v0.47）。

    ⚠️ 与 `load_system_prompt` / `load_ir_prompt` 同一条纪律：**文件不在就抛错**。

    这个文件只在开关打开且本题属于 `pentest-*` 时才被读。回退成空串会让
    模型拿到的是"解题"纪律（判 flag、给 candidates），于是在一次真实客户
    评估里它会去追一个不存在的 flag，而不是去核实一个真实存在的漏洞 ——
    而结论看起来还"跑通了"。

    另一个理由与 IR 块相同：它要求模型**只读优先**、**写操作要有理由**。
    缺了它，模型会按"打穿副本"的默认策略行动，而这里的目标是客户的
    生产系统。
    """
    try:
        return PROMPT_PENTEST_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise KernelError(
            f"读不到渗透评估提示词 {PROMPT_PENTEST_PATH}。"
            "它必须随部署一起分发 —— 缺了它这类任务会按 CTF 解题模式跑，"
            "而它们要的是「核实真实目标上有哪些问题」。"
        ) from exc


#: ★ v0.58：多智能体编排的 root / child 提示词。
#:
#: ## 为什么是两份而不是一份
#:
#: root 的职责是**调度**（拆解目标、派子 agent、聚合报告），而子 agent 的职责
#: 是**动手**（在分到的范围里深挖）。把两份合成一份等于让每个 agent 都
#: "既能调度又能干活"，而实测那种形态下 root 会自己跑去敲命令、忘了它还有
#: 子 agent 在等它派活 —— 这正是 strix `root_agent_directive` 反复强调
#: "root 绝不动手"的理由。
#:
#: ⚠️ 它们**不是**替换 `kernel_system.md`，而是在其**之上**再叠一层角色说明
#:    （与 `build_system_prompt` 的追加纪律一致）。
PROMPT_ROOT_PATH = PROJECT_ROOT / "prompts" / "kernel_root.md"
PROMPT_CHILD_PATH = PROJECT_ROOT / "prompts" / "kernel_child.md"


def load_root_prompt() -> str:
    """读多智能体 root（调度者）提示词（v0.58）。**文件不在就抛错。**"""
    try:
        return PROMPT_ROOT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise KernelError(
            f"读不到多智能体 root 提示词 {PROMPT_ROOT_PATH}。"
            "它必须随部署一起分发 —— 缺了它 root 会退化成"
            "「自己动手的单 agent」，多智能体编排形同虚设。"
        ) from exc


def load_child_prompt() -> str:
    """读多智能体 child（执行者）提示词（v0.58）。**文件不在就抛错。**"""
    try:
        return PROMPT_CHILD_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise KernelError(
            f"读不到多智能体 child 提示词 {PROMPT_CHILD_PATH}。"
            "它必须随部署一起分发 —— 缺了它子 agent 会去抢别人"
            "的活、或重复已经覆盖的范围。"
        ) from exc


def build_multi_agent_prompts(spec: SolveSpec) -> tuple[str, str]:
    """拼出多智能体的 `(root_system_prompt, child_system_prompt)`（v0.58）。

    两份都是「本题基础提示词（含场景块）+ 角色说明」，只差最后那一层角色。

    ⚠️ 基础部分走 `build_system_prompt_by_spec(spec)` —— 与单循环**同一份**，
       这样场景纪律（IR 的只读不变量、渗透的只读优先、三级证据）在两种模式
       下逐字节相同，不会因为走多智能体就漏掉。
    """
    base = build_system_prompt_by_spec(spec)
    root = base + "\n" + load_root_prompt()
    child = base + "\n" + load_child_prompt()
    return root, child


def build_system_prompt(
    category: str,
    *,
    agentsec_enabled: bool,
    ir_enabled: bool = False,
    offensive_enabled: bool = False,
    pentest_enabled: bool = False,
) -> str:
    """拼出本题要用的 system prompt（v0.40 / v0.41 / v0.42）。

    ## 规则只有两条

    1. 本题**不属于**任何已启用场景包，或对应开关**没开** → 返回原
       提示词，一个字节都不改；
    2. 否则 → 原提示词 + 空行 + 场景块，**追加在末尾**。

    ## ★ 为什么是追加，不是替换

    见 `PROMPT_AGENTSEC_PATH` 的说明：`kernel_system.md` 里的纪律对这几类题
    **一样适用**（它们更容易越界，不是更容易）。替换等于在最需要那批
    纪律的时候把它们丢掉。

    ## ★ 为什么开关没开时**连读都不读**那个文件

    两个理由：
      · 关着的部署里那个文件可能根本不存在（不该因此让普通解题报错）；
      · "关着 = 完全没有影响"要能在行为上成立，而不只是看上去成立。
        读取本身不改变输出，但"关着也去读"意味着有一天有人把读取挪到
        别的分支上就会静默改变行为 —— 不读，这个可能性就不存在。

    ## ★ v0.41 / v0.42：为什么新开关都是**带默认值**的参数

    不带默认值会让现有每一个调用点都必须改 —— 而它们的语义是
    "我不关心那个场景包"，改成显式传 `False` 只是把同一个
    事实抄一遍，抄漏一处就是静默失效。带默认值 `False` 之后，
    **本函数在加参数之前的所有调用行为逐字节不变**。

    ## ★★ v0.42：为什么手法块**不按分类**判，而是按分类**排除**

    另外两个块是"某一类题该按另一套纪律做"，所以要先判分类
    （`agentsec-*` / `ir-*`）。手法块不是那个意思：它说的是
    "同一件事做得更全"——打 Web 的手法是打 Web 的手法，
    与这道题叫 `web` 还是叫 `misc` 无关。所以它**对五个既有分类
    一律生效**（这是它唯一有用的形态），只排除那两个场景包：

      · `agentsec-*` —— 那四类题要的是"判定某个考察点是否成立"，
        而手法块整篇在教"怎么把系统打穿"。两段放在一起是
        **互相矛盾的两套指令**，模型会左右摇摆，而那正是
        `_agentsec_enabled` 的 docstring 里记过的那类后果。
      · `ir-*` —— 那三类题第一节就写"你的工作不是攻破系统""除 `/work`
        外一律只读"。给一份越权手法矩阵进去，最坏的情况是它真的
        照着打了一遍被复盘的那台机器。

    ⚠️ 排除是**按前缀**而不是白名单：将来平台加新分类时，
    它应当默认就享受到更全的手法，而不是因为没人记得同步而静默少一截。
    """
    base = load_system_prompt()
    # ⚠️ 三个开关都关时**提前返回**，连 notes 都不 import —— 见上面
    # 第二条理由。这也是"关闭时逐字节相同"这条验收标准的实现方式。
    if not agentsec_enabled and not ir_enabled and not offensive_enabled and not pentest_enabled:
        return base
    # 延迟 import：`notes` 在模块顶层 import 本模块（要 `FLAG_PATTERN`），
    # 顶层反向 import 会撞环。这与本文件其它地方是同一条纪律。
    from app.kernel import notes as notes_mod

    is_scenario_task = (
        notes_mod.is_agentsec_category(category)
        or notes_mod.is_ir_category(category)
        or notes_mod.is_pentest_category(category)
    )

    blocks: list[str] = []
    if agentsec_enabled and notes_mod.is_agentsec_category(category):
        blocks.append(load_agentsec_prompt())
    if ir_enabled and notes_mod.is_ir_category(category):
        blocks.append(load_ir_prompt())
    if pentest_enabled and notes_mod.is_pentest_category(category):
        blocks.append(load_pentest_prompt())
    # ★ v0.42：手法块对五个既有分类一律生效，只排除两个场景包（见 docstring）。
    if offensive_enabled and not is_scenario_task:
        blocks.append(load_offensive_playbook())
    if not blocks:
        return base
    # 空行分隔：块自己以 `## ` 起头，紧贴上一行会让 Markdown 标题
    # 并进上一个小节。原提示词**不做任何 strip** —— 它的结尾空白也是
    # 原样的一部分，"关闭时逐字节相同"这条验收标准要靠这个保证。
    return base + "".join(f"\n{block}" for block in blocks)


def build_system_prompt_by_spec(spec: SolveSpec) -> str:
    """按 `SolveSpec` 上那三个预计算开关拼 system prompt（v0.40 / .41 / .42）。

    为什么单独一个函数而不是在调用点写 `build_system_prompt(spec.category,
    agentsec_enabled=..., ir_enabled=..., offensive_enabled=...)`：那样调用点
    就得同时知道"分类"和三个开关该从哪里取，而 `spec.agentsec` / `spec.ir` /
    `spec.offensive` **已经把每个都合成一个布尔值了**。在这里再拆回去，
    等于把"合成"和"拆分"都留在代码里，将来改判据要改两处。

    参数化成 `SolveSpec` 之后，判据只有**一个**来源。
    """
    return build_system_prompt(
        spec.category,
        agentsec_enabled=spec.agentsec,
        ir_enabled=spec.ir,
        offensive_enabled=spec.offensive,
        pentest_enabled=spec.pentest,
    )


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


class AnswerFormatError(KernelError):
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
    `KernelSubmissionStatus` 的说明）。它**可能已经到达平台**。
    把它排除在外的话，模型重新提出同一个 flag 时会再发一次 ——
    如果第一次其实成功了，第二次就是对同一个 flag 的重复提交，
    而很多平台把重复提交计为错误。宁可漏掉一次重试机会，
    也不要冒这个险。

    ⚠️ `rejected` **不算**（它根本没发出去），所以不在这里的列表里。
    """
    rows = db.execute(
        select(KernelSubmission.id)
        .where(
            KernelSubmission.challenge_id == challenge_id,
            KernelSubmission.flag == normalize_flag(flag),
            KernelSubmission.status.in_(
                (
                    KernelSubmissionStatus.CORRECT.value,
                    KernelSubmissionStatus.WRONG.value,
                    KernelSubmissionStatus.PENDING.value,
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


@dataclass(frozen=True, slots=True)
class FallbackLlm:
    """一个**备用** LLM 接入点（v0.35）。

    与 `SolveSpec` 的 `llm_*` 是同一组字段，只是它排在主接入点**之后**：
    主接入点调不动时按顺序试。凭据在这一层同样是**明文**，所以纪律
    与 `SolveSpec` 完全一致 —— 不 `repr`、不落库、不进日志。
    """

    model: str
    api_key: str = field(repr=False)
    base_url: str = ""
    label: str = ""
    kind: str = "cloud"


# ══════════════════════════════════════════════════════════
# HTTP 请求历史（学 strix 的 proxy/Caido，但只做**记录 + 重放**）
# ══════════════════════════════════════════════════════════

#: 最多保留多少条历史。超出后丢最旧的（FIFO）。
#:
#: 定 200 的理由：一次渗透/应急响应的 http_request 次数通常在几十到一百多；
#: 200 条足够覆盖"刚才那个请求我想再看一眼 / 再打一次"这个真实需求，
#: 又不会把一次长扫描的所有请求都囤在内存里（每条带响应体，最多几十 KB）。
_MAX_HTTP_HISTORY = 200


@dataclass(slots=True)
class HttpRecord:
    """一次 http_request 的记录。id 是稳定短标识（`req-N`），供模型回引。"""

    id: str
    method: str
    url: str
    request_headers: dict[str, str]
    request_body: str
    status_code: int | None
    response_snippet: str
    error: str | None = None


@dataclass(slots=True)
class HttpHistory:
    """本次解题内所有 `http_request` 的**共享**记录（多智能体同用一份）。

    ## 为什么要有它（学 strix 的 proxy/Caido）

    strix 挂一个 MITM 代理，把每个请求记进 sitemap，agent 可以
    `list_requests` / `view_request` / `repeat_request`。我们没有 MITM 代理
    （那要额外一套基础设施，且我们的请求走宿主侧 `http_request`、
    容器内请求本就拦不住），但**那个能力的实用价值**是两条：
      ① "我刚打过的请求是什么" → 不用凭记忆；
      ② "把它改一个参数再打一次" → 不用从零拼一条。

    所以这里不做代理，只做**记录 + 重放**：每发一次 `http_request` 就追加
    一条 `HttpRecord`，再给模型 `list_requests` / `get_request` /
    `repeat_request` 三个工具。

    ## 为什么是可变对象而不是 spec 上的 tuple

    它是随跑随长的，而 `SolveSpec` 是不可变入参。把它放成 spec 上的一个
    可变容器（`default_factory=HttpHistory`），多智能体各子 agent 拿到的是
    **同一份**（spec 在编排里被共享），于是 A 打的请求 B 也看得到 ——
    这正是"共享 sitemap"的语义。单循环路径同理，天然就一份。
    """

    records: list[HttpRecord] = field(default_factory=list)
    _seq: int = 0

    def next_id(self) -> str:
        self._seq += 1
        return f"req-{self._seq}"

    def add(self, record: HttpRecord) -> None:
        self.records.append(record)
        if len(self.records) > _MAX_HTTP_HISTORY:
            # 丢最旧的，保持 FIFO。⚠️ 不要改 seq 计数器，否则新 id 会与
            # 被丢掉的旧 id 撞车（req-5 出现两次，模型回引必乱）。
            overflow = len(self.records) - _MAX_HTTP_HISTORY
            del self.records[:overflow]

    def get(self, record_id: str) -> HttpRecord | None:
        for rec in self.records:
            if rec.id == record_id:
                return rec
        return None

    def render_list(self, *, limit: int = 50) -> str:
        """给模型看的紧凑清单（只列 id/method/url/status，不带响应体）。"""
        if not self.records:
            return "还没有发过任何 http_request。"
        shown = self.records[-limit:]
        lines = [f"共 {len(self.records)} 条请求（显示最近 {len(shown)} 条）："]
        for rec in shown:
            status = rec.status_code if rec.status_code is not None else "ERR"
            lines.append(f"- {rec.id}  {rec.method} {rec.url}  → {status}")
        lines.append(
            "要看某条的完整请求与响应：get_request(record_id)。"
            "要改参数重放：repeat_request(record_id, ...)。"
        )
        return "\n".join(lines)


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
    # ★ v0.35：备用接入点（主接入点调不动时按顺序试）。**空 = 不启用**。
    #
    # ## 为什么是"按顺序试"而不是"轮询/负载均衡"
    #
    # 主接入点是用户明确选定的（默认 provider），它的优先级最高。
    # 备用只在主接入点**失败**时兜底 —— 不是"分摊流量"。
    # 把它做成轮询会让"用哪个模型解题"变得不确定，而复盘时
    # 看到的模型可能不是当时真正用的那个。
    fallbacks: tuple[FallbackLlm, ...] = ()
    # ── 策略 ──
    mode: str = KernelSubmitMode.SAFE.value
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

    #: ★ v0.73：**取消时也不丢发现**的活收集器（默认 `None` = 不收集）。
    #:
    #: ## 它解决的那个具体事故
    #:
    #: 一次 60 分钟的渗透扫描跑到 500 轮上限被墙钟超时取消，期间真找到了
    #: 6 条已确认漏洞（子 agent 全写进了沙箱里的 `FINDINGS_NOTES.md`，
    #: 事件流里 337 条 tool_call 也在），而入库结果是 **findings=0**。
    #:
    #: 根因不在"没找到"，而在收尾形状：模型只在写 `FINAL: {...}` 时才会
    #: `outcome.findings = final.get("findings")`，而一个跑满 500 轮的多
    #: 智能体编排**几乎不可能**收到 FINAL —— root 一直在等子 agent 回报。
    #: 超时取消后 `_timeout_outcome()` 只能交回一个空 outcome，于是所有
    #: 已经确认的发现随协程一起消失。P0 那次 findings=0 是同一个机制。
    #:
    #: ## 为什么是"传进来的容器"而不是"引擎自己留着"
    #:
    #: `outcome` 是 `solve_in_sandbox` 的**局部变量**，`task.cancel()` 之后
    #: 它就没了。而 `SolveSpec` 是调用方持有的，取消后仍然在 —— 所以把
    #: 收集器挂在 spec 上，`_timeout_outcome()` 才拿得到。
    #:
    #: ## 刻意只收"已经结构化"的东西
    #:
    #: 只在两处登记（`multiagent` 子 agent 的 `FINAL.findings`、单循环/root
    #: 的 `FINAL.findings`），也就是**模型自己明确声明为发现的结构化对象**。
    #: 绝不从自然语言推理、笔记正文、命令输出里"猜"发现 —— 那是编造数据。
    #: 收集器的语义就是"这些是模型已经交出来的，别因为进程被砍就丢了"。
    finding_sink: list[object] | None = None

    #: ★ v0.75：结束条件外置的门槛（学 BreachWeave 的"结束条件外置"）。
    #:
    #: root 写 `FINAL:` 时由编排层按**可数的**覆盖率与结论条数判定能不能
    #: 收口，而不是听模型自己说"我扫完了"（见 `app/kernel/termination.py`）。
    #:
    #: `None` = 用模块默认值（`MIN_COVERAGE = 5`）。显式给一个数可以放宽/收紧。
    #: 传 `0` = **关闭这道门**，行为退回 v0.74 —— 只有测试需要这么干：
    #: 那些用一个写死脚本 gateway 跑 3 步的端到端测试里没有任何 coverage，
    #: 门会把它们的 `FINAL` 全退回，测的就不再是它们本来要测的东西。
    #:
    #: ⚠️ 生产路径（`scans/agent_runner.build_solve_spec_from_scan`）**不传**，
    #: 所以门恒开。要关它得在代码里显式写 `min_coverage=0`。
    min_coverage: int | None = None

    #: ★ v0.79：项目黑板的存取器（学 CyberStrikeAI 的 `project_facts`）。
    #:
    #: ## 为什么是"存取器对象"而不是 session / 仓储函数
    #:
    #: kernel 是**纯编排层**，它不该 import SQLAlchemy —— 那样
    #: `tests/kernel/` 里那些不碰数据库的用例就跑不起来了。所以这里收一个
    #: 鸭子类型对象，只要两个方法：
    #:
    #:   · `record(**fields) -> {fact_key, created, version}`
    #:   · `get(fact_key) -> dict | None`
    #:
    #: 生产实现是 `app/facts/store.py::DbFactStore`，由
    #: `scans/agent_runner.build_solve_spec_from_scan` 注入。
    #:
    #: ## `None`（默认）= 本次扫描没有黑板
    #:
    #: CTF 解题路径与纯逻辑测试都是 `None`。这时 `record_fact` /
    #: `get_scan_fact` 两个工具**仍然存在**（否则提示词里提到的工具名会变成
    #: "不存在的工具"，模型白跑一轮 —— 那是 v0.72 `mcp_servers` 事故的形状），
    #: 但调用会返回一句明确的"本次未启用黑板"，让模型改用 `add_fact`。
    #:
    #: ## 为什么不让两个记账工具合并
    #:
    #: `add_fact`（内存态）是 v0.75 就有、已被实测验证过的（v0.77 那轮出了
    #: 5 条真发现）。`record_fact`（落库）依赖部署侧一次迁移。合并会让
    #: "迁移没跑"变成"记账功能整体失效" —— 分开之后黑板挂了不影响原有记账。
    fact_store: Any | None = None

    #: ★ v0.79：项目黑板存取器（学 CyberStrikeAI 的 `project_facts`）。
    #:
    #: `None`（CTF 解题路径、纯逻辑测试）= **没有黑板**。此时
    #: `record_fact` / `get_scan_fact` 两个工具仍然**存在于工具表里**
    #: （否则提示词提到的工具名会变成"不存在的工具"，模型白跑一轮 ——
    #: 这正是 v0.72 `mcp_servers` 那类事故的形状），调用时返回一句
    #: "本次扫描未启用"，不抛。
    #:
    #: 生产路径由 `scans/agent_runner.build_solve_spec_from_scan` 注入
    #: `app.facts.store.DbFactStore`。kernel 层因此**不需要 import
    #: SQLAlchemy**，与 `finding_sink` 是同一条分层纪律。
    fact_store: Any | None = None

    # ★ v0.70：token 预算上限（学 strix 的 cost budget，但用 token 不用美元）。
    #
    # `None`（默认）= 不设成本维度，只按回合梯度催收尾。
    # 非空时主循环每轮算 `spent / max_total_tokens`，跨过 COST_WARN_BANDS
    # （70/85/95%）也催收尾 —— 用于"轮数还很宽裕但钱已经花很多"的场景
    # （例如换个贵模型、或工具输出异常肥大把 prompt token 顶上去了）。
    #
    # 为什么是 token 不是美元：我们没有可靠统一的定价表（各 provider 价差大、
    # 会漂移），填错价格会把"省钱"变成"提前瞎收工"，比不设更糟。
    max_total_tokens: int | None = None

    # ★ v0.36：续解时，持久区里**已经有什么**（宿主侧列出来的清单）。
    #
    # ## 为什么不能只让模型自己去 `ls`
    #
    # 原来的做法是在提示词里写一句"⚠️ 先 `ls -la /work/persist` 看上次留了什么"。
    # 实测（2026-10-12，`[FlareOn3]GUI` A 组）：模型**不一定照做** ——
    # 它可能直接开始解题，把几十轮的产物晾在持久区里没用上。
    #
    # 而"上次留了什么"是**我们本来就知道的事实**（文件就在宿主上）。
    # 让模型花一轮去 `ls` 才能知道，是把一次已知的信息变成一次探索 ——
    # 而那一轮可能正是它缺的。所以由服务端列好、直接写进提示词。
    #
    # ⚠️ 只列**文件名与大小**，不读内容：内容可能很大，而且里面可能有
    #    上一次的中间结果（模型自己会去读需要的那个）。
    persist_listing: tuple[str, ...] = ()

    # ★ v0.28：用户补充的线索（"考点在反调试"、"libc 是 2.27"）。
    #
    # ## 为什么它必须进来（2026-10-11 修）
    #
    # `POST /solve` 的 schema 一直有这个字段（`schemas.KernelSolveIn.brief`），
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

    #: ★ 这次解题**拿到了几张**同类题方法卡（v0.45，生产遥测）。
    #:
    #: ## 为什么要有这个数（而不是只看 `notes_text` 空不空）
    #:
    #: "注入了卡片"与"注入了 3 张卡片"是两件事，而后者才是
    #: "方法卡到底有没有帮上忙"这个问题的分母。只看文本非空的话，
    #: 一次注入 1 张和一次注入 3 张在统计里长得一模一样。
    #:
    #: ⚠️ 它由 `build_spec` 从 `_recall_notes` 的返回值填进来 ——
    #:    召回发生在那里（有 session），这里只是携带。默认 0 表示
    #:    "没有召回"（分类为空、库里没有同类题、或召回失败）。
    notes_count: int = 0

    #: ★ v0.41：攻防知识互查的结果（**已渲染成段落**，由调用方传入）。
    #:
    #: 与 `notes_text` 的区别在于"知识的方向"：`notes_text` 是**本次任务
    #: 同类题**的方法卡（横向，同题型别人怎么解），这一段是**同一个资产**
    #: 的历史发现（纵向，这个资产被我们自己打过什么）。
    #:
    #: ## 为什么是已渲染的字符串而不是结构化的 list
    #:
    #: 渲染逻辑（`history.render_history_block`）里带着一段必须原样
    #: 出现的"这不是结论"声明。把结构传进来再在这里渲染，等于把那段
    #: 声明的摆放位置交给两个地方决定 —— 而它一旦被放到模型看不见的
    #: 位置（比如段首而不是段尾），"历史不得自动判 confirmed"这条约束
    #: 就退化成一句建议。
    #:
    #: ## ★ 空串与"查不到"是两种情况
    #:
    #: 查不到时 `history` 模块返回的**不是空串**，而是写着「本次没有历史
    #: 记录可参考」的一段。空串只表示"这道题不是 IR 题、压根没查"。
    #: 两者在模型眼里必须可区分 —— 见 `history` 模块 docstring 约束 2。
    #:
    #: ⚠️ 默认空串保证 v0.41 之前构造 `SolveSpec` 的地方行为逐字节不变。
    history_text: str = ""

    #: ★ v0.41：召回到几条历史发现（0 表示"查了但没命中"或"没查"）。
    #:
    #: 与 `notes_count` 同一条纪律：只靠 `history_text` 非空分不清
    #: "一条都没查到"与"没查"，而这两者在遥测里是不同的分母。
    history_count: int = 0

    #: 本题要不要追加智能体安全评测块（v0.40）。
    #:
    #: ⚠️ 它在 `build_spec` 里**一次算好**（那里有 session 能读配置），
    #:    而不是在跑解题的时候现读。理由与 `notes_text` 相同：解题循环
    #:    是 async 且**不碰数据库**（见 `solve_in_sandbox` 的说明）。
    #:
    #: ★ 算的是**两者之与**：开关开着 **且** 本题分类属于 `agentsec-*`。
    #:   拆成两个字段会让"什么时候该追加"这条判断散到两个地方，
    #:   而那正是最容易出现"开关开了但没生效"的地方。
    agentsec: bool = False

    #: 本题要不要追加应急响应块（v0.41）。
    #:
    #: 与 `agentsec` **完全同构**：在 `build_spec` 里一次算好（那里有
    #: session 能读配置），算的是"开关开着 **且** 本题分类属于 `ir-*`"。
    #:
    #: ## 为什么是两个独立字段而不是一个 `scenario: str | None`
    #:
    #: 因为两者**可能同时关着也可能只有一个开着**，而"哪个场景包生效"
    #: 不是互斥的运行时状态 —— 它是由分类名决定的。压成一个字段会让
    #: `build_system_prompt` 里出现"先看 scenario 是不是 ir、不是的话
    #: 再看 agentsec"，而那两个判断本来就已经各自在 `is_*_category`
    #: 里了，等于抄第二遍。
    #:
    #: 默认 `False` 保证 v0.41 之前构造 `SolveSpec` 的地方行为不变。
    ir: bool = False

    #: 本题要不要追加渗透评估块（v0.47）。
    #:
    #: 与 `ir` **完全同构**：在构造 spec 时一次算好，算的是"开关开着
    #: **且** 本题分类属于 `pentest-*`"。不在这里重复判断的理由与上面
    #: 两个字段相同 —— 判据只留一个来源。
    #:
    #: 默认 `False` 保证 v0.47 之前构造 `SolveSpec` 的地方行为不变。
    pentest: bool = False

    #: ★ v0.58：要不要走**多智能体编排**（root 调度 + 专职子 agent 并行）。
    #:
    #: ## 为什么是 spec 上的开关，而不是一个全局配置
    #:
    #: "这一道题值不值得多智能体"是逐题的判断：一道简单的 CTF 单点题开多智能体
    #: 是纯浪费（一个 root 加一个 child 才勉强够），而一次真实渗透/应急响应任务
    #: 天然要并行侦察。做成全局开关会让"简单题也背上一整套编排开销"，也会让
    #: "这道题到底走没走多智能体"变得不可考。
    #:
    #: ## 默认 `False`
    #:
    #: 保证 v0.58 之前构造 `SolveSpec` 的所有路径行为**逐字节不变**——
    #: 走的是原来那条单循环（`solve_in_sandbox` 里 `if spec.multi_agent` 早退）。
    multi_agent: bool = False

    #: 本题要不要追加越权与注入手法块（v0.42）。
    #:
    #: ## ★ 与上面两个**不同构**：它在 `build_spec` 里**只读开关，不判分类**
    #:
    #: 上面两个算的是"开关开着 **且** 分类属于那一族"—— 因为
    #: `kernel_agentsec.md` / `kernel_ir.md` 只对特定分类成立。
    #:
    #: 而手法块对**五个既有分类一律生效**（它的内容与题目叫什么无关），
    #: 只排除那两个场景包。那条排除判断需要 `category` 与两个开关三者
    #: 一起看，放在 `build_system_prompt` 里做（那里三者都在手上），
    #: 这里存的是**纯粹的"用户打开了这个开关吗"**。
    #:
    #: 所以这个字段的语义是"开关值"，不是"本题该不该注入"。听上去
    #: 它与上面两个不一致，但把它也预计算成"该不该"会让
    #: `build_system_prompt` 里那段排除逻辑**只能**读 spec，
    #: 而它就再也无法被单独测试了（得先造一个能算出 exclusion 的 spec）。
    offensive: bool = False

    #: ★ v0.71：本次解题的 HTTP 请求历史（学 strix proxy/Caido 的记录+重放）。
    #:
    #: 用 `default_factory` 造一份**可变**容器，多智能体各子 agent 共享同一
    #: 对象（spec 在编排里被共享）→ A 打的请求 B 也看得到。单循环路径本就一份。
    #: 详见 `HttpHistory` 的 docstring。
    http_history: HttpHistory = field(default_factory=HttpHistory)

    #: ★ v0.72：本次解题可用的 MCP server 配置（学 strix 的 list_mcps 三件套）。
    #:
    #: 这是一个**配置元组**（来自 `KernelConfig.mcp_servers`），不是活连接 ——
    #: 活连接由 `mcp_registry` 按调用临时起（见 `app/kernel/mcp.py`）。
    #: 默认为空元组：没配 MCP 时 `list_mcps` 会说"没有可用的连接"，
    #: 其余工具与提示词逐字节不变（与场景包开关同一条纪律：默认关）。
    mcp_servers: tuple[Any, ...] = ()
    #: ★ v0.72：惰性建的 MCP 注册表（缓存工具清单、串起三条 MCP 工具）。
    #:
    #: 不用 `field(default_factory=...)` 主动建：多数解题没配 MCP，提前建一个
    #: 空注册表是浪费；而 `registry_from_spec` 会在第一次用到时建。
    #: 用 `Any` 标注是为了避免 runner↔mcp 的强耦合 import；运行时它就是
    #: `app.kernel.mcp.McpRegistry | None`。
    mcp_registry: Any = None

    #: ★ v0.82：本次运行**被关掉**的内置工具名（来自
    #: `KernelConfig.disabled_tools`，经 `kernel.tools.normalize_disabled`
    #: 规整 —— 未知名字已被剔除，所以这里全是 `TOOL_NAMES` 里真实存在的）。
    #:
    #: ## 为什么存"关掉的"而不是"可用的"
    #:
    #: 与配置列的取舍同一条（见 `settings/models.py::KernelConfig.disabled_tools`）：
    #: 空元组 = 一件都没关 = 引入这个开关之前的行为，逐字节不变。
    #: 存"可用的"会让本字段在默认情况下必须被填满全部工具名，
    #: 而那等于把 `TOOL_NAMES` 抄了第二份进每个 spec。
    #:
    #: ## 它与 `mcp_servers` 的关系
    #:
    #: 两者**独立**：关掉某个 MCP 相关工具不要求删掉那条连接，反之亦然 ——
    #: 一个是"我想不想用"，一个是"存不存在"。
    disabled_tools: tuple[str, ...] = ()

    def summary(self) -> str:
        """可以安全打进日志的摘要 —— **不含凭据**。"""
        return (
            f"challenge={self.challenge_id} profile={self.profile} "
            f"model={self.llm_model} key={'有' if self.llm_api_key else '无'} "
            f"mode={self.mode} urls={len(self.access_urls)}"
        )


def _sink_findings(spec: object, findings: object) -> None:
    """把模型已经交出来的一批发现登记到 `spec.finding_sink`（若已开启）。

    ## 只做一件事：追加，不去重、不校验、不解释

    去重交给 `multiagent._merge_outcomes`（它按标题去重）与
    `findings.importer`（它按 `(scan_id, source_ref)` 幂等 upsert）。
    这里若也去重，三处策略迟早漂移 —— 而症状是"某条发现神秘消失"。

    ## 为什么不抛

    它跑在引擎主循环里（`FINAL` 解析成功那一刻）。登记失败绝不该让一次
    已经跑了几十轮的扫描崩掉；`sink` 是观测性附件，不是正确性的一部分。
    与 `notify()` 同一条纪律。

    ## ⚠️ 为什么必须判类型

    `FINAL.findings` 是**模型给的原始值**（见 `SolveOutcome.findings` 的
    说明），可能是字符串、对象、含 null 的数组。`extend` 一个字符串会把它
    拆成单个字符 —— 那会造出一堆垃圾记录。所以只接受 list。
    """
    sink = getattr(spec, "finding_sink", None)
    if sink is None or not isinstance(findings, list):
        return
    try:
        sink.extend(findings)
    except Exception:  # noqa: BLE001 - 见上面「为什么不抛」
        logger.warning("登记发现到收集器失败（不影响本次运行）", exc_info=True)


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

    #: ★ v0.42：这一场里**每一次工具调用的完整输出**（按调用顺序，未截断）。
    #:
    #: ## 为什么不能复用 `transcript`
    #:
    #: `transcript` 里的 result 是 `_clip(…, 2000)` 之后的 —— 拿来给人看够了，
    #: 拿来**判据**不够：flag 正好在 2000 字符之后就被切掉了，于是"答案确实
    #: 来自工具输出"这件事会被判成"没出现过"，白白 hold 掉一个真候选。
    #:
    #: ## 为什么值得留
    #:
    #: 见 `decide_submission` 的 `tool_outputs` 参数。auto 模式原来只要求
    #: "跑过工具 + evidence 非空"，而 evidence 是模型自己写的 —— 它完全可以
    #: 编一个 flag、再编一段"我看到了它"的说明。把**原始输出**留下来，
    #: 就能做一层便宜且几乎不误伤的校验：声称的答案必须在某次真实输出里
    #: 出现过（归一化后子串匹配，见 `answer_is_grounded`）。
    #:
    #: ⚠️ 内存：上限由轮次决定（`spec.max_turns` × 每条 `MAX_TOOL_OUTPUT_CHARS`
    #:    的量级）。80 轮 × 8000 字符 ≈ 640KB，一次性、随 `SolveOutcome` 一起
    #:    释放 —— 这个代价换掉"凭编造消耗不可再生额度"是划算的。
    tool_outputs: list[str] = field(default_factory=list)

    #: ★ v0.36：每一次 LLM 调用的 token 用量（按轮次顺序）。
    #:
    #: ## 为什么是列表而不是累计值
    #:
    #: 需求要的是"细分到每一次任务的消耗"。累计值答不了：
    #:   · 哪一轮最贵；
    #:   · 是不是在兜圈子烧钱；
    #:   · 换模前后各花多少（v0.35 的 fallback 会让同一道题用两个模型）。
    #:
    #: 每项形如 `(turn, model, provider_label, TokenUsage)` ——
    #: 带上 model/label 是因为**换模后同一道题的调用可能来自不同模型**，
    #: 而那正是"换模值不值"这个问题的答案所在。
    usages: list[tuple[int, str, str, TokenUsage]] = field(default_factory=list)

    #: 这次解题**一共成功调用了几次 LLM**。
    #:
    #: ⚠️ 数的是**成功返回的调用**（抛异常的那些没有 usage 可记）。
    #:    所以它与 `len(usages)` **恒等**；两者与"真实 token 数"的区别
    #:    只在于接入点给没给 usage —— 那由 `TokenUsage.reported` 表达。
    #:
    #: 为什么仍单独留一个计数而不直接用 `len(usages)`：
    #: 调用次数是**免费且永远准确**的指标（我们从循环里数得出来），
    #: 而 token 数依赖外部。分开记让"跑了多少轮"这件事不受
    #: "接入点给不给 usage"影响。
    llm_calls: int = 0

    def total_tokens_spent(self) -> int:
        """到目前为止累计消耗的 token 数（v0.70，成本维度催促用）。

        累加 `usages` 里每次调用 `TokenUsage.billable_total`。接入点没给
        usage 的那些调用贡献 0 —— 所以这个数**只会低估、不会高估**：低估的
        后果是"该催时没催"，比"没花钱却说花超了、逼模型提前收工"要好。

        每次调用 O(n) 重算而不是维护一个增量计数器：`usages` 每条才几十字节，
        一题跑 80 轮也就 80 条，每轮算一次是免费的量级；而增量计数器一旦
        在某条记账路径上漏加（例如换模、失败重试），就会静默偏低且无人发现。
        拿这几十次加法换"永远等于 usages 之和"的确定性，值得。
        """
        return sum(usage.billable_total for _, _, _, usage in self.usages)

    #: ★ v0.45：这次解题**一共执行了几次工具调用**（生产遥测）。
    #:
    #: ## 为什么不能用 `len(tool_outputs)` 代替
    #:
    #: `tool_outputs` 存的是**完整输出原文**，用来做答案锚定
    #: （`answer_is_grounded`）。拿它数个数当然算得出来，但那意味着
    #: "想知道跑了几次工具"要先留住全部输出 —— 而这是一个**纯计数**，
    #: 不该为了它多背一份内存，也不该在将来有人给 `tool_outputs` 加
    #: 采样/截断策略时让它变成错的数。
    #:
    #: ## 为什么它比回合数更能说明问题
    #:
    #: 回合数由对话轮次决定，而一回合可以下发多条命令
    #: （`MAX_ACTIONS_PER_TURN`）。所以"模型有没有在兜圈子"这件事，
    #: 回合数只看得出一半 —— 另一半藏在"每回合跑了几条"里。
    #: 实测依据见 README「跨题方法卡」那一节：同一题的回合数可以是 4，
    #: 工具调用数却相差一倍以上。
    #:
    #: ⚠️ 数的是**真正执行了**的调用（`dispatch_tool` 返回之后自增），
    #:    不包含"模型写了但被 `MAX_ACTIONS_PER_TURN` 丢掉"的那些。
    tool_calls: int = 0

    #: ★ v0.45：这次解题**注入了几张**同类题方法卡（生产遥测）。
    #:
    #: 与 `spec.notes_count` 同源，收尾时拷过来 —— 遥测落库发生在
    #: `_record_outcome`，而那里只拿得到 `outcome`，拿不到 `spec`。
    notes_count: int = 0

    #: ★ v0.47：这次解题**开关有没有开着**（生产遥测的原料）。
    #:
    #: 与 `spec.offensive` 同源，收尾时拷过来 —— 理由与 `notes_count` 完全
    #: 相同（遥测落库在 `_record_outcome`，那里只拿得到 `outcome`）。
    #:
    #: ⚠️ 它的语义与 `spec.offensive` 一样是"**开关值**"，不是"块真的进去了"：
    #:    七类场景题按前缀排除（见 `build_system_prompt`），而那条判断需要
    #:    分类。真正的"带没带"由 `service.record_solve_telemetry` 落库前
    #:    再按分类排除一次 —— 那一列记的才是会被拿去切分统计的事实。
    offensive: bool = False

    # ── ★ v0.38：`FINAL` 结论文档里的三块分析 ─────────────────
    #
    # ## 为什么必须把它们带出来（这是"题解简陋"的真正根因）
    #
    # `parse_final` 一直能解出完整的结论文档，但收尾时**只取了
    # `candidates`**（`extract_candidates_with_reasons`），
    # `summary` / `tried` / `ruled_out` 解析完就丢掉了 ——
    # 于是模型写的那份最有价值的分析**从来没进过题解**。
    #
    # 实测（2026-10-15，`[ACTF2020 新生赛]Upload`）：题解 5873 字符，
    # 而模型在 FINAL 里写了「用响应长度做旁路：baseline=8628；
    # 上传 .php→8532…」这样的推理，在题解里 grep「响应长度」「旁路」
    # 「黑名单」**全部 0 次命中**。那不是排版问题，是**内容被丢弃**。
    #
    # ## 三个字段的语义（照 FINAL 协议，见 `_final_stretch_note`）
    #
    # · `summary` —— 人话综述：这道题是什么、做了什么、结论如何；
    # · `tried` —— **试过的方向**逐条（含"为什么这么试"）；
    # · `ruled_out` —— **已排除的可能**逐条（附反例，不是"感觉不行"）。
    #
    # ⚠️ 它们是**自由文本列表**，形状由模型决定，所以这里只做
    #    "取出来 + 过滤掉非字符串项"，不做结构化解析 ——
    #    猜错结构会把整块内容丢掉，那正是这次要修的毛病。
    summary: str = ""
    tried: list[str] = field(default_factory=list)
    ruled_out: list[str] = field(default_factory=list)

    #: ★ v0.41：`FINAL.workorders` 的**原始值**（应急响应任务的处置建议）。
    #:
    #: ## 为什么存原始值而不是解析好的 `WorkOrder` 列表
    #:
    #: `runner` 是执行层，`workorder` 是展示层 —— 让执行层 import 展示层
    #: 会把"解析容错"与"运行循环"绑在一起，而那正是本文件一直在避免的
    #: 耦合（见 `_initial_prompt` 为什么是纯函数）。
    #:
    #: 所以这里原样带出去，解析发生在 `writeup.build_writeup` 里 ——
    #: 那里本来就是"把 outcome 变成给人看的文档"的那一层。
    #:
    #: ## 为什么类型是 `object` 而不是 `list[dict]`
    #:
    #: 因为模型给什么都可能（字符串、对象、含 null 的数组）。
    #: 声明成 `list[dict]` 会让读者以为它已经过了校验，而它没有 ——
    #: 校验在 `workorder.parse_workorders` 里。
    #:
    #: ⚠️ 非 IR 任务它恒为 `None`（提示词里没让它们写这个字段）。
    workorders: object | None = None

    #: ★ v0.47：`FINAL.findings` 的**原始值**（渗透评估任务的安全发现）。
    #:
    #: ## 为什么与 `candidates` 并存而不是复用
    #:
    #: `candidates` 是"flag 形态"：`extract_candidates_with_reasons` 会按
    #: flag 的形态去校验、去重、判 grounding，而一条安全发现（标题 + 严重度 +
    #: 证据 + 位置）**不是** flag，硬塞进去会被那道校验拦掉。
    #:
    #: ## 为什么存原始值
    #:
    #: 与 `workorders` 同一条理由：`runner` 是执行层，校验与转换在收尾
    #: （`scans/agent_runner` 把它们翻成 `vulnerabilities.json`）。
    #: 声明成 `list[dict]` 会让读者以为它已校验，而它没有 —— 模型给什么
    #: 都可能（字符串、对象、含 null 的数组）。
    #:
    #: ⚠️ 非渗透任务它恒为 `None`（提示词里没让它们写这个字段）。
    findings: object | None = None


def needs_raw_network(category: str | None) -> bool:
    """这道题是否需要**容器内**的原始网络。

    pwn / 网络类题要直连 TCP 服务，所以不能把容器关进 `none`。
    crypto / 取证 / 逆向全在本地文件上，一律用最严的隔离。

    ## ★ 2026-10-18：web 也放开，理由是**目录探测做不了**

    原来 web 走 `--network none` + 宿主侧 `http_request`。那个设计对
    "读一个页面的内容"够用，对"扫出一个没人告诉你的路径"则是死路：
    `http_request` 一次只发一个 URL，模型只能手工猜路径名。实测一道
    需要爆破隐藏入口文件的题跑了 **76 轮、手工猜了 40 个路径**全部 404，
    最后模型自己写的复盘第一句就是「沙箱无外网…无法用 shell 侧对靶站做
    字典爆破，只能靠 http_request 逐个 URL 请求」。

    而镜像里本来就装着 `gobuster` / `ffuf` / `nmap` / `nikto` / `sqlmap`
    和 `/usr/share/dirb/wordlists/`（`common.txt` 4600 词、`big.txt` 2 万词），
    只因为没网，一次都用不上 —— 工具齐全却用不了，那是配置问题，
    不是模型能力问题。

    ## 放开之后，边界靠什么收窄

    不靠这个函数，靠 **iptables 的 `DOCKER-USER` 白名单**：只放行
    `suvex-ctf2` 网段到 dasctf 靶站网段（`198.18.0.0/15`）的流量，
    其余一律 DROP。所以容器即使被 LLM 指挥去扫内网或出公网，包也出不去。
    规则见交接包 `05-网络与沙箱边界.md`。

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
        "web",  # ★ 2026-10-18：目录探测要靠容器内的 gobuster/ffuf
    }


# ══════════════════════════════════════════════════════════
# 四个工具
# ══════════════════════════════════════════════════════════

TOOL_NAMES = (
    "read_attachment",
    "http_request",
    "run_shell",
    "load_skill",
    "list_requests",
    "get_request",
    "repeat_request",
    "list_mcps",
    "describe_mcp",
    "call_mcp",
)
#: 技能包按需加载（学 strix 的 load_skill）。它**不碰沙箱、不碰网络** ——
#: 只是把一篇随代码分发的 markdown 读出来给模型看，是纯查表操作，
#: 所以它比另外三个工具都安全（没有命令执行、没有出网）。
#: 单独放这个元组是因为 dispatch_tool 要知道哪些工具不走沙箱/网络分支。


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


async def _do_http(
    *,
    method: str,
    url: str,
    headers: dict[str, str] | None,
    body: Any,
    http: httpx.AsyncClient,
) -> tuple[int | None, str, str | None]:
    """真正发一次请求，返回 `(status_code, response_text, error)`。

    ★ v0.71：把"发请求"从"记录到历史"里抽出来，好让
    `http_request`（首打）与 `repeat_request`（重放）走同一条发送逻辑，
    只在**记录**这一点上不同（重放不再产生第二条记录，而是更新原记录的状态）。
    """
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
        return None, "", f"请求失败（{type(exc).__name__}）：{exc}"

    text = b"".join(chunks).decode("utf-8", errors="replace")
    if truncated:
        # ⚠️ 这段提示**不能教模型去容器里 curl**：沙箱默认是
        #    `--network none`（`_argv` 里那个最重要的安全参数），容器内
        #    出不去网（实测 transcript 里 `curl` 直接 `exit=6`，Could not
        #    resolve host）。原来的文案正是这么写的，等于给了一条走不通的路
        #    —— 模型照着试、失败、再回来，白烧轮次。
        #    给的办法必须是**这个工具自己能做到的**：换更精确的 URL/参数，
        #    或用 `Range` 头分次取。
        text = (
            f"⚠️ 响应体超过 {_READ_CAP // 1024} KB，只读了前 {_READ_CAP // 1024} KB —— "
            "要看后面的内容，用 http_request 换更精确的 URL 或参数，"
            "或者带 Range 头分次取（如 Range: bytes=-4000）\n\n"
        ) + text
    # ★ 头尾双段：这条路径送的是**工具原始输出**，关键信息经常在末尾
    #   （见 `_clip_both_ends` 的说明）。
    return status_code, text, None


async def _tool_http_request(
    args: dict[str, Any], *, spec: SolveSpec, http: httpx.AsyncClient
) -> str:
    """发一次 HTTP 请求（**宿主侧**，所以白名单拦得住）。

    见模块 docstring 第 3 条：容器内的请求拦不住，所以这个能力
    必须由宿主提供。

    ★ v0.71：发完把这条请求记进 `spec.http_history`（学 strix proxy/Caido），
    返回的回执里带上 `req-N` 号，模型可据此 `get_request` / `repeat_request`。
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

    status_code, text, error = await _do_http(
        method=method, url=url, headers=headers, body=body, http=http
    )

    # 记进历史（成功与失败都记 —— 失败的请求也是"我试过什么"的证据）。
    record_id = spec.http_history.next_id()
    spec.http_history.add(
        HttpRecord(
            id=record_id,
            method=method,
            url=url,
            request_headers={str(k): str(v) for k, v in (headers or {}).items()},
            request_body=str(body)[:2000] if body else "",
            status_code=status_code,
            response_snippet=text[:2000],
            error=error,
        )
    )

    if error is not None:
        return f"{error}（已记入历史：{record_id}）"

    head = f"HTTP {status_code}\n"
    return (
        f"{head}{_clip_both_ends(text, MAX_TOOL_OUTPUT_CHARS)}"
        f"\n\n（已记入请求历史：{record_id}。可 get_request / repeat_request 回引。）"
    )


async def _tool_list_requests(args: dict[str, Any], *, spec: SolveSpec) -> str:
    """列出本次解题发过的 HTTP 请求（学 strix list_requests）。"""
    limit = int(args.get("limit") or 50)
    return spec.http_history.render_list(limit=max(1, min(limit, _MAX_HTTP_HISTORY)))


async def _tool_get_request(args: dict[str, Any], *, spec: SolveSpec) -> str:
    """看某条请求的完整内容（学 strix view_request）。"""
    record_id = str(args.get("record_id") or "").strip()
    if not record_id:
        return "参数错误：需要 `record_id`（形如 req-3，可用 list_requests 查）。"
    rec = spec.http_history.get(record_id)
    if rec is None:
        return f"找不到请求 `{record_id}`。先用 list_requests 看看有哪些。"
    lines = [
        f"{rec.id}  {rec.method} {rec.url}",
        f"状态：{rec.status_code if rec.status_code is not None else 'ERR'}",
    ]
    if rec.request_headers:
        lines.append("请求头：")
        lines.extend(f"  {k}: {v}" for k, v in rec.request_headers.items())
    if rec.request_body:
        lines.append(f"请求体：\n{rec.request_body}")
    if rec.error:
        lines.append(f"错误：{rec.error}")
    lines.append(f"响应（前 2000 字）：\n{rec.response_snippet}")
    return "\n".join(lines)


async def _tool_repeat_request(
    args: dict[str, Any], *, spec: SolveSpec, http: httpx.AsyncClient
) -> str:
    """重放某条请求，可改 method/url/headers/body（学 strix repeat_request）。

    ★ 重放**不再产生新记录**，而是更新原记录的状态与响应 —— 否则
    "重放三次"会让历史里冒出三条几乎一样的条目，淹掉真正的探索轨迹。
    """
    record_id = str(args.get("record_id") or "").strip()
    if not record_id:
        return "参数错误：需要 `record_id`（形如 req-3，可用 list_requests 查）。"
    rec = spec.http_history.get(record_id)
    if rec is None:
        return f"找不到请求 `{record_id}`。先用 list_requests 看看有哪些。"

    method = str(args.get("method") or rec.method).upper()
    if method not in ("GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"):
        return f"不支持的 HTTP 方法：{method}"
    url = str(args.get("url") or rec.url).strip()
    if "://" not in url:
        url = "http://" + url
    # ★ 重放同样过白名单 —— 换个 url 不能绕过。
    guard_tool_url(url, access_urls=spec.access_urls)
    headers = args.get("headers") if isinstance(args.get("headers"), dict) else rec.request_headers
    body = args.get("body") if args.get("body") is not None else (rec.request_body or None)

    status_code, text, error = await _do_http(
        method=method, url=url, headers=headers, body=body, http=http
    )
    # 更新原记录（不新增）。
    rec.method = method
    rec.url = url
    rec.request_headers = {str(k): str(v) for k, v in (headers or {}).items()}
    rec.request_body = str(body)[:2000] if body else ""
    rec.status_code = status_code
    rec.response_snippet = text[:2000]
    rec.error = error

    if error is not None:
        return f"重放失败：{error}（已更新记录 {record_id}）"
    return (
        f"已重放 {record_id}：{method} {url} → HTTP {status_code}\n"
        f"{_clip_both_ends(text, MAX_TOOL_OUTPUT_CHARS)}"
    )


async def _tool_list_mcps(args: dict[str, Any], *, spec: SolveSpec) -> str:  # noqa: ARG001
    """列出本次解题可用的 MCP 连接（学 strix list_mcps）。"""
    reg = mcp_mod.registry_from_spec(spec)
    if not reg:
        return "本次解题没有配置任何 MCP 连接。"
    import json as _json

    return _json.dumps({"connections": reg.summaries()}, ensure_ascii=False, indent=2)


async def _tool_describe_mcp(args: dict[str, Any], *, spec: SolveSpec) -> str:
    """看某个 MCP 连接提供哪些工具（学 strix describe_mcp）。"""
    connection = str(args.get("connection") or "").strip()
    if not connection:
        return "参数错误：需要 `connection`（用 list_mcps 查可用的连接名）。"
    reg = mcp_mod.registry_from_spec(spec)
    if not reg:
        return "本次解题没有配置任何 MCP 连接。"
    return await reg.describe(connection)


async def _tool_call_mcp(args: dict[str, Any], *, spec: SolveSpec) -> str:
    """调一个 MCP 工具（学 strix call_mcp）。"""
    connection = str(args.get("connection") or "").strip()
    tool = str(args.get("tool") or "").strip()
    if not connection or not tool:
        return "参数错误：需要 `connection` 和 `tool`（先用 list_mcps / describe_mcp 查）。"
    reg = mcp_mod.registry_from_spec(spec)
    if not reg:
        return "本次解题没有配置任何 MCP 连接。"
    raw_args = args.get("arguments")
    # 与 strix 一致：arguments 可能是对象，也可能被序列化成 JSON 字符串。
    if isinstance(raw_args, str):
        stripped = raw_args.strip()
        if not stripped:
            arguments: dict[str, Any] = {}
        else:
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return (
                    f"参数错误：`arguments` 应该是 JSON 对象，或省略。"
                    f"可先用 describe_mcp 看连接 `{connection}` 的输入 schema。"
                )
            if not isinstance(parsed, dict):
                return "参数错误：`arguments` 必须是 JSON 对象。"
            arguments = parsed
    elif isinstance(raw_args, dict):
        arguments = raw_args
    elif raw_args is None:
        arguments = {}
    else:
        return "参数错误：`arguments` 必须是 JSON 对象，或省略。"
    return await reg.call(connection, tool, arguments)


async def _tool_run_shell(args: dict[str, Any], *, sandbox: SandboxContainer) -> str:
    """在沙箱容器里执行命令。**这是解题的主力工具。**

    ## ★ v0.87：补齐「工作目录」与「输出截断的明示」

    对照 CyberStrikeAI 的 `exec` 工具配方后发现我们缺两样**高频实用**的能力：

      1. **工作目录**：解一道题常常在同一个目录里连做几步（解压 → 进去 →
         跑脚本）。没有 `cwd` 时模型只能每步都写 `cd /work/x && ...`，
         容易因为漏 `&&` 而在错误目录执行 —— 那种错误**不报错**，
         只会让后续判断全错。
      2. **截断要说清**：输出被截断时，模型看到的是一段"看起来完整"的文本。
         它据此下的结论可能是错的（比如要找的东西正好在截断点之后）。
         所以截断必须**在返回里明说**，而不是静默丢。

    ## ⚠️ 但执行位置不跟它

    它的 `exec` 是宿主机 `sh -c`；我们是**独立容器**
    （`--network none` / `--read-only` / `--cap-drop ALL` / `--pids-limit`）。
    那是本项目最硬的差异化，不因为"对方有这个工具"而放弃 ——
    能加的是**参数能力**，不是**执行位置**。
    """
    command = str(args.get("command") or "").strip()
    if not command:
        return "参数错误：需要 `command`。"
    timeout = int(args.get("timeout_seconds") or DEFAULT_SHELL_TIMEOUT)
    # 夹在合理区间里：一个 `timeout_seconds: 100000` 会让这道题
    # 永远不返回，而调度器只能看到"还在跑"。
    timeout = max(5, min(timeout, DEFAULT_SHELL_TIMEOUT))

    # ★ `cwd` 只允许 `/work` 之下。
    #
    # 为什么限制：容器里只有 `/work` 是可写 tmpfs，其余是只读根。
    # 让模型把 cwd 指到 `/etc` 不是安全漏洞（那是只读的），而是会
    # **让它误以为自己在工作区**——随后写文件会失败，而报错是
    # "Read-only file system"，看起来像权限问题。直接挡住并说清
    # "只能给 /work 之下"，比让它撞一次墙更省回合。
    raw_cwd = str(args.get("cwd") or "").strip()
    if raw_cwd:
        if not raw_cwd.startswith("/work"):
            return (
                f"参数错误：`cwd` 只能是 `/work` 之下的路径（收到 {raw_cwd!r}）。\n"
                "容器里只有 `/work` 可写，其余文件系统是只读的。"
            )
        # 用 `cd ... &&` 前缀而不是给执行器加参数：容器执行是
        # `sh -c <command>`，前缀最简单且不依赖执行器支持 cwd。
        command = f"cd {shlex.quote(raw_cwd)} && {command}"

    result = sandbox.shell(command, timeout=timeout)
    if result.exit_code == 124:
        return f"命令超时（{timeout} 秒）已被容器内 timeout 终止。\n{result.combined()}"

    out = result.combined()
    # ★ 截断必须明示：`MAX_TOOL_OUTPUT_CHARS` 是工具输出上限，超了会被切。
    #
    # ★★ 这里曾经是 `out[:MAX_TOOL_OUTPUT_CHARS]` —— **只留头**。
    #    那是 v0.41 已经修过一次的缺陷的复发形态（见 `_clip_both_ends`
    #    的文档：实测某个上传题的响应体长 8530~8640 字符，关键提示在
    #    末尾，只留头让模型 78 轮都没看到它）。
    #
    #    `run_shell` 是**最容易踩到这条**的工具（`cat` 一个大文件、
    #    `curl` 一个长页面、`ls -R` 一棵树），而它偏偏是唯一还在用
    #    "只留头"的那条路径。证据池收的就是这里的返回值 —— 所以尾部
    #    被切掉时，连"事后还能补救"的机会都没有：flag 在 2000 字符之后
    #    这件事就彻底记录不到了。
    #
    #    改成 `_clip_both_ends` 后，返回值结构变成"头 + 中间省略说明 +
    #    尾"，模型能同时看到开头和结论，而总长仍受 `limit` 约束。
    if len(out) > MAX_TOOL_OUTPUT_CHARS:
        return (
            _clip_both_ends(out, MAX_TOOL_OUTPUT_CHARS)
            + f"\n\n⚠️ 输出被截断：原长 {len(out)} 字符，上限 {MAX_TOOL_OUTPUT_CHARS}。"
            "\n**你看到的是头尾两段，中间被省略了。** 如果刚找的东西没出现，"
            "它可能在中间 —— 用 `head`/`grep`/`sed -n 'X,Yp'` 缩小范围再看，"
            "不要因为没看到就断定不存在。"
        )
    return out


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

    ## ★ v0.82：先挡「被关掉的工具」

    这一道判断必须在**所有分支之前**。理由与那三条 MCP 前置检查同源：
    只在界面上把开关画成"关"，而 `dispatch_tool` 照样执行，就是又一个
    "配了却静默失效" —— 只不过这次失效的方向更糟：**用户以为自己关掉了
    它**（比如关掉 `run_shell` 以求稳），而模型调起来一切照常。

    ⚠️ 拦下时的措辞要给出**下一步**，不能只说"已禁用"：模型会拿着这句话
    继续走，它需要知道"这个能力不存在了，换别的路"而不是"再试一次"。
    """
    disabled = set(getattr(spec, "disabled_tools", ()) or ())
    if name in disabled:
        enabled = ", ".join(
            n for n in TOOL_NAMES if n not in disabled
        ) or "（一个都没有）"
        return (
            f"`{name}` 已被本次运行的部署配置**关闭**，不是你没用对。\n"
            f"别再试它。当前可用工具：{enabled}。"
        )
    try:
        if name == "read_attachment":
            return await _tool_read_attachment(args, spec=spec)
        if name == "http_request":
            return await _tool_http_request(args, spec=spec, http=http)
        if name == "run_shell":
            return await _tool_run_shell(args, sandbox=sandbox)
        if name == "load_skill":
            # 纯查表：读一篇随代码分发的 markdown。不碰沙箱、不碰网络。
            # 名字取自参数 `name`（与 strix 的 load_skill 同名参数，方便
            # 模型迁移既有习惯）。拼错时 load_skill_text 会回可用清单。
            return skills_mod.load_skill_text(str(args.get("name") or ""))
        if name == "list_requests":
            return await _tool_list_requests(args, spec=spec)
        if name == "get_request":
            return await _tool_get_request(args, spec=spec)
        if name == "repeat_request":
            return await _tool_repeat_request(args, spec=spec, http=http)
        if name == "list_mcps":
            return await _tool_list_mcps(args, spec=spec)
        if name == "describe_mcp":
            return await _tool_describe_mcp(args, spec=spec)
        if name == "call_mcp":
            return await _tool_call_mcp(args, spec=spec)
    except KernelError as exc:
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


def _iter_json_blocks(text: str, marker: str) -> Iterator[dict[str, Any]]:
    """按出现顺序产出 `marker` 之后**所有的**合法 JSON 对象。

    v0.42 从 `_parse_json_block` 里抽出来的：批量执行要"全部"，
    而收尾协议（`FINAL`）只要"第一个"。两处共用同一套切分 ——
    否则容错规则会漂移（"ACTION 认代码围栏、FINAL 不认"这种）。

    **认不出就跳过**，不猜。容错两条（都是实测会遇到的模型习惯）：

    · 外面裹了 ```json 代码围栏（先找 `{`，围栏自然被跳过）；
    · JSON 后面跟着解释文字（靠 `_extract_json_object` 配对切出第一段）。
    """
    if not text:
        return
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
            yield parsed


def _parse_json_block(text: str, marker: str) -> dict[str, Any] | None:
    """从 `marker`（`ACTION` / `FINAL`）那一行之后取出**第一个** JSON 对象。

    认不出返回 None。容错见 `_iter_json_blocks`。
    """
    for parsed in _iter_json_blocks(text, marker):
        return parsed
    return None


def parse_action(text: str) -> tuple[str, dict[str, Any]] | None:
    """`ACTION: {...}` → `(工具名, 参数)`。认不出返回 None。

    ⚠️ v0.42 起它只返回**第一个** —— 完整的批量语义见 `parse_actions`。
    保留它是因为"这条回复里有没有动作"这个判断到处都是，
    而那些调用方 genuinely 只需要一个。
    """
    actions = parse_actions(text)
    return actions[0] if actions else None


def parse_actions(text: str) -> list[tuple[str, dict[str, Any]]]:
    """把一条回复里**所有** `ACTION` 按出现顺序解析出来（v0.42）。

    ## 为什么是全部而不是第一个

    见 `MAX_ACTIONS_PER_TURN`：实测 271 轮（14.2%）一轮写了多个
    ACTION，旧版只执行第一个、其余静默丢弃 —— 模型据此以为自己
    已经看过那些输出。执行全部才是**与模型认知对齐**的做法。

    ## 容错

    · 某一条不是合法 JSON → **跳过它，继续后面的**（不是整轮作废）：
      模型常先写一个"形状示意"再写真的那个；
    · 没有 `tool` 字段 / `args` 不是对象 → 跳过那一条；
    · 一条都没有 → 空列表，调用方据此走"纯思考回合"分支。

    ⚠️ **不在这里封顶**：封顶要在执行处做，因为只有那里知道
    一共写了几条、丢了几条，也才能把"丢了"告诉模型
    （见循环里的 `_batch_header`）。解析层悄悄截断会让
    "为什么最后两条没跑"变成不可解释的现象。
    """
    actions: list[tuple[str, dict[str, Any]]] = []
    for payload in _iter_json_blocks(text, "ACTION"):
        name = str(payload.get("tool") or "").strip()
        if not name:
            continue
        args = payload.get("args")
        actions.append((name, args if isinstance(args, dict) else {}))
    return actions


def parse_final(text: str) -> dict[str, Any] | None:
    """`FINAL: {...}` → 结论文档。认不出返回 None。"""
    return _parse_json_block(text, "FINAL")


def extract_final_analysis(payload: dict[str, Any] | None) -> tuple[str, list[str], list[str]]:
    """从结论文档里取 `(summary, tried, ruled_out)`（v0.38）。

    ## 为什么单独一个函数，而不是在收尾那处内联取

    历史题解**重生成**要走同一条提取逻辑（`scripts/regenerate_ctf2_writeups.py`
    从 transcript 重跑）。内联一份的话，两处口径迟早漂移 ——
    而症状是"重生成的题解少了 `tried`"，看起来像模型没写。

    ## 容错：形状不对就当没有，**不抛**

    `tried` / `ruled_out` 在协议里是字符串数组，但模型实测会给：
      · 缺字段 → 空列表；
      · 给成单个字符串 → 包成一项（别丢内容）；
      · 给成含 `null` / 数字 / 嵌套对象的数组 → **逐项过滤**，
        只留非空字符串（与 `extract_candidates_with_reasons`
        "坏的就丢并记下"同一条思路）。

    ⚠️ **不做"猜结构"**：如果它给的是 `[{"step": "...", "why": "..."}]`，
       这里会**丢掉**那一条（因为不是字符串）。这是刻意的取舍 ——
       猜错结构会把整块内容按错误的形状渲染出来，而"少一条"至少
       不会误导。真遇到那种形状时应该改协议，不是在这里猜。
    """
    if not isinstance(payload, dict):
        return "", [], []

    raw_summary = payload.get("summary")
    summary = raw_summary.strip() if isinstance(raw_summary, str) else ""

    def _strings(value: object) -> list[str]:
        if isinstance(value, str):
            # 单个字符串包成一项：模型偶尔把数组写成一个长句。
            return [value.strip()] if value.strip() else []
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out

    return summary, _strings(payload.get("tried")), _strings(payload.get("ruled_out"))


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


def _truncation_reminder(reply_chars: int) -> str:
    """输出**被长度上限切断**时喂回去的那句话（v0.41.2）。

    ## 为什么必须与 `_format_reminder()` 分开

    这两件事以前在代码里是同一件事：`parse_action` 返回 None 就喂
    `_format_reminder()`，于是「格式写错了」和「话说一半被物理切断」收到
    同一句「请严格用下面两种格式之一」。

    而它们的**解决办法相反**：

    - 格式错 → 重写一遍，照着模板来。
    - 被切断 → 再写一遍照样会被切（同样的产出规模，同样的上限）。模型
      必须先**压缩输出**，把动作提前，才有可能写完。

    ⚠️ 为什么会被切：**不知道**。`completion_tokens` 到顶是实测，但
    "推理部分占了多少"无从判断 —— 网关不上报 `reasoning_tokens`（见
    `TokenUsage` 的说明，那份响应里 `reasoning_content` 有 6412 字符而
    `reasoning_tokens` 仍是 0）。所以这里只说"再写一遍还会被切"，不写
    "推理吃满了额度"——那是没有证据的归因。

    实测出处：`ctf2try_01M4Z94FND98QAC7T513ZTPM89`（78 轮 unsolved）
    里有 **22 轮** `completion_tokens >= 4096`，散在 turn 11/17/21/24/29/
    30/38/41/42/50/53/55/56/58/60/62/64/67/68/69/72/73。它看到的是自己
    的话说到一半就没了，但**不知道自己是被切的** —— 因为提醒语说的是格式。

    ## 参数

    `reply_chars` 是这次回复的字符数，写进提示里让模型有个量感（它自己
    不知道 4096 token 是多长）。传 0 或负数也照样出提示，只是不带数字。

    ⚠️ 不提"4096 token"这个具体额度：那是 `LlmGateway.max_tokens`，
    哪天调了它，这句话就会开始说谎。说"达到输出长度上限"永远是对的。
    """

    size = f"（这次回复 {reply_chars} 字符）" if reply_chars > 0 else ""
    return (
        f"你上一次回复**达到输出长度上限被切断了**{size}，后面的内容没发出来 —— "
        "这不是格式问题，重写一遍还会被切。\n"
        "这次请**先只写 ACTION 那一行**，把推理压到一两句（动作执行完再想下一步）。\n"
        'ACTION: {"tool": "run_shell", "args": {"command": "ls -la /work"}}\n'
        f"（可用工具只有 {' / '.join(TOOL_NAMES)}）\n"
        "如果确实已经拿到答案，就直接写 FINAL 那一行，不要先写长篇分析。"
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
        # ⚠️ 不转达的后果：`kernel_system.md` 里写着"沙箱默认无网络"，
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
        elif not spec.network_isolated:
            # ★ 2026-10-18：WEB 题的沙箱也有网了（为了让容器里的
            #   `gobuster` 能用）。**必须显式写进首轮提示**，否则模型
            #   照旧只会 `http_request` 一个个猜路径 —— 实测那种猜法
            #   76 轮 40 个路径全 404，而字典里一次能扫 4600 个。
            lines.append(
                "- 沙箱**有网络**，可以直连上面的地址。镜像里装着 `gobuster` / "
                "`ffuf` / `curl` / `nmap`，字典在 `/usr/share/dirb/wordlists/`。"
                "要找没人告诉你的路径时，用 `run_shell` 跑 `gobuster dir "
                "-u <靶站> -w /usr/share/dirb/wordlists/common.txt -t 10 "
                "--timeout 8s -q` —— 比用 `http_request` 一个个猜快几个数量级。"
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
    # ★ v0.36：把持久区里**已经有什么**直接列出来（宿主侧读的，不用模型探索）。
    #
    # 见 `SolveSpec.persist_listing` 的说明：实测模型不一定照做那句 `ls`，
    # 所以由服务端把已知的事实写进提示词。
    if spec.persist_listing:
        lines += [
            "",
            f"## 持久区里已经有这些东西（`{PERSIST_DIR}`，上次留下的）",
            "",
            "**直接用，不要重做**：",
            *[f"- {item}" for item in spec.persist_listing],
            "",
            f"（需要看内容就 `cat` / `file` 它们，都在 `{PERSIST_DIR}` 下。）",
        ]
    # ★ v0.41：攻防知识互查 —— 这个资产在既往评估里留下过什么。
    #
    # ## 位置：在方法卡**之前**
    #
    # 理由：这一段讲的是**这个资产自己**（纵向），而方法卡讲的是**同类题
    # 别人怎么解**（横向）。离题目更近的那个该排前面 —— 模型对越靠前的
    # 参考材料权重越高，而"这台机器三个月前有过未授权访问"显然比
    # "另一道题用过 gobuster"更该被先考虑。
    #
    # ⚠️ 内容不含攻击载荷（只取 title/severity/category/verification），
    #    见 `history` 模块 docstring 的约束 3。
    # ⚠️ 查不到时这里**不是空串**，是一段"没有历史记录"的说明 ——
    #    见 `SolveSpec.history_text` 的说明。
    if spec.history_text.strip():
        lines.append(spec.history_text)
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


def _band_of(fraction: float, bands: tuple[float, ...]) -> int | None:
    """返回 `fraction` 越过的最深梯度下标（0 起），没越过任何一个返回 None。

    学 strix `core/hooks.py::_crossed_stage`：`fraction` 是"已用比例"。
    只返回**最深**那档，调用方据此保证"每档只提示一次、越接近越急"
    （若每档都返回，模型会同时收到三条互相矛盾的催促）。
    """
    crossed: int | None = None
    for i, edge in enumerate(bands):
        if fraction >= edge:
            crossed = i
    return crossed


#: 三档收尾指令（学 strix `_ROOT_DIRECTIVES`）。
#:
#: ★ 措辞刻意**不鼓励猜**：在「自动提交」模式下，一个猜出来的 flag 会被直接
#: 提交并**消耗一次错误机会** —— 那是不可再生资源（见
#: `DEFAULT_CTF2_MAX_WRONG_SUBMISSIONS`）。所以三档的共同底线都是
#: "给出有依据的候选，或者把卡点说清楚"，而不是"随便交一个"。
#:
#: 三档的**唯一区别是紧迫度**：第 0 档还允许"收尾性验证"，第 1 档只许关必要的
#: 线，第 2 档明令"停止一切新动作、立刻交结论"。
_WRAPUP_DIRECTIVES: tuple[str, ...] = (
    (
        "⏳ 预算已用约 70%。**开始规划收尾**：不要再开大的新方向，"
        "把正在验证的线收一收，确保核心目标能按时结出结论。"
    ),
    (
        "⏳ 预算已用约 85%。**优先收口**：停止开启新方向，只把必要的几条线关掉。"
        "开始整理「已经确认了什么」，准备按协议交出结论。"
    ),
    (
        "🛑 预算已用约 95%。**立即停止一切新动作**：不要再跑新命令、不要再探新路径。"
        "现在就把已有证据整理成结论 —— 此刻还没收尾的部分，到上限时会**全部作废**。"
    ),
)


def _final_stretch_note(remaining: int) -> str:
    """只剩几轮时的收尾提醒（第 2 档，向后兼容的旧入口）。

    ⚠️ v0.70 起主循环走 `_wrapup_note(stage, remaining)` 的三档梯度版本；
    这个函数只为兼容旧调用/测试保留，行为等价于"第 2 档 + 具体剩余轮数"。
    """
    return _wrapup_note(2, remaining)


def _wrapup_note(stage: int, remaining: int | None = None) -> str:
    """按梯度档位生成收尾提醒（v0.70，学 strix 三档 urgency）。

    ## 为什么三档的措辞不能只是"催得更急"这一维度

    模型在预算尾声最容易犯的错不是"不着急"，而是"开着三条没跑完的线不肯放"。
    所以每一档除了催，都要给出**这一档该做什么、不该做什么**：
      · 第 0 档：允许把正在验证的线收尾，禁止开新的；
      · 第 1 档：只关必要的，开始整理结论；
      · 第 2 档：一切新动作停止，立刻交。
    只喊"快点"而不说"停什么、留什么"，模型只会把手头命令跑完再停，等于没催。

    `remaining` 可空（成本维度触发时没有"剩余轮数"概念），非空时补一句
    精确剩余量，让模型对"还有多少空间"有具体感。
    """
    stage = max(0, min(stage, len(_WRAPUP_DIRECTIVES) - 1))
    head = _WRAPUP_DIRECTIVES[stage]
    if remaining is not None:
        head = f"{head}\n（本轮结束后只剩 {remaining} 轮。）"
    return (
        f"{head}\n"
        "- 已经有依据充分的答案 → 用 FINAL 给出候选，并写清证据来自哪几次工具输出；\n"
        "- 还没拿到答案 → 用 FINAL 说清：试过什么、结果如何、卡在哪一步、"
        "还差什么信息。**这比再开一个新方向有用得多** —— 那份说明会留在"
        "过程里，人和下一轮都能接着用；\n"
        "- 不要在收尾阶段开新的探索方向：它大概率跑不完，"
        "而你会因此连「目前已经知道什么」都没说清。"
    )


def _human_hint_note(hint: str) -> str:
    """把人运行中补充的线索包成一条注入消息（v0.57，P2）。

    ## 为什么要包一层，而不是把原文直接 append

    裸文本会被模型当成"又一段上下文"扫过就忘。包一层明确标注
    「这是人在你运行过程中补充的线索」，模型才知道它**优先级高于**
    它自己的探索方向 —— 这正是"人机协同"要的那个效果：人看过程看到
    卡点，直接给方向，模型下一轮就转向。

    ## ⚠️ 措辞纪律

    「请把它并入你当前的判断，但不要因此放弃已有证据」—— 防止模型
    因为一条人类线索就丢掉前面几十轮已经确认的事实（那是比"不听人"
    更糟的失败：把已验证的证据作废）。人给的是**增量**，不是**重置**。
    """
    return (
        "📌 人在你运行过程中补充了一条线索（不是系统指令，是一位工程师看着你的"
        "过程后给的提示）。请把它并入你当前的判断：\\n"
        f"\\n{hint}\\n\\n"
        "⚠️ 但不要因为这条线索就放弃你**已经验证过**的证据 —— 它是对你当前方向的"
        "增量补充，不是让你推倒重来。如果这条线索与你已确认的事实冲突，以证据为准。"
    )


def _sandbox_revived_note(round_no: int) -> str:
    """沙箱被重建之后，告诉模型"这是新容器、旧东西没了"（v0.43）。

    ★ 这条消息的**唯一目的**是阻止模型拿旧假设继续：它会接着上一轮的思路
    往下走（"刚才那个文件里的内容……"），而那个文件已经不存在了。不说清楚，
    它会把"文件不见了"当成自己记错，然后花好几轮去找一个本来就没了的东西。

    ⚠️ "还在"那三项必须逐条点明，否则模型的恢复成本比重新开始还高：
      附件目录与宿主资源是**只读挂载**、`/work/persist` 是**读写挂载** ——
      三者都不属于容器的生命周期。而 `/work` 其余部分是 tmpfs，容器一停就没。
    """
    return (
        f"⚠️ 系统通知：你的解题沙箱在上一轮之后**被外部移除**了（不是你的操作导致）。"
        f"系统已经重建了一个新容器（第 {round_no} 次），你可以继续解题。\n"
        "\n"
        "**重建意味着这些没了**：\n"
        "- `/work` 下你自己下载、解压、编译、写出来的文件**全部消失**"
        "（它是内存盘，容器一停就没了）；\n"
        "- 你之前起的后台进程（`nc` 监听、`http.server`、编译任务等）**都没了**；\n"
        "\n"
        "**这些还在**：\n"
        f"- `{PERSIST_DIR}`（持久区，宿主目录挂载）—— 笔记、脚本、中间结果"
        "如果放在这里就还在；\n"
        f"- `{ATTACHMENT_DIR}`（题目附件）与 `{HOST_RESOURCES_PATH}`（宿主资源）"
        "都是**只读挂载**，也还在；\n"
        "\n"
        "请**先花一条命令**确认现状（比如 `ls -la "
        f"{PERSIST_DIR}`），再决定从哪一步接着做。"
        "不要假设之前的文件或进程还在。"
    )


def _per_action_budget(count: int) -> int:
    """一轮批量动作里，**每条**输出能占多少字符（v0.42）。

    单条时就是 `MAX_TOOL_OUTPUT_CHARS`（行为与批量之前完全一致）；
    多条时把 `MAX_TURN_OUTPUT_CHARS` 平分，且**不低于 2000** ——
    低于这个数的输出基本没有分析价值，不如让模型自己缩小命令范围。

    ⚠️ 下限是 `max(2000, …)` 而不是 `MAX_TURN_OUTPUT_CHARS // count`：
       5 条时平分为 3200 尚可，但如果哪天把上限调大、这个函数被
       用在更多条数上，纯平分会算出几百字符的预算 —— 那样的回灌
       等于把输出截成一句没用的开头。
    """
    if count <= 1:
        return MAX_TOOL_OUTPUT_CHARS
    return max(2000, MAX_TURN_OUTPUT_CHARS // count)


def _format_action_results(
    completed: Sequence[tuple[str, str, str, str]], dropped: int
) -> str:
    """把一轮里所有动作的输出拼成**一条** user 消息（v0.42）。

    ## 单条时输出与批量之前**逐字符相同**

    这是刻意的：既有行为（含提示词里教给模型的「工具 `x` 的输出：」这种
    形状）不因为批量能力而变。只有真的写了多条时才多出抬头。

    ## 多条时为什么要把条数写在抬头里

    模型必须知道"我写的 7 个里只跑了 5 个"。不说的话它会按 7 份输出
    继续推理 —— 那正是 v0.41 想治而没治到的病（旧版只提醒"只跑了 1 个"
    却还是只跑 1 个）。现在两件事一致了：跑了哪些、没跑哪些，都写清楚。
    """
    if len(completed) == 1 and not dropped:
        name, _command, shown, repeated_note = completed[0]
        return f"{repeated_note}工具 `{name}` 的输出：\n{shown}"

    total = len(completed) + dropped
    header = (
        f"（这一轮你写了 {total} 个 ACTION，系统按顺序执行了 {len(completed)} 个。）\n\n"
    )
    if dropped:
        header += (
            f"⚠️ 一轮最多执行 {MAX_ACTIONS_PER_TURN} 个 —— 最后 {dropped} 个**没有执行**。"
            f"需要的话下一轮再写（一次别超过 {MAX_ACTIONS_PER_TURN} 个）。\n\n"
        )
    blocks = [
        f"{repeated_note}### {name}：{_clip(command, 120)}\n{shown}"
        for name, command, shown, repeated_note in completed
    ]
    return header + "\n\n".join(blocks)


def _clip(text: str, limit: int) -> str:
    """截断长文本，并在末尾**明确说明被截断了**。

    静默截断是本项目吃过亏的一类：模型看到的是"命令输出就这些"，
    于是它基于不完整的信息下结论。一句话的提示就能避免。

    ⚠️ **这个函数只留头**，它的调用点里有一处（动作纪要，`digest.append`）
       正是要"短而全"的一行索引 —— 那里首尾拼接会把一行可读的纪要毁成
       `ls -la /work`…（中间省略 120 字符）…`total 0`。要首尾双段的是
       **工具原始输出**那条路径，用 `_clip_both_ends`，别改这里。
    """
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原长 {len(text)} 字符；用更精确的命令缩小范围）"


def _clip_both_ends(text: str, limit: int) -> str:
    """截断长文本，**保留头尾两段**，中间写省略说明。

    ## 为什么工具原始输出必须留尾（v0.41）

    关键信息经常在**末尾**：HTTP 响应体的最后才是上传结果、命令输出的
    最后几行才是结论。只留头的直接后果实测在
    `ctf2chal_01M4QRAT254DE91N89338Z7P4M`（[ACTF2020 新生赛]Upload，
    78 轮、5,536,545 token）—— 模型 FINAL 的原话是：

        上传结果信息位于响应体的末尾，而返回页面长达 8530~8640 字符
        （内嵌一个巨大的 SVG），始终看不到上传后的提示/路径

    全库 66 份解题过程里，截断标记出现 **764 处**，落在 **693 / 1905 个
    assistant 轮**里（36.4%），其中原长超过 `MAX_TOOL_OUTPUT_CHARS` 的
    有 **222 处**。这些轮次里模型看到的都是"只有开头"。

    ⚠️ 比例按**轮次去重**算：一处标记可能在一轮里出现多次（同一轮的工具
    块里 heredoc 携带的字符串也会带上这个形态），用处数当分子会虚高。

    ⚠️ 上面那个 token 数字（5,536,545）只覆盖**有记账的那几次解题**：
    真库里 `ctf2_solve_usages` 共 215 行，只属于 6 个 attempt
    （78+63+33+22+12+7 轮），而解题过程文件有 66 份。它**不能**当作
    「CTF2 的整体 token 结构」引用。

    ## 为什么只给这两条路径用，不给纪要

    见 `_clip` 的说明：纪要是**索引**，索引要短要全；首尾拼接会把
    一行索引变成三段碎片的拼贴，既更难读也更占位置。

    ## 边界：两边加起来正好等于 `limit`

    `limit` 可能是奇数（比如 8001），所以左边取 `limit // 2`、右边取
    `limit - limit // 2` —— 两者的和恒为 `limit`，不多切一个字符。
    省略掉的字符数写 `len(text) - limit`，与两段之和严格互补。
    """
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    head_len = limit // 2
    tail_len = limit - head_len
    omitted = len(text) - limit
    return (
        text[:head_len]
        + f"\n…（中间省略 {omitted} 字符；要看被省略的部分，用更精确的命令缩小范围）\n"
        + text[-tail_len:]
    )


def _output_fingerprint(text: str) -> str:
    """工具输出的指纹，用于判断"这条命令是不是白跑了第二遍"。

    归一化只做两件事：把连续空白折叠成一个空格、去掉首尾空白，然后取
    sha256。**不做时间戳/数字归一化** —— 那需要知道每种输出的格式（`ls`
    的时间列、`ping` 的耗时、`hashcat` 的 banner），猜错就会把两次不同的
    输出算成一样，比漏判更坏。

    代价是"只有时间戳不同"的两次输出不算重复。这批数据里它恰好不影响：
    同命令且整段相同的对子有 56 个，其中时间戳不同的那些本来就带着新的
    信息（至少能看出"服务还活着"），不提示也不算错。

    ⚠️ 已知限制：两端都被截断的输出（差异被切在 `MAX_TOOL_OUTPUT_CHARS`
    之后看不见）会算成相同 —— 这类约占旧方案命中的三成。判不了就是判不了，
    不在这里假装判得了。
    """
    return hashlib.sha256(re.sub(r"\s+", " ", (text or "").strip()).encode()).hexdigest()


async def solve_in_sandbox(
    spec: SolveSpec,
    *,
    sandbox: SandboxContainer,
    gateway: LlmGateway,
    system_prompt: str,
    transcript: list[str] | None = None,
    on_progress: Callable[[str, str, dict[str, str]], None] | None = None,
    hint_provider: Callable[[], str | None] | None = None,
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

    ## ★ v0.57 `hint_provider`：运行中的人机协同（P2）

    `hint_provider` 是一个**无参回调**，每轮开始前被调用一次，返回
    一条人类在运行中补充的线索（或 `None` 表示没有）。返回非 `None`
    时它被追加进 `recent`（`role="user"`），下一轮模型就能看到。

    ### 为什么用回调而不是直接传一个队列

    本函数按设计**不碰数据库**（见上面的 ⚠️）。队列住在 DB，而读 DB
    需要同步 Session —— 在 async 里同步查库会阻塞事件循环。所以由
    **调用方**（worker 的同步段）持有 Session 并实现这个回调：
    每轮开始前 worker 已经把"有没有新线索"查好，回调只是把那条字符串
    交出来。这样本函数仍然对 DB 一无所知。

    ### 为什么追加成 `role="user"` 而不是 system

    模型对"用户刚说的"遵从度高于"系统一直说的" —— 系统提示词在
    `_messages()` 里每轮都在，模型会逐渐当背景；而一条新鲜的 user
    消息是"新信息"。这与 `_final_stretch_note` / `_sandbox_revived_note`
    的做法一致（那些也是 `role="user"`）。

    ### ⚠️ 回调也要"不抛"

    与 `on_progress` 同一条纪律：取线索失败（DB 抖一下）不该让一次
    跑了三十轮的解题停摆。所以下面用 try 包住，异常只记 warning。
    """
    # ★★ v0.58：多智能体编排在这里**分流**，而不是在循环体里加分支。
    #
    # ## 为什么在入口分流
    #
    # `run_multi_agent` 是"root + N 个 child 各跑一条循环"，它的控制流与下面
    # 这条单循环**结构不同**（多协程、有 spawner、有汇总）。硬塞进同一个
    # `for turn` 循环会让两边互相污染 —— 单循环的读者永远要同时想着
    # "这里多 agent 时走不走"。分流之后，单循环对 `multi_agent=False`
    # **逐字节不变**（这正是本版最硬的验收标准），多智能体是一条独立的、
    # 可单独测的路径。
    #
    # ⚠️ `hint_provider` / `on_progress` 原样透传：人给线索只喂给 root
    #    （子 agent 有明确 task，不掺人类线索，见 `run_agent_loop`）。
    if spec.multi_agent:
        from app.kernel.multiagent import run_multi_agent

        root_prompt, child_prompt = build_multi_agent_prompts(spec)
        return await run_multi_agent(
            spec,
            sandbox=sandbox,
            gateway=gateway,
            root_system_prompt=root_prompt,
            child_system_prompt=child_prompt,
            on_progress=on_progress,
            hint_provider=hint_provider,
        )
    log = transcript if transcript is not None else []
    # ★★ v0.35：备用接入点（换模 fallback）。
    #
    # 主接入点一旦调不动（401 key 失效 / 429 限流 / 5xx / 长时间过载），
    # 在**判整道题失败之前**先试备用 —— 那时可能已经跑了三十多轮、
    # 在沙箱里做完了大量真金白银的分析（实测一次 ReadError 让跑到第 14 轮、
    # 已反汇编出完整 IL 的题全部作废）。
    #
    # ⚠️ 只换一次（不是每轮都重试全部备用）：一次换模失败就说明备用也不行，
    #    继续试只会把回合预算耗在"连不上模型"上，而回合预算本该花在解题上。
    pending_fallbacks: list[FallbackLlm] = list(spec.fallbacks)
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

    # ★ v0.70：收尾催促已提示到第几档（-1 = 还没提示过任何一档）。
    # 回合维度与成本维度各记一份 —— 各自只喊一次，取更深那档注入（见主循环）。
    _warn_stage: int = -1
    _cost_warn_stage: int = -1

    # ★ v0.45：把"这次带了几张方法卡"从 spec 拷到 outcome。
    #
    # 为什么不在收尾时直接读 `spec.notes_count`：遥测落库在
    # `_record_outcome(db, challenge, outcome)`（见 `_run_worker` 的
    # ③ 同步段），那里**只拿得到 outcome** —— spec 是 `solve_in_sandbox`
    # 的入参，早就出了作用域。而要为此改 `_record_outcome` 的签名，
    # 就得动它在 `tests/` 里的十几处调用，把一处纯记账变得比它值得的
    # 更重。
    outcome.notes_count = spec.notes_count

    # ★ v0.47：同理，把"这次带没带手法块"拷进 outcome，给遥测落库用。
    #
    # 直接读 `spec.offensive` 而不在这里重算"开关 ∧ 分类" —— 那正是
    # `build_spec` 算它的理由（见 `SolveSpec.offensive` 的注释）。
    outcome.offensive = spec.offensive

    # ★ 本次解题内"跑过的命令"台账（v0.41 修订）—— 用于识别空转。
    #
    #   键是 `(工具名, 参数序列化, 整段输出的指纹)`，值是**第一次**跑它的轮次。
    #   三条一起比对才算重复：命令相同、输出也一模一样。同命令不同输出说明
    #   拿到了新信息，那**不是**空转（判据与被否掉的"前 400 字符"方案见主
    #   循环里那段注释）。
    seen_calls: dict[tuple[str, str, str], int] = {}

    def notify(kind: str, summary: str, payload: dict[str, str]) -> None:
        """把一条过程送给回调（有回调时）。**吞掉回调的异常。**"""
        if on_progress is None:
            return
        # 单循环也带归属：与多智能体（multiagent.notify）用同一组键，
        # 导入器就不用按"跑的是哪个引擎"分叉。单循环里只有一个 agent，
        # 用固定的自身标识，行为与旧版逐字节一致（旧版靠 importer 的默认值）。
        enriched = dict(payload)
        enriched.setdefault("agent", _AGENT_SELF_REF)
        enriched.setdefault("agentName", "自研引擎")
        try:
            on_progress(kind, summary, enriched)
        except Exception:  # noqa: BLE001 - 过程不该影响解题结果
            logger.warning("CTF2 过程事件发送失败（不影响解题）", exc_info=True)

    # ★ `trust_env=False` —— 这个 client 只喂 `_tool_http_request`，而那条
    #   路径**已有白名单**（`guard_tool_url` + `assert_allowed_target`），
    #   所以目标本身是校验过的。加这一条的理由与那层不同：
    #   httpx 的 `trust_env` 默认 `True`，会读环境里的 `HTTP_PROXY` /
    #   `HTTPS_PROXY` / `ALL_PROXY` 把请求交给那个代理 —— 白名单校验的是
    #   URL，管不了"谁在实际收这个请求"。所以这里补上，关掉那条改道。
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0), trust_env=False) as http:
        # ★ v0.43：本次解题已经重建过几次沙箱（上限见 `MAX_SANDBOX_REVIVALS`）。
        revivals = 0
        for turn in range(1, spec.max_turns + 1):
            outcome.turns = turn
            # ★★ 沙箱已经没了 → 立刻中止（v0.41）。
            #
            # ⚠️ **位置必须在 `await gateway.chat()` 之前**：放在工具执行之后
            #    的话，这一轮已经花掉一次 LLM 调用才被发现，而那一次调用
            #    必然白花（容器都没了，它说什么都做不成）。放在之前，容器
            #    一死就省下后续**所有**轮。
            #
            # 实测代价（2026-10-16「被偷走的文件」）：容器在第 24 轮的工具
            # 调用后死掉，没人报，模型用 `echo x1` 探到第 63 轮 —— 39 轮、
            # 约 62 万 token，换回的是一句 "The container has been
            # persistently unavailable"。
            if sandbox.died:
                # ★★ v0.43：先试着**重建**，而不是立刻放弃。
                #
                # ## 为什么值得重建
                #
                # v0.41 那条"一死就停"解决的是**白烧 token**（模型对着没了
                # 的容器敲几十轮），但它把"外部误伤"也一并判成了死刑。实测
                # 2026-10-17「粗心的开发人员」：跑到 turn 42、40 分钟，容器
                # 被外部清掉，下一轮直接中止 —— 而模型当时的判断、已探明的
                # 目标结构、写在 `/work/persist` 里的笔记全都还在上下文里。
                # 一个新容器 + 一句"这些东西还在、那些东西没了"就能接着跑。
                #
                # ## 为什么重建能救回来
                #
                # 沙箱里真正值钱的东西都不属于容器生命周期：`/work/persist`
                # 是宿主目录的读写挂载，附件与 `HOST_RESOURCES_PATH` 是只读
                # 挂载。丢的只有 tmpfs 上的临时产物和后台进程 —— 代价明确，
                # 而且是可以用一条 `ls` 重新确认的。
                #
                # ⚠️ 顺序：先 `destroy()` 再 `start()`。`destroy()` 是幂等的
                #    （见它的 docstring），而 `start()` 里的 `docker run --name`
                #    遇到**同名残留容器**会直接 `Conflict`。`died` 的判据
                #    （`No such container`）说明容器已经不在了，但"不在了"和
                #    "名字没被占用"是两件事，这一步不省。
                if revivals < MAX_SANDBOX_REVIVALS:
                    revivals += 1
                    log.append(f"[turn {turn}] 沙箱容器已消失，第 {revivals} 次重建")
                    notify(
                        "reasoning",
                        f"解题沙箱被外部移除，正在重建容器（第 {revivals} 次）",
                        {"text": f"沙箱容器已消失，第 {revivals} 次重建"},
                    )
                    try:
                        sandbox.destroy()
                        sandbox.start()
                    except Exception as exc:  # noqa: BLE001 - 起不来的原因很多，都要落到中止
                        log.append(f"[turn {turn}] 重建沙箱失败：{exc}")
                    else:
                        log.append(f"[turn {turn}] 沙箱已重建：{sandbox.name}")
                        # ★ 必须告诉模型"这是新容器"：否则它会接着上一轮的
                        #   思路去找一个已经不存在的文件，并把"不见了"当成
                        #   自己记错 —— 那种弯路比重新开始更贵。
                        recent.append({"role": "user", "content": _sandbox_revived_note(revivals)})
                # 重建过 `MAX_SANDBOX_REVIVALS` 次还是死的，或者重建失败 → 中止。
                # ⚠️ 判据是 `sandbox.died` 而不是上面那个 `try` 的结果：
                #    `start()` 成功但容器起完就死（`_verify_running` 会抛）
                #    这条路径也必须在下一轮之前被拦住。
                if sandbox.died:
                    outcome.error = (
                        "解题沙箱已消失（容器被外部移除），重建也没成功，本次解题中止。"
                        "已完成的轮次见解题过程。"
                    )
                    log.append(f"[turn {turn}] {outcome.error}")
                    outcome.transcript = "\n\n".join(log)
                    return outcome
            # ★ 收尾提醒（v0.70：单档 → 三档梯度，学 strix `core/hooks.py`）。
            #
            # 动机来自实测：旧实现只在"剩 5 轮"时提醒一次，太晚太突然 ——
            # 模型早已开了三条没跑完的线，只能硬停（2026-10-22 一次渗透扫描
            # 被 60 分钟墙钟截断，就是这种死法）。改成按**已用比例**分 70/85/95
            # 三档，越接近越急，模型有足够轮次把手上证据整理成结论。
            #
            # ⚠️ `_warn_stage` 记录"已提示到第几档"，只在**跨到更深一档**时才注入，
            #   避免每轮重复同一句话（那会挤掉有用内容）。用 `>` 而不是 `>=`
            #   正是"每档只喊一次"。
            remaining = spec.max_turns - turn
            used_fraction = turn / spec.max_turns if spec.max_turns else 0.0
            turn_stage = _band_of(used_fraction, TURN_WARN_BANDS)
            if turn_stage is not None and turn_stage > _warn_stage:
                _warn_stage = turn_stage
                note = _wrapup_note(turn_stage, remaining)
                recent.append({"role": "user", "content": note})
                log.append(f"[turn {turn}] 预算催促（第 {turn_stage + 1} 档）：{used_fraction:.0%}")

            # ★ 成本/预算维度（v0.70）：若调用方给了 token 上限，按同一套梯度判。
            #   `_cost_warn_stage` 与 `_warn_stage` 分开记 —— 两个维度各自只喊一次，
            #   且取**更深**那档注入，避免同轮收到两条互相打架的催促。
            if spec.max_total_tokens:
                spent = outcome.total_tokens_spent()
                cost_fraction = spent / spec.max_total_tokens
                cost_stage = _band_of(cost_fraction, COST_WARN_BANDS)
                if cost_stage is not None and cost_stage > _cost_warn_stage:
                    _cost_warn_stage = cost_stage
                    if cost_stage > _warn_stage:
                        # 成本维度比回合维度更急 → 用它覆盖本次注入
                        recent.append(
                            {
                                "role": "user",
                                "content": _wrapup_note(cost_stage, remaining)
                                + f"\n（token 预算已用 {cost_fraction:.0%}。）",
                            }
                        )
                        log.append(
                            f"[turn {turn}] 预算催促（成本维度，第 {cost_stage + 1} 档）："
                            f"{cost_fraction:.0%}"
                        )
                        _warn_stage = cost_stage
            # ★ v0.57：每轮开始前问一次"人有没有补线索"（P2 人机协同）。
            #
            # 放在 final_stretch 之后、chat 之前：这样这一轮模型一定能看到
            # 最新的线索。`hint_provider` 是调用方（worker 同步段）给的
            # 无参回调 —— 本函数不碰 DB，只把它返回的字符串追加进 recent。
            #
            # ⚠️ 回调失败只记 warning，不中断解题（理由见函数 docstring：
            #    取线索失败不该让已经跑了三十轮的解题停摆）。
            if hint_provider is not None:
                try:
                    hint = hint_provider()
                except Exception:  # noqa: BLE001 - 见 docstring 的"回调也要不抛"
                    logger.warning("读取人给线索失败，本轮跳过", exc_info=True)
                    hint = None
                if hint:
                    recent.append({"role": "user", "content": _human_hint_note(hint)})
                    log.append(f"[turn {turn}] 已注入人给线索：{hint[:80]}")
                    # 注入也是一条过程事件 —— 人要知道"我给的线索进去了"，
                    # 否则他会怀疑输入框没生效而反复输入。
                    try:
                        notify(
                            "reasoning",
                            f"已把人工线索并入本轮推理：{hint[:60]}",
                            {"text": hint[:200]},
                        )
                    except Exception:  # noqa: BLE001 - notify 内部已吞，这里再兜一层
                        logger.warning("推送人工线索事件失败", exc_info=True)
            try:
                reply = await gateway.chat(_messages(), system=system_prompt)
            except KernelError as exc:
                # ★★ v0.35：判失败之前先试备用接入点。
                #
                # `LlmGateway.chat` 对**连接层**抖动（连不上 / 超时 / 读中断）
                # 已经重试过 4 次，走到这里的是它重试完仍失败、或是一个
                # 不该重试的错误（4xx / 5xx / 畸形响应）。两类都意味着
                # "这个接入点现在不行"，而不是"这道题做不出来"。
                if pending_fallbacks:
                    nxt = pending_fallbacks.pop(0)
                    logger.warning(
                        "主接入点调用失败（%s），换备用模型 %s 继续解题：%s",
                        exc.message[:120],
                        nxt.label or nxt.model,
                        spec.challenge_id,
                    )
                    log.append(f"[turn {turn}] LLM 失败，换备用接入点：{exc.message}")
                    notify(
                        "reasoning",
                        f"主模型调用失败，已切换到备用模型 {nxt.label or nxt.model} 继续。",
                        {"text": f"换模：{exc.message}"},
                    )
                    gateway = gateway.failover(nxt)
                    continue  # ★ 不计入这一轮：换模本身不是一次解题尝试
                # LLM 层面的失败不重试：这一层上面没有"退避"的语义
                # （重复问同一个问题不会得到不同答案），而回合预算要
                # 留给真正在做事的回合。
                outcome.error = exc.message
                log.append(f"[turn {turn}] LLM 失败：{exc.message}")
                outcome.transcript = "\n\n".join(log)
                return outcome

            # ★ v0.36：记下这次调用的 token 用量。
            #
            # ⚠️ 位置在**成功之后** —— 抛异常的调用没有 usage 可记，
            #    所以 `llm_calls` 数的是**成功返回的调用**，
            #    与 `usages` 的长度恒等。两者不等的情况只出现在
            #    "接入点没给 usage"（那时 `TokenUsage.reported=False`）。
            #
            # ⚠️ 带上 `gateway.model` / `gateway.label`：v0.35 换模之后
            #    `gateway` 已经指向新接入点，所以同一次解题的不同轮次
            #    可能落在不同模型上 —— 而那正是"换模值不值"要回答的。
            outcome.llm_calls += 1
            outcome.usages.append((turn, gateway.model, gateway.label, reply.usage))

            text = reply.text or ""
            log.append(f"[turn {turn}] assistant({reply.source}): {_clip(text, 4000)}")
            recent.append({"role": "assistant", "content": text})

            # ★ v0.41.2：这次回复是不是被输出长度上限切断了。
            #
            # 见 `_truncation_reminder` 的说明：以前"格式错"与"被切断"收到
            # 同一句提醒，而它们的解决办法相反 —— 前者要重写，后者重写没用
            # （推理又吃满额度），必须**压缩推理、把动作提前**。
            #
            # ⚠️ 两个都要认：OpenAI 说 `length`，Anthropic 说 `max_tokens`。
            truncated = reply.finish_reason in ("length", "max_tokens")

            # ① 结论？
            final = parse_final(text)
            if final is not None:
                outcome.candidates, rejected = extract_candidates_with_reasons(final)
                # ★ v0.38：把结论文档里的三块分析也带出来。
                #
                # ⚠️ 位置在**这个 return 之前**，且与 `candidates` 同一层 ——
                #    放晚了（比如放到 `if not outcome.candidates` 里面）
                #    会让"解出了"的那些题丢掉 summary/tried/ruled_out，
                #    而它们同样是题解正文的来源。
                (
                    outcome.summary,
                    outcome.tried,
                    outcome.ruled_out,
                ) = extract_final_analysis(final)
                # ★ v0.41：处置建议（只有 IR 任务的提示词里让模型写这个字段）。
                #
                # ⚠️ 原样带走**不做校验** —— 见 `SolveOutcome.workorders`
                #    的说明：校验发生在展示层。这里若也校验一遍，两处
                #    容错策略迟早漂移，而症状是"界面上少了某条工单"。
                outcome.workorders = final.get("workorders")
                # ★ v0.47：渗透评估的安全发现（只有 `pentest-*` 的提示词让模型写）。
                # 与 workorders 完全同构：原样带走、不在这里校验 ——
                # 转换与容错发生在 `scans/agent_runner` 那一刻。
                outcome.findings = final.get("findings")
                # ★ v0.73：同时登记到活收集器 —— 见 `SolveSpec.finding_sink`。
                # 这一行与上一行不重复：上一行是"本次运行的结果"，这一行是
                # "进程被砍时也别丢的存根"。两者角色不同，缺谁都会出事。
                _sink_findings(spec, outcome.findings)
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

            # ② 动作？（v0.42：可能一次好几个）
            actions = parse_actions(text)
            if not actions:
                # 没有动作也没有结论：这一轮是纯思考/追问，把它当推理发出去。
                # ⚠️ 只发**有实义**的文本（空串或极短的寒暄不值得进活动流，
                #    而那些行会把真正有信息量的推理淹掉）。
                if len(text.strip()) >= 20:
                    notify("reasoning", _clip(text.strip(), 200), {"text": text.strip()})
                # ★ v0.41.2：被切断时喂**另一句** —— 见 `_truncation_reminder`。
                reminder = (
                    _truncation_reminder(len(text)) if truncated else _format_reminder()
                )
                recent.append({"role": "user", "content": reminder})
                continue

            # ★ v0.42：**批量执行**这一轮写的所有 ACTION。
            #
            # ## 为什么从"只回告"改成"真的执行"
            #
            # v0.41 对多写 ACTION 的处理是**只补一句提醒**（"只执行了第 1 个"），
            # 执行逻辑不动 —— 理由是"一次工具调用一次结果"是本循环的基本契约。
            # 但 v0.41 同时记下了一个数字：**271 轮（14.2%）** 写了 ≥2 个
            # ACTION，极端一轮 23 个。模型**一直在试图批量**，而提醒它
            # "一条一条来"并没有让它少写 —— 只是让它继续以为自己写过的都跑了。
            #
            # 顺着模型的行为做才是对的：同一轮里的多个动作**串行执行**，
            # 轮次仍算 1 轮。于是 LLM 调用次数不变、有效动作数 ×N，
            # 而"它以为自己看过某些输出"这个错误认知也一并消失。
            #
            # ## 为什么串行而不是并发
            #
            # 沙箱是有状态的：`cd`、写文件、起后台进程都要求顺序。
            # 并发会让"第二步读到第一步的产物"变成运气问题。
            #
            # ## 为什么封顶、并把丢弃数说出来
            #
            # 模型写 23 个时若只跑 5 个又不告诉它，它仍会以为自己看到了
            # 23 份输出 —— 同一个病在另一头复发。所以 `_format_action_results`
            # 会把"写了几个 / 执行了几个 / 丢了几个"一次说清。
            batch = actions[:MAX_ACTIONS_PER_TURN]
            dropped = len(actions) - len(batch)
            per_action_budget = _per_action_budget(len(batch))
            #: 每条一个 `(工具名, 命令, 已截断输出, 重复提醒)`，循环外组装消息。
            completed: list[tuple[str, str, str, str]] = []

            # ★ v0.41.2：这一轮虽然动作完整、跑成功了，但**回复本身被切过**。
            #
            # 切在动作之后 → JSON 完好，动作照常执行；可后面那段推理（往往
            # 是"下一步该干什么"）丢了。不告诉它，它会以为自己那轮想完了。
            truncation_note = (
                "\n\n（提醒：你上一次回复达到长度上限被切断，末尾内容丢失。）"
                if truncated
                else ""
            )

            # ★ 把这一轮的理由先送出去（"先说后做"）。
            #
            # 模型在 `ACTION:` 之前写的那段话（"The shell is failing, let me retry"
            # / "Cracked! Password = 9919"）是**为什么这么做的解释**，
            # 先于那几条命令出现才读得通。批量时它只发一次 ——
            # 一段理由对应这一整轮的动作。
            narration = _narration_before_action(text)
            if narration:
                notify("reasoning", _clip(narration, 200), {"text": narration})

            for name, args in batch:
                command = str(args.get("command") or json.dumps(args, ensure_ascii=False))
                log.append(
                    f"[turn {turn}] tool {name} "
                    f"{_clip(json.dumps(args, ensure_ascii=False), 500)}"
                )
                result = await dispatch_tool(
                    name, args, spec=spec, sandbox=sandbox, http=http
                )
                log.append(f"[turn {turn}] result: {_clip(result, 2000)}")
                # ★ 完整输出进证据池（v0.42）—— 提交闸门要逐字符核对
                #   "这个答案到底有没有在工具输出里出现过"。用 `transcript`
                #   做不了这件事：它里面的 result 被 `_clip(…, 2000)` 截过。
                outcome.tool_outputs.append(result)
                # ★ v0.45：工具调用数（生产遥测）。
                #
                # ⚠️ 数的是**执行之后**的这一次，与上面的 `log.append`
                #    一进一出 —— 被 `MAX_ACTIONS_PER_TURN` 丢掉的那些
                #    根本没走到这里，所以不会被数进来。
                outcome.tool_calls += 1
                # ★ 重复执行要变成**可见信号**（v0.41，v0.41.1 换判据）。
                #
                # ⚠️ v0.42：判据本身没变，但它现在**逐条**应用 ——
                # 同一轮里的第 3 条与第 1 条一样会被认出来（`seen_calls`
                # 在循环内即时更新，所以"这一轮里写了两遍同一条命令"
                # 也抓得到，而那正是批量执行最容易产生的浪费）。
                #
                # 判据 = **命令指纹 + 整段输出指纹**两个都相同，且比对范围是
                # **本次解题全程**（不是只比上一轮）。
                #
                # ## 为什么不用"输出前 400 字符"
                #
                # 那个方案两头都错，数据可核（66 份解题过程、1717 次工具调用）：
                #
                # - **偏高**：前 400 字符相同但后面不同的情形很多，最典型的是
                #   同一页面的两次提交 ——
                #   `ctf2chal_01M3SF0MAZPNSARD7CWECXYWPC-20261001-020127.txt`
                #   turn 9 与 turn 10 都 POST 到 `/charge`，前 400 是同一份
                #   `<div class="container">` 模板，**差异在 700 字符之后**：
                #   turn9 是 `alert-danger`「操作失败」，turn10 是
                #   `alert-success`「操作成功」。模型拿到了新信息，那时提示它
                #   "一样，换方向"会把一次成功的探测说成重复。
                #   （顺带：这两次请求的 `body` 其实是 `id=1&price=9` 与
                #   `id=4&price=万` —— 命令本来就不同，所以带命令指纹的判据
                #   天然不会把它们判成重复。）
                # - **偏低（更严重）**：命令与整段输出**完全一致**的对子里，绝大
                #   多数**不相邻**（同一题 t3→t12、t3→t17、t5→t30 反复打同一
                #   payload，2032 字节一模一样）。只比上一轮，这些一次都抓不到。
                #
                # ## 实测口径（我自己复算，Kali，66 份 / 1717 个结果块）
                #
                # | 口径 | 命中 |
                # | --- | --- |
                # | 同命令 + 整段输出**原始字节**相同（本题内任意早前） | 54 对 |
                # | 同命令 + 整段输出**折叠空白后**相同（本题内任意早前） | **56 对（3.3%）** |
                # | 同命令 + 前 400 字符相同（本题内任意早前） | 55 对 |
                # | 不看命令 + 前 400 相同 + **只取结果首行**（任意早前） | 523 |
                # | 不看命令 + 前 400 相同 + 完整结果（任意早前） | 370 |
                # | 不看命令 + 前 400 相同 + 完整结果（只跟相邻轮比） | 222 |
                #
                # ⚠️ 54 / 55 / 56 三个数**只差口径，不是笔误**，引用时必须写明是
                #    哪一种（本判据用的是**折叠空白**那一档，即 56）：
                #
                #    - 54 = 原始字节：判据最严，纯空白差异（行尾空格、连续空行）
                #      不算重复。代价是同一份输出换个缩进就漏判。
                #    - 56 = 折叠空白（`_output_fingerprint` 用的就是这个）：
                #      判据按"信息量"算，纯空白差异不算新信息。
                #    - 55 = 只取前 400 字符：比 56 少 1 对 —— 那一对的差异在
                #      400 字符之后，前 400 之内只差空白，所以它被算成重复；
                #      而按整段折叠空白看，两段只在空白处不同，也该算重复，
                #      于是它**同时**落在 55 与 56 里。差的那 1 对不是判据冲突，
                #      是"前 400 看不到整段"的边界效应。
                #
                #    ★ 做判据验证**必须用归一化指纹而不是原始字节**：否则 54 与
                #      56 这种差会反复出现，而且每次都要重新查一遍才发现是口径。
                #
                # 前 400 那一档与整段折叠空白只差 1 对，说明"命令一样时前 400
                # 相同"基本等价于"整段相同"；反过来说，只要命令不同，前 400
                # 相同就毫无意义 —— 上面那个 HTML 模板的例子里两次的 `body` 是
                # `id=1&price=9` 与 `id=4&price=万`，本来就是两条不同的命令。
                # 这就是为什么判据必须**带上命令指纹**。
                #
                # ⚠️ 523 那个数（旧版注释引用过）是**另一个口径**：它把每个
                #    `[turn N] result:` 块**只取首行**、丢掉续行。多行输出的首行
                #    常常是同一种页头（`HTTP 200`、hashcat banner、HTML 模板），
                #    于是判出一大批"重复"，实际后文完全不同。把续行算上就掉到
                #    370，"只跟相邻轮比"再掉到 222 —— **222 与别的复核者得到的
                #    221 是同一族**（差 1 对是空输出的计法）。三个数各自对应一种
                #    语义，引用时必须写明是哪种，否则会被当成同一件事。
                #
                # ⚠️ **已知限制**：两端都被截断的重复（差异被切在 8000 字符之后
                #    看不见）本判据判不了 —— 那类约占前 400 方案命中的三成。
                #    这条限制没被解决，只是不再假装"前 400 相同就是重复"。
                #
                # ⚠️ 比对范围是**本次解题**（`seen_calls` 随这次 `solve_in_sandbox`
                #    存活），**不是跨 attempt 的历史**。跨 attempt 才是真"重复
                #    尝试"，但那需要持久化台账，本版没做。
                #
                # ## 为什么要有这条
                #
                # 提示词里写了「连续 3 次尝试没有新信息就换方向」
                # （`prompts/kernel_system.md:149-158`），但代码里没有任何判据
                # （grep `新信息|consecutive|streak|same_output|stagnat` 零命中）——
                # 完全靠模型自觉。这里把它接上执行。
                #
                # ⚠️ 这一行**只进给模型看的那条 `recent`**：不进 `log`（落盘
                #    transcript，题解正文靠它）、不进 `digest`（动作纪要）、
                #    也不发事件 —— 否则题解里会混进一堆系统提示。
                call_key = (
                    name,
                    json.dumps(args, ensure_ascii=False, sort_keys=True),
                    _output_fingerprint(result),
                )
                repeated_note = ""
                first_turn = seen_calls.get(call_key)
                if first_turn is not None:
                    repeated_note = (
                        f"⚠️ 这条命令你在第 {first_turn} 轮跑过，输出与那次完全一样 —— "
                        "它不会再给你新信息，换一条路。\n\n"
                    )
                else:
                    seen_calls[call_key] = turn

                # ── 动作纪要：这一轮做过什么 ────────────────────
                #
                # ★ 见 `DEFAULT_MAX_TURNS` 那段说明。这一行是**永久保留**的
                # （直到超过 `MAX_DIGEST_LINES`），而完整的对话内容会被裁掉。
                #
                # 一行里放三样东西：轮次、命令、输出开头 —— 足够让模型认出
                # "这件事我做过了"，又不至于把上下文撑爆。
                # ★ v0.41.2：被切断的那几轮，纪要里要看得出来。
                #
                # 纪要现在是"这一轮做过什么"的唯一长期记录（完整对话会被裁掉）。
                # 不标记的话，一个话说到一半的回合与一个完整回合在纪要里长得
                # 一模一样 —— 模型回头看会以为自己那轮已经想清楚了。
                #
                # ★ v0.42：批量时每个动作各占一行（turn 号相同）——
                # "这一轮干了三件事"在纪要里就该是三行，压成一行会让
                # 模型回头看不到其中两件做过什么。
                digest.append(
                    f"[t{turn}] {name}: {_clip(command, 110)} → {_clip(result, 140)}"
                    + (" ⚠️输出被截断" if truncated else "")
                )
                if len(digest) > MAX_DIGEST_LINES:
                    del digest[: len(digest) - MAX_DIGEST_LINES]

                completed.append(
                    (name, command, _clip(result, per_action_budget), repeated_note)
                )

                notify(
                    "tool_call",
                    _clip(f"{name} {command}", 200),
                    {"tool": name, "command": command, "output": result},
                )

            recent.append(
                {
                    "role": "user",
                    "content": _format_action_results(completed, dropped) + truncation_note,
                }
            )
            # ★ 记录"这次解题真的跑过工具"（v0.31）—— 供 auto 模式的
            #   证据门禁判断"答案是跑出来的还是想出来的"。
            #   见 `SolveOutcome.saw_tool_output`。
            outcome.saw_tool_output = True

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


#: 归一化时用来折叠空白（含换行）。
#:
#: ⚠️ 不能只 `replace(" ", "")`：工具输出里 flag 经常被折行
#:    （`flag{abc\ndef}`）、或被表格/JSON 缩进隔开，只去空格会漏判。
_WS_RE = re.compile(r"\s+")


def _normalize_for_match(text: str) -> str:
    """把文本归一化成"只比内容、不比排版"的形式（v0.42）。

    三步，顺序固定：

      1. `html.unescape` —— 工具输出里的 `flag&#123;xxx&#125;` 是**同一个 flag**
         的另一种写法（HTTP 响应体、HTML 源码里极常见）。不先解码就会漏判，
         而那会白白 hold 掉一个真候选。
      2. 折叠所有空白 —— 换行/制表/连续空格都不影响"这个字符串出没过"。
      3. `casefold` —— flag 大小写敏感是老传统，但**我们这里比的是"出没出现过"，
         不是"是不是标准答案"**。平台那边才是判大小写的地方；这层闸只问
         "模型说的这串东西是不是从工具输出里来的"。放宽到 casefold 能把
         "模型把 `Flag{...}` 写成 `flag{...}`"这类无害差异放过去。
    """
    return _WS_RE.sub("", html.unescape(text)).casefold()


def answer_is_grounded(answer: str, outputs: Sequence[str]) -> bool:
    """声称的答案**是否真的在某次工具输出里出现过**（v0.42）。

    ## 这道闸问的和不问的

    问：答案的来源是不是真实工具输出（挡"凭空编一个 flag"）。
    不问：这个答案对不对（那是平台的判据，我们付不起误判）。

    所以判据刻意宽松 —— 归一化后子串匹配。宁可放过一个错的，
    不可误杀一个对的：`hold` 不消耗额度，而"真候选被 hold"会让人
    多看一眼，代价远小于"编造的答案烧掉不可再生的提交额度"。

    ## 空答案

    `answer` 为空时返回 `False` —— 空串是任何字符串的子串，不特判的话
    这道闸对空答案完全失效。
    """
    needle = _normalize_for_match(answer)
    if not needle:
        return False
    return any(needle in _normalize_for_match(text) for text in outputs)


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
    flag: str = "",
    tool_outputs: Sequence[str] = (),
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

    ## ★★ v0.42：第三道闸 —— 答案必须落在真实输出里

    上面两道闸有一个共同的弱点：**它们只读模型自己写的东西**。
    `saw_tool_output` 是一个布尔位（跑过工具吗），`evidence` 是模型的
    自述。于是"跑过几条无害命令 + 编一段 evidence + 编一个 flag"
    仍然能走到 `submit`。

    补的这道闸把**原始工具输出**（`tool_outputs`）拉进判据：声称的
    答案必须在某次输出里出现过。归一化后子串匹配 —— 换行、HTML 实体、
    大小写都不影响判定（见 `answer_is_grounded`）。

    仍然**不是**"答案正确性校验"：它只回答"这串字符从哪来"。
    正确与否是平台的事，我们付不起误判成本。

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
    if mode != KernelSubmitMode.AUTO.value:
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
    # ★ v0.42：答案必须在**真实工具输出**里出现过。
    #
    # 上面两道闸都只用了模型自己写的东西（`saw_tool_output` 也只是一个
    # 布尔位）。一个"跑了工具、也给了一段 evidence"的模型，完全可以
    # 在这两者之上编出一个 flag —— 而错误提交烧的是不可再生额度。
    #
    # 这道闸第一次把**原始输出**拉进判据：`tool_outputs` 是这一场里每次
    # 工具调用的完整结果（见 `SolveOutcome.tool_outputs`）。判据宽松
    # （归一化后子串匹配，见 `answer_is_grounded`），所以它挡住的是
    # "凭空编"而不是"格式不对"。
    #
    # ⚠️ `tool_outputs` 为空时**不拦截**：调用方没提供证据池（旧调用点、
    #    单测、人工确认路径）时，这道闸无从判断，拦下来只会误伤。
    #    "有没有跑过工具"由上一道闸负责，两者职责不重叠。
    if tool_outputs and not answer_is_grounded(flag, tool_outputs):
        return SubmitDecision(
            "hold",
            "auto 模式下证据不足：这个候选 flag **没有在任何一次工具输出里出现过** —— "
            "它可能是推出来的，也可能是编出来的。候选已保留，请人工核对后再决定是否提交。",
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

    `KernelSubmission` 的行**必须在发出请求之前**就落库（`pending`）。
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
    submission_id = new_id("knsub")
    with session_scope() as db:
        db.add(
            KernelSubmission(
                id=submission_id,
                challenge_id=challenge_id,
                flag=normalize_flag(flag),
                status=KernelSubmissionStatus.PENDING.value,
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
        challenge = db.get(KernelChallenge, challenge_id)
        if challenge is not None and challenge.status != KernelChallengeStatus.SOLVED.value:
            challenge.status = KernelChallengeStatus.SUBMITTING.value
        db.commit()

    # ── 发请求（async） ──────────────────────────────────
    status_value = KernelSubmissionStatus.PENDING.value
    message = ""
    receipt: str | None = None
    already_solved = False

    # ★★ 先把客户端建出来，**再进 try**（2026-10-10 修）。
    #
    # ## 为什么必须挪出来
    #
    # `client_factory()` 在"账号不存在 / 凭据解不开"时抛的是
    # **`ApiError`**（见 `service.build_client_sync`），而 `ApiError` 与
    # `KernelError` **没有继承关系** —— 下面那个 `except KernelError` 捕不到它。
    # 于是异常穿透本函数，而上面**已经写下了一条 `pending` 提交行**。
    #
    # ## 那条假 `pending` 的代价比"一次报错"大得多
    #
    # `pending` 的语义是"请求已发出、结果未知"（见
    # `vocab.KernelSubmissionStatus.PENDING`）—— 那是**一张要人工对账的欠条**。
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
            row = db.get(KernelSubmission, submission_id)
            if row is not None:
                row.status = KernelSubmissionStatus.FAILED.value
                row.local_error = _safe_text(reason)
            challenge = db.get(KernelChallenge, challenge_id)
            if challenge is not None:
                _advance_challenge(
                    challenge, KernelSubmissionStatus.FAILED.value, flag, already_solved=False
                )
            db.commit()
        return KernelSubmissionStatus.FAILED.value, reason

    try:
        async with client:
            payload = await client.submit_flag(
                practice_id=str(remote_ids.get("competition_remote_id") or ""),
                challenge_id=str(remote_ids.get("remote_id") or ""),
                flag=normalize_flag(flag),
                sub_flag_id=remote_ids.get("sub_flag_id"),
            )
        status_value, message, receipt = interpret_submit(payload)
    except KernelError as exc:
        # 传输层失败 → `failed`（**不是** `wrong`，不消耗额度）。
        # 这个区分见 `KernelSubmissionStatus` 的说明：把失败记成错误提交
        # 会让"还剩几次"莫名其妙地变少。
        status_value = KernelSubmissionStatus.FAILED.value
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
        if isinstance(exc, KernelResponseError) and exc.code == CTF2_ERROR_ALREADY_SOLVED:
            already_solved = True
            message = PLATFORM_ALREADY_SOLVED_MESSAGE

    # ── 回写（sync） ────────────────────────────────────
    with session_scope() as db:
        row = db.get(KernelSubmission, submission_id)
        if row is not None:
            row.status = status_value
            row.remote_message = message or None
            row.remote_submission_id = receipt
            if status_value == KernelSubmissionStatus.FAILED.value:
                row.local_error = message
        challenge = db.get(KernelChallenge, challenge_id)
        if challenge is not None:
            _advance_challenge(challenge, status_value, flag, already_solved=already_solved)
            # ★★ v0.48：判对了就把这题那条解题 scan 的状态跟着翻过来。
            #
            # ## 修的是什么（实测，2026-10-20，Kali 生产库 SimpleRev）
            #
            # `finalize_solve_scan` 只在 **worker 收尾那一刻**看题目状态，
            # 而 safe 模式下那一刻题目还是 `candidate_ready`（候选在等人
            # 确认）→ scan 被记成 `failed`。用户随后确认成功、题目变
            # `solved`，却**没有任何代码回头改那行 scan** —— 界面上它
            # 永远显示失败。实测「失败」比确认早了 41 秒。
            #
            # ## 为什么放在这里而不是 `confirm_submit` 里
            #
            # 这是**两条提交路径唯一的汇合处**（auto 与人工都调本函数），
            # 而"平台判对了但 scan 状态没跟上"与"谁提交的"无关。
            # 只修人工那条会让 auto 路径留着同一个洞。
            #
            # ⚠️ 该函数内部自己判了"只在 scan 已终态时才翻"：auto 模式在
            #    worker 还跑着的时候就提交候选，那时 scan 是 `running`，
            #    提前标成 completed 会让界面显示完成而进程还在干活。
            if status_value == KernelSubmissionStatus.CORRECT.value:
                ctf2_events.promote_solve_scan_to_solved(db, challenge)
        db.commit()

    return status_value, message


def interpret_submit(payload: dict[str, Any]) -> tuple[str, str, str | None]:
    """平台响应 → `(状态, 说明, 回执id)`。

    ## ★ 判"对"需要**两个**信号：明确的成功标志 + 一个回执 id

    只凭 `correct: true` 就标 `correct` 是危险的：我们没法区分
    "平台真的判对了"与"某个中间层返回了一个形状相似的默认值"。
    而回执 id 是**平台侧的凭据** —— 有它才说明这次提交真的进了
    平台的记录（`KernelSubmission` 上那条 `correct_implies_receipt`
    约束守着这条不变量）。

    拿不到回执 id 时退回 `pending` 并记下原因：这是**诚实**的处理，
    而不是"少解析一层"。用户看到一条待确认的记录，
    比看到"已解出"而下次同步又变回未解出要好得多。
    """
    if not isinstance(payload, dict):
        return KernelSubmissionStatus.PENDING.value, "平台返回了无法解析的响应。", None

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
                KernelSubmissionStatus.CORRECT.value,
                message or "平台判定 flag 正确。",
                receipt,
            )
        return (
            KernelSubmissionStatus.PENDING.value,
            "平台表示正确，但没有给出提交回执 id，无法确认这次提交真的入账。",
            None,
        )
    if correct is False or correct == 0:
        return (
            KernelSubmissionStatus.WRONG.value,
            message or "平台判定 flag 不正确。",
            receipt,
        )
    return KernelSubmissionStatus.PENDING.value, message or "平台未给出明确判定。", receipt


def _advance_challenge(
    challenge: KernelChallenge, status_value: str, flag: str, *, already_solved: bool = False
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
        if challenge.status == KernelChallengeStatus.SOLVED.value:
            return
        challenge.status = KernelChallengeStatus.UNSOLVED.value
        challenge.last_error = PLATFORM_ALREADY_SOLVED_MESSAGE
        return

    if status_value == KernelSubmissionStatus.CORRECT.value:
        challenge.status = KernelChallengeStatus.SOLVED.value
        challenge.confirmed_flag = normalize_flag(flag)
        challenge.candidate_flag = None
        challenge.solved_at = now_utc()
        challenge.last_error = None
    elif status_value == KernelSubmissionStatus.WRONG.value:
        # ★ 只有真正被平台判错的才计数（见 vocab.CTF2_CREDITED_WRONG_STATUSES）。
        challenge.wrong_submission_count = int(challenge.wrong_submission_count or 0) + 1
        challenge.candidate_flag = None
        if challenge.remaining_wrong_submissions <= 0:
            challenge.status = KernelChallengeStatus.FAILED.value
            challenge.last_error = "错误提交次数已用尽。"
        else:
            challenge.status = KernelChallengeStatus.SOLVING.value
    elif status_value == KernelSubmissionStatus.FAILED.value:
        # ⚠️ 请求没到达平台（`failed`）时**候选也不清**：那个候选还没被
        # 判定过，它不是"错的"。清掉它等于让用户重跑一次解题才能再交。
        challenge.status = KernelChallengeStatus.FAILED.value
        challenge.last_error = "提交请求失败（未到达平台）。"


# ══════════════════════════════════════════════════════════
# worker 入口
# ══════════════════════════════════════════════════════════


def build_spec(
    db: Session,
    challenge: KernelChallenge,
    *,
    mode: str | None = None,
    resume: bool = False,
    attempt_id: str | None = None,
) -> SolveSpec:
    """从数据库行拼出 `SolveSpec`。**唯一**读凭据的地方。

    凭据通过 `providers.service.decrypt_provider_key` 拿（复用既有的
    "明文只在一处出现"那条纪律）；额度默认值来自 CTF2 自己的配置行
    （`service.effective_ctf2_settings`），而不是环境变量 ——
    见 `settings.models.KernelConfig` 的说明。

    ## ★★ `attempt_id`：策略以快照为准（v0.28 修）

    这是那条时序漏洞的修复点。在这之前，`mode` / 回合预算都在**这里**
    现读 `effective_ctf2_settings(db)` —— 也就是"worker 真正开跑的
    那一刻"的全局设置，而它与"用户点解题的那一刻"之间隔着一段排队
    时间。后果见 `service.start_solve` 的说明（用户选了 safe 却被
    auto 提交）。

    现在的优先级是**快照 > 全局**：

      · 有 `attempt_id` → 读 `KernelSolveAttempt`，用它里面的 mode /
        回合预算 / brief / 模型。**不再读全局设置**（那正是修复的意义）；
      · 没有（测试直接调、或历史任务）→ 退回读全局，行为与修复前一致。

    ⚠️ 保留全局兜底不是为了"两条路并存"，而是因为 `build_spec` 是个
       公开函数（测试与脚本会直接调它）。让它在没有快照时直接报错，
       会把"兼容旧调用"变成"所有旧调用都炸"。
    """
    from app.kernel import service as ctf2_service
    from app.providers.models import ProviderAccount
    from app.providers.service import decrypt_provider_key

    # 沙箱配额与 `scans` 侧读的是**同一行配置**（见下面 `sandbox_row` 的说明）。
    # 局部导入与上面几个一致：这个模块被 scheduler 的线程反复调用，
    # 而 settings 领域只在真正启动一次解题时才需要。
    from app.settings import service as settings_service

    # ── 策略快照（有就用它，没有才读全局）────────────────────
    attempt: KernelSolveAttempt | None = None
    if attempt_id:
        attempt = db.get(KernelSolveAttempt, attempt_id)
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
        raise KernelError(
            "还没有配置 LLM provider，无法解题。请先在设置页添加一个 provider。"
        )

    environment = (
        db.execute(
            select(KernelEnvironment)
            .where(KernelEnvironment.challenge_id == challenge.id)
            .order_by(KernelEnvironment.created_at.desc())
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

    # ★ v0.36：续解时列出持久区里已有的产物（宿主侧读，不用模型探索）。
    #   与 `resume_digest` 同一个门（只在续解时做）—— 全新解题时
    #   持久区要么是空的、要么是更早留下的，把它写进提示词会让模型
    #   以为"这些是这次的输入"。
    persist_listing: list[str] = []
    if resume:
        persist_listing = _list_workspace_files(challenge.id)

    # ★★ v0.32 / v0.45：召回同类题的方法卡，并记下**张数**。
    #
    # `_recall_notes` 一次查询同时给出片段与张数 —— 张数进 `SolveSpec`
    # 只是为了被 `solve_in_sandbox` 拷进 `outcome`，最终落到遥测表。
    notes_text, notes_count = _recall_notes(db, challenge)

    # ★ v0.40：本题要不要追加智能体安全评测块。
    #
    # 两个条件**必须同时**成立：配置开关开着 **且** 本题分类属于那四类。
    # 少任何一个都返回 False（普通题、或开关关着时行为完全不变）。
    #
    # ⚠️ 放在这里算而不是解题循环里：那里是 async 且约定不碰数据库。
    # ⚠️ 召回失败不能连累这个判断 —— 所以它不是从 `_recall_notes` 里
    #    借返回值，而是独立算。
    agentsec_on = _agentsec_enabled(db, challenge)

    # ★ v0.41：本题要不要追加应急响应块。与上面那条**完全同构**，
    # 包括"两个条件同时成立"与"读配置失败返回 False"两条。
    #
    # ⚠️ 与 `agentsec_on` 分两次读配置，看起来是多余的查询。不合并的理由：
    #    合并要么改 `_agentsec_enabled` 的返回类型（那会让它的语义从
    #    "这道题要不要 agentsec 块"变成"这道题要哪几个块"，而调用点
    #    只关心前者），要么在 build_spec 里手写一遍"读配置 + 判前缀"，
    #    而那就有了第二份判据实现。两次查询的代价是一次主键命中，
    #    判据分叉的代价是静默失效 —— 取前者。
    ir_on = _ir_enabled(db, challenge)

    # ★ v0.42：本题要不要追加越权与注入手法块。
    #
    # ⚠️ 与上面两个**不同**：这里只读开关，不判分类（见 `SolveSpec.offensive`）。"排除两个场景包"那条判断在
    # `build_system_prompt` 里做，因为那里同时有 category 与两个场景开关。
    #
    # ⚠️ 复用上面已经读过的 `sandbox_row`，**不新增一次查询** —— 这一列
    # 就在 `sandbox_config` 那张单例表上，而 `sandbox_row` 在函数稍早处
    # 已经为主机配额读过一次了（见 `read_sandbox_config` 那段说明）。
    # 没配置行时按 False 走：与"关着"同义，不替用户编一个他设过的值。
    offensive_on = bool(sandbox_row.enable_offensive_prompt) if sandbox_row else False

    # ★ v0.58：是否走多智能体编排。与题型无关（执行拓扑是一个独立维度）。
    multi_agent_on = _multi_agent_enabled(db)

    # ★ v0.72：本次解题可用的 MCP server 配置（学 strix 的 list_mcps 三件套）。
    #
    # ## 为什么解析在**这里**而不在 mcp 模块里惰性做
    #
    # `settings_row` 已经是本函数读过的配置（见上面那段说明），再单独去
    # 读一次配置既多余、又会让"配置改了但这次没生效"的时序问题多一个来源。
    # 而且解析**必须容错**（坏条目跳过）：把它放在配置读取的同一步，部署者
    # 改完配置后的第一条日志里就能看到 warning。
    #
    # ⚠️ 解析失败**不影响解题**：`parse_server_configs` 内部吞掉形状错误
    #    只记 warning，最坏情况是"没有可用 MCP"——那正是默认状态。
    mcp_server_cfgs = mcp_mod.parse_server_configs(getattr(settings_row, "mcp_servers", None))

    # ★ v0.82：本次被关掉的内置工具。与上面那段同一条纪律 ——
    # 解析容错交给 `normalize_disabled`（未知名字跳过并记 warning），
    # 坏配置最坏退化成"全开"，也就是引入这个开关之前的行为。
    disabled_tool_names = tools_mod.normalize_disabled(
        getattr(settings_row, "disabled_tools", None)
    )

    # ★ v0.41：攻防知识互查。只在**本题确实是 IR 题且开关开着**时才查 ——
    # 理由与 `_ir_enabled` 两条件同构：
    #
    #   · 不是 IR 题却注入"历史发现"，等于给一道 pwn 题塞一段"这台机器
    #     有过未授权访问"，而它当时该做的是打二进制。多给的信息会变成
    #     错误的方向引导，比不给更糟。
    #   · 开关关着时不查，保证"关着 = 完全没有影响"在行为上成立
    #     （一条 SQL 都不发）。
    #
    # ⚠️ 查库失败**不影响解题**（`history` 模块内部已吞掉异常并退回
    #    "无历史记录"）。"因为查不到历史就不复盘"明显更糟。
    if ir_on:
        # ⚠️ 第二个返回值（资产标识）这里**丢弃**：`recall_for_challenge`
        #    内部已经把它写进日志（"为资产 %s 召回了 %d 条"），
        #    而 `SolveSpec` 没有对应字段 —— 解题目录里不需要它。
        #    接住再不用会让静态检查报 F841，而下一个人会猜"是不是漏传了"。
        history_text, _history_asset, history_count = recall_for_challenge(
            db,
            title=challenge.title or "",
            description=challenge.description or "",
            brief=resolved_brief or "",
        )
    else:
        history_text, _history_asset, history_count = "", "", 0

    return SolveSpec(
        challenge_id=challenge.id,
        title=challenge.title,
        category=challenge.category or "",
        description=challenge.description or "",
        profile=challenge.sandbox_profile or KernelSandboxProfile.BASE.value,
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
        # v0.36：持久区已有的产物清单（见 `persist_listing` 的说明）。
        persist_listing=tuple(persist_listing),
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
        notes_text=notes_text,
        # ★ v0.45：同一棵树的果实 —— 张数走另一条路（spec → outcome →
        #   遥测表），片段走 prompt。见 `_recall_notes` 的说明。
        notes_count=notes_count,
        # ★ v0.41：攻防知识互查的结果（见 `history_text` 的说明）。
        #   段落进 prompt（与 `notes_text` 同一条路），条数与资产标识只
        #   用于日志与遥测 —— 资产标识不进 prompt 是因为它已经写在
        #   段落里了，再放一份没有信息量。
        history_text=history_text,
        history_count=history_count,
        # ★ v0.40：是否追加智能体安全评测块（见 `SolveSpec.agentsec`）。
        agentsec=agentsec_on,
        # ★ v0.41：是否追加应急响应块（见 `SolveSpec.ir`）。
        ir=ir_on,
        # ★ v0.58：是否走多智能体编排（见 `SolveSpec.multi_agent`）。
        multi_agent=multi_agent_on,
        # ★ v0.72：MCP server 配置（见 `SolveSpec.mcp_servers`）。空元组 =
        #   没配，三个 MCP 工具会如实说"没有可用的连接"。
        mcp_servers=mcp_server_cfgs,
        # ★ v0.82：被关掉的内置工具（空元组 = 一件都没关，行为逐字节不变）。
        disabled_tools=disabled_tool_names,
        # ★ v0.42：手法块开关值（见 `SolveSpec.offensive` —— 它存的是
        #   开关本身，"该不该注入"要等 `build_system_prompt` 排掉场景包才定）。
        offensive=offensive_on,
        # 沙箱配额（见上面那段说明：这四个参数原来漏了，容器一直没有上限）
        cpus=sandbox_cpus,
        memory_mb=sandbox_memory_mb,
        # ★ v0.35：备用接入点。见 `_fallback_llms` —— 配置关掉时是空元组，
        #   与引入这个功能之前的行为完全一致。
        fallbacks=_fallback_llms(db, settings_row, primary=provider),
    )


def _fallback_llms(db: Session, settings_row, *, primary) -> tuple[FallbackLlm, ...]:  # noqa: ANN001
    """按配置解析出备用接入点。**没配就返回空元组**。

    ## 规则

    - `fallback_provider_id` 为空 → 空元组（不换模，默认）；
    - 指向的 provider 不存在 → 空元组 + **一条 warning**（配置指向了一个
      已被删掉的 provider，不能静默当作"没配" —— 那会让用户以为备用生效了）；
    - 指向的就是主接入点本身 → 空元组（换成自己是无意义的空转）。

    ## 为什么只有**一个**备用，而不是一串

    配置里只存一个 id。串式的"备用链"（A→B→C）在单机自托管的场景里
    没有实际需求，而每多一层就多一层"到底用的哪个模型"的歧义。
    将来真要支持，把这个函数改成读一张表即可 —— 调用方拿到的仍是
    `tuple[FallbackLlm, ...]`，形状不用变。
    """
    from app.providers.models import ProviderAccount
    from app.providers.service import decrypt_provider_key

    fid = getattr(settings_row, "fallback_provider_id", None)
    if not fid:
        return ()
    if primary is not None and fid == primary.id:
        logger.warning("CTF2 备用 provider 与主 provider 相同，忽略：%s", fid)
        return ()
    row = db.get(ProviderAccount, fid)
    if row is None:
        logger.warning(
            "CTF2 配置里的备用 provider %s 不存在（可能已被删除），本次不换模", fid
        )
        return ()
    return (
        FallbackLlm(
            model=row.model,
            api_key=decrypt_provider_key(row),
            base_url=row.base_url or "",
            label=row.label,
            kind=row.kind,
        ),
    )


def _agentsec_enabled(db: Session, challenge: KernelChallenge) -> bool:
    """这道题要不要追加智能体安全评测块（v0.40）。

    两个条件**同时**成立才返回 True：

    1. CTF2 配置里 `enable_agentsec_scenarios` 打开了；
    2. 本题分类属于 `agentsec-*` 那四类。

    ## ★ 为什么第二个条件不是"开关开着就注入"

    开关是"这个功能可用"，不是"所有题都按它跑"。把评测块注入到一道
    `pwn` 题里，会让模型读到"你的工作不是攻破系统拿 flag" ——
    而它当时确实该去攻破系统。那不是多给一段提示，是给出**互相矛盾
    的两套指令**，模型的行为会变得不可预测。

    ## 为什么读配置失败时**返回 False**

    失败时返回 False 是安全方向：不注入 = 行为与引入这个功能之前
    完全一致。返回 True 则意味着"配置读不到，于是我按最激进的那条
    假设走"，而那会给普通题注入错误的纪律。
    """
    from app.kernel import notes as notes_mod

    if not notes_mod.is_agentsec_category(challenge.category or ""):
        return False
    try:
        from app.kernel.service import effective_ctf2_settings

        return bool(effective_ctf2_settings(db).enable_agentsec_scenarios)
    except Exception:  # noqa: BLE001 - 读配置失败不该拦住解题
        logger.warning("读 CTF2 配置失败，本次不追加智能体安全评测块", exc_info=True)
        return False


def _ir_enabled(db: Session, challenge: KernelChallenge) -> bool:
    """这道题要不要追加应急响应块（v0.41）。

    两个条件**同时**成立才返回 True：

    1. CTF2 配置里 `enable_ir_scenarios` 打开了；
    2. 本题分类属于 `ir-*` 那三类。

    ## 为什么第二个条件不是"开关开着就注入"

    与 `_agentsec_enabled` 完全同一条理由，但后果更重一点：应急响应块
    第一节写的是"你的工作不是攻破系统""除 `/work` 外一律只读"。
    把这段注入到一道 `pwn` 题里，模型会拒绝执行任何攻击工具 ——
    那不是"多给一段提示"，是把这道题直接废掉，而且失败的样子是
    "模型不肯干活"，不是报错。

    ## 为什么读配置失败时**返回 False**

    失败时返回 False 是安全方向：不注入 = 行为与引入这个功能之前
    完全一致。返回 True 则意味着"配置读不到，于是我按最激进的那条
    假设走"，而那会给普通题注入"你是只读的"这条错误纪律。
    """
    from app.kernel import notes as notes_mod

    if not notes_mod.is_ir_category(challenge.category or ""):
        return False
    try:
        from app.kernel.service import effective_ctf2_settings

        return bool(effective_ctf2_settings(db).enable_ir_scenarios)
    except Exception:  # noqa: BLE001 - 读配置失败不该拦住解题
        logger.warning("读 CTF2 配置失败，本次不追加应急响应块", exc_info=True)
        return False


def _multi_agent_enabled(db: Session) -> bool:
    """这次解题要不要走多智能体编排（v0.58）。

    只有一个条件：CTF2 配置里 `enable_multi_agent` 打开了。它**不看
    分类** —— 与两个场景包开关不同，多智能体是执行拓扑，任何题型都可
    跑单循环或编排。

    读配置失败时返回 False（安全方向：不编排 = 行为与引入前一致）。
    """
    try:
        from app.kernel.service import effective_ctf2_settings

        return bool(effective_ctf2_settings(db).enable_multi_agent)
    except Exception:  # noqa: BLE001 - 读配置失败不该拦住解题
        logger.warning("读 CTF2 配置失败，本次走单循环", exc_info=True)
        return False


def _recall_notes(db: Session, challenge: KernelChallenge) -> tuple[str, int]:
    """召回同类题的方法卡并渲染成 prompt 片段。返回 `(片段, 张数)`。

    查不到就返回 `("", 0)`。

    ## 为什么返回张数而不只是文本

    v0.45 起要落生产遥测："这道题解题时到底有没有参考到别题的解法"。
    只看文本空不空会把"1 张"和"3 张"抹平，而这两者对"方法卡值不值"
    的意义完全不同 —— 张数是那条统计的分母。

    ⚠️ 之所以不用"稍后自己再查一次库"，是因为**这里就是唯一一次
    真实查询**：拿它的结果直接返回，比事后再查一遍既省一次往返，
    也不会在两次查询之间被并发写卡改变答案。

    ## 为什么吞异常

    召回是**锦上添花**：没有卡片只是少了参考，而"因为查不到卡片就不
    解题"明显更糟。所以任何失败（表不存在、查询出错）都退化成空串。

    ## 为什么在这里打印一条 info

    模型看到卡片与没看到卡片，解题路径会明显不同 —— 所以"这次到底
    召回了没有"是排查时该能一眼看到的事。没有这条日志，复盘时只能
    从 prompt 里翻。
    """
    try:
        from app.kernel import notes as notes_mod

        rows = notes_mod.notes_for_challenge(db, challenge)
        if not rows:
            return "", 0
        text = notes_mod.render_notes_for_prompt(rows)
        logger.info(
            "为 %s 召回了 %d 张同类题方法卡（category=%s）",
            challenge.id,
            len(rows),
            challenge.category or "未分类",
        )
        return text, len(rows)
    except Exception:  # noqa: BLE001 - 召回失败不该拦住解题
        logger.warning("召回同类题方法卡失败（本次不带参考）", exc_info=True)
        return "", 0


def _provider_from_snapshot(
    db: Session, attempt: KernelSolveAttempt, *, fallback
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

    ## ★★ 建完必须 `chown 1000:1000`（2026-10-13 修，实测发现）

    ⚠️⚠️ **这个目录此前一直是只读的，也就是说 v0.29 的持久区
    从未真正生效过。**

    根因是两个 uid 对不上：

      · 容器以 `USER solver`（**uid 1000**）运行 —— 见 base 镜像
        Dockerfile 的 `useradd --uid 1000`；
      · 而这个目录由服务进程（**root**）`mkdir` 出来，属主是
        `root:root`，权限 `0755`。

    挂载进容器后，uid 1000 对它**只有读权限**。实测（2026-10-13，
    `[FlareOn3]GUI` 的 A 组对照实验）模型在第 23 轮写了
    `/work/persist/harness.cs`，第 26 轮自己发现了问题并放弃：

        /work/persist is not writable. Let me use /work.

    后果**正是持久区要解决的那个问题本身**：产物退回 `/work`（tmpfs），
    容器一停全没。而且它还额外浪费了轮次 —— 模型花了 3 轮才绕开这个坑。

    ⚠️ 为什么用 `chown` 而不是 `chmod 0777`：容器里跑的是**题目给的
    不可信内容**（见 `workspace_dir` 的安全说明）。0777 会让宿主上
    任何本地用户都能往这个目录写，而它是 `:rw` 挂载进容器的 ——
    那等于开了一条"本地用户 → 容器内"的投毒路径。chown 到固定 uid
    既够用（容器里就是 1000）又不扩大攻击面。

    ⚠️ 为什么失败**不抛**：`chown` 需要特权，而服务在开发机上可能
    不以 root 跑。那时最坏结果是"持久区不可写"（退回修复前的行为），
    而不该是"整道题解不了"。但要**出声**记 warning —— 静默退化成
    修复前的行为，正是这个 bug 藏了这么久的原因。
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
        _make_writable_by_solver(path)
    return path if path.is_dir() else None


#: 容器内 `solver` 用户的 uid/gid。
#:
#: ⚠️ 与 base 镜像 Dockerfile 里的 `useradd --uid 1000 --gid 1000 solver`
#:    **必须一致**。两处各写一份会漂移，而漂移的症状就是持久区又变只读
#:    （而那时现象与这个 bug 一模一样，很难再查一遍）。
#:    镜像那边也留了一条指回来的注释。
SOLVER_UID = 1000
SOLVER_GID = 1000


#: 持久区清单最多列几项。防止一个塞了几百个文件的目录把提示词撑爆。
_MAX_PERSIST_LISTING = 40


def _list_workspace_files(challenge_id: str) -> list[str]:
    """列出持久区里**已有的产物**（宿主侧，给续解的提示词用）。v0.36。

    ## 为什么由服务端列，而不是让模型自己 `ls`

    见 `SolveSpec.persist_listing` 的说明：实测模型不一定照做那句
    "先 `ls -la /work/persist`"，而"上次留了什么"是我们**本来就知道**的
    事实（文件就在宿主上）。把已知事实写进提示词，省掉一轮探索。

    ⚠️ 只列**文件名与大小**，不读内容 —— 内容可能很大，而且模型自己
    会去读它需要的那个。

    ⚠️ 读目录失败返回空列表（不影响解题，只是少一段提示）。
    """
    workspace = _workspace_dir(challenge_id, create=False)
    if workspace is None:
        return []
    try:
        entries = sorted(workspace.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        logger.warning("读取持久区清单失败（这次续解不列它）：%s", exc)
        return []

    items: list[str] = []
    for path in entries:
        if len(items) >= _MAX_PERSIST_LISTING:
            items.append(f"…（还有更多，`ls {PERSIST_DIR}` 看全部）")
            break
        try:
            if path.is_dir():
                # 目录只标出来，不递归 —— 递归会让清单爆炸。
                items.append(f"{path.name}/（目录）")
            else:
                items.append(f"{path.name}（{path.stat().st_size} 字节）")
        except OSError:  # pragma: no cover - 并发删除
            continue
    return items


def _make_writable_by_solver(path: Path) -> None:
    """把持久工作区交给容器里的 `solver`（uid 1000）。

    见 `_workspace_dir` 的 ★★ 那一段：不这么做，`:rw` 挂载对容器而言
    实际是只读的。

    ⚠️ 只 chown **目录本身**，不递归：里面的内容是上一次容器写的
    （已经是 1000 了），而递归 chown 会在产物很多时变慢 ——
    这个函数在每次起容器前都会被调用。
    """
    try:
        os.chown(path, SOLVER_UID, SOLVER_GID)
    except (OSError, AttributeError) as exc:
        # AttributeError：Windows 上没有 os.chown（本机开发/跑测试时）。
        # OSError：权限不足（服务不是以 root 跑）。
        # 两种都只记 warning —— 见 docstring 里"为什么失败不抛"。
        logger.warning(
            "持久工作区无法改属主（容器内将不可写，产物退回 /work 会在容器停止后丢失）："
            "%s → %s",
            path,
            exc,
        )


def _attachment_paths(challenge: KernelChallenge) -> list[Path]:
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
    from app.kernel.attachments import FINGERPRINT_SUFFIX

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
                challenge = db.get(KernelChallenge, challenge_id)
                if challenge is not None and challenge.status in (
                    KernelChallengeStatus.SOLVING.value,
                    # ★ 加上 environment_ready（2026-10-10 修）。
                    #
                    # 与 `_record_outcome` 是同一个洞的另一半：
                    # worker 起来之后主状态就是它，而不是 `solving`。
                    # 漏了它，崩溃时这道题会**永久留在"环境已就绪"**——
                    # 而 `finally` 里的 `release_environment` 已经把环境
                    # 停掉、`access_url` 清空，两边说法不一致且看不出来。
                    KernelChallengeStatus.ENVIRONMENT_READY.value,
                    KernelChallengeStatus.SUBMITTING.value,
                ):
                    challenge.status = KernelChallengeStatus.FAILED.value
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
            select(KernelSubmission.id)
            .where(
                KernelSubmission.challenge_id == challenge_id,
                KernelSubmission.status == KernelSubmissionStatus.CORRECT.value,
            )
            .limit(1)
        ).scalar()
        is not None
    )


async def _run_worker(
    challenge_id: str, *, resume: bool = False, attempt_id: str | None = None
) -> None:
    """worker 的 async 主体。见 `run_worker` 的三段式说明。"""
    from app.kernel import service as ctf2_service

    # ── ① 同步段：准备 ────────────────────────────────────
    with session_scope() as db:
        challenge = db.get(KernelChallenge, challenge_id)
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
        if challenge.status == KernelChallengeStatus.SOLVED.value:
            logger.info("这道题已经解出，跳过解题：%s", challenge_id)
            return
        try:
            spec = build_spec(db, challenge, resume=resume, attempt_id=attempt_id)
        except KernelError as exc:
            challenge.status = KernelChallengeStatus.FAILED.value
            challenge.last_error = _safe_text(exc.message)
            ctf2_events.emit_abandoned(db, challenge, reason=exc.message)
            db.commit()
            return
        # ★★ 这道题背后有没有一个 CTF2 平台（v0.43）。
        #
        # 平台镜像的题（`competition` / `practice`）在下面要打三次平台：
        # 查"队友是不是已经解出了"、起靶机环境、收尾释放环境。而
        # **本地复盘案例**（`kind='local'`）三次都必然失败 —— 它没有远端、
        # 没有靶机，平台的开放接口里压根不存在这道题。
        #
        # 在同步段就把这个判断算好带出去，而不是在三个调用点各查一次库：
        # 三个地方分别判等于三次机会漏掉一处，而漏掉的那一处表现是
        # "应急响应任务跑了两秒就失败，错误说同步可能没完成"——
        # 一句与真实原因毫无关系的提示。
        #
        # ⚠️ 判据是 `kind != 'local'`，即**默认走平台**。认不出的 kind
        #    因此仍然会去碰平台并如实报错，这与
        #    `service._assert_submission_supported` 的 fail-closed 同向。
        _competition = db.get(KernelCompetition, challenge.competition_id)
        platform_backed = (
            _competition is not None
            and _competition.kind != KernelCompetitionKind.LOCAL
        )

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
    # ★ 本地复盘案例跳过这一步（v0.43）：没有远端 id，`platform_reports_solved`
    #   会直接返回 False（它内部判 remote_id 缺失即早退），也就是说这里原本
    #   不会出错 —— 但那是**依赖一个下游函数的实现细节**来保证正确性。
    #   显式跳过让"本地题不打平台"成为本函数自己的一条事实。
    reports_solved = False
    if platform_backed:
        try:
            reports_solved = await ctf2_service.platform_reports_solved(challenge_id)
        except KernelError as exc:
            logger.warning(
                "开跑前的平台状态探测失败（不阻断解题）：%s → %s", challenge_id, exc
            )
            reports_solved = False

    if reports_solved:
        with session_scope() as db:
            blocked = db.get(KernelChallenge, challenge_id)
            if blocked is not None and not _has_correct_submission(db, challenge_id):
                # ★ 回到 `unsolved` 而**不是** `failed`：这不是我们的失败。
                #   写成 `failed` 会让一道**已经解出的题**在界面上显示
                #   "失败" —— 那正是最容易误导用户的地方。
                blocked.status = KernelChallengeStatus.UNSOLVED.value
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

    # ★ 本地复盘案例不建 client（v0.43）。
    #
    # `build_client_sync()` 在没有绑定的 CTF2 账号时抛
    # `ApiError(409, "CTF2_NOT_BOUND")`，而本地复盘案例**根本不需要账号**
    # —— 它要的证据包是本机上传的，整个流程一次都不碰 CTF2 域名。这里
    # 若照旧无条件建 client，用户会看到「尚未绑定 CTF2 账号。请先绑定
    # 一个 PAT。」而他要做的是传一批日志做复盘，那句话把他指向了完全
    # 无关的方向。
    #
    # `client = None` 之后，下面三处平台调用各自按 `platform_backed` 跳过。
    client = ctf2_service.build_client_sync() if platform_backed else None
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
        started = (
            await ctf2_service.start_environment_for(client, challenge_id)
            if platform_backed and client is not None
            else None
        )
        # access_urls 是环境才拿到的东西，spec 里那份是空的 ——
        # 补上它，否则 http_request 的白名单里一个地址都没有。
        #
        # ⚠️ `started` 为 None 表示**这道题没有靶机**（本地复盘案例）。
        #    此时 `spec.access_urls` 保持 `build_spec` 给的值：IR 场景是
        #    纯附件复盘，容器本来就该 `--network none`，一个可访问地址都
        #    不该有。不做任何"补一个空地址"的动作 —— 空地址进白名单会
        #    让 `http_request` 的守卫判出"没有允许的目标"，而它本该
        #    压根不会被调用。
        if started is not None:
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
        #
        # ★ v0.43：`started is None`（本地复盘案例）时整段跳过，于是
        #   `spec.network_isolated` 保持 `build_spec` 算出的值。对 `ir-*`
        #   分类那是 True（`needs_raw_network` 只放开 pwn/network/web 一类），
        #   也就是 `--network none` —— 复盘一个已被入侵的现场时，容器
        #   能出网只会让"证据"变得不可信（谁都能去网上拉一份别人的日志）。
        if started is not None:
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
                    challenge = db.get(KernelChallenge, challenge_id)
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
            # ★ v0.40：这里不能用 `load_system_prompt()` 了 —— 是否追加
            #   智能体安全评测块取决于"开关 + 本题分类"，而那个判断
            #   在 `build_spec` 时已经算好、存在 `spec.agentsec` 上
            #   （这里没有 db session，也读不到配置）。
            system_prompt=build_system_prompt_by_spec(spec),
            on_progress=_on_progress,
        )

        # ★ 本地复盘案例不处理候选 flag（v0.43）。
        #
        # 那段逻辑的全部意义是"要不要把 flag 提交到平台"，而本地复盘
        # **没有平台也没有 flag**。照旧走一遍的话有两个坏结果：
        # ① safe 模式会把这道题写成 `candidate_ready` —— 那是"等人工
        #    确认要不要提交"的意思，而这里根本没有可提交的东西；
        # ② auto 模式会真的调 `submit_candidate`，拿着空的远端 id 去
        #    请求一个不存在的路径。
        #
        # 跳过之后，`_record_outcome` 会按"没有 correct 提交且没有 error"
        # 把它退回 `unsolved` —— 这正是应急响应复盘该有的终态：复盘做完了，
        # 产出在题解与工单文件里，而这道题本身没有"解出"这回事。
        if platform_backed:
            await _handle_candidates(challenge_id, spec, outcome)
    except KernelEnvironmentTimeout as exc:
        outcome.error = exc.message
        with session_scope() as db:
            challenge = db.get(KernelChallenge, challenge_id)
            if challenge is not None:
                challenge.status = KernelChallengeStatus.FAILED.value
                challenge.last_error = _safe_text(exc.message)
        logger.warning("CTF2 环境启动超时：%s（%d 次轮询）", challenge_id, exc.polls)
    except KernelError as exc:
        outcome.error = exc.message
        with session_scope() as db:
            challenge = db.get(KernelChallenge, challenge_id)
            if challenge is not None and challenge.status not in (
                KernelChallengeStatus.CANDIDATE_READY.value,
                KernelChallengeStatus.SOLVED.value,
            ):
                challenge.status = KernelChallengeStatus.FAILED.value
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
        #
        # ★ v0.43：本地复盘案例没有平台环境可释放，`client` 也是 None。
        #   不跳过的话这一行会把 `None` 传进 `release_environment`，
        #   而它内部是 `client.stop_environment(...)` —— 那是一个
        #   `AttributeError`，且发生在 `finally` 里，会**盖住上面刚记下的
        #   真实失败原因**。所以这个判断不是优化，是必须的。
        if platform_backed and client is not None:
            await ctf2_service.release_environment(
                client, challenge_id, mark_failed=False
            )

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
        # ★ v0.43：本地复盘案例没有 client（见上面 `client = None` 的说明）。
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - 见上：收尾绝不抛
                logger.debug("关闭 CTF2 共享 client 时出错（忽略）", exc_info=True)

    # ── ③ 同步段：落库 ────────────────────────────────────
    with session_scope() as db:
        challenge = db.get(KernelChallenge, challenge_id)
        if challenge is not None:
            _record_outcome(db, challenge, outcome, platform_backed=platform_backed)
            # ★ v0.45：生产遥测（回合数 / 工具调用数 / 方法卡张数）。
            #
            # ⚠️ 位置在 `_record_outcome` **之后**：它要写 `solved`，而那个
            #    值来自 `challenge.status` —— 上面那句刚定完。放前面会让
            #    一次成功的解题被记成 `solved=False`，于是"解出率"这项
            #    统计从此偏低，而偏低不会被任何人发现。
            #
            # ⚠️ 它自己吞异常（内部 try/except + warning）：遥测是**附加
            #    产物**，失败不该把一次成功的解题变成报错 —— 与
            #    `record_solve_usages` / `learn_from_solve` 同一条纪律。
            ctf2_service.record_solve_telemetry(
                db, attempt_id=attempt_id, challenge=challenge, outcome=outcome
            )
            # ★ v0.36：token 用量与题解。
            #
            # ⚠️ 顺序在 `_record_outcome` **之后** —— 题解要写
            #    "这道题解没解出"，而那个状态正是 `_record_outcome`
            #    刚刚定下的。放前面会把一道刚解出的题写成"未解出"。
            #
            # ⚠️ 两者都**自己吞异常**（内部 try/except + warning）：
            #    统计与题解是**附加产物**，失败不该把一次成功的解题
            #    变成报错 —— 与 `learn_from_solve` 同一条纪律。
            # ⚠️ 耗时从**借的壳 scan** 上取（`Scan.duration_sec`）——
            #    `KernelChallenge` 自己没有耗时列。
            #
            # ⚠️⚠️ **必须在 `finalize_solve_scan` 之后读**：`duration_sec`
            #     正是那个函数在收尾时算出来写上去的（见它的实现）。
            #     放在它之前读，永远拿到 `None`，而题解会写"未记录"——
            #     看起来像"没测到"，实际是**读的时机错了**。
            ctf2_service.record_solve_usages(
                db, attempt_id=attempt_id, challenge_id=challenge_id, outcome=outcome
            )
            try:
                ctf2_events.finalize_solve_scan(db, challenge)
            except Exception:  # noqa: BLE001 - 收尾失败不该盖住解题结果
                logger.warning(
                    "CTF2 解题 scan 收尾失败（不影响解题结果）：%s",
                    challenge_id,
                    exc_info=True,
                )
            # ★ 题解放在最后：它要读 `duration_sec`（上一句刚写的），
            #   而那个值只有收尾之后才存在。
            ctf2_service.save_writeup(
                db,
                challenge=challenge,
                outcome=outcome,
                attempt_id=attempt_id,
                elapsed_sec=_solve_elapsed_seconds(db, challenge),
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

    以前签名里有个 `client: KernelClient`，并且被塞进 `client_factory`
    交给 `submit_candidate` —— 而那个函数会对它 `async with`（即 aclose），
    等于**替调用方关掉共享资源**。实测后果：提交成功后收尾阶段
    `release_environment` 复用同一个 client 直接炸
    （`RuntimeError: Cannot send a request, as the client has been closed.`），
    而那次提交**其实是成功的**。

    现在每次提交都新建客户端，共享 client 的生命周期只归 `_run_worker`。
    参数随之删掉 —— 留着它只会让"谁负责关"继续含混。
    """
    from app.kernel import service as ctf2_service

    if not outcome.candidates:
        return

    for candidate in outcome.candidates:
        # 同步段：读状态 + 决定 + （safe 时）落候选
        with session_scope() as db:
            challenge = db.get(KernelChallenge, challenge_id)
            if challenge is None:
                return
            if challenge.status not in (
                KernelChallengeStatus.SOLVING.value,
                KernelChallengeStatus.ENVIRONMENT_READY.value,
                KernelChallengeStatus.CANDIDATE_READY.value,
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
                # ★ v0.42：第三道闸 —— 答案必须落在真实工具输出里。
                #   传的是**完整**输出（`outcome.tool_outputs`），不是
                #   `transcript` 里被 `_clip` 过的版本。
                #   见 `answer_is_grounded` 与 `SolveOutcome.tool_outputs`。
                flag=candidate.flag,
                tool_outputs=outcome.tool_outputs,
            )
            challenge.candidate_count = int(challenge.candidate_count or 0) + 1
            if decision.action == "hold":
                # ★ safe 模式的落点：候选**落库但不提交**。
                #   写的是 `candidate_flag` 而不是 `confirmed_flag` ——
                #   这两列的区别正是"它猜的"与"平台认的"（见 models）。
                challenge.candidate_flag = candidate.flag
                challenge.status = KernelChallengeStatus.CANDIDATE_READY.value
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
            # 而这个 `RuntimeError` 不是 `KernelError`，穿透了收尾函数的
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
        if status_value == KernelSubmissionStatus.CORRECT.value:
            outcome.submitted.append(candidate.flag)
            with session_scope() as db:
                challenge = db.get(KernelChallenge, challenge_id)
                if challenge is not None:
                    ctf2_events.emit_submitted_ok(db, challenge, flag=candidate.flag)
                db.commit()
        elif status_value == KernelSubmissionStatus.WRONG.value:
            outcome.rejected.append(f"{candidate.flag}：平台判错（{message}）")
            with session_scope() as db:
                challenge = db.get(KernelChallenge, challenge_id)
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
    from app.kernel.models import KernelCompetition

    with session_scope() as db:
        challenge = db.get(KernelChallenge, challenge_id)
        if challenge is None:
            return {}
        competition = db.get(KernelCompetition, challenge.competition_id)
        return {
            "competition_remote_id": competition.remote_id if competition else "",
            "remote_id": challenge.remote_id,
            "sub_flag_id": challenge.sub_flag_id,
        }


def _solve_elapsed_seconds(db: Session, challenge: KernelChallenge) -> int | None:
    """这次解题跑了多久（秒）。取不到返回 `None`。

    ## 为什么从**借的壳 scan** 上取

    `KernelChallenge` 没有耗时列；而 CTF2 借的那条 scan
    （`events.ensure_solve_scan` 建的）有 `duration_sec`，
    由 `finalize_solve_scan` 在收尾时算出。

    ## ★ 为什么返回 `None` 而不是 0

    两者语义不同：`None` = **没测到**（收尾没跑、scan 被删），
    0 = 真的 0 秒。题解里前者写"未记录"、后者写"0 秒" ——
    编一个 0 会让"未记录"伪装成一个具体读数。

    ⚠️ 调用点必须在 `finalize_solve_scan` **之后** —— 那个值由它写入。
    """
    from app.scans.models import Scan

    if not challenge.scan_id:
        return None
    scan = db.get(Scan, challenge.scan_id)
    if scan is None or scan.duration_sec is None:
        return None
    return int(scan.duration_sec)


def _record_outcome(
    db: Session,
    challenge: KernelChallenge,
    outcome: SolveOutcome,
    *,
    platform_backed: bool = True,
) -> None:
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

    ## ★ `platform_backed=False`：本地复盘案例的收尾口径（v0.43）

    应急响应复盘**没有 flag**，所以"本次未产出候选 flag。"那句话对它
    是错的 —— 它会出现在一道刚刚正常完成复盘的题的 `lastError` 上，
    而复盘的全部价值在题解与工单文件里。用户看到那句话会以为复盘失败了。

    这个标志位不改变状态机的走向（仍然是"没解出、可重跑"），只换
    `last_error` 的措辞。分叉刻意做得**这么小**：状态机的语义对两种任务
    是一样的（这是同一套流水线），错的只是"失败原因"该说什么。
    """
    solved = (
        db.execute(
            select(KernelSubmission.flag).where(
                KernelSubmission.challenge_id == challenge.id,
                KernelSubmission.status == KernelSubmissionStatus.CORRECT.value,
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
        challenge.status = KernelChallengeStatus.SOLVED.value
        challenge.confirmed_flag = challenge.confirmed_flag or solved
        challenge.last_error = None
        return

    if outcome.error and challenge.status not in (
        KernelChallengeStatus.CANDIDATE_READY.value,
        KernelChallengeStatus.SOLVED.value,
    ):
        challenge.status = KernelChallengeStatus.FAILED.value
        challenge.last_error = _safe_text(outcome.error)
        ctf2_events.emit_abandoned(db, challenge, reason=outcome.error)
    elif challenge.status not in (
        KernelChallengeStatus.CANDIDATE_READY.value,
        KernelChallengeStatus.SOLVED.value,
        KernelChallengeStatus.FAILED.value,
        KernelChallengeStatus.SUBMITTING.value,
    ):
        # 没出错、也没产出候选 —— 回到"未解出"，让人可以再点一次。
        #
        # ★★ 判据从 `== SOLVING` 改成"不在终态/候选态里"（2026-10-10 修）。
        #
        # 原来写的是 `elif challenge.status == KernelChallengeStatus.SOLVING.value`，
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
        challenge.status = KernelChallengeStatus.UNSOLVED.value
        # ★ v0.43：措辞按任务类型分叉。
        #
        # 本地复盘案例没有 flag，所以"未产出候选 flag"是一句**错话** ——
        # 它会挂在一道刚刚正常跑完的复盘题上，让人以为复盘失败了。而
        # 复盘有没有价值，看的是题解与工单文件，不是 flag。
        #
        # 状态仍然回到 `unsolved`（可重跑），这一点两种任务一致 ——
        # 改的只是那句话该说什么。
        challenge.last_error = (
            "本次未产出候选 flag。"
            if platform_backed
            else "本次复盘未完成，未产出结论。"
        )

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
        from app.kernel import service as ctf2_service

        ctf2_service.learn_from_solve(db, challenge, outcome)
    except Exception:  # noqa: BLE001 - 提炼知识失败不该影响解题结果
        logger.warning("写解题方法卡失败（不影响解题结果）", exc_info=True)


__all__ = [
    "ATTACHMENT_DIR",
    "Candidate",
    "CommandResult",
    "KernelSandboxError",
    "DEFAULT_MAX_TURNS",
    "FINAL_STRETCH_TURNS",
    "MAX_DIGEST_LINES",
    "FLAG_PATTERN",
    "MAX_ANSWER_LENGTH",
    "AnswerFormatError",
    "FlagFormatError",
    "IMAGE_BASE",
    "IMAGE_REVERSE",
    "IMAGE_IR",
    "SANDBOX_NAME_PREFIX",
    "sweep_orphan_sandboxes",
    "ensure_ctf2_network",
    "LlmGateway",
    "LlmReply",
    "SandboxContainer",
    "SolveOutcome",
    "SolveSpec",
    "HttpHistory",
    "HttpRecord",
    "SubmitDecision",
    "TOOL_NAMES",
    "TokenUsage",
    "parse_usage",
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
    "extract_final_analysis",
    "load_system_prompt",
    "load_agentsec_prompt",
    "load_ir_prompt",
    "load_root_prompt",
    "load_child_prompt",
    "build_multi_agent_prompts",
    "load_offensive_playbook",
    "build_system_prompt",
    "build_system_prompt_by_spec",
    "PROMPT_AGENTSEC_PATH",
    "needs_raw_network",
    "normalize_flag",
    "answer_is_grounded",
    "parse_action",
    "parse_actions",
    "parse_final",
    "profile_for_category",
    "run_worker",
    "solve_in_sandbox",
    "submit_candidate",
    "validate_flag",
    "validate_answer",
]
