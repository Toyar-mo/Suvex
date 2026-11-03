"""v1.56：读出口兜底清洗历史 `<system>…</system>` 刷屏。

写入口（engine）已把新消息里的 `<system>` 剥干净，但**修复前**落库的旧消息
仍带标签 —— 那次线上会话（851 条）里 146 条正文含 `<system>`。这些旧消息
走 `GET /chat/sessions/{id}` 时经 `ChatMessageOut.model_validate` 序列化，
必须在读出口再兜一层，否则历史会话回放仍刷屏（用户截图正是这一屏）。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.chat.schemas import ChatMessageOut, strip_system_wrappers


def _orm(**over: object) -> SimpleNamespace:
    base = dict(
        id="m1",
        seq=1,
        role="assistant",
        content="",
        reasoning=None,
        tool_name=None,
        tool_args=None,
        tool_failed=None,
        created_at="2026-11-03T00:00:00Z",
        surface="answer",
        approval_status=None,
        approval_decided_by=None,
        approval_decided_at=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


class TestStripSystemWrappers:
    def test_fragment_blocks_removed(self) -> None:
        text = "先看驱动。\n<system>ACTION.</system>\n<system>执行.</system>\n<system>先.</system>\n再反汇编。"
        out = strip_system_wrappers(text)
        assert "<system" not in out
        assert "ACTION." not in out
        assert "执行." not in out
        assert "先." not in out
        assert "先看驱动" in out
        assert "再反汇编" in out

    def test_real_action_kept_as_readable_text(self) -> None:
        text = '跑一下。<system>ACTION: {"tool":"run_shell","args":{"command":"id"}}</system>'
        out = strip_system_wrappers(text)
        assert "<system" not in out
        assert 'ACTION: {"tool":"run_shell","args":{"command":"id"}}' in out
        assert "跑一下" in out

    def test_idempotent(self) -> None:
        text = "人话。<system>先.</system>"
        once = strip_system_wrappers(text)
        twice = strip_system_wrappers(once)
        assert once == twice

    def test_plain_text_untouched(self) -> None:
        text = "这是一段普通正文，没有标签。"
        assert strip_system_wrappers(text) == text

    def test_stray_unclosed_tag_removed(self) -> None:
        assert "<system" not in strip_system_wrappers("残留 <system> 半个标签")


class TestChatMessageOutValidator:
    def test_model_validate_strips_system_from_orm(self) -> None:
        """走 ORM → ChatMessageOut 的路径（真实序列化），旧消息刷屏被清掉。"""
        orm = _orm(content="<system>ACTION.</system><system>执行.</system>真正的正文")
        out = ChatMessageOut.model_validate(orm)
        assert "<system" not in out.content
        assert "ACTION." not in out.content
        assert "真正的正文" in out.content

    def test_model_validate_keeps_plain_content(self) -> None:
        orm = _orm(content="没问题的一条历史正文")
        out = ChatMessageOut.model_validate(orm)
        assert out.content == "没问题的一条历史正文"

    def test_model_validate_unpacks_real_action(self) -> None:
        orm = _orm(
            content='<system>ACTION: {"tool":"run_shell","args":{"command":"ls"}}</system>'
        )
        out = ChatMessageOut.model_validate(orm)
        assert "<system" not in out.content
        assert 'ACTION: {"tool":"run_shell","args":{"command":"ls"}}' in out.content


class TestStripSystemFamilyV157:
    """v1.57：读出口扩到整族 `system*`（`_warning`/`_welcome`/`_call`）。"""

    def test_system_warning_removed(self) -> None:
        out = strip_system_wrappers(
            "收尾。\n<system_warning>你没写 ACTION，请补上。</system_warning>"
        )
        assert "<system_warning" not in out
        assert "你没写 ACTION" not in out
        assert "收尾" in out

    def test_system_welcome_removed(self) -> None:
        out = strip_system_wrappers("起。<system_welcome/>止。")
        assert "<system_welcome" not in out
        assert "起" in out and "止" in out

    def test_system_call_bare_json_kept_as_readable_action(self) -> None:
        out = strip_system_wrappers(
            '<system_call>\n{"tool": "run_shell", "args": {"command": "pwd"}}'
        )
        assert "<system_call" not in out
        assert 'ACTION: {"tool": "run_shell", "args": {"command": "pwd"}}' in out

    def test_system_call_invalid_json_removed(self) -> None:
        out = strip_system_wrappers('<system_call>{"tool": broken')
        assert "<system_call" not in out

    def test_validator_strips_family_from_orm(self) -> None:
        orm = _orm(
            content="正文。\n<system_warning>补 ACTION。</system_warning>"
            '<system_call>{"tool":"run_shell","args":{}}'
        )
        out = ChatMessageOut.model_validate(orm)
        assert "<system" not in out.content
        assert "正文" in out.content

    def test_plain_text_with_underscore_word_not_misread(self) -> None:
        """正文里出现 `system_x` 这类普通词（非标签）不受影响。"""
        text = "变量名叫 system_config，不是标签。"
        assert strip_system_wrappers(text) == text
