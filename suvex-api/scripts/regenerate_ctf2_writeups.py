#!/usr/bin/env python
"""用 **v0.38 的新版式**重生成已有的题解（一次性数据修复）。

## 为什么要重生成

v0.38 改了题解的版式，而改动**不在渲染层**，在**内容层**：

`runner` 收尾时原来只从 `FINAL` 结论文档里取 `candidates`，
`summary` / `tried` / `ruled_out` 三块分析**解析完就丢掉了** ——
所以 v0.36/v0.37 生成的题解**永久缺少那三块内容**，
不是"排版旧"，是"内容不在里面"。

重跑一遍 `build_writeup` 就能补上：三块分析的来源是
**transcript 里的 `FINAL` 块**，而 transcript 文件一直在磁盘上。

## 与 `backfill_ctf2_writeups.py` 的关系

那个脚本解决的是"**没有**题解行"（v0.36 之前解出的 26 道）；
这个解决的是"有题解行、但内容是旧版式"。
两者都走同一条 `build_writeup`，所以**先跑哪个都行**，
但**都要在 v0.38 代码就位之后跑**。

## 为什么要重跑 `_parse_outcome` 而不是读库里的 markdown

库里那份 markdown 是**渲染结果**，从它反推 `summary`/`tried` 不可靠
（要写一个 markdown 解析器去猜段落边界）。而 transcript 是**原始输入**，
`FINAL` 块原样在里面 —— 从输入重建比从输出反推可靠得多。

## ⚠️ `FINAL` 在 transcript 里有两种形态

实测（2026-10-15，61 份 transcript）：

* **裸行**：`FINAL: {...}` 单独成行；
* **带前缀**：`[turn N] assistant(content): FINAL: {...}`。

`runner.parse_final` 用的是 `_MARKER = re.compile(r"^\\s*(ACTION|FINAL)\\s*[:：]", re.MULTILINE)`
—— 它对**两种都认**（前缀那行以 `[turn N]` 开头，而正则不锚定行首内容，
`^` 匹配的是那一行的开头，所以 `[turn N] assistant(content): FINAL:` 这行
**不匹配** `^\\s*FINAL`）。

⚠️ 所以**不能只靠 `parse_final`**：它会漏掉带前缀那种。本脚本额外
扫一遍"行内含 `FINAL:`"的位置，用 `rfind` 从**最后往前**找 ——
最后那份才是最终结论（模型可能在解释里先提一句形状）。
两路都试，取解析成功的那份。

## 幂等

重跑结果**相同**（`build_writeup` 是纯函数，输入来自文件与库，
两者在两次运行之间不变）。所以"幂等"在这里的含义是
**重跑不改变结果**，而不是"跳过"。

⚠️ `now=` 必须传**稳定**的值，否则永远不幂等。
第一版我传 `row.updated_at`（"它就是这一行的当前时间"）—— **那是错的**。
`updated_at` 带 `onupdate`，每次 UPDATE 都被刷新，于是"写库→再读"
拿到上一次写入的时刻，下次生成又用它 —— 每次重跑差两秒。
实测：重跑报"重生成 28 / 内容未变 0"，差异只有"生成时间"那一行。

`generated_at` 只在首次 INSERT 写、**没有** `onupdate`，是稳定锚点。

## 用法

    cd suvex-api
    .venv/bin/python scripts/regenerate_ctf2_writeups.py --dry-run
    .venv/bin/python scripts/regenerate_ctf2_writeups.py

⚠️ **必须在 Kali 上跑**（transcript 文件在那里）。
"""

from __future__ import annotations

import argparse
import contextlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):  # pragma: no cover
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

from sqlalchemy import select  # noqa: E402

from app.core.db import session_scope  # noqa: E402
from app.kernel import runner, service  # noqa: E402
from app.kernel import writeup as writeup_mod  # noqa: E402
from app.kernel.models import Ctf2Writeup  # noqa: E402

#: 与 `runner._save_transcript` 的落点一致。
SOLVES_DIR = PROJECT_ROOT / "_scan_runs" / "_ctf2_solves"

_TURN_LINE = re.compile(r"^\[turn (\d+)\]", re.MULTILINE)

#: 行内含 `FINAL:` 的位置（覆盖"带 `[turn N] assistant(...)` 前缀"那种）。
_FINAL_INLINE = re.compile(r"FINAL\s*[:：]")


def _max_turn(transcript: str) -> int:
    """transcript 里出现过的最大轮次号（= 生产代码里那个循环变量的终值）。

    ⚠️ 不是"数 `[turn N]` 行"：实测一个文件里那类行有 210 行，
    而真实轮数是 76 —— 一行里能塞多轮。
    """
    return max((int(m) for m in _TURN_LINE.findall(transcript)), default=0)


@dataclass
class _Outcome:
    """重建出来的 `SolveOutcome` 替身（只带 `build_writeup` 会读的属性）。"""

    transcript: str = ""
    turns: int = 0
    candidates: list = field(default_factory=list)
    error: str | None = None
    #: ★ v0.38：这三块是本次重生成要补回来的内容。
    summary: str = ""
    tried: list[str] = field(default_factory=list)
    ruled_out: list[str] = field(default_factory=list)


def _final_payload(transcript: str) -> dict | None:
    """取出 transcript 里**最后一份**可解析的 `FINAL` 结论文档。

    ## 为什么要两路

    `runner.parse_final` 认"裸行 `FINAL:`"，但对
    `[turn N] assistant(content): FINAL: {...}` 那种**可能漏**
    （取决于该行是否以 `FINAL` 开头）。实测两种形态都存在，
    所以这里再扫一遍"行内含 FINAL"，从后往前找。

    ⚠️ 从**后**往前：模型可能在解释里先提一句 FINAL 的形状，
       最后那份才是真结论。
    """
    # 第一路：交给 runner 自己的解析器（它处理了 ```json 围栏、
    # JSON 后跟解释文字、坏块跳过等实测遇到的情况）。
    payload = runner.parse_final(transcript)
    if payload:
        return payload

    # 第二路：按"行内含 FINAL:"切段，从后往前逐段试。
    positions = [m.start() for m in _FINAL_INLINE.finditer(transcript)]
    for start in reversed(positions):
        candidate = runner.parse_final(transcript[start:])
        if candidate:
            return candidate
    return None


def _parse_outcome(transcript: str) -> _Outcome:
    """从 transcript 重建 outcome（含 v0.38 的三块分析）。"""
    outcome = _Outcome(transcript=transcript, turns=_max_turn(transcript))

    final = _final_payload(transcript)
    if final:
        candidates, _rejected = runner.extract_candidates_with_reasons(final)
        outcome.candidates = list(candidates)
        # ★ v0.38：三块分析 —— 这正是本次重生成要补的东西。
        (
            outcome.summary,
            outcome.tried,
            outcome.ruled_out,
        ) = runner.extract_final_analysis(final)
        summary_text = outcome.summary
        if summary_text:
            outcome.error = summary_text
    return outcome


def _transcript_for(challenge_id: str) -> Path | None:
    """这道题的 transcript（多次解题取最新那份）。"""
    if not SOLVES_DIR.is_dir():
        return None
    matches = sorted(SOLVES_DIR.glob(f"{challenge_id}-*.txt"))
    return matches[-1] if matches else None


def _elapsed_sec(db, challenge) -> int | None:  # noqa: ANN001
    from app.scans.models import Scan

    if not challenge.scan_id:
        return None
    scan = db.get(Scan, challenge.scan_id)
    return getattr(scan, "duration_sec", None) if scan is not None else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 道（0=全部）")
    args = parser.parse_args()

    if not SOLVES_DIR.is_dir():
        print(f"FATAL: transcript 目录不存在：{SOLVES_DIR}")
        return 1

    regenerated = skipped_no_transcript = unchanged = failed = 0
    gained = {"summary": 0, "tried": 0, "ruled_out": 0}
    rows: list[tuple[str, int, int, str]] = []

    with session_scope() as db:
        existing_rows = list(
            db.execute(select(Ctf2Writeup).order_by(Ctf2Writeup.generated_at))
            .scalars()
            .all()
        )
        if args.limit:
            existing_rows = existing_rows[: args.limit]

        print(f"库里已有题解：{len(existing_rows)} 份\n")

        for row in existing_rows:
            challenge = service.get_challenge_or_404(db, row.challenge_id)
            path = _transcript_for(row.challenge_id)
            if path is None:
                skipped_no_transcript += 1
                print(f"  [无 transcript] {challenge.title}")
                continue

            transcript = path.read_text(encoding="utf-8", errors="replace")
            outcome = _parse_outcome(transcript)
            solved = challenge.status == "solved"

            # ★ 显式传 now= **generated_at**（不是 updated_at！），
            #   让重跑逐字节一致。见模块 docstring 里那段实测说明：
            #   `updated_at` 带 onupdate，每次写库都被刷新，
            #   用它会导致"每次重跑都差两秒"而永不幂等。
            markdown = writeup_mod.build_writeup(
                challenge=challenge,
                outcome=outcome,
                solved=solved,
                elapsed_sec=_elapsed_sec(db, challenge),
                now=row.generated_at,
            )

            if outcome.summary:
                gained["summary"] += 1
            if outcome.tried:
                gained["tried"] += 1
            if outcome.ruled_out:
                gained["ruled_out"] += 1

            if markdown == row.markdown:
                unchanged += 1
                continue

            if args.dry_run:
                regenerated += 1
                rows.append(
                    (challenge.title, len(row.markdown), len(markdown), path.name)
                )
                continue

            row.markdown = markdown
            row.solved = solved
            row.turns = outcome.turns
            row.elapsed_sec = _elapsed_sec(db, challenge)
            regenerated += 1
            rows.append((challenge.title, len(row.markdown), len(markdown), path.name))

        if not args.dry_run:
            db.commit()

    print(f"\n{'（dry-run，未写库）' if args.dry_run else '已写库'}")
    print(f"  重生成：{regenerated}")
    print(f"  内容未变（跳过）：{unchanged}")
    print(f"  跳过（无 transcript）：{skipped_no_transcript}")
    print(f"  生成失败：{failed}")
    print(f"\n★ 补回的内容覆盖：summary {gained['summary']} / "
          f"tried {gained['tried']} / ruled_out {gained['ruled_out']}")

    if rows:
        print(f"\n{'将重生成' if args.dry_run else '已重生成'}的 {len(rows)} 份：")
        for title, old_len, new_len, _fname in rows:
            delta = new_len - old_len
            print(f"  {title:<32} {old_len:>6} → {new_len:>6} 字符 ({delta:+d})")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
