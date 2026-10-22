"""IR 闭环多次实跑驱动：每个证据包变体一道独立的题，产出结构化 JSON。

与 `run_ir_closed_loop.py` 的关系：那个是**单次**驱动（标题与分类写死成
case-001）。这个版本把标题/分类/证据路径参数化，并且把**每一次**的实测
指标原样吐成 JSON —— 因为对比表要的是真值，不能靠人从 stdout 里抄。

省略的仍然只有「向 CTF2 平台起环境」那一步（纯附件题没有靶机，详见
`run_ir_closed_loop.py` 的模块 docstring）。其余步骤（写库建批次+题、
铺附件、起 `ctf2-sandbox-ir` 容器、`solve_in_sandbox`、`save_writeup`、
落工单）全部走生产代码。

用法：

    .venv/bin/python scripts/run_ir_variant_loop.py \\
        --evidence /root/ir_evidence/ir_case_002.tar.gz \\
        --title "应急响应复盘 002：db-02 数据外泄疑云" \\
        --slug case002 \\
        --out /root/ir_evidence/runs/case002.json

产出：
  · **结构化 JSON**（stdout 最后一行 `###JSON###{...}`，同时写 `--out`）
  · 题解 markdown 全文（落库）
  · `_ctf2_workorders/<challengeId>.md` 工单文件
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

IR_CATEGORY = "ir-root-cause"


def _description(evidence_name: str) -> str:
    """题目描述。**与 case-001 同一套判据要求**，只换证据包名字。"""
    return (
        f"web/app 侧主机在 2026-11 疑似被入侵。现有一份从该环境采集的证据包"
        f"（附件 `{evidence_name}`，解压后是日志与配置的取证副本）。\n\n"
        "请做一次根因复盘：\n"
        "1. 攻击者是怎么进来的（入口点、时间、依据哪一条日志）；\n"
        "2. 进来之后做了什么（持久化、提权、横向、数据处置），每一环都要能指到证据；\n"
        "3. 哪些环节**因为证据不足**只能给出待跟进的结论 —— 不要把'查了没找到'写成'排除'；\n"
        "4. 给出处置建议工单。\n\n"
        "注意：所有时间都要换算到 UTC 并写明换算规则；涉及凭据的内容要脱敏。"
    )


def _ensure_rows(db, *, evidence: Path, title: str):  # noqa: ANN001
    from app.core.ids import new_id
    from app.core.vocab import Ctf2ChallengeStatus
    from app.kernel.models import Ctf2Challenge, Ctf2Competition
    from app.kernel.service import get_account

    comp = db.query(Ctf2Competition).filter(Ctf2Competition.name == "IR-Lab").one_or_none()
    if comp is None:
        account = get_account(db)
        if account is None:
            raise SystemExit("这台机器上没有绑定的 CTF2 账号，无法建赛事行。")
        comp = Ctf2Competition(
            id=new_id("ctf2comp"),
            account_id=account.id,
            kind="practice",
            name="IR-Lab",
            remote_id="ir-lab",
        )
        db.add(comp)
        db.commit()
        db.refresh(comp)

    chal = (
        db.query(Ctf2Challenge)
        .filter(Ctf2Challenge.competition_id == comp.id)
        .filter(Ctf2Challenge.title == title)
        .one_or_none()
    )
    if chal is None:
        chal = Ctf2Challenge(
            id=new_id("ctf2chal"),
            competition_id=comp.id,
            remote_id=f"ir-case-{new_id('x')[-8:]}",
            title=title,
            category=IR_CATEGORY,
            description=_description(evidence.name),
            attachments=[{"name": evidence.name, "url": "http://ctf2.dasctf.com/ir/local"}],
            status=Ctf2ChallengeStatus.UNSOLVED.value,
        )
        from app.kernel import runner as _runner

        chal.sandbox_profile = _runner.profile_for_category(chal.category)
        db.add(chal)
        db.commit()
        db.refresh(chal)

    # 每跑一次都要对账（旧的 profile / 状态都可能是上一轮留下的）
    from app.kernel import runner as _runner

    want_profile = _runner.profile_for_category(chal.category)
    dirty = False
    if chal.sandbox_profile != want_profile:
        print(f"[case] profile 对账：{chal.sandbox_profile} -> {want_profile}")
        chal.sandbox_profile = want_profile
        dirty = True
    if chal.status != Ctf2ChallengeStatus.UNSOLVED.value:
        chal.status = Ctf2ChallengeStatus.UNSOLVED.value
        dirty = True
    # 描述与附件名按本次证据包重写（同一个标题可能在多次跑之间换了包）
    if chal.description != _description(evidence.name):
        chal.description = _description(evidence.name)
        dirty = True
    want_att = [{"name": evidence.name, "url": "http://ctf2.dasctf.com/ir/local"}]
    if chal.attachments != want_att:
        chal.attachments = want_att
        dirty = True
    if dirty:
        db.commit()
        db.refresh(chal)

    from app.kernel.attachments import attachment_root

    dest = attachment_root(chal.id)
    dest.mkdir(parents=True, exist_ok=True)
    # ★ 清掉旧附件：同一个 challenge 行换证据包时，旧 tar.gz 留着会让模型
    #   读到上一份包（而症状是"分析得很对，只是分析的是另一件事"）。
    for stale in dest.iterdir():
        if stale.is_file():
            stale.unlink()
    target = dest / evidence.name
    target.write_bytes(evidence.read_bytes())
    return comp, chal, target


def _enable_ir(db) -> None:  # noqa: ANN001
    from app.kernel.service import effective_ctf2_settings
    from app.settings.models import Ctf2Config

    row = db.get(Ctf2Config, "default")
    if row is None:
        row = Ctf2Config(id="default")
        db.add(row)
    row.enable_ir_scenarios = True
    row.submit_mode = "safe"          # 不向任何平台提交
    db.commit()
    print(f"[cfg] enable_ir_scenarios={effective_ctf2_settings(db).enable_ir_scenarios}")


def _usage_summary(outcome) -> dict:  # noqa: ANN001
    """把 `outcome.usages` 折成可对比的数。

    直接照 `Ctf2SolveUsages` 的口径：优先 `total_tokens`，它缺失（=0）
    而分项有值时用 `prompt+completion`（见 `runner.TokenUsage.total`）。
    """
    rows = list(getattr(outcome, "usages", None) or [])
    prompt = sum(u.prompt_tokens for _, _, _, u in rows)
    completion = sum(u.completion_tokens for _, _, _, u in rows)
    total = 0
    reported = 0
    models: dict[str, int] = {}
    for _turn, model, _label, u in rows:
        t = u.total_tokens or (u.prompt_tokens + u.completion_tokens)
        total += t
        if u.reported:
            reported += 1
        models[model] = models.get(model, 0) + t
    return {
        "llm_calls": len(rows),
        "llm_calls_reported": reported,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "per_model_tokens": models,
        "per_turn_tokens": [
            {
                "turn": turn,
                "model": model,
                "total": (u.total_tokens or (u.prompt_tokens + u.completion_tokens)),
                "prompt": u.prompt_tokens,
                "completion": u.completion_tokens,
            }
            for turn, model, _label, u in rows
        ],
    }


async def _main(args: argparse.Namespace) -> int:
    from app.core.db import get_session_factory
    from app.kernel import runner
    from app.kernel import service as ctf2_service
    from app.kernel import workorder as wo
    from app.kernel import writeup as writeup_mod

    evidence = Path(args.evidence).resolve()
    if not evidence.is_file():
        print(f"证据包不存在：{evidence}")
        return 2

    result: dict = {
        "slug": args.slug,
        "title": args.title,
        "evidence": str(evidence),
        "evidence_bytes": evidence.stat().st_size,
        "ok": False,
    }
    db = get_session_factory()()
    try:
        _comp, chal, attached = _ensure_rows(db, evidence=evidence, title=args.title)
        _enable_ir(db)
        challenge_id = chal.id
        result["challenge_id"] = challenge_id
        print(f"[case] challenge={challenge_id} profile={chal.sandbox_profile}")
        print(f"[case] attachment={attached} ({attached.stat().st_size} bytes)")

        spec = runner.build_spec(db, chal, mode="safe")
        print(f"[spec] profile={spec.profile} image={runner.image_for_profile(spec.profile)}")
        print(f"[spec] ir={spec.ir} network_isolated={spec.network_isolated}")
        print(f"[spec] model={spec.llm_model} max_turns={spec.max_turns}")
        result["spec"] = {
            "profile": spec.profile,
            "image": runner.image_for_profile(spec.profile),
            "ir": bool(spec.ir),
            "network_isolated": bool(spec.network_isolated),
            "model": spec.llm_model,
            "max_turns": spec.max_turns,
            "history_count": spec.history_count,
            "notes_count": getattr(spec, "notes_count", 0),
        }
        prompt = runner.build_system_prompt_by_spec(spec)
        base = runner.load_system_prompt()
        result["prompt_chars"] = len(prompt)
        result["prompt_base_chars"] = len(base)
        print(f"[prompt] len={len(prompt)} base={len(base)} ir_block={'yes' if len(prompt) > len(base) else 'NO'}")

        runner.ensure_docker_available()
        runner.ensure_ctf2_network()

        sandbox = runner.SandboxContainer(
            name=f"{runner.SANDBOX_NAME_PREFIX}{challenge_id[-12:]}-{os.urandom(4).hex()}",
            image=runner.image_for_profile(spec.profile),
            network_isolated=spec.network_isolated,
            cpus=spec.cpus,
            memory_mb=spec.memory_mb,
            host_resources=runner._host_resources_path(),
            attachments_dir=runner._attachment_dir(challenge_id),
            workspace_dir=runner._workspace_dir(challenge_id, create=True),
            extra_env={"PWNLIB_NOTERM": "1"},
        )
        sandbox.start()
        print(f"[sandbox] started name={sandbox.name}")
        result["sandbox"] = sandbox.name

        gateway = runner.LlmGateway(
            model=spec.llm_model,
            api_key=spec.llm_api_key,
            base_url=spec.llm_api_base,
            label=spec.llm_label,
            kind=spec.llm_kind,
        )

        started_at = time.time()
        progress: list[dict] = []

        def on_progress(kind: str, summary: str, payload: dict[str, str]) -> None:  # noqa: ANN001
            progress.append({"kind": kind, "summary": summary[:400]})
            print(f"  · {kind}: {summary[:150]}", flush=True)

        try:
            outcome = await runner.solve_in_sandbox(
                spec,
                sandbox=sandbox,
                gateway=gateway,
                system_prompt=prompt,
                on_progress=on_progress,
            )
        finally:
            try:
                sandbox.destroy()
            except Exception as exc:  # noqa: BLE001
                print(f"[sandbox] stop failed: {exc}")

        elapsed = int(time.time() - started_at)
        usage = _usage_summary(outcome)
        result.update(
            {
                "elapsed_sec": elapsed,
                "turns": outcome.turns,
                "llm_calls": outcome.llm_calls,
                "tool_calls": getattr(outcome, "tool_calls", None),
                "saw_tool_output": bool(getattr(outcome, "saw_tool_output", False)),
                "error": outcome.error,
                "ruled_out": list(outcome.ruled_out),
                "ruled_out_count": len(outcome.ruled_out),
                "tried": list(outcome.tried),
                "tried_count": len(outcome.tried),
                "summary": (getattr(outcome, "summary", "") or "")[:2000],
                "usage": usage,
                "progress_lines": len(progress),
            }
        )

        # ★ FINAL 里 workorders 的**原始值**（落库前先记下来：render 会丢 grade）
        raw_orders = None
        if isinstance(outcome.workorders, list):
            raw_orders = outcome.workorders
        elif isinstance(outcome.workorders, dict):
            raw_orders = outcome.workorders.get("workorders")
        result["workorders_raw"] = raw_orders

        solved = False
        markdown = writeup_mod.build_writeup(
            challenge=chal, outcome=outcome, solved=solved, elapsed_sec=elapsed
        )
        ctf2_service.save_writeup(
            db, challenge=chal, outcome=outcome, attempt_id=None, elapsed_sec=elapsed
        )
        result["writeup_chars"] = len(markdown)

        path = wo.order_path(challenge_id)
        result["workorder_path"] = str(path)
        result["workorder_exists"] = path.is_file()
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            result["workorder_chars"] = len(text)
            result["workorder_bytes"] = path.stat().st_size
            result["workorder_lines"] = len(text.splitlines())
            result["workorder_count"] = wo.count_orders(text)
            # 分级统计：直接数渲染出来的字面量（render 会写 `**置信级别**：`confirmed``）
            result["workorder_grades"] = {
                "confirmed": text.count("`confirmed`"),
                "needs_follow_up": text.count("`needs_follow_up`"),
            }
        result["ok"] = outcome.error is None
        print(f"[run] elapsed={elapsed}s turns={outcome.turns} llm_calls={outcome.llm_calls}")
        print(f"[run] tokens={usage['total_tokens']} ruled_out={len(outcome.ruled_out)} tried={len(outcome.tried)}")
        print(f"[run] workorders={result.get('workorder_count')} grades={result.get('workorder_grades')}")
        print(f"[writeup] chars={len(markdown)}")
        if path.is_file():
            print("=" * 70)
            print(path.read_text(encoding="utf-8"))
            print("=" * 70)
            print(markdown[:5000])
        return 0
    except Exception as exc:  # noqa: BLE001 - 失败要留结构化痕迹，不能只 print
        result["ok"] = False
        result["exception"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()[-4000:]
        print("[FAIL]", result["exception"])
        print(result["traceback"])
        return 1
    finally:
        db.close()
        blob = json.dumps(result, ensure_ascii=False, indent=2)
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(blob, encoding="utf-8")
            print(f"[out] wrote {out} ({len(blob)} chars)")
        print("###JSON###")
        print(blob)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--out", default="")
    return asyncio.run(_main(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
