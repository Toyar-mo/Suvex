"""攻防知识互查：把**历史发现**变成复盘任务的假设优先级输入（v0.41）。

## 这个模块解决什么问题

应急响应最难的不是"分析日志"，是**从哪查起**。同一批证据，先查"这台机器
有没有对外开放的未授权接口"和先查"有没有人翻过 /tmp"，花费的时间差一个
数量级 —— 而答案可能都是"没有"。

Suvex 恰好有别人没有的东西：它**自己打过这个资产**。一次渗透评估留下的
`findings` 表里就有"这台机器的 8080 端口不需要认证"这种结论，而那条结论
对复盘的价值极高 —— 它是**已经被人验证过的攻击面**。

所以这个模块做一件很窄的事：复盘开始时按资产标识查一次既有 `findings`，
把结果整理成一小段上下文，追加进 system prompt。

## ★ 三条不能省的约束

### 1. 只做「假设优先级」，**不能**代替验证

历史命中「未授权访问」**不等于**这次入侵就是从这个口子进来的。历史
发现可能是三个月前的、可能已经修了、也可能是另一台机器。

代码层面这条约束怎么落实：本模块**不产生任何 verdict**，也不碰
`resolve_verdict`。它只往 prompt 里塞一段文字，最终定级仍由模型基于
**本次任务**的工具输出给出（见 `prompts/ctf2_ir.md` §四 的两道闸）。

### 2. 查不到要**显式说明**，不能静默当成没这功能

`recall_for_challenge` 永远返回一段非空文本 —— 查不到时那一段写的是
"本次没有历史记录可参考"。理由：模型看到"没有历史段落"和看到
"没有历史记录"是两件事。前者它不知道有没有这个功能，后者它知道
自己该独立推理。而用户复盘时看到的 prompt 里，这两者应当可区分。

### 3. ★ 历史卡片是**跨资产**进上下文的，所以必须脱敏

`findings` 里的 `detail_evidence` 存的是真实响应片段（原样保留不翻译），
而它可能含 cookie / token / 内网路径。这段文字进的是**另一次任务**的
prompt，与 `notes.py` 里「flag 绝不入库」是同一条注入路径安全纪律。
这里只取 `title` / `severity` / `category` / `verification` 四个字段，
**不碰** `detail_evidence` / `detail_reasoning` / `poc_code`。

## 为什么单独一个模块而不是塞进 `runner.py`

`runner.py` 已经 6000 行。而这个模块的依赖方向与 runner 相反：它要
`findings` 的模型，而 `runner` 是解题执行层 —— 把它放在 `runner` 里
会让"解题执行"与"查历史库"耦合，而那正是 `_recall_notes` 刻意
避开的那种耦合（见 `build_spec` 里那段"为什么召回放在这里"的说明）。
"""

from __future__ import annotations

import ipaddress
import logging
import re

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: 一段历史上下文最多带几条。3 条是刻意的：
#: 更多会让"历史"淹没"本次证据"，而复盘的主角永远是本次的日志。
MAX_HISTORY_ITEMS = 3

#: 资产标识的最大长度。与 `findings.target` 的 `String(512)` 对齐后再收紧 ——
#: 拿整段描述去查 `ILIKE %…%` 会让一次 8000 字的 brief 变成全表扫描。
MAX_ASSET_LEN = 64

#: IPv4 或 host:port。刻意**不匹配纯主机名**（`web01` 这类）：
#: 主机名在 CTF 题面里大量出现但几乎都不是可检索的资产标识，
#: 用它去查会把"任意含相同词的 finding"都捞回来 —— 那是噪声不是线索。
_IPV4 = re.compile(r"\b((?:\d{1,3}\.){3}\d{1,3})(?::(\d{1,5}))?\b")

#: `scheme://host[:port]` 或裸 `host:port`，host 允许字母数字与 `.-`。
_URL_HOST = re.compile(
    r"\b(?:https?|ftp)://([A-Za-z0-9._-]{3,253})(?::(\d{1,5}))?", re.IGNORECASE
)


def _private_or_public(raw: str) -> bool:
    """这个 IPv4 字面量是否**看起来像**一个真实资产（而不是版本号）。

    题面里 `1.2.3.4` 既可能是资产也可能是版本号（`v1.2.3.4`）。
    判据：能被 `ipaddress` 解析 **且** 不是 `0.0.0.0`/广播/回环。
    版本号通常落在 "x.y.z" 三段，四段纯数字的概率低到可以忽略，
    而误判的代价只是"查了一次查不到"，不是错误结论。
    """
    try:
        addr = ipaddress.IPv4Address(raw)
    except ValueError:
        return False
    return not (addr.is_loopback or addr.is_unspecified or addr.is_multicast)


def asset_hint(*texts: str) -> str:
    """从题面里**猜**一个资产标识，猜不到返回空串。

    ## 为什么是"猜"而不是要求用户填

    IR 题的分类是 `ir-*`，但没有一个"资产"字段 —— 加字段意味着改
    `Ctf2Challenge` 的表结构与同步流程，而那要动平台接口。所以第一版
    从题面里抽：IPv4 优先（最像资产），其次 URL 里的主机名。

    ⚠️ 猜错了的代价被刻意限制得很小：查不到就是"无历史记录"，
        不会产生错误结论。这是"猜"能被接受的前提。

    ## 为什么优先 IPv4 而不是 URL host

    因为 `findings.target` 里存的是**扫描目标原文**，而扫描的
    `TargetKind` 有 `service`（`host:port`）这种形态 —— IPv4 字面量
    是唯一在所有形态里都保持原样的部分。URL 会带上 scheme 与前缀路径，
    用它去 `ILIKE` 匹配命中率明显更低。
    """
    for text in texts:
        if not text:
            continue
        for match in _IPV4.finditer(text):
            ip = match.group(1)
            if not _private_or_public(ip):
                continue
            port = match.group(2)
            # 带端口的形态更精确（`findings.target` 对 service 目标
            # 存的就是 `host:port`），优先返回它。
            #
            # ⚠️ 但**只在这种情况下**用带端口的：`host:80` 这种默认端口
            #    在 findings 里可能存成不带端口的 URL，带上反而查不到。
            if port and port not in ("80", "443"):
                return f"{ip}:{port}"[:MAX_ASSET_LEN]
            return ip[:MAX_ASSET_LEN]
        for match in _URL_HOST.finditer(text):
            host = match.group(1)
            if host.lower() in ("localhost", "127.0.0.1"):
                continue
            return host[:MAX_ASSET_LEN]
    return ""


def _severity_rank(severity: str) -> int:
    """严重度排序权重。与 `findings/service.py` 里那个 CASE 表达式一致。

    ⚠️ 刻意**不按字符串排**：`critical/high/info/low/medium` 的字典序
    毫无意义（`info` 会排在 `low` 前面）。这里重写一份而不是复用
    SQL 里的 CASE，因为那边的结果是数据库排好的行序，而这里要在
    Python 里对**已取回的少量行**再排一次。
    """
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    return order.get((severity or "").strip().lower(), 5)


def recall_for_asset(db: Session, asset: str) -> list[dict[str, str]]:
    """按资产标识查既有发现，返回最多 `MAX_HISTORY_ITEMS` 条。

    ## 查询形态

    复用 `findings/service.py` 的 `list_findings(q=...)` —— 它的 `q`
    已经会同时匹配 `title` 与 `target`（见那里的
    `Finding.title.ilike(pattern) | Finding.target.ilike(pattern)`）。
    **不新写一条 SQL**：两份"怎么算命中"的实现必然漂移。

    ## 排序：先按严重度，再按时间

    严重度优先是因为"历史未授权访问（high）"比"历史缺安全头（low）"
    更该被先查。同severity 内按 `discovered_at` 倒序 —— 新近的发现
    更可能仍然有效。

    ## 排除已修/误报

    `status` 为 `fixed` 或 `false_positive` 的历史发现**不进上下文**：
    它们恰恰是"这条路已经堵上了"的证据，把它们当假设优先级会引导
    模型去查一个不存在的口子。这是本模块唯一一处主动过滤。

    ## 失败时返回空列表

    与 `_recall_notes` 同一条纪律：查历史是锦上添花，任何失败
    （表不存在、列名变了、DB 抖动）都退化成空，不拦复盘。
    """
    query = (asset or "").strip()
    if not query:
        return []
    try:
        from app.findings.service import list_findings

        rows = list_findings(db, q=query, sort="recent", direction="desc")
    except Exception:  # noqa: BLE001 - 查历史失败不该拦住复盘
        logger.warning("查历史发现失败（本次不带历史上下文）", exc_info=True)
        return []

    # 只取四个**不含攻击载荷**的字段，理由见模块 docstring 的约束 3。
    picked: list[dict[str, str]] = []
    for row in rows:
        if (row.status or "").lower() in ("fixed", "false_positive"):
            continue
        picked.append(
            {
                "title": (row.title or "")[:200],
                "severity": (row.severity or "").strip().lower(),
                "category": (row.category or "").strip().lower(),
                "verification": (row.verification or "").strip().lower(),
                "target": (row.target or "")[:200],
            }
        )
    picked.sort(key=lambda item: _severity_rank(item["severity"]))
    return picked[:MAX_HISTORY_ITEMS]


def render_history_block(asset: str, items: list[dict[str, str]]) -> str:
    """把历史发现渲染成一段可追加进 prompt 的文字。

    ## ★ 永远返回非空

    查不到时写的是「本次没有历史记录可参考」而不是空串 —— 理由见
    模块 docstring 的约束 2。这一段的形状刻意与命中时**同构**
    （都是 `## ` 开头的段落），这样模型不会因为"格式变了"而
    误以为任务类型变了。

    ## ★ 结尾那段"这不是结论"是硬要求

    模型拿到"历史未授权访问"之后最自然的错误是直接把它当根因写进
    报告。所以这一段必须以明确的否定句结尾，且要指出**该怎么做**
    （"按本次证据独立验证"），而不只是"不要盲信"。
    """
    header = "## 历史发现参考（攻防知识互查）"
    if not items:
        return (
            f"\n{header}\n\n"
            f"本次没有历史记录可参考（资产标识：`{asset or '未能识别'}`）。\n\n"
            "这意味着你要**完全从本批证据出发**建立假设，不要假设某类问题"
            "「大概率存在」。这也是正常情况：这个资产可能从没被评估过，"
            "或者评估过但没有命中项。\n"
        )

    lines = [
        f"\n{header}\n",
        f"资产 `{asset}` 在既往评估里留下过下面这些**已确认**的发现。",
        f"共取最近、最严重的 {len(items)} 条：\n",
    ]
    for i, item in enumerate(items, start=1):
        # verification 的中文口径与前端一致：verified=已复现，
        # partial=部分验证，none=未验证。三级都要如实写出来 ——
        # 一条「未验证」的历史发现不该被当成确定事实。
        grade = {
            "verified": "已复现",
            "partial": "部分验证",
            "none": "未验证",
        }.get(item["verification"], item["verification"] or "未标注")
        lines.append(
            f"{i}. **{item['title']}**"
            f"（严重度 {item['severity'] or '未标注'} / "
            f"类别 {item['category'] or '未分类'} / 证据强度 {grade}）"
        )
    lines.append("")
    lines.append(
        "**这些只用来决定「先查什么」，不是本次的结论。**"
        "它们可能已经修复、可能属于另一台机器、也可能是误报。"
        "本次每一条根因/时间线/范围结论都必须由**本次任务的工具输出**"
        "支撑，按 §四 的两道闸给证据分级；历史命中本身**不构成**任何"
        "`confirmed`。如果本次证据与历史发现相反（比如那个口子已经关了），"
        "那本身就是一条值得写进报告的结论。\n"
    )
    return "\n".join(lines)


def recall_for_challenge(
    db: Session, *, title: str = "", description: str = "", brief: str = ""
) -> tuple[str, str, int]:
    """一次调用拿到 `(渲染好的段落, 资产标识, 命中条数)`。

    ## 为什么把三样一起返回

    调用方（`build_spec`）要把段落塞进 `SolveSpec`，同时把"资产是什么"
    与"命中几条"记进日志。分成三个函数会让每个调用点都重复一遍
    "猜资产 → 查 → 渲染"的三步，而少走一步就是静默失效
    （比如忘了渲染、直接塞了空串，那就退化成"没有这个功能"）。

    ## `brief` 也参与猜资产

    人工线索里最常出现的就是真实资产地址（"这台机器是 10.x.x.x"）。
    它比题面描述更可信，所以放在**最后**参与 —— 顺序上题面优先，
    因为题面是平台给的、brief 是人填的，而人可能填错。
    """
    asset = asset_hint(title, description, brief)
    if not asset:
        return render_history_block("", []), "", 0
    items = recall_for_asset(db, asset)
    if items:
        logger.info(
            "为资产 %s 召回了 %d 条历史发现（用于假设优先级）", asset, len(items)
        )
    return render_history_block(asset, items), asset, len(items)
