"""覆盖记录落库的测试（v1.81）。"""

from __future__ import annotations

from app.kernel.multiagent import AgentCoordinator


class _FakeStore:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        return {"fact_key": kwargs["fact_key"], "created": True, "version": 1}


def test_record_coverage_persists_a_surface_fact() -> None:
    store = _FakeStore()
    coord = AgentCoordinator(fact_store=store)

    coord.shared.record_coverage(
        "GET /api/v1/users", "high", "401 无鉴权", by="root"
    )

    assert len(store.calls) == 1
    call = store.calls[0]
    assert call["fact_key"] == "surface/get-api-v1-users"
    assert call["category"] == "surface"
    assert call["summary"] == "401 无鉴权"
    assert call["detail"] == "high"
    assert call["recorded_by"] == "root"


def test_record_coverage_without_store_is_memory_only() -> None:
    coord = AgentCoordinator()

    coord.shared.record_coverage("GET /x", "low", "clean", by="a1")

    assert coord.shared.coverage["GET /x"]["outcome"] == "clean"


def test_store_failure_does_not_break_coverage() -> None:
    class _Boom:
        def record(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("db down")

    coord = AgentCoordinator(fact_store=_Boom())

    coord.shared.record_coverage("GET /x", "low", "clean", by="a1")

    # 内存态仍然记下了 —— 黑板是增强项，不能反向打断编排。
    assert coord.shared.coverage["GET /x"]["outcome"] == "clean"
