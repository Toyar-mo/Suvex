"""CTF2 的**审计留痕**（v0.20）。

## 为什么 CTF2 的动作必须留痕

这一组动作有两个别处没有的性质：

1. **它们代表用户去操作一个外部平台**（绑定 PAT、起环境、提交 flag）。
   事后要能回答"谁在什么时候用我们系统里的哪把钥匙做了什么"。
2. **其中一步不可逆**：`safe → auto` 之后，机器可以在**无人确认**的
   情况下对外提交 flag，而 CTF2 平台的错误提交次数是**不可再生**的。
   改它的那一刻必须留在记录里 —— 审计要回答的不是"改过配置"，
   而是"从什么时候起机器开始能自己交了"。

## 打真实 HTTP，再从库里读那一行

与 `tests/test_audit.py` 同一条纪律：直接调 `audit.record()` 只能证明
"写函数能用"，证明不了"那个端点真的调了它"。而"端点忘埋点"正是
这类需求最常见的失败方式（编译期不报错、测试不红、界面上看不出来）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.models import AuditLog


def _rows(db: Session, action: str) -> list[AuditLog]:
    return list(
        db.execute(select(AuditLog).where(AuditLog.action == action)).scalars()
    )


def _settings_body(mode: str, source: dict) -> dict:
    """按 `Ctf2SettingsIn` 的契约拼一个**完整**的请求体。

    ⚠️ 字段全部必填（见那个 schema 的说明：漏传一项是调用方 bug，
    给默认值会让它静默改回默认）。所以这里把另外几项原样带回去 ——
    这个 helper 的价值就在于"只改想改的那一项"，
    而它的正确性依赖于**把 source 里其余项都透传**。
    """
    return {
        "submitMode": mode,
        "maxWrongSubmissions": source["maxWrongSubmissions"],
        "defaultSandboxProfile": source["defaultSandboxProfile"],
        # v0.22 新增。漏了它 PUT 会 422，而报错指向的是"字段缺失"，
        # 与这条测试真正关心的"提交策略有没有留痕"毫无关系。
        "maxConcurrentSolves": source["maxConcurrentSolves"],
        # v0.24 新增的四项，同一条理由：`Ctf2SettingsIn` 的字段**全部
        # 必填**（漏传一项是调用方 bug，给默认值会让它静默改回默认），
        # 所以每加一项，这个 helper 都要跟着透传一次。
        "solvePollSeconds": source["solvePollSeconds"],
        "autoSyncSeconds": source["autoSyncSeconds"],
        "autoPriorityNew": source["autoPriorityNew"],
        "autoStartNew": source["autoStartNew"],
        # v0.26 新增。同一条理由 —— 这个 helper 的存在的意义就是
        # "只改想改的那一项"，而它依赖于把 source 里**其余全部**透传。
        "maxTurns": source["maxTurns"],
        # v0.35 新增（换模 fallback）。它**可选**（有默认值 None），
        # 但既然 source 里有，就一并透传，保持"只改想改的那一项"。
        "fallbackProviderId": source.get("fallbackProviderId"),
    }


def test_changing_the_submit_policy_is_audited(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ `safe ↔ auto` 的切换必须留痕，**而且要记下改成了什么**。

    只写一句"更新了 CTF2 配置"是没有意义的：事后真正要回答的是
    "从什么时候起机器可以自己提交了"。所以 detail 里必须有那个值。
    """
    before = ready_client.get("/api/v1/ctf2/settings")
    assert before.status_code == 200, before.text
    current = before.json()
    flipped = "auto" if current["submitMode"] == "safe" else "safe"

    changed = ready_client.put("/api/v1/ctf2/settings", json=_settings_body(flipped, current))
    assert changed.status_code == 200, changed.text

    rows = _rows(db_session, "ctf2.settings_update")
    assert len(rows) == 1, f"期望 1 条审计，实际 {len(rows)} 条"

    row = rows[0]
    assert row.detail is not None
    assert row.detail["submitMode"] == flipped, "审计里没记下改成了什么"
    assert flipped in row.summary
    # 「谁」必须能回答 —— 否则这条记录只能证明"有人改过"
    assert row.actor_label and row.actor_kind == "session"
    assert row.at is not None


def test_cancelling_a_run_is_audited(
    ready_client: TestClient, db_session: Session, ctf2_challenge
) -> None:
    """取消会**丢掉已经产出的候选**并中止一次有代价的运行。

    它的审计价值在于"这道题怎么突然没结果了" —— 那是用户会来问的问题，
    而答案（他/别人点了取消）只有审计里才有。
    """
    response = ready_client.post(f"/api/v1/ctf2/challenges/{ctf2_challenge.id}/cancel")
    assert response.status_code == 200, response.text

    rows = _rows(db_session, "ctf2.solve_cancel")
    assert len(rows) == 1
    assert rows[0].target == ctf2_challenge.id
    assert rows[0].detail is not None
    assert rows[0].detail["toStatus"] == "unsolved"


def test_releasing_an_environment_is_not_audited(
    ready_client: TestClient, db_session: Session, ctf2_challenge
) -> None:
    """★ 释放环境**刻意不记**（`AuditAction` 里没有这个动作）。

    它幂等、可重建、没有不可逆后果 —— 按 `AuditAction` 的规矩
    （只记有后果的操作）它不该进审计表。记它只会让表里多出一堆
    "我又点了一次释放"的噪音，而噪音会让真正的记录难找。

    这条用例是**反向**的：它保证将来有人"顺手补一个埋点"时，
    会先看到这里已经说过为什么不做。
    """
    response = ready_client.delete(f"/api/v1/ctf2/challenges/{ctf2_challenge.id}/environment")
    assert response.status_code == 204, response.text

    leftovers = list(
        db_session.execute(
            select(AuditLog).where(AuditLog.action.like("ctf2.%"))
        ).scalars()
    )
    assert leftovers == [], f"释放环境不该产生审计记录，实际有：{[r.action for r in leftovers]}"


def test_audit_never_contains_the_token(
    ready_client: TestClient, db_session: Session, ctf2_challenge
) -> None:
    """★ 审计里不许出现 PAT。

    这里**主动**制造一个"很像会泄露"的场景：账号已经绑好了（库里有
    真实密文），此时改配置、取消解题 —— 两次操作都可能顺手把账号
    信息带进 summary/detail。断言的是整张表里没有任何一条记录含有
    凭据形状的字符串。

    ⚠️ 这条不能替代 `record()` 里那道 `redact()`，它是**第二处表达**：
    那一处挡的是"任何调用方写进去的字符串"，这一条挡的是"CTF2 的
    这几个端点写进去的东西"。
    """
    from tests.ctf2.conftest import TEST_PAT

    current = ready_client.get("/api/v1/ctf2/settings").json()
    ready_client.put(
        "/api/v1/ctf2/settings",
        json=_settings_body("safe" if current["submitMode"] == "auto" else "auto", current),
    )
    ready_client.post(f"/api/v1/ctf2/challenges/{ctf2_challenge.id}/cancel")

    blob = " ".join(
        f"{row.summary} {row.detail} {row.target or ''}"
        for row in db_session.execute(select(AuditLog)).scalars()
    )
    assert TEST_PAT not in blob
    assert "pat_" not in blob
