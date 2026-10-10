"""CTF 题解（Writeup）的拼装与端点（任务 19）。

## 为什么要有这套东西

用户的原话：**「解出 flag 后我要的是生成 ctf 比赛形式的那种 wp 报告，
而不是原来那种漏洞报告」**。

做不到让 strix 直接产出 —— 它的报告标题与分节标题都是硬编码的
（见 `scans/writeup.py` 的模块说明）。所以我们把 agent 写的那四节
**重排**成题解版式。

## 这套测试盯的两类事

1. **切节要认名字，不认位置**。位置会随 strix 版本变，认错位置的后果是
   把"方法论"排在"结论"的小节下 —— 一份**看起来正常但读起来错乱**的题解，
   而它的内容全都是真的。
2. **拿不到就说拿不到**。没 flag 时不能留一个空的代码块，
   那看起来像"flag 是个空字符串"。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.vocab import ScanGoal, ScanStatus
from app.scans import writeup
from tests.factories import make_scan

API = "/api/v1"
RUN_ID = "challenge_7f94"

# 一份形状与 strix 真实产出一致的报告
REPORT = """# Security Penetration Test Report

**Generated:** 2026-09-27 13:16:00 UTC

# Executive Summary

这是一道逆向题，最后解出了 flag。

# Methodology

先 `file` 看类型，再 `strings` 捞出常量。

# Technical Analysis

自定义 base64 字母表 + XOR 0x02，逆向求解得到 flag。

# Recommendations

同类题可以直接先看比对常量。
"""


def _scan(tmp_path: Path, *, report: str | None = REPORT, **overrides):
    """造一条跑完了、有产物的夺旗扫描（不落库，只用来测渲染）。"""
    base = tmp_path / "scan"
    if report is not None:
        out = base / "strix_runs" / RUN_ID
        out.mkdir(parents=True, exist_ok=True)
        (out / "penetration_test_report.md").write_text(report, encoding="utf-8")

    fields = dict(
        id="scan_test",
        name="tryre",
        target="tryre.zip",
        target_kind="artifact",
        goal=ScanGoal.CAPTURE_FLAG.value,
        flag="DASCTF{abc-123}",
        brief="逆向题：附件里有一个 ELF，找出 flag。",
        duration_sec=98,
        agent_count=4,
        started_at=datetime(2026, 9, 27, 13, 14, tzinfo=UTC),
        run_dir=str(base),
    )
    fields.update(overrides)
    return type("FakeScan", (), fields)()


# ══════════════════════════════════════════════════════════
# 一、切节
# ══════════════════════════════════════════════════════════


def test_sections_are_matched_by_heading_name() -> None:
    """★ 按**标题名**认节 —— 名字是 strix 写在代码里的常量，位置不是。"""
    sections = writeup.split_sections(REPORT)

    assert set(sections) == {
        "executive_summary",
        "methodology",
        "technical_analysis",
        "recommendations",
    }
    assert "逆向题" in sections["executive_summary"]
    assert "file" in sections["methodology"]
    assert "XOR" in sections["technical_analysis"]
    assert "比对常量" in sections["recommendations"]


def test_unknown_headings_fall_back_to_position() -> None:
    """★ 标题名全改掉时按位置兜底 —— 至少给出一份**内容正确**的题解。

    这条防的是"strix 升版把标题换了，于是题解变成空白"。
    兜底版的小节名未必贴切，但比什么都没有强；而正文位置是稳定的
    （报告的第一个一级节恒为结论）。
    """
    renamed = REPORT.replace("# Executive Summary", "# 一").replace(
        "# Methodology", "# 二"
    ).replace("# Technical Analysis", "# 三").replace("# Recommendations", "# 四")

    sections = writeup.split_sections(renamed)

    assert "逆向题" in sections["executive_summary"]
    assert "file" in sections["methodology"]


def test_partially_unknown_headings_keep_the_known_ones() -> None:
    """只认得一部分时，**认出来的照用**，不去按位置硬凑其余几节。

    硬凑会把"方法论"塞进"结论"的位置 —— 而那份题解读起来是通顺的，
    只是每一节都不是它该有的内容。宁可少一节。
    """
    partial = "# Security Penetration Test Report\n\n# Methodology\n\n只有这一节。\n"

    sections = writeup.split_sections(partial)

    assert sections == {"methodology": "只有这一节。"}


@pytest.mark.parametrize("content", ["", "   ", "没有任何标题的一段话。\n"])
def test_report_without_sections_yields_nothing(content: str) -> None:
    """没有一级标题就返回空 —— 不猜、不硬切。"""
    assert writeup.split_sections(content) == {}


# ══════════════════════════════════════════════════════════
# 一·五、★ 代码块里的 `#` 不是标题（2026-10-10 实测事故）
# ══════════════════════════════════════════════════════════
#
# 上面那份 `REPORT` 的每一节都只有一句话，所以它**测不出**这个 bug。
# 真实报告不是那样的：agent 在 `Technical Analysis` 里贴了一段 bash，
# 而 bash 注释正好是 `# ` 开头 —— 逐行看与一级标题**完全同形**。
#
# 事故现场（`scan_01M4J5QGQHKC7YMXRCREZTBRC2`）：报告 16371 字节，
# 第 45 行是真标题 `# Technical Analysis`，第 55 行是代码块里的 `# 或`。
# 于是"解题过程"被拦腰截断（丢了 80 行），并且**留下一个未闭合的
# ``` 围栏** —— 后面所有内容（含"复盘与技巧"整节）都被吞进代码块，
# 在界面上渲染成一大坨灰色等宽字。内容全是真的，版式全毁了。
#
# 所以这一组盯的是"**围栏内的行不算结构**"这件事本身。
REPORT_WITH_CODE_BLOCK = '''# Security Penetration Test Report

**Generated:** 2026-10-10 05:49 UTC

# Executive Summary

未取得 flag，阻塞点是附件不可得。

# Methodology

先判协议，再找附件。

# Technical Analysis

复现步骤如下：

```bash
# 1) 列题，定位 cid
curl -s "$API/practice/$PID/challenges/$CID/" -H "Authorization: Bearer $JWT"
# 2) 取 download_url
# 3) 下载 8.3GB 附件，离线取证
```

以上是完整步骤，缺一不可。

# Recommendations

止损线要真的执行。
'''


def test_headings_inside_code_fences_are_not_sections() -> None:
    """★ 代码块里的 `# xxx` **不是**分节标题。

    这一条是上面那次事故的直接复现。判据是**正文的完整性**：
    修复前 `technical_analysis` 会在 `# 1) 列题` 那一行断掉，
    于是"以上是完整步骤"这句结尾消失 —— 而题解看起来仍然是"有内容的"。
    """
    sections = writeup.split_sections(REPORT_WITH_CODE_BLOCK)

    body = sections["technical_analysis"]
    assert "复现步骤如下" in body
    assert "# 1) 列题" in body, "代码块里的注释行被吃掉了"
    assert "以上是完整步骤" in body, "正文在代码块中间被截断了"
    # 下一节的正文不能被吞进这一节
    assert "止损线" not in body


def test_the_next_real_section_is_still_found() -> None:
    """围栏内的假标题不能把**后面真正的标题**也一起吞掉。"""
    sections = writeup.split_sections(REPORT_WITH_CODE_BLOCK)

    assert sections["recommendations"].strip() == "止损线要真的执行。"


def test_fenced_blocks_stay_balanced_in_the_writeup(tmp_path: Path) -> None:
    """★ 拼出来的题解里，``` 必须成对。

    未闭合的围栏是最坏的一种：它不会报错，只是把**后面所有内容**
    变成代码块。用户看到的是"题解后半段全变成了灰色等宽字"，
    而第一反应会是"agent 写的东西格式乱了"，不会想到是我们切节切坏的。
    """
    md = writeup.build_writeup(_scan(tmp_path, report=REPORT_WITH_CODE_BLOCK))

    assert md.count("```") % 2 == 0, "围栏没配对上"
    assert md.count("```") >= 4, "代码块本身应当被保留下来"


# ══════════════════════════════════════════════════════════
# 一·六、★ agent 正文里的标题要降级（2026-10-10 实测事故二）
# ══════════════════════════════════════════════════════════
#
# 上一条修完之后，实测那份题解里又出现了这个：
#
#     ## 解题过程          ← 我们生成的
#     ## 完整解题过程      ← agent 在报告正文里自己写的，同级
#     ## 解题思路          ← 我们生成的
#     ## 解题思路（怎么想到的、试过什么、如何排除）
#
# 读者会以为这是四个并列章节。降一级之后层级才对。
REPORT_WITH_AGENT_SUBHEADINGS = '''# Security Penetration Test Report

# Executive Summary

## 题目信息

这是一道取证题。

## 结论

未取得 flag。

# Methodology

## 解题思路（怎么想到的）

先判协议。

# Technical Analysis

## 完整解题过程

第一步先连上去。

```bash
# 这段注释不能被当成标题
echo hi
```

# Recommendations

止损要趁早。
'''


def test_agent_subheadings_are_demoted(tmp_path: Path) -> None:
    """★ agent 正文里的 `##` 要降到 `###`，不能与我们的小节同级。

    判据用**行首计数**：`## 完整解题过程` 必须变成 `### 完整解题过程`。
    只看"标题还在不在"是不够的 —— 它本来就在，只是层级错了。
    """
    md = writeup.build_writeup(_scan(tmp_path, report=REPORT_WITH_AGENT_SUBHEADINGS))
    lines = md.splitlines()

    assert "## 解题过程" in lines, "我们自己的小节标题没了"
    assert "### 完整解题过程" in lines, "agent 的二级标题没被降级"
    assert "## 完整解题过程" not in lines, "agent 的标题仍与我们的小节同级"
    assert "### 题目信息" in lines
    assert "### 解题思路（怎么想到的）" in lines


def test_demote_skips_code_fences() -> None:
    """降级时同样要跳过代码围栏 —— bash 注释里的 `#` 不是标题。

    这与 `_iter_headings` 那次事故是同一个坑：不跳围栏的话，
    `# 这段注释` 会被加一个井号，把正文改坏。
    """
    body = "## 真标题\n\n```bash\n# 这是注释，不是标题\necho hi\n```\n"

    out = writeup._demote_headings(body)

    assert "### 真标题" in out
    assert "# 这是注释，不是标题" in out, "代码块里的注释被改了"


def test_demote_leaves_six_hashes_alone() -> None:
    """已经是 `######`（markdown 上限）的不再降 —— 否则标题会变成普通段落。"""
    assert writeup._demote_headings("###### 最深的一层") == "###### 最深的一层"


# ══════════════════════════════════════════════════════════
# 二、拼出来的题解
# ══════════════════════════════════════════════════════════


def test_writeup_has_the_ctf_shape(tmp_path: Path) -> None:
    """★ 版式是题解，不是渗透报告：标题、结果、题目背景、四节。"""
    md = writeup.build_writeup(_scan(tmp_path))

    assert md.startswith("# CTF Writeup · tryre")
    assert "## 结果" in md
    assert "## 题目背景" in md
    assert "## 题目与结论" in md
    assert "## 解题思路" in md
    assert "## 解题过程" in md
    assert "## 复盘与技巧" in md
    # 渗透口径的小节名**必须消失**（用户明确说不要那种报告）
    for gone in ("Executive Summary", "Methodology", "vulnerability", "Penetration"):
        assert gone not in md


def test_flag_is_in_a_fenced_block(tmp_path: Path) -> None:
    """★ flag 放进围栏代码块 —— 用户要整段复制去平台提交。

    夹在句子中间的那串字符得用鼠标小心地划；代码块一点即可全选。
    """
    md = writeup.build_writeup(_scan(tmp_path))

    assert "```\nDASCTF{abc-123}\n```" in md


def test_missing_flag_says_so_without_an_empty_block(tmp_path: Path) -> None:
    """没拿到时说清楚，**但不要留一个空代码块**。

    空代码块看起来像"flag 是个空字符串"，而那是另一种意思。
    """
    md = writeup.build_writeup(_scan(tmp_path, flag=None))

    assert "本次没有确认到 flag" in md
    assert "```\n\n```" not in md
    assert "```\n```" not in md


def test_brief_is_quoted_and_separate(tmp_path: Path) -> None:
    """我提供的题面单独成节、用引用块 —— 它是**用户的原话**，
    与 agent 的结论要能分开看（发到比赛平台上时，这一段往往正是别人要的）。"""
    md = writeup.build_writeup(_scan(tmp_path))

    assert "> 逆向题：附件里有一个 ELF，找出 flag。" in md


def test_missing_sections_are_skipped_not_faked(tmp_path: Path) -> None:
    """缺哪一节就不写哪一节 —— 不留空标题。

    空标题读起来像"agent 这一节什么都没写"，而真相是"报告里没有这一节"。
    """
    sparse = "# Security Penetration Test Report\n\n# Executive Summary\n\n只有结论。\n"
    md = writeup.build_writeup(_scan(tmp_path, report=sparse))

    assert "## 题目与结论" in md
    assert "## 解题思路" not in md
    assert "## 解题过程" not in md


def test_writeup_survives_a_missing_report(tmp_path: Path) -> None:
    """报告文件不在（被清理过）→ 仍然给出一份**只剩元信息与 flag** 的题解，
    而不是抛异常。端点那边会先拦住"完全没报告"的情况。"""
    md = writeup.build_writeup(_scan(tmp_path, report=None))

    assert md.startswith("# CTF Writeup · tryre")
    assert "DASCTF{abc-123}" in md


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("tryre", "CTF-Writeup-tryre.md"),
        # 题目名里带路径分隔符/空格时不能拼进文件名
        ("a/b c:d", "CTF-Writeup-a-b-c-d.md"),
        ("", "CTF-Writeup-scan.md"),
    ],
)
def test_filename_is_sanitised(tmp_path: Path, name: str, expected: str) -> None:
    assert writeup.filename_for(_scan(tmp_path, name=name)) == expected


# ══════════════════════════════════════════════════════════
# 三、端点
# ══════════════════════════════════════════════════════════


def _stored_scan(db: Session, tmp_path: Path, *, report: str | None = REPORT, **overrides):
    base = tmp_path / "scan"
    if report is not None:
        out = base / "strix_runs" / RUN_ID
        out.mkdir(parents=True, exist_ok=True)
        (out / "penetration_test_report.md").write_text(report, encoding="utf-8")

    scan = make_scan(db, status=overrides.pop("status", ScanStatus.COMPLETED.value))
    scan.run_dir = str(base)
    scan.goal = overrides.pop("goal", ScanGoal.CAPTURE_FLAG.value)
    scan.flag = overrides.pop("flag", "DASCTF{stored-one}")
    db.commit()
    return scan


def test_endpoint_returns_the_writeup(
    ready_client: TestClient, db_session: Session, tmp_path: Path
) -> None:
    scan = _stored_scan(db_session, tmp_path)

    response = ready_client.get(f"{API}/scans/{scan.id}/writeup")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["markdown"].startswith("# CTF Writeup")
    assert "DASCTF{stored-one}" in body["markdown"]
    assert body["filename"].endswith(".md")


def test_endpoint_refuses_a_non_ctf_scan(
    ready_client: TestClient, db_session: Session, tmp_path: Path
) -> None:
    """★ 不是夺旗任务 → 409，而且要说清**为什么没有**。

    用户点「题解」时若只看到"不可用"，他下一步会去猜是不是坏了。
    说清"渗透评估的产物是漏洞报告，不是解题过程"，他才知道这是设计如此。
    """
    scan = _stored_scan(db_session, tmp_path, goal=ScanGoal.FIND_VULNS.value)

    response = ready_client.get(f"{API}/scans/{scan.id}/writeup")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "WRITEUP_UNAVAILABLE"
    assert "夺旗" in error["message"]


def test_endpoint_refuses_while_still_running(
    ready_client: TestClient, db_session: Session, tmp_path: Path
) -> None:
    scan = _stored_scan(db_session, tmp_path, status=ScanStatus.RUNNING.value, report=None)

    response = ready_client.get(f"{API}/scans/{scan.id}/writeup")

    assert response.status_code == 409
    assert "还没有结束" in response.json()["error"]["message"]


def test_endpoint_refuses_when_the_report_file_is_gone(
    ready_client: TestClient, db_session: Session, tmp_path: Path
) -> None:
    """★ 报告文件被清理过 → 409，而**不是**返回一份只剩标题和 flag 的空题解。

    后者看起来是成功的（200 + 一段 markdown），用户要读半天才发现
    里面除了 flag 什么都没有 —— 而那时他已经不知道该怪谁了。
    """
    scan = _stored_scan(db_session, tmp_path, report=None)

    response = ready_client.get(f"{API}/scans/{scan.id}/writeup")

    assert response.status_code == 409
    assert "报告文件" in response.json()["error"]["message"]


def test_endpoint_404s_for_an_unknown_scan(ready_client: TestClient) -> None:
    response = ready_client.get(f"{API}/scans/scan_does_not_exist/writeup")

    assert response.status_code == 404
