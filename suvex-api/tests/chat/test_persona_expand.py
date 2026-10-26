"""人格（persona）扩容的三处一致性与迁移正确性（v0.99）。

## 为什么这个模块值得单独一组测试

人格 id 同时存在于**三个地方**，而它们不一致时的坏法各不相同：

| 位置 | 少了一个 id 的后果 |
| --- | --- |
| `app/chat/models.py::CHAT_PERSONAS` | 数据库 CHECK 拒绝写入 —— 用户点保存直接报错（还算明显） |
| `app/chat/prompts.py::CHAT_PERSONAS_SPEC` | 能存进库，但 `PERSONA_BY_ID` 查不到 → **静默退回默认人格**，界面上毫无异常 |
| `alembic/versions/..._persona_expand.py` | 与上一条相同（迁移建的 CHECK 才是真正生效的那条） |

中间那一行是**最坏的一种**：用户明明选了「CTF 解题」，模型却按通用助手回答，
而界面上那个人格是选中的。没有任何日志、没有任何报错。

所以这里不测"能不能存"（那是 API 测试的事），测的是**三处是否逐字一致**，
以及迁移是否真的把约束扩了。

## 反面同样重要

只测"新值能写进去"是测不出"旧值还能写"的 —— 而后者一旦坏掉，
**所有历史会话**都会变成不可保存的行。所以两个方向都钉。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.chat.models import CHAT_PERSONAS
from app.chat.prompts import CHAT_PERSONAS_SPEC, PERSONA_BY_ID

#: v0.99 新加的四个方向。
NEW_PERSONAS = ("api", "cloud", "ctf", "aillm")

#: 扩容前就有的七个。它们必须继续可用（库里已有会话可能选着它们）。
LEGACY_PERSONAS = ("default", "recon", "pentest", "binary", "postex", "container", "forensics")

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "20261026_1700_f3a1c8d52e07_persona_expand.py"
)


@pytest.fixture(scope="module")
def migration():
    """把迁移模块当普通模块导进来（不执行 upgrade/downgrade）。"""
    assert MIGRATION_PATH.exists(), f"找不到迁移文件 {MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location("persona_expand_mig", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ══════════════════════════════════════════════════════════
# 1. 三处一致
# ══════════════════════════════════════════════════════════


def test_models_and_prompts_agree() -> None:
    """`models.CHAT_PERSONAS` 与 `prompts.CHAT_PERSONAS_SPEC` 必须同一组、同顺序。

    顺序也重要：它是界面上卡片/下拉的顺序。
    """
    assert tuple(p.id for p in CHAT_PERSONAS_SPEC) == CHAT_PERSONAS, (
        "models 与 prompts 的人格列表不一致 —— "
        "不一致时能存进库但解析不到，会静默退回默认人格"
    )


def test_migration_list_matches_code(migration) -> None:
    """迁移里的全集必须与代码里的 `CHAT_PERSONAS` 逐字一致。

    ## 为什么这条不能省

    迁移建的 CHECK 才是**数据库里真正生效**的那一条。迁移少了某个 id，
    代码能通过 pydantic、能通过 `PERSONA_BY_ID`，却在 `INSERT` 时被库拒；
    迁移多了某个 id，写入成功但引擎解析不到 → 静默退默认。
    """
    assert migration.CHAT_PERSONAS_NEW == CHAT_PERSONAS, (
        "迁移 CHAT_PERSONAS_NEW 与 models.CHAT_PERSONAS 不一致：\n"
        f"  迁移多出：{sorted(set(migration.CHAT_PERSONAS_NEW) - set(CHAT_PERSONAS))}\n"
        f"  代码多出：{sorted(set(CHAT_PERSONAS) - set(migration.CHAT_PERSONAS_NEW))}"
    )


def test_migration_old_list_is_exactly_the_legacy_set(migration) -> None:
    """`downgrade` 用的旧全集必须是扩容前那七个。

    多一个会让回滚建出一个**比原来更宽**的约束（回滚没真回滚）；
    少一个会让回滚建出**比原来更窄**的约束，于是回滚之后
    某些原本合法的历史会话变得不可保存。
    """
    assert migration.CHAT_PERSONAS_OLD == LEGACY_PERSONAS


# ══════════════════════════════════════════════════════════
# 2. 四个新人格本身
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize("persona_id", NEW_PERSONAS)
def test_new_personas_are_resolvable(persona_id: str) -> None:
    """每个新 id 都要能在 `PERSONA_BY_ID` 里查到，且有非空 guidance。

    ★ guidance 非空是关键：空 guidance 的 `render_persona` 会返回空串
    （见那个函数的实现），也就是"人格选了但没生效"——
    与"查不到"是同一种后果，只是原因不同。
    """
    assert persona_id in PERSONA_BY_ID, f"{persona_id} 解析不到"
    persona = PERSONA_BY_ID[persona_id]
    assert persona.guidance.strip(), f"{persona_id} 的 guidance 是空的 —— 选了等于没选"
    assert persona.label.strip()
    assert persona.summary.strip()


@pytest.mark.parametrize("persona_id", NEW_PERSONAS)
def test_new_persona_guidance_reaches_the_prompt(persona_id: str) -> None:
    """`render_persona` 拼出来的提示词里要有那个人格的正文。

    这是"存了但没进提示词"那一类失效的守卫 —— 与 v0.98 配方的洞同形状。
    """
    from app.chat.prompts import render_persona

    rendered = render_persona(persona_id)
    assert rendered.strip(), f"{persona_id} 渲染出来是空的"
    assert PERSONA_BY_ID[persona_id].label in rendered, "提示词里没有人格名"
    # 正文首句要真的出现（不是只有标题）
    first_line = PERSONA_BY_ID[persona_id].guidance.strip().splitlines()[0][:20]
    assert first_line in rendered, (
        f"{persona_id} 的正文没进提示词（只渲染了标题？）：{first_line!r}"
    )


def test_legacy_personas_still_resolvable() -> None:
    """★ 原来的七个必须继续可用。

    库里已经有会话选了它们。丢一个的后果不是"少一个选项"，
    而是那些会话**每次保存都失败**，或者渲染成空人格。
    """
    for persona_id in LEGACY_PERSONAS:
        assert persona_id in PERSONA_BY_ID, f"历史人格 {persona_id} 解析不到了"


def test_default_persona_still_renders_nothing() -> None:
    """`default` 必须继续渲染成空串 —— 它代表"不加任何角色约束"。

    这是 `render_persona` 的既有契约，扩列表时最容易顺手弄坏：
    如果给 default 也加了 guidance，所有没选人格的对话都会突然变得"有立场"。
    """
    from app.chat.prompts import render_persona

    assert render_persona("default") == ""
    assert render_persona(None) == ""


def test_default_persona_guidance_must_stay_empty() -> None:
    """★ `default` 的 guidance 必须为空串 —— 因为 `render_persona` **不读它**。

    ## 这条为什么必要（变异验证抓出来的）

    第一版只断言 `render_persona("default") == ""`。而那个函数的第一行是
    `if not persona_id or persona_id == DEFAULT_PERSONA_ID: return ""` ——
    它在碰 guidance **之前**就返回了。

    所以：给 `default` 写一段 guidance，`render_persona` 照样返回空串、
    测试照样绿，而那截文字**永远不会到达模型**。写它的人会以为
    「默认人格现在也有立场了」，实际什么都没发生。

    这是本项目反复抓的那类形状（配了/写了但静默失效）在人格模块里的一个实例。
    修法有两条：让 `render_persona` 读它，或者禁止有人写它。
    这里选后者并钉住 —— 因为「默认不加任何约束」是一个**设计决定**，
    不是待实现的功能。真要给默认人格加立场，应该新增一个人格，而不是
    偷偷改 `default`（那会让所有历史会话的行为一起变）。
    """
    default_persona = PERSONA_BY_ID["default"]
    assert default_persona.guidance == "", (
        f"`default` 人格被写了 guidance，但 `render_persona` 在默认分支"
        f"直接返回空串、不读它 —— 那段文字永远不会进提示词，"
        f"写的人却会以为生效了。内容：{default_persona.guidance[:80]!r}"
    )


def test_persona_labels_are_unique() -> None:
    """人格名不能重复 —— 下拉里两行一样的字，用户没法选。"""
    labels = [p.label for p in CHAT_PERSONAS_SPEC]
    assert len(labels) == len(set(labels)), f"人格名重复：{labels}"


def test_persona_ids_have_no_accidental_duplicates_in_the_list() -> None:
    """`CHAT_PERSONAS` 里不能有重复 id。

    重复不会报错（tuple 允许重复），但 CHECK 表达式会拼出
    `IN ('a','a','b')`，而 `PERSONA_BY_ID` 的字典会静默少一项 ——
    与"少一个人格"同一种后果，且更难看懂。
    """
    assert len(CHAT_PERSONAS) == len(set(CHAT_PERSONAS)), (
        f"CHAT_PERSONAS 里有重复：{CHAT_PERSONAS}"
    )


def test_every_persona_id_is_in_the_prompt_spec() -> None:
    """`CHAT_PERSONAS` 里每一个 id 都必须在 spec 里**有对应条目**。

    这是"能存进库但解析不到 → 静默退默认"那条失效路径的直接守卫。
    与 `test_models_and_prompts_agree` 的区别：那条比的是**顺序敏感的全等**，
    这条比的是**覆盖关系**，报错信息更能指向"缺了哪一个"。
    """
    spec_ids = {p.id for p in CHAT_PERSONAS_SPEC}
    missing = [pid for pid in CHAT_PERSONAS if pid not in spec_ids]
    assert not missing, (
        f"这些 id 在 CHAT_PERSONAS 里但 spec 里没有：{missing}。"
        " 它们能被写进库，但 PERSONA_BY_ID 查不到 → 引擎静默退回默认人格，"
        " 界面上完全看不出异常"
    )


# ══════════════════════════════════════════════════════════
# 3. 迁移本身
# ══════════════════════════════════════════════════════════


def test_migration_check_sql_allows_null(migration) -> None:
    """CHECK 表达式必须放行 NULL（NULL = 没选过，不是非法值）。"""
    sql = migration._check_sql(migration.CHAT_PERSONAS_NEW)
    assert "persona IS NULL" in sql
    for persona_id in NEW_PERSONAS:
        assert repr(persona_id) in sql, f"CHECK 里没有 {persona_id}"


def test_migration_chains_onto_the_previous_head(migration) -> None:
    """新迁移必须挂在 `e8f3c2a71b54` 之后，否则 alembic 有两个 head。"""
    assert migration.down_revision == "e8f3c2a71b54"
    assert migration.revision != migration.down_revision


def test_migration_does_not_try_to_update_existing_rows(migration) -> None:
    """★ `downgrade` 不能偷偷把用新人格的会话改回 default。

    那会让用户看到"我选的人格自己变了"，而且没有任何解释。
    宁可让回滚失败（撞 CHECK），让人自己决定那些会话怎么办。

    ## ★ 为什么用 AST 而不是搜源码文本

    第一版就是搜文本（`"UPDATE" not in src.upper()`），它当场误报：
    `downgrade` 的 docstring 里写着「**不要**在失败前偷偷 UPDATE 那些行」——
    那句说明本身含有 UPDATE 这个词。也就是说，一条**讲清楚"不要这么做"**
    的注释会让断言变红，而真正写了 `op.execute("UPDATE ...")` 时它反而不一定抓得住
    （可以拆成 f-string、可以走变量）。

    搜文本测的是"文件里有没有这五个字母"，不是"有没有执行这个动作"。
    AST 看的是真的调用了什么。
    """
    import ast
    import inspect
    import textwrap

    src = inspect.getsource(migration.downgrade)
    tree = ast.parse(textwrap.dedent(src))
    func_def = tree.body[0]
    assert isinstance(func_def, ast.FunctionDef | ast.AsyncFunctionDef)

    # 函数体第一条语句若是字符串常量，那是 **docstring**，不是 SQL。
    # （第一版没排除它，于是"不要偷偷 UPDATE"这句说明把断言弄红了 ——
    #   讲清楚规矩的注释反而被判违规。）
    body = func_def.body
    docstring_node = None
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            docstring_node = body[0].value
    real_body = body[1:] if docstring_node is not None else body

    # 禁止的调用：op.execute(...) / connection.execute(...) / sa.text("UPDATE...")
    forbidden_calls = {"execute", "text"}
    for stmt in real_body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                func = node.func
                name = ""
                if isinstance(func, ast.Attribute):
                    name = func.attr
                elif isinstance(func, ast.Name):
                    name = func.id
                assert name not in forbidden_calls, (
                    f"downgrade 调用了 `{name}` —— 见 docstring："
                    "不该静默改写用户选的人格，回滚失败是对的"
                )

    # 也不允许出现改写数据的 SQL 字面量（已排除 docstring）
    for stmt in real_body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                assert "UPDATE " not in upper and "DELETE " not in upper, (
                    f"downgrade 里有改写数据的 SQL 字面量：{node.value[:80]!r}"
                )
