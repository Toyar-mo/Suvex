"""实时事件流：历史 seed、名册、SSE 帧与补发游标（契约 §4）。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.events.stream import (
    encode_event,
    encode_keepalive,
    encode_open,
    encode_resync,
    resolve_cursor,
    stream_events,
)
from tests.factories import make_scan, make_scan_with_events


def _parse_frame(frame: str) -> dict[str, object]:
    lines = [line for line in frame.split("\n") if line]
    parsed: dict[str, object] = {}
    for line in lines:
        if line.startswith(":"):
            parsed["comment"] = line
            continue
        field, _, value = line.partition(": ")
        parsed[field] = value
    return parsed


# ══════════════════════════════════════════════════════════
# 历史 seed（契约 §4.4）
# ══════════════════════════════════════════════════════════


def test_recent_events_are_ascending_with_full_shape(
    db_session: Session, ready_client: TestClient
) -> None:
    """seed 返回**升序**，且每条都是**完整事件**（8 种形状，带 detail + agentPhase）。"""
    scan, _agent, events = make_scan_with_events(db_session, count=3)

    response = ready_client.get(f"/api/v1/scans/{scan.id}/events/recent")

    assert response.status_code == 200
    body = response.json()
    assert body["hasMore"] is False
    assert [item["id"] for item in body["events"]] == [event.id for event in events]

    first = body["events"][0]
    # 字段集合与契约 §2.5 的"完整事件"逐字一致 —— 包括 agentPhase
    # （它是契约相对前端现状唯一新增的字段）。
    assert set(first) == {
        "id",
        "scanId",
        "agentId",
        "agentPhase",
        "type",
        "summary",
        "detail",
        "at",
    }
    assert first["detail"]["kind"] == "tool_call"
    # exitCode 可为 null（进程被信号杀死时没有退出码）。
    assert first["detail"]["exitCode"] is None


def test_recent_events_has_more_flag(db_session: Session, ready_client: TestClient) -> None:
    """`hasMore` 用"多取一条"判断，而不是 count(*)。

    它只有两个状态，但错了会让前端以为历史已经取完 ——
    表现是活动流"开头少了一段"，而那看起来像事件丢失。
    """
    scan, _agent, _events = make_scan_with_events(db_session, count=3)

    body = ready_client.get(f"/api/v1/scans/{scan.id}/events/recent?limit=2").json()

    assert len(body["events"]) == 2
    assert body["hasMore"] is True


def test_agents_roster_endpoint(db_session: Session, ready_client: TestClient) -> None:
    scan, agent, _events = make_scan_with_events(db_session, count=1)

    response = ready_client.get(f"/api/v1/scans/{scan.id}/agents")

    assert response.status_code == 200
    roster = response.json()
    assert len(roster) == 1
    assert set(roster[0]) == {
        "id",
        "role",
        "status",
        "target",
        "scanId",
        "eventCount",
        "lastActiveAt",
    }
    assert roster[0]["id"] == agent.id
    assert roster[0]["scanId"] == scan.id


def test_agents_roster_is_empty_for_scan_without_agents(
    db_session: Session, ready_client: TestClient
) -> None:
    """名册为空是**正常答案**，不是错误。

    返回 404 会让前端把「还没开始」渲染成「出错了」。
    """
    scan = make_scan(db_session)

    response = ready_client.get(f"/api/v1/scans/{scan.id}/agents")

    assert response.status_code == 200
    assert response.json() == []


def test_events_endpoints_404_on_unknown_scan(ready_client: TestClient) -> None:
    """订阅一个不存在的扫描必须是 404。

    不检查的话会得到一个**永远沉默的 200**，而前端无法区分
    「目标不存在」与「暂时没有事件」。
    """
    for path in ("/events", "/events/recent"):
        response = ready_client.get(f"/api/v1/scans/scan_nope{path}")
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "NOT_FOUND"


# ══════════════════════════════════════════════════════════
# 补发游标（契约 §4.4）
# ══════════════════════════════════════════════════════════


def test_cursor_follows_last_event_id(db_session: Session) -> None:
    scan, _agent, events = make_scan_with_events(db_session, count=3)

    cursor, reason = resolve_cursor(scan.id, events[0].id)

    assert reason is None
    assert cursor == events[0].seq


def test_cursor_without_id_starts_at_tail(db_session: Session) -> None:
    """没有游标 = 客户端没做 seed → 从**当前末尾**开始，不重放历史。

    从头发的话，一个跑了三小时的扫描会让每次打开页面都推几千帧，
    而前端会全部渲染一遍。
    """
    scan, _agent, events = make_scan_with_events(db_session, count=3)

    cursor, reason = resolve_cursor(scan.id, None)

    assert reason is None
    assert cursor == events[-1].seq


def test_stale_cursor_asks_for_resync(db_session: Session) -> None:
    """`Last-Event-ID` 指向一条已经不存在的事件 → 发 `resync` 帧。

    静默地从当前位置继续会让客户端以为中间没有漏，而实际上漏了一段 ——
    这正是契约 §4.4 要求发 resync 的原因。
    """
    scan, _agent, events = make_scan_with_events(db_session, count=3)

    cursor, reason = resolve_cursor(scan.id, "evt_already_cleaned_up")

    assert reason is not None and "清理" in reason
    assert cursor == events[-1].seq


def test_huge_backlog_asks_for_resync(db_session: Session, monkeypatch) -> None:
    """积压超过上限也发 resync。

    「事件还在但积压了十万条」与「事件被清理了」对客户端是**同一件事**：
    都该回退到重拉历史，而不是让服务端慢慢推。
    """
    scan, _agent, events = make_scan_with_events(db_session, count=3)

    from app.core import config as config_module

    config_module.get_settings.cache_clear()
    monkeypatch.setenv("SUPEX_SSE_REPLAY_MAX_EVENTS", "1")
    try:
        cursor, reason = resolve_cursor(scan.id, events[0].id)
        assert reason is not None and "上限" in reason
        assert cursor == events[-1].seq
    finally:
        monkeypatch.undo()
        config_module.get_settings.cache_clear()


# ══════════════════════════════════════════════════════════
# 帧格式（契约 §4.2）
# ══════════════════════════════════════════════════════════


def test_event_frame_format(db_session: Session) -> None:
    _scan, _agent, events = make_scan_with_events(db_session, count=1)
    frame = encode_event(events[0])
    parsed = _parse_frame(frame)

    # `id` 必须给 —— 它是 Last-Event-ID 补发的唯一依据。
    assert parsed["id"] == events[0].id
    assert parsed["event"] == "activity"
    # 每帧以空行结束（SSE 规范）。
    assert frame.endswith("\n\n")

    payload = json.loads(str(parsed["data"]))
    assert payload["detail"]["kind"] == "tool_call"
    # 中文不转义（ensure_ascii=False）—— 转成 \uXXXX 会让帧体积涨 3 倍，
    # 而且日志里完全不可读。SSE 规范本身规定流是 UTF-8。
    assert "端口扫描" in str(parsed["data"])


def test_keepalive_is_a_comment_line() -> None:
    """心跳用注释行（以 `:` 开头）。

    空帧在部分代理上会被当作"无内容"而触发缓冲；注释行是明确有内容
    但不触发任何事件的。
    """
    frame = encode_keepalive()

    assert frame.startswith(":")
    assert frame.endswith("\n\n")


def test_resync_frame_is_its_own_event_type() -> None:
    frame = encode_resync("需要重新拉取")

    assert "event: resync" in frame


# ══════════════════════════════════════════════════════════
# ★ 首帧必须立刻发（2026-10-10 实测事故）
# ══════════════════════════════════════════════════════════
#
# 实测：直连后端 TTFB 0.005 秒，经 Vite 代理 22.4 秒，而后者等到的
# 首个字节正是 20 秒心跳。Node 的 `res.writeHead()` 只记头不写 socket，
# 要等第一次 `write()` 正文 —— 而流在心跳前不产出任何字节。
#
# 症状是前端一直显示「连接中」（`onopen` 等响应头），而事件照常显示
# （来自 seed），所以看起来像状态标签坏了。
#
# 这一组盯的是"**生成器的第一帧就是 open**"这个契约。
def test_open_frame_is_a_comment_line() -> None:
    """首帧也是注释行 —— 空帧会被部分代理当成"无内容"而继续缓冲。"""
    frame = encode_open()

    assert frame.startswith(":")
    assert frame.endswith("\n\n")


def test_open_and_keepalive_are_distinguishable() -> None:
    """两种注释行文字要能分辨。

    排查时 `curl -N` 的输出里，`: open` 说明"连上了"、`: keepalive`
    说明"又跳了一次心跳"。两者一样的话，就分不出代理有没有及时放行。
    """
    assert encode_open() != encode_keepalive()


def test_stream_emits_open_before_anything_else(db_session: Session) -> None:
    """★ **第一帧必须是 open**，早于补发事件、早于 resync。

    这一条是上面那次事故的直接复现。判据用"第一帧的内容"而不是
    "open 出现在输出里" —— 后者只要它在任何位置就算过，
    而我们需要的是它**排在最前**（放中间的话，响应头仍然是攒到
    第一条事件才发，代理那条路上的症状一点没变）。
    """
    scan, _agent, _events = make_scan_with_events(db_session, count=2)

    generator = stream_events(scan_id=scan.id, last_event_id=None)
    try:
        first = next(generator)
    finally:
        generator.close()

    assert first == encode_open(), f"首帧不是 open，而是 {first[:40]!r}"


def test_open_comes_before_the_resync_frame(db_session: Session) -> None:
    """游标失效时，open 仍要排在 resync **之前**。

    顺序反了的话，resync 之前仍然没有任何字节 —— 而"游标失效"恰恰是
    重连场景，那是最需要代理立刻放行的时候。
    """
    scan, _agent, _events = make_scan_with_events(db_session, count=1)

    generator = stream_events(scan_id=scan.id, last_event_id="evt_gone")
    try:
        first, second = next(generator), next(generator)
    finally:
        generator.close()

    assert first == encode_open()
    assert "event: resync" in second


def test_open_frame_does_not_consume_the_replay_cursor(db_session: Session) -> None:
    """首帧是注释、不是事件 —— 它不能把补发游标往前推。

    判据是**补发内容本身**：游标之后的两条一条不少、顺序不变。
    只断言"open 在"是不够的 —— 游标被首帧污染的话，事件会少一条，
    而那种失败在只看首帧的测试里完全看不出来。
    """
    scan, _agent, events = make_scan_with_events(db_session, count=3)

    generator = stream_events(scan_id=scan.id, last_event_id=events[0].id)
    try:
        frames = [next(generator), next(generator), next(generator)]
    finally:
        generator.close()

    # 第一帧是 open，其后两条才是补发的事件
    assert frames[0] == encode_open()
    ids = [_parse_frame(frame)["id"] for frame in frames[1:]]
    assert ids == [events[1].id, events[2].id]


def test_stream_replays_only_events_after_cursor(db_session: Session) -> None:
    """补发的语义：**游标之后**的事件，一条不多一条不少。

    ⚠️ 生成器的第一帧恒为 `open`（见下面那一组），所以这里要先跳过它。
    不跳的话拿到的是注释行、`_parse_frame` 里没有 `id`，测试会以一个
    看不出原因的方式挂掉。
    """
    scan, _agent, events = make_scan_with_events(db_session, count=3)

    generator = stream_events(scan_id=scan.id, last_event_id=events[0].id)
    try:
        assert next(generator) == encode_open()
        frames = [next(generator), next(generator)]
    finally:
        generator.close()

    ids = [_parse_frame(frame)["id"] for frame in frames]
    assert ids == [events[1].id, events[2].id]
    # 游标之前的那条**不能**出现 —— 出现了就说明补发是从头开始的。
    assert events[0].id not in ids


def test_stream_sends_resync_then_continues(db_session: Session) -> None:
    scan, _agent, events = make_scan_with_events(db_session, count=2)

    generator = stream_events(scan_id=scan.id, last_event_id="evt_gone")
    try:
        # 首帧是 open，resync 排在它之后（顺序本身另有专门的用例盯着）
        assert next(generator) == encode_open()
        first = next(generator)
    finally:
        generator.close()

    assert "event: resync" in first


# ══════════════════════════════════════════════════════════
# HTTP 层
# ══════════════════════════════════════════════════════════


def test_sse_endpoint_wires_streaming_response_correctly(
    db_session: Session, ready_client: TestClient, monkeypatch
) -> None:
    """HTTP 层的接线：状态码、media type、响应头、以及帧真的被送出去了。

    ⚠️ 这里**把 `stream_events` 换成了一个有限生成器**，是刻意的：

    真实的 `stream_events` 是**永不结束**的（它一直轮询等新事件）。
    用 `TestClient` 打一个永不结束的响应会让测试永久挂住 ——
    实测就是这样（整轮 `pytest` 卡了 7 分钟没有任何输出）。
    给它加一个"只跑 3 秒"的开关是更坏的做法：那会让生产代码里
    多一个只为测试存在的分支。

    所以职责切开：**生成器逻辑**由上面的用例直接调 `stream_events` 覆盖
    （那是真正的逻辑），**HTTP 接线**由这条覆盖（把生成器换成有限的）。
    端到端的真实流由 `scripts/verify_acceptance.py` 对着真 uvicorn 覆盖 ——
    那里可以真的开一个 socket、读几帧、然后关掉。
    """
    scan, _agent, events = make_scan_with_events(db_session, count=2)

    def finite_stream(*, scan_id, last_event_id):  # noqa: ANN001, ARG001
        yield encode_event(events[0])
        yield encode_event(events[1])

    monkeypatch.setattr("app.events.router.stream_events", finite_stream)

    response = ready_client.get(f"/api/v1/scans/{scan.id}/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    # 少了 X-Accel-Buffering，前面挂 nginx 时会出现"活动流一直空白，
    # 直到扫描结束才一次性出现"，而那个现象看起来像前端问题。
    assert response.headers.get("x-accel-buffering") == "no"
    assert response.headers["cache-control"].startswith("no-cache")

    body = response.text
    assert f"id: {events[0].id}" in body
    assert f"id: {events[1].id}" in body
    assert body.count("event: activity") == 2


def test_sse_endpoint_rejects_unknown_scan_before_streaming(ready_client: TestClient) -> None:
    """订阅不存在的扫描 → 404，而不是一个**永远沉默的 200**。

    永远沉默的 200 让前端无法区分「目标不存在」与「暂时没有事件」，
    而这两种情况该给用户的提示完全不同。
    """
    response = ready_client.get("/api/v1/scans/scan_missing/events")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
