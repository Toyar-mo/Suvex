"""`ctf2.writeup` 的题解拼装（v0.36）。

## 这组用例守的是什么

题解是**赛后提交给平台的依据**，它有两个一旦错就很贵的特性：

1. **flag 必须原样保留** —— 它与「方法卡」相反。方法卡的产物会注入
   **另一道题**的 prompt，所以必须脱敏；题解是交给自己/平台的，
   flag 正是最有价值的内容。两边共用了
   `notes.key_steps_from_transcript`，而那个函数的 `sanitize`
   默认是 `True` —— **忘了传 `sanitize=False`，题解里就少两样东西，
   而它看起来仍然生成成功了**。所以这里有用例直接盯住这一点。

2. **三种"没有 token 数字"必须说成三句不同的话**：
   * `usages` 为空 → "本次没有调用记录"（可能是 v0.36 之前跑的）；
   * 有调用但接入点全没给用量 → "接入点没有返回用量"（是接入点的问题）；
   * 只有部分没给 → 数字是**下限**，必须显式说明。

   混成一句会让用户没法判断该找谁 —— 所以三种措辞各有一条用例。

## 为什么这一组能直接调纯函数

`build_writeup` 的 `challenge` / `outcome` 都是**参数**（不从库里查），
所以这里用最小的假对象就够，不必起数据库。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.ctf2 import writeup

#: 一个形状完整的解题过程 —— 用来验证"不脱敏"。
#:
#: ⚠️ 行的格式必须与 `runner` 真正写出来的**逐字一致**
#: （`log.append(f"[turn {turn}] tool {name} {json.dumps(args)}")`，
#: 见 `runner.py` 里那两处 `log.append`）。`_TRANSCRIPT_TOOL` 的正则是
#: `^\[turn (\d+)\] tool (\w+) (.*)$` —— 所以 **`tool` 后面没有冒号**。
#: 第一版我按"看着舒服"写成了 `tool:`，结果一条关键步骤都提不出来，
#: 而症状只是"断言失败"，看不出是格式错了。
TRANSCRIPT = "\n".join(
    [
        '[turn 1] tool run_shell {"command": "file /work/ex/easyre.exe"}',
        "[turn 1] result: PE32 executable (console) Intel 80386",
        '[turn 2] tool run_shell {"command": "strings -n 6 /work/ex/easyre.exe | grep -i flag"}',
        "[turn 2] result: CTF2{e1775a04-dead-beef-0000-1234567890ab}",
        '[turn 3] tool run_shell {"command": "curl -s http://1.2.3.4:8080/check"}',
        "[turn 3] result: ok",
    ]
)


def _challenge(**overrides) -> SimpleNamespace:
    kwargs = {
        "id": "ctf2chal_test",
        "title": "easyre",
        "category": "reverse",
        "remote_id": "1234",
        "description": "解出后包上 flag{}",
        "confirmed_flag": "CTF2{e1775a04-dead-beef-0000-1234567890ab}",
    }
    kwargs.update(overrides)
    return SimpleNamespace(**kwargs)


def _outcome(**overrides) -> SimpleNamespace:
    kwargs = {
        "transcript": TRANSCRIPT,
        "turns": 3,
        "candidates": [],
        "error": None,
        "usages": [],
        # ★ v0.38：FINAL 的三块分析（默认空 —— 缺省时题解要说"没有留下"）
        "summary": "",
        "tried": [],
        "ruled_out": [],
    }
    kwargs.update(overrides)
    return SimpleNamespace(**kwargs)


def _usage(*, prompt: int = 10, completion: int = 20, total: int = 0, reported: bool = True):
    from app.ctf2.runner import TokenUsage

    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        reported=reported,
    )


# ══════════════════════════════════════════════════════════
# ★★ flag **不脱敏**（与方法卡相反）
# ══════════════════════════════════════════════════════════


def test_solved_writeup_keeps_the_flag_verbatim() -> None:
    """★ 解出时 flag 原文出现在题解里 —— 这正是题解要展示的东西。

    ⚠️ 如果哪天有人"顺手统一脱敏"（给 `key_steps_from_transcript`
    去掉 `sanitize=False`，或在题解上套 `sanitize_for_reuse`），
    这条会红 —— 那就是它存在的意义。
    """
    md = writeup.build_writeup(
        challenge=_challenge(), outcome=_outcome(), solved=True
    )

    flag = "CTF2{e1775a04-dead-beef-0000-1234567890ab}"
    assert flag in md
    # 头部那行是给平台看的第一眼。
    assert f"> **Flag**：`{flag}`" in md
    assert "平台判定 flag 正确" in md


def test_unsolved_writeup_lists_candidates_without_masking() -> None:
    """未解出也产出，且候选值**不脱敏** —— "我们试过什么"正是提交材料。"""
    from app.ctf2.runner import Candidate

    md = writeup.build_writeup(
        challenge=_challenge(confirmed_flag=None),
        outcome=_outcome(
            candidates=[
                Candidate(flag="CTF2{candidate-1}", evidence="strings 命中"),
                Candidate(flag="CTF2{candidate-2}", evidence="爆破得到"),
            ]
        ),
        solved=False,
    )

    # ★ v0.38：有候选时头部**区分**"没解出"与"有候选没判对" ——
    #   后者说明模型给出了答案、只是没被平台认可，那是有信息量的区别。
    assert "> **Flag**：未确认（产出 2 个候选，平台未判对）" in md
    assert "本次产出 2 个候选" in md
    assert "CTF2{candidate-1}" in md
    assert "CTF2{candidate-2}" in md
    # 候选的 evidence 也一并列出（v0.38：让人看出"为什么觉得它是答案"）
    assert "strings 命中" in md


def test_unsolved_without_candidates_says_so_plainly() -> None:
    """既没解出也没候选时，措辞必须诚实 —— 不能含糊成"待跟进"。"""
    md = writeup.build_writeup(
        challenge=_challenge(confirmed_flag=None),
        outcome=_outcome(candidates=[], error="沙箱起不来"),
        solved=False,
    )

    assert "本次没有产出候选 flag" in md
    # ★ v0.38：从"失败原因"改成"卡在哪" —— 它回答的是用户真正的问题
    assert "**卡在哪**：沙箱起不来" in md


def test_target_address_is_kept_in_key_steps() -> None:
    """★ 靶机地址同样保留 —— 它是复现题解的必要信息。

    这条与方法卡的分歧是**刻意的**：卡片进别的题的 prompt，
    带上这道题的地址会误导模型。判据是产物的去向，不是"地址敏不敏感"。
    """
    md = writeup.build_writeup(
        challenge=_challenge(), outcome=_outcome(), solved=True
    )

    assert "1.2.3.4:8080" in md


# ══════════════════════════════════════════════════════════
# 三种"没有 token 数字"必须说成三句不同的话
# ══════════════════════════════════════════════════════════


def test_no_usage_rows_says_no_record_at_all() -> None:
    """`usages` 为空 = **根本没记录**（比如 v0.36 之前的解题）。"""
    md = writeup.build_writeup(
        challenge=_challenge(), outcome=_outcome(usages=[]), solved=True
    )

    assert "本次没有调用记录（token 统计自 v0.36 起）" in md


def test_all_usage_unreported_blames_the_endpoint() -> None:
    """★ 有调用但接入点没给用量 —— 不能说"消耗 0"。

    说成 0 会与"没跑过"混淆，而这一列存在的理由就是消除那个歧义。
    """
    usages = [
        (1, "m1", "label-a", _usage(prompt=0, completion=0, total=0, reported=False)),
        (2, "m1", "label-a", _usage(prompt=0, completion=0, total=0, reported=False)),
    ]
    md = writeup.build_writeup(
        challenge=_challenge(), outcome=_outcome(usages=usages), solved=True
    )

    assert "2 次调用，**接入点没有返回 token 用量**（无法统计）" in md
    assert "共 **0** token" not in md


def test_partially_reported_usage_is_flagged_as_a_lower_bound() -> None:
    """部分没给时，汇总里必须写明"其中 N 次未返回用量"（数字是下限）。"""
    usages = [
        (1, "m1", "label-a", _usage(prompt=10, completion=20, total=30)),
        (2, "m1", "label-a", _usage(prompt=0, completion=0, total=0, reported=False)),
        (3, "m1", "label-a", _usage(prompt=5, completion=5, total=10)),
    ]
    md = writeup.build_writeup(
        challenge=_challenge(), outcome=_outcome(usages=usages), solved=True
    )

    assert "3 次调用，共 **40** token（其中 1 次未返回用量）" in md


def test_single_model_has_no_redundant_breakdown() -> None:
    """只有一个模型时不列"按模型拆分" —— 那是冗余，题解要短。"""
    usages = [
        (1, "m1", "label-a", _usage(prompt=10, completion=20, total=30)),
        (2, "m1", "label-a", _usage(prompt=10, completion=20, total=30)),
    ]
    md = writeup.build_writeup(
        challenge=_challenge(), outcome=_outcome(usages=usages), solved=True
    )

    assert "按模型拆分" not in md


def test_fallback_across_two_models_is_broken_down_by_model() -> None:
    """★ 换模（v0.35 fallback）后同一道题会用两个模型。

    "一共花了多少"答不了"换模值不值"，所以必须按模型拆开。
    """
    usages = [
        (1, "primary-model", "primary-label", _usage(prompt=10, completion=20, total=30)),
        (2, "fallback-model", "fallback-label", _usage(prompt=100, completion=200, total=300)),
    ]
    md = writeup.build_writeup(
        challenge=_challenge(), outcome=_outcome(usages=usages), solved=True
    )

    assert "按模型拆分（换模后会出现多个）：" in md
    assert "`primary-model（primary-label）`：1 次调用，30 token" in md
    assert "`fallback-model（fallback-label）`：1 次调用，300 token" in md


def test_billable_total_prefers_total_tokens_when_present() -> None:
    """汇总口径与 `TokenUsage.billable_total` 一致（缺 total 时才用分项和）。

    两边口径不一致的症状很具体：题解里的"总计"与总览页逐题相加对不上。
    """
    usages = [
        # total 与分项和不一致时，以 total 为准（接入点可能含推理开销）
        (1, "m", "l", _usage(prompt=10, completion=20, total=979)),
    ]
    md = writeup.build_writeup(
        challenge=_challenge(), outcome=_outcome(usages=usages), solved=True
    )

    assert "共 **979** token" in md


# ══════════════════════════════════════════════════════════
# 版式与页脚
# ══════════════════════════════════════════════════════════


def test_sections_are_present_in_order() -> None:
    """题解骨架（v0.38 版式）：五节正文 + 三个附录，顺序固定。"""
    md = writeup.build_writeup(
        challenge=_challenge(), outcome=_outcome(), solved=True
    )

    for heading in (
        "# CTF Writeup · easyre",
        "## 一、题目信息",
        "## 二、解题思路",
        "## 三、解题过程",
        "## 四、尝试与排除",
        "### 试过的方向",
        "### 已排除的可能",
        "## 五、结论",
        "## 附录 A · 用到的工具",
        "## 附录 B · 资源消耗",
        "## 附录 C · 完整过程纪要",
    ):
        assert heading in md, f"缺章节：{heading}"

    # 顺序：正文五节必须依次出现（附录排在它们之后）
    order = [
        "## 一、题目信息",
        "## 二、解题思路",
        "## 三、解题过程",
        "## 四、尝试与排除",
        "## 五、结论",
        "## 附录 A · 用到的工具",
    ]
    indexes = [md.index(h) for h in order]
    assert indexes == sorted(indexes), "章节顺序不对"


def test_headline_block_shows_the_conclusion_at_a_glance() -> None:
    """★ 头部一眼看到结论：flag / 结果 / 轮数 / 耗时 —— 不用往下翻。"""
    md = writeup.build_writeup(
        challenge=_challenge(), outcome=_outcome(turns=7), solved=True
    )

    head = md.split("## 一、")[0]
    assert "> **Flag**：`CTF2{e1775a04-dead-beef-0000-1234567890ab}`" in head
    assert "**结果**：已解出" in head
    assert "**轮数**：7" in head
    assert "**耗时**：" in head


def test_table_cells_escape_pipes() -> None:
    """★★ 表格单元格里的裸 `|` 会被 GFM 拆成两列 —— 生成侧必须转义。

    Review 实测确认：题目标题/分类来自平台，可能带 `|`。
    不转义的后果是**整张表错位**，而那是渲染器行为一致的规范问题，
    前端转义不了（它拿到的是已经坏掉的结构）。
    """
    md = writeup.build_writeup(
        challenge=_challenge(title="[ACTF2020]a|b", category="web|xss"),
        outcome=_outcome(),
        solved=True,
    )

    info = md.split("## 一、题目信息")[1].split("## 二、")[0]
    assert "a\\|b" in info
    assert "web\\|xss" in info
    # 表格行里不能再有未转义的裸 `|` 造成的多余列
    for line in info.splitlines():
        if line.startswith("| 标题 ") or line.startswith("| 分类 "):
            # 去掉转义后再数：恰好 3 个分隔符（首尾 + 中间）
            assert line.count("|") - line.count("\\|") == 3, line


def test_pipes_outside_tables_are_not_escaped() -> None:
    """★★ 反向钉子：**代码块里的 `|` 绝不能转义**。

    那是 shell 管道（`curl … | grep`）或正则的或运算。
    转义它会**改坏命令** —— 症状是"用户照着题解跑不通"，
    而这比表格错位严重得多。
    """
    transcript = (
        '[turn 1] tool run_shell {"command": "cat /work/x.txt | grep -i flag | head -5"}\n'
        "[turn 1] result: ok"
    )
    md = writeup.build_writeup(
        challenge=_challenge(), outcome=_outcome(transcript=transcript), solved=True
    )

    # 管道原样保留，没有被转义成 \|
    assert "| grep -i flag |" in md
    assert "\\|" not in md.split("## 三、解题过程")[1].split("## 四、")[0]


def test_layout_never_goes_deeper_than_h3() -> None:
    """★ 版式最深到 **h3** —— 明确回答"需要多深的标题层级"。

    `#` 文档标题 / `##` 五节正文与附录 / `###` 节内子标题
    （`题面`、`试过的方向`、`已排除的可能`）。

    ⚠️ 这条是**给渲染侧的契约**：前端原来没映射 h4/h5/h6，
    在 Tailwind preflight 下会整片隐形。新版式**不依赖** h4 ——
    所以那条映射是防御性的（将来若嵌一层，它已经能渲染）。
    若哪天这里用到 h4，这条会红，提醒去确认渲染侧已支持。
    """
    md = writeup.build_writeup(
        challenge=_challenge(),
        outcome=_outcome(
            summary="综述",
            tried=["一条"],
            ruled_out=["一条"],
        ),
        solved=False,
    )

    for line in md.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            assert level <= 3, f"出现 h{level}，超出 h3：{line!r}"


def test_headings_are_real_headings_not_bold_text() -> None:
    """★ 多级标题用**真标题**，不用加粗冒充 —— 渲染器与目录都靠它。"""
    md = writeup.build_writeup(
        challenge=_challenge(), outcome=_outcome(), solved=True
    )

    for heading in ("## 一、题目信息", "## 二、解题思路", "## 三、解题过程", "## 五、结论"):
        assert heading in md
    # 不该用 `**一、题目信息**` 这种冒充形式
    assert "**一、题目信息**" not in md


def test_description_and_metadata_are_rendered() -> None:
    """题面原样贴、题目信息表填全 —— 缺一栏就是一份不完整的提交材料。"""
    md = writeup.build_writeup(
        challenge=_challenge(), outcome=_outcome(), solved=True
    )

    assert "| 标题 | easyre |" in md
    assert "| 分类 | reverse |" in md
    assert "| 平台题目 id | `1234` |" in md
    assert "| 结果 | 已解出 |" in md
    assert "| 轮数 | 3 |" in md
    assert "### 题面" in md
    assert "解出后包上 flag{}" in md


def test_missing_optional_fields_do_not_crash() -> None:
    """字段全缺时也要产出（不能抛）—— 历史上同步来的题可能没有分类/描述。"""
    md = writeup.build_writeup(
        challenge=SimpleNamespace(id="x"), outcome=SimpleNamespace(), solved=False
    )

    assert "# CTF Writeup · 未命名题目" in md
    assert "| 分类 | 未分类 |" in md
    # remote_id 为空时那一行不出现（而不是渲染一个空值）
    assert "平台题目 id" not in md


def test_empty_transcript_is_stated_not_faked() -> None:
    """没有 transcript 时说"没有可提取的关键步骤"，而不是编一段。"""
    md = writeup.build_writeup(
        challenge=_challenge(), outcome=_outcome(transcript=""), solved=False
    )

    assert "（本次解题没有可提取的关键步骤）" in md
    assert "（本次解题没有留下动作纪要）" in md


def test_explicit_now_makes_the_footer_deterministic() -> None:
    """传 `now` 时生成时间可断言（纯函数的好处）。"""
    md = writeup.build_writeup(
        challenge=_challenge(),
        outcome=_outcome(),
        solved=True,
        elapsed_sec=90,
        now=datetime(2026, 10, 15, 6, 0, 0, tzinfo=UTC),
    )

    assert "| 生成时间 | 2026-10-15 06:00:00 UTC |" in md
    assert "| 耗时 | 1 分 30 秒 |" in md


# ══════════════════════════════════════════════════════════
# ★★ v0.38：FINAL 的三块分析必须进题解
# ══════════════════════════════════════════════════════════
#
# 这一组是本次重做的**核心钉子**。重做前的根因不是排版：
# `parse_final` 一直能解出 summary/tried/ruled_out，但收尾只取了
# `candidates`，那三块**解析完就丢了** —— 所以题解里根本没有它们，
# 而**看起来仍然生成成功**（有标题、有步骤、有统计）。
#
# 下面每一条都对着"丢了也能通过"的假象。


def test_summary_lands_in_the_thinking_section() -> None:
    """★ `summary` 必须出现在「二、解题思路」—— 这是现在缺的那块。"""
    md = writeup.build_writeup(
        challenge=_challenge(),
        outcome=_outcome(summary="目标是 PHP 文件上传点，后端黑名单只挡了 php 后缀。"),
        solved=False,
    )

    thinking = md.split("## 二、解题思路")[1].split("## 三、")[0]
    assert "目标是 PHP 文件上传点" in thinking
    # 不能落到"没有留下综述"那句兜底上
    assert "模型没有留下综述" not in md


def test_tried_and_ruled_out_land_in_section_four() -> None:
    """★ `tried` / `ruled_out` 必须逐条出现在「四、尝试与排除」。"""
    md = writeup.build_writeup(
        challenge=_challenge(),
        outcome=_outcome(
            tried=["用响应长度做旁路：baseline=8628；上传 .php→8532", "试过 .phtml 后缀"],
            ruled_out=["上传目录 /upload/、/uploads/ 均不存在", "getimagesize 不校验"],
        ),
        solved=False,
    )

    section = md.split("## 四、尝试与排除")[1].split("## 五、")[0]
    tried_part = section.split("### 已排除的可能")[0]
    ruled_part = section.split("### 已排除的可能")[1]

    assert "用响应长度做旁路：baseline=8628；上传 .php→8532" in tried_part
    assert "试过 .phtml 后缀" in tried_part
    assert "上传目录 /upload/、/uploads/ 均不存在" in ruled_part
    assert "getimagesize 不校验" in ruled_part


def test_analysis_survives_when_the_challenge_is_unsolved() -> None:
    """★★ **未解出时 `tried`/`ruled_out` 仍在** —— 这时它们才是主体。

    解出的题有 flag 兜底；未解出的题**只有过程**。
    所以"未解出就不写这两节"是一个会静默毁掉大半价值的实现。
    """
    md = writeup.build_writeup(
        challenge=_challenge(confirmed_flag=None),
        outcome=_outcome(
            summary="确认了反序列化文件读取，但没读到 flag。",
            tried=["SplFileObject 读 /etc/hostname 成功"],
            ruled_out=["/flag、/flag.txt 不存在（GlobIterator 无返回）"],
        ),
        solved=False,
    )

    assert "> **Flag**：未确认" in md
    assert "确认了反序列化文件读取" in md
    assert "SplFileObject 读 /etc/hostname 成功" in md
    assert "/flag、/flag.txt 不存在" in md


def test_missing_analysis_says_so_without_faking() -> None:
    """三块都缺时说"模型没有留下"，而不是编一段。"""
    md = writeup.build_writeup(
        challenge=_challenge(), outcome=_outcome(summary="", tried=[], ruled_out=[]), solved=False
    )

    assert "模型没有留下综述" in md
    assert "模型没有留下 `tried` 记录" in md
    assert "模型没有留下 `ruled_out` 记录" in md


def test_long_command_is_not_truncated_in_the_writeup() -> None:
    """★★ v0.38：关键步骤里**单条命令不再截断到 160**。

    实测的坏样子（160 字符截断的产物）：

        cd /work && curl -s -D headers.txt 'http://…' -o index.html;
        echo "exit=$?"; echo '--- headers ---'; cat headers.tx…

    那是"抄不了也读不懂"的半截命令 —— 题解是给人照着重跑的。
    """
    long_cmd = (
        "cd /work && curl -s -D headers.txt 'http://1.2.3.4:8080/index.php' -o index.html; "
        "echo \"exit=$?\"; echo '--- headers ---'; cat headers.txt; echo '--- body ---'; head -c 500 index.html"
    )
    assert len(long_cmd) > 160, "这条测试命令必须长于 160，否则测不到东西"

    transcript = f'[turn 1] tool run_shell {{"command": "{long_cmd}"}}\n[turn 1] result: ok'
    md = writeup.build_writeup(
        challenge=_challenge(), outcome=_outcome(transcript=transcript), solved=True
    )

    # 尾部那段（160 字符之后）必须在题解里
    assert "head -c 500 index.html" in md
    assert long_cmd in md or long_cmd.replace('"', '\\"') in md


def test_key_steps_use_a_fenced_block() -> None:
    """命令用围栏代码块（命令里有引号/反引号时行内代码会渲染坏）。"""
    md = writeup.build_writeup(
        challenge=_challenge(), outcome=_outcome(), solved=True
    )

    section = md.split("## 三、解题过程")[1].split("## 四、")[0]
    assert "```bash" in section
    assert "```" in section


def test_notes_keep_the_short_clip_for_method_cards() -> None:
    """★★ 反向钉子：放宽 `clip` **不能**改变方法卡的挑选与长度。

    题解传 `clip=STEP_CLIP`（800），方法卡走默认 160。
    如果哪天有人"统一"成 800，方法卡的 prompt 会被长命令挤爆 ——
    而它**看起来仍然工作**（卡片照样生成，只是又长又杂）。
    """
    from app.ctf2 import notes

    long_cmd = "x" * 400
    transcript = f'[turn 1] tool run_shell {{"command": "strings -n 6 {long_cmd}"}}\n[turn 1] result: ok'

    # 方法卡路径（默认）：160 上限
    card_steps = notes.key_steps_from_transcript(transcript, sanitize=True)
    for step in card_steps:
        assert len(step) <= 161, f"方法卡步骤超过 160：{len(step)}"

    # 题解路径：800 上限
    wp_steps = notes.key_steps_from_transcript(
        transcript, sanitize=False, clip=writeup.STEP_CLIP
    )
    assert any(len(s) > 160 for s in wp_steps), "题解路径应当保留更长的命令"


# ══════════════════════════════════════════════════════════
# 下载文件名
# ══════════════════════════════════════════════════════════


def test_filename_replaces_unsafe_characters() -> None:
    """文件名不能带路径分隔符 —— 标题是外部数据。"""
    name = writeup.filename_for(_challenge(title="re/../etc:passwd"))

    assert "/" not in name
    assert "\\" not in name
    assert ":" not in name
    assert name.startswith("CTF-Writeup-")
    assert name.endswith(".md")


def test_filename_falls_back_when_title_is_empty() -> None:
    assert writeup.filename_for(_challenge(title="")) == "CTF-Writeup-writeup.md"


def test_filename_is_truncated_and_keeps_chinese() -> None:
    """中文标题要保留（这是中文项目），超长要截断。"""
    name = writeup.filename_for(_challenge(title="中文标题（测试）" + "x" * 200))

    assert "中文标题（测试）" in name
    assert len(name) <= len("CTF-Writeup-") + 80 + len(".md")


# ══════════════════════════════════════════════════════════
# 过程纪要的人话化
# ══════════════════════════════════════════════════════════


def test_digest_args_json_is_unwrapped_for_humans() -> None:
    """`{"command": "..."}` 的外壳要剥掉 —— 题解是给人读的。

    但**剥不掉的不动**：宁可少剥，也不要把内容弄丢。
    """
    digest = [
        '[t1] run_shell {"command": "cd /work/ex && file easyre.exe"} → PE32 executable',
        "[t1] LLM 失败：连接被拒绝",  # 非 JSON 形状，原样保留
    ]
    out = writeup._humanize_digest(digest)

    assert out[0] == "[t1] run_shell cd /work/ex && file easyre.exe → PE32 executable"
    assert out[1] == "[t1] LLM 失败：连接被拒绝"
