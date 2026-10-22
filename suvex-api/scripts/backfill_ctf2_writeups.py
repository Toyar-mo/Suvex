#!/usr/bin/env python
"""回填 v0.36 之前已解出、但没有题解的题（一次性数据修复）。

## 为什么需要它

`runner._run_worker` 收尾时**无条件**调 `ctf2_service.save_writeup` ——
但那一句是 **v0.36 才加的**。在此之前解出的题**从来没有生成过题解**，
于是它们在界面上点「题解」得到的是「这道题还没有解过」——
**对一道平台判过对的题说假话**。

实测（2026-10-15）：`solved=26`，而 `ctf2_writeups=1` ——
**26 道已解出的题里只有 1 道有题解。**

## 为什么能回填：transcript 还在

`runner._save_transcript` 一直把解题全过程写到
`_scan_runs/_ctf2_solves/<challenge_id>-<时间戳>.txt`（**v0.36 之前就在写**）。
所以题解需要的四样输入里，三样能重建：

| 输入 | 来源 |
| --- | --- |
| `challenge` | 库里直接读（`confirmed_flag` / `title` / `category` 都有） |
| `outcome.transcript` | transcript 文件原文 |
| `outcome.turns` | ⚠️ **见下** |
| `outcome.candidates` / `error` | 从 transcript 里的 `FINAL:` 块解析（复用 runner 自己的解析器） |
| `elapsed_sec` | 借的壳 scan 的 `duration_sec`（`KernelChallenge` 自己没有耗时列） |

## ⚠️ `turns` 不能靠"数 `[turn N]` 行"

第一版我打算数 `^\\[turn \\d+\\]` 的行数 —— **那是错的**。理由：

* 一行 transcript 可能包含**多个** turn（实测：一个文件里 `[turn 1] tool …`
  与 `[turn 2] assistant(content): ACTION …` 交替出现，而 `[turn N]` 行数
  210 ≠ 真实轮数）；
* `outcome.turns` 在生产代码里是**循环变量**（`for turn in range(1, max_turns+1)`
  里 `outcome.turns = turn`），语义是"跑到第几轮"，不是"有多少行"。

**正确做法：取 transcript 里出现过的最大 turn 号。** 那与循环变量的
终值一致（循环是顺序递增的，最后一轮就是最大值）。

## ⚠️ `sanitize=False` 必须传

`writeup.build_writeup` 内部调
`notes.key_steps_from_transcript(..., sanitize=False)` ——
题解**不脱敏**（靶机地址与 flag 正是题解该有的内容，见那个函数的说明）。
本脚本**复用 `build_writeup` 本身**，所以这一点自动成立；
**千万不要**为了"重建得更干净"而绕过它自己拼题解 —— 那样会漏掉
`_humanize_digest`、`MAX_STEPS` 截断、按模型分组的 token 汇总等
一整层已经调好的逻辑。

## 幂等

`ctf2_writeups` 有 `uq_ctf2_writeups_challenge`（一题一份），
且本脚本**先查后写**（已有题解的题直接跳过）。
重跑不会产生第二行，也不会覆盖已有题解（除非显式 `--overwrite`）。

## 为什么不是 alembic 迁移

这是**数据修复**，不是 schema 变更。迁移会在每次部署时对**所有**环境
重跑一遍逻辑，而它依赖 `_scan_runs/_ctf2_solves/` 下的**部署本地文件**——
那些文件不在版本库里，别的环境根本没有。迁移里做这件事必然失败或静默跳过。

## 用法

    cd suvex-api
    .venv/bin/python scripts/backfill_ctf2_writeups.py --dry-run   # 先看会改什么
    .venv/bin/python scripts/backfill_ctf2_writeups.py             # 真的写
    .venv/bin/python scripts/backfill_ctf2_writeups.py --overwrite # 连已有的也重建

⚠️ **必须在 Kali 上跑**（transcript 文件在那里）。本机没有这些文件。
"""

from __future__ import annotations

import argparse
import contextlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 与 `scripts/` 下其它脚本同一套写法（见 probe_ctf2_endpoints.py）：
# 把仓库根加进 sys.path，脚本才能直接 `python scripts/xxx.py` 跑。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# 中文输出在 Windows/容器里默认编码可能不是 UTF-8 —— 不重配的话
# 打印题名会抛 UnicodeEncodeError，而那看起来像脚本坏了。
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):  # pragma: no cover
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

from sqlalchemy import select  # noqa: E402

from app.core.db import session_scope  # noqa: E402
from app.core.vocab import KernelChallengeStatus  # noqa: E402
from app.kernel import runner, service  # noqa: E402
from app.kernel import writeup as writeup_mod  # noqa: E402
from app.kernel.models import KernelChallenge  # noqa: E402
from app.scans.models import Scan  # noqa: E402

#: transcript 存放处。与 `runner._save_transcript` 的落点必须一致。
SOLVES_DIR = PROJECT_ROOT / "_scan_runs" / "_ctf2_solves"

#: 取 transcript 里出现过的最大 turn 号（见模块 docstring 的说明）。
_TURN_LINE = re.compile(r"^\[turn (\d+)\]", re.MULTILINE)


def _max_turn(transcript: str) -> int:
    """transcript 里出现过的最大轮次号。

    为什么不是"数行"：见模块 docstring。这里取最大值 —— 它与生产代码里
    那个循环变量的终值一致。
    """
    return max((int(m) for m in _TURN_LINE.findall(transcript)), default=0)


@dataclass
class _Outcome:
    """重建出来的 `SolveOutcome` 替身。

    ⚠️ 刻意**不用** `runner.SolveOutcome` 本尊：那个类还带
    `usages` / `saw_tool_output` 等 v0.31+ 的字段，而回填这批题时
    **它们本来就该是空的**（那时还没记 token）。用一个最小替身能保证
    "不会假装有那些数据"。`build_writeup` 只按属性名取值，所以够用。
    """

    transcript: str = ""
    turns: int = 0
    candidates: list = field(default_factory=list)
    error: str | None = None


def _parse_outcome(transcript: str) -> _Outcome:
    """从 transcript 重建 outcome：轮数 + 候选 + 失败原因。

    候选与失败原因**复用 runner 自己的解析器**（`parse_final` /
    `extract_candidates_with_reasons`）—— 不自己写正则。
    理由：那套解析器处理过实测遇到的模型习惯（```json 围栏、JSON 后
    跟解释文字、坏候选逐条丢弃），重写一份只会更差，而且两份口径迟早漂移。
    """
    outcome = _Outcome(transcript=transcript, turns=_max_turn(transcript))

    final = runner.parse_final(transcript)
    if final:
        candidates, _rejected = runner.extract_candidates_with_reasons(final)
        outcome.candidates = list(candidates)
        summary = final.get("summary")
        if isinstance(summary, str) and summary.strip():
            # 未解出的题把模型自己的总结当"失败原因" —— 那是题解里
            # "卡在哪"那一段的来源，比留空有用。
            outcome.error = summary.strip()
    return outcome


def _transcript_for(challenge_id: str) -> Path | None:
    """这道题的 transcript 文件。多次解题时取**最新**那份。

    为什么取最新：同一道题可能解过几轮，最后那份最接近最终结论
    （`_save_transcript` 每次收尾都写一份新文件，不覆盖旧的）。
    文件名的 `<时间戳>` 是排序键。
    """
    if not SOLVES_DIR.is_dir():
        return None
    matches = sorted(SOLVES_DIR.glob(f"{challenge_id}-*.txt"))
    return matches[-1] if matches else None


def _elapsed_sec(db, challenge: KernelChallenge) -> int | None:
    """借的壳 scan 的耗时。没有就返回 None（题解会写"未记录"）。"""
    if not challenge.scan_id:
        return None
    scan = db.get(Scan, challenge.scan_id)
    return getattr(scan, "duration_sec", None) if scan is not None else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="只报告会做什么，不写库"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="连已有题解的题也重建（默认跳过，避免覆盖 v0.36 之后生成的真题解）",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="只处理前 N 道（0 = 全部，调试用）"
    )
    args = parser.parse_args()

    if not SOLVES_DIR.is_dir():
        print(f"FATAL: transcript 目录不存在：{SOLVES_DIR}")
        print("（这个脚本必须在 Kali 上跑 —— transcript 是部署本地文件）")
        return 1

    filled = skipped_have_wp = skipped_no_transcript = failed = 0
    rows: list[tuple[str, str, int, int]] = []

    with session_scope() as db:
        challenges = list(
            db.execute(
                select(KernelChallenge)
                .where(KernelChallenge.status == KernelChallengeStatus.SOLVED.value)
                .order_by(KernelChallenge.solved_at)
            )
            .scalars()
            .all()
        )
        if args.limit:
            challenges = challenges[: args.limit]

        print(f"库里已解出的题：{len(challenges)} 道\n")

        for challenge in challenges:
            existing = service.get_writeup(db, challenge.id)
            if existing is not None and not args.overwrite:
                skipped_have_wp += 1
                continue

            path = _transcript_for(challenge.id)
            if path is None:
                skipped_no_transcript += 1
                print(f"  [无 transcript] {challenge.title}")
                continue

            transcript = path.read_text(encoding="utf-8", errors="replace")
            outcome = _parse_outcome(transcript)
            elapsed = _elapsed_sec(db, challenge)

            try:
                markdown = writeup_mod.build_writeup(
                    challenge=challenge,
                    outcome=outcome,
                    solved=True,  # 这批题都是 solved（上面的 where 保证）
                    elapsed_sec=elapsed,
                )
            except Exception as exc:  # noqa: BLE001 - 单题失败不该中断整批
                failed += 1
                print(f"  [生成失败] {challenge.title}: {type(exc).__name__}: {exc}")
                continue

            if args.dry_run:
                filled += 1
                rows.append((challenge.title, path.name, outcome.turns, len(markdown)))
                continue

            # 写库走 service 自己的 upsert（一题一份、重解覆盖）——
            # 不自己 INSERT：那会绕过 `uq_ctf2_writeups_challenge` 的
            # 处理逻辑与 updated_at 语义。
            service.save_writeup(
                db,
                challenge=challenge,
                outcome=outcome,
                attempt_id=None,  # 这批题没有受理快照（v0.28 之前）
                elapsed_sec=elapsed,
            )
            filled += 1
            rows.append((challenge.title, path.name, outcome.turns, len(markdown)))

    print(f"\n{'（dry-run，未写库）' if args.dry_run else '已写库'}")
    print(f"  回填：{filled}")
    print(f"  跳过（已有题解）：{skipped_have_wp}")
    print(f"  跳过（无 transcript）：{skipped_no_transcript}")
    print(f"  生成失败：{failed}")

    if rows:
        print(f"\n{'将要回填' if args.dry_run else '已回填'}的 {len(rows)} 道：")
        for title, fname, turns, size in rows:
            print(f"  {title:<34} turns={turns:<4} {size:>6} 字符  ({fname})")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
