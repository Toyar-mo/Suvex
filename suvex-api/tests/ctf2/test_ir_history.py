"""攻防知识互查（v0.41，会话 B 工单）。

## 这个文件守什么

工单（m00878 会话 B）的三条要求逐条对应：

1. **按资产查既有 Findings/方法卡，作为根因假设优先级输入** ——
   `test_recall_hits_*`；
2. **查不到要显式说明「无历史记录」，不能静默当成没这功能** ——
   `test_no_history_says_so_explicitly`；
3. **历史只做优先级提示，不能代替验证 / 不能自动判 confirmed** ——
   `test_history_block_forbids_auto_confirm` +
   `test_history_does_not_touch_verdict`。

## ★ 为什么这些测试不跑真实解题循环

工单要求的"跑两次复盘任务确认假设顺序"需要 strix + docker，本机都没有
（`which strix` 失败）。但这个功能的风险**不在执行层**，而在两处纯逻辑：

- 资产怎么从题面里猜出来（猜错的代价是查不到，不是错误结论）；
- 历史段落怎么措辞（写错一句就会让模型把历史当结论）。

这两处都是纯函数，能完整测。执行层那一半在 `prompts/ctf2_ir.md` 的
§四 两道闸里，靠 `test_ir_scenarios.py` 的提示词测试守着。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.ctf2 import history
from app.findings.models import Finding
from app.scans.models import Scan
from tests.factories import make_scan

# ══════════════════════════════════════════════════════════
# 造数据
# ══════════════════════════════════════════════════════════


def _make_finding(
    db: Session,
    scan: Scan,
    *,
    title: str,
    target: str,
    severity: str = "high",
    category: str = "broken_access_control",
    verification: str = "verified",
    status: str = "new",
) -> Finding:
    """直接造一条 `findings` 行。

    不走 `import_from_run`（那要造 `vulnerabilities.json` 与 strix 形状的
    记录）：本文件测的是**查询与渲染**，不是导入。用导入造数据会让一个
    导入侧的字段改名把这里的测试也带红，而那两条链路没有共同的失败模式。
    """
    finding = Finding(
        # ⚠️ 用 `new_id` 而不是拼 title：同长度标题 + 同严重度会撞成
        #    同一个主键，而报错看起来像"工厂写错了"而不是"测试想造 6 条"。
        id=new_id("fnd"),
        scan_id=scan.id,
        source_ref=f"vuln-{title}",
        title=title,
        target=target,
        severity=severity,
        category=category,
        verification=verification,
        status=status,
        discovered_at=now_utc(),
        # ★ 这两个字段**故意填上攻击载荷**，用来验证它们不进 prompt。
        #   见 `history` 模块 docstring 的约束 3。
        detail_evidence="GET /debug/env -> 200\nAWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE",
        detail_reasoning="鉴权中间件注册在路由之后，所以先命中路由。",
        poc_code="import requests; print(requests.get('https://x/debug/env').text)",
    )
    db.add(finding)
    db.commit()
    return finding


# ══════════════════════════════════════════════════════════
# 1. 资产识别
# ══════════════════════════════════════════════════════════


def test_asset_hint_prefers_ipv4_with_port() -> None:
    """题面里的 `IP:端口` 优先于裸 IP。

    理由（见 `asset_hint` docstring）：`findings.target` 对 `service`
    形态存的就是 `host:port`，带上端口命中率更高。
    """
    hint = history.asset_hint("目标服务器：10.20.30.40:8080，请复盘入侵过程。")
    assert hint == "10.20.30.40:8080"


def test_asset_hint_drops_default_port() -> None:
    """`host:80` / `host:443` 退回裸 host。

    `findings.target` 里对这类目标通常存成不带端口的 URL，
    带上端口反而查不到 —— 这是"精确匹配反而更差"的一个真实情形。
    """
    assert history.asset_hint("访问 http://10.1.2.3:80/admin") == "10.1.2.3"
    assert history.asset_hint("https://10.1.2.3:443/") == "10.1.2.3"


def test_asset_hint_falls_back_to_url_host() -> None:
    """没有 IPv4 时，从 URL 里取主机名。"""
    hint = history.asset_hint("请复盘 https://web01.corp.example 上的入侵。")
    assert hint == "web01.corp.example"


def test_asset_hint_skips_loopback_and_version_like() -> None:
    """回环地址与"版本号形状"的数字都不算资产。

    `127.0.0.1` 在题面里基本都指"沙箱自己"，拿它去查会把所有本地
    测试留下的 finding 捞回来 —— 那是噪声。
    """
    assert history.asset_hint("在 127.0.0.1 上复现") == ""
    # 三段式版本号不会被 `_IPV4` 匹配（正则要求四段），所以只剩空串。
    assert history.asset_hint("升级到 v1.2.3 后出现异常") == ""


def test_asset_hint_empty_on_no_signal() -> None:
    assert history.asset_hint("日志里有可疑登录。", "", "") == ""


# ══════════════════════════════════════════════════════════
# 2. 查询：命中 / 不命中 / 过滤已修
# ══════════════════════════════════════════════════════════


def test_recall_hits_by_target(db_session: Session) -> None:
    """按资产标识查得到历史发现（工单要求 1）。"""
    scan = make_scan(db_session, status="completed")
    _make_finding(
        db_session, scan, title="调试接口未授权可读环境变量", target="10.20.30.40:8080"
    )

    items = history.recall_for_asset(db_session, "10.20.30.40:8080")

    assert len(items) == 1
    assert items[0]["title"] == "调试接口未授权可读环境变量"
    assert items[0]["severity"] == "high"


def test_recall_returns_empty_for_unknown_asset(db_session: Session) -> None:
    """没打过这个资产就返回空列表 —— 不是异常。"""
    scan = make_scan(db_session, status="completed")
    _make_finding(db_session, scan, title="X", target="10.9.9.9")

    assert history.recall_for_asset(db_session, "10.20.30.40") == []


def test_recall_excludes_fixed_and_false_positive(db_session: Session) -> None:
    """`fixed` / `false_positive` 的历史发现不进上下文。

    ★ 这是本模块唯一一处主动过滤，理由见 `recall_for_asset` docstring：
    它们恰恰是"这条路已经堵上了"的证据，当假设优先级会引导模型去查一个
    不存在的口子。
    """
    scan = make_scan(db_session, status="completed")
    _make_finding(
        db_session, scan, title="已修好的口子", target="10.20.30.40", status="fixed"
    )
    _make_finding(
        db_session,
        scan,
        title="误报",
        target="10.20.30.40",
        status="false_positive",
    )

    assert history.recall_for_asset(db_session, "10.20.30.40") == []


def test_recall_orders_by_severity(db_session: Session) -> None:
    """严重度高的排前面 —— 它更该被先查。

    ⚠️ 刻意不按字符串排（`info` < `low` < `medium` 的字典序毫无意义），
    见 `_severity_rank`。
    """
    scan = make_scan(db_session, status="completed")
    for sev, title in [
        ("info", "缺安全响应头"),
        ("critical", "未授权 RCE"),
        ("low", "目录列举"),
    ]:
        _make_finding(db_session, scan, title=title, target="10.5.5.5", severity=sev)

    items = history.recall_for_asset(db_session, "10.5.5.5")

    assert [item["severity"] for item in items] == ["critical", "low", "info"]


def test_recall_caps_at_three(db_session: Session) -> None:
    """最多 3 条（`MAX_HISTORY_ITEMS`）—— 更多会让历史淹没本次证据。"""
    scan = make_scan(db_session, status="completed")
    for i in range(6):
        _make_finding(
            db_session, scan, title=f"问题{i}", target="10.6.6.6", severity="medium"
        )

    assert len(history.recall_for_asset(db_session, "10.6.6.6")) == history.MAX_HISTORY_ITEMS


def test_recall_empty_asset_returns_empty(db_session: Session) -> None:
    """空资产不查库（防一次 `LIKE '%%'` 全表扫描）。"""
    assert history.recall_for_asset(db_session, "   ") == []


# ══════════════════════════════════════════════════════════
# 3. 渲染：★ 查不到要说出来
# ══════════════════════════════════════════════════════════


def test_no_history_says_so_explicitly() -> None:
    """★ 查不到时**必须显式说「没有历史记录」**（工单要求 2）。

    不能返回空串：模型看到"没有这一段"和看到"明确说没有历史"，是两件
    不同的事。前者它不知道自己有没有这个功能。
    """
    block = history.render_history_block("10.0.0.1", [])

    assert block.strip(), "查不到时不能返回空串"
    assert "没有历史记录" in block
    assert "10.0.0.1" in block
    # 还要告诉它该怎么做，而不只是"没有"。
    assert "完全从本批证据出发" in block


def test_no_history_without_asset_still_explains() -> None:
    """连资产都没识别出来时，也要说明（措辞指向"未能识别"）。"""
    block = history.render_history_block("", [])

    assert "没有历史记录" in block
    assert "未能识别" in block


def test_history_block_forbids_auto_confirm() -> None:
    """★ 历史段落必须写明「不得自动判 confirmed」（工单要求 3）。"""
    block = history.render_history_block(
        "10.0.0.1",
        [{"title": "未授权访问", "severity": "high", "category": "broken_access_control", "verification": "verified", "target": "10.0.0.1"}],
    )

    # 声明必须在段**尾**（模型对最后读到的那句权重最高）。
    assert block.rstrip().endswith("那本身就是一条值得写进报告的结论。")
    assert "不是本次的结论" in block
    assert "不构成" in block
    assert "confirmed" in block
    # 必须指出该怎么做，而不只是"别盲信"。
    assert "本次任务的工具输出" in block


def test_history_block_renders_evidence_grade(db_session: Session) -> None:
    """三级证据强度都要如实写出来 —— 「未验证」不该被当成确定事实。"""
    block = history.render_history_block(
        "10.0.0.1",
        [
            {"title": "A", "severity": "high", "category": "xss", "verification": "verified", "target": "t"},
            {"title": "B", "severity": "medium", "category": "xss", "verification": "partial", "target": "t"},
            {"title": "C", "severity": "low", "category": "xss", "verification": "none", "target": "t"},
        ],
    )

    assert "已复现" in block
    assert "部分验证" in block
    assert "未验证" in block


# ══════════════════════════════════════════════════════════
# 4. ★ 攻击载荷不进 prompt（注入路径安全）
# ══════════════════════════════════════════════════════════


def test_history_never_leaks_attack_payload(db_session: Session) -> None:
    """`detail_evidence` / `detail_reasoning` / `poc_code` 一律不进上下文。

    ★ 这是与 `notes.py` 里「flag 绝不入库」同一条纪律：历史卡片会拼进
    **另一次任务**的 prompt，所以它能带的字段是白名单，不是黑名单。
    """
    scan = make_scan(db_session, status="completed")
    _make_finding(
        db_session, scan, title="调试接口未授权", target="10.20.30.40:8080"
    )

    text, asset, count = history.recall_for_challenge(
        db_session, title="复盘 10.20.30.40:8080 的入侵"
    )

    assert count == 1
    assert asset == "10.20.30.40:8080"
    # 白名单字段在。
    assert "调试接口未授权" in text
    # ★ 载荷全都不在。
    assert "AKIAIOSFODNN7EXAMPLE" not in text
    assert "AWS_SECRET_ACCESS_KEY" not in text
    assert "requests.get" not in text
    assert "鉴权中间件注册在路由之后" not in text


# ══════════════════════════════════════════════════════════
# 5. ★ 与 verdict 链路完全解耦
# ══════════════════════════════════════════════════════════


def test_history_does_not_touch_verdict() -> None:
    """`history` 模块**不产生任何 verdict**（工单要求 3 的代码层保证）。

    ## 为什么用 AST 而不是 grep 源码

    第一版写成 `assert "resolve_verdict" not in source`，当场就红了 ——
    因为模块 docstring 里**正文引用**了那个名字来解释"本模块不碰它"：
    「本模块**不产生任何 verdict**，也不碰 `resolve_verdict`」。
    散文里提到一个名字，和代码里调用它，是两件事。

    所以这里走 AST：只看**真的被加载/调用的标识符**与**真的存在的
    属性访问**，docstring 与注释不参与。这比 grep 精确，也比
    "跑一次看看"可靠 —— 后者只覆盖跑过的那条分支。
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(history))

    # 收集所有被当作名字用的标识符（Name.id）与被调用的属性名（Call.func）。
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.rsplit(".", 1)[-1])

    # 1. 不定级、不写卡。
    for forbidden in ("resolve_verdict", "counts_as_solved", "build_note_fields"):
        assert forbidden not in names, f"history 模块不该用到 {forbidden}"

    # 2. 不写库。`session.commit()` / `db.add(...)` 会以 Attribute 形式出现，
    #    已经在 names 里了；这条断言把它们单独点出来，好让失败的报错可读。
    for forbidden in ("commit", "add", "flush", "delete"):
        assert forbidden not in names, f"history 模块不该调用 {forbidden}()"


def test_recall_for_challenge_three_tuple_shape(db_session: Session) -> None:
    """返回值固定三元组 `(段落, 资产, 条数)`，且段落恒非空。

    少走一步（比如忘了渲染直接塞空串）就会静默退化成"没有这个功能"，
    所以函数的返回形状本身要被钉住。
    """
    text, asset, count = history.recall_for_challenge(
        db_session, title="复盘 10.20.30.40 的入侵"
    )

    assert isinstance(text, str) and text.strip()
    assert asset == "10.20.30.40"
    assert count == 0  # 库里没有这个资产
    assert "没有历史记录" in text


def test_recall_for_challenge_uses_brief(db_session: Session) -> None:
    """题面里没有资产时，从 `brief`（人工线索）里猜。"""
    scan = make_scan(db_session, status="completed")
    _make_finding(db_session, scan, title="SSRF", target="172.16.8.8", severity="high")

    text, asset, count = history.recall_for_challenge(
        db_session, title="复盘一次入侵", brief="受害主机是 172.16.8.8"
    )

    assert asset == "172.16.8.8"
    assert count == 1
    assert "SSRF" in text
