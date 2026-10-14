"""报告 IR 的前后端对账（契约 §3.5.1 的硬要求）。

## 为什么必须有一个对账脚本

契约 §3.5.1 原文：

> 前端有 `npm run verify:report` 做机器化比对（12 组范围×选项组合），
> **后端接入后那个脚本应当改为对着 API 跑**。

理由是这份 IR 的权威定义在**前端**（`lib/report-document.ts`），
而后端为了产出可下载的 Markdown 与（将来的）PDF，必须**复刻**它。
复刻就会漂移 —— 而漂移的表现是"预览和下载的文件内容不一样"，
那不报错，只是客户拿到的和别人看到的不一致。

所以这个脚本做一件事：**把两端产出同一份 IR 的关键断言放在一起比**。

## 它比什么、不比什么

比：
* **常量表**（严重度标签/定义、分类标签、状态标签、验证程度标签、技能标签）
  —— 从**前端源码**里解析出来，与后端的常量逐条比；
* **纯函数的措辞**（`riskVerdict` 的四条分支、`coverageNote` 的五条）
  —— 后端与前端必须给出**同一句话**；
* **IR 的字段集合**（顶层与每层）—— 后端少一个字段会让前端渲染崩。

不比：
* 具体数据渲染出来的字符串（那需要两端跑同一份数据，而前端是 TS）。

> ⚠️ **它证明什么、不证明什么**，如实说：
> 它证明"后端复刻的常量与措辞与前端的**源码**一致"。
> 它**不**证明"两端对同一份数据产出的 IR 逐字节相同" ——
> 那需要能跑 TS 构建产物，属于后续工作（可行的做法是把前端
> `verify:report` 改成打后端 API，那需要前端先接上 API）。

用法：

    python scripts/verify_report_parity.py
"""

from __future__ import annotations

import contextlib
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# 前端仓库是后端的兄弟目录（`../suvex-web`）。
FRONTEND = PROJECT_ROOT.parent / "suvex-web" / "src"

for stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):  # pragma: no cover
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

from app.reports import document as ir  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, title: str, detail: str = "") -> bool:
    RESULTS.append((ok, title, detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {title}"
    if detail:
        line += f"\n         {detail}"
    print(line, flush=True)
    return ok


def read(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def parse_ts_record(source: str, name: str) -> dict[str, str]:
    """从 TS 里抠出一个 `Record<K, string> = { key: 'value', ... }` 的字面量表。

    用正则而不是引入 TS 解析器：这些表都是**扁平的字面量**，
    而引一个 parser 只为了读 6 个表不值得。代价是它读不懂含嵌套的表 ——
    真遇到时会返回空 dict，而下面的检查会当场发现（不会静默通过）。
    """
    match = re.search(rf"{name}\s*(?::[^=]+)?=\s*\{{(.*?)\n\}}", source, re.S)
    if not match:
        return {}
    body = match.group(1)
    pairs: dict[str, str] = {}
    for key, value in re.findall(r"^\s*'?([\w.-]+)'?\s*:\s*'([^']*)'", body, re.M):
        pairs[key] = value
    return pairs


def main() -> int:
    print("报告 IR 前后端对账（契约 §3.5.1）", flush=True)
    print(f"  后端：{PROJECT_ROOT}", flush=True)
    print(f"  前端：{FRONTEND}", flush=True)

    if not FRONTEND.is_dir():
        print("\n⚠️ 找不到前端源码目录，无法对账。", flush=True)
        return 1

    findings_ts = read(FRONTEND / "data" / "findings.ts")
    finding_ts = read(FRONTEND / "data" / "finding.ts")
    scan_ts = read(FRONTEND / "data" / "scan.ts")

    print("\n── 常量表 ──", flush=True)

    # 严重度标签（SEVERITY_META 是嵌套对象，所以单独抠 label: '…'）
    severity_labels = dict(
        zip(
            re.findall(r"^\s*(\w+):\s*\{", findings_ts, re.M),
            re.findall(r"label:\s*'([^']*)'", findings_ts),
            strict=False,
        )
    )
    expected = {key: value for key, value in severity_labels.items() if key in ir.SEVERITY_ORDER}
    check(
        expected == ir.SEVERITY_LABEL,
        "严重度标签一致（SEVERITY_META.label）",
        f"前端={expected} 后端={ir.SEVERITY_LABEL}",
    )

    definitions = dict(
        zip(
            re.findall(r"^\s*(\w+):\s*\{", findings_ts, re.M),
            re.findall(r"definition:\s*'([^']*)'", findings_ts),
            strict=False,
        )
    )
    expected_defs = {key: value for key, value in definitions.items() if key in ir.SEVERITY_ORDER}
    check(
        expected_defs == ir.SEVERITY_DEFINITION,
        "严重度定义一致（SEVERITY_META.definition）",
        f"前端 {len(expected_defs)} 条 / 后端 {len(ir.SEVERITY_DEFINITION)} 条",
    )

    order_match = re.search(r"SEVERITY_ORDER[^=]*=\s*\[([^\]]+)\]", findings_ts)
    front_order = (
        tuple(re.findall(r"'(\w+)'", order_match.group(1))) if order_match else ()
    )
    check(
        front_order == ir.SEVERITY_ORDER,
        "严重度顺序一致（SEVERITY_ORDER）—— 排序直接用它",
        f"前端={front_order} 后端={ir.SEVERITY_ORDER}",
    )

    for ts_source, name, ours, label in (
        (finding_ts, "FINDING_CATEGORY_LABEL", ir.CATEGORY_LABEL, "分类标签"),
        (finding_ts, "FINDING_STATUS_LABEL", ir.STATUS_LABEL, "状态标签"),
        (finding_ts, "VERIFICATION_LABEL", ir.VERIFICATION_LABEL, "验证程度标签"),
    ):
        parsed = parse_ts_record(ts_source, name)
        check(parsed == ours, f"{label}一致（{name}）", f"前端={parsed} 后端={ours}")

    skill_pairs = re.findall(r"id:\s*'(\w+)',\s*\n\s*label:\s*'([^']*)'", scan_ts)
    front_skills = {key: value for key, value in skill_pairs if key in ir.SKILL_LABEL}
    check(
        front_skills == ir.SKILL_LABEL,
        "技能标签一致（SKILLS）—— 报告附录 A 要用",
        f"前端={front_skills} 后端={ir.SKILL_LABEL}",
    )

    print("\n── 措辞（必须是同一句话）──", flush=True)

    build_ts = read(FRONTEND / "lib" / "report-build.ts")
    # 后端产出的四条结论与预期的固定句
    verdict_cases = [
        ({"total": 0, "urgent": 0, "verified": 0}, "本次范围内未发现安全问题。"),
        (
            {"total": 5, "urgent": 0, "verified": 0},
            "本次范围内共发现 5 个问题，均不构成直接可利用风险，建议按常规加固计划处理。",
        ),
        (
            {"total": 5, "urgent": 2, "verified": 0},
            "本次范围内共发现 5 个安全问题，其中 2 个为严重或高危，建议优先处置。",
        ),
        (
            {"total": 5, "urgent": 2, "verified": 1},
            "本次范围内共发现 5 个安全问题，其中 2 个为严重或高危，"
            "已通过可复现的验证脚本确认其中 1 个，建议优先处置。",
        ),
    ]
    # ⚠️ 前端的源码是**模板字符串**（`共发现 ${stats.total} 个问题`），
    # 所以不能拿整句去 `in` 匹配 —— 数字位置上源码里是 `${...}`。
    # 第一版就是这么栽的：措辞其实完全一致，但匹配逻辑把它判成了不一致。
    #
    # 正确做法：把源码里的 `${...}` 抹成占位符，再把后端产出的句子里的数字
    # 也抹成占位符，然后比**骨架**。
    skeleton_source = re.sub(r"\$\{[^}]*\}", "\x00", build_ts)

    for stats, expected_sentence in verdict_cases:
        produced = ir.risk_verdict(stats)
        produced_skeleton = re.sub(r"\d+", "\x00", produced)
        check(
            produced == expected_sentence and produced_skeleton in skeleton_source,
            f"结论措辞一致（total={stats['total']} urgent={stats['urgent']}）",
            produced,
        )

    doc_ts = read(FRONTEND / "lib" / "report-document.ts")
    coverage_cases = {
        "completed": "完整",
        "aborted": "不完整（已终止）",
        "running": "不完整（进行中）",
        "failed": "未产出结果",
    }
    for status, expected_note in coverage_cases.items():
        produced = ir.coverage_note(status)
        check(
            produced == expected_note and expected_note in doc_ts,
            f"覆盖说明一致（{status}）",
            produced,
        )

    print("\n── IR 字段集合 ──", flush=True)

    # 后端实现里断言过的字段集合（与 tests/test_reports.py 同一份口径）
    top = {"meta", "verdict", "stats", "distribution", "entries", "method", "severityDefinitions"}
    entry = {
        "index", "findingId", "title", "severity", "severityLabel", "categoryLabel",
        "target", "affected", "statusLabel", "verificationLabel", "discoveredAt",
        "description", "reasoning", "evidence", "poc", "remediation", "dispositions",
    }
    for name, fields in (("ReportDocument", top), ("ReportEntry", entry)):
        missing_in_frontend = [f for f in fields if f not in doc_ts]
        check(
            not missing_in_frontend,
            f"{name} 的字段前端都声明了（后端复刻的是它的形状）",
            f"前端源码里找不到：{missing_in_frontend}" if missing_in_frontend else f"{len(fields)} 个字段",
        )

    print("\n── 摘要 ──", flush=True)
    passed = sum(1 for ok, _, _ in RESULTS if ok)
    failed = [title for ok, title, _ in RESULTS if not ok]
    print(f"\n{'=' * 60}", flush=True)
    print(f"合计 {len(RESULTS)} 项：通过 {passed}，失败 {len(failed)}", flush=True)
    for title in failed:
        print(f"  ✗ {title}", flush=True)

    if not failed:
        print(
            "\n⚠️ 这个脚本证明的是「后端复刻的常量与措辞与前端的源码一致」。\n"
            "   它**不**证明「两端对同一份数据产出的 IR 逐字节相同」——\n"
            "   那需要把前端 verify:report 改成打后端 API（要先接上前端）。",
            flush=True,
        )
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
