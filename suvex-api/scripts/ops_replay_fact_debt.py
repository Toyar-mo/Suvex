"""从 scan.log 重建 root agent 的 fact_debt 走势，对照 FACT_DEBT_WINDOW=6。

## 为什么要单独算

`scan.log` 只记 `[tool_call] <name> <args>`，不记 tool result。
但 `note_fact_recorded` 只在**写库成功**后才清零，而日志里看不到成败。
所以这里用「调用顺序」近似重建：按 `_DEBT_FREE_TOOL_NAMES` 口径判断
每次调用是否 +1、是否清零，然后看峰值有没有摸到 6。

如果算出来峰值是 3、4 这种，说明 v0.83 的提醒**在这个节奏下永远不会触发** ——
这不是 bug（模型确实每几轮就记一次），但意味着 6 这个阈值对此目标过松。
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter

TOOL_RE = re.compile(r"^\[tool_call\]\s+(\S+)\s*(.*)$")

#: 与 multiagent.py 的 `_DEBT_FREE_TOOL_NAMES` 完全一致。
#: 这里刻意重复一份而不是 import，因为脚本要能离线跑在日志上。
DEBT_FREE = frozenset(
    {
        "record_fact",
        "get_scan_fact",
        "add_fact",
        "get_memory",
        "record_coverage",
        "list_coverage",
        "write_note",
        "list_notes",
        "think",
        "get_threat_model",
        "amend_threat_model",
        "add_idea",
        "resolve_idea",
        "create_todo",
        "list_todos",
        "update_todo",
        "view_agent_graph",
        "wait_for_agents",
        "send_message_to_agent",
    }
)

WINDOW = 6


def main() -> int:
    ap = argparse.ArgumentParser(
        description="从 scan.log 重建 root agent 的 fact_debt 走势，对照 multiagent.FACT_DEBT_WINDOW。",
    )
    ap.add_argument(
        "log",
        nargs="?",
        default="-",
        help="scan.log 路径；省略或 '-' 时从 stdin 读。"
        "（Kali 上通常在 /root/suvex/suvex-api/_scan_runs/<scan_id>/scan.log）",
    )
    args = ap.parse_args()
    if args.log == "-":
        lines = sys.stdin.read().splitlines()
    else:
        with open(args.log, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()

    debt = 0
    peak = 0
    would_nudge = 0
    empty_fact_calls = 0
    fact_calls = 0
    timeline: list[tuple[int, str]] = []

    for line in lines:
        m = TOOL_RE.match(line.strip())
        if not m:
            continue
        name, raw_args = m.group(1), m.group(2).strip()
        if name == "record_fact":
            fact_calls += 1
            # `{}` 或空 → 模型不知道填什么，硬调。
            if raw_args in ("{}", ""):
                empty_fact_calls += 1
            # 写库成功才清零；日志看不出成败，按「参数非空的算成功」近似。
            else:
                debt = 0
        elif name not in DEBT_FREE:
            debt += 1
            if debt > peak:
                peak = debt
            # ⚠️ 不做「触发后自动回落」的近似 —— 那会把峰值人为压到 5。
            # 真实实现里 `render_live_state` 每轮都看 debt，但日志看不出
            # 模型那一轮看到的是几，所以这里只数「有多少轮 debt>=阈值」。
            if debt >= WINDOW:
                would_nudge += 1
        timeline.append((debt, name))

    print(f"总调用（逐行）: {len(timeline)}")
    print(f"record_fact 调用: {fact_calls}（其中空参数 {empty_fact_calls}）")
    print(f"debt 峰值: {peak} / 阈值 {WINDOW}")
    print(f"debt>=阈值 的轮数: {would_nudge}")
    print()
    if would_nudge == 0:
        print("→ 峰值没到阈值，提醒**结构上不可能触发**：")
        print("  每次 record_fact 都紧跟几轮 run_shell，债务被反复清零。")
    else:
        print("→ 重建显示应该触发过；若日志里 0 次，说明清零时机或")
        print("  render_live_state 的调用点与预期不同，需要看真实代码路径。")
    print()
    print("== 每个工具对 debt 的贡献 ==")
    counts = Counter(t[1] for t in timeline)
    for name, n in counts.most_common(12):
        mark = "免单" if name in DEBT_FREE else "+1"
        print(f"{n:5d}  {name:28s} {mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
