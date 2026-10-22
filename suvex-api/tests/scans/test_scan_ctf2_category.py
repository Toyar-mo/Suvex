"""`ScanOut.ctf2Category` 与列表派生（v0.44）。

## 这个文件守什么

一次应急响应复盘在库里是**ctf2 借的壳**（`ctf2/events.ensure_solve_scan`），
于是它在扫描列表里带着两样从 CTF 场景继承来的东西：

* `target_kind = "service"` → 显示「TCP 服务」
* `goal = "captureFlag"`    → 零条目时的措辞在说"夺旗"

而那次任务**没有对任何地址发过包**。所以 v0.44 给 `ScanOut` 加了一个
只读派生字段 `ctf2Category`，前端据此把这一行认成「应急响应复盘」。

## 四条必须钉住的

| # | 约束 | 为什么 |
| --- | --- | --- |
| 1 | 非 CTF2 壳恒为 `None` | 普通扫描页是常态，不能因为加了字段就多一次查询 |
| 2 | CTF2 壳能拿到真实分类 | 否则整行继续显示「TCP 服务」 |
| 3 | 题目被删/`scan_id` 为 NULL 时为 `None` | 必须能降级，不能抛 |
| 4 | 普通 CTF2 题拿到的分类**不是** `ir-*` | 特判挂在分类上，挂错了最先坏这侧 |

★ 第 1 条同时是个性能契约：`_ctf2_categories` 在没有任何 ctf2 壳时
**一条 SQL 都不发**。这条用 `before_cursor_execute` 事件数 SQL 来验证
—— 只断言"返回值是空字典"证明不了它没查库。
"""

from __future__ import annotations

import pytest
from sqlalchemy import event, select

from app.core.ids import new_id
from app.core.vocab import SCAN_ORIGIN_CTF2, ScanGoal, ScanStatus
from app.kernel.models import KernelChallenge, KernelCompetition
from app.scans import router as scans_router
from app.scans.models import Scan
from app.scans.schemas import ScanOut

# ══════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════


def _make_scan(db_session, *, origin: str | None = None, goal: str = ScanGoal.FIND_VULNS.value):  # noqa: ANN001
    """造一条最小可用的扫描记录。"""
    from app.core.timeutil import now_utc

    moment = now_utc()
    scan = Scan(
        id=new_id("scan"),
        name="[CTF2] 一次入侵复盘" if origin else "example.com",
        target="ctf2:local-abc" if origin else "example.com",
        target_kind="service" if origin else "url",
        status=ScanStatus.COMPLETED.value,
        skills=[],
        goal=goal,
        origin=origin,
        provider_id="prov_test",
        started_at=moment,
        # ★ `status='completed'` **必须**同时有 `finished_at`：
        #   `ck_scans_finished_at_matches_terminal_status` 守着这条。
        #   本文件测的是展示层派生，用不到真的时长 —— 但**不能**为了
        #   省事把状态改成 running：那样测的就不是"跑完的一次复盘"
        #   这个真实形状了。
        finished_at=moment,
        duration_sec=0,
    )
    db_session.add(scan)
    db_session.flush()
    return scan


def _make_challenge(db_session, scan, *, category: str, remote_id: str | None = None):  # noqa: ANN001
    """给某条 scan 挂一道 CTF2 题（`scan_id` 指过去）。"""
    comp = db_session.execute(select(KernelCompetition).limit(1)).scalar_one_or_none()
    if comp is None:
        comp = KernelCompetition(
            id=new_id("ctf2comp"),
            account_id=None,
            remote_id="local-reviews",
            kind="local",
            name="本地复盘",
            challenge_count=0,
        )
        db_session.add(comp)
        db_session.flush()

    challenge = KernelChallenge(
        id=new_id("ctf2chal"),
        competition_id=comp.id,
        remote_id=remote_id or new_id("local"),
        title="一次入侵复盘",
        category=category,
        description="证据包",
        attachments=[],
        status="unsolved",
        sandbox_profile="ctf2-sandbox-ir",
        scan_id=scan.id if scan is not None else None,
    )
    db_session.add(challenge)
    db_session.flush()
    return challenge


# ══════════════════════════════════════════════════════════
# 一、`_ctf2_categories` 的判据
# ══════════════════════════════════════════════════════════


def test_no_ctf2_shells_returns_empty(db_session) -> None:  # noqa: ANN001
    """没有 ctf2 壳时返回空字典。"""
    scan = _make_scan(db_session)
    assert scans_router._ctf2_categories(db_session, [scan]) == {}


def test_empty_list_returns_empty(db_session) -> None:  # noqa: ANN001
    """空列表不能炸 —— 列表页可以合法地返回 0 条。"""
    assert scans_router._ctf2_categories(db_session, []) == {}


def test_non_ctf2_shells_issue_no_query(db_session) -> None:  # noqa: ANN001
    """★ 没有 ctf2 壳时**一条 SQL 都不发**。

    只断言返回值是空字典证明不了它没查库 —— 而这里真正的契约是
    "普通扫描列表页不为这个字段付一次查询"。所以直接数 SQL。
    """
    scan = _make_scan(db_session)

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        result = scans_router._ctf2_categories(db_session, [scan])
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert result == {}
    # 过滤掉 SQLAlchemy 自己的元数据查询（这里不该有，但保守一点）
    ctf2_queries = [s for s in statements if "ctf2_challenges" in s.lower()]
    assert ctf2_queries == [], f"不该查 ctf2_challenges，却发了：{ctf2_queries}"


def test_ctf2_shell_gets_its_category(db_session) -> None:  # noqa: ANN001
    """CTF2 壳能拿到真实分类。"""
    scan = _make_scan(db_session, origin=SCAN_ORIGIN_CTF2, goal=ScanGoal.CAPTURE_FLAG.value)
    _make_challenge(db_session, scan, category="ir-root-cause")

    result = scans_router._ctf2_categories(db_session, [scan])
    assert result == {scan.id: "ir-root-cause"}


def test_batch_query_covers_all_shells(db_session) -> None:  # noqa: ANN001
    """一次查询覆盖全部壳（不是只取第一条）。"""
    a = _make_scan(db_session, origin=SCAN_ORIGIN_CTF2, goal=ScanGoal.CAPTURE_FLAG.value)
    b = _make_scan(db_session, origin=SCAN_ORIGIN_CTF2, goal=ScanGoal.CAPTURE_FLAG.value)
    _make_challenge(db_session, a, category="ir-root-cause")
    _make_challenge(db_session, b, category="web")

    result = scans_router._ctf2_categories(db_session, [a, b])
    assert result[a.id] == "ir-root-cause"
    assert result[b.id] == "web"


def test_mixed_list_only_maps_shells(db_session) -> None:  # noqa: ANN001
    """普通扫描混在壳里时不出现在结果里，也不干扰壳的取值。"""
    normal = _make_scan(db_session)
    shell = _make_scan(db_session, origin=SCAN_ORIGIN_CTF2, goal=ScanGoal.CAPTURE_FLAG.value)
    _make_challenge(db_session, shell, category="ir-timeline")

    result = scans_router._ctf2_categories(db_session, [normal, shell])
    assert normal.id not in result
    assert result[shell.id] == "ir-timeline"


def test_shell_without_challenge_returns_nothing(db_session) -> None:  # noqa: ANN001
    """★ 壳存在但题目已被删 / `scan_id` 为 NULL → 不出现在结果里。

    这是"必须能降级"的那条：前端 `.get()` 得到 `None` 后按普通
    记录处理。这里**不能**补一个 `""` 之类的占位值 —— 那会让前端
    以为拿到了分类。
    """
    scan = _make_scan(db_session, origin=SCAN_ORIGIN_CTF2, goal=ScanGoal.CAPTURE_FLAG.value)
    # 不挂 challenge
    assert scans_router._ctf2_categories(db_session, [scan]) == {}


def test_empty_category_is_dropped(db_session) -> None:  # noqa: ANN001
    """空分类不进结果 —— `""` 命中不了任何前缀，传下去只是假判据。"""
    scan = _make_scan(db_session, origin=SCAN_ORIGIN_CTF2, goal=ScanGoal.CAPTURE_FLAG.value)
    _make_challenge(db_session, scan, category="")

    assert scans_router._ctf2_categories(db_session, [scan]) == {}


# ══════════════════════════════════════════════════════════
# 二、`_category_of`（单条版）
# ══════════════════════════════════════════════════════════


def test_category_of_normal_scan_is_none_without_query(db_session) -> None:  # noqa: ANN001
    """★ 普通扫描直接返回 `None` **且不查库**。

    写操作回执（终止 / 续跑 / 重跑）绝大多数打在普通扫描上。
    """
    scan = _make_scan(db_session)

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        assert scans_router._category_of(db_session, scan) is None
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert [s for s in statements if "ctf2_challenges" in s.lower()] == []


def test_category_of_shell_returns_category(db_session) -> None:  # noqa: ANN001
    scan = _make_scan(db_session, origin=SCAN_ORIGIN_CTF2, goal=ScanGoal.CAPTURE_FLAG.value)
    _make_challenge(db_session, scan, category="ir-scope")

    assert scans_router._category_of(db_session, scan) == "ir-scope"


# ══════════════════════════════════════════════════════════
# 三、`ScanOut.from_model` 的形状
# ══════════════════════════════════════════════════════════


def test_from_model_defaults_category_to_none(db_session) -> None:  # noqa: ANN001
    """不传时是 `None` —— 其余七八个调用点因此不用改。"""
    scan = _make_scan(db_session)
    assert ScanOut.from_model(scan).ctf2Category is None


def test_from_model_accepts_category_kwarg(db_session) -> None:  # noqa: ANN001
    scan = _make_scan(db_session, origin=SCAN_ORIGIN_CTF2, goal=ScanGoal.CAPTURE_FLAG.value)
    assert ScanOut.from_model(scan, ctf2Category="ir-root-cause").ctf2Category == "ir-root-cause"


def test_from_model_is_keyword_only(db_session) -> None:  # noqa: ANN001
    """`ctf2Category` 必须只能按关键字传。

    位置传会让"给 from_model 多加一个参数"变成一次静默的错位：
    `from_model(scan, "ir-x")` 看着像传了分类，实际也可能被当成
    未来的第二个位置参数。
    """
    import inspect

    params = inspect.signature(ScanOut.from_model).parameters
    assert params["ctf2Category"].kind is inspect.Parameter.KEYWORD_ONLY


def test_schema_dumps_category_as_camel_case(db_session) -> None:  # noqa: ANN001
    """契约字段名是 `ctf2Category`（camelCase）—— 前端按这个键读。"""
    scan = _make_scan(db_session, origin=SCAN_ORIGIN_CTF2, goal=ScanGoal.CAPTURE_FLAG.value)
    payload = ScanOut.from_model(scan, ctf2Category="ir-root-cause").model_dump(by_alias=True)
    assert payload["ctf2Category"] == "ir-root-cause"


def test_normal_scan_payload_has_key_set_to_null(db_session) -> None:  # noqa: ANN001
    """★ 普通扫描也要带这个键，值为 `null`。

    省略键会让 TypeScript 侧 `scan.ctf2Category` 变成 `undefined`
    而不是 `null`，两者在可选链里表现相同、在严格比较里不同 ——
    而"有时有时没有"正是最容易写出 `?? ''` 这种掩盖 bug 的代码的形状。
    """
    scan = _make_scan(db_session)
    payload = ScanOut.from_model(scan).model_dump(by_alias=True)
    assert "ctf2Category" in payload
    assert payload["ctf2Category"] is None


@pytest.mark.parametrize(
    "category",
    ["ir-root-cause", "ir-timeline", "ir-scope", "web", "pwn", "agentsec-data-leak"],
)
def test_category_passes_through_verbatim(db_session, category: str) -> None:  # noqa: ANN001
    """后端**不改写**分类值 —— 认不认 IR 是前端的事。

    ★ 这条是"同一件事实只能有一个出处"的直接体现：后端只说
      分类是什么，前缀判据在 `suvex-web/src/data/scan.ts` 里。
      若后端在这里把 `ir-*` 改写成 `"ir"` 之类的归一值，前后端
      就各有一份前缀表了。
    """
    scan = _make_scan(db_session, origin=SCAN_ORIGIN_CTF2, goal=ScanGoal.CAPTURE_FLAG.value)
    _make_challenge(db_session, scan, category=category)

    assert scans_router._category_of(db_session, scan) == category
