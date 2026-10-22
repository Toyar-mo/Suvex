"""比赛平台（AI Agent API）的端到端演练：拉题 → 起环境 → 沙箱解题 → 提交。

## 它验证的正是比赛要用的那条链路

| 步骤 | 用到的接口 |
| --- | --- |
| 拉题目列表 | `GET /ctf/exercise-list` |
| 取题目详情（附件 + 靶机） | `GET /ctf/exercise?exerciseId=` |
| 起环境 | `POST /ctf/build-exercise-env` + 轮询详情到 `isNeedCheck=false` |
| **解题** | 本地沙箱 + LLM（复用 `app/kernel/runner.py`，与平台无关） |
| 提交 | `POST /answer-panel/answer` |
| 回收环境 | `POST /ctf/recover-exercise-env` |

## 关于靶机

mock 的 `endpoints` 是**假地址**（10.20.30.40 之类），沙箱连不上 —— 但
mock 的 flag 藏在**附件**里，所以解题不需要出网。默认因此保持
`--network none`（更安全）；真到比赛时靶机是真的，那时加 `--allow-network`。

## 跑法（需要 Docker，所以在 Kali 上跑）

    # 1) 起 mock（同机）
    python scripts/mock_arena_server.py --port 8901 --key ak_test_local &
    # 2) 端到端
    python scripts/arena_e2e.py --base-url http://127.0.0.1:8901 --key ak_test_local --exercise 1001

真比赛时把 `--base-url` / `--key` 换成赛事方给的即可，其余一行不用改。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402

from app.arena.client import ArenaClient, ArenaError, Exercise  # noqa: E402
from app.core.db import session_scope  # noqa: E402
from app.kernel import runner as solver  # noqa: E402

PROMPT = PROJECT_ROOT / "prompts" / "kernel_system.md"
ATTACH_DIR = PROJECT_ROOT / "_tmp" / "arena-attachments"


def log(line: str = "") -> None:
    print(line, flush=True)


# ══════════════════════════════════════════════════════════
# 各步骤
# ══════════════════════════════════════════════════════════


async def pick_exercise(client: ArenaClient, wanted: int | None) -> Exercise:
    """按 id 挑一道，或挑第一道没解出的。"""
    items = await client.flatten_exercises()
    log(f"① 拉到 {len(items)} 道题：")
    for item in items:
        mark = "已解" if item.has_solved else "未解"
        log(f"   · [{item.category}] #{item.exercise_id} {item.name}（{mark}）")
    if not items:
        raise SystemExit("平台没有返回题目")

    if wanted is not None:
        hit = next((i for i in items if i.exercise_id == wanted), None)
        if hit is None:
            raise SystemExit(f"题目 {wanted} 不在列表里")
        return hit
    return next((i for i in items if not i.has_solved), items[0])


async def ensure_environment(client: ArenaClient, exercise_id: int, *, no_env: bool) -> Exercise:
    """按文档第 4 条：需要就先起，然后轮询到就绪。"""
    detail = await client.exercise(exercise_id)
    if no_env or not detail.is_need_init:
        log(f"② 题目不需要起环境（isNeedInit={detail.is_need_init}）")
        return detail

    log("② 起环境（异步，之后要轮询详情）")
    await client.build_environment(exercise_id)
    started = time.monotonic()
    ready = await client.wait_until_ready(exercise_id, timeout_seconds=120, interval_seconds=2)
    log(f"   环境就绪，用时 {time.monotonic() - started:.1f}s，靶机 {len(ready.endpoints)} 条：")
    for endpoint in ready.endpoints:
        log(f"   · {endpoint.describe()}")
    return ready


def download_attachments(exercise: Exercise) -> tuple[Path, ...]:
    """把附件下到本地（`SolveSpec.attachments` 要的是本地路径）。"""
    if not exercise.attachments:
        log("③ 题目没有附件")
        return ()
    target_dir = ATTACH_DIR / str(exercise.exercise_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    # 先清掉上一次的残留：这个目录是跨运行复用的（_tmp 不随 git 走，也不自动清理），
    # 而它随后被整个只读挂进容器供模型浏览。题目改过附件名之后（mock 的 baby →
    # pwn_baby 就是这样），旧文件会和新文件一起出现在 /work/attachments 里 ——
    # 模型会花回合去分析一个题目根本没给的陈旧文件，而报告上不会留下任何痕迹。
    stale = [p for p in target_dir.iterdir() if p.is_file()]
    for path in stale:
        path.unlink(missing_ok=True)
    if stale:
        log(f"③ 清掉 {len(stale)} 个上次残留的附件：{'、'.join(p.name for p in stale)}")

    saved: list[Path] = []
    with httpx.Client(timeout=30, trust_env=False, follow_redirects=True) as http:
        for item in exercise.attachments:
            name = str(item.get("name") or "attachment")
            url = str(item.get("url") or "")
            if not url:
                continue
            if url.startswith("/"):
                # mock 给的是相对路径；真实平台会给绝对地址
                base = (exercise.raw.get("_base_url") or "").rstrip("/")
                url = base + url
            try:
                response = http.get(url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                log(f"③ 附件 {name} 下载失败：{exc.__class__.__name__}")
                continue
            path = target_dir / name
            path.write_bytes(response.content)
            saved.append(path)
            log(f"③ 附件 {name} → {path}（{len(response.content)} 字节）")
    return tuple(saved)


@dataclass(frozen=True, slots=True)
class LlmCredentials:
    """一次解题要用的 LLM 凭据。

    单独成型而不是散装五个变量，是为了让「凭据从哪来」这一层可以替换 ——
    见 credentials_from_env()。
    """

    model: str
    api_key: str
    base_url: str
    label: str
    kind: str


def credentials_from_env() -> LlmCredentials | None:
    """从环境变量取凭据；没配就返回 None。

    为什么要这条路：基线必须跑在**有 Docker 的机器**上（镜像在那儿），
    而那台机器未必有数据库。把「拿凭据」和「用凭据」分开，就不必为了
    读 providers 表里的一行记录，把整个后端连同数据库一起搬过去。

    变量名沿用 CTF2_LLM_* 前缀，与 runner 里的同名约定保持一致。
    """
    model = os.environ.get("CTF2_LLM_MODEL", "").strip()
    key = os.environ.get("CTF2_LLM_KEY", "").strip()
    if not model or not key:
        return None
    return LlmCredentials(
        model=model,
        api_key=key,
        base_url=os.environ.get("CTF2_LLM_BASE_URL", "").strip(),
        label=os.environ.get("CTF2_LLM_LABEL", "env").strip() or "env",
        kind=os.environ.get("CTF2_LLM_KIND", "cloud").strip() or "cloud",
    )


def load_provider(preferred: str | None) -> LlmCredentials:
    """取一个可用的 LLM。优先环境变量，其次 providers 表。

    ⚠️ 明文 key 只在这一层出现一次，用完即随 SolveSpec 交给 LLM（与 ctf2 那边同一条纪律）。
    """
    from_env = credentials_from_env()
    if from_env is not None:
        return from_env

    from sqlalchemy import select

    from app.providers.models import ProviderAccount
    from app.providers.service import decrypt_provider_key

    with session_scope() as db:
        stmt = select(ProviderAccount).order_by(
            ProviderAccount.is_default.desc(), ProviderAccount.created_at
        )
        rows = list(db.execute(stmt).scalars())
        if not rows:
            raise SystemExit(
                "库里没有配置任何 LLM provider，也没有设 CTF2_LLM_MODEL / CTF2_LLM_KEY"
            )
        row = next((r for r in rows if preferred and preferred in (r.label or "")), None) or rows[0]
        return LlmCredentials(
            model=row.model,
            api_key=decrypt_provider_key(row),
            base_url=row.base_url or "",
            label=row.label or "",
            kind=row.kind or "cloud",
        )


async def solve(
    exercise: Exercise,
    *,
    provider: str | None,
    allow_network: bool,
    max_turns: int,
    llm: LlmCredentials | None = None,
    transcript: list[str] | None = None,
    notes_text: str = "",
):
    """在沙箱里让模型解这道题（复用与平台无关的那套引擎）。

    `llm` 给定时直接用这份凭据，否则按 load_provider() 的顺序去找（环境变量 → providers 表）。

    `transcript` 给定时，解题过程的每一轮都会被追加进去（原文，含工具的
    完整输出）。**基线必须留下它**：只记"几回合、过没过"的话，一道题失败
    了也没法回答"是模型不会，还是工具装错了、提示词写歪了、靶机连不上" ——
    而那三个问题的修法完全不同。代价是可能很大（20 轮的 shell 输出），
    所以由调用方决定落不落盘、落到哪。

    `notes_text` 是**同类题方法卡**渲染好的片段，原样进 prompt。

    ## ⚠️ 为什么这个参数必须存在（2026-10-23 补）

    在这之前，这条链路**一次都没注入过方法卡**：`SolveSpec` 由本函数
    构造，而构造处没有 `notes_text=`，于是它一直是默认空串 ——
    生产 worker（`runner.py:4447`）走的是另一条路，那里才有召回。

    后果不是"少了个功能"，而是**没法做 A/B**：想测"方法卡有没有用"，
    两个臂必须能被区分开。链路没有口子的时候，两边跑出来是同一件事，
    而差异全被归进采样噪声 —— 那比不做实验更坏，因为它给了个数字。

    ⚠️ 传进来的内容**必须已经脱敏**（`notes.sanitize_for_reuse`，
    在 `build_note_fields` 里做的）。这里不再过滤：两处都做会让
    "谁负责"含混，而这条链路上漏一次就是跨题泄漏 flag。
    """
    creds = llm or load_provider(provider)
    model, api_key, base_url = creds.model, creds.api_key, creds.base_url
    label, kind = creds.label, creds.kind
    log(f"④ 用模型 {label}（{model}）")

    profile = solver.profile_for_category(exercise.category)

    # ★ 网络策略必须和正式 worker 一致（`runner.py:4406` 就是这么写的），
    #   不能一律 `--network none`。
    #
    #   pwn 要直连靶机、web 要做目录探测（镜像里本来就装着 gobuster / ffuf /
    #   nmap 和 4600 词的 dirb 字典）。隔离网络下这些工具**一次都用不上** ——
    #   实测过一道需要爆破隐藏入口的题跑了 76 轮、手工猜了 40 个路径全是 404，
    #   模型的复盘第一句是「沙箱无外网…只能靠 http_request 逐个 URL 请求」。
    #   那是配置问题，不是能力问题；拿这种数据当基线，会把"装错了"测成"不会"。
    #
    #   `allow_network` 保留为**额外**强制放开（比如将来某类题需要出公网）。
    raw_network = allow_network or solver.needs_raw_network(exercise.category)

    attachments = download_attachments(exercise)

    spec = solver.SolveSpec(
        challenge_id=f"arena-{exercise.exercise_id}",
        title=exercise.name,
        category=exercise.category,
        description=exercise.description,
        profile=profile,
        access_urls=tuple(
            f"{e.connect_host()}:{e.connect_port(p)}" for e in exercise.endpoints for p in e.ports
        ),
        attachments=attachments,
        llm_model=model,
        llm_api_key=api_key,
        llm_api_base=base_url,
        llm_label=label,
        llm_kind=kind,
        mode="auto",
        max_turns=max_turns,
        max_wrong_submissions=3,
        network_isolated=not raw_network,
        # ★ 靶机类型。服务器说 `nc` 时，`runner._initial_prompt` 才会提示
        #   "用 nc 直连，别只试 http_request"；不给的话 TCP 靶机在模型眼里
        #   跟 HTTP 靶机没区别，而 `#1003`/`#1006` 现在是真靶机了。
        access_type=exercise.access_type,
        cpus=None,
        memory_mb=None,
        # ★ 方法卡（已脱敏）。空串 = 这一臂不注入，与改动前完全一致。
        #
        #   放在这里而不是在 `solve_in_sandbox` 里查库：那条路径按设计
        #   不碰 DB（它要能在没有数据库的机器上跑，见提交 55016f4）。
        notes_text=notes_text,
    )

    image = solver.image_for_profile(profile)
    sandbox = solver.SandboxContainer(
        name=f"arena-e2e-{exercise.exercise_id}",
        image=image,
        network_isolated=not raw_network,
        extra_env={"PWNLIB_NOTERM": "1"},
        # ★★ 附件必须**同时挂进容器**，不能只下载到宿主。
        #
        #   `download_attachments()` 只负责把文件落到宿主目录；容器里能看见
        #   它们，靠的是 `docker run -v <dir>:/work/attachments:ro`，而这个挂载
        #   由 `SandboxContainer(attachments_dir=...)` 决定（`runner.py:1084`，
        #   且要求目录 `is_dir()` 才挂）。正式 worker 是这么传的
        #   （`runner.py:5092`），这里漏了整整一次基线。
        #
        #   漏传的现场（2026-10-22 transcript-1001，54470 字节）：
        #     [turn 1] 模型第一步就 `ls -la /work/attachments` + `unzip` →
        #              `No such file or directory`
        #     [turn 2] `find / -name 'source.zip'` → 空
        #     [turn 3] `ls -la /opt/host-resources` → 空目录
        #     之后 20 轮全在找一个不存在的文件，候选 0。
        #
        #   那不是"模型不会解"，是"题目说给附件、容器里没有"。拿这种数据当
        #   基线，等于用一次配置事故去否定模型能力。
        #
        #   目录取 `attachments[0].parent` 而不是自己拼 `ATTACH_DIR / id`：
        #   下载逻辑哪天换了布局（比如按名字分桶），这里不用跟着改。
        #   没有附件时传 None —— 别挂一个空目录，那会让模型以为题目有附件。
        attachments_dir=attachments[0].parent if attachments else None,
    )
    gateway = solver.LlmGateway(model=model, api_key=api_key, base_url=base_url, label=label, kind=kind)

    log(
        f"④ 启动沙箱（镜像 {image}，"
        f"网络 {'放开' if raw_network else '隔离'}"
        f"{' [该分类默认放开]' if not allow_network and raw_network else ''}）"
    )
    sandbox.start()
    try:
        outcome = await solver.solve_in_sandbox(
            spec,
            sandbox=sandbox,
            gateway=gateway,
            system_prompt=PROMPT.read_text(encoding="utf-8"),
            transcript=transcript,
        )
    finally:
        sandbox.destroy()

    log(f"   解题结束：回合 {outcome.turns}，候选 {len(outcome.candidates)} 个")
    for candidate in outcome.candidates:
        log(f"   · {candidate.flag}  ← {candidate.evidence[:80]}")
    if outcome.error:
        log(f"   错误：{outcome.error}")
    return outcome


async def submit_all(client: ArenaClient, exercise_id: int, outcome) -> bool:
    """把候选逐个提交，直到有一个对。"""
    if not outcome.candidates:
        log("⑤ 没有候选 flag，跳过提交")
        return False
    log("⑤ 提交候选：")
    for candidate in outcome.candidates:
        result = await client.submit(exercise_id, candidate.flag)
        if result.is_correct:
            log(f"   ★ {candidate.flag} → 正确！")
            return True
        if result.already_submitted:
            # ★ A0409「重复提交」不等于答错。
            #
            # 它只说明这个 flag 在本轮里交过 —— 而基线**被重复跑**时必然
            # 出现：上一次已经把题解了，这一次再交同一个答案就被挡住。
            # 直接判 ✗ 会让基线数字凭空掉一题：#1001 就是这么被误判成
            # "未通过"的，它的 FINAL 答案其实完全正确，日志里那行
            # `flag{welcome_to_the_arena} → 重复提交` 就是全部证据。
            #
            # 所以要回查题目的已解状态：平台说这题已经解了，那这次交的
            # 就是正确答案，只是"交过"了而已。没解 → 说明先前交的是错
            # 答案，这个候选仍然不算数。
            try:
                refreshed = await client.exercise(exercise_id)
            except ArenaError:
                refreshed = None
            if refreshed is not None and refreshed.has_solved:
                log(f"   ★ {candidate.flag} → 重复提交，但该题已解 → 算通过")
                return True
            log(f"   · {candidate.flag} → 重复提交（该题仍未解，先前交的是错答案）")
            continue
        reason = (
            "答案错误" if result.wrong_answer
            else f"code={result.code} {result.message}"
        )
        log(f"   · {candidate.flag} → {reason}")
    return False


async def main() -> int:
    parser = argparse.ArgumentParser(description="比赛平台 Agent API 端到端演练")
    parser.add_argument("--base-url", default="http://127.0.0.1:8901")
    parser.add_argument("--key", default="ak_test_local", help="X-Agent-AccessKey")
    parser.add_argument("--exercise", type=int, default=None, help="指定题目 id（默认挑第一道未解的）")
    parser.add_argument("--provider", default=None, help="模型名里含这个子串就用它")
    parser.add_argument("--max-turns", type=int, default=20)
    parser.add_argument("--allow-network", action="store_true", help="放开沙箱出网（连真实靶机时需要）")
    parser.add_argument("--no-env", action="store_true", help="跳过起环境（只测附件解题）")
    args = parser.parse_args()

    log(f"比赛平台：{args.base_url}")
    started = time.monotonic()
    async with ArenaClient(base_url=args.base_url, access_key=args.key) as client:
        info = await client.match_info()
        log(f"   规则：{str(info.get('rule'))[:60]}")

        exercise = await pick_exercise(client, args.exercise)
        exercise.raw["_base_url"] = args.base_url
        exercise = await ensure_environment(client, exercise.exercise_id, no_env=args.no_env)
        exercise.raw["_base_url"] = args.base_url

        outcome = await solve(
            exercise, provider=args.provider, allow_network=args.allow_network, max_turns=args.max_turns
        )
        ok = await submit_all(client, exercise.exercise_id, outcome)

        if not args.no_env:
            try:
                await client.recover_environment(exercise.exercise_id)
                log("⑥ 已回收环境")
            except ArenaError as exc:
                log(f"⑥ 回收环境失败：{exc}")

    log(f"\n{'=' * 56}")
    log(f"结果：{'★ 端到端成功（拉到题 → 解出 → 提交正确）' if ok else '未拿到正确的 flag'}")
    log(f"总用时 {time.monotonic() - started:.1f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
