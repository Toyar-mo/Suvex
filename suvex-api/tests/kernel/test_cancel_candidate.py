"""取消要连候选一起作废（2026-11-08 真库事故的收尾口径）。

## 为什么单独一条

取消的 docstring 说它是「放弃这一轮」—— 而候选就是这一轮的产出。
不清候选的话，会留下「未开始 + 候选列里一个看起来像答案的字符串 +
已取消。」这种行：状态与内容互相矛盾，用户不知道该信哪一个。

⚠️ 另一条路径**刻意**保留候选（提交时平台回"本队伍已解出"），
   那条不走 cancel —— 两者不冲突。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.vocab import KernelChallengeStatus
from app.kernel.models import KernelChallenge

API = "/api/v1"


def test_cancel_clears_the_candidate(
    ready_client: TestClient, db_session: Session, ctf2_challenge: KernelChallenge
) -> None:
    ctf2_challenge.status = KernelChallengeStatus.SOLVING.value
    ctf2_challenge.candidate_flag = "flag{stale}"
    db_session.commit()

    resp = ready_client.post(f"{API}/kernel/challenges/{ctf2_challenge.id}/cancel")
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    row = db_session.get(KernelChallenge, ctf2_challenge.id)
    assert row.status == KernelChallengeStatus.UNSOLVED.value
    assert row.candidate_flag is None, "取消之后不该还挂着一个候选"
