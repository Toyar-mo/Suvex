"""flag 抽取与落库（任务 18）。

## 这一套测试来自一次真实的反馈

用户跑完一道逆向题，说「任务结束了，没看到 flag」。

查下来 flag 确实拿到了，但它只活在两个地方：报告文件里的一个代码块，
以及 `finish_scan` 事件 `detail` 的 JSON 里 —— 而那条事件的 `summary`
是被 `_clip` 截断的 JSON 原文（`finish_scan {"executive_summary":"本次评估…`），
**flag 正好落在截断点之后**。于是整条记录最要紧的那个值，在界面上不可见。

## 这些测试里最要紧的一条

`test_ambiguous_report_is_left_empty` 与 `test_findvulns_scan_never_gets_a_flag`。

用户会**直接拿 `scans.flag` 去提交答案**。所以这个字段一旦会"猜"，
它就再也不能被信任了 —— 一个看起来确定的错值，比一块诚实的空白坏得多。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.core.vocab import ScanGoal, ScanStatus
from app.scans import service
from app.scans.flag import extract_flag
from tests.factories import make_scan

RUN_ID = "challenge_7f94"


def _run_dir(tmp_path: Path, *, report: str | None = None, notes: str | None = None) -> Path:
    """造一个扫描产物目录，形状与 strix 真实产出一致。"""
    base = tmp_path / "scan"
    out = base / "strix_runs" / RUN_ID
    out.mkdir(parents=True, exist_ok=True)
    if report is not None:
        (out / "penetration_test_report.md").write_text(report, encoding="utf-8")
    if notes is not None:
        state = out / ".state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "notes.json").write_text(notes, encoding="utf-8")
    return base


# ══════════════════════════════════════════════════════════
# 一、两条来源
# ══════════════════════════════════════════════════════════


def test_marker_line_wins(tmp_path: Path) -> None:
    """★ `FLAG: xxx` 标记行优先 —— 它是**按协议明确要求**写的，不是猜出来的。"""
    base = _run_dir(
        tmp_path,
        report="结论如下。\n\nFLAG: DASCTF{aaa-111}\n\n补充说明。\n",
    )

    assert extract_flag(base) == "DASCTF{aaa-111}"


def test_chinese_colon_marker_also_works(tmp_path: Path) -> None:
    """中文冒号同样收 —— 中文输出是这个项目的默认口径，
    只认半角冒号等于让一半的真实输出失效。"""
    base = _run_dir(tmp_path, report="结论：\nFLAG：DASCTF{bbb-222}\n")

    assert extract_flag(base) == "DASCTF{bbb-222}"


def test_report_token_is_used_when_there_is_no_marker(tmp_path: Path) -> None:
    """★ 没有标记行时退回"报告里唯一的花括号串"。

    这正是那次事故的形状：报告里有 flag，但协议当时还没要求标记行 ——
    于是它埋在正文的一个代码块里。
    """
    base = _run_dir(
        tmp_path,
        report=(
            "**核心判据已达成 —— 成功提取出完整 flag 字符串：**\n\n"
            "```\nDASCTF{454646fa-2462-4392-82ea-5f809ad5ddc2}\n```\n\n"
            "验证方式见下。\n"
        ),
    )

    assert extract_flag(base) == "DASCTF{454646fa-2462-4392-82ea-5f809ad5ddc2}"


def test_report_is_preferred_over_a_decoy_in_notes(tmp_path: Path) -> None:
    """★★ 报告优先于 notes。

    notes 是 agent 的**过程**记录，里面出现形状相似的东西很正常
    （试过的假解法、题目给的示例格式）。报告是它写给人的结论 ——
    两者冲突时，信后者。
    """
    base = _run_dir(
        tmp_path,
        report="结论：\n\nFLAG: DASCTF{real-one}\n",
        notes=json.dumps({"note": "先试了 flag{decoy-from-question}"}),
    )

    assert extract_flag(base) == "DASCTF{real-one}"


# ══════════════════════════════════════════════════════════
# 二、★ 宁可留空，绝不猜
# ══════════════════════════════════════════════════════════


def test_ambiguous_report_is_left_empty(tmp_path: Path) -> None:
    """★★ 报告里有**两个不同**的花括号串 → 留空，不许挑一个。

    用户会拿这个值直接去提交。挑错了他要等到平台判错才知道，
    而那时他已经在怀疑整条记录了。空白至少让他知道"得自己去报告里看"。
    """
    base = _run_dir(
        tmp_path,
        report="候选 A 是 flag{aaa}，但候选 B flag{bbb} 也说得通。\n",
    )

    assert extract_flag(base) is None


def test_explicit_not_found_is_not_recorded_as_a_flag(tmp_path: Path) -> None:
    """★★ `FLAG: NOT_FOUND` **不能**变成一个值为 "NOT_FOUND" 的 flag。

    这条很容易写错，而且错得很难看：那个记录在界面上长得就像
    "拿到了一个叫 NOT_FOUND 的 flag"，用户会真的去提交它。

    协议里明确要求没拿到时也要写这一行（为了区分"没拿到"与"忘了写"），
    所以它一定会出现。
    """
    base = _run_dir(tmp_path, report="尝试了多种路径，均未成功。\n\nFLAG: NOT_FOUND\n")

    assert extract_flag(base) is None


@pytest.mark.parametrize("marker", ["FLAG: NONE", "FLAG: N/A", "FLAG：未找到", "FLAG：无"])
def test_all_absent_forms_are_ignored(tmp_path: Path, marker: str) -> None:
    """"没找到"的各种写法都要认 —— 只认一种的话，其余的会漏成假 flag。"""
    base = _run_dir(tmp_path, report=f"结论：\n{marker}\n")

    assert extract_flag(base) is None


@pytest.mark.parametrize(
    "placeholder",
    ["flag{...}", "FLAG{...}", "CTF{***}", "flag{???}", "DASCTF{___}"],
)
def test_placeholder_shaped_values_are_refused(tmp_path: Path, placeholder: str) -> None:
    """★★ 占位符形状的"答案"不能收 —— 这是**回填时真实撞到**的一个 bug。

    那次回填，一条**失败**的扫描把自己报告里的模板占位符 `flag{...}`
    抽了出来，于是库里多了一条"拿到了 flag{...}"的记录。

    这正是这个字段最不能出的错：用户会**照抄它去提交**，
    而它看起来还挺像回事（前缀是 `flag`，形状也对）。
    第一版正则要求正文至少 3 个字符，而 `...` 恰好是 3 个 —— 漏了。

    判据只收"正文里只有标点"。刻意**不做**单词黑名单：黑名单一定会漏
    （漏掉就是假 flag），还会误伤形状古怪的真题 flag（误伤就是真 flag 被丢）。
    """
    base = _run_dir(tmp_path, report=f"结论：尚未拿到。\n\nFLAG: {placeholder}\n")

    assert extract_flag(base) is None


def test_a_real_flag_survives_alongside_a_placeholder(tmp_path: Path) -> None:
    """★ 真 flag 与占位符同时出现时，**真 flag 不能被占位符连累**。

    剔占位符必须在"是否唯一"的判定**之前**做。反过来的话，一份真 flag
    加一个 `flag{...}` 会被判成"有歧义"，于是真 flag 也一起丢了 ——
    那是"为了防假阳性而制造假阴性"，同样让这个字段不可信。
    """
    base = _run_dir(
        tmp_path,
        report="题目给的格式是 flag{...}，我拿到的是 DASCTF{real-one-here}。\n",
    )

    assert extract_flag(base) == "DASCTF{real-one-here}"


def test_missing_directory_is_not_an_error(tmp_path: Path) -> None:
    """产物目录不在（被清过、还没落盘）→ 返回 None，不抛。

    ⚠️ 这条不是凑数的：`finalize` 在**每次扫描收尾时**都会调它，
    抛异常会让一次正常结束的扫描被标成失败 —— 而原因是"读不到一个可选文件"。
    """
    assert extract_flag(tmp_path / "nope") is None
    assert extract_flag(None) is None
    assert extract_flag("") is None


def test_empty_run_dir_returns_none(tmp_path: Path) -> None:
    """目录在但什么都没有 —— 同样是正常情况。"""
    base = tmp_path / "scan"
    (base / "strix_runs" / RUN_ID).mkdir(parents=True)

    assert extract_flag(base) is None


# ══════════════════════════════════════════════════════════
# 三、收尾时落库
# ══════════════════════════════════════════════════════════


def _running_ctf_scan(db: Session, run_dir: Path, *, goal: str = ScanGoal.CAPTURE_FLAG.value):
    scan = make_scan(db, status=ScanStatus.RUNNING.value)
    scan.goal = goal
    scan.run_dir = str(run_dir)
    db.commit()
    return scan


def test_finalize_records_the_flag_it_was_given(db_session: Session, tmp_path: Path) -> None:
    """★ 收尾时把**调用方传进来的** flag 写进记录 —— 界面靠这一列显示。

    ⚠️ 抽取**不在** `finalize` 里做，值是调用方算好传进来的。
    原因见 `finalize` 里那段注释：它跑在一个已经开着的事务里，
    在那里读文件会让持锁窗口跨越磁盘 I/O —— 实测撞过一次
    `DeadlockDetected`，而关掉抽取就再也复现不出来。
    所以下面几条测的是**真实的抽取入口** `scheduler._flag_of`。
    """
    scan = _running_ctf_scan(db_session, tmp_path / "scan")

    service.finalize(db_session, scan, exit_code=0, flag="DASCTF{final-ized}")
    db_session.commit()

    db_session.expire_all()
    row = db_session.get(type(scan), scan.id)
    assert row.status == ScanStatus.COMPLETED.value
    assert row.flag == "DASCTF{final-ized}"


def test_flag_of_reads_the_real_artifact(db_session: Session, tmp_path: Path) -> None:
    """★ 真实的抽取入口：`_flag_of` 从产物里认出 flag。

    它是 `_reap_finished` 在**开写事务之前**调的那一个 ——
    也是唯一一条会写 `scans.flag` 的路径。
    """
    from app.scans.scheduler import _flag_of

    base = _run_dir(tmp_path, report="结论：\n\nFLAG: DASCTF{from-artifact}\n")
    scan = _running_ctf_scan(db_session, base)

    assert _flag_of(scan.id) == "DASCTF{from-artifact}"


def test_findvulns_scan_never_gets_a_flag(db_session: Session, tmp_path: Path) -> None:
    """★★ 例行评估的扫描**绝不能**被抽 flag。

    真实渗透报告里 `{...}` 形状的东西太多了：payload、模板占位符、
    示例令牌、JSON 片段。对这类报告跑同一套抽取会**稳定地产出假 flag**，
    而假 flag 比没有 flag 坏得多 —— 它会让人以为"这次顺手也拿到旗了"。

    ⚠️ 报告内容必须是**真的会被抽出来**的那一种（这里放了一个
    `flag{...}` 形状的示例令牌）。第一版我写的是 `{{7*7}}` 和
    `${user.name}` —— 那两个**不匹配** flag 形状，于是不管有没有
    goal 守卫，抽取都返回 None：**这条测试对守卫是空洞的**。
    是变异测试（拆掉守卫后它没变红）把这个漏洞抓出来的。
    """
    from app.scans.scheduler import _flag_of

    base = _run_dir(
        tmp_path,
        # 一份典型的渗透报告尾段：里面有一个"看起来就像 flag"的示例令牌
        report=(
            "复现 payload：`{{7*7}}`，返回 `49`。\n\n"
            "另外，建议把文档里的示例令牌 `flag{example-not-real}` 删掉，"
            "以免被误当成真实凭据。\n"
        ),
    )
    scan = _running_ctf_scan(db_session, base, goal=ScanGoal.FIND_VULNS.value)

    assert _flag_of(scan.id) is None


def test_failed_ctf_scan_keeps_flag_empty(db_session: Session, tmp_path: Path) -> None:
    """没拿到时不写空串 —— 留 NULL。

    界面靠"NULL 还是有值"区分"没有"和"拿到了"。多一个空串状态，
    渲染就多一条没人处理过的分支，而它显示出来是"有个空白的 flag 条"。
    """
    from app.scans.scheduler import _flag_of

    base = _run_dir(tmp_path, report="没成功。\n\nFLAG: NOT_FOUND\n")
    scan = _running_ctf_scan(db_session, base)

    assert _flag_of(scan.id) is None

    service.finalize(db_session, scan, exit_code=1, flag=None)
    db_session.commit()

    db_session.expire_all()
    row = db_session.get(type(scan), scan.id)
    assert row.status == ScanStatus.FAILED.value
    assert row.flag is None
