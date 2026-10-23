"""证据分级与阴性结果留存（v0.75）的回归测试。

这些测试保护的是**可信度**，不是功能：每一条都对应"如果这条规则失效，
我们会对人报告一个不成立的漏洞"。
"""

from __future__ import annotations

from app.kernel import evidence


class TestLevelMapping:
    """`evidence_kind` → L0/L1/L2 的折算规则。"""

    def test_cross_identity_is_l2(self) -> None:
        # 越权类结论的唯一有效证法是"两个身份各测一次"。
        assert evidence.level_for_evidence_kind("cross_identity") == "L2"

    def test_single_response_is_l1(self) -> None:
        # 单次请求有原始响应：有证据，但同身份自证，够不上 L2。
        assert evidence.level_for_evidence_kind("single_response") == "L1"

    def test_inferred_is_l0(self) -> None:
        assert evidence.level_for_evidence_kind("inferred") == "L0"

    def test_missing_or_unknown_kind_defaults_to_l0(self) -> None:
        """没写 / 写错 → L0。

        反向验证：把 `DEFAULT_LEVEL` 改成 "L1"，本条立刻 FAILED ——
        这是防"模型忘了写字段就自动升级"的那道闸。
        """
        assert evidence.level_for_evidence_kind(None) == "L0"
        assert evidence.level_for_evidence_kind("") == "L0"
        assert evidence.level_for_evidence_kind("我验证过了") == "L0"
        assert evidence.level_for_evidence_kind(123) == "L0"

    def test_kind_is_case_and_space_insensitive(self) -> None:
        assert evidence.level_for_evidence_kind("  CROSS_IDENTITY  ") == "L2"

    def test_every_kind_maps_to_a_known_level(self) -> None:
        # 表里写错一个值（比如 "L3"）会被这里抓住。
        for kind, level in evidence.EVIDENCE_KIND_TO_LEVEL.items():
            assert level in evidence.TRUST_RANK, f"{kind} → {level} 不是合法级别"


class TestExplicitLevel:
    """人工复核可以把级别写死在记录里，且优先于 evidence_kind。"""

    def test_explicit_level_wins(self) -> None:
        rec = {"title": "x", "trust_level": "L2", "evidence_kind": "inferred"}
        assert evidence.level_for(rec) == "L2"

    def test_explicit_level_is_normalized(self) -> None:
        assert evidence.level_for({"title": "x", "trust_level": " l1 "}) == "L1"

    def test_invalid_explicit_level_falls_back_to_kind(self) -> None:
        # 写了 L9 不能崩、也不能当成高级别。
        rec = {"title": "x", "trust_level": "L9", "evidence_kind": "cross_identity"}
        assert evidence.level_for(rec) == "L2"


class TestFindingsGating:
    """L0 不许进 findings —— 漏洞统计不能被推断污染。"""

    def test_l0_does_not_enter_findings(self) -> None:
        assert evidence.should_enter_findings("L0") is False

    def test_l1_and_l2_enter_findings(self) -> None:
        assert evidence.should_enter_findings("L1") is True
        assert evidence.should_enter_findings("L2") is True

    def test_verification_mapping_matches_importer_vocabulary(self) -> None:
        """`findings` 域只认 verified / partial / none 这几个词。

        这里直接读那个模块自己的映射表来对表，避免两边各写一份词汇表
        然后悄悄漂移（本项目已经因为"文档说一套、代码做一套"栽过）。
        形状是 `confidence(high|medium|low) → verification`。
        """
        from app.findings import strix_vulns

        known = set(strix_vulns._CONFIDENCE_TO_VERIFICATION.values())
        for level in ("L0", "L1", "L2"):
            word = evidence.verification_for_level(level)
            assert word in known, (
                f"{level} → {word} 不在 importer 的词汇表 {sorted(known)} 里"
            )

    def test_l2_maps_to_verified_and_l1_to_partial(self) -> None:
        # 具体到词，不只是"在集合里"：L2 必须是 verified，L1 必须不是。
        assert evidence.verification_for_level("L2") == "verified"
        assert evidence.verification_for_level("L1") == "partial"
        assert evidence.verification_for_level("L0") == "partial"


class TestNormalizeObservation:
    def test_dict_keeps_fields(self) -> None:
        rec = evidence.normalize_observation(
            {
                "title": " 越权读取  ",
                "summary": "user 能读 admin 的列表",
                "evidence": "GET /users → 200",
                "evidence_kind": "cross_identity",
                "severity": "high",
            }
        )
        assert rec is not None
        assert rec["title"] == "越权读取"
        assert rec["trust_level"] == "L2"
        assert rec["polarity"] == "positive"

    def test_string_becomes_title_only(self) -> None:
        rec = evidence.normalize_observation("GET /api/v1/users 越权")
        assert rec is not None
        assert rec["title"] == "GET /api/v1/users 越权"
        # 纯字符串没有 evidence_kind → L0，不能因为"模型写了"就当发现
        assert rec["trust_level"] == "L0"

    def test_blank_and_weird_inputs_are_dropped(self) -> None:
        assert evidence.normalize_observation("") is None
        assert evidence.normalize_observation("   ") is None
        assert evidence.normalize_observation(None) is None
        assert evidence.normalize_observation(42) is None
        assert evidence.normalize_observation({"title": ""}) is None

    def test_all_output_keys_present(self) -> None:
        rec = evidence.normalize_observation({"title": "t"})
        assert rec is not None
        for key in (
            "title",
            "summary",
            "evidence",
            "target",
            "cwe",
            "severity",
            "evidence_kind",
            "trust_level",
            "polarity",
            "reported_by",
        ):
            assert key in rec, f"缺字段 {key}"

    def test_default_target_fills_in(self) -> None:
        rec = evidence.normalize_observation({"title": "t"}, default_target="http://x/")
        assert rec is not None
        assert rec["target"] == "http://x/"


class TestNegativeObservations:
    """阴性结果：留存、可审计，但永不算漏洞。"""

    def test_ruled_out_becomes_negative_l2(self) -> None:
        rec = evidence.make_negative_observation("alg:none 被拒（401）", reported_by="a3")
        assert rec is not None
        assert rec["polarity"] == "negative"
        assert rec["trust_level"] == "L2"
        assert rec["reported_by"] == "a3"

    def test_negative_never_counts_as_finding(self) -> None:
        """它 `should_enter_findings()` 恒 False —— 排除项不是漏洞。

        注意 `should_enter_findings` 只看级别，所以"不进 findings"这件事
        在 `_write_vulnerabilities` 里由 `polarity` 决定（只遍历 findings
        列表，ruled_out 根本不进那条路径）。这里钉住的是级别本身不低。
        """
        rec = evidence.make_negative_observation("排除了 X")
        assert rec is not None
        assert evidence.level_for(rec) == "L2"

    def test_blank_negative_dropped(self) -> None:
        assert evidence.make_negative_observation("") is None
        assert evidence.make_negative_observation(None) is None
        assert evidence.make_negative_observation({"a": 1}) is None


class TestBuildObservations:
    def test_merges_findings_and_ruled_out(self) -> None:
        obs = evidence.build_observations(
            findings=[{"title": "越权", "evidence_kind": "cross_identity"}],
            ruled_out=["alg:none 被拒"],
            reported_by="agent",
        )
        assert len(obs) == 2
        polars = {o["polarity"] for o in obs}
        assert polars == {"positive", "negative"}

    def test_dedupes_same_title_same_polarity(self) -> None:
        obs = evidence.build_observations(
            findings=[{"title": "X", "evidence_kind": "single_response"}] * 3
        )
        assert len(obs) == 1

    def test_conflict_between_polarities_is_kept(self) -> None:
        """一个 agent 说成立、另一个说排除 —— 两条都留。

        悄悄合并成一条会掩盖分歧，而分歧本身是"这个结论不可靠"的
        最有价值信号。
        """
        obs = evidence.build_observations(
            findings=[{"title": "限流可锁定全部登录", "evidence_kind": "single_response"}],
            ruled_out=["限流可锁定全部登录"],
        )
        assert len(obs) == 2

    def test_non_list_inputs_are_ignored(self) -> None:
        assert evidence.build_observations(findings="x", ruled_out=None) == []
        assert evidence.build_observations() == []

    def test_summarize_levels(self) -> None:
        obs = evidence.build_observations(
            findings=[
                {"title": "a", "evidence_kind": "cross_identity"},
                {"title": "b", "evidence_kind": "single_response"},
                {"title": "c"},
            ],
            ruled_out=["d"],
        )
        levels = evidence.summarize_levels(obs)
        assert levels == {"L0": 1, "L1": 1, "L2": 2}

class TestPromptTeachesEvidenceKind:
    """★ 提示词必须教 `evidence_kind` —— 否则 U3 判级永远拿不到输入。

    2026-10-23 的 P2 重跑就是这种情况：`app/kernel/evidence.py` 判级表写好了、
    测试全绿、接线也在 `_write_vulnerabilities` 里 —— 但**提示词一个字没提**
    `evidence_kind`。模型交上来的 7 条观察全都没有这个字段，于是全部落到
    `DEFAULT_LEVEL="L0"`，再被 `should_enter_findings` 挡在报告外，
    `vulnerabilities.json` 最后是 `[]`。

    这是 v0.72 `mcp_servers` 事故的同一种形状：**代码写好了，但没有告诉
    使用方要用它**。纯函数测试抓不到这种失效，只能盯提示词文本本身。
    """

    def test_pentest_prompt_requires_evidence_kind(self) -> None:
        from pathlib import Path

        text = (
            Path(__file__).resolve().parents[2] / "prompts" / "kernel_pentest.md"
        ).read_text(encoding="utf-8")
        assert "evidence_kind" in text, "提示词没提 evidence_kind，模型不会写这个字段"
        assert "必填" in text or "必须" in text

    def test_pentest_prompt_lists_every_kind(self) -> None:
        """判级表里的每个取值都必须在提示词里出现 —— 漏一个就有一条路走不通。"""
        from pathlib import Path

        from app.kernel.evidence import EVIDENCE_KIND_TO_LEVEL

        text = (
            Path(__file__).resolve().parents[2] / "prompts" / "kernel_pentest.md"
        ).read_text(encoding="utf-8")
        missing = [k for k in EVIDENCE_KIND_TO_LEVEL if k not in text]
        assert not missing, f"提示词里缺这些 evidence_kind 的说明：{missing}"


class TestPromptTeachesExcludedPaths:
    """★ 提示词必须教「已排除路径」—— 这是 P2 真实损失的机制。

    来源：2026 智能渗透黑客松第 7 名队伍的 CLAUDE.md 硬约束：
      > ✗ 禁止重复已在「已排除路径」中的命令/URL/payload（换参数也算重复）

    我们的 P2 实测：`/api/v1/scans` 被探 13 次、`/auth/login` 12 次、
    `/auth/state` 9 次 —— 每次换参数，打的是同一个点，80 轮耗尽。
    提示词若不讲这条，`target_shape` 的自动提醒也只会被当成噪音。
    """

    def _text(self) -> str:
        from pathlib import Path

        return (
            Path(__file__).resolve().parents[2] / "prompts" / "kernel_pentest.md"
        ).read_text(encoding="utf-8")

    def test_section_exists(self) -> None:
        assert "已排除路径" in self._text()

    def test_says_changing_params_is_not_new(self) -> None:
        """核心那句必须在：换参数不算新尝试。"""
        t = self._text()
        assert "换参数" in t and "不算新" in t

    def test_lists_four_redirection_directions(self) -> None:
        """必须给出可执行的方向，不能只说"换个思路"。"""
        t = self._text()
        for kw in ("换身份", "换入口", "换一层", "换一个面"):
            assert kw in t, f"缺方向：{kw}"

    def test_tells_agent_to_record_exclusions(self) -> None:
        """排除掉了要写下来，否则下一个 agent 会重走。"""
        t = self._text()
        assert "add_fact" in t or "record_coverage" in t
