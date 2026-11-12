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
NEW_PERSONAS = (
    "goals",
    "planner",
    "mainagent",
    "worker",
    "worker.intranet",
    "auto",
    "pentest",
    "reporter",
    "retester",
    "verifier",
)

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "20261112_1200_j0e1f2a3b4c5_artex_roles.py"
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
    assert migration.ROLES == CHAT_PERSONAS, (
        "迁移 CHAT_PERSONAS_NEW 与 models.CHAT_PERSONAS 不一致：\n"
        f"  迁移多出：{sorted(set(migration.CHAT_PERSONAS_NEW) - set(CHAT_PERSONAS))}\n"
        f"  代码多出：{sorted(set(CHAT_PERSONAS) - set(migration.CHAT_PERSONAS_NEW))}"
    )



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


def test_default_persona_still_renders_nothing() -> None:
    """`default` 必须继续渲染成空串 —— 它代表"不加任何角色约束"。

    这是 `render_persona` 的既有契约，扩列表时最容易顺手弄坏：
    如果给 default 也加了 guidance，所有没选人格的对话都会突然变得"有立场"。
    """
    from app.chat.prompts import render_persona

    assert render_persona("default") == ""
    assert render_persona(None) == ""


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


def test_persona_output_language_follows_locale() -> None:
    """提示词必须跟随界面 locale，而不是写死中文。"""
    from app.chat.prompts import build_system_prompt, render_persona

    assert "English" in render_persona("planner", "en")
    assert "日本語" in render_persona("planner", "ja")
    assert "繁體中文" in render_persona("planner", "zh-TW")
    assert "始终使用English回答" in build_system_prompt(persona="pentest", locale="en")
