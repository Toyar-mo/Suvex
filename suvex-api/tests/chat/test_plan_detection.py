"""`FINAL:` 里写的是"下一步计划"，不是结论（v1.29.1）。

## 这条用例来自哪次线上会话

`chat_01M63WHGQJHAJ684E5YTBQC0QX` 的最后一轮（2026-10-29 09:11 EDT）：
这一轮跑了 2 次 `run_shell`，模型最后写的是

    FINAL: stripped、动态链接、只有 read/puts/setbuf，两次输入。看反汇编确认溢出点。

`_PLAN_START` 要求句首出现「先/现在/接下来/下一步/然后/下面/接着/待会」，
`_PLAN_VERB` 还要求这些词与动词**紧邻**。这句话的最后一个句子以动词「看」
开头，两个正则都没命中，于是：

* `premature_final` = False → 这条消息落成 `surface='answer'`（用户看到的气泡）
* 不走 `final-check` 打回 → 整轮 `done`

界面就停在"看反汇编确认溢出点。"这半句话上。用户的原话是「输出一半就停了」。

## 判据的边界

补的只有**句首直接是动词**这一种形态，并且排除"已经做完"的标记
（`flag{...}`、`已解出`、`拿到`、`成功`…）与过去式（「看了」「查到」）。
误判的代价是多花一次模型调用问一句"真的交付了吗"，
漏判的代价是用户以为解完了、其实停在半路 —— 后者更贵。
"""

from app.chat.nudge import (
    MAX_UNFINISHED_CHARS,
    _has_trailing_plan,
    looks_like_plan,
    looks_unfinished,
)


def test_bare_verb_plan_from_the_live_session():
    """线上原句：句首直接是动词，也要认出来。"""
    body = "stripped、动态链接、只有 read/puts/setbuf，两次输入。看反汇编确认溢出点。"

    assert looks_like_plan(body) is True


def test_bare_imperative_verbs_are_plans():
    assert looks_like_plan("确认一下 libc 版本。") is True
    assert looks_like_plan("分析主函数里 read 的缓冲区大小。") is True
    assert looks_like_plan("跑一遍 checksec 看保护。") is True


def test_plan_after_a_comma_is_still_a_plan():
    """线上原句（`chat_01M64ANA9DQVW1FGWMPEZY6NZD` 第 9 条）：

        FINAL: 主逻辑在 0x40070b，看它。

    前半句是事实，**逗号**后面才是下一步。原来的裸动词判据只认
    「。；！？换行」后的动词，逗号后的「看它」漏掉 → 落成 `answer` → 整轮结束。
    """
    assert looks_like_plan("主逻辑在 0x40070b，看它。") is True
    assert looks_like_plan("没开 PIE，跑一下 ROPgadget 找 gadget。") is True
    assert looks_like_plan("防护：NX 开着，查一下 libc 版本。") is True


def test_comma_clause_does_not_turn_past_tense_into_plan():
    """放宽分句边界后，过去式仍不能被误判。"""
    assert looks_like_plan("偏移是 0x28，确认了。") is False
    assert looks_like_plan("binsh 在 libc 里，查到了。") is False


def test_discourse_marker_form_still_matches():
    """v1.29 原来的形态不能被这次改动弄丢。"""
    assert looks_like_plan("先看反汇编。") is True
    assert looks_like_plan("现在查 libc 版本。") is True
    assert looks_like_plan("接下来跑一下 libc.rip。") is True


def test_past_tense_is_not_a_plan():
    """「看了 / 查到」是已经做过的事，不是下一步。"""
    assert looks_like_plan("看了反汇编，偏移是 0x28。") is False
    assert looks_like_plan("查到了，libc 是 2.31。") is False


def test_delivered_result_is_not_a_plan():
    """真结论里有交付物，打回它只是白花一次调用。"""
    assert looks_like_plan("已解出 flag{local_test_only}。") is False
    assert looks_like_plan("拿到 shell 了，成功读出 flag。") is False
    assert looks_like_plan("溢出点在 0x4006a0，偏移 0x28。") is False


def test_future_plan_containing_result_word_is_still_a_plan():
    """reviewer-7d4f40：计划句里出现「拿到 / 成功」不等于已经完成。

    这两句都会落成 `answer` 后整轮结束：句中有一个裸的「拿到」，
    旧判据直接 `_RESULT_MARKERS` 命中 → 返回 False。
    """
    assert looks_like_plan("先看反汇编，拿到偏移后再写 exploit。") is True
    assert looks_like_plan("接下来拿到 shell 再读 flag。") is True


def test_completion_state_still_wins_over_bare_verb():
    """完成态（…了 / …过）仍然算交付物。"""
    assert looks_like_plan("拿到 shell 了，成功读出 flag。") is False
    assert looks_like_plan("已验证完偏移，试过两个 gadget。") is False


def test_conclusion_with_bare_verb_is_not_a_plan():
    """reviewer-7d4f40：结论句里的动词不是下一步。"""
    assert looks_like_plan("偏移 0x28，验证通过。") is False
    assert looks_like_plan("地址在 0x40070b，写入即覆盖返回地址。") is False


def test_long_body_is_not_a_plan():
    """长正文里顺带一句计划，不算"停在半路"。"""
    body = "看反汇编。" + "反汇编里逐条对照栈帧，" * 30

    assert looks_like_plan(body) is False


def test_short_success_sentence_is_not_a_plan():
    """reviewer-9e8e8e 的 P1：短结论不能被当成计划打回（v1.32）。

    * 「跑通了」是完成态（旧判据只认 `跑通` 时没有任何完成标记）；
    * 「看起来」是结论式开场，不是下一步（裸动词 `看` 误命中）；
    * 「反汇编如下」是交付物（旧判据不认识 `如下`）。
    """
    assert looks_like_plan("跑通了，flag 是 DASCTF{abc}") is False
    assert looks_like_plan("看起来 flag 在 /flag 里。") is False
    assert looks_like_plan("反汇编如下：main 在 0x40070b。") is False


def test_plan_with_tong_still_is_a_plan():
    """补 marker 不能把计划句一起放过（v1.32）。"""
    assert looks_like_plan("接下来跑通 exploit。") is True
    assert looks_like_plan("先看如下命令再跑。") is True


def test_plan_after_a_result_marker_is_still_a_plan():
    """v1.34：完成标记只说明做到哪一步，结尾仍是计划就要打回。

    reviewer-4e5f63 的线上级复现（真实树未改）：这三句都会落成
    `surface=answer` 并让整轮 done，用户又停在半路。
    """
    assert looks_like_plan("已拿到 libc 版本，接下来跑 one_gadget。") is True
    assert looks_like_plan("反汇编如下：main 在 0x40070b。接下来写 exploit。") is True
    assert looks_like_plan("输出如下：uid=0。接下来读 /flag。") is True
    # 计划也可能写在完成标记前面。
    assert looks_like_plan("接下来写 exploit，已拿到 libc 版本。") is True


def test_result_marker_without_a_trailing_plan_stays_a_result():
    """v1.34 不能把真结论一起打回。"""
    assert looks_like_plan("反汇编如下：main 在 0x40070b。") is False
    assert looks_like_plan("跑通了，flag 是 DASCTF{abc}") is False
    assert looks_like_plan("已解出 flag{abc}") is False
    # 计划是写给用户的，不是模型自己的下一步。
    assert looks_like_plan("已解出 flag{abc}，接下来你可以提交。") is False


def test_first_person_bare_verb_is_a_plan():
    """v1.34：`我` 不能挡住裸动词计划。"""
    assert looks_like_plan("我看下反汇编。") is True
    assert looks_like_plan("我先把 gadget 和连通性都确认下来。") is True
    assert looks_like_plan("我看了反汇编，偏移是 0x28。") is False


def test_trailing_plan_helper_handles_empty_edges():
    """v1.34：两个边界不能抛异常。"""
    assert _has_trailing_plan("没有完成标记") is False
    assert _has_trailing_plan("已解出 flag{") is False


def test_last_step_wording_is_a_plan():
    """reviewer-266f16：没有「接下来」也能是计划。"""
    assert looks_like_plan("还差最后一步：打远程拿 shell。") is True
    assert looks_like_plan("接下来连接靶机拿 flag。") is True
    assert looks_like_plan("最后一步：上传 exploit 打远程。") is True


def test_conclusion_heading_is_not_a_plan():
    """「接下来是结论」是在引出结论，不能当计划打回。"""
    assert looks_like_plan("已解出 flag{abc}，接下来是结论：偏移 0xb8。") is False
    assert looks_like_plan("已拿到 shell，下面是总结：偏移 0xb8。") is False


#: 一段足够长的技术性正文（>200 字），用来把「长正文」路径和短句路径分开。
_LONG_CONTEXT = "反汇编里逐条对照栈帧，确认每一个局部变量与保存寄存器的偏移。" * 8


def test_long_body_with_a_trailing_plan_is_a_plan():
    """v1.35：长正文不再一刀切放过 —— 收尾那句仍是计划就要打回。

    线上形态：模型先讲 200 多字的保护/反汇编结论，最后写
    「接下来写 exploit。」。旧代码 `len(body) >= 200 → False`，整轮
    `done`，用户停在半路。
    """
    body = _LONG_CONTEXT + "接下来写 exploit。"
    assert len(body) >= 200
    assert looks_like_plan(body) is True


def test_long_conclusion_with_a_mid_body_plan_word_is_not_a_plan():
    """长正文中段提到「先看」但收尾是结论 → 不能误判。"""
    body = "先看入口点，然后逐段核对。" + _LONG_CONTEXT + "最终偏移是 0x28。"
    assert len(body) >= 200
    assert looks_like_plan(body) is False


def test_long_body_keeps_the_second_person_and_conclusion_exemptions():
    """窗口化之后，两条豁免仍然生效。"""
    assert looks_like_plan(_LONG_CONTEXT + "接下来你可以提交。") is False
    assert looks_like_plan(_LONG_CONTEXT + "接下来是结论：偏移 0xb8。") is False


#: 几个"句首话语标记 + 完成态动词"的收尾句 —— 是**结论**，不是计划（v1.36）。
#:
#: 线上形态：模型写了一大段（>200 字）反汇编/排查结论，收尾句是
#: 「然后我把所有 gadget 都过了一遍，结论如上。」。`_PLAN_START` 只认句首
#: 「然后」，不看后面其实已经「…过了」——于是被判成计划句，整轮被补一句
#: 「这一轮停在了下一步上」并置 `needs_continue`，用户看着一条**已交付的
#: 结论**却被说成"停在计划上"。`_PLAN_BARE_VERB` 早有 `(?!了|过|完…)` 守卫，
#: `_PLAN_START` 这条路径漏了同一个道理。
_COMPLETED_TAILS = [
    "然后我把所有 gadget 都过了一遍，结论如上。",
    "接着我把所有配置文件都读完了，结论如下。",
    "然后我把样本都过了一遍，结论如上。",
    "最后我把这几条路径都验证完了，没有额外防护。",
]


def test_long_completed_action_tail_is_not_a_plan():
    """长正文收尾是「然后/接着…了/过/完」的**完成态** → 不是计划。

    守卫的是 `_PLAN_START` 路径上的完成态误判（v1.36）。每条都必须
    带足长正文，确保走的是「长正文只扫收尾窗口」这条路。
    """
    for tail in _COMPLETED_TAILS:
        body = _LONG_CONTEXT + tail
        assert len(body) >= 200, tail
        assert looks_like_plan(body) is False, tail


def test_long_forward_looking_tail_after_marker_is_still_a_plan():
    """对照：同样的句首标记，但后面是**未完成**的下一步 → 仍是计划。

    防止上一条的修法把真计划也放过（过修）。这几句动词后面都没有
    「了/过/完/到」，是明确要去做的事。
    """
    assert looks_like_plan(_LONG_CONTEXT + "然后我要看 libc 版本，接着写 exploit。") is True
    assert looks_like_plan(_LONG_CONTEXT + "下一步跑 one_gadget 找 gadget。") is True
    assert looks_like_plan(_LONG_CONTEXT + "接下来写脚本拿 shell。") is True


# ══════════════════════════════════════════════════════════════
# 英文碎句复读（v1.55）
# ══════════════════════════════════════════════════════════════
#
# 线上复读（chat_01M6BWQ69MZJNBAB7C7QMVXPV2）：
#     Let me continue.
#     I need to check the state.
#     Let me continue. I need to check the state.
# 三句不到 200 字，但全是英文 —— 原来的 looks_unfinished 只认中文 _INTENT，
# 而这一轮又 tools_used==0，于是既不走纠偏也不走工具分支，整轮 done，
# 界面停在"让我继续"上。修法：补一个英文 `_EN_INTENT`，仍受长度闸约束。



_EN_FRAGMENTS = [
    "Let me continue.\nI need to check the state.",
    "Let me continue. I need to check the state. Let me continue.",
    "I'll run a quick scan now.",
    "Next, I will look at the symbols.",
    "Let me check the disassembly first.",
]


def test_english_intent_fragments_count_as_unfinished():
    for body in _EN_FRAGMENTS:
        assert len(body) < MAX_UNFINISHED_CHARS, body
        assert looks_unfinished(body) is True, body


def test_english_complete_answers_are_not_flagged():
    # 短结论（无意图词）不该被判成"没做完"
    assert looks_unfinished("The flag is flag{abc}. Done.") is False
    assert looks_unfinished("All checks passed.") is False
    # 超过长度闸的长英文正文也不打回（与中文同一条闸）
    long_en = "Let me explain. " + ("This binary has no stack canary. " * 20)
    assert len(long_en) >= MAX_UNFINISHED_CHARS
    assert looks_unfinished(long_en) is False
