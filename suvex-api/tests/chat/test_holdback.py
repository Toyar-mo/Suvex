"""暂扣缓冲与流式增量解析（v0.102）。

## 这个文件为什么存在

`app/chat/holdback.py` 决定「界面上能看到什么」。它的失败模式是**静默**的：
放早了 → 内部协议 `ACTION: {...}` 出现在用户视野里（`engine.py:715` 记录过的
真实事故形态）；放晚了 → 用户那句话少一截，而那看起来像渲染 bug。

两种都不抛异常、都不进日志。所以只能靠这里的断言钉住。

## 钉住的核心不变式

**放行过的文本，规范化后必须是最终落库正文的前缀。**

不是"相等"：未闭合的代码围栏只有在流结束后才知道配对关系，而 emit 出去
的帧撤不回来，所以围栏这一层两边本来就无法逐字节一致（详见
`test_holdback_matches_narration_for_real_shapes` 的 docstring）。

这条不变式被 `test_holdback_matches_narration_for_real_shapes` 用真实的
`_narration_before_action` 交叉验证 —— 两边各写一套切分逻辑，必须把
同一段输入切成同一个开头。
"""

from __future__ import annotations

import pytest

from app.chat.holdback import PROTOCOL_MARKERS, HoldbackBuffer


def _run(chunks: list[str]) -> tuple[str, bool]:
    """把一串增量喂进缓冲，返回 `(放行全文, 是否锁死)`。"""
    buffer = HoldbackBuffer()
    out: list[str] = []
    for chunk in chunks:
        out.append(buffer.feed(chunk))
    out.append(buffer.flush())
    return "".join(out), buffer.blocked


def _normalize(text: str) -> str:
    """把围栏与首尾空白去掉，用来比较「说的是不是同一句话」。

    ## 为什么比较前要规范化

    落库侧 `_narration_before_action` 会 `.replace("```", "").strip()`，
    而实时缓冲**做不到**：未闭合的 ` ``` ` 只有在流结束后才知道它是不是
    一个代码块的开头，而已经 emit 出去的帧撤不回来（见
    `test_holdback_matches_narration_for_real_shapes` 的 docstring）。

    所以两边在"围栏"这一层本来就不可能逐字节一致，而那个差异**不影响
    用户看到的任何东西**（围栏在 Markdown 渲染里是零宽的结构标记，
    半截围栏也不会显示成文字）。比较前统一去掉，剩下的差异才是真的问题。
    """
    return text.replace("```", "").strip()


# ══════════════════════════════════════════════════════════
# 放行：正常正文一个字都不能少
# ══════════════════════════════════════════════════════════


def test_plain_text_fully_released() -> None:
    """没有协议的正文：全部放行，一个字节不差。"""
    text = "好的，我先查一下最近的扫描。\n稍等。"
    released, blocked = _run(list(text))
    assert released == text
    assert blocked is False


def test_text_split_at_arbitrary_boundaries() -> None:
    """按**单个字符**切开喂进去，结果必须与一次性喂一样。

    这是最重要的一条：真实的流是按 token 切的，切点完全不在我们控制之内
    （可能正好切在 "ACT" 与 "ION:" 之间）。如果实现里假设"一个 delta 至少
    包含完整的一行"，这条会红。
    """
    text = "第一行\n第二行\n第三行没有换行结尾"
    released, _ = _run(list(text))
    assert released == text


def test_no_trailing_newline_is_flushed() -> None:
    """最后一行没有换行也要放行。

    不放的话界面上表现为「最后一句话少了一半」—— 最容易被当成渲染 bug。
    """
    buffer = HoldbackBuffer()
    assert buffer.feed("只有一行没有换行") == ""
    assert buffer.flush() == "只有一行没有换行"


# ══════════════════════════════════════════════════════════
# 扣住：协议一个字都不能出去
# ══════════════════════════════════════════════════════════


def test_action_line_is_never_released() -> None:
    """`ACTION:` 那一行与它之后的一切都不放行。"""
    released, blocked = _run(
        [
            "好的，我先查一下。\n",
            'ACTION: {"tool": "list_scans", ',
            '"args": {}}',
            "\n后面还有解释",
        ]
    )
    assert released == "好的，我先查一下。\n"
    assert blocked is True
    assert "ACTION" not in released


def test_action_is_blocked_even_without_newline() -> None:
    """协议起头没有换行也立刻锁死。

    ⚠️ 这条是**实现差点写错的地方**：第一版只在遇到 `\\n` 时判定，
    于是 `ACTION: {...}`（模型常不写结尾换行）要等到 `flush()` 才判 ——
    那时正文和协议混在一个 pending 里，flush 一判就整段丢弃，
    连**安全的前半句**也一起丢了。
    """
    buffer = HoldbackBuffer()
    assert buffer.feed("ACTION:") == ""
    assert buffer.blocked is True
    assert buffer.feed(' {"tool": "x"}') == ""
    assert buffer.flush() == ""


def test_fullwidth_colon_is_blocked() -> None:
    """全角冒号同样是协议（`_MARKER` 的正则里有 `[:：]`）。

    判据必须与解析器一致，否则会出现最难查的形态：文字漏给了用户，
    **同时**动作真的被执行了。
    """
    released, blocked = _run(["好的。\n", "ACTION：{\"tool\": \"x\"}"])
    assert released == "好的。\n"
    assert blocked is True


def test_final_is_blocked_too() -> None:
    """`FINAL:` 也要拦。

    对话页不用它（那是解题侧的收尾协议），但模型偶尔会照猫画虎写出来。
    显示给用户与显示 ACTION 一样糟。
    """
    released, blocked = _run(["先说结论。\n", "FINAL: {\"summary\": \"x\"}"])
    assert released == "先说结论。\n"
    assert blocked is True
    for marker in PROTOCOL_MARKERS:
        assert marker in ("ACTION", "FINAL")


def test_leading_whitespace_before_protocol() -> None:
    """协议前有缩进/空行也要拦（`_MARKER` 是 `^\\s*`）。"""
    for head in ("  ACTION: {", "\tACTION: {", "\n ACTION: {"):
        buffer = HoldbackBuffer()
        buffer.feed(head)
        assert buffer.blocked is True, head


# ══════════════════════════════════════════════════════════
# 不误伤：正文里"提到"协议词不是协议
# ══════════════════════════════════════════════════════════


def test_mentioning_action_mid_sentence_is_released() -> None:
    """正文里出现 `ACTION:` 但**不在行首** → 不是协议，正常放行。

    ## 为什么这条必须存在

    否则「我看到一段 ACTION: 开头的日志」这种正常句子会被从中间切断 ——
    而切断的位置正好在一个词中间，看起来像编码错误。
    """
    text = "我看到一段 ACTION: 开头的日志，那段是模型写的。\n后面继续。\n"
    released, blocked = _run(list(text))
    assert released == text
    assert blocked is False


def test_action_after_text_on_same_line_is_released() -> None:
    """`好的。ACTION: {...}` 在**同一行**且协议不在行首 → 不是协议。

    ## 与解析器保持一致

    `parse_actions` 用的 `_MARKER` 是 `re.MULTILINE` 的 `^\\s*(ACTION|FINAL)\\s*[:：]`，
    所以这种形态**它也解析不出来**，因此这一轮不会真的去执行工具。
    既然机器也不认，把它放给用户看就不是泄漏（那是一句普通的话）。

    ⚠️ 如果哪天解析器放宽到"任意位置"，这里必须跟着改 ——
    否则会出现"机器执行了、界面上也显示了"的双重错误。
    """
    text = '好的。ACTION: {"tool": "x"}'
    released, blocked = _run([text])
    assert released == text
    assert blocked is False


def test_words_starting_with_action_prefix_are_not_protocol() -> None:
    """`ACTIONS:` / `ACTIONABLE` 之类不是协议（冒号位置不对）。

    `startswith("ACTION")` 就判的话，"ACTIONABLE 的建议"整行会被吞掉。
    """
    for text in ("ACTIONABLE 的建议：先看日志\n", "ACTIONS 列表为空\n"):
        released, blocked = _run([text])
        assert released == text, text
        assert blocked is False


# ══════════════════════════════════════════════════════════
# 与真实引擎的交叉验证：核心不变式
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "raw",
    [
        # 纯正文，无动作
        "好的，最近的扫描一共 3 条。",
        # 正文 + 动作（最常见）
        '好的，我先查一下。\n\nACTION: {"tool": "list_scans", "args": {}}',
        # 一上来就写动作，没有铺垫
        'ACTION: {"tool": "list_scans", "args": {}}',
        # 动作包在代码围栏里（模型常这么写）
        '先看一下。\n```\nACTION: {"tool": "list_scans", "args": {}}\n```',
        # 多个动作
        '我同时做两件事。\nACTION: {"tool": "a", "args": {}}\nACTION: {"tool": "b", "args": {}}',
        # 正文里提到协议词（非行首 → 不是协议）
        "我看到一段 ACTION: 开头的日志，那是模型写的。",
        # 全角冒号（v0.102 修掉的漏给用户那条）
        '好的。\nACTION：{"tool": "x", "args": {}}',
        # 只有 FINAL，无正文
        'FINAL: {"summary": "x"}',
    ],
)
def test_holdback_matches_narration_for_real_shapes(raw: str) -> None:
    """★ 核心不变式：**放行的文本，规范化后是落库正文的前缀**。

    ## 为什么这一条是整套改动的安全绳

    界面中途看到的是 `holdback` 放行的前缀，而刷新页面看到的是
    `_narration_before_action(raw)` 落库的正文。两者不能是"两句话"的
    关系 —— 用户会认为界面不可信。

    ## 为什么是「前缀」而不是「相等」

    第一版这里断言了逐字节相等，在代码围栏那个用例上红了：

        输入：'先看一下。\\n```\\nACTION: {"tool": ...}\\n```'
        落库：'先看一下。'                 （`_narration_before_action` 剥掉围栏）
        放行：'先看一下。\\n```'            （buffer 放行了围栏行）

    ★ 这不是 buffer 的 bug，而是**「相等」这个断言本身要求过高**：
    未闭合的 ` ``` ` 只有在流结束后才能确定"它是不是一个代码块的开头"，
    而 `emit` 出去的帧无法撤回。要求 buffer 预判围栏配对，等于要求它
    拥有整段文本 —— 那正好是流式做不到的事。

    所以不变式退到**真正必须成立的那一层**：

        放行文本（去掉围栏与首尾空白）是落库正文的前缀

    它守住的正是用户能观察到的那个性质 —— **刷新前后不会出现两套不同
    的说法**；而它不要求 buffer 复刻落库侧的每一个字符级清理动作
    （`.replace("```","")` 这种只有拿到整段才做得对的事）。

    ## ★ 这条测试在 v0.102 抓到过真实的线上缺陷

    第一版跑它红了 4 条，其中两条是**引擎本身**的缺陷而不是缓冲的：
    `_narration_before_action` 用朴素的 `find("ACTION:")`，与解析器的
    `_MARKER` 正则判据不一致 —— 全角冒号时解析器认、它不认（裸 JSON
    落库给用户看），非行首出现时它认、解析器不认（正文被腰斩）。
    两处已改为共用 `_MARKER`。单独两条用例钉在下面。
    """
    from app.chat.engine import _narration_before_action

    expected = _normalize(_narration_before_action(raw))
    released, _ = _run(list(raw))
    actual = _normalize(released)
    # 前缀关系：流式看到的东西，必须是刷新后那句话的开头。
    assert expected.startswith(actual), (
        f"放行的文本不是落库正文的前缀\n  落库: {expected!r}\n  放行: {actual!r}"
    )


def test_narration_strips_fullwidth_colon_protocol() -> None:
    """★ 全角冒号的协议必须被剥离，不能落库给用户看。

    ## 这个缺陷是真实的，而且不是本次改动引入的

    修之前：

        输入：'好的。\\nACTION：{"tool": "run_shell", "args": {}}'
        落库：整段（含裸 JSON）  ← ★ 用户看到内部协议
        执行：run_shell          ← ★ 同时工具真的跑了

    也就是说界面上显示"机器人把协议漏出来了"，而实际上它成功了。
    原因是 `find("ACTION:")` 只认半角，而 `_MARKER` 的 `[:：]` 两种都认。
    """
    from app.chat.engine import _narration_before_action
    from app.kernel.runner import parse_actions

    raw = '好的。\nACTION：{"tool": "run_shell", "args": {}}'
    # 解析器确实认（所以工具会被执行）——
    assert parse_actions(raw) == [("run_shell", {})]
    # 而落库的正文明文必须不含协议
    stored = _narration_before_action(raw)
    assert stored == "好的。"
    assert "ACTION" not in stored


def test_narration_keeps_body_when_action_word_is_not_at_line_start() -> None:
    """正文里**非行首**提到 `ACTION:` 不能把句子腰斩。

    修之前：'我看到一段 ACTION: 开头的日志' → 落库 '我看到一段'
    （半句话，看起来像编码错误）。

    判据：`_MARKER` 要求行首，所以这句话里没有动作 —— `parse_actions`
    返回空，落库就该是完整原句。两处必须一致。
    """
    from app.chat.engine import _narration_before_action
    from app.kernel.runner import parse_actions

    raw = "我看到一段 ACTION: 开头的日志，那是模型写的。"
    assert parse_actions(raw) == []
    assert _narration_before_action(raw) == raw


def test_narration_and_holdback_agree_on_final() -> None:
    """`FINAL:` 也归到"协议"，一律不显示给用户。

    对话页从不产出 FINAL（那是解题侧的收尾协议），但模型偶尔会照猫画虎
    写出来。holdback 与落库两边都剥掉 —— 一致比"哪边对"更重要：
    不一致会导致刷新前后文字不同。
    """
    from app.chat.engine import _narration_before_action

    raw = '先说结论。\nFINAL: {"summary": "x"}'
    assert _narration_before_action(raw) == "先说结论。"
    released, blocked = _run([raw])
    assert released.strip() == "先说结论。"
    assert blocked is True
