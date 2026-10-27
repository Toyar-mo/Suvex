"""「给模型看的材料」与「用户说的话」分开存（v0.112）。

## 这条需求不是审美

`engine.run_turn` 原来这么写：

    stored_content = content + _render_attachments(files, handed=handed)

于是界面上用户气泡里显示的是他自己**没打过**的一大段：

```
## 附件：`challenge.pcapng`
文本附件的正文已经贴在上面了；但被截断过的、或者你想 grep/awk 全
文的，直接读这些路径。二进制附件（pcapng/zip/ELF）在上面只有一句
「读不了」，真正的内容在这条路径上 —— 用 `run_shell` 分析：`file`
看类型、`xxd` 看字节、`binwalk` 拆包、`tcpdump -r` 读抓包、`strings`
捞可打印串。路径里的数字前缀是附件 id，用完整路径，别只按文件名
`ls`。
```

里面有附件 id、容器内路径、以及"用 binwalk / tcpdump 分析"这类**我们
写给模型的指令**。用户看到会合理地以为"这是我打的吗？"

`app/chat/engine.py` 里为同一个形状早写过判例（`_narration_before_action`
的 docstring）：

> 内部协议出现在用户视野里，会让人无法判断这句话是模型说的还是系统拼的
> —— 复核证据时这条是致命的。

## 这个文件在守什么

三条不变量，缺一条这个修复就等于没做：

1. **用户可见的 `content` 里没有注入材料**（否则界面照旧显示）；
2. **模型上下文里那份材料一字不少**（否则我"修"掉了模型的输入 —— 附件
   分析立刻退化，而这是 v0.109 刚验证通过的能力）；
3. **两者拼起来与改动前逐字节一致**（否则"拆开"就变成了"顺手改了格式"，
   模型会看到不一样的东西，而我无法归因后续任何变化）。
"""

from __future__ import annotations

import importlib.util
import pathlib

from sqlalchemy.orm import Session

from app.chat import engine
from app.chat import service as chat_service

# ══════════════════════════════════════════════════════════
# 1. 材料不落进用户正文
# ══════════════════════════════════════════════════════════


def test_model_extra_is_stored_in_its_own_column(db_session: Session) -> None:
    """`model_extra` 落到自己的列，`content` 只有用户原话。"""
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()

    message = chat_service.append_user_message(
        db_session,
        session=session,
        content="看看这个包",
        model_extra="\n\n---\n## 附件：`a.pcapng`\n读不了\n---\n",
    )
    db_session.commit()

    assert message.content == "看看这个包"
    assert message.model_extra is not None
    assert "## 附件：`a.pcapng`" in message.model_extra
    # 反过来也要断言：材料绝不能出现在正文里。
    assert "附件" not in message.content


def test_no_attachments_means_null_not_empty_string(db_session: Session) -> None:
    """不带附件时 `model_extra` 是 `NULL`，不是 `''`。

    ★ 两者语义不同（见 `ChatMessage.model_extra` 的 docstring）：NULL =
      这条消息没有注入材料。写成 `''` 会让"有没有材料"这个判断变成
      "字符串空不空"，而 `''` 在 `or ""` 的口径下与 NULL 无法区分 ——
      将来若有人按 `is NULL` 查"哪些消息带过附件"，`''` 会漏掉。
    """
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()

    message = chat_service.append_user_message(
        db_session, session=session, content="你好"
    )
    db_session.commit()

    assert message.model_extra is None


def test_model_extra_is_nul_scrubbed_like_every_other_text_column(
    db_session: Session,
) -> None:
    """`model_extra` 也必须过 NUL 清洗。

    ★ 这是**最容易**带 NUL 的新入口：它的内容就是附件正文，而附件正文
      可以是任意字节。`service` 模块 docstring 里的那句职责 ——「`_append`
      必须是"写进 Postgres 的都能写进去"的唯一保证」—— 对新增的列一个字
      也不能少。漏掉的话，用户上传一个含零填充的二进制附件，`content` 是
      干净的（用户那句话），而 `model_extra` 里全是 `\\x00` →
      同一条 500 从新入口复现，且表现与 v0.110 修掉的那个一模一样。

    ★ 这条测试是变异验证逼出来的：把 `model_extra` 的 `_clip_body(...)`
      换成直接透传，`tests/chat/` 全量 177 条**一条都没红**。SQLite 接受
      NUL 所以行为上也看不出来 —— 缺口只在 Kali 的 PostgreSQL 上才暴露，
      正是最难自查的那类。所以判据取"进 ORM 之前的字符串"（与
      `test_nul_bytes.py` 的 `TestAppendNeverHandsNulToTheDatabase` 同一口径）。
    """
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()

    message = chat_service.append_user_message(
        db_session,
        session=session,
        content="看看这个",
        model_extra="\x00\x00## 附件：`a.bin`\x00file 说它是 data\x00",
    )
    db_session.commit()

    assert message.model_extra is not None
    assert "\x00" not in message.model_extra, (
        "model_extra 没清 NUL —— PostgreSQL 会拒收，用户看到 500「处理失败」"
    )
    assert "## 附件：`a.bin`" in message.model_extra, "清洗不该丢掉有用内容"


def test_session_title_is_taken_from_content_not_from_extra(
    db_session: Session,
) -> None:
    """会话标题取用户原话。

    ★ 拼进去的话侧栏会变成一排「## 附件：access.log」—— 每个带附件的
      会话都长得一样，用户无法在侧栏区分它们。
    """
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()

    chat_service.append_user_message(
        db_session,
        session=session,
        content="帮我分析这个日志",
        model_extra="\n\n## 附件：`access.log`\nGET / HTTP/1.1\n",
    )
    db_session.commit()

    db_session.refresh(session)
    assert session.title == "帮我分析这个日志"


# ══════════════════════════════════════════════════════════
# 2. 模型上下文里材料一字不少
# ══════════════════════════════════════════════════════════


def test_history_puts_model_extra_back_after_the_user_text(
    db_session: Session,
) -> None:
    """渲染成上下文时拼回去，顺序是「先问题、后材料」。"""
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()
    chat_service.append_user_message(
        db_session,
        session=session,
        content="看看这个包",
        model_extra="\n\n---\n## 附件：`a.pcapng`\n-- 二进制 --\n",
    )
    db_session.commit()

    rows = chat_service.list_messages(db_session, session=session)
    rendered = engine._render_history(rows)

    assert len(rendered) == 1
    payload = rendered[0]["content"]
    assert payload.startswith("看看这个包"), "用户原话必须在最前面"
    assert "## 附件：`a.pcapng`" in payload
    assert payload == "看看这个包" + "\n\n---\n## 附件：`a.pcapng`\n-- 二进制 --\n"


def test_history_without_extra_is_byte_identical_to_before(
    db_session: Session,
) -> None:
    """没有材料时上下文与改动前一模一样（不能凭空多出空白或换行）。"""
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()
    chat_service.append_user_message(db_session, session=session, content="你好")
    db_session.commit()

    rows = chat_service.list_messages(db_session, session=session)
    rendered = engine._render_history(rows)

    assert rendered == [{"role": "user", "content": "你好"}]


def test_history_survives_a_row_that_predates_the_column() -> None:
    """**存量行**（迁移之前落的）必须安全。

    迁移加了可空列，老行的 `model_extra` 是 `NULL`。而 `_render_history`
    的入参是 `list[Any]`（测试里能传任何像行的东西），所以这里用一个
    没有该属性的哑对象 —— 它等价于"某天有人在别处构造了行"。
    没有 `getattr` 兜底的话，这里会 `AttributeError` 而不是正常渲染。
    """

    class Row:
        role = "user"
        content = "老消息"
        tool_name = None
        tool_args = None
        tool_failed = None

    assert engine._render_history([Row()]) == [{"role": "user", "content": "老消息"}]


# ══════════════════════════════════════════════════════════
# 3. 端到端：`run_turn` 的写入路径
# ══════════════════════════════════════════════════════════


def test_attachment_material_never_lands_in_the_stored_user_content() -> None:
    """守住**写侧**：`engine.run_turn` 不能再自行拼接。

    ★ 不用跑整个 `run_turn`（它要 DB、要 LLM、要沙箱）。这里检查源码里
      那个赋值语句已经不存在 —— 这是本项目对"接线还在不在"的既有做法
      （见 `tests/kernel/test_termination.py` 用 `inspect.getsource`）。
      纯粹的行为测试覆盖不到这一条：把 `content` 与 `model_extra` 都传对
      之后，谁也无法从结果反推"它是不是又拼了一次"。

    ★ 注释行必须排除掉。第一版没排除，测试当场红了 —— 因为解释"以前是
      `stored_content = content + _render_attachments(...)`"这句注释里就
      含那个名字。断言的是**代码**，不是文字；不排注释的话，将来谁在注释
      里提一句这个变量名就会假红。
    """
    import inspect

    lines = [
        line
        for line in inspect.getsource(engine.run_turn).splitlines()
        if not line.lstrip().startswith("#")
    ]
    code = "\n".join(lines)

    assert "stored_content" not in code, (
        "run_turn 里又出现 stored_content 了 —— 附件材料重新拼进了用户正文"
    )
    assert "model_extra=attachments_extra or None" in code, (
        "run_turn 没有把附件材料放进 model_extra"
    )


def test_render_attachments_output_is_what_goes_into_extra() -> None:
    """`_render_attachments` 的产物就是塞进 `model_extra` 的东西。

    钉住格式：它的开头必须有一个换行分隔（因为它被**接在**用户正文之后，
    而不是替换正文）。写成 `f"{name}..."` 而不带前导换行的话，模型看到的
    会变成「看看这个包## 附件：`a.pcapng`」—— 粘在一行上。
    """
    rendered = engine._render_attachments(
        [("a.pcapng", "-- 二进制 --")],
        handed=[("a.pcapng", "/work/attachments/7f3a-a.pcapng")],
    )

    assert rendered.startswith("\n"), "必须以换行开头，否则会粘在用户正文后面"
    assert "## 附件：`a.pcapng`" in rendered
    assert "/work/attachments/7f3a-a.pcapng" in rendered


def test_render_attachments_is_empty_without_attachments() -> None:
    """没有附件时返回空串 —— 调用方据此写 `or None`（存 NULL 而不是 `''`）。"""
    assert engine._render_attachments([]) == ""


# ══════════════════════════════════════════════════════════
# 4. 迁移必须真的加这一列
# ══════════════════════════════════════════════════════════


MIGRATION_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "20261027_0900_b71c4d9e83a5_chat_message_model_extra.py"
)


def _load_migration():
    """把迁移模块当普通模块导进来（不执行 upgrade/downgrade）。"""
    assert MIGRATION_PATH.exists(), f"找不到迁移文件 {MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location("model_extra_mig", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_adds_the_column_the_model_declares() -> None:
    """模型声明了 `model_extra`，迁移里就必须有 `add_column`。

    ★ 这是本项目反复踩的那一类：`__APP_VERSION__` 声明了但零处使用、
      `default_limitation` 读取了但从不进提示词。这次的形式是「ORM 有列、
      库里没列」—— 本机测试用 SQLite 由 `create_all` 建表，**永远看不出**
      这个缺口；到 Kali 上跑 `alembic upgrade head` 之后，第一条带附件的
      消息就会 `UndefinedColumn: column chat_messages.model_extra does not
      exist`，而报错读起来像 ORM 写错了。

    ★ 判据取**迁移源码**而不是连库跑：单测连不上生产库，而且
      `alembic upgrade` 需要真 PostgreSQL。静态契约由这个测试守，
      运行时的正确性由实机验收覆盖（与 `test_deploy_unit_migrations.py`
      同一套口径）。
    """
    migration = _load_migration()
    src = MIGRATION_PATH.read_text(encoding="utf-8")

    assert "add_column" in src, "迁移没有 add_column —— 库里的表不会有这一列"
    assert "model_extra" in src, "迁移没提 model_extra"
    assert migration.TABLE == "chat_messages"
    assert migration.COLUMN == "model_extra"


def test_migration_hangs_off_the_previous_head() -> None:
    """必须挂在 `f3a1c8d52e07` 之后，否则 alembic 有两个 head。

    ★ 两个 head 时 `alembic upgrade head` 直接报错退出 —— 而 systemd unit
      用的是 `alembic upgrade head && uvicorn`，`&&` 让它**不起服务**。
      看起来是"部署挂了"，实际原因是一行 `down_revision` 写错。
    """
    migration = _load_migration()
    assert migration.down_revision == "f3a1c8d52e07"
    assert migration.revision != migration.down_revision


def test_this_migration_is_the_only_head() -> None:
    """全仓扫一遍：除了本迁移，没有别的 head。

    ★ 单个迁移改对了但**别人**后来也挂错 `down_revision`，一样是两个 head。
      所以这条要扫全目录，而不是只看本文件。
    """
    import re

    versions = MIGRATION_PATH.parent
    revs: dict[str, str] = {}
    downs: set[str] = set()
    pattern_rev = re.compile(
        r"^revision(?:\s*:\s*[^=\n]+)?\s*=\s*[\"']([0-9a-zA-Z_]+)[\"']", re.M
    )
    pattern_down = re.compile(
        r"^down_revision(?:\s*:\s*[^=\n]+)?\s*=\s*(?:[\"']([0-9a-zA-Z_]+)[\"']|None)",
        re.M,
    )
    for path in sorted(versions.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        found_rev = pattern_rev.search(text)
        if not found_rev:
            continue
        revs[found_rev.group(1)] = path.name
        found_down = pattern_down.search(text)
        if found_down and found_down.group(1):
            downs.add(found_down.group(1))

    heads = sorted(set(revs) - downs)
    assert heads == ["b71c4d9e83a5"], (
        "alembic 不止一个 head —— `alembic upgrade head` 会直接失败，"
        f"systemd 的 `&& uvicorn` 于是起不了服务。当前 heads：{heads}"
    )


# ══════════════════════════════════════════════════════════
# 5. 材料不上接口
# ══════════════════════════════════════════════════════════


def test_model_extra_is_not_exposed_on_the_wire(db_session: Session) -> None:
    """`model_extra` 不进 API 响应。

    ★ 两个理由，都不轻：

      1. **界面上它不该出现**。`ChatPage.tsx` 的用户气泡只渲染 `content`，
         所以今天就算漏出去也不会显示 —— 但那是靠前端偶然没用它。真正的
         保证应该在这一层：类型里没有这个字段，写渲染代码的人就无从用到。
      2. **它是附件正文的全部内容**，最多 6 万字。每条消息都带一遍，会让
         `GET /chat/sessions/{id}` 的响应体随附件线性膨胀，而界面一个字节
         都不需要它。

    ★ 判据取序列化结果（`to_wire`，与 router 用的是同一段代码），而不是
      "schema 里有没有声明" —— 后者在有人改用宽松模型时不会红。
    """
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()
    chat_service.append_user_message(
        db_session,
        session=session,
        content="看看这个",
        model_extra="## 附件：`secret-probe.pcapng`\nSENTINEL-EXTRA-BODY",
    )
    db_session.commit()

    rows = chat_service.list_messages(db_session, session=session)
    wire = chat_service.to_wire(rows)

    assert len(wire) == 1
    assert wire[0]["content"] == "看看这个"
    rendered = repr(wire[0])
    assert "modelExtra" not in rendered, "model_extra 漏到接口上了"
    assert "model_extra" not in rendered, "model_extra 漏到接口上了"
    assert "SENTINEL-EXTRA-BODY" not in rendered, (
        "附件正文泄进了 API 响应 —— 界面不需要它，却要付带宽"
    )
