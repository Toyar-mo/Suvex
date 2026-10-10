"""比赛平台（AI Agent API）的端到端演练：拉题 → 起环境 → 沙箱解题 → 提交。

## 它验证的正是比赛要用的那条链路

| 步骤 | 用到的接口 |
| --- | --- |
| 拉题目列表 | `GET /ctf/exercise-list` |
| 取题目详情（附件 + 靶机） | `GET /ctf/exercise?exerciseId=` |
| 起环境 | `POST /ctf/build-exercise-env` + 轮询详情到 `isNeedCheck=false` |
| **解题** | 本地沙箱 + LLM（复用 `app/ctf2/runner.py`，与平台无关） |
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
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402

from app.arena.client import ArenaClient, ArenaError, Exercise  # noqa: E402
from app.core.db import session_scope  # noqa: E402
from app.ctf2 import runner as solver  # noqa: E402

PROMPT = PROJECT_ROOT / "prompts" / "ctf2_system.md"
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


def load_provider(preferred: str | None) -> tuple[str, str, str, str, str]:
    """从 providers 表取一个可用的 LLM（返回 model/key/base/label/kind）。

    ⚠️ 明文 key 只在这一层出现一次，用完即随 SolveSpec 交给 LLM（与 ctf2 那边同一条纪律）。
    """
    from sqlalchemy import select

    from app.providers.models import ProviderAccount
    from app.providers.service import decrypt_provider_key

    with session_scope() as db:
        stmt = select(ProviderAccount).order_by(
            ProviderAccount.is_default.desc(), ProviderAccount.created_at
        )
        rows = list(db.execute(stmt).scalars())
        if not rows:
            raise SystemExit("库里没有配置任何 LLM provider")
        row = next((r for r in rows if preferred and preferred in (r.label or "")), None) or rows[0]
        key = decrypt_provider_key(row)
        return row.model, key, row.base_url or "", row.label or "", row.kind or "cloud"


async def solve(exercise: Exercise, *, provider: str | None, allow_network: bool, max_turns: int):
    """在沙箱里让模型解这道题（复用与平台无关的那套引擎）。"""
    model, api_key, base_url, label, kind = load_provider(provider)
    log(f"④ 用模型 {label}（{model}）")

    profile = solver.profile_for_category(exercise.category)
    spec = solver.SolveSpec(
        challenge_id=f"arena-{exercise.exercise_id}",
        title=exercise.name,
        category=exercise.category,
        description=exercise.description,
        profile=profile,
        access_urls=tuple(
            f"{e.connect_host()}:{e.connect_port(p)}" for e in exercise.endpoints for p in e.ports
        ),
        attachments=download_attachments(exercise),
        llm_model=model,
        llm_api_key=api_key,
        llm_api_base=base_url,
        llm_label=label,
        llm_kind=kind,
        mode="auto",
        max_turns=max_turns,
        max_wrong_submissions=3,
        network_isolated=not allow_network,
        cpus=None,
        memory_mb=None,
    )

    image = solver.image_for_profile(profile)
    sandbox = solver.SandboxContainer(
        name=f"arena-e2e-{exercise.exercise_id}",
        image=image,
        network_isolated=not allow_network,
        extra_env={"PWNLIB_NOTERM": "1"},
    )
    gateway = solver.LlmGateway(model=model, api_key=api_key, base_url=base_url, label=label, kind=kind)

    log(f"④ 启动沙箱（镜像 {image}，网络 {'放开' if allow_network else '隔离'}）")
    sandbox.start()
    try:
        outcome = await solver.solve_in_sandbox(
            spec,
            sandbox=sandbox,
            gateway=gateway,
            system_prompt=PROMPT.read_text(encoding="utf-8"),
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
        reason = (
            "答案错误" if result.wrong_answer
            else "重复提交" if result.already_submitted
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
