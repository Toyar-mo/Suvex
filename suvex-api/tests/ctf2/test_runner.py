"""`ctf2.runner` 的行为：镜像选择、白名单、去重、额度、提交路径、沙箱参数。

## 打桩打在**边界**上，不打在逻辑上

* Docker → 换掉 `runner._run`（于是 `SandboxContainer` 的参数构造
  仍然真的跑一遍，只有子进程是假的）；
* LLM → 用 `_FakeGateway`（于是循环的解析、工具分发、历史裁剪
  仍然真的跑一遍）。

直接 mock `solve_in_sandbox` 会让最值得测的那部分（"模型说什么、
我们怎么理解、怎么落库"）整块跳过。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session

from app.core.vocab import (
    Ctf2SandboxProfile,
    Ctf2SubmissionStatus,
    Ctf2SubmitMode,
)
from app.ctf2 import runner
from app.ctf2.models import Ctf2Submission


def _run(coro):
    """跑一个协程。

    ⚠️ 这些用例**刻意不用** `@pytest.mark.asyncio`：它们要测的是
    "在一个同步上下文里把 async 逻辑跑完整"，而那正是 worker 的真实
    调用方式（`asyncio.run`）。用 pytest-asyncio 反而会盖掉这层。
    """
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════
# 镜像选择
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "category", ["reverse", "REVERSE", " reverse ", "rev", "re", "逆向", "逆向工程"]
)
def test_reverse_categories_pick_the_reverse_image(category: str) -> None:
    assert runner.profile_for_category(category) == Ctf2SandboxProfile.REVERSE.value
    assert (
        runner.image_for_profile(runner.profile_for_category(category))
        == runner.IMAGE_REVERSE
    )


@pytest.mark.parametrize(
    "category",
    ["web", "crypto", "misc", "pwn", "forensics", "stego", "osint", "", None],
)
def test_other_categories_pick_the_base_image(category: str | None) -> None:
    assert runner.profile_for_category(category) == Ctf2SandboxProfile.BASE.value


def test_forensics_is_not_misread_as_reverse() -> None:
    """★ 回归测试：`forensics` 里含子串 `re`，**不能**子串匹配。

    这个坑的症状极其隐蔽：re 镜像是 base 的超集，所以"取证题被送进
    逆向镜像"在功能上**完全正常工作** —— 没有任何报错，没人会发现。
    直到某天有人为了收窄体积把 re 里的东西删了，取证题才开始失败，
    而那时已经查不出原因了。
    """
    assert runner.profile_for_category("forensics") == Ctf2SandboxProfile.BASE.value
    # 顺手把另一个含 re 的常见词也钉住。
    assert runner.profile_for_category("osint-recon") == Ctf2SandboxProfile.BASE.value


def test_unknown_profile_falls_back_to_base() -> None:
    """未知 profile 退回 base 并且不抛 —— 迁移期间库里可能有旧值。"""
    assert runner.image_for_profile("something-else") == runner.IMAGE_BASE


def test_raw_network_is_opt_in() -> None:
    """只有 pwn / 网络类才放开容器网络，认不出来一律隔离。"""
    assert runner.needs_raw_network("pwn") is True
    assert runner.needs_raw_network("network") is True
    assert runner.needs_raw_network("web") is False
    assert runner.needs_raw_network(None) is False
    assert runner.needs_raw_network("未知分类") is False


# ══════════════════════════════════════════════════════════
# 网络白名单
# ══════════════════════════════════════════════════════════


def test_guard_allows_official_domain_and_challenge_hosts() -> None:
    access = ["http://1.2.3.4:8080", "10.0.0.5:9999"]
    # 本题环境
    runner.guard_tool_url("http://1.2.3.4:8080/login", access_urls=access)
    runner.guard_tool_url("http://10.0.0.5:9999", access_urls=access)
    # CTF2 官方域名（附件与 API）
    runner.guard_tool_url("https://ctf2.dasctf.com/files/hint.txt", access_urls=access)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # 云元数据
        "http://127.0.0.1:8000/api/v1/scans",  # 后端自己
        "https://evil-dasctf.com/",  # 后缀匹配会放行的那一类
        "https://dasctf.com.attacker.net/",
        "http://example.com/",  # 题面里可能出现的随便一个站
    ],
)
def test_guard_rejects_out_of_scope_urls(url: str) -> None:
    """★ 越界请求必须被拒绝，且**抛**而不是返回 None。

    返回 None 的校验会让"忘了判返回值"变成一次真实的越界请求 ——
    而那种 bug 不会报错，只会安静地发出去。
    """
    from app.ctf2.client import Ctf2Error

    with pytest.raises(Ctf2Error):
        runner.guard_tool_url(url, access_urls=["http://1.2.3.4:8080"])


def test_guard_does_not_trust_the_description() -> None:
    """★ 白名单来源**只有** `access_urls` 参数。

    这条钉的是一个很容易犯的改法：拿题面文本里的地址当白名单。
    题面是参赛者/出题人可控的内容（"请访问 http://10.0.0.1/"），
    拿它当白名单等于没有白名单。
    """
    from app.ctf2.client import Ctf2Error

    with pytest.raises(Ctf2Error):
        runner.guard_tool_url("http://10.0.0.1/flag", access_urls=["http://1.2.3.4:8080"])


def test_allowed_hosts_handles_bare_host_port() -> None:
    """`access_urls` 里可能是裸的 `host:port`（没有协议前缀）。"""
    assert runner.allowed_hosts(["1.2.3.4:9999"]) == frozenset({"1.2.3.4"})
    assert runner.allowed_hosts(["https://a.example/x", "b.example"]) == frozenset(
        {"a.example", "b.example"}
    )


# ══════════════════════════════════════════════════════════
# flag 校验与去重
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "value",
    [
        "flag{hello_world}",
        "DASCTF{1t_is_a_rea1_flag}",
        "HTB{a-b-c}",
        "sf{中文也行}",
        # ★ 正文里**可以有空格**。见 `test_flag_with_space_is_accepted`
        #   与 `FLAG_PATTERN` 上那段说明 —— 这几条是从真实事故改回来的。
        "flag{and 1=1}",
        "flag{has space}",
        "flag{a  b}",  # 连续空格也不该拦
    ],
)
def test_valid_flags_pass(value: str) -> None:
    assert runner.validate_flag(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "flag{...}",  # 占位符
        "flag{***}",  # 占位符
        "",  # 空
        "   ",  # 纯空白
        "x" * (runner.MAX_ANSWER_LENGTH + 1),  # 超长
    ],
)
def test_impossible_answers_are_rejected(value: str) -> None:
    """★ 本地只拦**明显不可能是答案**的东西。

    v0.21 起判据从"像不像 flag"改成了"是不是明显不可能"。
    以前这份参数表里有 `{leading_brace}`、`**no_braces**`、`flag{a{b}c}`
    —— 它们现在**必须被放行**（见下面那条用例），因为比赛允许的答案
    形态本来就包括命令行、IP、时间戳这些"不像 flag"的串。

    留下的这三类各自对应一种真实损失：

    · 空 / 纯空白 → 平台回参数错误，白费一次机会；
    · 占位符      → 模板里的 `flag{...}` 被当成答案提交出去；
    · 超长        → 远超平台的上限，且几乎不可能是答案。
    """
    with pytest.raises(runner.AnswerFormatError):
        runner.validate_answer(value)


@pytest.mark.parametrize(
    "value",
    [
        # ★★ 比赛规则原文：「提交指定格式的字符串（如 flag、命令行、
        #    文件名、时间戳、IP信息等）」。下面几条就是那些形态。
        "cat /flag",
        "/var/log/nginx/access.log",
        "2026-11-15T13:04:19Z",
        "192.168.1.100",
        "10.0.0.0/8",
        # 不像 flag，但显然是答案
        "admin' OR 1=1--",
        "e3b0c44298fc1c149afbf4c8996fb924",
        # 以及本来就该放行的 flag
        "flag{and 1=1}",
        "DASCTF{anything}",
    ],
)
def test_non_flag_answers_are_accepted(value: str) -> None:
    """★★ 非 flag 形态的答案**必须被放行** —— 这是比赛规则要求的。

    旧实现要求答案匹配 `FLAG_PATTERN`（`前缀{正文}`），于是上面这些
    **正确答案**会在本地被判"格式不合法"丢掉，而用户完全没有补救路径：
    系统连试都没试 —— 这道题就永远拿不到分了。

    两种错法的代价完全不对称：

    * 错杀一个真答案 → **这题永远拿不到分**；
    * 漏放一个垃圾串 → 损失**一次额度**，且平台会给明确判定。

    所以判据松到"只拦明显不可能的"。方向是**宁可放过，不可错杀**。

    ⚠️ 这条用例同时守着一个**已经真实发生过**的事故形状：`flag{and 1=1}`
    曾因 `\\s` 被排除在字符集外而丢掉（见 `test_flag_with_space_is_accepted`）。
    两次是同一个病：**拿"形状"当"对错"的判据**。
    """
    assert runner.validate_answer(value) == value


def test_flag_with_space_is_accepted() -> None:
    """★★ 正文含空格的 flag **必须放行** —— 这条是真实事故改回来的。

    实测（2026-10-01，BUUCTF「Url编码」）：模型完整解出了题 ——
    `unzip` 出 `Url编码/题目.txt`（内容 `%66%6c%61%67…`）、
    `urllib.parse.unquote` 解出 `flag{and 1=1}`、以 confidence=high
    收尾。**而它被自己的格式校验丢了**：

        FLAG_PATTERN 旧写法 `[^{}\\s]{1,200}`  →  `\\s` 排除空格
        →  FlagFormatError  →  extract_candidates 静默丢弃
        →  candidates=[]  →  一次提交都没发  →  题目 failed

    这正是 `validate_flag` 的 docstring 自己警告过的失败模式：
    **本地拦下一个真 flag，用户完全没有补救路径**。

    ⚠️ 这条用例以前是反的 —— 旧测试把 `"flag{has space}"` 断言为
    **必须拒绝**，于是把错误行为钉死了。测试写反方向比没有测试更危险：
    它会让修复看起来像破坏。
    """
    assert runner.validate_flag("flag{and 1=1}") == "flag{and 1=1}"
    # 归一化（去引号/反引号）之后同样放行
    assert runner.validate_flag("`flag{and 1=1}`") == "flag{and 1=1}"
    # 提取链路也要能拿到它（这是真正被丢掉的那一步）
    got = runner.extract_candidates(
        {"candidates": [{"flag": "flag{and 1=1}", "evidence": "unquote", "confidence": "high"}]}
    )
    assert [c.flag for c in got] == ["flag{and 1=1}"]


def test_rejected_candidates_carry_a_reason() -> None:
    """★★ 候选被拒时**必须能把原因带出去** —— 否则失败无法排查。

    同一个事故的另一半：原因当时只进了一句 `logger.info`，界面上是
    `failed` + 「环境已释放。」，用户完全无从下手。而"哪个字符串被拒、
    为什么被拒"恰恰是**唯一**能让人判断该不该改校验的信息。

    所以 `extract_candidates_with_reasons` 把原因返给调用方，
    由 `solve_in_sandbox` 写进 `outcome.error`，最终落到
    `challenge.last_error` 显示在题目卡片上。
    """
    accepted, rejected = runner.extract_candidates_with_reasons(
        {
            "candidates": [
                {"flag": "flag{good_one}", "confidence": "high"},
                {"flag": "", "confidence": "low"},  # 空
                {"flag": "flag{...}", "confidence": "low"},  # 占位符
            ]
        }
    )
    # 好的留下
    assert [c.flag for c in accepted] == ["flag{good_one}"]
    # 坏的各自带着理由，且**带上原样的字符串**（不然用户不知道是哪个被拒了）
    assert len(rejected) == 2
    assert any("flag{...}" in r for r in rejected)

    # 空载荷不该炸
    assert runner.extract_candidates_with_reasons(None) == ([], [])


def test_dasctf_prefix_is_accepted() -> None:
    """★ 前缀**不写死**成 `flag`。

    CTF2 上是 DASCTF 系列。把前缀写死成 `flag` 会让所有真题 flag
    被自己的校验挡在门外 —— 而那是**最坏的一种失败**：
    真的解出来了，但系统连试都没试。
    """
    assert runner.validate_flag("DASCTF{anything}") == "DASCTF{anything}"


def test_quotes_are_stripped() -> None:
    """模型经常用反引号/引号包住 flag（Markdown 习惯）——
    不剥掉会让一次**正确**的提交被平台判错，白白扣一次额度。"""
    assert runner.validate_flag("`flag{x}`") == "flag{x}"
    assert runner.validate_flag('"flag{x}"') == "flag{x}"


def test_flag_fingerprint_is_stable_and_normalized() -> None:
    assert runner.flag_fingerprint("flag{x}") == runner.flag_fingerprint(" flag{x} ")
    assert runner.flag_fingerprint("flag{x}") != runner.flag_fingerprint("flag{y}")


def test_already_submitted_detects_duplicates(db_session: Session, ctf2_challenge) -> None:
    """去重：`correct` / `wrong` / `pending` 三种都算"已经提交过"。

    `pending` 尤其重要：它意味着请求**可能已经到达平台**。
    不把它算进去的话，模型重新提出同一个 flag 时会再发一次 ——
    而重复提交在多数平台计为错误。宁可漏掉一次重试机会。
    """
    db_session.add(
        Ctf2Submission(
            id="ctf2sub_test_1",
            challenge_id=ctf2_challenge.id,
            flag="flag{dup}",
            status=Ctf2SubmissionStatus.WRONG.value,
            mode=Ctf2SubmitMode.SAFE.value,
        )
    )
    db_session.commit()

    assert runner.already_submitted(
        db_session, challenge_id=ctf2_challenge.id, flag="flag{dup}"
    )
    assert not runner.already_submitted(
        db_session, challenge_id=ctf2_challenge.id, flag="flag{other}"
    )


def test_rejected_submissions_do_not_block_a_retry(
    db_session: Session, ctf2_challenge
) -> None:
    """★ `rejected` **不算**已提交 —— 它根本没发出去。

    本地拦截（额度用尽、形状不符）不消耗外部额度，所以它不该
    影响去重判断。把它算进去会让"修好形状之后再试一次"被自己挡住。
    """
    db_session.add(
        Ctf2Submission(
            id="ctf2sub_test_2",
            challenge_id=ctf2_challenge.id,
            flag="flag{retry}",
            status=Ctf2SubmissionStatus.REJECTED.value,
            mode=Ctf2SubmitMode.SAFE.value,
        )
    )
    db_session.commit()
    assert not runner.already_submitted(
        db_session, challenge_id=ctf2_challenge.id, flag="flag{retry}"
    )


# ══════════════════════════════════════════════════════════
# 提交决策：safe / auto / 额度
# ══════════════════════════════════════════════════════════


def test_safe_mode_holds_instead_of_submitting() -> None:
    """★ safe 模式**不提交**，只产出候选。这是默认路径。

    ⚠️ 刻意**不传**证据信号：safe 模式不受证据门禁影响
    （人工会看 evidence）。这条同时钉住"门禁只对 auto 生效"。
    """
    decision = runner.decide_submission(mode="safe", already=False, remaining_wrong=3)
    assert decision.action == "hold"
    assert "人工确认" in decision.reason


def test_auto_mode_submits_within_quota() -> None:
    """★ auto 模式 + 证据齐备 + 额度内 → 提交。

    ⚠️ v0.31 起必须显式给证据信号（`evidence` + `saw_tool_output`）——
    否则会命中证据门禁。那不是测试的权宜，而是**刻意的接口设计**：
    让"忘了传信号"表现为"候选被 hold"（可见、可查），
    而不是"门禁静默失效"（不可见）。
    """
    decision = runner.decide_submission(
        mode="auto",
        already=False,
        remaining_wrong=3,
        evidence="cat /flag 的输出里就是它",
        saw_tool_output=True,
    )
    assert decision.action == "submit"


def test_auto_mode_holds_when_no_tool_was_ever_run() -> None:
    """★★ v0.31 证据门禁：**一次工具都没跑**就给答案 → 不提交。

    这是门禁要挡的最坏情况：模型凭空编一个 flag（可能连附件都没看），
    而 auto 模式原来只看"没交过 + 还有额度"就把它发出去 ——
    代价是不可再生的错误提交额度。
    """
    decision = runner.decide_submission(
        mode="auto",
        already=False,
        remaining_wrong=3,
        evidence="我推理出来的",
        saw_tool_output=False,
    )
    assert decision.action == "hold", "★ 没跑过工具就提交了"
    assert "没有跑过任何工具" in decision.reason


def test_auto_mode_holds_when_evidence_is_empty() -> None:
    """★★ 跑了工具、但候选**没给证据** → 不提交。

    与上一条是两个独立信号：跑了工具不等于"这个答案是跑出来的"。
    """
    decision = runner.decide_submission(
        mode="auto",
        already=False,
        remaining_wrong=3,
        evidence="   ",  # 只有空白
        saw_tool_output=True,
    )
    assert decision.action == "hold", "★ 没有证据也提交了"
    assert "evidence" in decision.reason


def test_evidence_gate_does_not_affect_safe_mode() -> None:
    """★★ 门禁**只对 auto 生效** —— safe 模式的去向必须与之前完全一致。

    这条挡的是"顺手把门禁加到所有模式上"：safe 模式的候选本来就等
    人工确认，而人会看 evidence —— 加闸只会让"候选落库"变复杂，
    收益为零。
    """
    for evidence, saw in (("", False), ("有证据", True)):
        decision = runner.decide_submission(
            mode="safe",
            already=False,
            remaining_wrong=3,
            evidence=evidence,
            saw_tool_output=saw,
        )
        assert decision.action == "hold"
        assert "人工确认" in decision.reason, (
            "★ safe 模式的理由变成了证据门禁的理由 —— 说明门禁误伤了 safe"
        )


def test_wrong_submission_ceiling_blocks_even_auto_mode() -> None:
    """★★ 额度用尽时**自动模式也必须停**（已定设计决策 4 的原文：
    "仍受每题错误提交次数上限约束，默认 3，可配置"）。

    这条是 auto 模式下最容易出的错：把"自动"理解成"无人干预"，
    于是额度用尽之后还在发 —— 而额度是不可再生的，
    每多发一次都在拿这道题"以后还能不能试"去换。

    ⚠️ 证据给足，确保**是额度**挡下的（而不是证据门禁）——
    否则这条会在门禁生效后变成假绿。
    """
    decision = runner.decide_submission(
        mode="auto",
        already=False,
        remaining_wrong=0,
        evidence="有证据",
        saw_tool_output=True,
    )
    assert decision.action == "reject"
    assert "用尽" in decision.reason


def test_duplicate_beats_everything() -> None:
    """去重优先于模式判定：已经提交过的不能再发一次。"""
    assert (
        runner.decide_submission(mode="auto", already=True, remaining_wrong=5).action
        == "reject"
    )
    assert (
        runner.decide_submission(mode="safe", already=True, remaining_wrong=5).action
        == "reject"
    )


# ══════════════════════════════════════════════════════════
# ★★ v0.42：第三道闸 —— 答案必须落在**真实工具输出**里
# ══════════════════════════════════════════════════════════
#
# 前两道闸（`saw_tool_output` / `evidence`）有一个共同弱点：**它们只读模型
# 自己写的东西**。一个"跑过几条无害命令 + 编一段 evidence + 编一个 flag"的
# 模型仍然能走到 `submit` —— 而错误提交烧的是不可再生额度。
#
# 这道闸第一次把原始工具输出拉进判据。判据刻意宽松（归一化后子串匹配）：
# 挡的是"凭空编"，不是"格式不对"。


def test_auto_mode_holds_when_the_answer_never_appeared_in_any_output() -> None:
    """★★ 答案没有在任何一次工具输出里出现过 → hold（不消耗额度）。"""
    decision = runner.decide_submission(
        mode="auto",
        already=False,
        remaining_wrong=3,
        evidence="cat /flag 的输出里就是它",
        saw_tool_output=True,
        flag="flag{invented}",
        tool_outputs=["total 0\ndrwxr-xr-x 2 root root 4096 .", "admin/admin"],
    )
    assert decision.action == "hold", "★ 凭空编的答案被提交了"
    assert "没有在任何一次工具输出里出现过" in decision.reason


def test_auto_mode_submits_when_the_answer_is_in_a_tool_output() -> None:
    """★ 反向断言：真在输出里出现过 → 照常提交（这道闸几乎不误伤）。"""
    decision = runner.decide_submission(
        mode="auto",
        already=False,
        remaining_wrong=3,
        evidence="cat /flag 的输出里就是它",
        saw_tool_output=True,
        flag="flag{real}",
        tool_outputs=["$ cat /flag\nwell done, flag{real}\n"],
    )
    assert decision.action == "submit"


def test_the_grounding_gate_is_skipped_when_no_outputs_were_passed() -> None:
    """★★ `tool_outputs` 为空时**不得拦截**。

    证据池是可选参数：旧调用点、单测、人工确认路径都不会传它。
    这时这道闸无从判断 —— 拦下来只会误伤。"有没有跑过工具"
    由上一道闸负责，两者职责不重叠。
    """
    decision = runner.decide_submission(
        mode="auto",
        already=False,
        remaining_wrong=3,
        evidence="有证据",
        saw_tool_output=True,
        flag="flag{whatever}",
    )
    assert decision.action == "submit"


def test_answer_is_grounded_ignores_whitespace_case_and_html_entities() -> None:
    """★ 归一化三件套：折行、大小写、HTML 实体。

    这三条都不是理论情况：flag 会被折行、会被写成 `Flag{...}`、
    在 HTTP 响应体/HTML 源码里会变成 `flag&#123;...&#125;`。
    漏掉任何一条，这道闸就会**误杀真候选** —— 而误杀的代价
    是人工要去复核一个本来可以直接提交的答案。
    """
    # 折行：输出里 flag 被换行拆开。
    assert runner.answer_is_grounded("flag{abc}", ["flag{ab\nc}"])
    # 大小写：模型把小写写成了大写（大小写由平台判，这层不管）。
    assert runner.answer_is_grounded("flag{ABC}", ["...Flag{abc}..."])
    # HTML 实体：HTTP 响应体里的写法。
    assert runner.answer_is_grounded("flag{abc}", ["<div>flag&#123;abc&#125;</div>"])
    # 反证：真不在输出里就是不在。
    assert not runner.answer_is_grounded("flag{zzz}", ["flag{abc}"])


def test_answer_is_grounded_rejects_an_empty_answer() -> None:
    """★ 空串是**任何**字符串的子串 —— 不特判的话这道闸对空答案完全失效。"""
    assert not runner.answer_is_grounded("", ["随便什么输出"])
    assert not runner.answer_is_grounded("   ", ["随便什么输出"])
    assert not runner.answer_is_grounded("flag{x}", [])


# ══════════════════════════════════════════════════════════
# 输出解析
# ══════════════════════════════════════════════════════════


def test_parse_action_reads_a_tool_call() -> None:
    text = '先看看有什么。\nACTION: {"tool": "run_shell", "args": {"command": "ls /work"}}'
    assert runner.parse_action(text) == ("run_shell", {"command": "ls /work"})


def test_parse_action_tolerates_a_code_fence() -> None:
    """模型会用 ```json 围栏包住 JSON —— 这是常态，不是异常。"""
    text = 'ACTION: ```json\n{"tool": "read_attachment", "args": {"path": "a.txt"}}\n```'
    assert runner.parse_action(text) == ("read_attachment", {"path": "a.txt"})


def test_parse_action_returns_none_on_garbage() -> None:
    assert runner.parse_action("我觉得这题是弱口令。") is None
    assert runner.parse_action('ACTION: {"tool": ""}') is None


def test_parse_final_reads_candidates() -> None:
    text = 'FINAL: {"candidates": [{"flag": "flag{ok}", "evidence": "curl 回显"}]}'
    payload = runner.parse_final(text)
    assert payload is not None
    assert [c.flag for c in runner.extract_candidates(payload)] == ["flag{ok}"]


# ══════════════════════════════════════════════════════════
# ★★ v0.41：工具输出截断改为「头 + 尾」
# ══════════════════════════════════════════════════════════
#
# 只留头的直接代价实测在 `ctf2chal_01M4QRAT254DE91N89338Z7P4M`
# （[ACTF2020 新生赛]Upload，78 轮、5,536,545 token）：模型 FINAL 原话是
# 「上传结果信息位于响应体的末尾，而返回页面长达 8530~8640 字符
# （内嵌一个巨大的 SVG），始终看不到上传后的提示/路径」。
#
# 拆成两个函数而不是改 `_clip`：纪要那一行（`digest.append`）是索引，
# 首尾拼接会把一行可读的纪要毁掉 —— 见 `runner._clip` 的说明。


def test_both_ends_keeps_the_head_and_the_tail() -> None:
    """超长输出 → 开头与结尾都在，中间有省略说明。"""
    text = "HEAD" + "x" * 5000 + "TAIL"
    clipped = runner._clip_both_ends(text, 200)

    assert clipped.startswith("HEAD")
    assert clipped.endswith("TAIL")
    assert "中间省略" in clipped
    assert "x" * 5000 not in clipped


def test_both_ends_is_a_noop_when_it_fits() -> None:
    """不超长时**逐字符不变** —— 别给所有输出都塞一句提示。"""
    text = "短输出"
    assert runner._clip_both_ends(text, 200) == text
    assert runner._clip_both_ends("", 200) == ""


def test_both_ends_takes_exactly_the_limit_with_an_odd_budget() -> None:
    """★ 奇数 limit 时截取总量必须精确 —— 不多切一个字符。

    左边取 `limit // 2`、右边取 `limit - limit // 2`，两者之和恒等于 limit。
    写成 `limit // 2` 两边各一次的话，奇数 limit 会少切一个字符。
    """
    text = "A" * 1000 + "B" * 1000
    limit = 201
    clipped = runner._clip_both_ends(text, limit)
    head, _, tail = clipped.partition("\n…（中间省略")

    assert len(head) == limit // 2
    assert len(tail.rsplit("\n", 1)[-1]) == limit - limit // 2
    assert len(head) + len(tail.rsplit("\n", 1)[-1]) == limit
    # 省略的字符数与 message 里写的数字一致
    assert f"省略 {len(text) - limit} 字符" in clipped


def test_the_old_clip_still_only_keeps_the_head() -> None:
    """★ 反向断言：`_clip` **只留头** —— 纪要路径没被连带改掉。

    ⚠️ 断言写成 `"TAIL" not in clipped` 而不是 `not clipped.endswith("TAIL")`：
    后者漏得住"照抄 `_clip_both_ends`"的破坏版 —— 那种版本把尾部片段接在
    中间、提示语留在最后，`endswith` 仍是 False。反向验证抓到了这个弱点
    （第一版就是这么写的，破坏后仍然绿）。
    """
    text = "HEAD" + "x" * 5000 + "TAIL"
    clipped = runner._clip(text, 200)

    assert clipped.startswith("HEAD")
    assert "TAIL" not in clipped
    assert "已截断" in clipped


def test_combined_uses_the_both_ends_clip() -> None:
    """`CommandResult.combined()` 走的是头尾双段（工具原始输出那条路径）。"""
    result = runner.CommandResult(0, "HEAD" + "x" * 5000 + "TAIL", "")
    combined = result.combined(limit=200)

    assert combined.startswith("HEAD")
    assert combined.endswith("TAIL")
    assert "中间省略" in combined


def test_the_truncated_http_hint_does_not_point_at_curl() -> None:
    """★ 截断提示**不能**教模型去容器里 curl。

    沙箱默认 `--network none`（`_argv` 里那个最重要的安全参数），容器内
    出不去网 —— 实测 transcript 里 `curl` 直接 `exit=6`（Could not
    resolve host）。原文案正是这么写的，等于给了一条走不通的路：
    模型照着试、失败、再回来，白烧轮次。

    这条钉的是"给的办法必须是这个工具自己能做到的"。
    """
    big = b"HEAD" + b"x" * 40000 + b"TAIL"

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, content=big)

    async def call() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await runner._tool_http_request(
                {"url": "http://1.2.3.4:8080/"}, spec=_spec(), http=http
            )

    out = _run(call())

    assert "只读了前" in out
    # ★ 核心断言：不能再出现那条走不通的指引。
    assert "curl" not in out
    # 给的办法必须是 http_request 自己能做的。
    assert "http_request" in out or "Range" in out
    # 头尾都在（大响应体的关键信息常出现在尾部）。
    assert out.startswith("HTTP 200")
    assert "TAIL" in out


# ══════════════════════════════════════════════════════════
# ★★ v0.38：FINAL 的三块分析（summary / tried / ruled_out）
# ══════════════════════════════════════════════════════════


def test_extract_final_analysis_reads_all_three() -> None:
    """★ 三块都要取出来 —— 它们以前被整段丢掉（题解"简陋"的根因）。"""
    payload = {
        "candidates": [],
        "summary": "目标是 PHP 上传点，黑名单只挡 php。",
        "tried": ["用响应长度做旁路：baseline=8628", "试过 .phtml"],
        "ruled_out": ["上传目录 /upload/ 不存在"],
    }

    summary, tried, ruled_out = runner.extract_final_analysis(payload)

    assert summary == "目标是 PHP 上传点，黑名单只挡 php。"
    assert tried == ["用响应长度做旁路：baseline=8628", "试过 .phtml"]
    assert ruled_out == ["上传目录 /upload/ 不存在"]


def test_extract_final_analysis_tolerates_bad_shapes() -> None:
    """形状不对就当没有，**不抛** —— 统计不该毁掉一次解题的结果。

    实测模型会给的形状：缺字段 / 单个字符串 / 数组里混 null 与数字。
    """
    # 全缺
    assert runner.extract_final_analysis({}) == ("", [], [])
    assert runner.extract_final_analysis(None) == ("", [], [])

    # 单个字符串包成一项（模型偶尔把数组写成一个长句）
    summary, tried, _ = runner.extract_final_analysis({"tried": "只试了一件事"})
    assert summary == ""
    assert tried == ["只试了一件事"]

    # 数组里混非字符串 → 逐项过滤，只留非空字符串
    _, tried, ruled = runner.extract_final_analysis(
        {"tried": ["好的", None, 123, "", "  ", {"step": "嵌套对象"}], "ruled_out": "不是数组但也不是列表"}
    )
    assert tried == ["好的"]
    # `ruled_out` 给的是字符串 → 包成一项
    assert ruled == ["不是数组但也不是列表"]

    # 不是 dict
    assert runner.extract_final_analysis("FINAL: {}") == ("", [], [])


def test_extract_final_analysis_strips_whitespace() -> None:
    """首尾空白要 strip —— 否则题解里会出现多余空行。"""
    summary, tried, ruled = runner.extract_final_analysis(
        {"summary": "  有内容  ", "tried": ["  项  "], "ruled_out": [" 项2 "]}
    )

    assert summary == "有内容"
    assert tried == ["项"]
    assert ruled == ["项2"]


def test_solve_outcome_carries_the_analysis_fields() -> None:
    """`SolveOutcome` 上真的有三个字段（默认空，不是没有）。"""
    outcome = runner.SolveOutcome()

    assert outcome.summary == ""
    assert outcome.tried == []
    assert outcome.ruled_out == []


def test_extract_candidates_drops_bad_and_keeps_good() -> None:
    """★ 坏候选丢掉、好候选留下，**不因为一条坏的就全丢**。

    模型经常给三个候选，其中一个是"我没找到"的占位。全丢会让
    一次成功的解题被判成失败。

    ⚠️ v0.21：`"not a flag"` 这条**现在是合法的**（比赛允许的答案形态
    包括命令行、IP、时间戳），所以它会被留下。把坏例子换成真正不可能的
    —— 空串与占位符。
    """
    payload = {
        "candidates": [
            {"flag": "flag{...}", "evidence": "占位符"},
            {"flag": "flag{real_one}", "evidence": "真拿到了"},
            {"flag": "", "evidence": "空"},
            {"flag": "cat /flag", "evidence": "★ 命令行 —— 必须留下"},
        ]
    }
    assert [c.flag for c in runner.extract_candidates(payload)] == [
        "flag{real_one}",
        "cat /flag",
    ]


def test_extract_candidates_deduplicates() -> None:
    """同一个 flag 写两遍（一个带反引号）只是一条。"""
    payload = {"candidates": [{"flag": "flag{x}"}, {"flag": "`flag{x}`"}]}
    assert len(runner.extract_candidates(payload)) == 1


def test_extract_candidates_accepts_a_single_flag_shape() -> None:
    """容错：有些模型直接给 `{"flag": "..."}` 而不是候选数组。"""
    assert [c.flag for c in runner.extract_candidates({"flag": "flag{single}"})] == [
        "flag{single}"
    ]


def test_interpret_submit_needs_a_receipt_to_claim_correct() -> None:
    """★ 判"对"需要**两个**信号：成功标志 + 平台回执 id。

    只有 `correct: true` 而没回执时必须退回 `pending` —— 那对应
    数据库上 `correct_implies_receipt` 那条约束，而约束的意图是
    "不要凭空宣称解出了一道题"。
    """
    status, _message, receipt = runner.interpret_submit({"correct": True, "id": 99})
    assert (status, receipt) == (Ctf2SubmissionStatus.CORRECT.value, "99")

    status, message, receipt = runner.interpret_submit({"correct": True})
    assert status == Ctf2SubmissionStatus.PENDING.value
    assert receipt is None
    assert "回执" in message


def test_interpret_submit_maps_wrong() -> None:
    status, _message, _receipt = runner.interpret_submit(
        {"correct": False, "message": "flag 格式应为 flag{...}"}
    )
    assert status == Ctf2SubmissionStatus.WRONG.value


def test_interpret_submit_understands_the_real_ctf2_field_names() -> None:
    """★ CTF2 官方的字段是 `accepted` / `is_solved`，不是 `correct`。

    这一条的样本来自**真实平台**（2026-09-29，`practice=BUUCTF` 的
    hashcat 题，故意提交一个错的 flag）：

        {"accepted": false, "attempt": 1, "earned_points": 0,
         "is_solved": false, "points": 0, "solved_sub_flag_count": 0,
         "submission_id": "b3a0790e-…", "total_sub_flag_count": 1}

    在加这条分支之前，这份响应会掉进 `status` 兜底分支：
    `payload.get("status")` 是 None → `"" in ("correct","accepted","ok")`
    → False。**错误的那一侧恰好蒙对**，所以只认旧字段名的代价不会
    在错 flag 上暴露，而是让一次**正确**的提交被记成 wrong ——
    这个用例钉的就是那个方向。
    """
    wrong = {
        "accepted": False,
        "attempt": 1,
        "earned_points": 0,
        "is_solved": False,
        "points": 0,
        "solved_sub_flag_count": 0,
        "submission_id": "b3a0790e-6c00-4a66-b3b2-792f4103939d",
        "total_sub_flag_count": 1,
    }
    status, _message, receipt = runner.interpret_submit(wrong)
    assert status == Ctf2SubmissionStatus.WRONG.value
    assert receipt == "b3a0790e-6c00-4a66-b3b2-792f4103939d"

    right = {**wrong, "accepted": True, "is_solved": True, "earned_points": 100}
    status, _message, receipt = runner.interpret_submit(right)
    assert status == Ctf2SubmissionStatus.CORRECT.value
    assert receipt == "b3a0790e-6c00-4a66-b3b2-792f4103939d"

    # 套题：单个子 flag 命中时 `accepted` 为真而整题仍未完成。
    # 判定按 `accepted`（这一次提交收不收），不按 `is_solved`。
    suite = {
        **right,
        "is_solved": False,
        "total_sub_flag_count": 3,
        "solved_sub_flag_count": 1,
    }
    status, _message, _receipt = runner.interpret_submit(suite)
    assert status == Ctf2SubmissionStatus.CORRECT.value

    # `accepted` 缺席时退回 `is_solved`，两个都没有才走旧的字符串兜底。
    only_solved = {
        "is_solved": True,
        "submission_id": "s-1",
    }
    status, _message, _receipt = runner.interpret_submit(only_solved)
    assert status == Ctf2SubmissionStatus.CORRECT.value


# ══════════════════════════════════════════════════════════
# 解题循环
# ══════════════════════════════════════════════════════════


class _FakeGateway:
    """按剧本回话的假 LLM。**记录收到的消息**，让"喂回去什么"可断言。

    ## ★ v0.36：必须带 `model` / `label` / `usage`

    解题循环现在会把每次调用的 `(turn, gateway.model, gateway.label,
    reply.usage)` 收进 `SolveOutcome.usages`（token 统计的数据来源）。
    所以假 gateway 要**与真 `LlmGateway` 同形** —— 少了属性，
    循环会在生产代码那一行抛 `AttributeError`。

    ⚠️ 这个坑是实测踩到的：第一版假 gateway 只有 `chat()`，
    加统计之后 `tests/ctf2/` 里 **8 个用例**同时报
    `'_FakeGateway' object has no attribute 'model'`。

    `usage` 默认给一个**有值**的用量：这样"统计链路通不通"这件事
    在每个既有用例里都被顺带验证了，而不是只在专门的用例里。
    需要测"接入点没给 usage"时传 `usage=TokenUsage()`（reported=False）。

    ## ★ v0.41.2：可以逐条指定 `finish_reasons`

    解题循环现在读 `reply.finish_reason` 判断"这次输出是不是被切断了"。
    默认全是 `""`（未知 → 当作没被切断），既有用例的行为因此不变；
    要测截断时传 `finish_reasons=["length", ...]`，**与 `replies` 一一对应**。
    """

    def __init__(
        self,
        replies: list[str],
        *,
        model: str = "fake-model",
        label: str = "fake-label",
        usage: runner.TokenUsage | None = None,
        finish_reasons: list[str] | None = None,
    ) -> None:
        self.replies = list(replies)
        self.seen: list[list[dict[str, str]]] = []
        self.model = model
        self.label = label
        # 默认给一个非零用量 —— 见类 docstring 的说明。
        self.usage = usage if usage is not None else runner.TokenUsage(
            prompt_tokens=10, completion_tokens=20, total_tokens=30, reported=True
        )
        self.finish_reasons = list(finish_reasons) if finish_reasons else []

    async def chat(self, messages, *, system):  # noqa: ANN001, ANN201
        self.seen.append(list(messages))
        text = self.replies.pop(0) if self.replies else 'FINAL: {"candidates": []}'
        reason = self.finish_reasons.pop(0) if self.finish_reasons else ""
        return runner.LlmReply(
            text=text, source="content", usage=self.usage, finish_reason=reason
        )


class _FakeSandbox:
    """假沙箱。记录执行的命令，可按命令返回预设输出。

    ## ★ v0.41：必须带 `died`

    解题循环每轮开头会读 `sandbox.died`（容器被外部移除后立刻中止，
    判据见 `runner.SandboxContainer.died` 的说明）。假沙箱要**与真
    `SandboxContainer` 同形** —— 少了这个属性，循环会在生产代码那一行
    抛 `AttributeError`。这个坑与 `_FakeGateway` 少了 `model` 那次同形。
    """

    def __init__(
        self,
        outputs: dict[str, str] | None = None,
        *,
        start_error: Exception | None = None,
    ) -> None:
        self.commands: list[str] = []
        self.destroyed = False
        self.outputs = outputs or {}
        self.died = False
        #: v0.43：重建（`start()`）被调用了几次。
        self.restarts = 0
        #: v0.43：给一个异常就表示"重建必定失败"。
        self.start_error = start_error
        #: 真 `SandboxContainer` 有这个属性，而重建成功的日志里会读它。
        self.name = "ctf2-fake-sandbox"

    def shell(self, command: str, *, timeout: int = 300):  # noqa: ANN001, ANN201
        self.commands.append(command)
        for needle, output in self.outputs.items():
            if needle in command:
                return runner.CommandResult(0, output, "")
        return runner.CommandResult(0, "(无输出)", "")

    def copy_in(self, host_path, container_path):  # noqa: ANN001, ANN201
        return runner.CommandResult(0, "", "")

    def start(self) -> None:
        """重建容器（v0.43）。与真类的**唯一契约**：成功就清掉 `died`。

        ⚠️ 必须清：真 `SandboxContainer.start()` 在 `_verify_running()` 之后
        会置 `self.died = False`（新容器 = 新的生命）。假沙箱不清的话，
        解题循环下一轮开头仍读到 `died=True`，会把"重建成功"当成失败，
        于是这条路径在测试里永远走不到"继续解题"那一支。
        """
        self.restarts += 1
        if self.start_error is not None:
            raise self.start_error
        self.died = False

    def destroy(self) -> None:
        self.destroyed = True


def _spec(**overrides) -> runner.SolveSpec:
    kwargs = {
        "challenge_id": "ctf2chal_test",
        "title": "签到题",
        "category": "web",
        "description": "登录口有弱口令。",
        "profile": Ctf2SandboxProfile.BASE.value,
        "access_urls": ("http://1.2.3.4:8080",),
        "attachments": (),
        "llm_model": "deepseek-chat",
        "llm_api_key": "sk-test-not-real",
        "llm_api_base": "https://api.deepseek.com",
        "max_turns": 5,
    }
    kwargs.update(overrides)
    return runner.SolveSpec(**kwargs)


async def _dispatch(name: str, args: dict, *, spec=None, sandbox=None) -> str:
    async with httpx.AsyncClient() as http:
        return await runner.dispatch_tool(
            name, args, spec=spec or _spec(), sandbox=sandbox or _FakeSandbox(), http=http
        )


def test_solver_loop_executes_tools_then_finishes(tmp_path: Path) -> None:
    """一轮完整的循环：调工具 → 拿到输出 → 给出结论。"""
    attachment = tmp_path / "hint.txt"
    attachment.write_text("提示：admin/admin", encoding="utf-8")

    sandbox = _FakeSandbox(outputs={"ls": "hint.txt"})
    gateway = _FakeGateway(
        [
            'ACTION: {"tool": "run_shell", "args": {"command": "ls -la /work/attachments"}}',
            'FINAL: {"candidates": [{"flag": "flag{w3ak}", "evidence": "curl 回显 flag{w3ak}"}]}',
        ]
    )
    spec = _spec(attachments=(attachment,))

    outcome = _run(
        runner.solve_in_sandbox(spec, sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    assert sandbox.commands == ["ls -la /work/attachments"]
    assert [c.flag for c in outcome.candidates] == ["flag{w3ak}"]
    assert outcome.turns == 2
    assert outcome.error is None


def test_solver_loop_feeds_a_format_reminder_back() -> None:
    """★ 模型没按格式输出时，**具体地**告诉它正确格式并重试。

    只写"格式错误"的话，模型的常见反应是把同一段话再说一遍 ——
    因为它不知道自己差在哪（这条实测过）。所以断言那句提醒里
    带着可照抄的模板。
    """
    sandbox = _FakeSandbox()
    gateway = _FakeGateway(["我觉得这题应该用弱口令试试。", 'FINAL: {"candidates": []}'])
    outcome = _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    reminder = gateway.seen[1][-1]["content"]
    assert "ACTION:" in reminder and "FINAL:" in reminder
    assert outcome.turns == 2


def test_solver_loop_gives_up_when_turns_run_out() -> None:
    """回合用尽 → 明确报错，**不编造一个 flag**。"""
    sandbox = _FakeSandbox()
    gateway = _FakeGateway(
        ['ACTION: {"tool": "run_shell", "args": {"command": "true"}}'] * 10
    )
    outcome = _run(
        runner.solve_in_sandbox(
            _spec(max_turns=3), sandbox=sandbox, gateway=gateway, system_prompt="sys"
        )
    )
    assert outcome.candidates == []
    assert outcome.error and "回合用尽" in outcome.error


# ══════════════════════════════════════════════════════════
# ★★ v0.41：沙箱容器已消失 → 立刻中止
# ══════════════════════════════════════════════════════════
#
# 实测出处（2026-10-16「被偷走的文件」，MISC）：容器在第 24 轮的工具调用后
# 被 Docker 侧带走（`--rm` 生效，服务没重启、没有 sweep 记录），此后每条命令
# 都返回 `No such container`，而循环里**零处检测** —— 模型用 `echo hello` /
# `echo x1` / `echo probe_final` 一路探到第 63 轮才收尾，39 轮、约 62 万 token
# 换回一句 "The container has been persistently unavailable"。
#
# 下面三条钉住：① 死了就在下一轮开头停；② 判据认那个精确子串；
# ③ 反向 —— "Docker 没装"不算容器死。


def test_the_loop_gives_up_when_the_sandbox_cannot_be_revived() -> None:
    """容器已死 **且重建也失败** → 下一轮开头就返回，不再调 LLM。

    断言的核心不是"报了个错"，而是 `gateway.seen` 的长度：容器救不回来
    之后一次都不该再调模型 —— 放了的话，每次调用都是白花的 token。

    ⚠️ v0.43 起"容器死了"**不再**等于"立刻中止"：会先试着重建一个
    （见 `MAX_SANDBOX_REVIVALS`）。所以这个用例必须让 `start()` 抛异常，
    否则重建会成功、循环会接着跑 —— 那时它测的就不是"放弃"了。
    """
    sandbox = _FakeSandbox(start_error=runner.Ctf2SandboxError("docker 起不来"))
    sandbox.died = True
    gateway = _FakeGateway(['FINAL: {"candidates": []}'])

    outcome = _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    assert outcome.error and "沙箱已消失" in outcome.error
    # ★ 一次 LLM 调用都没有 —— 这是这条改动的全部价值所在。
    assert gateway.seen == []
    assert sandbox.commands == []
    # 试过**一次**重建才放弃：不能退化成"根本不试"，也不能变成每轮重试。
    assert sandbox.restarts == 1
    # 中止也要留下一个可读的轮次记录（题解与排查都靠它）。
    assert outcome.transcript and "沙箱已消失" in outcome.transcript


def test_a_lost_container_is_revived_and_the_solve_carries_on() -> None:
    """容器被外部移除 → 重建一个、告诉模型"这是新容器"、接着解题。

    ★ 这是 v0.43 的正题。2026-10-17「粗心的开发人员」跑到 turn 42、
    40 分钟，容器被外部清掉后整轮作废 —— 而模型当时的判断、已探明的
    目标结构、写在 `/work/persist` 里的笔记**全都还在上下文里**。
    一个新容器 + 一句"什么还在、什么没了"就能接着跑。

    ⚠️ 这条用例同时钉住三件事，少一件这个能力就是假的：
    （1）真的重建了；（2）重建后命令真的在新容器里跑了；
    （3）模型**被告知**了这件事 —— 不然它会去找一个已经不存在的文件，
        并把"不见了"当成自己记错，那种弯路比重新开始更贵。
    """
    sandbox = _FakeSandbox({"ls -la": "notes.txt 还在"})
    sandbox.died = True
    gateway = _FakeGateway(
        [
            'ACTION: {"tool": "run_shell", "args": {"command": "ls -la /work/persist"}}',
            'FINAL: {"candidates": []}',
        ]
    )

    outcome = _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    assert not outcome.error
    assert sandbox.restarts == 1
    assert sandbox.commands == ["ls -la /work/persist"]
    # 重建之后模型被接着问了（而不是默默中止）。
    assert len(gateway.seen) == 2

    first_call = "\n".join(str(m.get("content", "")) for m in gateway.seen[0])
    assert "沙箱" in first_call
    assert runner.PERSIST_DIR in first_call


def test_the_revival_budget_is_capped() -> None:
    """重建次数用完还是死的 → 中止，且**不会**无限重试。

    上限存在的理由与 `MAX_SANDBOX_REVIVALS` 的注释同一条：无限重建
    会让每一轮都撞在同一个已经救不回来的容器上、白烧 token —— 那正是
    v0.41 那条"一死就停"要防的浪费，只是换了个形式。

    模拟方式：每跑一条命令就把 `died` 重新置上，等价于"重建了但立刻又没了"。
    """
    sandbox = _FakeSandbox()
    sandbox.died = True
    original_shell = sandbox.shell

    def shell_then_die(command, *, timeout=300):  # noqa: ANN001, ANN202
        result = original_shell(command, timeout=timeout)
        sandbox.died = True
        return result

    sandbox.shell = shell_then_die  # type: ignore[method-assign]
    gateway = _FakeGateway(['ACTION: {"tool": "run_shell", "args": {"command": "ls"}}'] * 5)

    outcome = _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    assert outcome.error and "沙箱已消失" in outcome.error
    # ★ 钉字面量 2，而**不是** `== runner.MAX_SANDBOX_REVIVALS`。
    #   写后者的话，把常量改成 0（= 退化成"一死就停"）这条用例照样绿 ——
    #   它守的会变成"用尽就停"这个逻辑，而不是"真的试满了一次预算"。
    #   实测确认：这一行写成 `== MAX_SANDBOX_REVIVALS` 时，反向验证只红了
    #   一条用例，这条没红。改这个预算必须同时改这里，那是对的。
    assert sandbox.restarts == 2
    # ★ 每个"还活着的"轮次只调一次模型：预算用完之后不再空转。
    assert len(gateway.seen) == sandbox.restarts


def test_the_revival_note_says_what_survived_and_what_did_not() -> None:
    """说明必须同时讲清"没了"与"还在"，并给出先 `ls` 的下一步。

    只讲"重建好了"是不够的 —— 那等于把"环境变了"这件事瞒下来，
    让模型拿旧记忆去操作新容器。
    """
    note = runner._sandbox_revived_note(2)

    assert "2" in note  # 第几次重建
    assert runner.PERSIST_DIR in note  # 读写挂载：还在
    assert "没了" in note or "消失" in note
    assert "ls" in note


def test_the_sandbox_marks_itself_dead_when_the_container_is_gone(monkeypatch) -> None:
    """`exec()` 见到 `No such container` → `died = True`。"""
    sandbox = runner.SandboxContainer(name="ctf2-test-abc", image="suvex/ctf2-sandbox:test")
    assert sandbox.died is False

    def fake_run(argv, *, timeout):  # noqa: ANN001, ANN202
        return runner.CommandResult(
            1, "", "Error response from daemon: No such container: ctf2-test-abc"
        )

    monkeypatch.setattr(runner, "_run", fake_run)
    result = sandbox.exec(["ls"])

    assert result.exit_code == 1
    assert sandbox.died is True


def test_docker_being_unavailable_does_not_look_like_a_dead_container(monkeypatch) -> None:
    """★ 反向断言：`找不到可执行文件：docker` **不是**容器死了。

    这两件事必须分开：前者是宿主环境坏了（Docker 没装/不在 PATH 上），
    后者是这一个容器没了。混在一起的话，"Docker 没装"会伪装成
    "容器死了"，于是排查方向直接跑偏 —— 那正是 `died` 只认一个精确
    子串的理由。
    """
    sandbox = runner.SandboxContainer(name="ctf2-test-abc", image="suvex/ctf2-sandbox:test")

    def fake_run(argv, *, timeout):  # noqa: ANN001, ANN202
        return runner.CommandResult(127, "", f"找不到可执行文件：{argv[0]}")

    monkeypatch.setattr(runner, "_run", fake_run)
    sandbox.exec(["ls"])

    assert sandbox.died is False


def test_a_successful_exec_does_not_mark_the_sandbox_dead(monkeypatch) -> None:
    """★ 反向断言：成功的命令不该误标死。

    防止有人把判据写成 `if not result.ok:`（漏掉子串判断）—— 那样任何一条
    返回非零的普通命令（`grep` 没匹配到就是 exit 1）都会终止整次解题。
    """
    sandbox = runner.SandboxContainer(name="ctf2-test-abc", image="suvex/ctf2-sandbox:test")

    def fake_run(argv, *, timeout):  # noqa: ANN001, ANN202
        return runner.CommandResult(1, "", "grep: /work/x: No such file or directory")

    monkeypatch.setattr(runner, "_run", fake_run)
    sandbox.exec(["grep"])

    assert sandbox.died is False


# ══════════════════════════════════════════════════════════
# ★★ v0.41：`start()` 之后必须验活
# ══════════════════════════════════════════════════════════
#
# 实测（Kali，用 `_argv()` 的原参数、只把入口换成 `/bin/true`）：
# `docker run -d --rm` 在容器**秒退**时仍返回退出码 0 + 完整 64 位 ID，
# 2 秒后 `docker ps` 已经查不到它。所以 `result.ok` 只证明"Docker 接受
# 了请求"，不证明"这个容器在跑"。
#
# 代价：真实解题过程里四份"容器不见了"的现场有**三份是 turn 1 就死**，
# 那种情况下 `died` 检测一次都救不回来（它要到下一次工具调用才置位）。

_FAKE_ID = "bcff6db03babb2d1cef6074d5b8497bca107d86dc1315590c18e7db8a8fb3b56"


def _start_with(monkeypatch, inspect_result: runner.CommandResult) -> runner.SandboxContainer:
    """让 `docker run` 返回成功 + 64 位 ID，`docker inspect` 返回给定结果。"""

    def fake_run(argv, *, timeout):  # noqa: ANN001, ANN202
        if argv[:2] == ["docker", "inspect"]:
            return inspect_result
        if argv[:2] == ["docker", "version"]:
            return runner.CommandResult(0, "28.0.0", "")
        return runner.CommandResult(0, _FAKE_ID + "\n", "")

    monkeypatch.setattr(runner, "_run", fake_run)
    sandbox = runner.SandboxContainer(name="ctf2-test-abc", image="suvex/ctf2-sandbox:test")
    sandbox.start()
    return sandbox


def test_start_fails_when_the_container_vanished_right_after_run(monkeypatch) -> None:
    """★ `docker run` 报告成功，但容器随即消失 → `start()` 必须抛错。

    这是实测出来的形状：`--rm` 让秒退的容器在下一瞬间被删掉，于是
    `docker inspect` 直接查不到它。不拦的话，模型会拿十几轮去撞
    `No such container`。
    """
    import pytest

    inspect = runner.CommandResult(
        1, "", "Error: No such object: bcff6db03babb"
    )
    with pytest.raises(runner.Ctf2SandboxError) as exc:
        _start_with(monkeypatch, inspect)

    message = str(exc.value)
    assert "docker run 报告成功" in message
    # 排查靠这两样：run 给的 ID（对照用）+ inspect 的原始报错。
    assert _FAKE_ID[:12] in message
    assert "No such object" in message


def test_start_fails_when_the_container_exists_but_is_not_running(monkeypatch) -> None:
    """★ 容器在、但 `Running=false` → 抛错，且消息带上 `.State` 四个字段。

    秒退的容器在 `--rm` 之前那一瞬就是这个形态。`Status`/`ExitCode`/
    `OOMKilled`/`Error` 这四个字段是排查"为什么秒退"的全部线索 ——
    少了任何一个，"被 OOM 杀"和"入口自己崩了"在外部看起来一模一样。
    """
    import pytest

    inspect = runner.CommandResult(0, "false|exited|137|true|something broke\n", "")
    with pytest.raises(runner.Ctf2SandboxError) as exc:
        _start_with(monkeypatch, inspect)

    message = str(exc.value)
    assert "没在跑" in message
    for field in ("status=exited", "exit=137", "oom=true", "error=something broke"):
        assert field in message, f"错误消息里缺 {field}，排查时就没有线索了"


def test_start_succeeds_when_the_container_really_is_running(monkeypatch) -> None:
    """★ 反向断言：`Running=true` 时正常返回，且 `container_id` 被记下。

    防的是"验活写死成永远失败" —— 那种改法所有用例都绿，但一道题也解不出来。
    """
    sandbox = _start_with(monkeypatch, runner.CommandResult(0, "true|running|0|false|\n", ""))

    assert sandbox.container_id == _FAKE_ID
    assert sandbox._started is True


def test_the_liveness_probe_asks_by_container_id_not_by_name(monkeypatch) -> None:
    """★ 验活必须按**容器 ID** 查，不能按名字。

    名字是外部可占用的资源：9-29 那次
    `Conflict. The container name "/ctf2-Q14RPF0NEVT2-1603" is already in use`
    就是实例 —— 按名字查有可能查到**另一个**容器，于是把死容器判成活。
    """
    seen: list[list[str]] = []

    def fake_run(argv, *, timeout):  # noqa: ANN001, ANN202
        seen.append(list(argv))
        if argv[:2] == ["docker", "inspect"]:
            return runner.CommandResult(0, "true|running|0|false|\n", "")
        if argv[:2] == ["docker", "version"]:
            return runner.CommandResult(0, "28.0.0", "")
        return runner.CommandResult(0, _FAKE_ID + "\n", "")

    monkeypatch.setattr(runner, "_run", fake_run)
    sandbox = runner.SandboxContainer(name="ctf2-test-abc", image="suvex/ctf2-sandbox:test")
    sandbox.start()

    probe = next(call for call in seen if call[:2] == ["docker", "inspect"])
    assert probe[2] == _FAKE_ID, "存活校验必须用容器 ID，不能用 name"
    assert "ctf2-test-abc" not in probe


# ══════════════════════════════════════════════════════════
# ★★ v0.42：一轮多个 ACTION 全部执行（v0.41 的"只跑第一个"被推翻）
# ══════════════════════════════════════════════════════════
#
# 实测：66 份解题过程里 **271 轮（14.2%）** 一轮写了 ≥2 个 ACTION（极端一轮
# 23 个）。v0.41 的做法是"只解析第一个 + 提醒模型"，但没有用 ——
# 模型下一轮照样批量写（它想批量，是我们按住了它），而那一句提醒
# 每轮都要占上下文。v0.42 改成**串行执行**：轮次仍算 1 轮，
# 有效动作 ×N。


def test_a_turn_with_several_actions_runs_them_all() -> None:
    """★ v0.42：一轮写 3 个 ACTION → **三个都执行**，而这一轮仍算 1 轮。"""
    sandbox = _FakeSandbox()
    gateway = _FakeGateway(
        [
            (
                'ACTION: {"tool": "run_shell", "args": {"command": "cmd-a"}}\n'
                'ACTION: {"tool": "run_shell", "args": {"command": "cmd-b"}}\n'
                'ACTION: {"tool": "run_shell", "args": {"command": "cmd-c"}}'
            ),
            'FINAL: {"candidates": []}',
        ]
    )

    _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    # ★ 三条**都执行了**，且顺序与模型写的顺序一致。
    assert sandbox.commands == ["cmd-a", "cmd-b", "cmd-c"]

    fed_back = gateway.seen[1][-1]["content"]
    # 抬头要说清"写了几个、跑了几个" —— 两个数字必须对得上。
    assert "你写了 3 个 ACTION" in fed_back
    assert "执行了 3 个" in fed_back
    # 每条输出各占一块，否则模型分不清哪段是哪个命令的结果。
    assert fed_back.count("### run_shell：") == 3

    # ★ 轮次没有因为批量而增加：第 2 次调用就是收尾轮。
    assert len(gateway.seen) == 2


def test_a_turn_with_one_action_gets_no_such_reminder() -> None:
    """★ 反向断言：只写 1 个 ACTION 时，回灌内容与批量之前**逐字符相同**。

    否则每轮都多一句抬头，而噪音会挤占上下文 —— 那正是这条改动想治的病。
    这条断言同时钉住了"单条路径没有被批量改动影响"。
    """
    sandbox = _FakeSandbox()
    gateway = _FakeGateway(
        [
            'ACTION: {"tool": "run_shell", "args": {"command": "cmd-a"}}',
            'FINAL: {"candidates": []}',
        ]
    )

    _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    fed_back = gateway.seen[1][-1]["content"]
    assert "个 ACTION" not in fed_back
    assert "###" not in fed_back
    # 老的形状原样保留（提示词里就是这么教它的）。
    assert fed_back.startswith("工具 `run_shell` 的输出：")


def test_parse_actions_keeps_every_action_in_order() -> None:
    """`parse_actions` 按出现顺序返回**全部** ACTION，不封顶。"""
    text = (
        '先看看目录。\n'
        'ACTION: {"tool": "run_shell", "args": {"command": "ls /work"}}\n'
        '再读一眼附件。\n'
        'ACTION: {"tool": "read_attachment", "args": {"path": "a.txt"}}'
    )

    assert runner.parse_actions(text) == [
        ("run_shell", {"command": "ls /work"}),
        ("read_attachment", {"path": "a.txt"}),
    ]


def test_parse_actions_skips_a_broken_block_but_keeps_the_rest() -> None:
    """★ 中间一个坏 JSON 不能带走后面的 —— 旧解析器会整段放弃。"""
    text = (
        'ACTION: {"tool": "run_shell", "args": {"command": "cmd-a"}}\n'
        'ACTION: {"tool": "run_shell", "args": {broken json\n'
        'ACTION: {"tool": "run_shell", "args": {"command": "cmd-c"}}'
    )

    assert runner.parse_actions(text) == [
        ("run_shell", {"command": "cmd-a"}),
        ("run_shell", {"command": "cmd-c"}),
    ]


def test_parse_action_still_returns_the_first_one() -> None:
    """`parse_action` 是 `parse_actions` 的兼容包装：内容不变。"""
    text = (
        'ACTION: {"tool": "run_shell", "args": {"command": "cmd-a"}}\n'
        'ACTION: {"tool": "run_shell", "args": {"command": "cmd-b"}}'
    )

    assert runner.parse_action(text) == ("run_shell", {"command": "cmd-a"})
    assert runner.parse_actions("") == []


def test_a_turn_with_too_many_actions_stops_at_the_cap_and_says_so() -> None:
    """★ 一轮超过 5 个 ACTION → 只跑前 5 个，且**明说**最后几个没执行。

    不说的话模型会按 7 份输出继续推理，而它手里只有 5 份 —— 这正是
    v0.41 想治而没治到的病。
    """
    # ★ 写死 5 而不是读 `runner.MAX_ACTIONS_PER_TURN`：动态参照被测常量
    #   会让"有人把上限改成 1"这种破坏**测不出来**（反证验证时实测踩到）。
    #   常量本身有设计理由（串行执行 + `run_shell` 单条 300 秒超时），
    #   要改就得连着这里一起改，那就应该有人想一想。
    cap = 5
    assert cap == runner.MAX_ACTIONS_PER_TURN, "上限被改了 —— 请同步这个测试的期望"
    total = 7
    actions = "\n".join(
        f'ACTION: {{"tool": "run_shell", "args": {{"command": "cmd-{index}"}}}}'
        for index in range(total)
    )
    sandbox = _FakeSandbox()
    gateway = _FakeGateway([actions, 'FINAL: {"candidates": []}'])

    _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    assert sandbox.commands == [f"cmd-{index}" for index in range(cap)]

    fed_back = gateway.seen[1][-1]["content"]
    assert f"你写了 {total} 个 ACTION" in fed_back
    assert f"执行了 {cap} 个" in fed_back
    assert "没有执行" in fed_back
    assert "最后 2 个" in fed_back


def test_the_evidence_pool_keeps_what_the_transcript_drops() -> None:
    """★ `SolveOutcome.tool_outputs` 保留的比 `transcript` 多。

    这是第三道闸（`answer_is_grounded`）的数据来源。`transcript` 里的
    result 是 `_clip(…, 2000)` 之后的（**只留头**），所以"flag 落在
    2000 字符之后"的题会被误判成"没出现过" —— 白白 hold 掉真候选。

    实测这条链上有两层剪：`dispatch_tool` 自己先剪到 8000 左右
    （用 `_clip_both_ends`，**保尾**，所以末尾的 flag 还在），
    `transcript` 再剪到 2000（只留头，flag 就没了）。
    """
    long_output = "A" * 9000 + " flag{deep}"
    sandbox = _FakeSandbox(outputs={"cat": long_output})
    gateway = _FakeGateway(
        [
            'ACTION: {"tool": "run_shell", "args": {"command": "cat /flag"}}',
            'FINAL: {"candidates": []}',
        ]
    )

    outcome = _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    assert outcome.tool_outputs, "★ 跑过工具却没有留下原始输出"
    recorded = "\n".join(outcome.tool_outputs)
    # ★ 核心断言：证据池里有那个 flag，transcript 里没有。
    assert "flag{deep}" in recorded
    assert "flag{deep}" not in outcome.transcript, "★ transcript 居然保住了尾部（判据该换）"
    # 回灌给模型的版本也被剪（否则上下文撑爆）—— 与证据池是两件事。
    fed_back = gateway.seen[1][-1]["content"]
    assert "已截断" in fed_back


# ══════════════════════════════════════════════════════════
# ★★ v0.41.1：重复执行做成可见信号（判据是"命令 + 整段输出"双指纹）
# ══════════════════════════════════════════════════════════
#
# 判据 = 工具名 + 参数序列化 + **整段输出**指纹，三者都相同才算重复；比对
# 范围是**本次解题全程**，不是只比上一轮。
#
# 旧方案（只看输出前 400 字符、只比相邻轮）两头都错，数据见
# `solve_in_sandbox` 里那段注释：偏高（HTML 模板前 400 相同、真正结果在
# 第 705 字符）与偏低（同命令同输出跨多轮，全都不相邻）。下面这几条分别
# 钉住两个方向。


class _ShiftySandbox(_FakeSandbox):
    """同一命令每次返回**不同**输出 —— 用来确认"拿到新信息不算空转"。"""

    def shell(self, command: str, *, timeout: int = 300):  # noqa: ANN001, ANN201
        self.commands.append(command)
        nth = sum(1 for existing in self.commands if existing == command)
        return runner.CommandResult(0, f"第 {nth} 次执行的结果", "")


class _HtmlPageSandbox(_FakeSandbox):
    """同一命令：前 400 字符是同一份页面模板，**真正结果在末尾**。

    照抄实测里的那个形状：`ctf2chal_01M3SF0MAZPNSARD7CWECXYWPC
    -20261001-020127.txt` turn 9→10，同一 URL 同一 POST，前 400 字符是
    同一份 `<div class="container">`，真正的结论在第 705 字符。
    """

    def shell(self, command: str, *, timeout: int = 300):  # noqa: ANN001, ANN201
        self.commands.append(command)
        nth = sum(1 for existing in self.commands if existing == command)
        template = '<div class="container"><div class="row"><p>提交结果：' * 20
        return runner.CommandResult(0, template + ("操作失败。" if nth == 1 else "操作成功。"), "")


def test_the_same_command_with_the_same_output_is_reported_with_its_turn() -> None:
    """同命令同输出 → 提示语里带上**第一次**跑它的轮次号。

    "你在第 N 轮跑过"比"换方向"有用：模型能回去看那一轮的上下文，
    而不是笼统地知道"重复了"。
    """
    sandbox = _FakeSandbox(outputs={"probe": "一模一样的输出" * 100})
    gateway = _FakeGateway(
        [
            'ACTION: {"tool": "run_shell", "args": {"command": "probe-1"}}',
            'ACTION: {"tool": "run_shell", "args": {"command": "probe-1"}}',
            'FINAL: {"candidates": []}',
        ]
    )

    _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    # 第 1 轮那次不该有提醒；第 2 轮把第 1 轮的结果喂回去时才有。
    assert "跑过" not in gateway.seen[1][-1]["content"]
    assert "第 1 轮跑过" in gateway.seen[2][-1]["content"]


def test_a_repeat_is_caught_even_when_it_is_not_adjacent() -> None:
    """★ 中间隔了一轮也要抓到 —— 旧实现只比上一轮，这里必然漏。

    实测出处：`ctf2chal_01M3SF0MAH4V6RKMS4BJ74ZSEK-20261002-101250.txt`
    turn 3→12、3→17、5→30 反复打同一 POP 链 payload，2032 字节完全一致，
    相邻一次都没挨着。
    """
    sandbox = _FakeSandbox(outputs={"probe-a": "同样的输出" * 80})
    gateway = _FakeGateway(
        [
            'ACTION: {"tool": "run_shell", "args": {"command": "probe-a"}}',
            'ACTION: {"tool": "run_shell", "args": {"command": "probe-b"}}',
            'ACTION: {"tool": "run_shell", "args": {"command": "probe-a"}}',
            'FINAL: {"candidates": []}',
        ]
    )

    _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    assert "第 1 轮跑过" in gateway.seen[3][-1]["content"]


def test_the_same_command_with_a_different_output_is_not_a_repeat() -> None:
    """★ 反向断言：同命令但输出变了 → **不**警示（那有信息量）。

    这条守的是"偏高"那一侧：同一条 `curl` 打两次，第二次页面变了，
    那正是模型要的反馈。提示它"重复了，换方向"会把它从正确的路上赶走。
    """
    sandbox = _ShiftySandbox()
    gateway = _FakeGateway(
        [
            'ACTION: {"tool": "run_shell", "args": {"command": "probe"}}',
            'ACTION: {"tool": "run_shell", "args": {"command": "probe"}}',
            'FINAL: {"candidates": []}',
        ]
    )

    _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    for seen in gateway.seen:
        assert "跑过" not in seen[-1]["content"]


def test_a_different_command_with_the_same_output_is_not_a_repeat() -> None:
    """★ 反向断言：命令不同、输出恰好一样 → **不**警示。

    旧实现只比输出，于是 `ls` 与 `find .` 拿到同一份清单会被判成重复。
    命令本身变了就说明模型在换角度，不该拦。
    """
    sandbox = _FakeSandbox(outputs={"probe": "一模一样的输出" * 100})
    gateway = _FakeGateway(
        [
            'ACTION: {"tool": "run_shell", "args": {"command": "probe-1"}}',
            'ACTION: {"tool": "run_shell", "args": {"command": "probe-2"}}',
            'FINAL: {"candidates": []}',
        ]
    )

    _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    for seen in gateway.seen:
        assert "跑过" not in seen[-1]["content"]


def test_a_repeat_with_the_same_first_400_chars_but_a_different_tail_is_not_a_repeat() -> None:
    """★★ 反向断言：前 400 字符相同、后面不同 → **不**警示。

    这条钉的是旧方案"偏高"那一侧，也是上一版反向验证漏掉的洞：把判据
    换成"只看前 400 字符"时，如果语料里没有"前 400 相同而整段不同"的
    形状，那个破坏版照样能跑绿 —— 用例就守不住它本要守的东西。

    实测出处：`ctf2chal_01M3SF0MAZPNSARD7CWECXYWPC-20261001-020127.txt`
    turn 9→10，同一 URL 同一 POST，前 400 是同一份 `<div class="container">`
    模板，真正结果在第 705 字符：turn9「操作失败。」turn10「操作成功。」
    模型拿到了新信息 —— 那时提示"重复了、换方向"会把一次成功的探测说成
    白跑，把它从正确的路上赶走。
    """
    sandbox = _HtmlPageSandbox()
    gateway = _FakeGateway(
        [
            'ACTION: {"tool": "run_shell", "args": {"command": "post"}}',
            'ACTION: {"tool": "run_shell", "args": {"command": "post"}}',
            'FINAL: {"candidates": []}',
        ]
    )

    _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    for seen in gateway.seen:
        assert "跑过" not in seen[-1]["content"]


def test_the_first_tool_call_never_carries_a_repeat_note() -> None:
    """反向断言：首轮没有历史可查，别凭空说"你重复了"。"""
    sandbox = _FakeSandbox(outputs={"probe": "随便什么输出"})
    gateway = _FakeGateway(
        [
            'ACTION: {"tool": "run_shell", "args": {"command": "probe"}}',
            'FINAL: {"candidates": []}',
        ]
    )

    _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    assert "跑过" not in gateway.seen[1][-1]["content"]


def test_the_output_fingerprint_ignores_whitespace_differences() -> None:
    """指纹要吃掉空白差异：同一份输出换行/缩进变了还是同一条。"""
    assert runner._output_fingerprint("a  b\n\nc\t d") == runner._output_fingerprint("a b c d")
    assert runner._output_fingerprint("a b") != runner._output_fingerprint("a b c")
    # `None` 不该炸（`result` 理论上是 str，但假沙箱与某些分支会给 None）
    assert runner._output_fingerprint(None) == runner._output_fingerprint("")


def test_the_repeat_signal_never_reaches_the_transcript() -> None:
    """★ 反向断言：那句提醒**只**进给模型看的 `recent`。

    它不能进 `log`（落盘 transcript，题解正文的原料）、不能进纪要、不发事件。
    漏了这一条，题解里会混进一堆系统提示 —— 而那正是用户会读到的正文。
    """
    sandbox = _FakeSandbox(outputs={"probe": "一模一样的输出" * 100})
    gateway = _FakeGateway(
        [
            'ACTION: {"tool": "run_shell", "args": {"command": "probe"}}',
            'ACTION: {"tool": "run_shell", "args": {"command": "probe"}}',
            'FINAL: {"candidates": []}',
        ]
    )
    events: list[tuple[str, str, dict]] = []
    outcome = _run(
        runner.solve_in_sandbox(
            _spec(),
            sandbox=sandbox,
            gateway=gateway,
            system_prompt="sys",
            on_progress=lambda k, s, p: events.append((k, s, p)),
        )
    )

    # 信号确实送到了模型
    assert "第 1 轮跑过" in gateway.seen[2][-1]["content"]
    # 但没进落盘的题解
    assert "跑过" not in (outcome.transcript or "")
    # 也没进事件流
    assert all("跑过" not in s and "跑过" not in json.dumps(p, ensure_ascii=False)
               for _k, s, p in events)


# ══════════════════════════════════════════════════════════
# ★★ v0.41.2：输出被长度上限切断时，不能当成"格式写错了"
# ══════════════════════════════════════════════════════════
#
# 实测：`ctf2try_01M4Z94FND98QAC7T513ZTPM89`（78 轮 unsolved）里有 22 轮
# `completion_tokens >= 4096`。输出到顶 → ACTION 被切在中间 →
# `parse_action` 返回 None → 喂 `_format_reminder()`（"请严格用下面两种
# 格式之一"）。它照做、再被切、再照做，22 次。
#
# ⚠️ 别把"输出到顶"写成"推理吃满额度"：`reasoning_tokens` 恒为 0 是网关
# 不上报这个字段，不是模型没推理（同一响应里 `reasoning_content` 有 6412
# 字符）。`completion_tokens` 包含推理部分，但分不出来。
#
# 关键是"格式错"与"被切断"的解决办法相反：前者要重写，后者重写没用。


def test_a_truncated_reply_gets_the_truncation_reminder_not_the_format_one() -> None:
    """★ 被切断且没有动作 → 喂 `_truncation_reminder`，不是 `_format_reminder`。

    反向断言（不含格式模板那两句）是关键：只证"含被切断"不够 ——
    两个都喂（比如先格式、再补一句）也能满足它，而那样模型的注意力
    还是在格式上。
    """
    sandbox = _FakeSandbox()
    gateway = _FakeGateway(
        [
            "我正在仔细分析这个上传点……（话说到一半）",
            'FINAL: {"candidates": []}',
        ],
        finish_reasons=["length", "stop"],
    )

    _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    fed_back = gateway.seen[1][-1]["content"]
    assert "达到输出长度上限被切断" in fed_back
    assert "先只写 ACTION 那一行" in fed_back
    # 反向：**不是**格式提醒
    assert "请**严格**用下面两种格式之一" not in fed_back


def test_a_normal_badly_formatted_reply_still_gets_the_format_reminder() -> None:
    """★ 反向断言：没被切断的格式错误 → 仍然走原来那句，不含"被切断"。"""
    sandbox = _FakeSandbox()
    gateway = _FakeGateway(
        [
            "这一轮我随便说点什么都不做。",
            'FINAL: {"candidates": []}',
        ],
        finish_reasons=["stop", "stop"],
    )

    _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    fed_back = gateway.seen[1][-1]["content"]
    assert "请**严格**用下面两种格式之一" in fed_back
    assert "被切断" not in fed_back


def test_an_unknown_finish_reason_is_treated_as_not_truncated() -> None:
    """★ 反向断言：接入点没给 `finish_reason`（空串）→ 当作没被切断。

    宁可漏报：在一条正常回复上说"你被切断了"会让模型无缘无故压缩推理，
    把它本该想清楚的事想浅。
    """
    sandbox = _FakeSandbox()
    gateway = _FakeGateway(
        [
            "这一轮我随便说点什么都不做。",
            'FINAL: {"candidates": []}',
        ]
    )

    _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    assert "被切断" not in gateway.seen[1][-1]["content"]


def test_a_truncated_reply_whose_action_survived_tells_the_model_the_tail_was_lost() -> None:
    """切断点在动作**之后**：JSON 完好、动作照跑，但末尾那段推理丢了。

    不告诉它，它会以为自己那轮已经想完了（而"下一步该干什么"恰好
    就丢在被切掉的那段里）。
    """
    sandbox = _FakeSandbox(outputs={"ls": "hint.txt"})
    gateway = _FakeGateway(
        [
            'ACTION: {"tool": "run_shell", "args": {"command": "ls -la /work"}}\n'
            "接下来我打算……（被切了）",
            'FINAL: {"candidates": []}',
        ],
        finish_reasons=["length", "stop"],
    )

    _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    # 动作照常执行了
    assert sandbox.commands == ["ls -la /work"]
    fed_back = gateway.seen[1][-1]["content"]
    assert "hint.txt" in fed_back
    assert "末尾内容丢失" in fed_back
    # 这一条**不该**出现"先只写 ACTION"——动作已经完整了，那句是给
    # "连动作都没写完"的情形用的。
    assert "先只写 ACTION 那一行" not in fed_back


def test_the_anthropic_stop_reason_max_tokens_is_also_recognised() -> None:
    """Anthropic 口径叫 `max_tokens`，OpenAI 叫 `length` —— 两个都得认。

    只认一个的后果：换到另一家接入点时这条提醒**静默失效**，而那正是
    这个功能最需要生效的场景（换模往往就是因为原接入点不顺手）。
    """
    sandbox = _FakeSandbox()
    gateway = _FakeGateway(
        [
            "我正在仔细分析……（话说到一半）",
            'FINAL: {"candidates": []}',
        ],
        finish_reasons=["max_tokens", "stop"],
    )

    _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    assert "达到输出长度上限被切断" in gateway.seen[1][-1]["content"]


def test_the_digest_marks_a_truncated_turn() -> None:
    """被切断的轮次在纪要里带 `⚠️输出被截断` 标记。

    纪要是"这一轮做过什么"的唯一长期记录（完整对话会被裁掉）。不标记的话，
    一个话说到一半的回合与一个完整回合在纪要里长得一模一样。
    """
    sandbox = _FakeSandbox(outputs={"ls": "hint.txt"})
    gateway = _FakeGateway(
        [
            'ACTION: {"tool": "run_shell", "args": {"command": "ls -la /work"}}',
            'ACTION: {"tool": "run_shell", "args": {"command": "cat /work/hint.txt"}}',
            'FINAL: {"candidates": []}',
        ],
        finish_reasons=["length", "stop", "stop"],
    )

    _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    # 最后一轮发给模型的消息里带着纪要：第 1 轮被标记、第 2 轮没有。
    final_view = gateway.seen[-1]
    digest_text = "\n".join(
        msg["content"] for msg in final_view if msg["role"] == "user"
    )
    assert "⚠️输出被截断" in digest_text
    # 只有一行带标记 —— 第 2 轮的 `finish_reason` 是 stop。
    assert digest_text.count("⚠️输出被截断") == 1


def test_the_digest_carries_no_truncation_mark_on_normal_turns() -> None:
    """★ 反向断言：没被切断的轮次，纪要里一个标记都不该有。"""
    sandbox = _FakeSandbox(outputs={"ls": "hint.txt"})
    gateway = _FakeGateway(
        [
            'ACTION: {"tool": "run_shell", "args": {"command": "ls -la /work"}}',
            'FINAL: {"candidates": []}',
        ],
        finish_reasons=["stop", "stop"],
    )

    outcome = _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    digest_text = "\n".join(
        msg["content"] for msg in gateway.seen[-1] if msg["role"] == "user"
    )
    assert "⚠️输出被截断" not in digest_text
    assert "被切断" not in digest_text
    assert "被切断" not in (outcome.transcript or "")


def test_the_gateway_actually_carries_finish_reason_into_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ 端到端：`LlmGateway.chat()` 真的把 `finish_reason` 填进 `LlmReply`。

    ## 为什么必须有这一条

    只测 `_finish_reason(parsed)` 那个纯函数**不够** —— 反向验证抓到过：
    把 `chat()` 里那句 `finish_reason=_finish_reason(parsed)` 删掉（改成
    永远传 `""`），七条 D-6 用例**全绿**。因为它们全都手工构造
    `LlmReply(finish_reason=...)`，那条"从响应体读出来"的线一次都没被走。

    这正是这个项目里反复踩到的形状：**用手造的对象验证一条真实路径**。
    这条用例走真 `LlmGateway`，只把网络层换成 `MockTransport`。
    """
    from app.ctf2 import runner as r

    body = {
        "choices": [
            {
                "finish_reason": "length",
                "message": {"content": "ACTION: {\"tool\": \"run_shell\""},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4096, "total_tokens": 4106},
    }

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json=body)

    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):  # noqa: ANN001, ANN202
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(r.httpx, "AsyncClient", factory)

    gateway = r.LlmGateway(
        model="m", api_key="sk-test-not-real", base_url="https://api.example.com"
    )
    reply = _run(gateway.chat([{"role": "user", "content": "go"}], system="sys"))

    assert reply.finish_reason == "length"
    # 顺带确认这条路径上 usage 也照旧被读出来（同一批"从响应体取值"的活）。
    assert reply.usage.completion_tokens == 4096


def test_the_gateway_reads_the_anthropic_stop_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """同上，Anthropic 口径：`stop_reason` 而不是 `choices[].finish_reason`。"""
    from app.ctf2 import runner as r

    body = {
        "content": [{"type": "text", "text": "ACTION: {\"tool\": \"run_shell\""}],
        "stop_reason": "max_tokens",
    }

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json=body)

    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):  # noqa: ANN001, ANN202
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(r.httpx, "AsyncClient", factory)

    gateway = r.LlmGateway(
        model="claude", api_key="sk-test-not-real", base_url="https://api.anthropic.com"
    )
    # `anthropic` 不是构造参数 —— 它由地址推出来（`is_anthropic(endpoint)`）。
    # 先确认这条用例真的走到了 Anthropic 那条线上，否则下面的断言在
    # OpenAI 分支上也成立，等于没测。
    assert gateway.anthropic is True
    reply = _run(gateway.chat([{"role": "user", "content": "go"}], system="sys"))

    assert reply.finish_reason == "max_tokens"


def test_finish_reason_is_read_from_both_wire_formats() -> None:
    """`_finish_reason` 两套口径都要认，取不到就返回空串（不抛）。"""
    # OpenAI 口径
    assert runner._finish_reason(
        {"choices": [{"finish_reason": "length", "message": {"content": "x"}}]}
    ) == "length"
    # Anthropic 口径
    assert runner._finish_reason({"stop_reason": "max_tokens"}) == "max_tokens"
    # 正常结束
    assert runner._finish_reason({"choices": [{"finish_reason": "stop"}]}) == "stop"
    # 缺字段 / 形状不对 → 空串，一律当作没被切断
    assert runner._finish_reason({}) == ""
    assert runner._finish_reason(None) == ""
    assert runner._finish_reason("not-a-dict") == ""
    assert runner._finish_reason({"choices": []}) == ""
    assert runner._finish_reason({"choices": "nope"}) == ""
    assert runner._finish_reason({"choices": [{}]}) == ""
    assert runner._finish_reason({"choices": [{"finish_reason": None}]}) == ""
    assert runner._finish_reason({"stop_reason": 42}) == ""


def test_the_digest_keeps_early_actions_after_they_leave_the_window() -> None:
    """★★ 被裁掉的回合**不能凭空消失** —— 它们要以纪要的形式留下。

    这是"跑满 40 轮却解不出题"的真正病灶（实测出处：用户截图里
    `[FlareOn3]GUI` 与 `WEB5[中等]` 都是「回合用尽（40 轮）仍未给出结论」）。

    原来历史是 `[题目] + 最近 24 条`，于是**第 25 轮开始模型就不知道
    前 24 轮做过什么**，接着重复跑同一条命令，一路空转到预算耗尽 ——
    而日志上看起来像"模型太笨"。

    所以这里跑 30 轮（超过 `MAX_HISTORY_TURNS`），断言第 1 轮那条命令
    **仍然出现在最后一轮发给模型的消息里**（在纪要中）。
    """
    sandbox = _FakeSandbox()
    replies = [
        f'ACTION: {{"tool": "run_shell", "args": {{"command": "cmd-{i}"}}}}'
        for i in range(1, 31)
    ]
    gateway = _FakeGateway(replies)

    outcome = _run(
        runner.solve_in_sandbox(
            _spec(max_turns=30), sandbox=sandbox, gateway=gateway, system_prompt="sys"
        )
    )
    assert outcome.error and "回合用尽" in outcome.error

    last = "\n".join(m["content"] for m in gateway.seen[-1])
    assert "cmd-1" in last, "第 1 轮的动作应该还在纪要里，不该被裁掉"
    assert "已经执行过的动作" in last
    # ★ 光给清单还不够：不说"别重复跑"，模型会把它当背景资料然后继续跑同一条。
    assert "不要重复执行" in last

    # 而最近几轮的**完整内容**仍然在（纪要不该取代它）。
    assert "工具 `run_shell` 的输出" in last


def test_the_recent_window_is_still_bounded() -> None:
    """纪要不能变成"什么都留着" —— 完整内容仍然只有最近 N 条。

    少了这条，上面那条可以通过"干脆不裁剪"满足，而那会让上下文溢出，
    症状是模型开始忘记题目要求（而不是报一个错）。
    """
    sandbox = _FakeSandbox()
    replies = [
        f'ACTION: {{"tool": "run_shell", "args": {{"command": "cmd-{i}"}}}}'
        for i in range(1, 31)
    ]
    gateway = _FakeGateway(replies)
    _run(
        runner.solve_in_sandbox(
            _spec(max_turns=30), sandbox=sandbox, gateway=gateway, system_prompt="sys"
        )
    )

    sent = gateway.seen[-1]
    # 三段：题目 + 纪要 + 最近 N 条。
    assert len(sent) <= 1 + 1 + runner.MAX_HISTORY_TURNS * 2 + 1


def test_the_final_stretch_note_arrives_before_the_budget_runs_out() -> None:
    """★ 只剩几轮时提醒模型收口。

    没有它的时候，模型会一路探索到最后一轮然后"回合用尽" —— 而它其实
    **早就掌握足够信息**了，只是一直在找"更确定"的证据。

    ⚠️ 提醒里**不能**鼓励猜：自动提交模式下，一个猜出来的 flag 会被直接
    提交并消耗一次错误提交机会，而那是不可再生资源。
    """
    sandbox = _FakeSandbox()
    gateway = _FakeGateway(
        ['ACTION: {"tool": "run_shell", "args": {"command": "true"}}'] * 10
    )
    _run(
        runner.solve_in_sandbox(
            _spec(max_turns=8), sandbox=sandbox, gateway=gateway, system_prompt="sys"
        )
    )

    # `max_turns=8` → 第 3 轮的 remaining 正好是 5。
    third = "\n".join(m["content"] for m in gateway.seen[2])
    assert "只剩 5 轮" in third
    assert "不要在最后几轮开新的探索方向" in third
    # 措辞里要有"说清卡点"这条出路，否则模型只剩"硬猜"一个选择。
    assert "卡在哪一步" in third

    # 第一轮不该有它 —— 否则模型从一开始就不敢深入探索。
    first = "\n".join(m["content"] for m in gateway.seen[0])
    assert "只剩" not in first


def test_the_final_stretch_note_is_inserted_only_once() -> None:
    """收尾提醒只**插入**一次 —— 但它会一直留在上下文里直到结束。

    ★ 这两件事看起来矛盾，其实是同一个设计的两个面：

    * 只插入一次（`remaining == FINAL_STRETCH_TURNS` 那个判断）——
      否则每一轮都塞一条新的，上下文里会堆六份同样的话，
      而模型对重复内容会迅速失去注意力；
    * 插入后**留在历史里** —— 收口是最后几轮**持续**要做的事，
      只在那一轮说一次，模型下一轮就忘了。

    所以这里断言的是"上下文里**只有一条**这样的消息"，
    而不是"只有一轮带着它"（后者会把正确行为判成 bug）。
    """
    sandbox = _FakeSandbox()
    gateway = _FakeGateway(
        ['ACTION: {"tool": "run_shell", "args": {"command": "true"}}'] * 12
    )
    _run(
        runner.solve_in_sandbox(
            _spec(max_turns=10), sandbox=sandbox, gateway=gateway, system_prompt="sys"
        )
    )

    for index, messages in enumerate(gateway.seen):
        notes = [m for m in messages if "只剩" in m["content"]]
        if index < 4:
            assert not notes, f"第 {index + 1} 轮不该有收尾提醒"
        else:
            assert len(notes) == 1, (
                f"第 {index + 1} 轮上下文里应该恰好一条收尾提醒，实际 {len(notes)} 条"
            )


def test_default_turn_budget_is_larger_than_the_history_window() -> None:
    """★★ 两个常量必须配套 —— 这条挡住"把轮数调大但忘了纪要"的回归。

    旧的组合是 `max_turns=40` + `history=24`：后 16 轮模型在失忆状态下空转。
    只把 40 调大、而没有纪要的话，那 80 轮里的后 56 轮全是浪费 ——
    烧的是真的钱。
    """
    assert runner.MAX_HISTORY_TURNS < runner.DEFAULT_MAX_TURNS
    assert runner.MAX_DIGEST_LINES >= runner.MAX_HISTORY_TURNS
    assert runner.FINAL_STRETCH_TURNS < runner.DEFAULT_MAX_TURNS


# ══════════════════════════════════════════════════════════
# 续解（v0.26）
# ══════════════════════════════════════════════════════════

TRANSCRIPT_SAMPLE = (
    "[turn 1] assistant(content): 先看看有什么。\n"
    '[turn 1] tool run_shell {"command": "ls -la /work/attachments"}\n'
    "[turn 1] result: total 8 / -rw-r--r-- 1 root root 42 hint.txt\n"
    "[turn 2] assistant(content): 读它。\n"
    '[turn 2] tool read_attachment {"path": "/work/attachments/hint.txt"}\n'
    "[turn 2] result: 提示：admin/admin\n"
)


def test_digest_from_transcript_keeps_the_commands() -> None:
    """★ 从上次的过程文件里还原"做过什么"。

    这是续解的基础：一次 40 轮的过程文件有 60–115 KB，而其中绝大部分
    是工具的**原始输出**。直接塞回上下文会把预算吃光（然后模型又开始
    忘记题目要求）—— 值得留下的只有"跑过什么、输出开头是什么"。
    """
    lines = runner.digest_from_transcript(TRANSCRIPT_SAMPLE)
    assert len(lines) == 2
    assert "run_shell" in lines[0]
    assert "ls -la /work/attachments" in lines[0]
    assert "hint.txt" in lines[0]
    assert "read_attachment" in lines[1]


def test_digest_from_transcript_is_one_line_per_turn() -> None:
    """一行一条 —— 工具输出里的换行必须被压掉。

    不压的话，一份 40 轮的纪要会膨胀成几百行，而"纪要"的全部意义
    就是紧凑到不挤占上下文。
    """
    messy = (
        '[turn 1] tool run_shell {"command": "cat a"}\n'
        "[turn 1] result: line1\nline2\nline3\n"
    )
    lines = runner.digest_from_transcript(messy)
    assert len(lines) == 1
    assert "\n" not in lines[0]


def test_digest_from_transcript_is_empty_for_garbage() -> None:
    """不是过程文件就返回空 —— **不抛异常**。

    过程文件是人类可读的文本；格式变了、被截断了、或者根本不是它，
    最好的行为是"少一点上下文"，而不是让整道题起不来。
    """
    assert runner.digest_from_transcript("随便一段文字") == []
    assert runner.digest_from_transcript("") == []


def test_latest_transcript_returns_none_when_there_is_none() -> None:
    """找不到上次的过程 → `None`（调用方据此退回从头开始，不是报错）。"""
    assert runner.latest_transcript("ctf2chal_does_not_exist_at_all") is None


def test_the_initial_prompt_announces_a_resume() -> None:
    """★ 续解必须**明说** —— 否则模型会把纪要当成"背景资料"。

    只说"这是你要解的题"而不说"你上次已经做过这些"，模型很可能从头
    再来一遍 —— 而那正是续解要避免的事。
    """
    fresh = runner._initial_prompt(_spec())
    assert "续解" not in fresh

    resumed = runner._initial_prompt(
        _spec(resume_digest=("[t1] run_shell ls → hint.txt",))
    )
    assert "续解" in resumed
    assert "不要重跑" in resumed


def test_the_resume_digest_reaches_the_model_on_the_first_turn() -> None:
    """★★ 续解的纪要必须在**第一次调用**就带上。

    这一条是整件事的判据：如果纪要要等到第 25 轮裁剪时才出现，
    那"续解"就名不副实 —— 模型前 24 轮仍然在从零摸索，
    而上一次的 37 步发现要到那时才能被看见。
    """
    sandbox = _FakeSandbox()
    gateway = _FakeGateway(['FINAL: {"candidates": []}'])
    _run(
        runner.solve_in_sandbox(
            _spec(resume_digest=("[t7] run_shell: objdump -d main → 看到 decryptBuffer",)),
            sandbox=sandbox,
            gateway=gateway,
            system_prompt="sys",
        )
    )
    first = "\n".join(m["content"] for m in gateway.seen[0])
    assert "decryptBuffer" in first, "上次的发现必须在第一轮就可见"
    assert "已经执行过的动作" in first


def test_run_worker_passes_resume_to_the_async_body() -> None:
    """★★ 回归测试：`run_worker` 必须把 `resume` 透传给 `_run_worker`。

    这是一个**真实发生过的生产 bug**（2026-10-02，用户截图）：

    我加了 `resume` 参数，并在 `build_spec(db, challenge, resume=resume)`
    里用了它 —— 但那一行**不在 `run_worker` 里**，而在它调用的
    `_run_worker`（另一个模块级 async 函数）里。于是 worker 一启动就
    `NameError: name 'resume' is not defined`，而题目卡片上显示的就是
    这行字。

    ## 为什么全量测试没抓到

    `_run_worker` 要真的起沙箱、连平台、跑满解题循环才会走到那一行，
    而测试里不会那么做（那需要 docker 和一个真实平台）。

    所以这里用**签名 + 调用点**把它钉住。它挡不住所有同类错，但挡得住
    最常见的那一种：**调用方传了、被调方没收**。
    """
    import inspect

    assert "resume" in inspect.signature(runner._run_worker).parameters, (
        "_run_worker 必须接收 resume —— 它是真正用它的那一层"
    )
    assert "resume=resume" in inspect.getsource(runner.run_worker), (
        "run_worker 必须把 resume 透传下去，否则 _run_worker 里就是 NameError"
    )


def test_run_worker_passes_attempt_id_to_the_async_body() -> None:
    """★★ 同上一条的**同类风险**：`attempt_id` 必须一路透传到 `build_spec`。

    ## 为什么它值得单独一条（v0.28）

    策略快照（`Ctf2SolveAttempt`）跨了**四层**才到达真正用它的地方：

        service.start_solve → _enqueue → scheduler._start
        → run_worker → _run_worker → build_spec

    而 `attempt_id` 在中间任何一层断掉，症状都是**静默的**：
    `build_spec` 收不到它 → 退回读全局设置 → 那条时序漏洞原样回来
    （用户选 safe、被 auto 提交）。不会报错、不会有日志 ——
    与上面那条 `resume` 的 NameError 不同，这个**连报错都没有**。

    ⚠️ 全量测试抓不到它：`_run_worker` 要真的起沙箱才会走到 `build_spec`。
       所以这里用签名 + 调用点钉住 —— 与上一条同样的手法。
    """
    import inspect

    # ① 三个函数都得收下它
    assert "attempt_id" in inspect.signature(runner._run_worker).parameters, (
        "_run_worker 必须接收 attempt_id"
    )
    assert "attempt_id" in inspect.signature(runner.build_spec).parameters, (
        "build_spec 必须接收 attempt_id —— 它是真正读快照的那一层"
    )
    assert "attempt_id" in inspect.signature(runner.run_worker).parameters, (
        "run_worker 必须接收 attempt_id（调度器传的就是它）"
    )

    # ② 每一层都要真的往下传，而不是收下就丢
    run_worker_src = inspect.getsource(runner.run_worker)
    assert "attempt_id=attempt_id" in run_worker_src, (
        "run_worker 必须把 attempt_id 透传给 _run_worker"
    )
    assert "attempt_id=attempt_id" in inspect.getsource(runner._run_worker), (
        "_run_worker 必须把它传给 build_spec —— 断在这里就退回读全局设置了"
    )


def test_solver_loop_refuses_out_of_scope_http_and_tells_the_model() -> None:
    """★ 越界的 HTTP 请求被拒，且**拒绝理由回到模型**——
    它必须知道为什么不行才能换路子，否则会反复试同一个地址。
    """
    sandbox = _FakeSandbox()
    gateway = _FakeGateway(
        [
            'ACTION: {"tool": "http_request", "args": {"url": "http://169.254.169.254/latest"}}',
            'FINAL: {"candidates": []}',
        ]
    )
    _run(
        runner.solve_in_sandbox(_spec(), sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )
    tool_result = gateway.seen[1][-1]["content"]
    assert "拒绝" in tool_result
    assert "只允许" in tool_result


def test_read_attachment_rejects_path_traversal() -> None:
    """附件读取拒绝路径分隔符（白名单式校验，不是 `..` 过滤）。"""
    result = _run(_dispatch("read_attachment", {"path": "../../etc/passwd"}))
    assert "拒绝" in result


def test_read_attachment_lists_available_files() -> None:
    result = _run(_dispatch("read_attachment", {"path": "nope.txt"}, spec=_spec()))
    assert "没有名为" in result


def test_dispatch_unknown_tool_lists_the_real_ones() -> None:
    result = _run(_dispatch("docker_escape", {}))
    assert "未知工具" in result
    for name in runner.TOOL_NAMES:
        assert name in result


def test_prompt_file_exists_and_has_the_discipline() -> None:
    """系统提示词必须存在，且包含那几条纪律。

    它**不在时抛错**（见 `load_system_prompt` 的说明：兜底会让
    "文件漏进部署包"完全无声）。所以这一条同时验证"文件在"与
    "内容是那份有约束力的版本"。
    """
    text = runner.load_system_prompt()
    assert "ACTION:" in text and "FINAL:" in text
    for keyword in ("不提交", "不越界", "不爆破", "不编造"):
        assert keyword in text, f"提示词里少了纪律条款：{keyword}"


# ══════════════════════════════════════════════════════════
# 沙箱参数（安全边界）
# ══════════════════════════════════════════════════════════


def _argv_of(**overrides) -> list[str]:
    kwargs: dict = {"name": "ctf2-test", "image": runner.IMAGE_BASE}
    kwargs.update(overrides)
    return runner.SandboxContainer(**kwargs)._argv()


def test_sandbox_defaults_to_no_network() -> None:
    """★ 默认 `--network none`。这是本模块最重要的一个安全参数。"""
    argv = _argv_of()
    assert argv[argv.index("--network") + 1] == "none"


def test_sandbox_uses_its_own_network_only_when_opted_in() -> None:
    argv = _argv_of(network_isolated=False)
    assert argv[argv.index("--network") + 1] == runner.CTF2_NETWORK
    # 绝不使用 host 网络。
    assert "host" not in argv


def test_sandbox_argv_never_grants_extra_privileges() -> None:
    """★ 三个"一票否决"的参数永远不出现：`--privileged`、
    docker socket 挂载、`--network host`。

    任何一条都会让上面所有收窄归零，所以它们被写成断言而不是
    留给"常识"。
    """
    argv = _argv_of()
    assert "--cap-add" not in argv
    assert "--privileged" not in argv
    assert "docker.sock" not in " ".join(argv)
    assert "--pid=host" not in argv
    assert "--userns=host" not in argv


def test_sandbox_argv_hardens_the_container() -> None:
    argv = _argv_of()
    assert "--read-only" in argv
    assert "no-new-privileges" in argv
    assert "--cap-drop" in argv and "ALL" in argv
    assert "--pids-limit" in argv
    # /work 可写但在内存里（容器一停就消失）。
    assert any(arg.startswith("/work:rw") for arg in argv)


def test_sandbox_mounts_host_resources_read_only(tmp_path: Path) -> None:
    """★ 宿主资源**只读**挂载，且只在路径存在时挂。

    不存在的路径不挂 —— 挂一个空目录会让"资源没配"与"mount 没生效"
    在容器里长得一样，而排查方向会跑到"是不是 mount 写错了"。
    """
    argv = _argv_of(host_resources=tmp_path)
    mount = [arg for arg in argv if str(tmp_path) in arg]
    assert mount and mount[0].endswith(":ro")

    argv_missing = _argv_of(host_resources=tmp_path / "nope")
    assert not [arg for arg in argv_missing if "nope" in arg]


def test_sandbox_never_mounts_executable_host_paths(tmp_path: Path) -> None:
    """★ 只挂数据，绝不挂 `/usr/bin` / `/opt` / `~/.ssh` 之类。

    白名单式：argv 里出现的宿主挂载路径必须**就是**传进来的那个目录。

    ⚠️ 用 `rsplit(":", 2)` 而不是 `split(":")` —— Windows 的盘符
    （`C:\…`）本身就是冒号，`split(":")[1]` 拿到的是 `\\Users\\…`，
    于是这条断言会**永远失败**，而报错看起来像"挂载参数写错了"。
    """
    argv = _argv_of(host_resources=tmp_path)
    mounts = [arg for arg in argv if arg.startswith(str(tmp_path))]
    assert mounts, "应该有一条 -v 挂载"
    for mount in mounts:
        parts = mount.rsplit(":", 2)
        assert len(parts) == 3, f"挂载参数形状不对：{mount}"
        assert parts[1] == runner.HOST_RESOURCES_PATH
        assert parts[2] == "ro"


def test_sandbox_env_has_no_credentials_by_default() -> None:
    """★ 沙箱环境变量里**绝不允许**出现凭据。

    代码里没法从类型上强制这一点，所以这条测试是那道防线：
    任何往 `extra_env` 里塞 key/token 的改动都会当场变红。
    """
    suspicious = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PAT", "CREDENTIAL")
    container = runner.SandboxContainer(name="x", image=runner.IMAGE_BASE)
    for key in container.extra_env:
        assert not any(word in key.upper() for word in suspicious), (
            f"沙箱环境变量 {key} 看起来像凭据 —— "
            "凭据绝不能进容器（见 runner 模块 docstring）"
        )


def test_worker_only_sets_harmless_env() -> None:
    """★ 实现里实际传进沙箱的那一份**只有无害开关**。
    上一条测的是默认值（`extra_env` 为空）；这一条测的是
    `runner._run_worker` 里**真实构造**的那次调用 —— 默认值是空的
    不等于实现里没塞东西进去。

    断言只盯 `extra_env=` 那一行，因为凭据**当然**会在 worker 里出现
    （它要交给 `LlmGateway`），关键是它**不能进容器**。
    """
    import inspect

    source = inspect.getsource(runner._run_worker)
    assert "PWNLIB_NOTERM" in source

    # ⚠️ 不按 `)` 截断 —— `SandboxContainer(` 的参数里就有
    # `int(time.time())`，第一个 `)` 在它上面，会把 extra_env 那行切掉
    # （实测撞到：断言报"没声明 extra_env"，而它明明在）。
    block = source[source.index("SandboxContainer(") :][:2000]
    env_lines = [line for line in block.splitlines() if "extra_env" in line]
    assert env_lines, "worker 应该显式声明 extra_env（而不是隐式带上什么）"

    declared = env_lines[0].lower()
    for word in ("key", "token", "secret", "password", "pat"):
        assert word not in declared, f"沙箱的 extra_env 里出现了 {word} 相关的东西"


# ══════════════════════════════════════════════════════════
# 镜像本身的静态约束（构建成功**证明不了**的那些）
# ══════════════════════════════════════════════════════════


def _dockerfile(profile: str) -> str:
    root = Path(__file__).resolve().parents[2]
    return (root / "docker" / profile / "Dockerfile").read_text(encoding="utf-8")


def test_sandbox_images_export_the_venv_to_login_shells() -> None:
    """★ 镜像必须让 **login shell** 也看到 venv 的 PATH。

    ## 这条断言挡的是一个实测出来的、会让解题直接失败的缺陷

    `runner.shell()` 用 `bash -lc`，而 kali-rolling 的 `/etc/profile`
    **重置** PATH，把 Dockerfile 里的 `ENV PATH="$VIRTUAL_ENV/bin:$PATH"`
    挤出去：

    ```
    非 login：python3 → /opt/venv/bin/python3   → import pwn ✓
    login   ：python3 → /usr/bin/python3        → ModuleNotFoundError: No module named 'pwn'
    ```

    于是"镜像里装了 pwntools"与"解题时能不能用它"是两件事。

    ⚠️ **构建成功完全证明不了它** —— 镜像照样建得出来，容器照样起得来，
    只有真去 `bash -lc 'python3 -c "import pwn"'` 才看得见。所以这里在
    源码层钉住那个 `/etc/profile.d/*.sh`：删掉它，这条断言变红。
    """
    base = _dockerfile("ctf2-sandbox")
    assert "/etc/profile.d/" in base, "base 镜像没有给 login shell 写 PATH"
    assert "/opt/venv/bin" in base, "base 镜像没把 venv 的路径写进 PATH"

    # ★ re 镜像**不重复写一遍**，它靠 `FROM ctf2-sandbox` 继承那一层。
    #   所以这里断言的是**继承关系**，而不是在 re 里再找一遍那段文本 ——
    #   让两处各写一遍，迟早只有一处被改（而那时症状是"re 镜像里
    #   import 不到 pwntools"，与 base 无关）。
    reverse = _dockerfile("ctf2-sandbox-re")
    assert "ARG BASE_IMAGE=ctf2-sandbox" in reverse, (
        "re 镜像必须从 base 继承（否则它的 venv 与 profile.d 都不是 base 那一份）"
    )


def test_sandbox_base_image_does_not_hardcode_the_apt_mirror() -> None:
    """★ apt 源必须可覆盖（`ARG`），且用 **http**。

    两条都是实测出来的：

    * 用 kali 官方源构建时 apt 层直接失败 —— CDN 返回 628 字节的错误页
      （`File has unexpected size (628 != 16189880). Mirror sync in progress?`），
      而 `http.kali.org` 在部分网络下根本连不上；
    * 换 https 的国内源也不行：基础镜像里**没有 ca-certificates**
      （它正是要装的第一个包），于是 `certificate verify failed` ——
      一个鸡生蛋问题。

    用 `ARG` 而不是写死镜像站：换机器时 `--build-arg APT_MIRROR=...`
    即可，不必改文件。
    """
    text = _dockerfile("ctf2-sandbox")
    assert "ARG APT_MIRROR" in text, "apt 源写死了 —— 换机器就得改 Dockerfile"
    assert "http://%s/kali" in text or "http://${APT_MIRROR}" in text, (
        "apt 源应当是 http —— https 会因为缺 ca-certificates 而失败"
    )


def test_dockerfile_installs_the_build_tools_pip_needs() -> None:
    """★ 编译工具必须在 apt 层里。

    kali-rolling 的 python3 是 **3.14**，而 `pwntools` 的 `unicorn`
    在这个版本上没有预编译 wheel —— pip 会退回源码构建，而源码构建要
    cmake。少了它的症状是构建在 `pip install pwntools` 那一步断：

        error: [Errno 2] No such file or directory: 'cmake'
        ERROR: Failed building wheel for unicorn

    ⚠️ 与"镜像里有没有 cmake 可用"是两件事：这条只保证声明了，
    真能不能装上是构建验证的事（见 `_tmp/kali_sandbox_smoke` 那一类脚本）。
    """
    text = _dockerfile("ctf2-sandbox")
    for tool in ("cmake", "ninja-build", "libzbar0"):
        assert tool in text, f"apt 层缺 {tool}（实测缺它会失败）"


# ══════════════════════════════════════════════════════════
# 候选 flag 与已确认 flag：两列的**生命周期**
# ══════════════════════════════════════════════════════════
#
# 拆分之前这两件事共用 `ctf2_challenges.flag` 一列，于是"界面上的那个
# flag 是答案还是它猜的"无法回答 —— 具体会出事的路径是：
#   safe 模式落候选 A → 用户提交 → 平台判错 → 状态退回未解出，
#   而 A 还挂在题目行上（一道"没解出却有一个 flag"的题）。
#
# 下面四条把每一端的行为钉住，包括**不该清空**的那一端。


def test_correct_answer_lands_in_confirmed_flag(
    db_session: Session, ctf2_challenge
) -> None:
    """判对 → `confirmed_flag` 有值、`candidate_flag` 清空、状态 solved。"""
    ctf2_challenge.candidate_flag = "flag{guess}"
    ctf2_challenge.status = "candidate_ready"
    db_session.commit()

    runner._advance_challenge(
        ctf2_challenge, Ctf2SubmissionStatus.CORRECT.value, "flag{real}"
    )
    db_session.commit()
    db_session.refresh(ctf2_challenge)

    assert ctf2_challenge.confirmed_flag == "flag{real}"
    assert ctf2_challenge.candidate_flag is None, "解出之后候选不该还挂着"
    assert ctf2_challenge.status == "solved"
    assert ctf2_challenge.solved_at is not None


def test_wrong_answer_clears_the_candidate(db_session: Session, ctf2_challenge) -> None:
    """★ 判错 → **清空候选**（这是与拆分前相反的行为）。

    旧行为把猜错的 flag 留在题目行上，理由是"别丢掉最后试的是哪一个"。
    那个诉求现在有更准的出处（`ctf2_submissions` 有完整的提交记录），
    而留着它的代价是一列**看起来像答案**的东西留在界面上。
    """
    ctf2_challenge.candidate_flag = "flag{wrong}"
    ctf2_challenge.status = "candidate_ready"
    db_session.commit()

    runner._advance_challenge(
        ctf2_challenge, Ctf2SubmissionStatus.WRONG.value, "flag{wrong}"
    )
    db_session.commit()
    db_session.refresh(ctf2_challenge)

    assert ctf2_challenge.candidate_flag is None
    assert ctf2_challenge.confirmed_flag is None
    assert ctf2_challenge.wrong_submission_count == 1
    assert ctf2_challenge.status == "solving"


def test_failed_submission_keeps_the_candidate(db_session: Session, ctf2_challenge) -> None:
    """★ 请求**没到达平台**时候选要留着。

    这一条与上一条方向相反，而区别是实质的：`failed` 意味着那次提交
    根本没被判定过（可能压根没发出去），所以那个候选**不是错的**。
    清掉它等于让用户重跑一次解题才能再交同一个 flag。
    """
    ctf2_challenge.candidate_flag = "flag{maybe}"
    ctf2_challenge.status = "candidate_ready"
    db_session.commit()

    runner._advance_challenge(
        ctf2_challenge, Ctf2SubmissionStatus.FAILED.value, "flag{maybe}"
    )
    db_session.commit()
    db_session.refresh(ctf2_challenge)

    assert ctf2_challenge.candidate_flag == "flag{maybe}"
    assert ctf2_challenge.confirmed_flag is None
    assert ctf2_challenge.status == "failed"


def test_already_solved_is_not_a_failure(db_session: Session, ctf2_challenge) -> None:
    """★ 平台回 `ALREADY_SOLVED` 时，**不能**把题目打成 `failed`。

    实测依据（2026-10-01）：向一道已经解出的题重复提交正确的 flag，
    平台回 `HTTP 400 {"error": {"code": "ALREADY_SOLVED"}}`。

    修复前的行为是走通用的 `failed` 分支，写出
    `status=failed` + `last_error="提交请求失败（未到达平台）。"` ——
    后半句在这里是**假的**：请求明明到了平台，还带回一个明确的机器码。
    于是一道**已经解出**的题在界面上显示"失败"，原因还写错了。
    """
    ctf2_challenge.candidate_flag = "flag{known}"
    ctf2_challenge.status = "candidate_ready"
    db_session.commit()

    runner._advance_challenge(
        ctf2_challenge,
        Ctf2SubmissionStatus.FAILED.value,
        "flag{known}",
        already_solved=True,
    )
    db_session.commit()
    db_session.refresh(ctf2_challenge)

    assert ctf2_challenge.status == "unsolved", "已解出不是失败，不该落 failed"
    assert ctf2_challenge.confirmed_flag is None, (
        "平台只说'这题已解出'，并没有认可我们这次交的这个 flag —— "
        "不能凭空把它写成已确认的答案"
    )
    assert ctf2_challenge.candidate_flag == "flag{known}", (
        "候选没被判定过，它不是错的，不该清"
    )
    assert "已被本队伍解出" in (ctf2_challenge.last_error or "")


def test_already_solved_flag_does_not_touch_the_wrong_counter(
    db_session: Session, ctf2_challenge
) -> None:
    """`ALREADY_SOLVED` 不消耗额度 —— 它连"判错"都不是。"""
    before = ctf2_challenge.wrong_submission_count

    runner._advance_challenge(
        ctf2_challenge,
        Ctf2SubmissionStatus.FAILED.value,
        "flag{known}",
        already_solved=True,
    )
    db_session.commit()
    db_session.refresh(ctf2_challenge)

    assert ctf2_challenge.wrong_submission_count == before


def test_already_solved_on_a_solved_challenge_does_not_downgrade_it(
    db_session: Session, ctf2_challenge
) -> None:
    """★★ 平台回 `ALREADY_SOLVED` 时，**已解出的题不能被降级**。

    实测撞出来的（2026-10-01）：拿一道已解出的题做端到端探测，得到 500 ——

        CheckViolation: new row ... violates check constraint
        "ck_ctf2_challenges_confirmed_only_when_solved"

    两处都会撞同一条约束，所以两处都得挡：

    * `submit_candidate` 把状态改成 `submitting`（已单独修）；
    * `_advance_challenge` 的 `already_solved` 分支把状态改成 `unsolved`。

    而 `ck_ctf2_challenges_confirmed_only_when_solved` 的语义是
    `confirmed_flag IS NULL OR status = 'solved'` —— 一道**我们自己解出过**
    的题带着 `confirmed_flag`，任何把它推出 `solved` 的改动都违反它。

    真实会走到这里的形状：拿一道自己解出过的题再次提交，平台回
    `ALREADY_SOLVED` —— 那时它本来就该保持 `solved`。

    产品路径本该拦住"再次提交已解出的题"（`_assert_resolvable` 抛 409），
    但 `submit_challenge` 只调了 `_assert_submission_supported` ——
    那道防线是漏的，所以这里必须兜住。
    """
    ctf2_challenge.status = "solved"
    ctf2_challenge.confirmed_flag = "flag{ours}"
    ctf2_challenge.candidate_flag = None
    db_session.commit()

    runner._advance_challenge(
        ctf2_challenge,
        Ctf2SubmissionStatus.FAILED.value,
        "flag{ours}",
        already_solved=True,
    )
    db_session.commit()
    db_session.refresh(ctf2_challenge)

    assert ctf2_challenge.status == "solved", "已解出的题不该被 ALREADY_SOLVED 降级"
    assert ctf2_challenge.confirmed_flag == "flag{ours}"


def test_response_error_carries_the_platform_error_code() -> None:
    """`Ctf2ResponseError.code` 要带出平台的机器码。

    没有它就只能靠状态码分类，而 `ALREADY_SOLVED` 与"参数写错了"都是
    400 —— 前者要停止解题，后者要改代码，方向完全相反。
    """
    from app.ctf2.client import Ctf2ResponseError

    exc = Ctf2ResponseError("boom", status_code=400, code="ALREADY_SOLVED")
    assert exc.code == "ALREADY_SOLVED"
    assert exc.status_code == 400
    # 不带 code 时必须是 None，而不是空串 —— 调用方用 == 比机器码。
    assert Ctf2ResponseError("boom", status_code=400).code is None


def test_has_correct_submission_separates_ours_from_a_teammates(
    db_session: Session, ctf2_challenge
) -> None:
    """★ 这条区分是整套修复的关键：**谁**解出的。

    | 平台 | 本地有 correct | 含义 | 该怎么办 |
    | --- | --- | --- | --- |
    | 已解出 | 有 | 我们解的 | 照常解（重置后重跑验证是合法的） |
    | 已解出 | 没有 | 队友解的 | 别解（拿不到 flag，白烧一轮） |

    判据必须是**提交记录**而不是题目行的 `status`：`status` 会被重置、
    会被取消打回，而一条 `correct` 提交一旦落库就永远算数。
    """
    from app.core.ids import new_id
    from app.ctf2.models import Ctf2Submission

    assert runner._has_correct_submission(db_session, ctf2_challenge.id) is False

    db_session.add(
        Ctf2Submission(
            id=new_id("ctf2sub"),
            challenge_id=ctf2_challenge.id,
            flag="flag{wrong}",
            status=Ctf2SubmissionStatus.WRONG.value,
            mode="auto",
        )
    )
    db_session.commit()
    assert runner._has_correct_submission(db_session, ctf2_challenge.id) is False, (
        "判错的提交不算解出过"
    )

    db_session.add(
        Ctf2Submission(
            id=new_id("ctf2sub"),
            challenge_id=ctf2_challenge.id,
            flag="flag{real}",
            status=Ctf2SubmissionStatus.CORRECT.value,
            mode="auto",
            # ⚠️ 必须带回执：库上有一条 `ck_ctf2_submissions_correct_implies_receipt`
            #    —— 没有平台回执 id 的提交**不许**自称判对。
            remote_submission_id="r-test-1",
        )
    )
    db_session.commit()
    assert runner._has_correct_submission(db_session, ctf2_challenge.id) is True


def test_platform_reports_solved_reads_the_platform_field(
    monkeypatch: pytest.MonkeyPatch, db_session: Session, ctf2_challenge
) -> None:
    """★ 探测必须**真的**看平台的 `is_solved`，而不是看本地状态。

    这条测试的价值在于把"两个问题"钉开：`ctf2_challenge` 本地是
    `unsolved`，而平台回 `is_solved: true` —— 探测要返回 True。
    如果它哪天被改成读本地 `status`，这条会立刻红。
    """
    from app.ctf2 import service as ctf2_service
    from tests.ctf2.conftest import json_response, patch_ctf2_transport

    recorded = patch_ctf2_transport(
        monkeypatch, json_response(200, {"data": {"is_solved": True}, "success": True})
    )

    assert ctf2_challenge.status == "unsolved", "本地状态与平台相反，这正是要区分的"
    assert asyncio.run(ctf2_service.platform_reports_solved(ctf2_challenge.id)) is True
    assert any("challenges" in p for p in recorded.paths)


def test_platform_reports_solved_is_false_when_not_solved(
    monkeypatch: pytest.MonkeyPatch, db_session: Session, ctf2_challenge
) -> None:
    from app.ctf2 import service as ctf2_service
    from tests.ctf2.conftest import json_response, patch_ctf2_transport

    patch_ctf2_transport(
        monkeypatch, json_response(200, {"data": {"is_solved": False}, "success": True})
    )
    assert asyncio.run(ctf2_service.platform_reports_solved(ctf2_challenge.id)) is False


def test_platform_reports_solved_treats_404_as_cannot_tell(
    monkeypatch: pytest.MonkeyPatch, db_session: Session, ctf2_challenge
) -> None:
    """★ 404 是"问不出来"，不是"已解出"。

    路径可能不适用（例如这题其实属于某个赛事 stage，而不是练习中心）。
    把"问不出来"当成"已解出"会让一整批题**永远解不了** —— 而失败是
    静默的：用户只看到点了解题却什么都没发生。
    """
    from app.ctf2 import service as ctf2_service
    from tests.ctf2.conftest import json_response, patch_ctf2_transport

    patch_ctf2_transport(
        monkeypatch,
        json_response(404, {"error": {"code": "NOT_FOUND"}, "success": False}),
    )
    assert asyncio.run(ctf2_service.platform_reports_solved(ctf2_challenge.id)) is False


def test_platform_reports_solved_is_false_without_remote_ids(
    db_session: Session, ctf2_challenge
) -> None:
    """★ 缺远端标识 → False（fail-open），而不是 True。

    同步没做完的题没有 remote_id，这时"问不出来"。返回 True 会让它
    直接卡死；返回 False 让它照常进入解题，由同步去补标识。
    """
    from app.ctf2 import service as ctf2_service

    original = ctf2_challenge.remote_id
    ctf2_challenge.remote_id = ""
    db_session.commit()
    try:
        assert asyncio.run(ctf2_service.platform_reports_solved(ctf2_challenge.id)) is False
    finally:
        ctf2_challenge.remote_id = original
        db_session.commit()


def test_normalize_flag_strips_nul_bytes() -> None:
    """★ `FLAG_PATTERN` 的正文 `[^{}]{1,200}` **放行 NUL**。

    实测（2026-10-01，`flag在哪？`那类要解二进制的题）：模型把 doc 文件
    按 `utf-16le` 解出来，候选里带着 `\\x00`。而 PostgreSQL 的 `text`
    存不了 NUL —— 它会让写库变成硬错误，进而**掀掉整个 worker**。

    NUL 既不是 `{` 也不是 `}`，所以正则拦不住它，必须在归一化这一步删掉。
    """
    assert runner.normalize_flag("flag{a\x00b}") == "flag{ab}"
    assert "\x00" not in runner.normalize_flag("flag{\x00}")
    # 别的控制字符**不能**动：tab/换行是正文的一部分（见 core/text.py）
    assert runner.normalize_flag("flag{a\tb}") == "flag{a\tb}"


def test_safe_text_keeps_nul_out_of_error_columns() -> None:
    """`last_error` 拿到的常是模型与工具的**原文**。

    2026-10-01 那次崩溃就是 `run_shell` 的 stdout 带 NUL；后来发现
    `_emit` 只是第一个撞墙的地方，`last_error` 是同一个坑的第二个入口。
    """
    assert "\x00" not in runner._safe_text("boom\x00" * 300)
    assert runner._safe_text(None) == ""
    assert len(runner._safe_text("x" * 900)) == 500


def test_events_strip_nul_before_writing(
    db_session: Session, ctf2_challenge, ctf2_provider: str
) -> None:
    """★ 事件里带 NUL 时**必须清洗后入库**，而不是让 worker 崩掉。

    实测崩溃形状：

        sqlalchemy.exc.DataError: (psycopg.errors.UntranslatableCharacter)
        unsupported Unicode escape sequence
        DETAIL:  \\u0000 cannot be converted to text.

    清洗点在 `_emit`（所有事件的唯一出口），所以 `summary` 与 `detail`
    都要过 —— 漏掉任意一边都还是插不进去。
    """
    from app.ctf2 import events as ctf2_events

    ctf2_events.emit_tool_call(
        db_session,
        ctf2_challenge,
        tool="run_shell",
        command="python3 -c \"print(open('x','rb').read())\"",
        output="HTTP/1.1 200 OK\r\n\r\n\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
    )
    db_session.commit()

    from sqlalchemy import select

    from app.events.models import ActivityEvent

    rows = db_session.execute(select(ActivityEvent)).scalars().all()
    assert rows, "事件应该被写进去了，而不是抛出去"
    for row in rows:
        assert "\x00" not in (row.summary or "")
        assert "\x00" not in json.dumps(row.detail, ensure_ascii=False)


def test_postgres_really_rejects_a_nul_byte(
    db_session: Session, ctf2_challenge, ctf2_provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★★ **反向证明**：不清洗时确实是硬错误，那条测试不是空过的。

    没有这一条，上面那条测试有个致命的解释漏洞：如果测试库是 SQLite
    （它**接受** TEXT 里的 NUL），断言 `"\\x00" not in ...` 在任何情况下
    都成立 —— 测试会绿，而生产（PostgreSQL）照样崩。

    这里把 `_emit` 用的清洗函数换成恒等函数，逼真地复现一次崩溃。
    它证明两件事：
    ① 测试库确实是会拒绝 NUL 的那个（真 PostgreSQL，见 `tests/conftest.py`
       的 `SUPEX_TEST_DATABASE_URL`）；
    ② 所以上面那条测试通过，靠的正是 `strip_nul`，而不是环境凑巧。
    """
    from sqlalchemy.exc import DataError

    from app.ctf2 import events as ctf2_events

    monkeypatch.setattr(ctf2_events, "strip_nul", lambda v: v)
    monkeypatch.setattr(ctf2_events, "strip_nul_deep", lambda v: v)

    with pytest.raises(DataError):
        ctf2_events.emit_tool_call(
            db_session,
            ctf2_challenge,
            tool="run_shell",
            command="cat blob",
            output="\x00",
        )
        db_session.commit()
    db_session.rollback()


def test_the_two_columns_cannot_both_hold_a_value(
    db_session: Session, ctf2_challenge
) -> None:
    """★ 数据库层的不变量：两列不会同时有值。

    这条不在应用层测是因为它**是数据不可能长成什么样**的定义
    （见 models 里那两条 CHECK）。绕过 `_advance_challenge` 直接写库
    也该被挡住 —— 挡住它的地方正是将来别的代码路径唯一会撞到的那道墙。
    """
    from sqlalchemy.exc import IntegrityError

    ctf2_challenge.status = "solved"
    ctf2_challenge.confirmed_flag = "flag{real}"
    db_session.commit()

    ctf2_challenge.candidate_flag = "flag{guess}"
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ══════════════════════════════════════════════════════════
# LLM 连接层重试：**哪些失败会作废整道题**
# ══════════════════════════════════════════════════════════


def test_read_error_is_retryable() -> None:
    """★★ `ReadError` 必须在重试列表里 —— 它是**最该重试**的那一类。

    ## 这条守的是一个真实事故（2026-10-12，实测）

    `[FlareOn3]GUI`（.NET 逆向）：模型已经用 `monodis` 反汇编出完整 IL、
    识别出这是 ConfuserEx 混淆、正在逐段分析方法表 —— 第 14 轮一次
    `ReadError` 让这一切作废。日志原话：

        CTF2 解题结束：turns=14 candidates=0 error=调用 LLM 失败（ReadError）：

    而它**远没有跑满 80 轮预算**。对比同一道题上一次的失败
    （"回合用尽（80 轮）"）—— 那次是能力问题，这次是基础设施问题。

    根因：`ReadError` 是 `httpx.HTTPError` 的子类但**不在重试元组里**，
    于是落到 `except httpx.HTTPError` 分支被直接抛出。

    ## 为什么它比 `ConnectError` 更该重试

      · `ConnectError` = 连不上 → 可能是地址/端口配错（重试无用）；
      · `ReadError`    = 连上了、读响应时断了 → 服务端抖动或本地模型
        过载（重试往往就好）。

    本地部署的模型在长推理下过载尤其常见，而那是本项目的典型用法。
    """
    import httpx

    assert httpx.ReadError in runner._LLM_RETRYABLE, (
        "★ ReadError 不在重试列表里 —— 一次读中断会作废整道题。"
        "见本用例 docstring 里 2026-10-12 的实测事故。"
    )


def test_remote_protocol_error_is_retryable() -> None:
    """★★ `RemoteProtocolError` 必须在重试列表里 —— 同 `ReadError`，一次断连作废整道题。

    ## 这条守的是一个真实事故（2026-10-17，实测）

    `[BJDCTF 2nd]文件探测`（WEB，静态目录探测）：模型已经跑到第 39 轮、
    花了 **1611 秒（27 分钟）** 在逐个试站点的静态路径 —— 一次
    `RemoteProtocolError` 让这一切作废。日志原话：

        CTF2 解题结束：turns=39 candidates=0
        error=调用 LLM 失败（RemoteProtocolError）：Server disconnected without sending a response

    根因与 `ReadError` 那条一模一样：它是 `httpx.HTTPError` 的子类，
    但**不在重试元组里**，于是落到 `except httpx.HTTPError` 分支被直接抛出。

    ## 为什么它该重试

    `Server disconnected without sending a response` 的语义是：连接建立了、
    请求发出去了，服务端在回应写完之前关掉了连接。典型原因是服务端
    过载，或 keep-alive 空闲连接被对端先关。两种情况**换一条连接重试
    大概率就好**，而代价只是几百毫秒到几秒。

    反例（不该重试、也不该被这条用例逼进来）：`httpx.UnsupportedProtocol`
    是 URL 写错、`httpx.LocalProtocolError` 是请求体有问题 —— 重试无用。
    """
    import httpx

    assert httpx.RemoteProtocolError in runner._LLM_RETRYABLE, (
        "★ RemoteProtocolError 不在重试列表里 —— 一次断连会作废整道题。"
        "见本用例 docstring 里 2026-10-17 的实测事故。"
    )


def test_retryable_set_covers_the_transient_failures() -> None:
    """★ 五个瞬时失败都在列表里；且**不含** 4xx 类（那些重试无用）。"""
    import httpx

    for exc_type in (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.ReadError,
        httpx.RemoteProtocolError,
    ):
        assert exc_type in runner._LLM_RETRYABLE, f"{exc_type.__name__} 应当可重试"


def test_retry_count_and_backoff_are_sane() -> None:
    """★ 重试次数与退避是有限且合理的（防止有人改成无限重试）。

    4 次、线性退避 1.5s：最坏 1.5+3.0+4.5 = 9 秒。这个量级的意义是
    "值得为它等"—— 因为它换回的是可能已经跑了 14 轮的工作。
    """
    assert 2 <= runner._LLM_RETRY_ATTEMPTS <= 6, (
        "重试次数太少会让瞬时抖动作废整道题；太多会把一次真实故障拖成长时间等待"
    )
    assert 0 < runner._LLM_RETRY_BACKOFF <= 5.0


# ══════════════════════════════════════════════════════════
# 换模 fallback（v0.35）
# ══════════════════════════════════════════════════════════


class _FailThenSucceedGateway:
    """第一次调用抛错，之后正常回话的假 LLM（模拟"主接入点挂了"）。

    `failover()` 返回的就是**它自己** —— 这样换模之后继续用的是同一个
    假对象，能直接断言"换模后确实又调了一次、并且拿到了结论"。

    ⚠️ v0.36：同样要带 `model` / `label`（见 `_FakeGateway` 的说明）。
    `failover()` 里改 `model` 是为了让**换模后的轮次记到新模型名下** ——
    那正是 `Ctf2SolveUsage.model` 这一列存在的理由（同一次尝试、
    两个模型，要能算出"换模前后各花多少"）。
    """

    def __init__(self, *, fail_times: int = 1, then: str) -> None:
        self.fail_times = fail_times
        self.then = then
        self.calls = 0
        self.failovers: list[runner.FallbackLlm] = []
        self.model = "primary-model"
        self.label = "primary-label"

    async def chat(self, messages, *, system):  # noqa: ANN001, ANN201
        self.calls += 1
        if self.calls <= self.fail_times:
            raise runner.Ctf2Error("调用 LLM 失败（ConnectError）：连接被拒绝")
        return runner.LlmReply(
            text=self.then,
            source="content",
            usage=runner.TokenUsage(
                prompt_tokens=10, completion_tokens=20, total_tokens=30, reported=True
            ),
        )

    def failover(self, other: runner.FallbackLlm) -> _FailThenSucceedGateway:
        self.failovers.append(other)
        # 换模后按**新**接入点记账（与真 `LlmGateway.failover` 返回新对象同义）。
        self.model = other.model
        self.label = other.label
        return self


def test_failover_switches_and_keeps_solving() -> None:
    """★★ 主接入点失败 → 换备用 → **继续解题并拿到结论**。

    这是这个功能存在的全部理由：一次 LLM 调用失败不该作废整道题 ——
    那时可能已经跑了三十多轮、在沙箱里做完了大量分析。
    """
    sandbox = _FakeSandbox()
    gateway = _FailThenSucceedGateway(
        fail_times=1,
        then='FINAL: {"candidates": [{"flag": "flag{ok}", "evidence": "跑出来了"}]}',
    )
    backup = runner.FallbackLlm(model="gpt-backup", api_key="sk-backup", label="备用")
    spec = _spec(fallbacks=(backup,))

    outcome = _run(
        runner.solve_in_sandbox(spec, sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    assert gateway.failovers == [backup], "应当恰好换模一次到指定的备用接入点"
    assert [c.flag for c in outcome.candidates] == ["flag{ok}"], "换模后应当能解出"
    assert not outcome.error, "换模成功就不该留下 error"


def test_without_fallback_a_failure_still_fails() -> None:
    """★★ 没配备用 → 行为与引入这个功能**之前完全一致**（判失败）。

    这条是"默认不改变行为"的守卫：少了它，一个"永远换模"的实现
    也能让上面那条通过。
    """
    sandbox = _FakeSandbox()
    gateway = _FailThenSucceedGateway(fail_times=99, then="FINAL: {}")
    spec = _spec()  # 没有 fallbacks

    outcome = _run(
        runner.solve_in_sandbox(spec, sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    assert gateway.failovers == [], "没配备用就不该换模"
    assert outcome.candidates == []
    assert "调用 LLM 失败" in outcome.error


def test_failover_happens_at_most_once() -> None:
    """★ 换模**只发生一次**：备用也不行时，别把回合预算耗在反复重试上。

    换模一次之后仍失败，应当直接判失败（而不是每轮再试一遍备用）。
    """
    sandbox = _FakeSandbox()
    gateway = _FailThenSucceedGateway(fail_times=99, then="FINAL: {}")
    backup = runner.FallbackLlm(model="gpt-backup", api_key="sk-backup")
    spec = _spec(fallbacks=(backup,), max_turns=10)

    outcome = _run(
        runner.solve_in_sandbox(spec, sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    assert len(gateway.failovers) == 1, "备用只该被换上一次"
    assert "调用 LLM 失败" in outcome.error


def test_fallback_llm_repr_hides_the_key() -> None:
    """★ 备用接入点的 `repr` **不含凭据**（与 `SolveSpec` 同一条纪律）。"""
    fb = runner.FallbackLlm(model="m", api_key="sk-super-secret", label="L")
    assert "sk-super-secret" not in repr(fb)
    assert "sk-super-secret" not in str(fb)


def test_gateway_failover_reuses_address_normalization() -> None:
    """★ `failover()` 复用 `__init__` 的地址归一化（本地模型补 `/v1`）。

    换模这条路径**不能**分叉出第二份"怎么拼 URL"的规则 —— 否则
    "本地模型补 /v1" 这条既有规则会在换模后失效，症状是 404。
    """
    primary = runner.LlmGateway(
        model="a", api_key="k", base_url="https://api.deepseek.com", label="主"
    )
    backup = runner.FallbackLlm(
        model="b", api_key="k2", base_url="http://127.0.0.1:11434", label="本地", kind="local"
    )
    swapped = primary.failover(backup)
    assert swapped.url.endswith("/v1/chat/completions"), swapped.url
    assert swapped.model == "b"
    # 换出的是新对象，原对象不受影响（便于"前 N 轮是谁跑的"可考）。
    assert primary.model == "a"


def test_fallback_llms_empty_when_not_configured(db_session: Session, ctf2_challenge) -> None:
    """★ 没配 `fallback_provider_id` → 空元组（默认不换模）。"""
    from app.ctf2 import service as ctf2_service

    settings_row = ctf2_service.effective_ctf2_settings(db_session)
    assert settings_row.fallback_provider_id is None
    assert runner._fallback_llms(db_session, settings_row, primary=None) == ()


def test_fallback_llms_skips_a_missing_provider(db_session: Session, ctf2_challenge) -> None:
    """★ 配了一个**不存在**的 provider → 空元组（不换模），不是崩。

    用户删掉一个 provider 之后，配置里可能还留着它的 id。
    """
    from app.ctf2 import service as ctf2_service

    settings_row = ctf2_service.effective_ctf2_settings(db_session)
    object.__setattr__(settings_row, "fallback_provider_id", "prov_does_not_exist")
    assert runner._fallback_llms(db_session, settings_row, primary=None) == ()


# ══════════════════════════════════════════════════════════
# 续解时列出持久区产物（v0.36）
# ══════════════════════════════════════════════════════════


def test_persist_listing_shows_up_in_the_prompt() -> None:
    """★ 持久区清单要**真的出现在提示词里**，而且带"不要重做"的指令。"""
    spec = _spec(persist_listing=("gui.il（1200 字节）", "res_layer1/（目录）"))
    prompt = runner._initial_prompt(spec)
    assert "gui.il" in prompt
    assert "res_layer1/" in prompt
    assert "不要重做" in prompt


def test_persist_listing_absent_when_empty() -> None:
    """★ 全新解题（没有清单）时不该出现那一节 —— 否则模型会去找不存在的东西。"""
    prompt = runner._initial_prompt(_spec())
    assert "持久区里已经有" not in prompt


def test_list_workspace_files_reads_the_host_dir(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """★ 清单从**宿主上的持久区目录**读，列文件名与大小。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "gui.il").write_text("x" * 1234, encoding="utf-8")
    (workspace / "res").mkdir()

    monkeypatch.setattr(runner, "_workspace_dir", lambda cid, create=False: workspace)
    items = runner._list_workspace_files("ctf2chal_x")

    assert any("gui.il" in i and "1234" in i for i in items)
    assert any("res" in i and "目录" in i for i in items)


def test_list_workspace_files_returns_empty_without_a_dir(monkeypatch) -> None:  # noqa: ANN001
    """★ 持久区不存在 → 空列表（不影响解题）。"""
    monkeypatch.setattr(runner, "_workspace_dir", lambda cid, create=False: None)
    assert runner._list_workspace_files("ctf2chal_x") == []


def test_list_workspace_files_caps_the_listing(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """★ 清单有上限：一个塞了几百个文件的目录不能把提示词撑爆。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    for i in range(runner._MAX_PERSIST_LISTING + 10):
        (workspace / f"f{i:03}.bin").write_bytes(b"x")

    monkeypatch.setattr(runner, "_workspace_dir", lambda cid, create=False: workspace)
    items = runner._list_workspace_files("ctf2chal_x")

    assert len(items) <= runner._MAX_PERSIST_LISTING + 1  # +1 是那句"还有更多"
    assert any("还有更多" in i for i in items)
