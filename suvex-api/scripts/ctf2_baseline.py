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
import re
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
from app.ctf2 import notes  # noqa: E402

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

    #: 这一题注入的方法卡**字符数**（0 = 没有注入）。
    #:
    #: ⚠️ 必须记下来，不能只看"我传了 --notes-cards"。注入链路断掉时
    #: （渲染出空串、分类对不上、文件读取失败）两个臂跑出来一模一样，
    #: 而报告上会写着"方法卡无效" —— 那是把"没接上"测成了"没用"。
    #: 有这一列，"0 字符"就是当场能看见的证据。
    notes_chars: int = 0


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

    # 开跑之前就已经解过的题。非空 → 这次基线的数字**不可信**：
    # 那些题再跑一遍会被平台的"重复提交"挡住，判出来的 ✗ 是假失败。
    # 记进 JSON 里，而不是只在控制台喊一嗓子 —— 看报告的人不该依赖
    # 谁有没有盯着终端看。
    pre_solved: list[int] = field(default_factory=list)

    #: 这次用的是哪一份方法卡文件（空 = 这一臂不注入方法卡）。
    #:
    #: ⚠️ 记的是**路径**而不是"有没有开开关"：A/B 的两个 JSON 摆在一起时，
    #: 光看 `passed` 无法判断两臂到底差在哪。有了这一列，`compare`
    #: 能直接指出"这两次跑的不是同一个配置"—— 那是比通过数更先要知道的事。
    notes_cards: str = ""

    #: 这一臂一共注入了几道题（`notes_chars > 0` 的题数）。
    #:
    #: 与 `notes_cards` 配合读：给了文件却 0 题注入 → 召回规则没命中
    #: （分类对不上 / 卡全是自己的 / 文件是空的），这时两臂等价，
    #: 跑出来的差异全是噪声。
    notes_injected: int = 0

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
    cards: list | None = None,
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

    `cards` 给定时才有方法卡（`notes_cards.NoteCard` 列表），否则与
    改动前完全一致。召回规则在 `notes_cards.cards_for` 里，与生产
    `notes.notes_for_challenge` 逐条对齐。
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

        # ★ 方法卡：没有 cards 就是空串（= 与改动前逐字一致的那一臂）。
        #
        #   `exclude_challenge_id` 传 `arena-<id>`，与 `SolveSpec.challenge_id`
        #   同一个形状 —— 生产里"排除自己"靠的也是这个键。写成
        #   `#<id>` 之类的话排除会失效，那题会读到自己上次的卡，
        #   而那不是"同类题的经验"（那是 resume 该做的事）。
        notes_text = ""
        if cards:
            import notes_cards

            notes_text = notes_cards.render_for(
                cards, category, exclude_challenge_id=f"arena-{exercise_id}"
            )
        result.notes_chars = len(notes_text)

        outcome = await asyncio.wait_for(
            arena_e2e.solve(
                exercise,
                provider=provider,
                allow_network=False,
                max_turns=max_turns,
                transcript=transcript,
                notes_text=notes_text,
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
        notes_cards=str(getattr(args, "notes_cards", "") or ""),
    )

    # ★ 方法卡：读一次，全程复用（每道题只是换 category 去召回）。
    #
    #   ⚠️ 读失败**直接退出**，而不是"退化成不带卡继续跑"。
    #      后者跑出来的是一份"没开方法卡"的基线，而文件名和命令行
    #      都写着开了 —— 那种报告比没有报告更坏。
    cards: list = []
    if report.notes_cards:
        import notes_cards as notes_cards_mod

        cards_path = Path(report.notes_cards)
        if not cards_path.is_file():
            log(f"❌ 方法卡文件不存在：{cards_path}")
            return 2
        cards = notes_cards_mod.load_cards(cards_path)
        log(f"方法卡：{cards_path} → {notes_cards_mod.describe(cards)}")

    log("=" * 72)
    log(
        f"CTF2 基线 · commit {report.git_commit}"
        f" · {model_label or '?'}/{model_name or '?'}"
        f" · 最多 {args.max_turns} 回合/题"
        + (f" · 方法卡 {'开' if cards else '关'}" if report.notes_cards or cards else "")
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

        # ★ 脏基线检测：开跑之前已经解过的题，这次再跑必然被平台的
        # "重复提交"挡住（A0409），判出来的 ✗ 是假失败。
        #
        # 这条检查是**用一整个 9/10 换来的**：#1001 在单题验证时已经解过，
        # 接着跑全量基线，它交了正确答案却得到"重复提交"，基线因此少了一题。
        # 当时差一点就去调模型能力了 —— 真正的毛病在测试夹具没重置。
        report.pre_solved = sorted(e.exercise_id for e in targets if e.has_solved)
        if report.pre_solved:
            log("⚠️ 这些题在本次跑之前就已经解过：")
            for exercise in targets:
                if exercise.has_solved:
                    log(f"   · #{exercise.exercise_id} {exercise.name}")
            log("   它们这次必然以「重复提交」告终，通过数会偏低。")
            log("   想让数字可信 → 先重启 mock（它把 hasSolved 存在进程内存里）。")
            log("")

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
                cards=cards,
            )
            report.runs.append(result)
            if result.notes_chars:
                report.notes_injected += 1
            verdict = "✓ 通过" if result.ok else "✗ 未通过"
            log(
                f"→ {verdict} | 回合 {result.turns} | 候选 {result.candidates} | "
                f"{result.elapsed_s}s"
                + (f" | 方法卡 {result.notes_chars} 字符" if cards else "")
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

    # ★ 方法卡到底注进去了没有 —— 不给这一行的话，"两臂一样"会被
    #   读成"方法卡没用"，而真相可能是 0 题召回（分类对不上）。
    if report.notes_cards:
        log(
            f"方法卡：{report.notes_injected}/{len(report.runs)} 道题注入了内容"
            + ("" if report.notes_injected else "  ⚠️ 一道都没注入 → 这一臂等于没开，别拿它当对照组")
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


# ══════════════════════════════════════════════════════════
# 子命令：learn（把 transcript 变成方法卡）
# ══════════════════════════════════════════════════════════

#: transcript 里的 NUL 字节。
#:
#: 工具输出（`xxd`、`strings`、二进制 hexdump）会把这些直接写进日志，
#: 于是 `grep` 报 "binary file matches"、`read_text` 在某些编码下抛
#: 一半截断。落盘时它们毫无信息量，读的时候先剔掉。
_NUL = "\x00"

#: 落盘前的最后一道闸：卡里出现这些形状就拒绝写。
#:
#: ⚠️ `build_note_fields` 已经在逐字段脱敏了，这里再查一遍不是重复劳动 ——
#: 它是**独立的一道**，因为我改不动生产那份脱敏的效果，而这张卡会被拼进
#: **另一道题**的 prompt。漏一次就是"A 题的答案进了 B 题的上下文"。
#: 而且这一步很便宜（就是几个正则），便宜到没有理由不做。
_FLAG_SHAPES = (
    re.compile(r"\b(?:ctf|dasctf|sctf|nssctf)\{", re.IGNORECASE),
    # 长串 base32/hex/base64ish —— 真 flag 常被切碎了嵌在这些里
    re.compile(r"\b[A-Za-z0-9+/]{32,}={0,2}\b"),
)


def _card_leaks(card) -> list[str]:
    """返回这张卡里**不该出现**的片段（空 = 干净）。

    ## ⚠️ `flag{` 这一条为什么不在 `_FLAG_SHAPES` 里

    它原来是第一条 `re.compile(r"\\bflag\\{", re.IGNORECASE)` —— 见到
    `flag{` 就拒收。脱敏侧修好"别把 `grep -ao 'flag{[^}]*}'` 吃成
    `grep -ao ']*}'`"之后，这条闸门**反过来把刚保住的搜索针当成泄漏**，
    `#1004` 和 `#1010` 两张卡被整张丢弃（实测：脱敏修好了、卡却学不到）。

    所以这一条改用 `notes.looks_like_flag_search_needle()` —— 与脱敏
    共用同一份"位置判据"：保住的是搜索针，删掉的才是答案。两处各写一份
    就会重演这次的分叉。
    """
    blob = f"{card.approach}\n{card.tools}\n{card.dead_ends}"
    hits: list[str] = []
    for pattern in _FLAG_SHAPES:
        for match in pattern.finditer(blob):
            hits.append(match.group(0)[:40])
    # `flag{...}` 形状单独判：只算"脱敏会删掉的那些"，搜索针放过。
    if re.search(r"\bflag\{", blob, re.IGNORECASE) and not (
        notes.looks_like_flag_search_needle(blob)
    ):
        hits.append("flag{")
    return hits


def cmd_learn(args: argparse.Namespace) -> int:
    import notes_cards

    result_path = Path(args.result)
    if not result_path.is_file():
        log(f"❌ 基线 JSON 不存在：{result_path}")
        return 2
    payload = _load(result_path)

    transcript_dir = Path(args.transcripts)
    if not transcript_dir.is_dir():
        log(f"❌ transcript 目录不存在：{transcript_dir}")
        return 2

    cards: list = []
    skipped: list[str] = []
    for run in payload.get("runs", []):
        exercise_id = run.get("exercise_id")
        log_path = transcript_dir / f"transcript-{exercise_id}.log"
        if not log_path.is_file():
            # 没留过程 → 学不出东西。**不编造一张空卡**：空卡进池子以后
            # 只会占掉 MAX_NOTES_IN_PROMPT 的位置，把有内容的卡挤出去。
            skipped.append(f"#{exercise_id} 没有 transcript")
            continue
        transcript = log_path.read_text(encoding="utf-8", errors="replace").replace(_NUL, "")
        card = notes_cards.learn_card(
            challenge_id=f"arena-{exercise_id}",
            category=run.get("category") or "",
            title=run.get("name") or "",
            transcript=transcript,
            error=run.get("error"),
            candidates=list(run.get("tried") or []),
            solved=bool(run.get("ok")),
        )
        leaks = _card_leaks(card)
        if leaks:
            # ⚠️ 丢弃**整张卡**，不是"把那一段删掉再写"：脱敏漏掉一个
            #    flag 形状，说明我对这条链路的判断已经不成立了，
            #    "只删掉匹配到的那几个字节"是最没有根据的补法。
            log(f"  ✗ #{exercise_id} 的卡含疑似答案，整张丢弃：{leaks[:3]}")
            continue
        cards.append(card)
        # ★ 学习顺序必须真的递增。
        #
        #   生产里次级排序键是 `updated_at.desc()`；文件形态没有时间戳，
        #   只能自己排。**留全 0 不是"无害的缺省"**：`cards_for` 的
        #   `(solved, seq)` 会全部并列，于是"最近学的优先"退化成一个
        #   不稳定的顺序 —— 池子小的时候看不出来，一过 3 张就开始
        #   随机丢掉新学的卡。
        card.seq = len(cards)
        log(
            f"  #{exercise_id} {run.get('name', '')} → "
            f"做法 {len(card.approach)} 字 / 工具 {len(card.tools)} 字 / "
            f"死胡同 {len(card.dead_ends)} 字"
        )

    if not cards:
        log("❌ 一道题都没学成（一条 transcript 都没有？）")
        for reason in skipped:
            log(f"   · {reason}")
        return 2

    out = notes_cards.save_cards(cards, Path(args.out))
    log(f"\n方法卡 {len(cards)} 张 → {out}（{notes_cards.describe(cards)}）")
    for reason in skipped:
        log(f"   ⚠️ 跳过 {reason}")
    return 0


def _warn_if_incomparable(before: dict, after: dict) -> None:
    """两臂配置不同时先喊出来 —— 比通过数更先要知道的事。

    ⚠️ 只警告不拦：有时候就是要比"带卡 vs 不带卡"，那本来就不一样。
    但**悄悄放过去不行**：一份"pass 9 → 10"的对比，如果读者不知道
    后一臂还换了模型或回合上限，那个 +1 会被当成方法卡的功劳。
    """
    notes_before = before.get("notes_cards") or ""
    notes_after = after.get("notes_cards") or ""
    if notes_before != notes_after:
        log(
            f"\n⚠️ 方法卡配置不同：前 {'开（' + notes_before + '）' if notes_before else '关'}"
            f" / 后 {'开（' + notes_after + '）' if notes_after else '关'}"
        )
        injected = after.get("notes_injected")
        if notes_after and not injected:
            log("   ⚠️ 后一臂给了卡文件，但 0 道题注入了内容 → 召回没命中，两臂其实等价。")
    for key, name in (("max_turns", "回合上限"), ("model_name", "模型")):
        if before.get(key) != after.get(key):
            log(f"⚠️ {name}不同：{before.get(key, '?')} → {after.get(key, '?')}")


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

    _warn_if_incomparable(before, after)

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
    run.add_argument(
        "--notes-cards",
        default="",
        help="同类题方法卡 JSON（scripts/notes_cards.py 生成）；不给 = 不注入，与改动前一致",
    )
    run.set_defaults(func=lambda args: asyncio.run(cmd_run(args)))

    compare = sub.add_parser("compare", help="对比两次基线")
    compare.add_argument("before")
    compare.add_argument("after")
    compare.set_defaults(func=cmd_compare)

    # 学卡：把已有的 transcript 变成方法卡。
    # 单独一个子命令而不是塞进 run，是因为"跑题"和"造卡"是两件事：
    # 造卡只要 transcript 就够，不必再烧一遍 LLM 的钱。
    learn = sub.add_parser("learn", help="从 transcript 生成方法卡 JSON")
    learn.add_argument("transcripts", help="transcript 目录（含 transcript-<id>.log）")
    learn.add_argument("--result", required=True, help="同一次跑出来的基线 JSON（取分类/通过与否）")
    learn.add_argument("--out", default="_tmp/notes-cards.json", help="方法卡落盘路径")
    learn.set_defaults(func=cmd_learn)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
