"""纠偏判据：这一轮"只叙述、没动作"的回复，要不要推它一下（v1.19）。

## 为什么从 `engine.py` 里搬出来

原来这套判据写在引擎主循环里，靠一张**中文意图词白名单**（`_NARRATION_INTENT`）
决定推不推。它漏了最常见的形态 —— 实测线上真的漏了：

    正文：「先把二进制拆出来看防护和行为，同时把服务交互读完。」（83 字）
    词表：`先(看|判|查|跑|扫|试|起|摸|确|列|读|拉|执行)` —— `先` 后面跟的是「把」，
    不在表里 → 判定"说完了" → 整轮结束，界面停在这半句话上。

用户看到的就是「对话又没反应」。**词表是天生的漏勺**：中文里"要动手"的说法
无穷无尽（先把/先来/先搞定/我先跑一下/这就去…），补词表永远补不完，而每漏
一个就表现为"卡住"。所以这里不再靠词表做主判据。

## 现在的主判据是**结构**，不是措辞

按证据强度从高到低：

| 信号 | 含义 | 推不推 |
|---|---|---|
| `at_limit` | 已到工具上限，这句总结就是我们要的结局 | 不推 |
| 写了 `FINAL:` | 模型**明确宣告**说完了 | 不推 |
| 有 ACTION | 正常，去执行 | 不推 |
| `truncated` | 被输出上限切断，话没说完 | 推 |
| 跑过工具且正文里有继续意图/计划/未执行命令 | 任务明显还没交付 | 推 |
| 跑过工具但正文是完整回答 | 结构上已收尾 | 不推 |
| 正文里有"要动手"的意图词 | 任务刚开头的一句打算 | 推（补充判据） |
| 以上都不是 | 纯问答（没跑过工具、很短、没有意图词） | 不推 |

关键判据是「跑过工具 **且** 正文仍有继续结构」。只看「跑过工具」会把已经
给出完整结论的轮次反复打回；只看正文措辞又会漏掉计划句。两者同时成立才推。

## `FINAL:` 为什么要有

"任务进行中"那条会误伤**真的做完了**的轮次（「已解出 flag{…}」）。所以给模型
一个**明确的终止符**：真做完了就写一行 `FINAL: <结论>`。有了它，"没有 FINAL
也没有 ACTION"就只剩"忘了写"一种解释，不再需要猜。

`FINAL:` 只在**存在歧义的地方**（任务型轮次）才要求；纯问答不受影响，所以不会
让每一次普通回答都多花一次模型调用 —— 那是上一版判据最怕的代价。
"""

from __future__ import annotations

import re

#: 模型显式宣告"说完了"的标记。与内核侧的 `FINAL:` **同一个词**（见
#: `app/kernel/runner.py` 的 `_MARKER = re.compile(r"^\\s*(ACTION|FINAL)\\s*[:：]")`），
#: 这样两套提示词对模型是同一句话，不会出现"对话里叫 FINAL、解题里叫 END"。
#:
#: 全角冒号也要认：模型输出中文时经常写成 `FINAL：`。
_FINAL = re.compile(r"^\s*FINAL\s*[:：]\s*", re.IGNORECASE)

#: 「只叙述、没动作」的正文里出现这些词，说明它**打算接着动手**（补充判据）。
#:
#: 保留它是因为有一类轮次结构上看不出来：任务刚开头（还没跑过任何工具）、
#: 模型先说一句"我先看一下环境"，然后忘了写 ACTION。`truncated` 和
#: `tools_used` 都抓不到这种情况。
#:
#: ⚠️ 它只是**补充**，不是主判据 —— 主判据是 `tools_used`。补词表救不了
#:   漏词的问题，但可以少漏几次，代价只是一次多花的模型调用。
_INTENT = re.compile(
    r"(我(先|这就|马上|现在)|先(看|判|查|跑|扫|试|起|摸|确|列|读|拉|执行|把|来|去)"
    r"|接下来|下一步|下面我|让我|我来(看|查|跑|试|扫))"
)

#: 英文版"打算接着动手"的短语（v1.55）。
#:
#: ## 为什么要有它
#:
#: 线上真实复读（`chat_01M6BWQ69MZJNBAB7C7QMVXPV2`，用户报"一直循环"）：
#:
#:     Let me continue.
#:     I need to check the state.
#:     Let me continue. I need to check the state.
#:
#: 三句加起来不到 200 字（没触发长度闸），但全是英文 —— `_INTENT` 只认中文，
#: 于是 `looks_unfinished=False`，而这一轮又 `tools_used==0`（还没跑过工具），
#: 结果既不走纠偏、也不走工具分支，整轮 `done`，界面停在"让我继续"上。
#:
#: ⚠️ 与中文 `_INTENT` 一样是**弱信号**，仍受 `MAX_UNFINISHED_CHARS` 长度闸约束：
#:   完整的长英文结论（>200 字）里顺带一句 "Let me explain" 不会被打回。
_EN_INTENT = re.compile(
    r"\b(let me|let us|let's|i'll|i will|i need to|i'm going to|i am going to|"
    r"now i|next i|next,|first,? i|first let me)\b",
    re.IGNORECASE,
)


#: 正文长度上界。意图词是弱信号：600 字的完整结论里完全可能顺带一句
#: 「我先看了一下」，那不是"停在半路"。真正的形态是**短短一句打算**。
MAX_UNFINISHED_CHARS = 200

#: 围栏代码块（``` ... ```）。v1.50：模型把 shell 命令**写进代码块正文**、
#: 却不写 `ACTION:` 时，那段命令是它\"想执行\"的强信号 —— 与长度无关。
_FENCED_BLOCK = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)

#: 一眼看着像 shell 命令的行（代码块里出现一行即可判定）。
#: 只认命令式的开头词，避免把普通英文/示例文本误判成\"要执行\"。
_SHELL_LINE = re.compile(
    r"(?:^|\n)\s*(?:"
    r"cd|ls|cat|grep|find|python3?|pip3?|bash|sh|curl|wget|apt-get|apt|"
    r"chmod|chown|mkdir|rm|cp|mv|tar|unzip|objdump|readelf|nm|strings|file|"
    r"xxd|hexdump|base64|echo|sed|awk|nc|ncat|socat|openssl|docker|git|"
    r"\./|/bin/|/usr/"
    r")\b"
)


def has_unactioned_command(text: str) -> bool:
    """正文里写了（围栏代码块中的）shell 命令，但没有 `ACTION:`（v1.50）。

    ## 为什么需要它

    线上实测（会话 `chat_01M6BWQ69MZJNBAB7C7QMVXPV2`，seq 660）：模型说
    「我现在做一件从未检查过的事：对全部图像做 LSB 隐写提取」，然后**把
    shell 命令原样贴进代码块**，没写 `ACTION:`。整轮 `chat.round` 记到
    `actions=0 / tools=0 / text_len=666`：

    - `looks_like_plan` → False（没有「先/现在/接下来」这类标记落在收尾三句窗口）
    - `looks_unfinished` → False（正文 ≥ `MAX_UNFINISHED_CHARS`=200，被判成\"长结论\"）
    - `tools_used == 0` → 那条\"跑过工具就推\"的分支够不着

    三条判据全部漏掉 → `should_nudge=False`、`nudges=0` → 直接 `giveup
    no_action`，整轮 `done`，界面停在「继续」上。**这就是用户说的\"还是会停\"。**

    但是\"正文里有一段 shell 命令、且没走 ACTION 协议\"是**极强**的\"想执行\"
    信号 —— 比任何长度/意图词都硬。把它单独认出来，与长度无关地推一次
    （提醒它\"命令要用 ACTION 协议写\"），就能救回这一类。

    ## 为什么不复用 `looks_unfinished`

    那个函数刻意很窄（短短一句 + 意图词），是给\"问答型半句\"用的。这里
    要认的是另一个形态：\"写了命令但没走协议\"。两者病因与药都不同（见
    模块 docstring 的表），共用一个判据会两头都判不准。
    """
    body = text or ""
    if "ACTION:" in body or "FINAL:" in body:
        # 已经走了文本协议 —— 不是这一类。`_normalize_xml_actions` 之外的
        # 残缺协议另有分支处理，这里不掺和。
        return False
    # 形态一：围栏代码块里的 shell 命令。
    for block in _FENCED_BLOCK.findall(body):
        if _SHELL_LINE.search(block):
            return True
    # 形态二（线上 seq 660 的真实形态）：命令**裸写在正文里**，没有围栏。
    # 用整段匹配 —— `_SHELL_LINE` 锚在行首，正文那行 `cd /work/... && python3`
    # 会命中。为了不把"叙述里提到 cd/curl"之类误判，要求该行是**命令式**
    # （行首就是命令词），而不是出现在句中。
    return bool(_SHELL_LINE.search(body))


#: 结论性/终止性措辞 —— 出现这些就**不要**把尾部的命令当"下一步要跑"去自动执行
#: （v1.51：模型在给出结论时也会贴示例命令，那是解释不是动作）。
_CONCLUSION_CUES = re.compile(
    r"(已解出|拿到了|找到\s*flag|flag\s*[是为:：]|结论[：:]|总结[：:]|"
    r"以上(就)?是|无法|没能|没有(找到|解出)|做完了|全部完成)",
)

#: 尾部意图提示词 —— 正文里说"接下来/现在/下面/我来"，才认为它真的想跑那段命令。
_NEXT_STEP_CUE = re.compile(
    r"(接下来|下面|现在|下一步|然后|接着|我来|我就|让我|这就|开始|执行|跑一下|跑一遍|再跑|试试)",
)

#: v1.51：一段**裸命令块**（用于从正文尾巴里抠出要执行的命令）。
#: 命令块是从某一行开始、到正文末尾（或到空行后的纯叙述）为止的命令式行。
_CMD_LINE_START = re.compile(
    r"^\s*(?:"
    r"cd|ls|cat|grep|find|python3?|pip3?|bash|sh|curl|wget|apt-get|apt|"
    r"chmod|chown|mkdir|rm|cp|mv|tar|unzip|zip|objdump|readelf|nm|strings|file|"
    r"xxd|hexdump|base64|echo|sed|awk|nc|ncat|socat|openssl|docker|git|"
    r"\./|/bin/|/usr/|R=|f=|if\s|for\s|while\s"
    r")"
)

#: 出现这些可疑/破坏性的命令片段就**拒绝**自动执行（宁可让模型自己发 tool_call）。
_DANGEROUS_CMD = re.compile(
    r"(rm\s+-rf?\s+/(?!work|tmp)|mkfs|dd\s+of=/dev/|:\(\)\s*\{|shutdown|reboot|"
    r"curl[^\n]*\|\s*(?:ba)?sh|wget[^\n]*\|\s*(?:ba)?sh)",
)


def extract_trailing_command(text: str) -> str | None:
    """从"说了要干、贴了命令、但没写 ACTION"的正文里，抠出那段命令（v1.51）。

    ## 为什么要这么干

    v1.50 让这类回复被**纠偏一次**——救回了"静默收尾"，但代价是**每两步走一步**：
    实测线上会话 `chat_01M6BWQ69MZJNBAB7C7QMVXPV2` 跑到 `round=33 / tools=16 /
    nudges=17` —— **近一半的模型调用是纯贴命令的轮次，被纠一下、下一轮才用
    原生 tool_call 补执行**。比赛按用时计分，这等于把时间扔了一半。

    既然命令已经在正文里、结构明确（`cd /work ...` / `python3 -c "..."`），
    直接把它变成 `run_shell` 动作就省掉一次完整模型往返。

    ## 为什么要一堆守卫（这是安全边界，不是注释）

    **自动执行模型写的命令**是危险操作，必须极窄：

    1. 没有 `ACTION:` / `FINAL:`（否则走正常协议）；
    2. 命中结论性措辞（`_CONCLUSION_CUES`，含"已解出/没法/结论是"）→ **放弃**，
       那是解释不是动作；
    3. 正文里要有"接下来/现在/执行"之类的下一步提示（`_NEXT_STEP_CUE`）；
    4. 抠出来的命令要含可疑/破坏性片段（`_DANGEROUS_CMD`）→ **放弃**。

    任一条不满足就返回 None，交回 v1.50 的纠偏路径（模型自己用 tool_call 重发）。
    宁可多花一次调用，不可错跑一条命令。
    """
    body = (text or "").strip()
    if not body:
        return None
    if "ACTION:" in body or "FINAL:" in body:
        return None
    if _CONCLUSION_CUES.search(body):
        return None
    if not _NEXT_STEP_CUE.search(body):
        return None
    # 从正文里找"第一行像命令"的位置，从那里开始到结尾整段作为候选命令。
    lines = body.split("\n")
    start = None
    for idx, line in enumerate(lines):
        if _CMD_LINE_START.match(line):
            start = idx
            break
    if start is None:
        return None
    cmd = "\n".join(lines[start:]).strip()
    # 裁剪：遇到连续两个空行之后的纯中文叙述就截断（命令块内部不会有两个空行）。
    if "\n\n" in cmd:
        # 保留到第一个双空行之前
        cmd = cmd.split("\n\n", 1)[0].strip()
    if not cmd:
        return None
    if _DANGEROUS_CMD.search(cmd):
        return None
    return cmd


#: 「同一结论复读」判据（v1.69）。
#:
#: ## 为什么需要它
#:
#: 实测（2026-11-05 会话 chat_01M6P1TAYTASS6P64P85PED8MB「千千的咲.zip解题」）：
#: 模型在一条 CTF 轮里跑了 **12+ 小时 / 429 次 run_shell / 127 轮**，最后反复给出
#: 同一句结论（「没拿到 flag——这题需要动态分析，而本沙箱没有 KVM/虚拟化」），
#: 却每次都在换个算法"再穷尽一下"。因为每步都是**不同**的真实命令，
#: `MAX_REPEATED_ACTIONS`（连续相同命令）永不触发；`MAX_TOOL_NUDGES` 只管纠偏；
#: `TURN_WALL_SECONDS` 单轮 60 分钟后停下，用户一点「继续」又续 60 分钟 ×N。
#:
#: 结果是：一个**已经想清楚、确定做不了**的任务，把算力和用户耐心一起烧光。
#: 正确行为是：当模型**连续多次**给出同一个"结论/阻塞"（近似重复的 answer），
#: 就判定它已经到底了，收尾并把"这是阻塞、不是没试够"讲清楚，别再自动续跑。
_REPEATED_CONCLUSION_SIMILARITY = 0.7
#: 最近多少个 answer 面消息参与比对（够看出"它在复读同一个结论"即可）。
_REPEATED_CONCLUSION_WINDOW = 10


def _fingerprint(text: str) -> set[str]:
    """把一段结论压成"特征集合"用于近似判重（不依赖分词库）。

    取法：去空白/标点后取字符 2-gram 集合。中文没有词边界，2-gram 足够稳，
    且对"同一结论换个说法"有容错。
    """
    norm = "".join(ch for ch in text if ch.isalnum()).lower()
    if len(norm) < 2:
        return set()
    return {norm[i : i + 2] for i in range(len(norm) - 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def is_repeated_conclusion(
    answer_texts: list[str],
    *,
    window: int = _REPEATED_CONCLUSION_WINDOW,
    similarity: float = _REPEATED_CONCLUSION_SIMILARITY,
    threshold: int = 3,
) -> bool:
    """最近若干条 assistant 正文里，是否已**几乎重复**地给出同一句结论 threshold 次及以上。

    `answer_texts`：按时间顺序（旧→新）的 assistant 正文（含 process，见 engine
    `_recent_assistant_texts`）。取最近 `window` 条。

    ## 判据（v1.69 改进：按"成簇"而非"锚最新一条"）
    早期版本只拿**最新一条**当锚，数有几条与它相似 —— 但线上实测（v169 冒烟）
    发现模型会"结论 A → 换句独特推理 → 结论 A → 换句独特推理"地**振荡**：
    最新一条常常是那句独特推理，于是锚点选错、复读漏判。
    改为**两两成簇**：在最近 window 条里找任意一个"簇"，簇内彼此 Jaccard
    ≥ similarity 且簇大小 ≥ threshold，即判复读。这样振荡也抓得住。

    纯函数（不看库/不看时间），便于逐条钉住。
    """
    if len(answer_texts) < threshold:
        return False
    recent = answer_texts[-window:]
    prints = [_fingerprint(t) for t in recent]
    valid = [i for i, fp in enumerate(prints) if fp]
    if len(valid) < threshold:
        return False
    # 以每条为潜在锚，统计与它相似（含自身）的条数；任一条达到 threshold 即成簇。
    for i in valid:
        cluster = sum(1 for j in valid if _jaccard(prints[i], prints[j]) >= similarity)
        if cluster >= threshold:
            return True
    return False


def split_final(raw: str) -> tuple[str, bool]:
    """把回复开头的 `FINAL:` 摘掉，并告诉调用方它有没有写。

    必须在 `_narration_before_action` **之前**调用：那个函数会在 `FINAL`
    标记处截断（它复用了内核的 `ACTION|FINAL` 正则），于是一条只写了
    `FINAL: 解出来了` 的回复会被截成空串，再落成"模型没有产出内容"的占位语。
    """
    matched = _FINAL.match(raw)
    if not matched:
        return raw, False
    return raw[matched.end():], True


def looks_unfinished(text: str) -> bool:
    """正文是不是"短短一句打算"（补充判据，见模块 docstring 的表）。"""
    body = text.strip()
    if not body or len(body) >= MAX_UNFINISHED_CHARS:
        return False
    return bool(_INTENT.search(body) or _EN_INTENT.search(body))


#: 「这句 FINAL 其实是个计划」的判据（v1.29）。
#:
#: ## 为什么要单独一个函数
#:
#: 任务型轮次收到 `FINAL:` 时我们要打回一次确认。但**真结论**也写 `FINAL:`，
#: 一律打回等于每解完一道题都多花一次模型调用。实测里问题只出在一种形态：
#: 模型把「现在查 libc 版本。」「接下来跑一下 libc.rip」这类**下一步计划**
#: 当结论写进了终止符。
#:
#: 判据刻意很窄：句子级起点词 + 紧邻的动词。「查到了，libc 是 2.31」不含
#: 这些形态，所以不会被误打回。
#: 分句边界：计划动词不一定在整条正文的第一句，更常见的是**逗号后面**那半句。
#:
#: 线上原句（`chat_01M64ANA9DQVW1FGWMPEZY6NZD` 第 9 条，2026-10-29 17:19 EDT）：
#:
#:     FINAL: 主逻辑在 0x40070b，看它。
#:
#: 前半句是事实，动作在「，」后面。只按句号/换行切分时「看它」漏掉 →
#: `plan_like_final=False` → 消息落成 `surface='answer'` → 整轮 `done`，
#: 界面就停在「主逻辑在 0x40070b，看它。」上，用户又得手打「继续」。
_CLAUSE_BREAK = r"(?:^|[。；！？\n，、：:])"

_PLAN_START = re.compile(
    _CLAUSE_BREAK
    + r"\s*(?:先|现在|接下来|下一步|然后|下面|接着|待会|稍后|还差|差一步|最后一步)"
)
_PLAN_VERB = re.compile(
    r"(?:现在|先|接下来|下一步|然后|接着|待会)\s*"
    r"(?:查|看|试|跑|去|来|要|开始|执行|写|扫|读|改|装|下|测|确认|分析|拿|抓|起)"
)

#: 句首**直接**是动词的计划句（v1.29.1）。
#:
#: ## 为什么 v1.29 的判据不够
#:
#: 上面两条正则都要求句首有**话语标记**（先/现在/接下来…），且
#: `_PLAN_VERB` 还要求标记与动词紧邻。但实测里模型更常写的是裸动词：
#:
#:     线上会话 `chat_01M63WHGQJHAJ684E5YTBQC0QX` 最后一轮的原句：
#:     `FINAL: stripped、动态链接、只有 read/puts/setbuf，两次输入。看反汇编确认溢出点。`
#:
#: `看反汇编确认溢出点` 前面没有任何标记词，两条正则都不命中 →
#: `premature_final=False` → 这条消息落成 `answer` 气泡 → 整轮 `done`。
#: 用户看到的就是「输出一半就停了」。
#:
#: ## 为什么负向断言 `(?!了|到|出|过|完)`
#:
#: 「看了」「查到了」「跑完了」是**已经发生**的事，属于结论的一部分；
#: 没有这个断言，`看了反汇编，偏移是 0x28。` 会被误打回。
_PLAN_BARE_VERB = re.compile(
    _CLAUSE_BREAK
    # ★ v1.34：`我` 不能挡住裸动词。「我看下反汇编。」在 v1.33 里
    #   因为分句边界后先遇到「我」而漏判 → 落成 answer → 整轮结束。
    + r"\s*(?:我)?\s*"
    r"(?:看|查|试|跑|分析|确认|读|写|扫|执行|找|验证|反汇编|构造|编写|调试|继续|跟进|连接|上传|下载|提交|发送|打)"
    # ★ v1.30.1：`通过/入/即/成/好` 也是"已经发生"或状态描述，不是下一步。
    #   reviewer-7d4f40 实测：`偏移 0x28，验证通过。` 与
    #   `地址在 0x40070b，写入即覆盖返回地址。` 都被误判成计划 ——
    #   于是真结论被多问一轮，用户多等一次模型调用。
    # ★ v1.32：`起` —— 「看起来」是结论式开场（「看起来 flag 在 /flag 里」），
    #   不是下一步动作；没有它，`看` 会被当成裸动词计划。
    # ★ 2026-11-13：`扫描完成：…` 是结论，不是下一步。`扫描` 被拆成
    #   `扫` + `描` 后命中了裸动词计划，导致一份完整汇总被当成计划反复打回。
    r"(?!了|到|出|过|完|通过|入|即|成|好|起|描)"
    # ★ v1.34：`我先把 gadget … 确认下来` 是计划；「把」不在动词表里，
    #   分句边界后又被「我」挡住，所以单独认「（我）先把」。
    + r"|"
    + _CLAUSE_BREAK
    + r"\s*(?:我)?先把"
)

#: 正文里出现这些，说明有**实际交付物**，不该当计划打回。
#:
#: 打回一次的成本是一次模型调用；把真结论打回的代价是用户多等一轮，
#: 而漏判的代价是用户以为解完了、其实停在半路 —— 后者更贵，所以判据
#: 只排除**明确的完成证据**，不做语义理解。
#:
#: ★ v1.30.1：`拿到` / `成功` 不能裸判 —— reviewer-7d4f40 实测
#:   `先看反汇编，拿到偏移后再写 exploit。` 因为句中有「拿到」被放过，
#:   落成 `answer` 后整轮结束。只有**完成态**（已…／…了／…过／成功读出）
#:   才算交付物；「拿到 X 再 Y」是计划。
_RESULT_MARKERS = re.compile(
    r"flag\{|flag："
    r"|已(?:经)?(?:解出|拿到|取得|完成|通过|验证|成功)"
    r"|(?:拿到|得到|解出|读出|取得|打通|getshell)[^，。；！？\n]{0,12}(?:了|过)"
    r"|成功(?:地)?(?:读出|拿到|解出|取得|打通)"
    # ★ v1.32：`跑通了/试通过` 是完成态；「接下来跑通 exploit」没有"了/过"，
    #   仍然是计划。
    r"|(?:跑|试)通(?:了|过)"
    # ★ v1.32：只认**明确交付物**的"如下"（反汇编/输出/结果/代码）。
    #   宽泛的"如下"会漏掉「先看如下命令」这类计划。
    r"|(?:反汇编|输出|结果|代码|分析)如下"
    # ★ v1.32：`flag 是 DASCTF{…}` 是结果；宽泛的"flag 是"会漏掉
    #   「确认 flag 是什么格式」——那句后面没有 `{`。
    r"|flag\s*(?:是|为)\s*[A-Za-z0-9_]{2,32}\{"
    r"|溢出点在|偏移是",
    re.IGNORECASE,
)


#: 计划动作是写给「你/您」的，说明模型在交代用法，不是自己还没做完。
#: 例：`已解出 flag{…}，接下来你可以提交。` 是真结论，不能打回。
_SECOND_PERSON_TRAILING = re.compile(
    r"(?:接下来|下一步|然后|接着|下面|待会|稍后|先|现在)"
    r"[^。；！？\n]{0,6}(?:你|您)"
)

#: 「接下来是结论/下面是总结」是在引出结论，不是下一步动作。
#: reviewer-266f16 实测：`已解出 flag{abc}，接下来是结论：偏移 0xb8。`
#: 被当成计划多打回一轮；它其实是结论。
_CONCLUSION_LEAD = re.compile(
    r"(?:接下来|下面|以下|后面)(?:是|为)?(?:结论|总结|汇总|结果)"
)


#: 长正文只看末尾这么多**非空分句**（v1.35）。
#:
#: 为什么不是"长正文一律不算计划"：实测里模型经常先写一段（>200 字）的
#: 反汇编/防护说明，最后一句才是「接下来写 exploit。」。整条按长度一刀切会
#: 把这种真正的半路停当成结论放过 —— 与 v1.29 修的是同一类线上形态，只是
#: 正文更长。
#:
#: 为什么是 3：计划是**收尾**句，最多再加上一句过渡（「因此」「所以」）。
#: 窗口再大就会把正文中段的「先看过 main」重新算进来，那正是长度判据要
#: 避免的误判。
TRAILING_PLAN_CLAUSES = 3


#: 完成态动词：`看过了`/`跑完了`/`读到了`/`扫过了`。动词后紧跟这些体标记，
#: 说明动作**已经发生**，属于结论的一部分（v1.36）。
#:
#: 为什么需要它：`_PLAN_START` 只认句首话语标记（先/然后/接着/下一步…），
#: **不看后面是不是已完成**。于是「然后我把样本都过了一遍，结论如上。」
#: 这类**完成态**长结论会被判成计划句 → 整轮被打回/标 `needs_continue`，
#: 用户看到「这一轮停在了下一步上」，可它其实已经交付了结论。
#: `_PLAN_BARE_VERB` 早有 `(?!了|到|出|过|完…)` 守卫，`_PLAN_START` 这条
#: 路径却漏了同一个道理。
_COMPLETED_ASPECT = re.compile(
    r"(?:看|查|试|跑|分析|确认|读|写|扫|执行|找|验证|反汇编|构造|编写|调试|"
    r"继续|跟进|连接|上传|下载|提交|发送|打|过|做|核|对|比|拼|解|提|算|记|读)"
    r"(?:了|过|完|到|出|好)"
)


def _clause_is_completed(clause: str) -> bool:
    """分句是不是"已经做完"的陈述（动词 + 了/过/完/到/出/好）。

    用于放松 `_PLAN_START` 的判定：句首虽是「然后/接着/下一步」这类标记，
    但正文其实是完成态时，它是结论，不是计划。
    """
    return bool(_COMPLETED_ASPECT.search(clause))


def _has_trailing_plan(body: str, *, allow_bare_verb: bool = True) -> bool:
    """完成标记附近是否还挂着下一步动作（v1.34）。

    reviewer-4e5f63 的线上级复现：

        FINAL: 已拿到 libc 版本，接下来跑 one_gadget。
        FINAL: 反汇编如下：main 在 0x40070b。接下来写 exploit。
        FINAL: 输出如下：uid=0。接下来读 /flag。

    三句都被 `_RESULT_MARKERS` 命中后直接放过，落成 `answer` 结束。
    正确语义是：完成标记只说明**已经做到哪一步**，同一句里还有
    「接下来…」就说明本轮没做完。

    ★ 不能只看最后一个完成标记的尾巴：计划也可能写在前面，例如
    `接下来写 exploit，已拿到 libc 版本。`。这里按分句扫描，先把
    完成标记剥掉，再判断剩下的部分有没有计划动作。
    """
    for clause in re.split(r"[。；！？\n，、：:]", body):
        if not clause.strip():
            continue
        if _SECOND_PERSON_TRAILING.search(clause):
            continue
        if _CONCLUSION_LEAD.search(clause):
            continue
        cleaned = _RESULT_MARKERS.sub("", clause)
        # ★ v1.36：`_PLAN_VERB` 是"标记 + 紧邻动词"，那个动词后面已有
        #   `(?!了|到|出|过|完…)` 守卫（见 `_PLAN_VERB` 定义）——命中即真计划。
        #   但 `_PLAN_START` 只认句首标记、不看后续，所以「然后我把样本都过
        #   了一遍」「接着我把配置都读完了」这种**完成态**会被误判成计划。
        #   这里补上同一道守卫：命中 `_PLAN_START` 且该分句是完成态时不算计划。
        if _PLAN_VERB.search(cleaned):
            return True
        if _PLAN_START.search(cleaned) and not _clause_is_completed(cleaned):
            return True
        # ★ v1.35：长正文只认带话语标记的计划（见 `TRAILING_PLAN_CLAUSES`）。
        #   裸动词表里「反汇编」「看」这类词在技术性结论里太常见，窗口化之后
        #   误判率反而上升（例如「反汇编里逐条对照栈帧」）。
        if allow_bare_verb and _PLAN_BARE_VERB.search(cleaned):
            return True
    return False


def _tail_clauses(body: str, window: int = TRAILING_PLAN_CLAUSES) -> str:
    """取正文最后 `window` 个非空**句子**（按句末标点切，逗号留在句内）。

    只按逗号切会让「，」把一个长句拆成很多碎片，一个 200 字的结论会只剩
    三个逗号短语；只按句号切又会把"最后一句里逗号后的计划"整句带进来。
    这里按句子切窗口，再交给 `_has_trailing_plan` 按分句扫描 —— 两个粒度
    各自负责一件事。
    """
    sentences = [s for s in re.split(r"[。；！？\n]", body) if s.strip()]
    if not sentences:
        return ""
    return "。".join(sentences[-window:])


_RESULT_BLOCK = re.compile(r"^\s*(?:#+\s*)?结果(?:\s*[:：]|\s|$)")


def looks_like_result(text: str) -> bool:
    """是不是已经按交付格式给出了「结果」块。

    只用于**收尾判定**：结果块里的「未完成/限制」是给用户看的边界，
    不能因此把整条答复再打回成"计划"；否则模型会反复重写同一份结论。
    """
    return bool(_RESULT_BLOCK.match(text or ""))


def looks_like_plan(text: str) -> bool:
    """这句 `FINAL:` 的内容是不是「接下来要做什么」（v1.29）。

    只判**长短 + 形态**，不理解语义：短句（<200 字）命中计划词才算；
    长正文（≥200 字）只看**末尾几个分句**里有没有带话语标记的计划 ——
    正文中段顺带一句「然后我看了下」通常是过程叙述，不是半路停。

    ★ v1.34：完成标记不再无条件放过。`已拿到 libc 版本，接下来跑
    one_gadget。` 前半句是交付物、后半句仍是计划；只看前半句会让整轮
    停在半路。现在改为只看**最后一个完成标记之后**的尾巴。

    ★ v1.35：长正文不再一刀切返回 False。线上形态是「先讲 200 多字的
    保护/反汇编结论，最后一句：接下来写 exploit。」—— 长度判据把它当成了
    真结论，整轮落 `done`。窗口化之后长正文也会打回，但只看收尾那几句。
    """
    body = text.strip()
    if not body:
        return False
    if len(body) >= MAX_UNFINISHED_CHARS:
        # 长正文：只扫收尾窗口，且只认带话语标记的计划（见 `_has_trailing_plan`）。
        return _has_trailing_plan(_tail_clauses(body), allow_bare_verb=False)
    # ★ v1.29.1：有交付物的句子通常不是计划；但 v1.34 起要检查尾巴。
    if _RESULT_MARKERS.search(body):
        return _has_trailing_plan(body)
    return bool(
        _PLAN_START.search(body)
        or _PLAN_VERB.search(body)
        or _PLAN_BARE_VERB.search(body)
    )


def should_nudge(
    *,
    at_limit: bool,
    said_final: bool,
    actions: list[object],
    truncated: bool,
    tools_used: int,
    text: str,
    nudges_used: int,
    max_nudges: int,
    truncations_used: int = 0,
    max_truncations: int = 6,
    tool_nudges_used: int = 0,
    max_tool_nudges: int = 8,
) -> bool:
    """要不要为这一轮"没有动作"的回复再推一次模型。

    纯函数：不碰数据库、不看时间，所以判据本身可以被逐条钉住 ——
    这套判据的失败模式是**静默的**（界面停在半句话上，没有报错），
    只能靠测试守。
    """
    if at_limit or said_final or actions:
        return False
    # ★ v1.21：被输出上限切断**单独记一个预算**，不与「格式错」共用。
    #
    # 实测（线上会话 chat_01M60YNRTZ0Z8S7BF85NNWMRNN 的三条 turn_run 全是
    # `nudges_used=2` 就收尾）：模型在正文里用英文长篇推理，单条回复反复
    # 撞到 `max_tokens`。原来的顺序是「先看格式预算、再看截断」，于是两次
    # 提醒用完之后，**第三次被切断时我们什么都不做**，界面上就停在
    # 「第二次 read 则」这种半句话上 —— 用户说的「解一半又停下」。
    #
    # 切断是**我们的上限**造成的，不是模型没听指令，所以它该有更大的
    # 额度；而"忘了写 ACTION"才是模型的问题，那个额度保持小（2 次）。
    if truncated:
        return truncations_used < max_truncations
    # ★ v1.23：**跑过工具的轮次不受纠偏预算限制**。
    #
    # 实测（线上会话 chat_01M61DWC71W8989RRMW6DZS9SP，33 条）：那一轮的
    # `chat_turn_runs` 是 `nudges_used=2 / truncations_used=0 / tools_used=2`。
    # 也就是说两次纠偏额度**已经被吃掉**（其中一次是前面一条空回复 `seq 18`
    # 长度 0 用掉的 —— 一次模型抽风就耗掉一半预算），于是轮到用户截图这一刻
    # 「刚跑完反汇编、只写了一句 `Let me see the rest of the main function.`」
    # 时，`nudges_used >= max_nudges` 先返回 False，`tools_used > 0` 这条
    # **最硬的结构信号根本没机会生效** —— 界面就停在那句英文上。
    #
    # 判断：纠偏预算是用来防「纯问答被反复推」的成本闸，而不是用来限制
    # 「任务中途不许推进」的。跑过工具的轮次本来就该继续，给它一个独立
    # 且更大的额度（`max_tool_nudges`），花的是钱，省的是用户手打「继续」。
    if tools_used > 0:
        # ★ 2026-11-13：终止只看**结构**。完整回答没有“接下来/我先/下一步”
        # 这类意图，也没有贴未执行的命令，就直接结束 —— 与 OMP/Agents SDK/
        # LangGraph 的“没有 tool call 就是 final”一致。文本 ACTION 协议也不再
        # 靠“跑过工具就推”反复重写同一份结论。
        if not (
            looks_unfinished(text)
            or looks_like_plan(text)
            or has_unactioned_command(text)
        ):
            return False
        return tool_nudges_used < max_tool_nudges
    if has_unactioned_command(text):
        return tool_nudges_used < max_tool_nudges
    if nudges_used >= max_nudges:
        return False
    return looks_unfinished(text)
