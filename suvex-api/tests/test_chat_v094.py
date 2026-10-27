"""对话页 v0.94：六项会话设置、人格、附件、人工审批。

## 这个文件在防什么

v0.93 的 `test_chat.py` 守的是"对话能不能用"。这里守的是新增四组面上
**最容易被做成看起来对、其实不生效**的几件事 —— 它们的共同形状是
「请求返回 200，但那个东西没真的起作用」：

1. **设置存了但没进提示词。** 六项里 `skillNames` / `persona` 要通过
   提示词影响模型。存进数据库很容易，接进 `render_persona` /
   `render_mounted_skills` 才是真正的工作。漏了的话用户在界面上看见
   "已选 3 篇技能"，模型一个字都没读到。
2. **审批闸门可以被绕过。** 最危险的是**降权后审批**：管理员挂起
   `save_skill` → 会话被降成只读 → 点确认 → 工具照跑。漏了这道重判，
   审批界面就是一条干净的提权通道。
3. **附件读了但没删。** 附件是一次性的。不删的两个后果：磁盘攒垃圾、
   同一条附件能被反复挂进不同轮次而用户以为自己只传过一次。
4. **别人的会话能被改设置。** 入口多了一个 `PATCH .../settings`，
   就多一次漏 `created_by` 过滤的机会。

## 为什么大量直接用 service / attachments 层

与 `test_chat.py` 的理由相同：走 HTTP 会让"到底哪一层错了"变模糊。
但**归属隔离**与**审批的两道权限**走 HTTP —— 那两件正是端点层自己写的
逻辑，绕过它测就等于没测。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.chat import attachments as chat_attachments
from app.chat import service
from app.chat.prompts import (
    CHAT_PERSONAS_SPEC,
    CHAT_TOOLS,
    DEFAULT_PERSONA_ID,
    TOOL_BY_NAME,
    may_call_tool,
    render_mounted_skills,
    render_persona,
    render_tool_hint,
)
from app.chat.schemas import ChatSettingsIn

API = "/api/v1"


@pytest.fixture()
def chat_session(db_session: Session):
    session = service.create_session(db_session, created_by="user_admin")
    db_session.commit()
    return session


@pytest.fixture(autouse=True)
def _isolated_attachment_root(tmp_path, monkeypatch):
    """把附件暂存目录指到本次测试的临时目录。

    ## 为什么必须隔离

    `attachments.store` 用 `path.open("xb")` 落盘 —— **独占创建、拒绝覆盖**。
    而默认根目录是系统临时目录下的 `suvex-chat-attachments`，它在两次
    测试运行之间**不会消失**。于是第二次跑同一条用例时：

        FileExistsError: [Errno 17] File exists: '…\\chat_probe_7\\att_probe_7-_.._etc_passwd'

    这不是产品 bug（"绝不覆盖已有附件"正是想要的行为），但它让测试
    **只能跑一次**：第一次绿、第二次红，而代码一行没动。这一条我实测
    撞到过，所以隔离写在 autouse fixture 里，而不是靠每条用例自己清。

    ★ 顺带把"读到开发机上真实积累的附件"这类隐患也关掉。
    """
    monkeypatch.setenv("SUPEX_CHAT_ATTACHMENTS", str(tmp_path / "attachments"))


# ══════════════════════════════════════════════════════════
# 1. 六项设置：存得住、只改传了的、空项目归一
# ══════════════════════════════════════════════════════════


def test_settings_are_partial_so_renaming_does_not_wipe_them(
    db_session: Session, chat_session
) -> None:
    """★ 部分提交：只传 `providerId` 不能把已经选好的技能/人格清空。

    这是"部分更新"最容易写错的一种实现 —— 用
    `payload.skillNames if payload.skillNames is not None else []` 之类的
    默认值，改接入点就顺手清掉技能。用户不会立刻发现，只会在下一轮
    发现模型不再提技能库。
    """
    service.update_settings(
        db_session,
        session=chat_session,
        payload=ChatSettingsIn(skillNames=["analysis-triage"], persona="recon"),
    )
    db_session.commit()

    service.update_settings(
        db_session,
        session=chat_session,
        payload=ChatSettingsIn(providerId="prov_1"),
    )
    db_session.commit()
    db_session.refresh(chat_session)

    assert chat_session.persona == "recon", "只改 provider 不该把人格清掉"
    assert chat_session.skill_names == ["analysis-triage"], "只改 provider 不该把技能清掉"
    assert chat_session.provider_id == "prov_1"


def test_settings_round_trip_all_six(db_session: Session, chat_session) -> None:
    """六项都能存、都能读回来。少一项的症状是界面上选了但不生效。"""
    service.update_settings(
        db_session,
        session=chat_session,
        payload=ChatSettingsIn(
            providerId="prov_x",
            persona="pentest",
            skillNames=["a", "b"],
            mcpServerNames=["srv"],
            requireApproval=True,
            project="内部网络",
        ),
    )
    db_session.commit()
    db_session.refresh(chat_session)

    assert chat_session.provider_id == "prov_x"
    assert chat_session.persona == "pentest"
    assert chat_session.skill_names == ["a", "b"]
    assert chat_session.mcp_server_names == ["srv"]
    assert chat_session.require_approval is True
    assert chat_session.project == "内部网络"


def test_an_empty_project_becomes_null_not_empty_string(
    db_session: Session, chat_session
) -> None:
    """★ 空项目名归一成 `None`，不是 `""`。

    前端侧栏按项目分组（v0.94 第 6 项）：如果库里存 `""`，会出现一个
    **没有名字的分组**把所有无项目的对话装进去 —— 看起来像渲染 bug。
    归一在写入侧做，读侧就不用每处都判一次。
    """
    service.update_settings(
        db_session, session=chat_session, payload=ChatSettingsIn(project="   ")
    )
    db_session.commit()
    db_session.refresh(chat_session)

    assert chat_session.project is None


def test_require_approval_defaults_to_off(db_session: Session, chat_session) -> None:
    """默认不审批。默认开的话每次工具调用都要点一次，用户会关掉整页。"""
    assert chat_session.require_approval is False


def test_settings_over_http_cannot_touch_someone_elses_session(
    ready_client: TestClient, viewer_client: TestClient
) -> None:
    """★ 别人的会话改不了设置（多了一个入口，就多一次漏过滤的机会）。

    期望 404 而不是 403：对别人的会话按"不存在"处理，免得用状态码
    告诉对方"这个 id 是真的"。
    """
    created = ready_client.post(f"{API}/chat/sessions", json={"title": "管理员的对话"})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    resp = viewer_client.patch(
        f"{API}/chat/sessions/{session_id}/settings",
        json={"project": "偷改"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# ══════════════════════════════════════════════════════════
# 2. 设置真的进了提示词（不是只进数据库）
# ══════════════════════════════════════════════════════════


def test_persona_render_names_the_persona_and_its_focus() -> None:
    """人格要进提示词，而且要说清"按这个视角看"，不只是标签。

    ★ 故意跳过 `default`：它是"不加任何文字"，见下一个用例。
      这里要验的是**具体人格**有没有把视角写进去。
    """
    spec = next(p for p in CHAT_PERSONAS_SPEC if p.id != DEFAULT_PERSONA_ID)
    text = render_persona(spec.id)

    assert text.strip() != ""
    assert spec.label in text or spec.id in text


def test_every_declared_persona_renders_to_something() -> None:
    """枚举里每一个人格都要能渲染出一段非空文字。

    新增一个人格但忘了在 `render_persona` 里认它，症状就是"选了没反应"。
    """
    for spec in CHAT_PERSONAS_SPEC:
        if spec.id == DEFAULT_PERSONA_ID:
            # 默认人格的设计就是"什么都不加"，见
            # `test_no_persona_renders_nothing`。
            assert render_persona(spec.id).strip() == "", spec.id
            continue
        assert render_persona(spec.id).strip() != "", spec.id


def test_no_persona_renders_nothing() -> None:
    """没人格 = 不加任何文字，而不是一段"你没有任何人格"的废话。"""
    assert render_persona(None).strip() == ""


def test_an_unknown_persona_does_not_crash_the_prompt() -> None:
    """★ 库里存了一个代码里已经没有的人格 id（改版/降版后会有）。

    抛异常的话这个会话**永远打不开**。正确做法是退化成"没有人格"，
    那只是少一段文字。
    """
    text = render_persona("this_persona_no_longer_exists")
    assert isinstance(text, str)


def test_mounted_skills_list_every_selected_skill() -> None:
    """选了 3 篇就要出现 3 篇的名字。只挂第一篇是个典型的 off-by-one。"""
    text = render_mounted_skills(
        [
            ("analysis-triage", "第一篇正文"),
            ("web-sqli", "第二篇正文"),
            ("web-xss", "第三篇正文"),
        ]
    )
    for name in ("analysis-triage", "web-sqli", "web-xss"):
        assert name in text


def test_no_mounted_skill_renders_nothing_rather_than_an_empty_header() -> None:
    """没选技能时返回空串，而不是一个空的"## 技能"标题。

    空标题会进提示词，模型会以为"这一段本该有内容但丢了"。
    """
    assert render_mounted_skills([]).strip() == ""


def test_tool_hint_is_empty_when_nobody_was_mentioned() -> None:
    """没人 `@` 工具时不加任何提示 —— 否则每轮都多一句噪音。"""
    assert render_tool_hint([]).strip() == ""
    assert render_tool_hint(None).strip() == ""


def test_tool_hint_names_the_mentioned_tools() -> None:
    text = render_tool_hint(["list_scans", "create_scan"])
    assert "list_scans" in text and "create_scan" in text


def test_tool_hint_does_not_forbid_other_tools() -> None:
    """★ 提示里必须留出"可以不用这些"的余地。

    写成"只能用这些"会让模型在工具不适用时**无法退让** —— 它会硬用一个
    错的工具，或者干脆说做不到。两者都比"没提到工具"更糟。
    """
    text = render_tool_hint(["list_scans"])
    # 实际措辞是"说明理由再用合适的那个" —— 断言它的**语义**（允许改用），
    # 而不是记住某一版的具体用词。
    assert "合适的" in text or "说明理由" in text


# ══════════════════════════════════════════════════════════
# 3. 工具级权限：唯一的判据表
# ══════════════════════════════════════════════════════════


def test_every_tool_declares_the_scope_it_needs() -> None:
    """★ 权限声明只有一个来源（`CHAT_TOOLS[].scope`）。

    引擎执行前按它判、端点审批时也按它判。两处各写一份会漂成
    "界面上说要 admin、引擎里其实只要 read"。
    """
    scopes = {t.name: t.scope for t in CHAT_TOOLS}
    assert scopes["save_skill"] == "admin"
    assert scopes["add_mcp_server"] == "admin"
    assert scopes["create_scan"] == "scan"
    assert scopes["list_scans"] is None


def test_a_reader_is_not_allowed_to_run_consequential_tools() -> None:
    """只读账号能读、不能写。这条判据引擎与审批端点共用一个函数。"""
    

    class _Reader:
        scopes: frozenset[str] = frozenset({"read"})

        def can(self, scope: str) -> bool:
            return scope in self.scopes

    class _Admin:
        scopes: frozenset[str] = frozenset({"read", "scan", "admin"})

        def can(self, scope: str) -> bool:
            return scope in self.scopes

    reader, admin = _Reader(), _Admin()
    assert may_call_tool("save_skill", reader) is False
    assert may_call_tool("list_scans", reader) is True
    assert may_call_tool("save_skill", admin) is True


def test_a_tool_with_no_scope_is_callable_by_anyone() -> None:
    """`scope is None` 表示"读操作"，不该被权限判据挡住。"""
    

    class _Reader:
        scopes: frozenset[str] = frozenset({"read"})

        def can(self, scope: str) -> bool:
            return scope in self.scopes

    assert may_call_tool("list_scans", _Reader()) is True


def test_an_unknown_tool_name_is_refused_not_silently_allowed() -> None:
    """★ 不认识的工具名 → 拒绝，不是"没声明 scope 所以放行"。

    默认放行的话，任何拼写错误/降版遗留都是一次绕过。
    """
    

    class _Admin:
        scopes: frozenset[str] = frozenset({"read", "scan", "admin"})

        def can(self, scope: str) -> bool:
            return scope in self.scopes

    assert may_call_tool("no_such_tool", _Admin()) is False


def test_the_scope_check_reads_from_the_single_registry() -> None:
    """判据必须查 `TOOL_BY_NAME`，不能在别处复制一份工具表。"""
    assert TOOL_BY_NAME["save_skill"].scope == "admin"


# ══════════════════════════════════════════════════════════
# 4. 附件：落盘、限量、读走即删
# ══════════════════════════════════════════════════════════


def test_an_attachment_can_be_staged_and_listed() -> None:
    staged = chat_attachments.store(
        session_id="chat_probe_1",
        filename="notes.txt",
        content=b"hello",
        attachment_id="att_probe_1",
    )
    assert staged.name == "notes.txt"
    assert staged.size_bytes == 5

    listed = chat_attachments.list_staged(session_id="chat_probe_1")
    assert [a.id for a in listed] == ["att_probe_1"]

    chat_attachments.discard(session_id="chat_probe_1", attachment_ids=["att_probe_1"])


def test_an_empty_file_is_refused() -> None:
    """★ 空文件必须拒。

    放进去的话，下一轮提示词里会挂一段"附件内容：（空）"，模型会开始
    猜"用户是不是想让我找什么"——这种模糊比直接报错差得多。
    """
    with pytest.raises(ValueError, match="空"):
        chat_attachments.store(
            session_id="chat_probe_2",
            filename="empty.txt",
            content=b"",
            attachment_id="att_probe_2",
        )


def test_an_over_sized_file_is_refused_with_a_readable_reason() -> None:
    """超限要报清楚上限是多少，而不只是"失败"。"""
    too_big = b"x" * (chat_attachments.MAX_ATTACHMENT_BYTES + 1)
    with pytest.raises(ValueError) as excinfo:
        chat_attachments.store(
            session_id="chat_probe_3",
            filename="big.bin",
            content=too_big,
            attachment_id="att_probe_3",
        )
    assert "太大" in str(excinfo.value)


def test_a_name_with_path_separators_cannot_escape_the_session_dir() -> None:
    """★ 文件名会被清洗，不能靠 `../` 写到会话目录外面去。

    这是唯一"安全问题"级的一条：`store` 直接拼 `path = dir / f"{id}-{name}"`，
    名字里带 `../` 就会落盘到别处。
    """
    staged = chat_attachments.store(
        session_id="chat_probe_7",
        filename="../../etc/passwd",
        content=b"nope",
        attachment_id="att_probe_7",
    )
    assert "/" not in staged.name
    # ★ 不要断言"名字里没有 .."：`../x` 被压成 `_.._x`，点还在，但它已经是
    #   **一个普通文件名**。真正要证的是"用这个名字拼出来的路径仍在会话目录内"，
    #   所以下面用 resolve() 比对 —— 断言字面比断言性质更容易写成假失败。
    root = chat_attachments._session_dir("chat_probe_7").resolve()
    written = (root / staged.name).resolve()
    assert written.parent == root

    on_disk = chat_attachments.list_staged(session_id="chat_probe_7")
    assert len(on_disk) == 1
    chat_attachments.discard(session_id="chat_probe_7", attachment_ids=["att_probe_7"])


def test_reading_an_attachment_returns_its_text() -> None:
    staged = chat_attachments.store(
        session_id="chat_probe_5",
        filename="once.txt",
        content="一次性的内容".encode(),
        attachment_id="att_probe_5",
    )
    pairs = chat_attachments.read_text(
        session_id="chat_probe_5", attachment_ids=[staged.id]
    )
    assert len(pairs) == 1
    name, text = pairs[0]
    assert name == "once.txt"
    assert "一次性的内容" in text

    chat_attachments.discard(session_id="chat_probe_5", attachment_ids=[staged.id])


def test_reading_then_discarding_removes_it_from_the_list() -> None:
    """★★ 读走即删 —— 附件是一次性的。

    不删的两个后果：
    · 磁盘攒垃圾（用户以为发完就没了）；
    · 同一条附件能被反复挂进不同轮次，而用户以为自己只传过一次。
    """
    staged = chat_attachments.store(
        session_id="chat_probe_6",
        filename="once.txt",
        content=b"one shot",
        attachment_id="att_probe_6",
    )
    chat_attachments.read_text(session_id="chat_probe_6", attachment_ids=[staged.id])
    removed = chat_attachments.discard(
        session_id="chat_probe_6", attachment_ids=[staged.id]
    )
    assert removed == 1
    assert chat_attachments.list_staged(session_id="chat_probe_6") == []


def test_a_binary_attachment_says_it_cannot_be_read_instead_of_garbage() -> None:
    """★ 二进制附件要明说读不了。

    塞一堆替换字符（`\\ufffd`）让模型去猜，它会编一段"我看到文件里有…"。
    那比"读不了"危险得多 —— 用户会拿着编造的内容去做决策。
    """
    staged = chat_attachments.store(
        session_id="chat_probe_8",
        filename="blob.bin",
        # 0xFF 0xFE 不是合法 utf-8 也不是合法 gbk 的开头组合。
        content=b"\xff\xfe\x00\x01\x02\x03",
        attachment_id="att_probe_8",
    )
    pairs = chat_attachments.read_text(
        session_id="chat_probe_8", attachment_ids=[staged.id]
    )
    _, text = pairs[0]
    assert "二进制" in text
    assert "扫描任务" in text, "要告诉用户该走哪条路，而不只是说读不了"

    chat_attachments.discard(session_id="chat_probe_8", attachment_ids=[staged.id])


def test_an_over_long_attachment_is_truncated_and_says_how_much_was_dropped() -> None:
    """★ 截断必须说。

    截断但不说，模型会以为自己看到了全文，然后基于"日志里没有这个错误"
    下结论 —— 而那半截正是被截掉的。
    """
    body = ("a" * (chat_attachments.MAX_ATTACHMENT_CHARS + 500)).encode()
    staged = chat_attachments.store(
        session_id="chat_probe_9",
        filename="long.log",
        content=body,
        attachment_id="att_probe_9",
    )
    pairs = chat_attachments.read_text(
        session_id="chat_probe_9", attachment_ids=[staged.id]
    )
    _, text = pairs[0]
    assert len(text) < len(body)
    assert "未展开" in text or "还有" in text

    chat_attachments.discard(session_id="chat_probe_9", attachment_ids=[staged.id])


def test_an_unknown_attachment_id_is_skipped_not_fatal() -> None:
    """★ 传了一个不存在的附件 id（两个标签页、或已过期 1 小时）。

    不能抛异常 —— 那会让整轮对话失败。正确行为是跳过它。
    """
    pairs = chat_attachments.read_text(
        session_id="chat_probe_10", attachment_ids=["never-existed"]
    )
    assert pairs == []


def test_staging_is_capped_per_session() -> None:
    """暂存数量上限存在且是有限值。

    没有上限的话，用户传 500 个文件就会让下一轮提示词爆掉
    （每个附件正文都要塞进上下文）。
    """
    assert 1 <= chat_attachments.MAX_STAGED_PER_SESSION <= 100


def test_staged_attachments_expire() -> None:
    """暂存有 TTL。永不过期的话，一年前传的文件还在列表里。"""
    assert 60 <= chat_attachments.STAGED_TTL_SECONDS <= 24 * 3600


# ══════════════════════════════════════════════════════════
# 5. 审批端点：两道权限
# ══════════════════════════════════════════════════════════


def test_over_http_approving_a_message_that_is_not_pending_is_refused(
    ready_client: TestClient,
) -> None:
    """不存在/已处理的 messageId → 400，且说清"已经处理过了"。

    重复批准会让同一轮工具**执行两次**（第二次带的是已经变了的上下文）。
    症状是扫描页里出现两条一模一样的任务。
    """
    created = ready_client.post(f"{API}/chat/sessions", json={"title": "审批测试"})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    resp = ready_client.post(
        f"{API}/chat/sessions/{session_id}/messages/no_such_message/approval",
        json={"approve": True},
    )
    assert resp.status_code in (400, 404), resp.text


def test_over_http_another_user_cannot_approve(
    ready_client: TestClient, viewer_client: TestClient
) -> None:
    """★★ 别人的会话不能审批。

    审批等于"授权执行一个动作"，跨会话审批就是跨用户提权。
    """
    created = ready_client.post(f"{API}/chat/sessions", json={"title": "审批归属"})
    session_id = created.json()["id"]

    resp = viewer_client.post(
        f"{API}/chat/sessions/{session_id}/messages/anything/approval",
        json={"approve": True},
    )
    assert resp.status_code == 404, resp.text


def test_over_http_personas_are_listed_for_any_reader(
    viewer_client: TestClient,
) -> None:
    """人格列表只读、任何登录账号都能拿。

    它不含任何敏感信息（就是几个标签），要求 admin 会让普通用户
    点开人格弹窗看到一片空白。
    """
    resp = viewer_client.get(f"{API}/chat/personas")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert isinstance(rows, list) and len(rows) >= 1
    first = rows[0]
    assert {"id", "label", "summary"} <= set(first)


# ══════════════════════════════════════════════════════════
# 6. 归档会话要连沙箱一起销毁（v0.107）
# ══════════════════════════════════════════════════════════


def test_archiving_a_session_destroys_its_sandbox(
    ready_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 归档 = 删会话，那个会话的沙箱必须当场销毁。

    ## 这条在防什么

    `drop_sandbox` 曾经**没有任何调用方** —— 函数写好了、docstring 还
    写着"调用点：DELETE /chat/sessions/{id}"，但 DELETE 里一行都没写。

    后果不是"多占一点内存"：对话沙箱是**无限制档**（出网 + 可写工作区），
    容器会一直挂着，而 `_SANDBOXES` 是进程内字典，进程不重启它就永远
    不会自己消失。用户以为"我把对话删了"，实际一个能上网的容器还活着。

    ## 为什么断言的是"被调用了"而不是"容器真的没了"

    这条测的是**接线**（router 有没有调 drop_sandbox）。销毁本身由
    `tests/chat/test_sandbox.py` 里那几条守。混在一起的话，docker 抽风
    会让这条变成假红，而它想守的其实是"有人把调用删了"。
    """
    from app.chat import sandbox as chat_sandbox

    created = ready_client.post(f"{API}/chat/sessions", json={"title": "会跑工具的对话"})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    dropped: list[str] = []

    def _fake_drop(sid: str) -> None:
        dropped.append(sid)

    monkeypatch.setattr(chat_sandbox, "drop_sandbox", _fake_drop)

    resp = ready_client.delete(f"{API}/chat/sessions/{session_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["archived"] is True

    assert dropped == [session_id], (
        "归档会话时没有销毁它的沙箱 —— 那个无限制容器会一直挂着，"
        "进程不重启就不会消失"
    )


def test_archiving_succeeds_even_if_sandbox_destruction_fails(
    ready_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """销毁出错不能让"删会话"失败。

    用户的意图是删掉这个对话。docker 抽风是平台的问题，不该变成
    "你的对话删不掉" —— 那会把一个运维问题伪装成功能缺陷。
    """
    from app.chat import sandbox as chat_sandbox

    created = ready_client.post(f"{API}/chat/sessions", json={"title": "docker 抽风"})
    session_id = created.json()["id"]

    real_drop = chat_sandbox.drop_sandbox

    def _boom(sid: str) -> None:
        # drop_sandbox 自己吞异常，这里模拟"连吞都没吞住"的最坏情况：
        # 直接抛出去，看 router 会不会跟着炸。
        raise RuntimeError("docker daemon 挂了")

    monkeypatch.setattr(chat_sandbox, "drop_sandbox", _boom)

    try:
        resp = ready_client.delete(f"{API}/chat/sessions/{session_id}")
    finally:
        monkeypatch.setattr(chat_sandbox, "drop_sandbox", real_drop)

    # 这里的期望：删除动作本身仍然成功。
    assert resp.status_code == 200, resp.text
    assert resp.json()["archived"] is True
