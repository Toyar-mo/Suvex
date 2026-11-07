"""会话详情的分页与增量（2026-11 性能）。

## 这个文件在防什么

长会话的详情曾经**一次性返回整段历史**。实测有会话到过几 MB：
打开它要下载、解析、渲染全部消息，而用户一进来只看得到最后几屏。
现在 `GET /chat/sessions/{id}` 支持三种取法：最近 N 条、往上游标之前、
游标之后的新消息。

三条判据都容易写错，而且错了**不会报错**，只会让用户"翻不到更早的消息"
或"刷新之后消息变少了"：

1. `limit` 必须取**最近** N 条、返回**升序** —— 取成最早 N 条的话，
   界面上打开一个长会话显示的是几个月前的开头。
2. `hasEarlier` 由服务端按 `messages[0].seq > 1` 算，不能让前端用
   `len < messageCount` 猜 —— seq 有空洞时那个不等式会算错。
3. `after` 只追加新消息，**不能**把 `hasEarlier` 翻成 true：
   那不是"更早的一段"，翻错了顶部会冒出一个永远点不出东西的「加载更早」。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.chat import service
from app.chat.models import ChatSession

API = "/api/v1"


def _seed(db_session: Session, session_id: str, count: int) -> list[str]:
    """往会话里写 `count` 条用户消息，返回正文（seq 从 1 递增）。"""
    session = db_session.get(ChatSession, session_id)
    bodies: list[str] = []
    for index in range(count):
        body = f"第 {index + 1} 句"
        service.append_user_message(db_session, session=session, content=body)
        bodies.append(body)
    db_session.commit()
    return bodies


@pytest.fixture()
def seeded(ready_client: TestClient, db_session: Session) -> tuple[str, list[str]]:
    created = ready_client.post(f"{API}/chat/sessions", json={"title": "长会话"})
    session_id = created.json()["id"]
    return session_id, _seed(db_session, session_id, 5)


def test_default_returns_everything(seeded, ready_client: TestClient) -> None:
    """不给参数 = 旧语义：全量、升序、没有更早的一段。"""
    session_id, bodies = seeded

    resp = ready_client.get(f"{API}/chat/sessions/{session_id}")
    assert resp.status_code == 200, resp.text
    payload = resp.json()

    assert [m["content"] for m in payload["messages"]] == bodies
    assert payload["hasEarlier"] is False
    assert payload["messageCount"] == len(bodies), "messageCount 是总条数，不是这一页的条数"


def test_limit_returns_the_latest_page_in_ascending_order(seeded, ready_client: TestClient) -> None:
    """`limit` 取**最近** N 条，返回仍是升序 —— 否则打开长会话看到的是开头。"""
    session_id, bodies = seeded

    payload = ready_client.get(f"{API}/chat/sessions/{session_id}", params={"limit": 2}).json()

    assert [m["content"] for m in payload["messages"]] == bodies[-2:]
    assert [m["seq"] for m in payload["messages"]] == [4, 5], "返回顺序必须是升序"
    assert payload["hasEarlier"] is True


def test_before_cursor_walks_backwards(seeded, ready_client: TestClient) -> None:
    """`before` 取游标之前的最近一页；走到头之后 hasEarlier 变 false。"""
    session_id, bodies = seeded

    # seq < 4 的是 1/2/3，取最近 2 条 = seq 2、3
    page = ready_client.get(
        f"{API}/chat/sessions/{session_id}", params={"before": 4, "limit": 2}
    ).json()
    assert [m["content"] for m in page["messages"]] == bodies[1:3]
    assert page["hasEarlier"] is True, "前面还有第 1 句"

    first = ready_client.get(
        f"{API}/chat/sessions/{session_id}", params={"before": 2, "limit": 2}
    ).json()
    assert [m["content"] for m in first["messages"]] == [bodies[0]]
    assert first["hasEarlier"] is False, "这一页已经从头开始了"


def test_after_cursor_returns_only_new_messages(seeded, ready_client: TestClient) -> None:
    """`after` 只返回新增的那些，且不把 hasEarlier 翻成 true。"""
    session_id, bodies = seeded

    payload = ready_client.get(
        f"{API}/chat/sessions/{session_id}", params={"after": 3}
    ).json()
    assert [m["content"] for m in payload["messages"]] == bodies[3:]
    assert payload["hasEarlier"] is False


def test_after_cursor_with_nothing_new_is_empty(seeded, ready_client: TestClient) -> None:
    session_id, _ = seeded

    payload = ready_client.get(f"{API}/chat/sessions/{session_id}", params={"after": 5}).json()

    assert payload["messages"] == []
    assert payload["hasEarlier"] is False


def test_pagination_cannot_reach_someone_elses_session(
    seeded, viewer_client: TestClient
) -> None:
    """分页参数不能成为绕过归属检查的入口 —— 别人的会话仍然 404。"""
    session_id, _ = seeded

    resp = viewer_client.get(f"{API}/chat/sessions/{session_id}", params={"limit": 1})
    assert resp.status_code == 404
