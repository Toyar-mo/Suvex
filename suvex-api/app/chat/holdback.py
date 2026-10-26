"""暂扣缓冲：让正文「逐字长出来」，同时保证内部协议一个字都不漏给用户。

## 这个模块解决的是什么

v0.102 之前，对话的正文是**整块**出现的：模型在服务端想完、落库、再推一帧
全文，界面上那块文字在一瞬间铺开。用户的原话是「消息是一瞬间蹦出一大块」。

调研三个同类项目（LibreChat / Open WebUI / LobeChat）之后的结论推翻了
我最初的判断：**它们不是靠「入场动画」解决这个问题的**。Open WebUI 的
`ResponseMessage.svelte` 里唯一的 `in:fade` 用在后继建议 chips 上，消息行
本身零入场动画；正文之所以「逐字出现」，是因为它本来就是**逐 token 长出来的**。
给我方消息行加一个 240ms 的动画只能掩盖症状 —— 动画结束后那块文字仍然
是同时出现在屏幕上的。

真正的修法是让 `LlmGateway` 也做 token 流。但直接推原文有一个硬问题。

## ★ 为什么不能直接把 token 推给界面

模型的一轮输出长这样：

    好的，我先查一下最近的扫描。

    ACTION: {"tool": "list_scans", "args": {}}

而**只有 `ACTION:` 之前那段**是该给用户看的。把后半段落进界面的后果，
`engine.py` 的 `_narration_before_action` docstring 里记着我实测撞到的样子：

    assistant | '好的，我先查一下最近的扫描。\nACTION: {"tool": "list_scans", ...}'

用户看到的是一行裸 JSON，无法判断这句话是模型说的还是系统拼的。
非流式时代这个问题好解决 —— 整段拿到手，`find("ACTION:")` 一刀切掉。
**流式时切不了**：`ACTION:` 可能在第 80 个 token 才出现，那时前 79 个
token 已经推到屏幕上了。

## 做法：确认安全才放行

本模块是一个**行缓冲**。规则三条：

1. 完整的一行才判断。半个单词不做决定 —— 否则 "ACT" 放出去、"ION:" 到
   下一块才来，已经被看见了。
2. 一行**不是协议起头**（不以 `ACTION:` / `FINAL:` 开头）才整行放行。
3. 一旦见到协议起头，**停止放行再也不放** —— 后面的都是给机器看的。

第 3 条的「再也不放」是刻意的：模型有时先写一段正文、再写 ACTION、
然后又写一段解释。按协议，见到 ACTION 之后的一切都不该进用户视野。

## 为什么按行而不是按句

按句要处理中文标点（。！？）与英文句点的歧义（"0.5" 里的点、
"e.g." 里的点），而那套启发式会在缩写上出错 —— 出错的表现是**把一句
话从中间切断**。按行简单、可预测，而模型的正文本来就分行写。

## 代价：首个字出现的延迟

第一行要等模型把它写完（或写完一段没有换行就结束）。实测这一段通常在
几百毫秒量级，而改造前要等**整轮**（含全部工具往返）—— 也就是几秒到
几十秒。所以延迟是显著变短，不是变长。

## ⚠️ 与 `_narration_before_action` 的关系

两者不重复，也不是二选一：

| | 用途 | 作用面 |
| --- | --- | --- |
| `_narration_before_action` | 决定**落库**什么 | 整段、一次 |
| 本模块 | 决定**实时推给界面**什么 | 逐行、流式 |

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


class HoldbackBuffer:
    """行缓冲：`feed()` 进增量，`drain()` 出「确认安全」的前缀。

    ## 用法

        buf = HoldbackBuffer()
        async for delta in gateway.chat_stream(...):
            for safe in buf.feed(delta):
                await emit(EVENT_DELTA, {"text": safe})
        for safe in buf.flush():
            await emit(EVENT_DELTA, {"text": safe})

    ## ★ 状态是「见过协议没有」，不是「当前行是不是协议」

    见模块 docstring 第 3 条。一旦 `blocked`，`feed()` 再多的正文也不放行。
    """

    __slots__ = ("_pending", "_blocked")

    def __init__(self) -> None:
        #: 尚未构成完整一行（或尚未确认安全）的尾巴。
        self._pending = ""
        #: 见过协议起头之后就永远 True。
        self._blocked = False

    @property
    def blocked(self) -> bool:
        """是否已经见过协议起头（此后不再放行任何字）。"""
        return self._blocked

    def feed(self, delta: str) -> str:
        """吃一个增量，返回**可以立刻推到界面**的那部分（可能是空串）。

        空串是常态，不是错误：一个 token 通常只是一两个字符，凑不齐一行。
        """
        if not delta:
            return ""
        if self._blocked:
            # 见过协议之后的一切都是给机器看的。⚠️ 仍然要消费掉这个 delta
            # （而不是提前 return 不理它）—— 这里没有别的状态要维护，
            # 但保持「feed 吃下所有输入」的语义，将来加统计不会漏。
            return ""
        self._pending += delta
        out: list[str] = []
        while True:
            newline = self._pending.find("\n")
            if newline < 0:
                # ── 没有换行 = 这一行还没写完。不能放行，但要先判一次
                #    「已经开始写协议了」。理由：模型常把协议与正文写在
                #    同一行（`好的。ACTION: {...}`），那种形态下**永远
                #    等不到换行**，直到整轮结束 —— 于是 `flush()` 才判。
                #    只靠 flush 的坏处是：这一行里的正文（"好的。"）也
                #    被一起扣住了，而它其实是安全的。
                #
                #    所以这里提前判一次：**只要 pending 里已经出现协议头，
                #    就把协议之前那部分正文放出去，然后锁死**。
                head = self._protocol_head_index(self._pending)
                if head is not None:
                    prefix = self._pending[:head]
                    if prefix:
                        out.append(prefix)
                    self._pending = ""
                    self._blocked = True
                    break
                break
            line = self._pending[: newline + 1]
            self._pending = self._pending[newline + 1 :]
            if _is_protocol_head(line):
                # ★ 这一行本身也不放行：`ACTION: {...}` 整行都是协议。
                self._blocked = True
                self._pending = ""
                break
            out.append(line)
        return "".join(out)

    @staticmethod
    def _protocol_head_index(text: str) -> int | None:
        """`text` 里第一个**协议行的起点**下标（行首），没有就返回 None。

        ## 为什么不能简单地 `text.find("ACTION:")`

        正文里完全可能出现这个词："我看到一段 ACTION: 开头的日志"。
        那不是协议（它不在行首），不该被当成协议 —— 判错的表现是
        **把用户正常的一句话从中间切断**。

        所以按行扫，每行用 `_is_protocol_head` 判（那个函数要求**行首**
        紧邻标记，允许前导空白）。
        """
        offset = 0
        for line in text.splitlines(keepends=True):
            if _is_protocol_head(line):
                return offset
            offset += len(line)
        return None

    def flush(self) -> str:
        """收尾：把最后一段没有换行的尾巴按同样规则判一次。

        流结束时 `_pending` 里可能留着正文的最后一行（模型没写结尾换行）。
        不 flush 的话那句话会被吞掉 —— 界面上表现为「最后一句话少了一半」，
        而那是最容易被当成渲染 bug 的形态。
        """
        if self._blocked or not self._pending:
            self._pending = ""
            return ""
        tail = self._pending
        self._pending = ""
        if _is_protocol_head(tail):
            self._blocked = True
            return ""
        return tail


__all__ = ["PROTOCOL_MARKERS", "HoldbackBuffer"]
