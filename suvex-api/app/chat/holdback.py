"""暂扣缓冲：让正文「逐字长出来」，同时保证内部协议一个字都不漏给用户。

## 这个模块解决的是什么

v0.102 之前，对话的正文是**整块**出现的：模型在服务端想完、落库、再推一帧
全文，界面上那块文字在一瞬间铺开。用户的原话是「消息是一瞬间蹦出一大块」。

调研三个同类项目（LibreChat / Open WebUI / LobeChat）之后的结论推翻了
我最初的判断：**它们不是靠「入场动画」解决这个问题的**。Open WebUI 的
`ResponseMessage.svelte` 里唯一的 `in:fade` 用在后继建议 chips 上，消息行
本身零入场动画；正文之所以「逐字出现」，是因为它本来就是**逐 token 长出来的**。

真正的修法是让 `LlmGateway` 也做 token 流。

## ★ 为什么不能直接把 token 推给界面

模型的一轮输出长这样：

    好的，我先查一下最近的扫描。

    ACTION: {"tool": "list_scans", "args": {}}

而**只有 `ACTION:` 之前那段**是该给用户看的。把后半段落进界面的后果，
`engine.py` 的 `_narration_before_action` docstring 里记着我实测撞到的样子：

    assistant | '好的，我先查一下最近的扫描。\nACTION: {"tool": "list_scans", ...}'

用户看到的是一行裸 JSON，无法判断这句话是模型说的还是系统拼的。

## 做法：能判定的立刻放行，判不了的才扣住

本模块的核心是一个**逐行状态机**，每个未完成的行有三种判决：

| 判决 | 条件 | 动作 |
| --- | --- | --- |
| 协议 | 去掉缩进后以 `ACTION`/`FINAL` 开头、紧跟（可空白）`: ` 或 `：` | 锁死，此后一个字都不放 |
| **安全** | 已能证明这行不可能成为协议头 | **整行（含后续增量）立刻放行** |
| 待定 | 还说不准（只写了 `A`、`ACT`、`ACTION`） | 只扣住这几个字符 |

「安全」判定为什么成立：协议头必须**紧贴行首**（`_MARKER` 是 `^\\s*`）。
所以一行只要开头几个字符已经岔开了 —— 即去掉缩进后既不是 `ACTION`/`FINAL`
的前缀，也不是这两个词加空白 —— 后面再写什么都不会让这一行变成协议。
一旦判为安全，**同一行剩余部分逐 token 放行**，不必等换行。

## ★ 为什么这里要改成「逐 token」而不是「逐行」

v0.102 的第一版是纯行缓冲：凑满一整行才放行。它安全，但实测下来
**界面还是一块一块的** —— 用 `_tmp/probe_sse_frames.py` 直接打 SSE 端点，
量到的帧长是 `69, 69, 69, 69, 69`（反复相同），正好等于表格一行的长度。
用户的反馈是「token 流怎么没了」。

粒度问题不是靠改前端能解决的：后端每帧就是那么多字。所以判定要细化到
「能判多少放多少」。

## 为什么按行而不是按句

按句要处理中文标点（。！？）与英文句点的歧义（"0.5" 里的点、
"e.g." 里的点），而那套启发式会在缩写上出错 —— 出错的表现是**把一句
话从中间切断**。按行简单、可预测，而模型的正文本来就分行写。
本模块的「逐 token」指的是**安全行内不再等换行**，不是把行拆成句。

## ⚠️ 与 `_narration_before_action` 的关系

两者不重复，也不是二选一：

| | 用途 | 作用面 |
| --- | --- | --- |
| `_narration_before_action` | 决定**落库**什么 | 整段、一次 |
| 本模块 | 决定**实时推给界面**什么 | 逐行/逐 token |

落库仍然走那个函数（库里的内容必须与非流式时代逐字节一致，否则刷新
页面会和流式时看到的对不上）。本模块只管「中途看到什么」，它放行过
的文本**规范化后**是最终落库文本的**前缀** —— 这条不变式由
`test_holdback.py` 钉住。

⚠️ 「规范化」这一步是必须的，不是偷懒：落库侧会 `.replace("```", "")`，
而本模块做不到 —— 未闭合的 ` ``` ` 只有在流结束后才知道它是不是一个
代码块的开头，而 emit 出去的帧撤不回来。那个差异在渲染上是隐形的
（围栏是 Markdown 的零宽结构标记），但逐字节比较会红。
"""

from __future__ import annotations

#: 协议起头的标记。与 `app/kernel/runner.py` 的
#: `_MARKER = re.compile(r"^\s*(ACTION|FINAL)\s*[:：]", re.MULTILINE)` 同一个集合。
#:
#: ⚠️ 必须包含 `FINAL`：对话页不用它（那是解题侧的收尾协议），但模型偶尔
#: 会照猫画虎写出来。把 `FINAL: {...}` 显示给用户与显示 ACTION 一样糟，
#: 所以这里一并拦掉。两处集合不一致的后果是**某一种协议漏出去**，
#: 而那种漏出只在特定模型上复现。
PROTOCOL_MARKERS: tuple[str, ...] = ("ACTION", "FINAL")

#: 一行未写完时的三种判决（见模块 docstring 的表）。
_PROTOCOL = "protocol"
_SAFE = "safe"
_UNDECIDED = "undecided"


def _is_protocol_head(line: str) -> bool:
    """这一行是不是协议起头（`ACTION:` / `ACTION：` / `FINAL:` ...）。

    ## 为什么要容忍中文全角冒号

    `_MARKER` 的正则里有 `[:：]`——模型确实会写全角冒号（中文语境下的
    自然写法）。判据必须与它一致，否则会出现「界面上放出去了、解析器
    也认了」这种最难查的形态：文字进了用户视野，同时动作真的被执行。

    ## 为什么容忍前导空白

    `_MARKER` 是 `^\\s*`。模型常在协议前空一行或缩进。
    """
    stripped = line.lstrip()
    for marker in PROTOCOL_MARKERS:
        if not stripped.startswith(marker):
            continue
        rest = stripped[len(marker) :].lstrip()
        if rest[:1] in (":", "："):
            return True
    return False


def _verdict(line: str) -> str:
    """判断**尚未写完的一行**属于哪种判决：`_PROTOCOL` / `_SAFE` / `_UNDECIDED`。

    这是「逐 token 放行」的全部依据，所以每一步都要能说清为什么安全。

    ## 判定顺序

    1. 已经是完整协议头（`_is_protocol_head`）→ `_PROTOCOL`。
    2. 去掉缩进后是某个标记的**前缀**（`""`/`A`/`ACT`/`ACTION`/`F`/`FIN`…）
       → `_UNDECIDED`：再写一个冒号它就是协议了，不能放。
       ⚠️ `""`（整行还只有空白）也归这里 —— `  ACTION:` 前面的空格
       不能先放出去，否则界面上会先出现两个空格再出现协议被锁死。
    3. 以某个标记开头但**后面还有别的内容**（`ACTIONABLE`、`ACTIONS`）
       → 不是协议（冒号位置不对），继续看下一个标记。
    4. 以上都不是 → `_SAFE`：这行永远不可能变成协议头，可以整段放行。

    ## 第 2 步为什么带长度条件

    只写 `marker.startswith(stripped)` 的话，`ACTIONABLE 的建议` 会因为
    前 6 个字符正好是 `ACTION` 而被判 `_UNDECIDED`，于是一整行要等到
    换行才放 —— 安全但粒度退化。加上 `len(stripped) <= len(marker)`
    就只在「还没写全」时待定。
    """
    if _is_protocol_head(line):
        return _PROTOCOL
    stripped = line.lstrip()
    for marker in PROTOCOL_MARKERS:
        if len(stripped) <= len(marker) and marker.startswith(stripped):
            return _UNDECIDED
        if stripped.startswith(marker):
            rest = stripped[len(marker) :].lstrip()
            if not rest:
                # `ACTION` / `ACTION ` / `ACTION\n` —— 只差一个冒号。
                return _UNDECIDED
    return _SAFE


class HoldbackBuffer:
    """行状态机：`feed()` 进增量，返回「确认安全」的那部分。

    ## 用法

        buf = HoldbackBuffer()
        async for delta in gateway.chat_stream(...):
            safe = buf.feed(delta)
            if safe:
                await emit(EVENT_DELTA, {"text": safe})
        tail = buf.flush()
        if tail:
            await emit(EVENT_DELTA, {"text": tail})

    ## 两个状态

    - `_blocked`：见过协议头，此后**再也不放**任何字（模块 docstring 第 3 条）。
    - `_line_safe`：当前这一行已判为安全，剩余部分可以逐 token 直接放行，
      直到换行才回到待定。
    """

    __slots__ = ("_pending", "_blocked", "_line_safe")

    def __init__(self) -> None:
        #: 尚未做出的判决所对应的尾巴（通常是当前行的开头几个字符）。
        self._pending = ""
        #: 见过协议起头之后就永远 True。
        self._blocked = False
        #: 当前行已判为安全。
        self._line_safe = False

    @property
    def blocked(self) -> bool:
        """是否已经见过协议起头（此后不再放行任何字）。"""
        return self._blocked

    def feed(self, delta: str) -> str:
        """吃一个增量，返回**可以立刻推到界面**的那部分（可能是空串）。

        返回空串只发生在两种情形：已经锁死，或这一行还只有 `A`/`ACT`
        这样判不定的开头。其余情况立刻返回，不再等换行 —— 这是 v0.103
        修掉「界面一块一块」的关键。
        """
        if not delta:
            return ""
        if self._blocked:
            # 见过协议之后的一切都是给机器看的。仍然消费掉这个 delta，
            # 保持「feed 吃下所有输入」的语义（将来加统计不会漏）。
            return ""
        self._pending += delta
        out: list[str] = []
        while self._pending:
            if self._line_safe:
                # 已判安全：放到换行为止，剩下的留给下一行重新判。
                newline = self._pending.find("\n")
                if newline < 0:
                    out.append(self._pending)
                    self._pending = ""
                    break
                out.append(self._pending[: newline + 1])
                self._pending = self._pending[newline + 1 :]
                self._line_safe = False
                continue

            verdict = _verdict(self._pending)
            if verdict == _PROTOCOL:
                # ★ 这一行本身也不放行：`ACTION: {...}` 整行都是协议。
                self._blocked = True
                self._pending = ""
                break
            if verdict == _SAFE:
                self._line_safe = True
                # 不 break：回到循环顶部按安全行的规则放行。
                continue
            # _UNDECIDED：只扣住这一小段，等更多增量。
            break
        return "".join(out)

    def flush(self) -> str:
        """收尾：把最后一段没有换行的尾巴按同样规则判一次。

        流结束时 `_pending` 里可能留着正文的最后一行（模型没写结尾换行）。
        不 flush 的话那句话会被吞掉 —— 界面上表现为「最后一句话少了一半」，
        而那是最容易被当成渲染 bug 的形态。

        `_UNDECIDED` 在这里按**放行**处理：能走到 flush 就说明这行到头了，
        而 `ACTION`（没有冒号）、`ACT` 这类文本本身不是协议头，显示它不
        泄漏任何给机器看的东西。旧实现也是这个口径（`_is_protocol_head`
        判否即放行）。
        """
        if self._blocked:
            self._pending = ""
            return ""
        tail = self._pending
        self._pending = ""
        if self._line_safe:
            return tail
        if _verdict(tail) == _PROTOCOL:
            self._blocked = True
            return ""
        return tail


__all__ = ["PROTOCOL_MARKERS", "HoldbackBuffer"]
