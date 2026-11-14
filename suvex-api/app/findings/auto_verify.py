"""新漏洞自动验证：把「验证」从手动按钮补成扫描收尾的一部分。

只对**新建**、且带 PoC 或证据的漏洞触发；每轮最多 ``limit`` 条。
真正的执行仍走 ``runner_service.run_task``，角色固定为 ``verifier``，
工具固定为 ``VERIFIER_TOOLS`` —— 与手动「发起验证」同一条链路。
"""

from __future__ import annotations

import asyncio
import logging
import threading

from sqlalchemy.orm import Session

from app.agents import graph_service, runner_service
from app.auth.deps import Identity
from app.findings.models import Finding
from app.scans.models import Scan

log = logging.getLogger("suvex.findings.auto_verify")

#: 同时在跑的自动验证线程上限。验证会调模型、跑命令，不是零成本；
#: 串行排队比一次开几十个线程更可控。
_SEM = threading.BoundedSemaphore(2)


def schedule(
    db: Session,
    scan: Scan,
    finding_ids: tuple[str, ...],
    *,
    limit: int = 3,
) -> list[str]:
    """为新建漏洞登记验证任务并异步启动；返回实际排队的任务 id。"""
    created: list[str] = []
    for finding_id in finding_ids[: max(0, limit)]:
        finding = db.get(Finding, finding_id)
        if finding is None:
            continue
        # 没有 PoC、也没有任何证据可重放时，验证只能得到 inconclusive。
        # 不拿模型预算做这种空转。
        if not (finding.poc_code or finding.detail_evidence):
            continue
        owner = scan.authorized_by or f"scan:{scan.id}"
        task = graph_service.create_task(
            db,
            created_by=owner,
            title=f"[验证] {finding.title}"[:200],
            objective=f"独立验证漏洞 {finding.id} 是否真实成立，并写回证据。",
            scope=finding.target,
        )
        graph_service.add_goal(
            db,
            task=task,
            text=f"给出 {finding.id} 的独立验证结论并写回",
        )
        graph_service.add_intent(
            db,
            task=task,
            summary=f"验证漏洞 {finding.id}（{finding.target}）",
            priority=9,
        )
        created.append(task.id)

        identity = Identity(
            kind="session",
            label=f"auto-verify:{scan.id}",
            scopes=frozenset({"read", "scan"}),
            user_id=scan.authorized_by,
            username=None,
            role=None,
        )
        threading.Thread(
            target=_run,
            args=(task.id, identity),
            name=f"auto-verify-{finding.id[-8:]}",
            daemon=True,
        ).start()
    if created:
        log.info("扫描 %s 自动登记 %d 条验证任务", scan.id, len(created))
    return created


def _run(task_id: str, identity: Identity) -> None:
    with _SEM:
        try:
            asyncio.run(
                runner_service.run_task(
                    task_id=task_id,
                    identity=identity,
                    worker_persona="verifier",
                    worker_tools=runner_service.VERIFIER_TOOLS,
                )
            )
        except Exception:  # noqa: BLE001 - 后台任务失败不能影响扫描
            log.warning("自动验证任务 %s 执行失败", task_id, exc_info=True)


__all__ = ["schedule"]
