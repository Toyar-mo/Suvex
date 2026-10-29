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
| 本轮**已经跑过工具** | 任务进行中，"没动作就收尾"几乎总是没做完 | 推 |
| 正文里有"要动手"的意图词 | 任务刚开头的一句打算 | 推（补充判据） |
| 以上都不是 | 纯问答（没跑过工具、很短、没有意图词） | 不推 |

关键的那一条是「本轮已经跑过工具」。它把**任务型轮次**和**问答型轮次**分开：
用户问「你能做什么」，整轮零工具调用，答完就结束，不该被推；而一旦这一轮
真的动过工具，突然"只写一句人话"就收尾，绝大多数是没做完。这不需要理解中文，
只看这一轮有没有跑过工具。

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

#: 正文长度上界。意图词是弱信号：600 字的完整结论里完全可能顺带一句
#: 「我先看了一下」，那不是"停在半路"。真正的形态是**短短一句打算**。
MAX_UNFINISHED_CHARS = 200


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
    return bool(_INTENT.search(body))


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
_PLAN_START = re.compile(
    r"(?:^|[。；！？\n])\s*(?:先|现在|接下来|下一步|然后|下面|接着|待会|稍后)"
)
_PLAN_VERB = re.compile(
    r"(?:现在|先|接下来|下一步|然后|接着|待会)\s*"
    r"(?:查|看|试|跑|去|来|要|开始|执行|写|扫|读|改|装|下|测|确认|分析|拿|抓|起)"
)


def looks_like_plan(text: str) -> bool:
    """这句 `FINAL:` 的内容是不是「接下来要做什么」（v1.29）。

    只判**长短 + 形态**，不理解语义：短句（<200 字）且命中计划词才算。
    长正文里顺带一句「然后我看了下」不算计划 —— 那通常是真的在讲过程。
    """
    body = text.strip()
    if not body or len(body) >= MAX_UNFINISHED_CHARS:
        return False
    return bool(_PLAN_START.search(body) or _PLAN_VERB.search(body))


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
        # ★ v1.25：仍然"跑过工具就推"（防"输出一次就停"的安全网不能松），
        #   但**重复气泡的问题改在引擎层解决**：被推掉的那句如果是提前给出的
        #   结论，就地降级成 `process`（见 `engine` 的 nudge 分支）。
        #
        #   为什么不在这里加"像不像半句"的判据：实测「`pwd` 输出是 `/work`。」
        #   这种 18 字的**完整**回答，任何长度/标点启发式都会误判；而误判成
        #   "说完了"就等于放行一次半路停 —— 那是更贵的错。
        return tool_nudges_used < max_tool_nudges
    if nudges_used >= max_nudges:
        return False
    return looks_unfinished(text)
