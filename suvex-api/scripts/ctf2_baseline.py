"""CTF2 回归基线：每类 2 道已知答案的题，跑一遍，记下 成功/回合数/耗时。

## 为什么要有这个东西

在这之前，所有改动（给 pwn 装 gdb、改提示词、加工具）都只有一个说法：
「我觉得这样更好」。没有数字，就无法回答最该回答的问题 ——
**这次改动到底让模型更强了还是更弱了？**

这个脚本把「我觉得」变成「上一次 10 道过 6 道、平均 11.4 回合；
这一次 10 道过 8 道、平均 8.9 回合」。哪怕样本只有 10 道，前后的对比
也远胜过凭印象。

## 它跑的是什么

复用 `arena_e2e.py` 那条链路（拉题 → 起环境 → 沙箱解题 → 提交），
只是把「跑一道」换成「跑一批，并记下来」。题目来自
`mock_arena_server.py`（5 类 × 2 道，flag 都藏在可解的附件里）。

## 跑法（需要 Docker + 可用的 LLM provider，所以在 Kali 上跑）

    # 1) 起 mock
    python scripts/mock_arena_server.py --port 8901 --key ak_test_local &

    # 2) 跑基线（10 道题，每题最多 20 回合）
    python scripts/ctf2_baseline.py run --out _tmp/baseline-before.json

    # 3) 改点东西

    # 4) 再跑一遍，然后对比
    python scripts/ctf2_baseline.py run --out _tmp/baseline-after.json
    python scripts/ctf2_baseline.py compare _tmp/baseline-before.json _tmp/baseline-after.json

## 读结果的规矩

* `ok=False` 且 `error` 为空 → 模型跑完了但**没解出来**（能力问题，是真信号）；
* `ok=False` 且有 `error` → 先看错误（可能是环境/网络/超时，不是模型不行）；
* 对照 `check_mock_attachments.py` 先排除「题目本身解不出来」。

⚠️ 这个脚本**不进 CI**：它要 Docker、要真实的 LLM 调用、要几分钟。
它是一次性的度量工具，不是每次提交都要跑的门禁。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SCRIPTS))

# ── 复用 arena_e2e 的既有链路（不重写一遍）──────────────
import arena_e2e  # noqa: E402

from app.arena.client import ArenaClient  # noqa: E402

DEFAULT_OUT = PROJECT_ROOT / "_tmp" / "ctf2-baseline.json"


def log(line: str = "") -> None:
    print(line, flush=True)


def git_commit() -> str:
    """记下这份基线是哪个 commit 跑出来的（否则"改进"无法归因）。

    允许用 `CTF2_GIT_COMMIT` 覆盖：基线是在**有 Docker 的那台机器**上跑的
    （目前是 Kali），而那边往往只是 `tar` 过去的一堆文件、不是 git 仓库，
    `git rev-parse` 会失败。这时候由发起方在环境变量里把 commit 传进来，
    比事后对着 "unknown" 猜"这到底是哪版"要可靠。
    """
    override = os.environ.get("CTF2_GIT_COMMIT", "").strip()
    if override:
        return override
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


# ══════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════


@dataclass
class RunResult:
    """一道题跑一次的结果。"""

    exercise_id: int
    category: str
    name: str
    ok: bool = False
    turns: int = 0
    candidates: int = 0
    elapsed_s: float = 0.0
    error: str | None = None
    #: 模型给出的候选 flag（便于人工核对"它到底错在哪"）
    tried: list[str] = field(default_factory=list)
    #: 解题过程的落盘路径（空 = 没留）。失败时先看这个，再谈"能力"。
    transcript: str = ""


@dataclass
class RunReport:
    """整份基线。"""

    started_at: str
    finished_at: str = ""
    git_commit: str = ""
    model_label: str = ""
    model_name: str = ""
    max_turns: int = 0
    base_url: str = ""
    runs: list[RunResult] = field(default_factory=list)

    # ── 汇总 ──────────────────────────────────────────────
    def summary(self) -> dict[str, object]:
        by_category: dict[str, dict[str, int]] = {}
        for run in self.runs:
            bucket = by_category.setdefault(run.category, {"total": 0, "passed": 0})
            bucket["total"] += 1
            bucket["passed"] += 1 if run.ok else 0

        passed = [r for r in self.runs if r.ok]
        failed = [r for r in self.runs if not r.ok]
        return {
            "total": len(self.runs),
            "passed": len(passed),
            "failed": len(failed),
            "avg_turns_all": round(sum(r.turns for r in self.runs) / len(self.runs), 2)
            if self.runs
            else 0.0,
            "avg_turns_passed": round(sum(r.turns for r in passed) / len(passed), 2)
            if passed
            else 0.0,
            "avg_elapsed_s": round(sum(r.elapsed_s for r in self.runs) / len(self.runs), 1)
            if self.runs
            else 0.0,
            "by_category": by_category,
        }


# ══════════════════════════════════════════════════════════
# 跑一道题的
# ══════════════════════════════════════════════════════════


async def run_one(
    client: ArenaClient,
    exercise_id: int,
    *,
    category: str,
    base_url: str,
    provider: str | None,
    max_turns: int,
    timeout_s: float,
    transcript_dir: Path | None = None,
) -> RunResult:
    """跑一道题：拉题 → 起环境 → 解题 → 提交，全程计时。

    `category` 得由调用方从题目列表传进来，**不能**从详情里取 ——
    `ensure_environment()` 返回的是详情对象，而详情接口里没有
    `category` 字段（分类只出现在列表接口的父节点上）。写基线的时候
    踩过这个：从详情取，10 道题全被记成空分类，`by_category` 汇总
    直接失效，而结果是"看起来跑完了"。

    `transcript_dir` 给定时，每道题的解题过程会落成
    `transcript-<id>.log`。**失败的时候没有比这更值钱的东西**：
    "8 回合用尽"可以是模型不会，也可以是靶机连不上、工具没装、
    提示词没说清楚 —— 这三种的修法完全不同，而只有过程能分辨。
    """
    started = time.monotonic()
    result = RunResult(exercise_id=exercise_id, category=category, name=f"#{exercise_id}")
    transcript: list[str] = []

    try:
        exercise = await arena_e2e.ensure_environment(client, exercise_id, no_env=False)
        result.name = exercise.name

        # ★★ 详情接口不带 `category`，但它是两个东西的输入：
        #    ① `solver.profile_for_category()` —— 决定用哪个镜像，
        #       Reverse 题要是被当成"无分类"，会落到基础镜像上，
        #       那一跑就等于没测 reverse；
        #    ② 提示词里的分类工具表。
        #    所以必须把列表里的分类补回详情对象，否则失败原因
        #    会指向"模型 reverse 不行"，而实际是"镜像装错了"。
        #
        # ⚠️ 只能 replace()，不能赋值：`Exercise` 是
        #    `@dataclass(frozen=True, slots=True)`（app/arena/client.py:145），
        #    `exercise.category = ...` 会抛 FrozenInstanceError 并被下面的
        #    `except Exception` 吞成一条错误记录 —— 第一次在 Kali 上跑基线
        #    就是这么"跑完了但 0 回合"的。
        exercise = replace(exercise, category=category)
        exercise.raw["_base_url"] = base_url

        result.turns = -1  # 标记"已进入解题，但还没拿到 outcome"
        outcome = await asyncio.wait_for(
            arena_e2e.solve(
                exercise,
                provider=provider,
                allow_network=False,
                max_turns=max_turns,
                transcript=transcript,
            ),
            timeout=timeout_s,
        )
        result.turns = outcome.turns
        result.candidates = len(outcome.candidates)
        result.tried = [candidate.flag for candidate in outcome.candidates]
        result.error = outcome.error
        result.ok = await arena_e2e.submit_all(client, exercise_id, outcome)

    except TimeoutError:
        # ★ 超时和"没解出来"是两件事，必须分开记 —— 混在一起会把
        #   环境问题误读成能力问题。
        result.error = f"超时（{timeout_s:.0f}s）"
    except Exception as exc:  # noqa: BLE001 - 基线要跑完全部题目，不能因一道崩掉
        result.error = f"{exc.__class__.__name__}: {exc}"
    finally:
        result.elapsed_s = round(time.monotonic() - started, 1)
        if result.turns < 0:
            result.turns = 0

        # 过程落盘。放在 finally 里、且自己吞掉异常 —— 写日志失败
        # 不该把这道题已经拿到的结果一起丢掉。
        if transcript_dir is not None and transcript:
            path = transcript_dir / f"transcript-{exercise_id}.log"
            with contextlib.suppress(OSError):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("\n".join(transcript), encoding="utf-8")
                result.transcript = str(path)
        # 回收失败不该影响基线结果 —— 真出问题的话，上面已经把原因
        # 记进 result.error 了，这里再抛一次只会把前面所有题的结果一起丢掉。
        with contextlib.suppress(Exception):
            await client.recover_environment(exercise_id)

    return result


# ══════════════════════════════════════════════════════════
# 子命令：run
# ══════════════════════════════════════════════════════════


async def cmd_run(args: argparse.Namespace) -> int:
    from datetime import datetime

    # 记下用的是哪个模型 —— compare 的表头要靠它判断"两次跑的是不是同一个模型"。
    # 拿不到不该让基线跑不起来，所以失败就留空（表头会显示 "?"）。
    try:
        creds = arena_e2e.load_provider(args.provider)
        model_label, model_name = creds.label, creds.model
    except Exception:  # noqa: BLE001 - 元数据缺失不该拦住度量本身
        model_label, model_name = "", ""

    report = RunReport(
        started_at=datetime.now().isoformat(timespec="seconds"),
        git_commit=git_commit(),
        model_label=model_label,
        model_name=model_name,
        max_turns=args.max_turns,
        base_url=args.base_url,
    )

    log("=" * 72)
    log(
        f"CTF2 基线 · commit {report.git_commit}"
        f" · {model_label or '?'}/{model_name or '?'}"
        f" · 最多 {args.max_turns} 回合/题"
    )
    log("=" * 72)

    async with ArenaClient(base_url=args.base_url, access_key=args.key) as client:
        exercises = await client.flatten_exercises()
        if not exercises:
            log("❌ 平台没返回任何题目 —— mock 起了吗？")
            return 2

        wanted = set(args.ids) if args.ids else None
        targets = [e for e in exercises if wanted is None or e.exercise_id in wanted]
        if wanted:
            missing = wanted - {e.exercise_id for e in targets}
            if missing:
                log(f"❌ 这些题不在列表里：{sorted(missing)}")
                return 2

        log(f"共 {len(targets)} 道题要跑：")
        for exercise in targets:
            log(f"   · [{exercise.category}] #{exercise.exercise_id} {exercise.name}")
        log("")

        # 解题过程落在 --out 旁边的 transcripts/ 下。没有 --out 就不留
        # （过程可能几十 MB，不该在"只看一眼通过数"的时候占盘）。
        transcript_dir = Path(args.out).parent / "transcripts" if args.out else None

        for index, exercise in enumerate(targets, start=1):
            log("-" * 72)
            log(f"[{index}/{len(targets)}] #{exercise.exercise_id} {exercise.name}")
            log("-" * 72)
            result = await run_one(
                client,
                exercise.exercise_id,
                category=exercise.category,
                base_url=args.base_url,
                provider=args.provider,
                max_turns=args.max_turns,
                timeout_s=args.timeout,
                transcript_dir=transcript_dir,
            )
            report.runs.append(result)
            verdict = "✓ 通过" if result.ok else "✗ 未通过"
            log(
                f"→ {verdict} | 回合 {result.turns} | 候选 {result.candidates} | "
                f"{result.elapsed_s}s"
                + (f" | 错误 {result.error}" if result.error else "")
            )
            log("")

            # 每跑完一道就落盘一次 —— 中途崩了不至于全丢
            if args.out:
                _write(report, Path(args.out))

    from datetime import datetime

    report.finished_at = datetime.now().isoformat(timespec="seconds")
    summary = report.summary()

    log("=" * 72)
    log(f"结果：{summary['passed']}/{summary['total']} 通过")
    for category, bucket in sorted(summary["by_category"].items()):
        log(f"   {category:<10} {bucket['passed']}/{bucket['total']}")
    log(
        f"平均回合 {summary['avg_turns_all']}（通过的题 {summary['avg_turns_passed']}）"
        f" · 平均耗时 {summary['avg_elapsed_s']}s"
    )

    if args.out:
        path = _write(report, Path(args.out))
        log(f"\n已写入 {path}")
    log("=" * 72)

    return 0 if summary["failed"] == 0 else 1


def _write(report: RunReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(report)
    payload["summary"] = report.summary()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ══════════════════════════════════════════════════════════
# 子命令：compare（改动前后各跑一遍之后，看差在哪）
# ══════════════════════════════════════════════════════════


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_compare(args: argparse.Namespace) -> int:
    before = _load(Path(args.before))
    after = _load(Path(args.after))

    log("=" * 80)
    log(f"基线对比：{args.before}  →  {args.after}")
    log(
        f"  前：commit {before.get('git_commit', '?')} · "
        f"{before.get('model_label', '?')}/{before.get('model_name', '?')} · "
        f"最多 {before.get('max_turns', '?')} 回合"
    )
    log(
        f"  后：commit {after.get('git_commit', '?')} · "
        f"{after.get('model_label', '?')}/{after.get('model_name', '?')} · "
        f"最多 {after.get('max_turns', '?')} 回合"
    )
    log("=" * 80)

    rows_before = {run["exercise_id"]: run for run in before.get("runs", [])}
    rows_after = {run["exercise_id"]: run for run in after.get("runs", [])}
    all_ids = sorted(set(rows_before) | set(rows_after))

    log(f"\n{'题目':<28}{'前':<14}{'后':<14}变化")
    log("-" * 80)

    improved = worsened = same = 0
    for exercise_id in all_ids:
        old = rows_before.get(exercise_id)
        new = rows_after.get(exercise_id)
        label = f"#{exercise_id} {(new or old)['name']}"

        def cell(row: dict | None) -> str:
            if row is None:
                return "—"
            mark = "✓" if row["ok"] else "✗"
            return f"{mark} {row['turns']}回合"

        if old is None:
            change = "新增"
        elif new is None:
            change = "本次没跑"
        elif not old["ok"] and new["ok"]:
            change, improved = "★ 由败转成", improved + 1
        elif old["ok"] and not new["ok"]:
            change, worsened = "⚠ 由成转败", worsened + 1
        else:
            delta = new["turns"] - old["turns"]
            if delta == 0:
                change, same = "不变", same + 1
            else:
                change = f"回合 {delta:+d}"
                same += 1

        log(f"{label:<28}{cell(old):<14}{cell(new):<14}{change}")

    log("\n" + "-" * 80)
    summary_before = before.get("summary", {})
    summary_after = after.get("summary", {})
    log(
        f"通过数 {summary_before.get('passed', '?')} → {summary_after.get('passed', '?')}"
        f" | 由败转成 {improved} | 由成转败 {worsened}"
    )
    log(
        f"平均回合 {summary_before.get('avg_turns_all', '?')} → "
        f"{summary_after.get('avg_turns_all', '?')}"
    )
    log("=" * 80)
    return 0


# ══════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CTF2 回归基线")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="跑一遍基线")
    run.add_argument("--base-url", default="http://127.0.0.1:8901")
    run.add_argument("--key", default="ak_test_local", help="X-Agent-AccessKey")
    run.add_argument("--ids", type=int, nargs="*", default=None, help="只跑这些题（默认全部）")
    run.add_argument("--provider", default=None, help="label 含这个子串的 provider")
    run.add_argument("--max-turns", type=int, default=20)
    run.add_argument("--timeout", type=float, default=600.0, help="单题超时（秒）")
    run.add_argument("--out", default=str(DEFAULT_OUT), help="结果 JSON 落盘路径")
    run.set_defaults(func=lambda args: asyncio.run(cmd_run(args)))

    compare = sub.add_parser("compare", help="对比两次基线")
    compare.add_argument("before")
    compare.add_argument("after")
    compare.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
