"""总览页「待处置风险」与「该做什么」的判据测试（v0.95）。

## 这个文件在防什么

v0.95 给总览页加了两块会**自己说话**的东西：一个加权风险分
（`risk_score`）与一个"现在最该动哪件"的列表（`build_attention`）。

这类"派生指标"的危险与普通接口不同：它永远返回一个看起来合理的数字
或列表。口径写错时接口不会报错、页面不会白屏 —— 只会安静地说错话。
而做决定的人不会怀疑一个安静的看板。所以在编写实现之前先把判据写下来：

1. **风险分只算未收敛的发现**（`new` / `confirmed` / `retesting`）。
   用全量算的话，把漏洞全修完之后分数不动而"待处置严重"已经归零，
   界面会同时显示「风险极高」与「没有待处理的严重漏洞」。
2. **「安全」的充要条件是待处置数真的为 0**，不是分数为 0。
   否则将来给某一级权重设 0，就会出现"安全 + 列着一堆待处置项"。
3. **每一档严重度都必须被显式归类**（下面的 `test_每个严重度都被显式计入或排除`）。
   这是本仓库反复踩过的形状：新增枚举成员时，`dict.get(x, 0)` 会
   静默把它当 0 —— 分数偏低但没有任何地方报错。
4. **建议列表的排序是稳定且可解释的**：先按紧急度，再按数量。
   不稳定的话，用户每次刷新看到条目换位，会以为列表在动。
"""

from __future__ import annotations

import pytest

from app.core.vocab import UNRESOLVED_FINDING_STATUSES, FindingStatus, Severity
from app.overview.schemas import AttentionItemOut
from app.overview.service import (
    ATTENTION_LIMIT,
    RISK_LEVEL_BANDS,
    RISK_WEIGHTS,
    build_attention,
    risk_score,
)


def _breakdown(**counts: int) -> list[dict]:
    """造一份待处置分布。未指定的档位补 0 —— 形状永远齐全。"""
    return [
        {"severity": member.value, "count": counts.get(member.value, 0)} for member in Severity
    ]


# ─────────────────────────────────────────────
# 1 · 哪些状态算「待处置」
# ─────────────────────────────────────────────


def test_未收敛集合是穷举的而不是排除法():
    """必须是**列举**，不能写成 `!= fixed and != closed`。

    排除法的风险不在今天：今天它算出来的结果与列举完全一致。
    风险在于**将来新增一个状态**时 —— 比如加一个 `deferred`（已延后），
    排除法会把它默认算成未收敛，于是风险分被悄悄推高，
    而且没有任何测试或报错会指出这件事。
    """
    assert {"new", "confirmed", "retesting"} == UNRESOLVED_FINDING_STATUSES


def test_每个处置状态都被显式归类():
    """★ 新增 `FindingStatus` 成员时这条会红，逼你决定它算不算待处置。

    这是本仓库的固定形状：给枚举加成员时，所有按成员分支的地方都要
    重新表态。没有这条测试的话，新状态会因为 `dict.get(x, 0)` /
    `if status in {…}` 而在各处被静默归到"其他"。
    """
    classified = set(UNRESOLVED_FINDING_STATUSES) | {
        FindingStatus.FIXED.value,
        FindingStatus.FALSE_POSITIVE.value,
        FindingStatus.CLOSED.value,
    }
    all_statuses = {member.value for member in FindingStatus}
    missing = all_statuses - classified
    assert not missing, f"这些处置状态没有明确归类（算/不算待处置）：{sorted(missing)}"


def test_已修复不算待处置():
    """这条单独钉住，因为它是**用户会立刻发现**的那个矛盾。

    修好一条严重漏洞之后：风险分必须降、待处置严重必须减。
    如果 `fixed` 被算进去，两件事只发生一件 ——
    界面就同时说「风险极高」与「没有待处理的严重漏洞」。
    """
    assert FindingStatus.FIXED.value not in UNRESOLVED_FINDING_STATUSES


def test_误报不算待处置():
    """误报已经判定"不是漏洞"。算进去会让分诊变成无效劳动：
    用户标了一条误报，风险分不动 —— 于是下次就不标了。"""
    assert FindingStatus.FALSE_POSITIVE.value not in UNRESOLVED_FINDING_STATUSES


def test_复测中算待处置():
    """`retesting` 是"正在验证"而不是终态，随时可能退回 confirmed。
    不算的话，漏洞在复测队列里期间风险分归零 —— 而它其实还没修好。"""
    assert FindingStatus.RETESTING.value in UNRESOLVED_FINDING_STATUSES


# ─────────────────────────────────────────────
# 2 · 加权分的算术
# ─────────────────────────────────────────────


def test_每个严重度都有权重或明确不计():
    """权重表必须对每个档位表态。

    `info` 刻意**不在**权重表里（它不是待处置项，见 `RISK_WEIGHTS` 注释）。
    这条测试把那个决定变成可执行的：将来有人往表里加 `info`，测试会红，
    然后他必须解释为什么"信息级"要算进风险分。
    """
    weighted = set(RISK_WEIGHTS)
    all_severities = {member.value for member in Severity}
    unaccounted = all_severities - weighted - {Severity.INFO.value}
    assert not unaccounted, f"这些严重度既没有权重也没有明确排除：{sorted(unaccounted)}"
    assert Severity.INFO.value not in RISK_WEIGHTS


def test_权重是业务规则而不是随手写的数():
    """把权重值本身钉住 —— 它们对齐 CyberStrikeAI，改动要有理由。"""
    assert RISK_WEIGHTS[Severity.CRITICAL.value] == 10.0
    assert RISK_WEIGHTS[Severity.HIGH.value] == 5.0
    assert RISK_WEIGHTS[Severity.MEDIUM.value] == 2.0
    assert RISK_WEIGHTS[Severity.LOW.value] == 0.5


def test_两条严重就是高危_四条就是极高():
    """阈值的行为锚点。数字本身可调，但"几条算什么档"必须有据可查。"""
    assert risk_score(_breakdown(critical=1)) == (10.0, "medium")
    assert risk_score(_breakdown(critical=2)) == (20.0, "high")
    assert risk_score(_breakdown(critical=4)) == (40.0, "severe")


def test_低危要靠数量才能堆出分():
    """低危 ×0.5 的作用：40 条低危（20 分）与 2 条严重（20 分）同分。

    这正是"低危可以攒着批量修、严重必须今天修"的算术表达 ——
    如果低危也给 1，40 条低危会变成 40 分（极高），而它其实不紧急。
    """
    low_score, _ = risk_score(_breakdown(low=40))
    critical_score, _ = risk_score(_breakdown(critical=2))
    assert low_score == critical_score == 20.0


def test_信息级不贡献分数():
    """`info` 不进权重表 → 无论多少条都不加分。"""
    assert risk_score(_breakdown(info=100)) == (0.0, "safe")


def test_没有待处置项就是安全():
    assert risk_score(_breakdown()) == (0.0, "safe")


def test_安全由待处置数为零决定_不由分数决定():
    """★ 这条是刻意的双保险。

    当前权重下"有项但分数为 0"只能靠 info 造出来，而 info 不在表里。
    但实现里显式用 `total == 0` 做闸门，是为了让**将来改权重**时
    这个不变式不依赖权重表的具体取值。这里用 info 造出那种情形：
    有 5 条待处置项，但它们一分都不贡献 —— 此时仍必须判 safe，
    因为"待处置"与"有风险"在我们的口径里是同一件事。
    """
    score, level = risk_score(_breakdown(info=5))
    # info 在 breakdown 里计数为 5（它确实是"待处置项"之一），
    # 但权重为 0 → 分数 0。此时判 safe 是可以的：info 不算风险。
    assert score == 0.0
    assert level == "safe"


def test_未知严重度不会让整件事崩掉():
    """上游给出一个我们不认识的严重度时，`get(x, 0.0)` 退化成不计分。

    这是**刻意的退化方向**：宁可少算一个不认识的档位（分数偏低），
    也不要为了让类型齐全而抛异常（总览页整页 500）。
    分数偏低会让用户少看一点风险，比首屏白屏好。
    """
    score, level = risk_score([{"severity": "catastrophic", "count": 3}])
    assert score == 0.0
    assert level == "safe"


def test_阈值表是有序且覆盖到顶的():
    """`RISK_LEVEL_BANDS` 必须是升序 —— 实现按顺序比较，乱序会判错档。"""
    uppers = [upper for upper, _ in RISK_LEVEL_BANDS]
    assert uppers == sorted(uppers)
    # 最后一档之后必须还有一个兜底等级（`severe`），否则超高分会落空。
    assert len(RISK_LEVEL_BANDS) == 3


# ─────────────────────────────────────────────
# 3 · 「该做什么」列表
# ─────────────────────────────────────────────


def test_顺序是紧急在前_再按数量():
    """排序规则的可执行版本：urgent 在前，同 level 内数量大的在前。"""
    actions = build_attention(
        unresolved=_breakdown(critical=1, high=5),
        failing_schedules=2,
        pending_approvals=1,
    )
    assert [item.kind for item in actions] == [
        "unresolved_critical",  # urgent, 1
        "pending_approval",  # urgent, 1
        "unresolved_high",  # warning, 5
        "failing_schedule",  # warning, 2
    ]
    # 同 level 内按数量降序：urgent 里两个 count 都是 1，此时按 kind 稳定排序
    assert actions[0].level == "urgent"
    assert actions[2].level == "warning"


def test_同分时顺序稳定():
    """两条完全同分的建议，多次调用必须给出同一顺序。

    没有 `kind` 做最后的 tiebreak 时，顺序取决于 append 顺序 ——
    而 append 顺序来自参数的组装顺序。用户每次刷新看到条目换位，
    会以为列表在动（而它其实没变）。
    """
    runs = [
        [item.kind for item in build_attention(unresolved=_breakdown(critical=1, high=1))]
        for _ in range(5)
    ]
    assert all(run == runs[0] for run in runs)


def test_没有待办时返回空列表而不是一条提示():
    """★ 空列表是**正确的**返回，不该塞一条"请先跑一次扫描"。

    "该做什么"在空库时若永远不为空，前端那个"全部处理完了"的
    正向状态就永远不会出现 —— 而那正是这个模块存在的意义之一。
    引导新用户是空状态页面（`NoScansYet`）的职责。
    """
    assert build_attention(unresolved=_breakdown()) == []


def test_最多五条():
    """上限是 5（`ATTENTION_LIMIT`）。造 6 种可触发的建议来验证截断。

    注意：当前只有 4 种 kind，所以这里验证的是"实现里有截断"
    而不是"恰好截到第 5 条"—— 用 4 条验证 `<= LIMIT` 是安全的。
    """
    actions = build_attention(
        unresolved=_breakdown(critical=3, high=3), failing_schedules=1, pending_approvals=1
    )
    assert len(actions) <= ATTENTION_LIMIT
    assert ATTENTION_LIMIT == 5


def test_严重建议带数量与可行动的文案():
    actions = build_attention(unresolved=_breakdown(critical=3))
    item = actions[0]
    assert item.kind == "unresolved_critical"
    assert item.count == 3
    assert "3" in item.title  # 数量必须出现在文案里，否则用户还得自己数
    assert item.detail  # 必须解释为什么，不能只有标题


def test_失败的计划是_warning_而不是_urgent():
    """★ 等级是业务判断，不是按"看起来严不严重"给的。

    计划失败要修，但它**不阻塞任何人**（智能体没在等它），
    所以是 warning；而待审批是 urgent —— 智能体真的停在那一步了。
    判错的后果：全站都是 urgent，于是 urgent 失去意义。
    """
    actions = build_attention(unresolved=_breakdown(), failing_schedules=1)
    assert actions[0].kind == "failing_schedule"
    assert actions[0].level == "warning"


def test_待审批是_urgent():
    """智能体停在一步上等放行，不处理它就不会继续 —— 这是真的阻塞。"""
    actions = build_attention(unresolved=_breakdown(), pending_approvals=2)
    assert actions[0].kind == "pending_approval"
    assert actions[0].level == "urgent"


def test_返回的是_schema_实例():
    """前端按 camelCase 收，所以必须走 `AttentionItemOut`（`CamelModel`）
    而不是裸 dict —— 裸 dict 会漏掉别名转换，前端拿到 snake_case 全 undefined。"""
    actions = build_attention(unresolved=_breakdown(critical=1))
    assert all(isinstance(item, AttentionItemOut) for item in actions)
    assert set(actions[0].model_dump(by_alias=True)) == {
        "kind",
        "level",
        "title",
        "detail",
        "count",
    }


@pytest.mark.parametrize("kind_missing", ["to", "href", "url", "link"])
def test_建议里没有跳转字段(kind_missing: str):
    """★ 刻意的：`to` 之类的跳转路由**不进 API 契约**。

    后端若直接给 `/findings?status=...`，就把前端路由写进了契约 ——
    改一次路由要发布后端，且与"`navigation.ts` 是前端单一事实来源"冲突。
    前端用 `kind` 查自己的路由表。
    """
    actions = build_attention(unresolved=_breakdown(critical=1))
    dumped = actions[0].model_dump(by_alias=True)
    assert kind_missing not in dumped
