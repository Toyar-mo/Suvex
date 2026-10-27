"""对话页（v0.93）：会话 CRUD、消息落库、工具级权限、引擎闭环。

## 这个功能的失败模式，按危害排序

1. **别人的对话被看见。** `created_by` 过滤漏一处，任何登录用户就能读到
   别人对话里的目标地址、内部路径、凭据。这是唯一"泄露"级别的一条。
2. **只读账号借对话越权。** `/chat` 端点只要 `read`（会话是私有便签，
   要求 admin 会让普通用户连话都不能存）。于是**危险的不是发消息，
   而是消息里那次工具调用** —— `read` 账号让模型去 `save_skill`
   必须被拒。这一条漏了，对话页就是一个绕过整个权限模型的洞。
3. **对话假装执行了但没执行。** v0.107 之前对话没有沙箱，所以风险是
   模型"假装跑了 nmap"并编一个结果，用户拿着假结论去做决策 —— 比报错
   危险得多。v0.107 起了沙箱之后，同一个风险换了形状：模型**能**跑了，
   于是"编一个看起来像 `nmap` 输出的东西"变得更容易被相信。所以现在
   要守的是"工具真的被执行了"（`test_chat.py` 里那组 FakeGateway 用例
   断言 tool 消息的 `tool_failed` 与内容来源），而不是"不许它尝试"。
4. **`seq` 乱序。** 界面会出现"工具结果跑在调用前面"。而且 `seq` 是
   唯一顺序依据（`created_at` 精度不足），它坏了没有替代品。
5. **空转被说成"模型没产出"。** 到工具轮数上限时模型想继续调工具，
   那条提示必须说清"它还想调 X"，而不是让用户去换接入点。
6. **首条消息不生成标题。** 侧栏一列"新对话"，用户找不到任何一次对话。

每一组对应用上面一条。

## 为什么这个文件里大量用 `gateway=` 注入

`run_turn` 有一个 `gateway` 参数，专门为测试留的（见其 docstring）。
真网关要外呼、要花钱、还不确定回什么 —— 用它写断言等于在测别人的模型。
注入假网关之后，"模型说了什么 → 我们落什么库 → 权限判没判对"
这一整条链路都是**确定的**，可以被断言。
"""

from __future__ import annotations

from datetime import UTC

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.deps import Identity
from app.chat import engine
from app.chat import service as chat_service
from app.core.timeutil import now_utc

API = "/api/v1"


# ══════════════════════════════════════════════════════════
# 替身
# ══════════════════════════════════════════════════════════


class _FakeReply:
    """与 `runner.LlmReply` 同形的最小对象。

    ⚠️ 必须有 `raw` / `reasoning`：`engine.run_turn` 会读它们取展示用推理段
      （`getattr(reply, "reasoning", None)` 后回落到 `raw.get(...)`）。
      少了属性本身不会炸（用的 getattr），但少了 `raw` 这个**容器**，
      走那条回落路径的用例会拿不到预期值 —— 所以这里两个都给。
    """

    def __init__(self, text: str, *, reasoning: str | None = None) -> None:
        self.text = text
        self.reasoning = reasoning
        self.raw: dict = {}


class FakeGateway:
    """按剧本回话的假模型。记录收到的 `messages` / `system`，让喂回去什么可断言。"""

    def __init__(self, replies: list[str], *, reasoning: list[str | None] | None = None) -> None:
        self.replies = list(replies)
        self.reasoning = list(reasoning) if reasoning else []
        self.seen: list[list[dict[str, str]]] = []
        self.systems: list[str] = []

    async def chat(self, messages, *, system):  # noqa: ANN001, ANN201
        self.seen.append([dict(m) for m in messages])
        self.systems.append(system)
        text = self.replies.pop(0) if self.replies else ""
        reasoning = self.reasoning.pop(0) if self.reasoning else None
        return _FakeReply(text, reasoning=reasoning)


def _identity(*, scopes: frozenset[str], user_id: str = "user_test") -> Identity:
    """直接造一个身份。比走登录快，也不污染认证链路的测试。"""
    return Identity(
        kind="session",
        label=user_id,
        scopes=scopes,
        user_id=user_id,
        username=user_id,
        role="admin" if "admin" in scopes else "viewer",
    )


ADMIN = _identity(scopes=frozenset({"read", "scan", "admin"}), user_id="user_admin")
READER = _identity(scopes=frozenset({"read"}), user_id="user_reader")


@pytest.fixture()
def chat_session(db_session: Session):
    """一个已落库的空会话（归属 `user_admin`）。

    ⚠️ 用 `db_session` 直接造而不是走 HTTP：本文件里大部分用例测的是
      **service / engine 层**，走 HTTP 会让"到底哪一层错了"变模糊。
      走 HTTP 的那几组（越权、归档、标题）自己调端点。
    """
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()
    return session


# ══════════════════════════════════════════════════════════
# 1. 会话 CRUD 与归属隔离
# ══════════════════════════════════════════════════════════


def test_a_new_session_is_private_and_starts_empty(db_session: Session) -> None:
    session = chat_service.create_session(db_session, created_by="user_a")
    db_session.commit()

    assert session.message_count == 0
    assert session.archived_at is None
    # ★ 归属必须真的落库，不能只是内存里的值。读回来再断一次。
    db_session.expire_all()
    again = chat_service.get_session_or_404(db_session, session_id=session.id, created_by="user_a")
    assert again.created_by == "user_a"


def test_another_users_session_looks_like_it_does_not_exist(db_session: Session) -> None:
    """别人的会话必须是 404，**不是 403**。

    区分"存在但不是你的"会告诉探测者哪些 id 是真的（与契约 §3.1
    「登录失败不区分用户不存在与口令错」同一条理由）。
    """
    session = chat_service.create_session(db_session, created_by="user_a")
    db_session.commit()

    from app.core.errors import ApiError

    with pytest.raises(ApiError) as caught:
        chat_service.get_session_or_404(db_session, session_id=session.id, created_by="user_b")
    assert caught.value.status_code == 404
    # ★ 文案不能泄露"这个 id 存在"。
    #
    # ⚠️ 断言写法要注意：正确文案是「这个对话不存在，可能已被删除。」——
    #    里面**本来就含**"存在"两个字（"不存在"）。所以不能断言
    #    `"存在" not in ...`（我第一版就这么写，红在一个正确实现上）。
    #    要断的是它把**任何一种**情况都统一说成"不存在"，也就是：
    #    文案里出现"不存在"或"已删除"，且**不出现** "不是你的" / "无权限"
    #    这类会区分出"id 真实存在"的措辞。
    text = str(caught.value)
    assert "不存在" in text or "已删除" in text
    assert "不是你的" not in text
    assert "无权限" not in text
    assert "forbidden" not in text.lower()


def test_listing_only_returns_my_own_sessions(db_session: Session) -> None:
    """这是本文件危害最高的一条：漏了过滤 = 泄露别人的对话。"""
    chat_service.create_session(db_session, created_by="user_a", title="我的")
    chat_service.create_session(db_session, created_by="user_b", title="别人的")
    db_session.commit()

    mine = chat_service.list_sessions(db_session, created_by="user_a")
    assert [s.title for s in mine] == ["我的"]

    theirs = chat_service.list_sessions(db_session, created_by="user_b")
    assert [s.title for s in theirs] == ["别人的"]


def test_archived_sessions_leave_the_default_list_but_are_not_deleted(
    db_session: Session,
) -> None:
    """归档是软删：从列表消失，但行还在（消息里的扫描 id 还要能查）。"""
    session = chat_service.create_session(db_session, created_by="user_a", title="要归档的")
    db_session.commit()

    chat_service.archive_session(db_session, session=session, archived=True)
    db_session.commit()

    assert chat_service.list_sessions(db_session, created_by="user_a") == []
    with_archived = chat_service.list_sessions(db_session, created_by="user_a", include_archived=True)
    assert len(with_archived) == 1
    # 行还在，`archived_at` 有值。
    assert with_archived[0].archived_at is not None


def test_renaming_rejects_a_blank_title(db_session: Session) -> None:
    """空标题会让侧栏出现一条看不见的条目。"""
    from app.core.errors import ApiError

    session = chat_service.create_session(db_session, created_by="user_a")
    db_session.commit()

    with pytest.raises(ApiError):
        chat_service.rename_session(db_session, session=session, title="   ")
    # 只有空白被折叠掉之后才判断空 —— "  a  " 应该变成 "a" 而不是被拒。
    chat_service.rename_session(db_session, session=session, title="  a  ")
    assert session.title == "a"


# ══════════════════════════════════════════════════════════
# 2. 消息落库：seq 连续、标题生成、计数不漂
# ══════════════════════════════════════════════════════════


def test_seq_is_contiguous_and_the_first_user_message_becomes_the_title(
    db_session: Session,
) -> None:
    session = chat_service.create_session(db_session, created_by="user_a")
    db_session.commit()

    chat_service.append_user_message(db_session, session=session, content="帮我扫一下 10.0.0.5 的 web")
    chat_service.append_assistant_message(db_session, session=session, content="好，我建一个扫描。")
    tool = chat_service.append_tool_message(
        db_session,
        session=session,
        tool_name="create_scan",
        tool_args={"target": "10.0.0.5"},
        content="已创建 scan_123",
    )
    db_session.commit()

    rows = chat_service.list_messages(db_session, session=session)
    assert [r.seq for r in rows] == [1, 2, 3]
    assert [r.role for r in rows] == ["user", "assistant", "tool"]

    # 首条 user 消息定标题（截到 32 字，这条没到）。
    assert session.title == "帮我扫一下 10.0.0.5 的 web"
    # 冗余计数不许漂 —— 它是列表页唯一不 N+1 的来源。
    assert session.message_count == 3
    # tool 行的结构化字段真的落下来了（前端靠它渲染工具卡）。
    assert tool.tool_name == "create_scan"
    assert tool.tool_args == {"target": "10.0.0.5"}


def test_a_long_first_message_is_clipped_into_a_one_line_title(db_session: Session) -> None:
    """标题是单行元素：换行会把侧栏顶开。"""
    session = chat_service.create_session(db_session, created_by="user_a")
    db_session.commit()

    chat_service.append_user_message(
        db_session,
        session=session,
        content="第一行\n第二行\n\n第三行这里有很长很长很长很长很长很长的内容不该全部进标题",
    )
    db_session.commit()

    assert "\n" not in session.title
    assert session.title.endswith("…")
    assert len(session.title) <= 33  # 32 + 省略号


def test_an_explicit_title_survives_the_first_message(db_session: Session) -> None:
    """用户手动建了带标题的会话时，首条消息**不能**覆盖它。"""
    session = chat_service.create_session(db_session, created_by="user_a", title="我自己起的")
    db_session.commit()

    chat_service.append_user_message(db_session, session=session, content="你好")
    db_session.commit()

    assert session.title == "我自己起的"


def test_list_messages_with_a_limit_returns_the_newest_but_still_ascending(
    db_session: Session,
) -> None:
    """「最近 N 条按时间正序」是聊天界面的标准语义。

    ⚠️ 这一条容易写错成"取最新 5 条、倒序返回"—— 那样界面上最后一条
      会变成中间那条。
    """
    session = chat_service.create_session(db_session, created_by="user_a")
    db_session.commit()
    for i in range(8):
        chat_service.append_user_message(db_session, session=session, content=f"第{i}句")
    db_session.commit()

    rows = chat_service.list_messages(db_session, session=session, limit=3)
    assert [r.content for r in rows] == ["第5句", "第6句", "第7句"]
    assert [r.seq for r in rows] == [6, 7, 8]


def test_an_empty_assistant_message_is_still_stored(db_session: Session) -> None:
    """模型只调工具、不说话时正文是空串 —— 那条消息仍要落。

    它保住的是"模型在这一轮说过什么"的**顺序**。丢掉它，界面上的
    工具卡会突然出现在用户消息后面，看起来像工具被自动触发了。
    """
    session = chat_service.create_session(db_session, created_by="user_a")
    db_session.commit()

    msg = chat_service.append_assistant_message(db_session, session=session, content="")
    db_session.commit()

    assert msg.content == ""
    assert db_session.get(type(msg), msg.id) is not None


def test_an_over_long_body_is_truncated_keeping_the_head(db_session: Session) -> None:
    """口径与 `run_shell` 一致：**留头**。

    关键信息通常在开头（命令回显、错误栈第一行），留尾会把
    "它到底执行了什么"截掉 —— 那正是复核时最需要看的。
    """
    from app.chat.models import MAX_MESSAGE_CHARS

    session = chat_service.create_session(db_session, created_by="user_a")
    db_session.commit()

    body = "HEAD-MARKER" + ("x" * (MAX_MESSAGE_CHARS + 5000)) + "TAIL-MARKER"
    msg = chat_service.append_tool_message(
        db_session,
        session=session,
        tool_name="run",
        tool_args=None,
        content=body,
    )
    db_session.commit()

    assert "HEAD-MARKER" in msg.content
    assert "TAIL-MARKER" not in msg.content
    assert "已截断" in msg.content


# ══════════════════════════════════════════════════════════
# 3. 引擎：上下文拼装
# ══════════════════════════════════════════════════════════


def test_a_tool_result_is_fed_back_as_a_user_turn(db_session: Session) -> None:
    """工具结果伪装成 `user` —— 我们的工具调用是文本协议，没有 id 可配。

    发一个没有 `tool_call_id` 的 `role: "tool"` 消息，多数 provider 会 400。
    """
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()
    chat_service.append_user_message(db_session, session=session, content="扫一下")
    chat_service.append_tool_message(
        db_session,
        session=session,
        tool_name="create_scan",
        tool_args={"target": "a.com"},
        content="已创建",
    )
    db_session.commit()

    rows = chat_service.list_messages(db_session, session=session)
    rendered = engine._render_history(rows)

    assert [m["role"] for m in rendered] == ["user", "user"]
    assert "create_scan" in rendered[1]["content"]
    assert "已创建" in rendered[1]["content"]
    # 一条 `role: "tool"` 都不许出现在要发给模型的消息里。
    assert all(m["role"] != "tool" for m in rendered)


def test_reasoning_is_stored_for_display_but_never_fed_back(db_session: Session) -> None:
    """推理段是展示物。

    回喂它有两个问题：多数 provider 拒绝把 reasoning 当输入；
    而且它显著增长上下文，那些推理对下一轮几乎无用（拿到结果就作废了）。
    """
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()
    chat_service.append_user_message(db_session, session=session, content="扫一下")
    msg = chat_service.append_assistant_message(
        db_session,
        session=session,
        content="我准备建一个扫描。",
        reasoning="SECRET-REASONING 用户可能想先看端口",
    )
    db_session.commit()

    # 落库了（界面要显示）。
    assert "SECRET-REASONING" in (msg.reasoning or "")

    # 但喂回模型时不见。
    rows = chat_service.list_messages(db_session, session=session)
    rendered = engine._render_history(rows)
    assert all("SECRET-REASONING" not in (m["content"] or "") for m in rendered)


def test_a_long_tool_output_is_clipped_before_being_fed_back(db_session: Session) -> None:
    """落库留档 6 万字，喂模型只给 4000 —— 喂模型是花钱。"""
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()
    chat_service.append_tool_message(
        db_session,
        session=session,
        tool_name="nmap",
        tool_args=None,
        content="y" * (engine.HISTORY_TOOL_CHARS + 2000),
    )
    db_session.commit()

    rows = chat_service.list_messages(db_session, session=session)
    rendered = engine._render_history(rows)
    body = rendered[0]["content"]

    assert "未展开" in body
    # 前缀 + 尾部说明；不许把 6000 字原样塞进去。
    assert len(body) < engine.HISTORY_TOOL_CHARS + 200


def test_an_empty_assistant_turn_is_not_sent_to_the_model(db_session: Session) -> None:
    """一个空的 assistant turn 在部分 provider 上是 400。"""
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()
    chat_service.append_user_message(db_session, session=session, content="扫一下")
    chat_service.append_assistant_message(db_session, session=session, content="   ")
    db_session.commit()

    rows = chat_service.list_messages(db_session, session=session)
    rendered = engine._render_history(rows)
    assert rendered == [{"role": "user", "content": "扫一下"}]


# ══════════════════════════════════════════════════════════
# 4. 引擎：工具级权限（本文件危害第二高的一组）
# ══════════════════════════════════════════════════════════


async def test_a_reader_cannot_make_the_model_edit_the_skill_library(
    db_session: Session, chat_session
) -> None:
    """★ `/chat` 只要 `read`，所以真正的门在**工具执行那一刻**。

    这一条漏了，只读账号就能借对话改技能库 —— 那是一个绕过整个权限
    模型的洞，而且从端点上完全看不出来。
    """
    gateway = FakeGateway(
        [
            'ACTION: {"tool": "save_skill", "args": '
            '{"name": "evil", "category": "analysis", "description": "d", "body": "b"}}',
            "我做不到，权限不够。",
        ]
    )

    result = await engine.run_turn(
        db_session,
        session=chat_session,
        content="帮我写一篇技能",
        identity=READER,
        gateway=gateway,
    )

    tool_rows = [m for m in result.messages if m.role == "tool"]
    assert len(tool_rows) == 1
    assert tool_rows[0].tool_failed is True
    assert "权限" in tool_rows[0].content
    # 而且**没有真的写盘**。
    from pathlib import Path

    from app.kernel import skills as skills_mod

    # ⚠️ 这个常量在 `app/kernel/skills.py` 上，不在 `skill_store.py` 上
    #    （`skill_store` 是从那边 import 的）。写错过一次。
    root = Path(skills_mod.CUSTOM_SKILLS_ROOT)
    assert not (root / "analysis" / "evil").exists()


async def test_a_reader_can_still_use_the_read_only_tools(db_session: Session, chat_session) -> None:
    """只读账号不能改东西，但"我上次扫了什么"当然要能问 —— 否则分角色没意义。"""
    gateway = FakeGateway(
        ['ACTION: {"tool": "list_scans", "args": {}}', "平台里还没有扫描任务。"]
    )

    result = await engine.run_turn(
        db_session,
        session=chat_session,
        content="我上次扫了什么",
        identity=READER,
        gateway=gateway,
    )

    tool_rows = [m for m in result.messages if m.role == "tool"]
    assert len(tool_rows) == 1
    assert tool_rows[0].tool_failed is False


async def test_a_reader_cannot_create_a_scan(db_session: Session, chat_session) -> None:
    """建扫描是"让机器对外发包"，`read` 账号不行 —— 与扫描端点同一口径。"""
    gateway = FakeGateway(
        [
            'ACTION: {"tool": "create_scan", "args": {"target": "example.com"}}',
            "权限不够。",
        ]
    )

    result = await engine.run_turn(
        db_session,
        session=chat_session,
        content="扫 example.com",
        identity=READER,
        gateway=gateway,
    )

    tool_rows = [m for m in result.messages if m.role == "tool"]
    assert tool_rows[0].tool_failed is True
    assert "scan" in tool_rows[0].content


def test_the_tool_list_endpoint_hides_what_this_identity_cannot_do(
    viewer_client: TestClient,
) -> None:
    """`GET /chat/tools` 的 `available` 必须反映这个账号的真实权限。

    ## ★ 这条测试原先什么都没测

    它原来的写法是：

        for tool in CHAT_TOOLS:
            available = True if tool.scope is None else reader.can(tool.scope)
            assert available is False, ...

    那个 `available` 是**测试自己按规则算出来的**，跟端点返回什么毫无关系。
    换句话说它只验证了"READER.can() 实现是对的"，而 `GET /chat/tools` 就算
    对所有人返回 `available: true`，这条也照样绿。我是在做变异验证时发现的：
    把端点改成 `available = True` 硬编码，这条仍然 passed。

    教训：**测端点的测试必须打到端点**。自己在测试里重算一遍判据，
    测的是自己的算术，不是产品。
    """
    response = viewer_client.get(f"{API}/chat/tools")
    assert response.status_code == 200

    rows = {row["name"]: row for row in response.json()}
    # 只读账号拿不到这些（引擎执行时会拒，界面必须一致地灰掉）。
    for name in (
        "save_skill",
        "delete_skill",
        "add_mcp_server",
        "create_scan",
        "start_ctf_solve",
    ):
        assert rows[name]["available"] is False, f"{name} 对只读账号不该显示为可用"

    # 读类工具必须仍然可用 —— 否则"全灰"也能让上面那组断言通过。
    for name in ("list_scans", "list_skills", "list_mcp_servers"):
        assert rows[name]["available"] is True, f"{name} 是读操作，不该被挡住"

    # 声明的 scope 要跟判据同一张表（`GET /chat/tools` 会把 scope 一起吐出来）。
    assert rows["save_skill"]["scope"] == "admin"
    assert rows["list_scans"]["scope"] is None


# ══════════════════════════════════════════════════════════
# 5. 引擎：一轮对话的完整闭环
# ══════════════════════════════════════════════════════════


async def test_a_plain_answer_stores_a_user_and_an_assistant_message(db_session: Session, chat_session) -> None:
    gateway = FakeGateway(["你好，我能帮你建扫描、改技能库、加 MCP 工具。"])

    result = await engine.run_turn(
        db_session,
        session=chat_session,
        content="你能做什么",
        identity=ADMIN,
        gateway=gateway,
    )

    assert [m.role for m in result.messages] == ["user", "assistant"]
    assert [m.seq for m in result.messages] == [1, 2]
    # 提示词真的进了 system，而不是被塞进 messages。
    assert gateway.systems and "扫描" in gateway.systems[0]


async def test_a_tool_round_produces_reason_assistant_tool_then_final(
    db_session: Session, chat_session
) -> None:
    """一轮里"模型说要做什么 → 工具卡 → 模型说结果"三段都要落库。

    只保留最后一条会让中间过程消失 —— 那正是对话感的来源。
    """
    gateway = FakeGateway(
        [
            '我先看看有哪些扫描。\nACTION: {"tool": "list_scans", "args": {"limit": 3}}',
            "目前平台上没有扫描任务。",
        ]
    )

    result = await engine.run_turn(
        db_session,
        session=chat_session,
        content="我上次扫了什么",
        identity=ADMIN,
        gateway=gateway,
    )

    assert [m.role for m in result.messages] == ["user", "assistant", "tool", "assistant"]
    # 第一条 assistant 里那句"我先看看"必须保住。
    assert "我先看看" in result.messages[1].content
    # 第二次调模型时，工具结果已经在上下文里了。
    assert any("list_scans" in (m["content"] or "") for m in gateway.seen[1])


async def test_the_stored_bubble_never_contains_the_raw_action_json(
    db_session: Session, chat_session
) -> None:
    """落库给用户看的正文里**不能出现** `ACTION: {...}`。

    ## 这不是美观问题

    模型把"人话"和动作协议写在同一次回复里。把原始的回复整段落库，用户
    在气泡里看到的就是：

        好的，我先查一下最近的扫描。
        ACTION: {"tool": "list_scans", "args": {}}

    —— 工具其实已经调成功了，但界面上看起来像机器人卡住、把内部协议漏了
    出来。真正致命的是**复核**：内部协议出现在用户视野里，就分不清这句话
    是模型说的还是系统拼的。

    ★ 三个必须同时成立的断言，少一个这个 bug 就能溜回来：
      1. 正面：`ACTION:` 不在落库正文里（这是主断言）；
      2. 反面：动作**确实**被执行了 —— 否则"把整段丢掉"也能过测试 1；
      3. 保真：动作前那句"人话"要一字不少地留下（否则修法变成"吞掉一切"）。
    """
    gateway = FakeGateway(
        [
            '好的，我先查一下最近的扫描。\nACTION: {"tool": "list_scans", "args": {"limit": 3}}',
            "没有扫描任务。",
        ]
    )

    result = await engine.run_turn(
        db_session,
        session=chat_session,
        content="我上次扫了什么",
        identity=ADMIN,
        gateway=gateway,
    )

    narration = result.messages[1]
    # 1. 主断言：协议不能出现在用户看得到的地方。
    assert "ACTION:" not in narration.content
    assert "list_scans" not in narration.content
    # 2. 但动作确实执行了 —— 工具卡在，说明我们是"剥掉"而不是"整段丢弃"。
    assert [m.role for m in result.messages] == ["user", "assistant", "tool", "assistant"]
    assert result.messages[2].role == "tool"
    # 3. 那句话要完整。
    assert narration.content == "好的，我先查一下最近的扫描。"


async def test_an_action_with_no_narration_still_does_not_leak_the_json(
    db_session: Session, chat_session
) -> None:
    """模型一上来就写 `ACTION:`（没有任何铺垫）时，正文不能变成整段 JSON。

    ★ 这一条对应 `_narration_before_action` 里 `marker >= 0` 而不是 `> 0`
      那个判断。用 `> 0` 时 `find` 返回 0 会走 else 分支，把整段（含 JSON）
      当成正文存下来 —— 正是要修的 bug，而且只在"没有铺垫"这一种输入下复现，
      上面那条测试抓不到。

    正文此时是空串，于是走"这一轮没有产出"那条占位文案，而不是漏出协议。
    """
    gateway = FakeGateway(
        [
            'ACTION: {"tool": "list_scans", "args": {}}',
            "没有扫描任务。",
        ]
    )

    result = await engine.run_turn(
        db_session,
        session=chat_session,
        content="有什么扫描",
        identity=ADMIN,
        gateway=gateway,
    )

    narration = result.messages[1]
    assert "ACTION" not in narration.content
    assert "list_scans" not in narration.content
    # 动作仍然执行了。
    assert [m.role for m in result.messages] == ["user", "assistant", "tool", "assistant"]


async def test_hitting_the_tool_round_cap_says_what_it_still_wanted(
    db_session: Session, chat_session
) -> None:
    """到上限时模型想继续调工具，提示必须说清"它还想调 X"。

    ★ 这一条是**分流**：把"模型什么都没产出"与"模型产出了但本轮不许用"
      说成同一句话是错的 —— 用户会据此去换接入点，换完还是一样。
    """
    loop = ['ACTION: {"tool": "list_scans", "args": {}}'] * (engine.MAX_TOOL_ROUNDS + 1)
    gateway = FakeGateway(loop)

    result = await engine.run_turn(
        db_session,
        session=chat_session,
        content="一直查",
        identity=ADMIN,
        gateway=gateway,
    )

    assert result.hit_tool_limit is True
    last = result.messages[-1]
    assert last.role == "assistant"
    assert "上限" in last.content
    assert "list_scans" in last.content
    # 不能说成"模型没有产出内容"。
    assert "没有产出内容" not in last.content


async def test_a_model_that_truly_says_nothing_gets_the_other_message(db_session: Session, chat_session) -> None:
    """与上一条对称：真空转时那句提示必须是"没产出内容"，并提到接入点。"""
    gateway = FakeGateway([""])

    result = await engine.run_turn(
        db_session,
        session=chat_session,
        content="你好",
        identity=ADMIN,
        gateway=gateway,
    )

    last = result.messages[-1]
    assert "没有产出内容" in last.content
    assert "上限" not in last.content


async def test_an_unknown_tool_becomes_a_failed_tool_result_not_a_crash(
    db_session: Session, chat_session
) -> None:
    """模型编一个不存在的工具名时，要回一句人话让它换个说法。

    抛异常会中断整轮，用户看到的是"服务器出错了"—— 而原因只是模型
    拼错了一个名字。

    ## ★ v0.107：这里原来用的是 `run_shell`

    那个名字当时**真的不存在**（对话侧只有平台操作工具），所以它是个
    天然的好例子。但 v0.107 把 `run_shell` 接进对话后，这条测试变成了
    "调用一个真实工具" —— 它会去起沙箱（本地没 docker 就抛
    `KernelSandboxError`），断言 `没有叫` 当场失效。

    改成 `scan_ports_quickly`：一个**永远**不该存在的名字，把这条测试的
    意图（"未知工具名 → 可读失败"）与"哪些工具恰好存在"解耦。
    这样将来再接工具也不会再撞一次。
    """
    gateway = FakeGateway(
        [
            'ACTION: {"tool": "scan_ports_quickly", "args": {"target": "1.2.3.4"}}',
            "抱歉，我没有那个能力。",
        ]
    )

    result = await engine.run_turn(
        db_session,
        session=chat_session,
        content="帮我扫一下端口",
        identity=ADMIN,
        gateway=gateway,
    )

    tool_row = next(m for m in result.messages if m.role == "tool")
    assert tool_row.tool_failed is True
    assert "没有叫" in tool_row.content
    # 并且告诉它可用的有哪些（否则模型只能盲猜下一个）。
    assert "create_scan" in tool_row.content


async def test_a_gateway_failure_is_turned_into_a_readable_error(db_session: Session, chat_session) -> None:
    """调模型失败要翻成用户能处理的错，而不是 500「服务器出错了」。"""

    class _Boom:
        async def chat(self, messages, *, system):  # noqa: ANN001, ANN201
            raise RuntimeError("connection reset")

    with pytest.raises(engine.ChatEngineError) as caught:
        await engine.run_turn(
            db_session,
            session=chat_session,
            content="你好",
            identity=ADMIN,
            gateway=_Boom(),
        )
    # 文案里要有异常类型，排查时能对上日志。
    assert "RuntimeError" in str(caught.value)


async def test_create_scan_rejects_an_artifact_target_with_an_explanation(
    db_session: Session, chat_session
) -> None:
    """对话里没有附件，`artifact` 必须明确拒绝并说明为什么。

    与 v0.91 在定时计划上修掉的是**同一个形状**：让一个竞态下必然失败的
    选项可选中，然后让用户对着一条永远修不好的任务猜。
    """
    gateway = FakeGateway(
        [
            'ACTION: {"tool": "create_scan", "args": {"target": "x.zip", "targetKind": "artifact"}}',
            "对话里传不了附件。",
        ]
    )

    result = await engine.run_turn(
        db_session,
        session=chat_session,
        content="扫这个附件",
        identity=ADMIN,
        gateway=gateway,
    )

    tool_row = next(m for m in result.messages if m.role == "tool")
    assert tool_row.tool_failed is True
    assert "artifact" in tool_row.content
    assert "附件" in tool_row.content


async def test_create_scan_without_a_target_asks_rather_than_guesses(
    db_session: Session, chat_session
) -> None:
    """没说扫什么就必须反问。

    猜一个目标去发包，是这个功能里**唯一会造成外部后果**的错误：
    扫错地址等于对别人的机器做了一次未授权扫描。
    """
    gateway = FakeGateway(
        ['ACTION: {"tool": "create_scan", "args": {"skills": ["web"]}}', "你想让我扫哪个地址？"]
    )

    result = await engine.run_turn(
        db_session,
        session=chat_session,
        content="帮我扫一下",
        identity=ADMIN,
        gateway=gateway,
    )

    tool_row = next(m for m in result.messages if m.role == "tool")
    assert tool_row.tool_failed is True
    assert "target" in tool_row.content


# ══════════════════════════════════════════════════════════
# 6. HTTP 层：归属与归档（走真端点）
# ══════════════════════════════════════════════════════════


def test_over_http_sessions_are_scoped_to_the_caller(ready_client: TestClient, viewer_client: TestClient) -> None:
    """★ 端到端再验一次归属隔离 —— service 层测过，但路由可能拼错参数。

    这一条的价值不在重复，而在覆盖"router 把 `identity` 传对了没"：
    service 的 `created_by` 是必填参数，传错值不会有任何报错，只会
    让所有人共用一个列表。
    """
    created = viewer_client.post(f"{API}/chat/sessions", json={"title": "viewer 的对话"})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    # viewer 自己能看见。
    mine = viewer_client.get(f"{API}/chat/sessions")
    assert mine.status_code == 200
    assert [s["id"] for s in mine.json()] == [session_id]

    # 管理员**看不到别人的**。
    theirs = ready_client.get(f"{API}/chat/sessions")
    assert theirs.status_code == 200
    assert theirs.json() == []

    # 直接猜 id 也拿不到（404，不是 403）。
    peek = ready_client.get(f"{API}/chat/sessions/{session_id}")
    assert peek.status_code == 404


def test_over_http_an_archived_session_refuses_new_messages(viewer_client: TestClient) -> None:
    """归档之后发消息要明确拒绝，而不是静默丢弃。

    ⚠️ 这一条得走真端点：`run_turn` 本身不检查归档（它只看会话对象），
      那道门在 router 里。只测引擎会漏掉它。
    """
    created = viewer_client.post(f"{API}/chat/sessions", json={})
    session_id = created.json()["id"]

    archived = viewer_client.delete(f"{API}/chat/sessions/{session_id}")
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived"] is True

    refused = viewer_client.post(
        f"{API}/chat/sessions/{session_id}/messages", json={"content": "还在吗"}
    )
    assert refused.status_code == 400, refused.text
    assert "归档" in refused.text


def test_over_http_a_blank_message_is_rejected(viewer_client: TestClient) -> None:
    """全空格能过 pydantic 的 min_length，但会让模型不知道在回什么。"""
    created = viewer_client.post(f"{API}/chat/sessions", json={})
    session_id = created.json()["id"]

    refused = viewer_client.post(
        f"{API}/chat/sessions/{session_id}/messages", json={"content": "    "}
    )
    assert refused.status_code == 400, refused.text


def test_over_http_the_tools_endpoint_reports_what_this_identity_can_do(
    viewer_client: TestClient,
) -> None:
    """界面上的能力条读这个端点 —— 它必须按**当前身份**算。"""
    listed = viewer_client.get(f"{API}/chat/tools")
    assert listed.status_code == 200, listed.text
    by_name = {tool["name"]: tool for tool in listed.json()}

    # 只读能用的。
    assert by_name["list_scans"]["available"] is True
    # 只读不能用的（这会真的改库/对外发包）。
    assert by_name["create_scan"]["available"] is False
    assert by_name["save_skill"]["available"] is False
    assert by_name["add_mcp_server"]["available"] is False


# ══════════════════════════════════════════════════════════
# 7. 工具清单与实现不许对不上
# ══════════════════════════════════════════════════════════


def test_every_declared_tool_has_an_implementation_and_vice_versa() -> None:
    """两处对不上就是本项目反复踩的那个形状：**声明了却不生效**。

    清单里有的名字没有实现 → 模型调它时永远得到"没有叫 X 的工具"，
    而界面上它显示为可用。反过来更糟：实现了但没声明 → 模型永远不知道它存在。
    """
    from app.chat.engine import _TOOL_IMPLS
    from app.chat.prompts import CHAT_TOOL_NAMES

    declared = set(CHAT_TOOL_NAMES)
    implemented = set(_TOOL_IMPLS)

    assert declared - implemented == set(), f"声明了但没有实现：{declared - implemented}"
    assert implemented - declared == set(), f"实现了但没有声明：{implemented - declared}"


def test_the_system_prompt_tells_the_model_it_has_a_sandbox() -> None:
    """★ v0.107：这条原来叫 `..._tells_the_model_it_has_no_sandbox`。

    ## 为什么它必须反过来

    v0.93 时对话**真的**没有沙箱，所以提示词里有一段"你做不到这些"。
    那段话是有价值的：模型不知道边界时会花几轮去试跑命令，而每一轮
    都是一次真实的 LLM 调用（金钱 + 延迟），最后还是失败。

    v0.107 把 `run_shell` / `browser` 接进来之后，那段话变成了**谎言** ——
    而它的危害比原来更大：模型会对着一个真能跑命令的沙箱说"我没有执行
    环境"，用户看到的就是他抱怨的那个现象（`User said (m02331): "还有这个
    对话不能直接解题，需要修"`）。

    ## 断言什么

    不查文案措辞（改一个字就红是假信号），查**结论**：
    提示词里必须出现"有沙箱"这个正向声明，且**不能**残留"没有沙箱容器"
    这种否定形态。后者是这次改动最容易漏的地方 —— 加了新段落了旧段。
    """
    from app.chat.prompts import CHAT_TOOL_NAMES, build_system_prompt

    prompt = build_system_prompt()

    # 正向：明确告诉模型它有执行环境。
    assert "专属沙箱容器" in prompt, "提示词没告诉模型它有沙箱 —— 它会继续说'我没有执行环境'"
    # 反向：旧的否定声明必须已经删掉。
    assert "你**没有**自己的沙箱容器" not in prompt, "旧的『没有沙箱』声明还留着，模型会两套说法都看到"
    # 执行工具必须被点名（否则模型不知道它能调）。
    for name in ("run_shell", "browser"):
        assert name in prompt, f"提示词里没提 `{name}`"

    # 每个声明的工具名都要在提示词里出现（v0.93 起就有的不变式）。
    for name in CHAT_TOOL_NAMES:
        assert name in prompt, f"提示词里没提 `{name}`"


def test_the_system_prompt_says_how_to_call_a_tool_not_just_that_tools_exist() -> None:
    """工具清单只说明"有什么"，不说明"怎么用" —— 后者必须在提示词里。

    ## 这个用例是一个真事故的回溯（Kali 线上实测）

    首版提示词列了 10 个工具，却**从没写过** `ACTION: {...}` 这个调用协议
    —— 协议只存在于 `engine.py` 的注释里。于是在真机上：

        user      | 现在手上有哪些技能？
        assistant | 用户问现在有哪些技能。调用 list_skills。

    模型**叙述**了自己的意图（第三人称、像旁白），却没有写那行机器可读的
    JSON。`parse_actions` 正确地返回空，工具一次都没执行，数据库里只有
    两条消息、没有任何 tool 记录。**用户看到的是"它做了"，实际上什么都没做**
    ——这比报错更坏，因为没有报错可查。

    所以断言的落点不是"提示词提到了 ACTION 这个词"，而是三件具体的事：
    协议形状、`{"tool":..,"args":..}` 的两个键、以及"叙述不算调用"这句警告。
    """
    from app.chat.prompts import build_system_prompt

    prompt = build_system_prompt()

    # 1) 协议本体：`ACTION:` 后面跟一个真正的 JSON 对象。
    assert 'ACTION: {"tool":' in prompt, "提示词没给出 ACTION 协议的形状"
    assert '"args"' in prompt, "提示词没说明 args 这个键"

    # 2) 协议必须排在工具清单**之前**：先教怎么调，再给清单。
    #    顺序反了模型会先读到一串工具名、然后按自己的习惯去"描述"它们。
    assert prompt.find('ACTION: {"tool":') < prompt.find("list_scans")

    # 3) ★ 最关键的免责：显式告诉模型"叙述不是调用"。
    #    真机失败正是这一句缺失导致的。
    assert "不是调用" in prompt or "不会被当成调用" in prompt

    # 4) 渲染不能吃掉 JSON：`.format()` 会把花括号当占位符（实测炸过
    #    `KeyError: '"tool"'`）。这里钉住"示例里的 JSON 完好无损"。
    assert '"args": {"limit": 5}' in prompt


def test_the_identity_that_owns_a_token_session_is_the_token_label() -> None:
    """令牌通道没有 `user_id`，退化成按令牌 label 归属。

    意思很明确：同一个令牌的多次调用看同一个列表，不同令牌之间隔离。
    这也是"CI 用令牌开对话"该有的语义。
    """
    from app.chat.router import _owner

    token_identity = Identity(kind="token", label="CI 流水线", scopes=frozenset({"read"}))
    assert _owner(token_identity) == "token:CI 流水线"

    assert _owner(ADMIN) == "user_admin"


def test_now_utc_is_timezone_aware() -> None:
    """所有时间列都是 `timestamptz`。返回裸 datetime 会让比较时炸在
    "can't compare offset-naive and offset-aware"。这一条是防回归。"""

    assert now_utc().tzinfo is not None
    assert now_utc().tzinfo is UTC
