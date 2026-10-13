"""解题方法卡（v0.32）：跨题复用的知识提炼与召回。

## ★ 这组用例守的**第一件事**：注入内容绝不能带 flag

卡片会被拼进**另一道题**的 prompt。一旦带上旧 flag，就变成
"把 A 题的答案泄漏到 B 题的上下文里" —— 而 B 题的模型完全可能
把它当成 B 题的答案交上去。

所以下面"脱敏"那一节的用例比"功能"那一节更重要。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.ctf2 import notes as notes_mod
from app.ctf2.models import Ctf2SolveNote
from app.core.ids import new_id


# ══════════════════════════════════════════════════════════
# 一、★★ 脱敏：flag 绝不进卡片
# ══════════════════════════════════════════════════════════


def test_flag_shaped_text_is_stripped() -> None:
    """★★ 看起来像 flag 的片段必须被剔除。

    这是整个模块存在的**前提条件** —— 卡片会进另一道题的 prompt。
    """
    text = "最后拿到了 flag{th1s_1s_th3_4nsw3r} 然后提交成功了"
    cleaned = notes_mod.sanitize_for_reuse(text)

    assert "flag{" not in cleaned, "★ flag 没有被剔除"
    assert "th1s_1s_th3_4nsw3r" not in cleaned
    # 其余内容保留（剔除的是 flag，不是整句话）
    assert "提交成功" in cleaned


def test_various_flag_prefixes_are_stripped() -> None:
    """★ 各种前缀都要挡（不同赛事用不同前缀）。"""
    for text in (
        "答案是 DASCTF{abc123}",
        "拿到 CTF2{e1775a04-1234} 了",
        "提交 FLAG{upper_case_works}",
        "结果 CTF{simple}",
    ):
        cleaned = notes_mod.sanitize_for_reuse(text)
        assert "{" not in cleaned, f"★ 没剔除：{text!r} → {cleaned!r}"


def test_urls_are_stripped() -> None:
    """★ URL 要剔除 —— 那是**别的题**的靶机地址，注入只会误导。"""
    text = "靶机在 http://10.0.0.5:8080/api/v1 上，直接打它"
    cleaned = notes_mod.sanitize_for_reuse(text)

    assert "http://" not in cleaned
    assert "10.0.0.5" not in cleaned
    assert "直接打它" in cleaned


def test_long_random_strings_are_stripped() -> None:
    """★ 32 位以上的 hex/base64 要剔除（可能是凭据或爆破出的口令）。"""
    text = "爆出的口令是 f81ae6f5710cb1340f90cd80d9c33107a1469615bf299e6057dea7f4337f67a3"
    cleaned = notes_mod.sanitize_for_reuse(text)

    assert "f81ae6f5710cb1340f90cd80d9c33107a1469615bf299e6057dea7f4337f67a3" not in cleaned
    assert "爆出的口令是" in cleaned


def test_short_hex_is_kept() -> None:
    """★ 短 hex **不该**被剔除（否则会误伤偏移量、短哈希这类正常内容）。

    这条挡的是"把阈值调得太低"：8 位 hex 在方法描述里很常见
    （`0x401000`、`deadbeef`），剔除它们会让描述变得读不懂。
    """
    text = "跳转到 0x401000，特征是 deadbeef"
    cleaned = notes_mod.sanitize_for_reuse(text)

    assert "0x401000" in cleaned
    assert "deadbeef" in cleaned


def test_sanitize_respects_the_length_limit() -> None:
    """★ 超过上限要截断（卡片不能撑爆 prompt 预算）。"""
    cleaned = notes_mod.sanitize_for_reuse("啊" * 5000, limit=100)
    assert len(cleaned) == 100


def test_sanitize_handles_empty_input() -> None:
    """★ 空输入返回空串（不抛）。"""
    assert notes_mod.sanitize_for_reuse("") == ""
    assert notes_mod.sanitize_for_reuse("   ") == ""


# ══════════════════════════════════════════════════════════
# 二、从解题产物提炼字段
# ══════════════════════════════════════════════════════════


def test_tools_are_extracted_from_transcript() -> None:
    """★ 从 transcript 里提取用过的工具名（去重、保序）。"""
    transcript = "\n".join(
        [
            "[turn 1] tool run_shell ls -la",
            "[turn 1] result: 一堆输出",
            "[turn 2] tool read_attachment x.zip",
            "[turn 2] result: 二进制",
            "[turn 3] tool run_shell strings x.zip",
            "[turn 3] result: 更多输出",
        ]
    )
    tools = notes_mod.tools_from_transcript(transcript)

    assert tools == "run_shell, read_attachment", f"实际：{tools!r}"


def test_tools_are_capped() -> None:
    """★ 工具名数量有上限（防止一条卡占太多 prompt）。"""
    transcript = "\n".join(f"[turn {i}] tool tool_{i} cmd" for i in range(50))
    tools = notes_mod.tools_from_transcript(transcript, limit=5)
    assert len(tools.split(", ")) == 5


def test_build_note_fields_never_includes_the_flag() -> None:
    """★★ 候选 flag **一个字都不进卡片**。

    `candidates` 出现在签名里只是为了区分"解出来了"与"没解出来"
    两种措辞 —— 它的**值**绝不落进任何字段。
    """
    fields = notes_mod.build_note_fields(
        transcript="[turn 1] tool run_shell cat /flag\n[turn 1] result: flag{secret}",
        error=None,
        candidates=["flag{secret_value}"],
        solved=True,
    )

    blob = " ".join(fields.values())
    assert "secret_value" not in blob, "★ flag 原文进了卡片"
    assert "flag{" not in blob


def test_failed_solve_records_the_dead_end() -> None:
    """★ 失败时把原因记进 `dead_ends` —— 那是卡片里最省时间的部分。"""
    fields = notes_mod.build_note_fields(
        transcript="[turn 1] tool run_shell objdump -d x\n[turn 1] result: 看不懂",
        error="回合用尽（80 轮）仍未给出结论。",
        candidates=[],
        solved=False,
    )

    assert "回合用尽" in fields["dead_ends"]
    assert "没解出来" in fields["approach"]


def test_solved_solve_has_no_dead_end() -> None:
    """★ 解出来时不写 `dead_ends`（没有"走不通"要记）。"""
    fields = notes_mod.build_note_fields(
        transcript="[turn 1] tool run_shell ls",
        error=None,
        candidates=["flag{x}"],
        solved=True,
    )
    assert fields["dead_ends"] == ""
    assert "解出来了" in fields["approach"]


def test_dead_end_is_sanitized_too() -> None:
    """★★ `dead_ends` 也要脱敏 —— 失败原因里可能带 flag 片段。

    实测里模型常在错误信息里引用它试过的候选（"flag{xxx} 被判错"），
    而那条信息会被写进卡片。
    """
    fields = notes_mod.build_note_fields(
        transcript="",
        error="平台判定 flag{wrong_guess} 不正确",
        candidates=[],
        solved=False,
    )
    assert "wrong_guess" not in fields["dead_ends"]
    assert "平台判定" in fields["dead_ends"]


# ══════════════════════════════════════════════════════════
# 三、召回
# ══════════════════════════════════════════════════════════


def test_recall_only_returns_the_same_category(
    db_session: Session, ctf2_challenge
) -> None:
    """★★ 只召回**同分类**的卡。

    不同分类的方法不能混用（逆向的经验对 crypto 题是噪音）。
    """
    # 造一张同类卡 + 一张异类卡
    db_session.add(
        Ctf2SolveNote(
            id=new_id("ctf2note"),
            challenge_id="ctf2chal_OTHER_SAME_CATEGORY",
            category=ctf2_challenge.category,
            solved=True,
            approach="同类题的做法",
            tools="run_shell",
            dead_ends="",
        )
    )
    db_session.add(
        Ctf2SolveNote(
            id=new_id("ctf2note"),
            challenge_id="ctf2chal_OTHER_DIFF_CATEGORY",
            category="完全不同的分类",
            solved=True,
            approach="异类题的做法",
            tools="run_shell",
            dead_ends="",
        )
    )
    db_session.commit()

    rows = notes_mod.notes_for_challenge(db_session, ctf2_challenge)
    approaches = [r.approach for r in rows]

    assert "同类题的做法" in approaches
    assert "异类题的做法" not in approaches, "★ 异类卡被召回了"


def test_recall_excludes_the_challenge_itself(
    db_session: Session, ctf2_challenge
) -> None:
    """★★ 不召回**自己**的卡。

    注入自己的卡等于把"上次做到哪"当成"别的题的经验" —— 而那是
    `resume` 该做的事。两者混淆会让模型分不清。
    """
    db_session.add(
        Ctf2SolveNote(
            id=new_id("ctf2note"),
            challenge_id=ctf2_challenge.id,  # ← 自己
            category=ctf2_challenge.category,
            solved=False,
            approach="自己的上次记录",
            tools="",
            dead_ends="",
        )
    )
    db_session.commit()

    rows = notes_mod.notes_for_challenge(db_session, ctf2_challenge)
    assert all(r.challenge_id != ctf2_challenge.id for r in rows), "★ 召回了自己"


def test_recall_puts_solved_notes_first(db_session: Session, ctf2_challenge) -> None:
    """★ 已解出的卡排前面（成功的经验信息量更大）。"""
    for solved, approach in ((False, "失败的经验"), (True, "成功的经验")):
        db_session.add(
            Ctf2SolveNote(
                id=new_id("ctf2note"),
                challenge_id=f"ctf2chal_ORDER_{solved}",
                category=ctf2_challenge.category,
                solved=solved,
                approach=approach,
                tools="",
                dead_ends="",
            )
        )
    db_session.commit()

    rows = notes_mod.notes_for_challenge(db_session, ctf2_challenge)
    assert rows, "应当召回至少一张"
    assert rows[0].solved is True, "★ 已解出的卡没排在前面"


def test_recall_returns_empty_without_a_category(db_session: Session) -> None:
    """★★ 题目没有分类时**返回空**，而不是"给全部"。

    给全部的后果是把无关题的方法灌进 prompt —— 比不给更坏。
    """

    class _NoCategory:
        id = "ctf2chal_NO_CATEGORY_AT_ALL"
        category = ""

    assert notes_mod.notes_for_challenge(db_session, _NoCategory()) == []


def test_recall_respects_the_limit(db_session: Session, ctf2_challenge) -> None:
    """★ 召回数量有上限。"""
    for i in range(10):
        db_session.add(
            Ctf2SolveNote(
                id=new_id("ctf2note"),
                challenge_id=f"ctf2chal_MANY_{i}",
                category=ctf2_challenge.category,
                solved=True,
                approach=f"做法 {i}",
                tools="",
                dead_ends="",
            )
        )
    db_session.commit()

    rows = notes_mod.notes_for_challenge(db_session, ctf2_challenge, limit=3)
    assert len(rows) == 3


# ══════════════════════════════════════════════════════════
# 四、渲染成 prompt 片段
# ══════════════════════════════════════════════════════════


def test_render_says_these_are_other_challenges(db_session: Session) -> None:
    """★★ 渲染时必须**明说**"这是别的题的经验"。

    不这么写的话，模型会把它当成当前题的已知条件 —— 而它是另一道题
    的方法，工具名可能对、参数一定不对。
    """
    note = Ctf2SolveNote(
        id=new_id("ctf2note"),
        challenge_id="ctf2chal_RENDER_TEST",
        category="REVERSE",
        solved=True,
        approach="用 monodis 反汇编",
        tools="monodis, mono",
        dead_ends="",
    )
    text = notes_mod.render_notes_for_prompt([note])

    assert "别的题" in text, "★ 没说清这些经验来自别的题"
    assert "不要照搬命令" in text, "★ 没提醒不要照搬"
    assert "monodis" in text


def test_render_includes_dead_ends(db_session: Session) -> None:
    """★ 走不通的方向也要渲染（那是卡片里最省时间的部分）。"""
    note = Ctf2SolveNote(
        id=new_id("ctf2note"),
        challenge_id="ctf2chal_DEADEND_TEST",
        category="REVERSE",
        solved=False,
        approach="没解出来",
        tools="",
        dead_ends="手工 patch 元数据走不通",
    )
    text = notes_mod.render_notes_for_prompt([note])
    assert "手工 patch 元数据走不通" in text


def test_render_empty_list_gives_empty_string() -> None:
    """★ 没有卡片时渲染成空串（而不是一段空标题）。"""
    assert notes_mod.render_notes_for_prompt([]) == ""


# ══════════════════════════════════════════════════════════
# 五、接进 prompt
# ══════════════════════════════════════════════════════════


def test_prompt_includes_notes_when_present() -> None:
    """★ 有卡片时 prompt 里能看到它。"""
    from app.ctf2 import runner

    spec = runner.SolveSpec(
        challenge_id="ctf2chal_X",
        title="某题",
        category="REVERSE",
        description="题面",
        profile=runner.IMAGE_BASE,
        access_urls=(),
        attachments=(),
        llm_model="m",
        notes_text="\n## 同类题的经验（★ 是**别的题**的）\n- 做法：用 monodis",
    )
    prompt = runner._initial_prompt(spec)
    assert "同类题的经验" in prompt
    assert "monodis" in prompt


def test_prompt_has_no_notes_section_when_empty() -> None:
    """★ 没卡片时 prompt 里**不该**出现那个标题（避免噪音）。"""
    from app.ctf2 import runner

    spec = runner.SolveSpec(
        challenge_id="ctf2chal_Y",
        title="某题",
        category="MISC",
        description="题面",
        profile=runner.IMAGE_BASE,
        access_urls=(),
        attachments=(),
        llm_model="m",
        notes_text="",
    )
    prompt = runner._initial_prompt(spec)
    assert "同类题的经验" not in prompt
