"""对话侧黑板工具（v1.88）：`record_coverage` / `record_fact`。

★ 这两个工具的验收点不是"能写库"，而是评测里被点名的三件事：

1. 提示词要求过的工具**真的存在** —— `CHAT_TOOLS` 与 `_TOOL_IMPLS` 双向对齐
   由 `test_chat.py::test_every_declared_tool_has_an_implementation_and_vice_versa`
   保证，这里补的是"参数形状能跑通"；
2. 记录按 `target` 归档 —— 对话写的是 `scan_id=None` 的行，而
   `uq_scan_facts_scan_key` 对 NULL 不生效；不按目标收窄覆盖，
   A 目标的 `endpoint/login` 会被 B 目标的同名 key 覆盖掉；
3. `已排除：…` 必须落 `ruled-out` 类，其余落 `surface` 类 ——
   `assets.service.coverage_for_target` 的分组全靠这个。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chat import engine
from app.chat import service as chat_service
from app.facts.models import ScanFact
from tests.test_chat import ADMIN


def _ctx(db: Session):  # noqa: ANN202 - 测试内部小工具
    session = chat_service.create_session(db, created_by="user_admin")
    db.commit()
    return engine._ToolContext(db=db, session=session, identity=ADMIN)


def _rows(db: Session) -> list[ScanFact]:
    return list(db.execute(select(ScanFact)).scalars())


async def test_record_coverage_writes_a_surface_row(db_session: Session) -> None:
    """「成立」落 `surface`；目标按 `normalize_target` 归一后归档。"""
    ctx = _ctx(db_session)
    out, _failed = await engine._execute_tool(
        ctx,
        "record_coverage",
        {
            "target": "HTTP://Example.COM:8080/",
            "surface": "POST /api/v1/login (form)",
            "risk": "账号枚举 + 口令爆破",
            "outcome": "成立：服务端按用户名返回不同的错误文案",
        },
    )

    assert not out.startswith("失败"), out
    rows = _rows(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.category == "surface"
    assert row.fact_key.startswith("surface/post-api-v1-login-form")
    assert row.target == "example.com:8080"
    assert row.recorded_by.startswith("chat:")
    assert row.summary.startswith("成立：")
    assert row.detail == "风险：账号枚举 + 口令爆破"


async def test_record_coverage_ruled_out_goes_to_its_own_category(db_session: Session) -> None:
    """「已排除」必须落 `ruled-out` —— 否则资产页会把它当成测过的面。"""
    ctx = _ctx(db_session)
    out, _failed = await engine._execute_tool(
        ctx,
        "record_coverage",
        {
            "target": "10.0.0.5:8080",
            "surface": "GET /api/v1/health",
            "outcome": "已排除：只回固定 JSON，无输入点",
        },
    )

    assert not out.startswith("失败"), out
    row = _rows(db_session)[0]
    assert row.category == "ruled-out"
    assert row.fact_key.startswith("ruled-out/")


async def test_same_fact_key_on_two_targets_stays_two_rows(db_session: Session) -> None:
    """★ 回归：`scan_id` 为 NULL 时唯一约束不生效，覆盖必须再按目标收窄。

    没有这一条，给第二个目标记同名事实会把第一个目标那条改写掉 ——
    表现是"某个目标的覆盖度凭空消失"，而不是报错。
    """
    ctx = _ctx(db_session)
    for target in ("10.0.0.5:8080", "10.0.0.6:8080"):
        out, _failed = await engine._execute_tool(
            ctx,
            "record_fact",
            {
                "target": target,
                "fact_key": "endpoint/login-requires-bearer",
                "summary": "登录接口要 Bearer，不接受 Cookie",
            },
        )
        assert not out.startswith("失败"), out

    rows = _rows(db_session)
    assert len(rows) == 2
    assert {row.target for row in rows} == {"10.0.0.5:8080", "10.0.0.6:8080"}


async def test_record_fact_same_target_overwrites_and_bumps_version(db_session: Session) -> None:
    """同一目标同一个 key 是**覆盖**（黑板要最新认知），版本号 +1。"""
    ctx = _ctx(db_session)
    args = {
        "target": "10.0.0.5:8080",
        "fact_key": "credential/admin-session",
        "summary": "管理员会话可读 /settings",
    }
    await engine._execute_tool(ctx, "record_fact", args)
    out, _failed = await engine._execute_tool(
        ctx, "record_fact", {**args, "summary": "管理员会话已过期", "confidence": "deprecated"}
    )

    assert "第 2 版" in out
    rows = _rows(db_session)
    assert len(rows) == 1
    assert rows[0].version == 2
    assert rows[0].confidence == "deprecated"


async def test_record_fact_rejects_a_key_without_category(db_session: Session) -> None:
    """归不了的 key 要**报错回模型**，不是写一条分类错误但看起来合法的行。"""
    ctx = _ctx(db_session)
    out, _failed = await engine._execute_tool(
        ctx,
        "record_fact",
        {"target": "10.0.0.5:8080", "fact_key": "login-requires-bearer", "summary": "x"},
    )

    assert out.startswith("失败")
    assert "category/slug" in out
    assert _rows(db_session) == []


async def test_record_fact_requires_target(db_session: Session) -> None:
    """没有目标的事实继承不到 —— 宁可不写。"""
    ctx = _ctx(db_session)
    out, _failed = await engine._execute_tool(
        ctx,
        "record_fact",
        {"fact_key": "note/no-target", "summary": "x"},
    )

    assert out.startswith("失败")
    assert "target" in out
    assert _rows(db_session) == []


async def test_list_facts_reads_back_only_its_own_target(db_session: Session) -> None:
    """★ 写和读必须成对：记完要能在同一会话里读回来，且不串目标。"""
    ctx = _ctx(db_session)
    for target, summary in (
        ("10.0.0.5:8080", "管理员会话可读 /settings"),
        ("10.0.0.6:8080", "另一个目标上的同名事实"),
    ):
        await engine._execute_tool(
            ctx,
            "record_fact",
            {
                "target": target,
                "fact_key": "credential/admin-session",
                "summary": summary,
            },
        )

    out, failed = await engine._execute_tool(ctx, "list_facts", {"target": "10.0.0.5:8080"})

    assert not failed, out
    assert "管理员会话可读 /settings" in out
    assert "另一个目标上的同名事实" not in out
    assert "record_fact" not in out or "credential/admin-session" in out


async def test_list_facts_on_an_empty_target_says_so(db_session: Session) -> None:
    ctx = _ctx(db_session)
    out, failed = await engine._execute_tool(ctx, "list_facts", {"target": "10.9.9.9"})

    assert not failed
    assert "还是空的" in out
