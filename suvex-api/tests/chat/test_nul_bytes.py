"""NUL 字节不能把整轮对话打成 500。

## 这批测试守的是一个实测过的 500（2026-10-27，Kali）

现象链完整到不需要猜：

1. 用户上传一个 2060 字节的 pcapng（`0a0d0d0a` + 零填充）；
2. 点发送；
3. 后端 500，`psycopg.DataError: PostgreSQL text fields cannot contain
   NUL (0x00) bytes`；
4. 前端只显示「处理失败」——用户完全看不出跟附件有关。

根因是**判定文本的方式**：`read_text` 用 try-decode 探"这是不是文本"，
而 `\\x00` 是合法 UTF-8（码位 U+0000）。于是整段二进制被当成文本存进
`ChatMessage.content`（`Text` 列），PostgreSQL 拒收。

## 为什么测试要分两层钉

只钉 `read_text` 不够：能落进 text 字段的还有**工具输出**与**推理段**。
模型跑一句 `cat /bin/ls` 就能产生 NUL —— 那条路径跟附件完全无关。
所以这批测试同时钉住：

- `attachments.read_text` 不再把含 NUL 的东西判成文本；
- `service._clip_body` / `_clip_title` / `_append` 这三处写入边界都会清洗。

## 为什么不用真 PostgreSQL

本机（Windows）跑的是 SQLite，它**接受** NUL —— 所以"写进去没报错"
在本地永远成立，测不出东西。真正的判据只能是"落库前字符串里还有没有
NUL"。这不是退让：`psycopg` 拒的是**发给它的字符串**，所以只要
`_append` 交给 ORM 的值里没有 NUL，Postgres 那条路就不可能失败。
"""

from __future__ import annotations

import pytest

from app.chat import attachments as chat_attachments
from app.chat import service as chat_service


#: 一段真 pcapng 的开头：魔数 + 版本 + 大段零填充。
#: 用真实字节而不是 `b"\x00\x00"`：真实的失败正是被这个形状触发的。
PCAPNG_BYTES = bytes.fromhex("0a0d0d0a") + b"\x1c\x00\x00\x00" + bytes(64)


class TestReadTextRejectsNul:
    """`read_text` 必须把含 NUL 的文件判成二进制。"""

    def test_pcapng_is_not_decoded_as_text(self, tmp_path, monkeypatch) -> None:
        """pcapng 的零填充不能被 UTF-8 解成文本。

        这是那个 500 的直接成因：`bytes(64)` 全是 `\\x00`，`decode("utf-8")`
        对它们**成功**，于是 `text` 非 None，整段进了 content 字段。
        """
        monkeypatch.setenv("SUPEX_CHAT_ATTACHMENTS", str(tmp_path))
        session_id = "chat_nul01"
        staged = chat_attachments.store(
            session_id=session_id,
            filename="evidence.pcapng",
            content=PCAPNG_BYTES,
            attachment_id="att01",
        )

        out = chat_attachments.read_text(
            session_id=session_id, attachment_ids=[staged.id]
        )

        assert len(out) == 1
        _name, text = out[0]
        assert "\x00" not in text, (
            "判断结果里不该出现 NUL —— 出现就说明它被当成文本读出来了，"
            "而这段字符串会一路走到 PostgreSQL 的 text 字段"
        )
        assert "二进制" in text, (
            "必须明确告诉模型这是二进制。含糊其辞会让它以为自己看到了内容，"
            "然后基于一堆零字节下结论"
        )

    def test_a_text_file_with_nul_is_still_treated_as_binary(
        self, tmp_path, monkeypatch
    ) -> None:
        """混杂 NUL 的"像文本"文件也要拦。

        真实样本：某些导出工具写出的 CSV/日志会在末尾带 `\\x00` 补位。
        它不是纯二进制，但只要有一个 NUL 就存不进 Postgres —— 判据必须是
        "有没有 NUL"，而不是"像不像文本"。
        """
        monkeypatch.setenv("SUPEX_CHAT_ATTACHMENTS", str(tmp_path))
        session_id = "chat_nul02"
        content = b"timestamp,event\n1700000000,login\n\x00\x00"
        staged = chat_attachments.store(
            session_id=session_id,
            filename="access.csv",
            content=content,
            attachment_id="att02",
        )

        out = chat_attachments.read_text(
            session_id=session_id, attachment_ids=[staged.id]
        )

        _name, text = out[0]
        assert "\x00" not in text
        assert "二进制" in text

    def test_ordinary_text_still_works(self, tmp_path, monkeypatch) -> None:
        """修 NUL 不能把普通文本也挡掉 —— 那是更严重的问题。"""
        monkeypatch.setenv("SUPEX_CHAT_ATTACHMENTS", str(tmp_path))
        session_id = "chat_nul03"
        staged = chat_attachments.store(
            session_id=session_id,
            filename="notes.md",
            content="这是一份普通笔记。\n第二行。".encode(),
            attachment_id="att03",
        )

        out = chat_attachments.read_text(
            session_id=session_id, attachment_ids=[staged.id]
        )

        _name, text = out[0]
        assert "普通笔记" in text, "普通文本必须照常读出来"
        assert "二进制" not in text


class TestWriteBoundaryScrubsNul:
    """写入边界必须清洗 NUL —— 不管内容是从哪个入口进来的。"""

    def test_clip_body_removes_nul(self) -> None:
        """工具输出含 NUL 时（`cat /bin/ls`）不能原样落库。"""
        dirty = "before\x00after"
        assert chat_service._clip_body(dirty) == "before after"

    def test_clip_body_removes_nul_in_truncated_branch(self) -> None:
        """截断分支也要清洗 —— 两处 return 各自独立，只修一处会留个缺口。"""
        from app.chat.models import MAX_MESSAGE_CHARS

        dirty = "\x00" * 10 + "x" * (MAX_MESSAGE_CHARS + 100)
        out = chat_service._clip_body(dirty)
        assert "\x00" not in out
        assert "已截断" in out, "截断标记不能丢"

    def test_clip_title_removes_nul(self) -> None:
        """标题也会落库（`ChatSession.title` 是 VARCHAR），同样存不了 NUL。"""
        assert "\x00" not in chat_service._clip_title("\x00会话\x00标题\x00")

    def test_scrub_json_handles_nested_structures(self) -> None:
        """`tool_args` 是 JSONB，嵌套结构里的 NUL 一样会让整轮 500。

        模型可能把一段含 NUL 的内容当作参数传（比如直接把二进制片段塞进
        `run_shell`），那时失败点是 JSONB 序列化，报
        `unsupported Unicode escape sequence`。
        """
        dirty = {
            "cmd": "cat /bin/ls\x00",
            "env": {"BIN\x00KEY": "val\x00ue"},
            "list": ["a\x00", {"deep": "\x00deep\x00"}],
            "count": 3,
            "flag": True,
            "none": None,
        }
        cleaned = chat_service._scrub_nul_json(dirty)

        rendered = repr(cleaned)
        assert "\x00" not in rendered, f"JSONB 里还有 NUL：{rendered}"
        # 非字符串类型必须原样保留 —— 清洗不该改变参数语义。
        assert cleaned["count"] == 3
        assert cleaned["flag"] is True
        assert cleaned["none"] is None
        assert cleaned["list"][1]["deep"] == " deep "

    def test_scrub_json_leaves_scalars_alone(self) -> None:
        assert chat_service._scrub_nul_json(None) is None
        assert chat_service._scrub_nul_json(42) == 42
        assert chat_service._scrub_nul_json("clean") == "clean"


class TestAppendNeverHandsNulToTheDatabase:
    """端到端的那一层：`_append` 交给 ORM 的值里不能有 NUL。

    ★ 这条的判据是**进 ORM 之前的字符串**，不是"数据库有没有报错"：
      本机 SQLite 接受 NUL，报错只会在 Kali 的 PostgreSQL 上出现。
      但既然 `psycopg` 拒的就是"发给它的字符串"，拦在这里就等价于拦住了
      那个 500。
    """

    def test_user_message_content_is_scrubbed(self, db_session) -> None:
        session = chat_service.create_session(db_session, created_by="u1")
        db_session.flush()

        message = chat_service.append_user_message(
            db_session,
            session=session,
            content="附件正文：\x00\x00零填充\x00",
        )

        assert "\x00" not in message.content
        assert "零填充" in message.content, "清洗不该把有用内容一起丢掉"

    def test_assistant_reasoning_is_scrubbed(self, db_session) -> None:
        """推理段不走 `_clip_body`，所以它需要单独清洗。

        漏了这条的话，模型只要在思考段里贴一段原始字节，同一条 500
        就会从另一个入口复现。
        """
        session = chat_service.create_session(db_session, created_by="u1")
        db_session.flush()

        message = chat_service.append_assistant_message(
            db_session,
            session=session,
            content="回答",
            reasoning="我看到字节是 \x00\x00 然后决定…",
        )

        assert message.reasoning is not None
        assert "\x00" not in message.reasoning
        assert "然后决定" in message.reasoning

    def test_tool_message_output_is_scrubbed(self, db_session) -> None:
        """工具输出是**最容易**带 NUL 的入口，而且它不需要用户做任何事。"""
        session = chat_service.create_session(db_session, created_by="u1")
        db_session.flush()

        message = chat_service.append_tool_message(
            db_session,
            session=session,
            tool_name="run_shell",
            tool_args={"command": "cat /bin/ls\x00"},
            content="ELF\x00\x02\x01\x01\x00输出",
        )

        assert "\x00" not in message.content
        assert message.tool_args is not None
        assert "\x00" not in repr(message.tool_args)


class TestTitlesNeverCarryNul:
    """标题落的是 PostgreSQL `VARCHAR`，与 text 字段同一条限制。

    ★ 这组是从「还有没有别的写入点」这个角度补出来的，而不是从"缺测试"。
      检查全部 `.title =` 与 `title=` 赋值后找到两处：`create_session`
      与 `rename_session`。后者更隐蔽 —— 见下面那条测试。
    """

    def test_create_session_scrubs_title(self, db_session) -> None:
        session = chat_service.create_session(
            db_session, created_by="u1", title="带\x00零\x00字节的标题"
        )
        assert "\x00" not in session.title
        assert "字节的标题" in session.title

    def test_rename_rejects_a_title_that_is_only_nul(self, db_session) -> None:
        """只由 NUL 组成的标题必须报「不能为空」，而不是 500。

        ★ 为什么这条值得单独钉：`" ".join(title.split())` 不会吃掉 `\\x00`
          （`"\\x00".isspace()` 是 False），所以这种标题能过原来的"非空"
          检查，然后在 `flush()` 时被 PostgreSQL 拒掉 —— 用户看到的是
          「处理失败」，而不是「标题不能为空」。
          清洗必须排在判空**之前**。
        """
        from app.core.errors import ApiError

        session = chat_service.create_session(db_session, created_by="u1")
        db_session.flush()

        with pytest.raises(ApiError):
            chat_service.rename_session(db_session, session=session, title="\x00\x00\x00")

    def test_rename_scrubs_mixed_title(self, db_session) -> None:
        session = chat_service.create_session(db_session, created_by="u1")
        db_session.flush()

        chat_service.rename_session(db_session, session=session, title="正常\x00标题")

        assert "\x00" not in session.title
        assert "正常" in session.title and "标题" in session.title
