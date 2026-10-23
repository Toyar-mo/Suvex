"""跨扫描继承（v0.80 补齐）：`seed_inherited` / `ScopedFactStore.seed`。

## 这里在钉什么

`seed_inherited` 在 v0.80 第一版里是**孤儿函数** —— 全仓库没有调用点，
`recorded_by="inherited"` 的行因此永远不会出现，"跨扫描继承"这条设计
只能靠人眼看代码来相信。这一组测试钉住三件事：

1. **继承什么**：只抄 `ruled-out` / `constraint`。另外三类
   （endpoint / credential / surface）是**会变的**，继承旧结论会让
   这次扫描跳过真正该测的东西 —— 那比"什么都不知道"更糟。
2. **覆盖谁**：本次扫描自己写过的 key 不被旧结论盖掉（现场证据优先）。
3. **调几次算几次**：重复调用不该插出重复行 —— `UNIQUE(scan_id, fact_key)`
   会把它变成 IntegrityError，而扫描入口吞异常后表现为"静默不继承"。

另外钉住 `normalize_target`：目标字符串在两次扫描之间几乎从来不是同一个
（`http://x:8080` / `x:8080/` / `X:8080`），继承若按原样比字符串，表现
不是报错而是**永远 0 条**。
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.facts import service as facts_service
from app.facts.models import ScanFact
from app.facts.scoped_store import ScopedFactStore
from app.facts.store import INHERITED_BY, seed_inherited
from tests.factories import make_scan

TARGET = "http://172.17.0.1:8080"


def _write(
    db: Session,
    *,
    scan_id: str | None,
    key: str,
    target: str = TARGET,
    summary: str = "一条结论",
    recorded_by: str = "agent-root",
) -> ScanFact:
    """按 `record_fact` 的真实路径写一条（走 validate + upsert，不手搓对象）。"""
    fields = facts_service.validate_fact(
        fact_key=key, summary=summary, confidence="confirmed"
    )
    fact, _ = facts_service.upsert_fact(
        db, scan_id=scan_id, target=target, recorded_by=recorded_by, fields=fields
    )
    db.commit()
    return fact


def _rows(db: Session, scan_id: str) -> list[ScanFact]:
    db.expire_all()  # `seed_inherited` 用的是同一条会话，别拿 identity map 里的旧值
    return facts_service.load_facts(db, scan_id)


class TestWhatGetsInherited:
    def test_copies_ruled_out_and_constraint(self, db_session: Session) -> None:
        old = make_scan(db_session, target=TARGET)
        new = make_scan(db_session, target=TARGET)
        _write(db_session, scan_id=old.id, key="ruled-out/settings-token-is-mock")
        _write(db_session, scan_id=old.id, key="constraint/api-is-read-only")

        copied = seed_inherited(db_session, scan_id=new.id, target=TARGET)

        assert copied == 2
        keys = {r.fact_key for r in _rows(db_session, new.id)}
        assert keys == {"ruled-out/settings-token-is-mock", "constraint/api-is-read-only"}

    def test_does_not_copy_volatile_categories(self, db_session: Session) -> None:
        """`endpoint` / `credential` / `surface` 是**会变的**，继承它们会让人跳过该测的点。

        ## 为什么要拿它跟 `ruled-out` 混在一起

        第一版只写了这三类，断言 `== 0` —— 结果**测不出来**：只要
        `seed_inherited` 返回 0 就算过，而那也可能是"根本没抄到任何东西"
        （比如 target 归一化坏了）。2026-10-23 反向验证实测：把
        `endpoint` 加进白名单后这条**照样绿**（3 条全被抄进来，返回 3 ≠ 0？
        不 —— 实际是绿，因为当时这条的断言对象形状让改坏版本恰好也过）。

        正确形状：同一次调用里**既有该抄的、也有不该抄的**，然后逐 key
        断言 —— 抄回来的集合必须恰好等于 `{ruled-out/..., constraint/...}`。
        这样"多抄了会变的类别"会红，"什么都没抄到"也会红。
        """
        old = make_scan(db_session, target=TARGET)
        new = make_scan(db_session, target=TARGET)
        _write(db_session, scan_id=old.id, key="ruled-out/keep-me")
        _write(db_session, scan_id=old.id, key="constraint/keep-me-too")
        for key in (
            "endpoint/put-auth-users-id",
            "credential/session-is-cookie",
            "surface/admin-panel-writable",
        ):
            _write(db_session, scan_id=old.id, key=key)

        copied = seed_inherited(db_session, scan_id=new.id, target=TARGET)

        assert copied == 2, "只该抄 ruled-out + constraint"
        assert {r.fact_key for r in _rows(db_session, new.id)} == {
            "ruled-out/keep-me",
            "constraint/keep-me-too",
        }

    def test_marks_rows_as_inherited(self, db_session: Session) -> None:
        """`recorded_by="inherited"` 是复盘时区分"现场验证"与"上次留下的"的唯一标记。"""
        old = make_scan(db_session, target=TARGET)
        new = make_scan(db_session, target=TARGET)
        _write(db_session, scan_id=old.id, key="ruled-out/x-y")

        seed_inherited(db_session, scan_id=new.id, target=TARGET)

        rows = _rows(db_session, new.id)
        assert len(rows) == 1
        assert rows[0].recorded_by == INHERITED_BY
        assert rows[0].scan_id == new.id
        assert rows[0].version == 1

    def test_other_target_is_not_inherited(self, db_session: Session) -> None:
        """跨目标继承等于拿别人的结论骗自己 —— 必须为 0。"""
        other = make_scan(db_session, target="http://10.0.0.9:8000")
        new = make_scan(db_session, target=TARGET)
        _write(db_session, scan_id=other.id, key="ruled-out/x-y", target="http://10.0.0.9:8000")

        assert seed_inherited(db_session, scan_id=new.id, target=TARGET) == 0

    def test_limit_is_respected(self, db_session: Session) -> None:
        old = make_scan(db_session, target=TARGET)
        new = make_scan(db_session, target=TARGET)
        for i in range(5):
            _write(db_session, scan_id=old.id, key=f"ruled-out/r{i}")

        copied = seed_inherited(db_session, scan_id=new.id, target=TARGET, limit=3)

        assert copied == 3
        assert len(_rows(db_session, new.id)) == 3


class TestOverwriteRules:
    def test_local_evidence_wins(self, db_session: Session) -> None:
        """本次已经写过同一个 key：不拿旧结论覆盖它（现场证据优先）。"""
        old = make_scan(db_session, target=TARGET)
        new = make_scan(db_session, target=TARGET)
        _write(db_session, scan_id=old.id, key="ruled-out/x-y", summary="上次的结论")
        _write(db_session, scan_id=new.id, key="ruled-out/x-y", summary="这次的结论")

        copied = seed_inherited(db_session, scan_id=new.id, target=TARGET)

        assert copied == 0
        rows = _rows(db_session, new.id)
        assert len(rows) == 1
        assert rows[0].summary == "这次的结论"
        assert rows[0].recorded_by == "agent-root"  # 没被改成 inherited

    def test_second_call_is_noop(self, db_session: Session) -> None:
        """连调两次不插重复行。

        ## 观测到的语义（不是猜的）

        第二次调用返回 **0** —— 短路条件是"本次已有 `recorded_by=inherited`
        的行"。没有这个短路，第二轮会复制出**新 id 的重复行**，
        撞 `UNIQUE(scan_id, fact_key)` 变 IntegrityError，而扫描入口的
        `except` 会把它吞成"静默不继承"（更难查）。
        """
        old = make_scan(db_session, target=TARGET)
        new = make_scan(db_session, target=TARGET)
        _write(db_session, scan_id=old.id, key="ruled-out/x-y")

        first = seed_inherited(db_session, scan_id=new.id, target=TARGET)
        second = seed_inherited(db_session, scan_id=new.id, target=TARGET)

        assert (first, second) == (1, 0)
        assert len(_rows(db_session, new.id)) == 1

    def test_own_records_do_not_block_first_seed(self, db_session: Session) -> None:
        """⚠️ 短路条件是「已有 inherited 行」，不是「已有行」。

        模型在扫描里先写了别的 key 之后才继承（比如重跑），不该因此一条都继承不到。
        """
        old = make_scan(db_session, target=TARGET)
        new = make_scan(db_session, target=TARGET)
        _write(db_session, scan_id=old.id, key="ruled-out/x-y")
        _write(db_session, scan_id=new.id, key="endpoint/other-thing")

        assert seed_inherited(db_session, scan_id=new.id, target=TARGET) == 1


class TestTargetKey:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("http://172.17.0.1:8080", "172.17.0.1:8080"),
            ("http://172.17.0.1:8080/", "HTTP://172.17.0.1:8080"),
            ("  172.17.0.1:8080  ", "http://172.17.0.1:8080/"),
        ],
    )
    def test_normalized_forms_are_one_target(self, db_session: Session, a: str, b: str) -> None:
        """同一目标的三种写法必须被认成同一个 —— 否则继承静默收 0 条。"""
        old = make_scan(db_session, target=a)
        new = make_scan(db_session, target=b)
        _write(db_session, scan_id=old.id, key="ruled-out/x-y", target=a)

        assert seed_inherited(db_session, scan_id=new.id, target=b) == 1

    def test_port_is_part_of_the_key(self, db_session: Session) -> None:
        """去掉端口就等于把"另一个服务面"当成同一个目标 —— 宁可少继承。"""
        old = make_scan(db_session, target="172.17.0.1:8080")
        new = make_scan(db_session, target="172.17.0.1:8443")
        _write(db_session, scan_id=old.id, key="ruled-out/x-y", target="172.17.0.1:8080")

        assert seed_inherited(db_session, scan_id=new.id, target="172.17.0.1:8443") == 0

    def test_empty_target_inherits_nothing(self, db_session: Session) -> None:
        """空 target 不能当通配符（否则没填目标的扫描会继承全场事实）。"""
        old = make_scan(db_session, target=TARGET)
        new = make_scan(db_session, target=TARGET)
        _write(db_session, scan_id=old.id, key="ruled-out/x-y")

        assert seed_inherited(db_session, scan_id=new.id, target="") == 0


class TestScopedStoreSeed:
    """生产路径注入的是 `ScopedFactStore`（`agent_runner` 里），它必须也有 `seed()`。"""

    def test_seed_returns_count_and_lands_in_index(self, db_session: Session) -> None:
        old = make_scan(db_session, target=TARGET)
        new = make_scan(db_session, target=TARGET)
        _write(db_session, scan_id=old.id, key="ruled-out/x-y", summary="上次走不通")

        store = ScopedFactStore(scan_id=new.id, target=TARGET)
        assert store.seed() == 1

        block = store.index_block()
        assert "ruled-out/x-y" in block
        assert facts_service.INDEX_FOOTER in block

    def test_seed_is_idempotent(self, db_session: Session) -> None:
        old = make_scan(db_session, target=TARGET)
        new = make_scan(db_session, target=TARGET)
        _write(db_session, scan_id=old.id, key="ruled-out/x-y")

        store = ScopedFactStore(scan_id=new.id, target=TARGET)
        assert (store.seed(), store.seed()) == (1, 0)

    def test_empty_target_returns_zero_without_raising(self) -> None:
        """`seed()` 在扫描入口被调用，抛出去会变成"这次扫描起不来"。"""
        store = ScopedFactStore(scan_id="scan_whatever", target="")
        assert store.seed() == 0

    def test_seed_failure_is_swallowed(self, db_session: Session, monkeypatch) -> None:
        """继承是增强项：库抖一下不该让整场扫描起不来（与 record_fact 的"未启用"分支同一条理由）。"""
        import app.facts.scoped_store as scoped

        def _boom(*a, **kw):
            raise RuntimeError("库抖了一下")

        monkeypatch.setattr(scoped, "seed_inherited", _boom)
        store = ScopedFactStore(scan_id="scan_x", target=TARGET)
        assert store.seed() == 0
