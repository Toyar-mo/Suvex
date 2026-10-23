"""黑板（`app/facts/`）的测试（v0.79）。

分两层：

* **纯函数**（key 归一、校验、索引渲染）—— 不碰 DB，全部可跑；
* **落库**（UPSERT 语义、跨扫描继承）—— 需要 PostgreSQL，走 `db_session` fixture。

## 这些测试在钉什么

这个模块唯一的读者是**模型**。它会给错形状的输入（整句当 key、
`confidence="high"`、summary 写 800 字）。所以测试的重点不是"正常路径能跑"，
而是**每条错误都给得出可读的错误、且不静默纠正**。
"""

from __future__ import annotations

import pytest

from app.facts.service import (
    INDEX_FOOTER,
    INDEX_MAX_CHARS,
    INDEX_SUMMARY_CHARS,
    normalize_fact_key,
    render_fact_index,
    validate_fact,
)


class TestNormalizeFactKey:
    def test_lowercases_and_squeezes(self):
        assert normalize_fact_key("Endpoint/PUT Auth Users") == "endpoint/put-auth-users"
        assert normalize_fact_key("  ruled_out/foo_bar  ") == "ruled-out/foo-bar"

    def test_keeps_valid_key(self):
        assert normalize_fact_key("endpoint/put-auth-users-id") == "endpoint/put-auth-users-id"

    def test_rejects_missing_slash(self):
        """整句塞进 key 是最常见的模型错误，必须报错而不是猜。"""
        with pytest.raises(ValueError, match="category/slug"):
            normalize_fact_key("PUT /auth/users/{id} 可以改 role")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="不能为空"):
            normalize_fact_key("   ")

    def test_rejects_non_ascii_slug(self):
        """中文 slug 会让 key 在不同编码环境下不稳定，直接拒。"""
        with pytest.raises(ValueError, match="小写字母"):
            normalize_fact_key("endpoint/用户可以改角色")


class TestValidateFact:
    def _ok(self, **kw):
        base = {
            "fact_key": "endpoint/put-auth-users-id",
            "summary": "PUT 改自己 role 能提权到 admin",
            "confidence": "confirmed",
            "trust_level": "L2",
        }
        base.update(kw)
        return validate_fact(**base)

    def test_happy_path(self):
        f = self._ok()
        assert f["fact_key"] == "endpoint/put-auth-users-id"
        assert f["category"] == "endpoint"
        assert f["confidence"] == "confirmed"
        assert f["trust_level"] == "L2"
        assert f["edges"] == []

    def test_category_derived_from_key(self):
        assert self._ok()["category"] == "endpoint"

    def test_category_must_match_key_prefix(self):
        with pytest.raises(ValueError, match="不一致"):
            self._ok(category="surface")

    def test_unknown_category_rejected(self):
        with pytest.raises(ValueError, match="未知 category"):
            validate_fact(fact_key="vulnerability/x", summary="s")

    def test_empty_summary_rejected(self):
        with pytest.raises(ValueError, match="summary 不能为空"):
            self._ok(summary="   ")

    def test_long_summary_rejected_with_guidance(self):
        """给的是"怎么改"而不是"太长了" —— 模型得知道该往哪挪。"""
        with pytest.raises(ValueError, match="detail"):
            self._ok(summary="x" * 301)

    def test_unknown_confidence_lists_choices(self):
        """模型会从 findings 那边学来 "high"，必须列出可用值。"""
        with pytest.raises(ValueError) as ei:
            self._ok(confidence="high")
        msg = str(ei.value)
        assert "confirmed" in msg and "tentative" in msg and "deprecated" in msg

    def test_unknown_trust_level_rejected(self):
        with pytest.raises(ValueError, match="L0 / L1 / L2"):
            self._ok(trust_level="L3")

    def test_trust_level_optional(self):
        assert self._ok(trust_level=None)["trust_level"] is None

    def test_edges_are_normalized(self):
        f = self._ok(edges=[{"type": "enables", "fact_key": "Surface/Admin Panel"}])
        assert f["edges"] == [{"type": "enables", "fact_key": "surface/admin-panel"}]

    def test_edge_missing_field_rejected(self):
        with pytest.raises(ValueError, match="type 和 fact_key"):
            self._ok(edges=[{"type": "enables"}])

    def test_edge_not_dict_rejected(self):
        with pytest.raises(ValueError, match="对象"):
            self._ok(edges=["enables->x"])


class _FakeFact:
    """渲染测试用的最小替身（不依赖 SQLAlchemy）。"""

    def __init__(self, key, summary, confidence="tentative", category=None, edges=None):
        self.fact_key = key
        self.summary = summary
        self.confidence = confidence
        self.category = category or key.split("/")[0]
        self.edges = edges or []


class TestRenderFactIndex:
    def test_empty_returns_empty_string(self):
        """没有事实就一个字节都不加 —— 与 render_live_state 同一条纪律。"""
        assert render_fact_index([]) == ""

    def test_contains_footer_about_get_detail(self):
        """页脚不是装饰：没有它模型会拿摘要当全文、凭摘要臆造细节。"""
        out = render_fact_index([_FakeFact("endpoint/a", "一句话")])
        assert "get_scan_fact" in out
        assert "禁止凭摘要臆造细节" in out

    def test_negative_category_sorts_first(self):
        out = render_fact_index(
            [
                _FakeFact("endpoint/a", "接口甲"),
                _FakeFact("ruled-out/z", "试过不行"),
            ]
        )
        assert out.index("ruled-out/z") < out.index("endpoint/a")

    def test_stable_order_within_same_priority(self):
        """顺序必须稳定 —— 否则每轮渲染抖动会打掉提示词缓存命中率。"""
        facts = [_FakeFact(f"endpoint/{c}", "说明") for c in "cab"]
        a = render_fact_index(facts)
        b = render_fact_index(list(reversed(facts)))
        assert a == b

    def test_summary_is_truncated(self):
        out = render_fact_index([_FakeFact("endpoint/a", "长" * 300)])
        assert "…" in out
        assert out.count("长") <= INDEX_SUMMARY_CHARS + 5

    def test_edges_rendered(self):
        out = render_fact_index(
            [_FakeFact("surface/x", "可写字段", edges=[{"type": "enables", "fact_key": "credential/y"}])]
        )
        assert "enables→credential/y" in out

    def test_respects_max_chars_and_reports_dropped(self):
        facts = [_FakeFact(f"endpoint/{i}", "说明" * 30) for i in range(100)]
        out = render_fact_index(facts, max_chars=400)
        assert len(out) <= 400 + len(INDEX_FOOTER) + 80
        assert "还有" in out and "未列出" in out

    def test_default_budget_is_bounded(self):
        facts = [_FakeFact(f"endpoint/{i}", "说明" * 30) for i in range(200)]
        assert len(render_fact_index(facts)) <= INDEX_MAX_CHARS + len(INDEX_FOOTER) + 80
