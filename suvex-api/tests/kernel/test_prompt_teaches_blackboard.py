"""钉住「提示词真的教了黑板工具」（v0.79）。

## 为什么这批测试必须有

v0.77 的 `evidence_kind` 事故：判级表写好、26 条单测绿、接线也在，
但 `grep -rn "evidence_kind" prompts/` 是空的 —— 模型不知道要写这个字段，
7 条质量不错的观察全落 L0 被挡在 findings 外面，`vulnerabilities.json` 是 `[]`。

**纯函数测试抓不到这种缺陷**，只有盯提示词文本才能抓到。所以这里
直接读 md 文件断言关键内容在。

同形状的历史事故还有 v0.72 的 `mcp_servers`。这是第二次，所以钉死。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.kernel import multiagent

PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "kernel_pentest.md"


@pytest.fixture(scope="module")
def text() -> str:
    return PROMPT.read_text(encoding="utf-8")


class TestPromptTeachesBlackboard:
    def test_mentions_record_fact(self, text):
        assert "record_fact" in text, "提示词没提 record_fact —— 模型不会用它"

    def test_mentions_get_scan_fact(self, text):
        assert "get_scan_fact" in text, "提示词没提 get_scan_fact —— 索引块里的摘要没法展开"

    def test_explains_when_to_use_which(self, text):
        """必须给出选择规则，否则模型会两个都记（噪音）或者记错那个（丢持久性）。"""
        assert "add_fact" in text
        # 判断规则那一句
        assert "下次扫同一个目标" in text

    def test_forbids_inventing_detail_from_summary(self, text):
        """索引只有 80 字符摘要。没有这条禁令，模型会把摘要扩写成假证据，
        那比没有黑板更糟。"""
        assert "禁止凭摘要臆造细节" in text

    def test_lists_all_three_confidences(self, text):
        for c in ("confirmed", "tentative", "deprecated"):
            assert c in text, f"提示词没解释 confidence={c}"

    def test_explains_fact_key_shape(self, text):
        """`category/slug` 的形状不教，模型会把整句塞进 key（validate 会拒，
        但每次都要多一轮往返）。"""
        assert "category/slug" in text

    def test_explains_blackboard_is_pushed_every_turn(self, text):
        """模型得知道"不用我去查就有" —— 否则它会一直 get_memory 空翻。"""
        assert "项目黑板" in text

    def test_all_categories_reachable_from_prompt(self, text):
        """提示词里举的例子必须落在合法 category 里，否则模型照着例子写会被拒。

        ⚠️ v0.84 修过一次**静默失效**：原来只扫 `record_fact(fact_key=` 这种
        Python 调用式，而 v0.84 把示例改成了真实 ACTION 协议的形状
        （`"fact_key": "xxx/yyy"`）。旧循环一行都匹配不上 -> 空转通过 ->
        守卫看似在、其实早就不检查任何东西了。

        现在同时认两种形状；并且**断言至少真的检查到了例子**，
        否则"零匹配"会让它再次静默变成空测试。
        """
        import re

        from app.facts.models import FACT_CATEGORIES

        # 至少要提到 ruled-out（最值钱的那类）
        assert "ruled-out" in text

        # 两种形状都认：老的 Python 调用式 + 新的 ACTION JSON 式
        patterns = (
            re.compile(r'record_fact\(fact_key="([^"]+)"'),
            re.compile(r'"fact_key"\s*:\s*"([^"]+)"'),
        )

        checked = 0
        for line in text.splitlines():
            for pat in patterns:
                for key in pat.findall(line):
                    # 骨架里的占位符不是真实例子，跳过
                    if key.startswith("<"):
                        continue
                    cat = key.split("/", 1)[0]
                    assert cat in FACT_CATEGORIES, (
                        f"示例 key {key!r} 的分类 {cat!r} 不合法"
                    )
                    checked += 1

        assert checked >= 2, (
            f"只检查到 {checked} 个示例 key —— 提示词的示例格式可能又变了，"
            "这个守卫正在静默失效，请同步更新正则"
        )


class TestToolNamesStillRegistered:
    """提示词提到了，工具表里也必须真有 —— 反过来漏了就是 v0.72 的形状。"""

    def test_record_fact_registered(self):
        assert multiagent.FACT_RECORD in multiagent.root_tool_names()

    def test_get_scan_fact_registered(self):
        assert multiagent.FACT_GET in multiagent.root_tool_names()
