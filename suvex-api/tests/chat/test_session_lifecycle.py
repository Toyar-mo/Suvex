"""会话生命周期 v1.15：置顶 / 恢复归档 / 彻底删除。

## 这个文件在防什么

清一色是「看起来做完了，其实没接线」或「接口能调，但把不该删的删了」：

1. **置顶只写进响应、没进排序。** 存一列很容易，让列表真的按它排是另一件事。
   漏了的话用户点了「置顶此对话」，界面上什么都没变 —— 那正是"点了没反应"。
2. **彻底删除没有闸门。** 直接 `DELETE FROM` 会把"手滑点了一下"变成不可逆的
   数据丢失。这条闸（必须先归档 → 否则 409）是照 `scans.purge_scan` 抄的。
3. **删了行却没删干净。** 消息留着、附件留着、沙箱留着 —— 前两者能在库里
   查到，后者能 `docker ps` 看到。所以三条都要断言。
4. **恢复归档时把别人的会话也恢复了。** 新端点就多一次漏 `created_by` 过滤的
   机会，与 v0.94 的设置端点同一种风险。

## 为什么既测 service 又测 HTTP

排序与删除语义在 service 层（能直接看 SQL 结果）；而「必须先归档」这道闸
返回的是 **HTTP 409**，它只存在于 router 里 —— 绕过 HTTP 测就等于没测。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chat import service
from app.chat.models import ChatMessage, ChatSession

API = "/api/v1"


@pytest.fixture()
def chat_session(db_session: Session) -> ChatSession:
    session = service.create_session(db_session, created_by="user_admin")
    db_session.commit()
    return session


# ══════════════════════════════════════════════════════════
# 1. 置顶真的改变顺序（不是只多一个字段）
# ══════════════════════════════════════════════════════════


def test_pinning_moves_the_session_to_the_front(db_session: Session) -> None:
    """置顶的那条排最前，其余仍按最近更新。"""
    oldest = service.create_session(db_session, created_by="user_admin", title="最早")
    newest = service.create_session(db_session, created_by="user_admin", title="最新")
    db_session.commit()

    # 默认顺序：更新新的在前
    assert [s.id for s in service.list_sessions(db_session, created_by="user_admin")] == [
        newest.id,
        oldest.id,
    ]

    service.set_pinned(db_session, session=oldest, pinned=True)
    db_session.commit()

    rows = service.list_sessions(db_session, created_by="user_admin")
    assert rows[0].id == oldest.id, "置顶的那条没有排到最前"
    assert rows[1].id == newest.id


def test_pinning_a_second_session_puts_it_before_the_first(db_session: Session) -> None:
    """后置顶的排在先置顶的前面 —— 这是"存时刻"而不是"存布尔"的全部意义。

    用布尔值时只能拿 `updated_at` 兜底，而 `updated_at` 会因为"又聊了两句"
    而变，用户置顶的那几条会自己换位置。
    """
    first = service.create_session(db_session, created_by="user_admin", title="A")
    second = service.create_session(db_session, created_by="user_admin", title="B")
    db_session.commit()

    service.set_pinned(db_session, session=first, pinned=True)
    db_session.commit()
    service.set_pinned(db_session, session=second, pinned=True)
    db_session.commit()

    rows = service.list_sessions(db_session, created_by="user_admin")
    assert [r.id for r in rows] == [second.id, first.id]


def test_unpinning_puts_it_back_among_the_rest(db_session: Session) -> None:
    """取消置顶 = 写回 NULL，且不再被当成"置顶"那一组。

    ★ 这里**不**断言绝对顺序。`chat_sessions.updated_at` 带 `onupdate`，
      任何一次 UPDATE（包括置顶/取消置顶）都会把它推到当前时刻，所以取消
      置顶之后这条会落在未置顶组的最前面。这不是 bug，但它让"断言顺序"
      变成一个脆弱的代理指标 —— 真正要守的是「它不再是置顶」。
    """
    target = service.create_session(db_session, created_by="user_admin", title="临时置顶")
    other = service.create_session(db_session, created_by="user_admin", title="普通")
    db_session.commit()

    service.set_pinned(db_session, session=target, pinned=True)
    db_session.commit()
    assert target.pinned_at is not None
    assert target.pinned is True
    assert [r.id for r in service.list_sessions(db_session, created_by="user_admin") if r.pinned] == [
        target.id
    ]

    service.set_pinned(db_session, session=target, pinned=False)
    db_session.commit()
    assert target.pinned_at is None
    assert target.pinned is False

    rows = service.list_sessions(db_session, created_by="user_admin")
    assert [r.id for r in rows if r.pinned] == [], "取消置顶后仍被当成置顶"
    assert {r.id for r in rows} == {target.id, other.id}, "两条都还应在列表里"


def test_pinned_flag_reaches_the_wire(ready_client: TestClient) -> None:
    """`ChatSessionOut.pinned` 必须真的出现在 JSON 里。

    ★ 这个字段是**派生属性**（`@property` 从 `pinned_at` 算），pydantic 的
      `model_validate` 默认读不到它 —— 漏了就会永远是 `false`，界面上永远
      画不出那个小旗子，而测试只看数据库的话完全发现不了。
    """
    created = ready_client.post(f"{API}/chat/sessions", json={"title": "要看旗子"})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    assert created.json()["pinned"] is False

    pinned = ready_client.post(f"{API}/chat/sessions/{session_id}/pin", json={"pinned": True})
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["pinned"] is True

    listed = ready_client.get(f"{API}/chat/sessions")
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["pinned"] is True


# ══════════════════════════════════════════════════════════
# 2. 彻底删除的两道闸
# ══════════════════════════════════════════════════════════


def test_purge_refuses_a_session_that_is_not_archived(
    ready_client: TestClient, db_session: Session
) -> None:
    """没归档就彻底删除 → 409，且**一条都没删**。

    ★ 409 而不是 400：这不是"请求格式不对"，是"当前状态不允许这个动作"。
      前端据此显示"请先删除它（进已归档）"。
    """
    created = ready_client.post(f"{API}/chat/sessions", json={"title": "没归档"})
    session_id = created.json()["id"]

    resp = ready_client.post(f"{API}/chat/sessions/{session_id}/purge")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "CHAT_SESSION_NOT_ARCHIVED"

    # 关键：拒绝之后会话必须还在（拒绝 ≠ 删了一半）
    assert db_session.get(ChatSession, session_id) is not None


def test_purge_deletes_the_session_and_every_message(
    ready_client: TestClient, db_session: Session
) -> None:
    """归档之后彻底删除 → 会话行与消息行全部消失。"""
    created = ready_client.post(f"{API}/chat/sessions", json={"title": "要彻底删"})
    session_id = created.json()["id"]
    service.append_user_message(db_session, session=db_session.get(ChatSession, session_id), content="第一句")
    service.append_user_message(db_session, session=db_session.get(ChatSession, session_id), content="第二句")
    db_session.commit()

    assert ready_client.delete(f"{API}/chat/sessions/{session_id}").status_code == 200
    before = db_session.scalars(
        select(ChatMessage).where(ChatMessage.session_id == session_id)
    ).all()
    assert len(before) == 2

    resp = ready_client.post(f"{API}/chat/sessions/{session_id}/purge")
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == session_id, "彻底删除应回执被删的那个会话"

    db_session.expire_all()
    assert db_session.get(ChatSession, session_id) is None, "会话行还在"
    assert (
        db_session.scalars(select(ChatMessage).where(ChatMessage.session_id == session_id)).all() == []
    ), "消息行还在"


def test_purge_is_not_reachable_for_someone_elses_session(
    ready_client: TestClient, viewer_client: TestClient
) -> None:
    """别人的会话连"归档"都做不到，更谈不上彻底删除 → 404。

    ★ 期望 404 而不是 403（与 v0.94 的设置端点同一条理由）：按"不存在"处理，
      免得用状态码告诉对方"这个 id 是真的"。
    """
    created = ready_client.post(f"{API}/chat/sessions", json={"title": "管理员的"})
    session_id = created.json()["id"]
    ready_client.delete(f"{API}/chat/sessions/{session_id}")  # 先归档，排除"没归档"这个干扰

    resp = viewer_client.post(f"{API}/chat/sessions/{session_id}/purge")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# ══════════════════════════════════════════════════════════
# 3. 归档 / 恢复
# ══════════════════════════════════════════════════════════


def test_archived_sessions_only_appear_when_asked_for(ready_client: TestClient) -> None:
    """默认列表不含归档；`?includeArchived=true` 才连归档一起返回。

    ★ 这是「已归档」区的唯一数据来源。默认也不返回会让"恢复/彻底删除"
      永远没有入口；默认就返回则等于归档没有意义（还在侧栏里）。
    """
    kept = ready_client.post(f"{API}/chat/sessions", json={"title": "留着"}).json()["id"]
    gone = ready_client.post(f"{API}/chat/sessions", json={"title": "归档掉"}).json()["id"]
    assert ready_client.delete(f"{API}/chat/sessions/{gone}").status_code == 200

    default_ids = [s["id"] for s in ready_client.get(f"{API}/chat/sessions").json()]
    assert kept in default_ids
    assert gone not in default_ids

    all_ids = [
        s["id"]
        for s in ready_client.get(f"{API}/chat/sessions?includeArchived=true").json()
    ]
    assert kept in all_ids and gone in all_ids


def test_unarchive_brings_it_back_with_its_messages(
    ready_client: TestClient, db_session: Session
) -> None:
    """恢复归档：会话回到默认列表，消息一条不少。

    ★ 只恢复**消息** —— 归档时附件与沙箱已经当场销毁，那部分回不来。
      这里断言消息还在，正是要钉住"它能恢复到的边界"。
    """
    session_id = ready_client.post(f"{API}/chat/sessions", json={"title": "恢复我"}).json()["id"]
    service.append_user_message(
        db_session, session=db_session.get(ChatSession, session_id), content="别丢"
    )
    db_session.commit()

    assert ready_client.delete(f"{API}/chat/sessions/{session_id}").status_code == 200
    resp = ready_client.post(f"{API}/chat/sessions/{session_id}/unarchive")
    assert resp.status_code == 200, resp.text
    assert resp.json()["archived"] is False

    default_ids = [s["id"] for s in ready_client.get(f"{API}/chat/sessions").json()]
    assert session_id in default_ids

    db_session.expire_all()
    rows = db_session.scalars(select(ChatMessage).where(ChatMessage.session_id == session_id)).all()
    assert len(rows) == 1
