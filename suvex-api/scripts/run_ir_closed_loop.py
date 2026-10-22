"""IR 闭环实测驱动：在真实 `ctf2-sandbox-ir` 容器里跑一次复盘。

## 它是什么，以及**不是**什么

这是一次**真跑**：真的 Docker 容器、真的 LLM、真的工具调用、真的题解与
工单文件。唯一的省略是**平台起环境那一步** —— IR 题是纯附件题，没有靶机，
而 `start_environment_for()` 要打真实 CTF2 平台（`ctf2.dasctf.com`）。
本次驱动的环境不对那个平台发请求，所以这里直接从 `build_spec` 之后接手，
把 `_run_worker` 里除了「起环境」之外的步骤按原样重放。

台词说清楚：省略的是**外部平台对接**，不是**智能体闭环**。

## 为什么不用 HTTP 接口 `/solve`

那样会走 worker，而 worker 第一步就是 `start_environment_for()`。
纯附件题在真实平台上会返回 404 并被当作"没有靶机"，但此刻我们调不通
那个平台（没有可用 PAT），调用会抛 `Ctf2Unavailable`，整个解题在进入
沙箱之前就失败了。所以这里驱动到 `solve_in_sandbox` 为止，其余（写库、
生成题解、落工单）仍走 `service.save_writeup` 这个生产函数。

用法：

    python scripts/run_ir_closed_loop.py --evidence /tmp/ir_case.tar.gz

产出：
  · 题解 markdown（含「六、处置建议」索引节）
  · `_ctf2_workorders/<challengeId>.md` 工单文件
  · 活动流事件行（每个 tool_call / reasoning 一条）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def _ensure_rows(db, *, evidence: Path):  # noqa: ANN001
    """建一份练习集 + 一道 IR 题，附件指到本地证据包。"""
    from app.core.ids import new_id
    from app.core.vocab import Ctf2ChallengeStatus
    from app.kernel.models import Ctf2Challenge, Ctf2Competition
    from app.kernel.service import get_account

    comp = (
        db.query(Ctf2Competition)
        .filter(Ctf2Competition.name == "IR-Lab")
        .one_or_none()
    )
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
        .filter(Ctf2Challenge.title == IR_TITLE)
        .one_or_none()
    )
    if chal is None:
        chal = Ctf2Challenge(
            id=new_id("ctf2chal"),
            competition_id=comp.id,
            remote_id="ir-case-001",
            title=IR_TITLE,
            category=IR_CATEGORY,
            description=IR_DESCRIPTION,
            attachments=[{"name": evidence.name, "url": "http://ctf2.dasctf.com/ir/case-001"}],
            status=Ctf2ChallengeStatus.UNSOLVED.value,
        )
        # ★ 落库时就要定下镜像 —— 这是 `service._profile_for` 在真实同步
        # 路径里做的事（唯一分类→镜像规则）。手工建行不会经过它，
        # 不补这一句的话会用列默认值 `ctf2-sandbox`，而症状是
        # 「IR 任务跑在 base 镜像里」：不报错、提示词也对，只是少了
        # sleuthkit 那批工具 —— 那正是 `_profile_for` 存在的理由。
        from app.kernel import runner as _runner

        chal.sandbox_profile = _runner.profile_for_category(chal.category)
        db.add(chal)
        db.commit()
        db.refresh(chal)

    # ★ 已存在的行也要对账 —— 第一版只在「新建」时设镜像，于是重跑时
    #   沿用上一轮留下的 `ctf2-sandbox`：镜像换了、代码修了，而
    #   这一道题仍然跑在 base 里，输出看起来完全正常。
    #   这种事只能靠"每次跑都把该对齐的对齐"来防。
    from app.kernel import runner as _runner

    want_profile = _runner.profile_for_category(chal.category)
    dirty = False
    if chal.sandbox_profile != want_profile:
        print(f"[case] profile 对账：{chal.sandbox_profile} -> {want_profile}")
        chal.sandbox_profile = want_profile
        dirty = True
    # 重跑要能重新解一遍：上一轮可能已经把状态改成了 solving/solved
    if chal.status != Ctf2ChallengeStatus.UNSOLVED.value:
        print(f"[case] 状态复位：{chal.status} -> unsolved")
        chal.status = Ctf2ChallengeStatus.UNSOLVED.value
        dirty = True
    if dirty:
        db.commit()
        db.refresh(chal)

    # 附件按"已下载"的状态放好（下载只在同步时发生，解题只看落盘结果）
    from app.kernel.attachments import attachment_root

    dest = attachment_root(chal.id)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / evidence.name
    target.write_bytes(evidence.read_bytes())
    for stale in dest.glob("*.sha256"):
        stale.unlink()
    return comp, chal, target


def _enable_ir(db) -> None:  # noqa: ANN001
    from app.kernel.service import effective_ctf2_settings
    from app.settings.models import Ctf2Config

    row = db.get(Ctf2Config, "default")
    if row is None:
        row = Ctf2Config(id="default")
        db.add(row)
    row.enable_ir_scenarios = True
    # safe 模式：本次不向任何平台提交，但保持与生产一致的默认值
    row.submit_mode = "safe"
    db.commit()
    print(f"[cfg] enable_ir_scenarios={effective_ctf2_settings(db).enable_ir_scenarios}")


IR_TITLE = "应急响应复盘：web-01 入侵事件（2026-10-19）"
IR_CATEGORY = "ir-root-cause"
IR_DESCRIPTION = (
    "web-01（10.0.3.11）在 2026-10-19 凌晨疑似被入侵。现有一份从该主机采集的"
    "证据包（附件 `ir_case.tar.gz`，解压后是日志与配置的取证副本）。\n\n"
    "请做一次根因复盘：\n"
    "1. 攻击者是怎么进来的（入口点、时间、依据哪一条日志）；\n"
    "2. 进来之后做了什么（持久化、提权、横向、数据处置），每一环都要能指到证据；\n"
    "3. 哪些环节**因为证据不足**只能给出待跟进的结论 —— 不要把'查了没找到'写成'排除'；\n"
    "4. 给出处置建议工单。\n\n"
    "注意：所有时间都要换算到 UTC 并写明换算规则；涉及凭据的内容要脱敏。"
)


async def _main(args: argparse.Namespace) -> int:
    from app.core.db import get_session_factory
    from app.kernel import runner
    from app.kernel import service as ctf2_service
    from app.kernel import writeup as writeup_mod

    evidence = Path(args.evidence).resolve()
    if not evidence.is_file():
        print(f"证据包不存在：{evidence}")
        return 2

    db = get_session_factory()()
    try:
        _comp, chal, attached = _ensure_rows(db, evidence=evidence)
        _enable_ir(db)
        challenge_id = chal.id
        print(f"[case] challenge={challenge_id} category={chal.category} profile={chal.sandbox_profile}")
        print(f"[case] attachment={attached} ({attached.stat().st_size} bytes)")

        spec = runner.build_spec(db, chal, mode="safe")
        print(f"[spec] profile={spec.profile} image={runner.image_for_profile(spec.profile)}")
        print(f"[spec] ir={spec.ir} agentsec={spec.agentsec} network_isolated={spec.network_isolated}")
        print(f"[spec] model={spec.llm_model} max_turns={spec.max_turns}")
        print(f"[spec] history_count={spec.history_count}")
        prompt = runner.build_system_prompt_by_spec(spec)
        base = runner.load_system_prompt()
        print(f"[prompt] len={len(prompt)} base={len(base)} ir_block={'ir_block' and 'yes' if len(prompt) > len(base) else 'NO'}")

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

        gateway = runner.LlmGateway(
            model=spec.llm_model,
            api_key=spec.llm_api_key,
            base_url=spec.llm_api_base,
            label=spec.llm_label,
            kind=spec.llm_kind,
        )

        started_at = time.time()
        lines: list[str] = []

        def on_progress(kind: str, summary: str, payload: dict[str, str]) -> None:  # noqa: ANN001
            lines.append(f"[{kind}] {summary}")
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
        print(f"[run] elapsed={elapsed}s turns={getattr(outcome, 'turns', '?')}")
        print(f"[run] ruled_out={len(outcome.ruled_out)} tried={len(outcome.tried)}")
        print(f"[run] workorders_raw_type={type(outcome.workorders).__name__}")
        if isinstance(outcome.workorders, list):
            print(f"[run] workorders_raw_count={len(outcome.workorders)}")

        solved = False
        markdown = writeup_mod.build_writeup(
            challenge=chal, outcome=outcome, solved=solved, elapsed_sec=elapsed
        )
        ctf2_service.save_writeup(db, challenge=chal, outcome=outcome, attempt_id=None, elapsed_sec=elapsed)
        print(f"[writeup] markdown_len={len(markdown)}")

        from app.kernel import workorder as wo

        path = wo.order_path(challenge_id)
        print(f"[workorder] path={path} exists={path.is_file()}")
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            print(f"[workorder] bytes={len(text)} count={wo.count_orders(text)}")
            print("=" * 70)
            print(text)
            print("=" * 70)
        print("=" * 70)
        print(markdown[:6000])
        return 0
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True)
    return asyncio.run(_main(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
