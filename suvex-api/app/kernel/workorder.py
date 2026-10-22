"""处置建议 → 可人工审查的**工单**（v0.41，会话 C 工单）。

## 这个模块的全部职责：把处置建议变成**文件**

它**不执行**任何动作，也**不提供**任何执行通道 —— 没有防火墙客户端、
没有 EDR 接口、没有 SSH、没有 `subprocess`、没有 `os.system`。
这不是"第一版先不做"，是一条**设计约束**：见下面"为什么不留后门"。

## ★ 为什么只出工单（与 `submit_mode=safe` 是同一条纪律）

`submit_mode` 的默认值是 `safe`：模型产出候选，提交由人确认。理由写在
`settings/models.py` 里 —— 提交会消耗**不可再生**的错误次数额度，
让一个可能猜错的自动程序直接去扣额度，赌的是"我每次都猜对"。

处置动作是同一件事的更严重后果版：

| | 提交 flag | 执行处置 |
| --- | --- | --- |
| 猜错的代价 | 少一次额度 | **切断业务 / 封掉办公出口 / 杀掉正常进程** |
| 可撤销 | 不可撤销但无外部影响 | 往往**不可撤销**，且影响在系统外 |

所以这里的形状与 `submit_mode=safe` 完全同构：**产出结构化建议，
执行永远是人的事**。

## ★★ 为什么不留"将来自动执行"的后门

代码里**不存在**这样的路径，也不存在"预留接口"。理由不是保守，是
**一条后门在未来的使用与现在写它时的意图无关**：

- 留一个 `executor: Callable | None = None` 参数，第一个月的调用点都传
  `None`；半年后某次"要不顺手接一下 EDR"就会传一个真的进来，而那时
  没人会回头读这段 docstring。
- 留一个"dry_run=False 就真发"的分支，同理。

所以本模块的**唯一输出是文本**（markdown 工单），
`test_ir_workorder.py::test_no_execution_path_in_module` 用 AST 盯着这一点。

## 工单为什么要落成**文件**而不是只放报告里

报告是一份叙事文档，人会通读；工单是**待办事项**，人要逐条批、批完
要留痕。两者的生命周期不同：报告归档，工单会被复制进工单系统。
混在一份文档里，等于让人从一篇散文里手工摘出待办 —— 那正是"漏掉一条
处置建议"的常见成因。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)

#: 一份工单最多收几条。与 `MAX_HISTORY_ITEMS` 同一个思路：过多会让
#: "哪条最要紧"消失。而处置建议超过 10 条通常说明分类没做，不是真的
#: 需要 10 个动作。
MAX_WORKORDERS = 10

#: `action` 的长度上限。超过这个长度的"动作"基本是一段分析而不是动作。
MAX_ACTION_CHARS = 300

#: `grade` 允许的取值。★ 只有两档 —— `ruled_out` 不产生处置动作。
#: 见 `prompts/kernel_ir.md` §六。
ALLOWED_GRADES = ("confirmed", "needs_follow_up")

#: 四个必填段。缺任何一段都降级为 `needs_follow_up` 并在工单里写明缺什么。
REQUIRED_FIELDS = ("action", "basis", "risk", "permission")


@dataclass(slots=True)
class WorkOrder:
    """一条处置建议。**字段就是人能审查的全部内容。**"""

    action: str
    basis: str
    risk: str
    permission: str
    grade: str = "needs_follow_up"
    #: 缺了哪几段（空列表 = 四段齐全）。缺段时 `grade` 被强制降级。
    missing: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def approved_grade(self) -> str:
        """★ 缺段一律降级为 `needs_follow_up`。

        见 `prompts/kernel_ir.md` §六：四段缺任何一段都按 `needs_follow_up`
        处理。这里把它变成**代码**而不是只写在提示词里 —— 提示词是
        请求，这里是保证。

        理由：一段缺 `risk` 的工单如果保留 `confirmed`，人审批时看到的
        是"证据充分"，而它没告诉你误做会怎样。级别是给审批人看的
        可信度标签，它必须同时反映证据与**完整性**。
        """
        return self.grade if self.complete else "needs_follow_up"


def _clean(value: object, *, limit: int = 1000) -> str:
    """把任意 JSON 值收敛成一个可读的短字符串。

    ⚠️ 模型实测会把字段写成列表或对象（`"risk": ["a", "b"]`）。这里
    **不猜结构**（与 `extract_final_analysis` 同一条纪律：猜错结构会把
    整块内容按错误形状渲染），只做"能变字符串就变，变不了就丢弃"。
    列表按行拼起来 —— 那是模型写得最多的形状，且拼接不引入歧义。
    """
    if isinstance(value, str):
        return value.strip()[:limit]
    if isinstance(value, (list, tuple)):
        parts = [_clean(item, limit=limit) for item in value]
        return "\n".join(part for part in parts if part)[:limit]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def parse_workorders(payload: dict[str, Any] | None) -> list[WorkOrder]:
    """从 `FINAL` 结论文档里取 `workorders`，容忍残缺。**

    ## 容错策略（与 `extract_final_analysis` 一致）

    - 整个字段缺失 / 不是数组 → 空列表（不是异常）；
    - 某一项不是对象 → 跳过并记 debug 日志（不因一条坏数据丢掉整批）；
    - 某一项缺字段 → **保留**该工单并把缺的字段记进 `missing`，
      而不是丢弃它。理由：一条缺 `risk` 的建议**仍然值得人看**
      （"他们想到了要封这个 IP，只是没写风险"），而丢掉它等于
      让一条真实发现凭空消失。
    - 某一项连 `action` 都空 → 只有这一种情况丢弃。没有动作的工单
      不是工单，是感想。

    ## `grade` 的清洗

    只保留 `ALLOWED_GRADES` 里的取值，其余（包括模型写 `ruled_out`、
    写中文、写大写）一律降为 `needs_follow_up`。理由：`grade` 是给
    审批人看的可信度标签，宁可低估不可高估。
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get("workorders")
    if not isinstance(raw, list):
        return []

    orders: list[WorkOrder] = []
    for item in raw[:MAX_WORKORDERS]:
        if not isinstance(item, dict):
            logger.debug("FINAL.workorders 里有一项不是对象，跳过：%r", type(item))
            continue
        action = _clean(item.get("action"), limit=MAX_ACTION_CHARS)
        if not action:
            # ★ 唯一一种丢弃：没有动作。
            continue
        values = {name: _clean(item.get(name)) for name in REQUIRED_FIELDS}
        missing = [name for name in REQUIRED_FIELDS if not values[name]]
        grade = _clean(item.get("grade"), limit=40).lower()
        if grade not in ALLOWED_GRADES:
            grade = "needs_follow_up"
        orders.append(
            WorkOrder(
                action=action,
                basis=values["basis"],
                risk=values["risk"],
                permission=values["permission"],
                grade=grade,
                missing=missing,
            )
        )
    return orders


#: 缺段的中文说法。用一张表而不是 `if` 链：加字段时只改表，
#: 而 `if` 链会漏掉一处、表现为"缺了段但工单里没写缺哪段"。
_FIELD_LABEL = {
    "action": "动作描述",
    "basis": "依据（关联哪条结论）",
    "risk": "风险提示",
    "permission": "需要的权限",
}


def render_workorders(
    orders: list[WorkOrder],
    *,
    challenge_title: str = "",
    asset: str = "",
) -> str:
    """渲染成一份可交付的 markdown 工单文件。

    ## ★ 文件头必须写明「这不是已执行的动作」

    读这份文件的人可能是两周后接手的人，也可能是审计。他看到一份
    "阻断 203.0.113.45"的清单，最坏的误读是"这些已经做了"。
    所以标题下面第一句就是否定句，且带 `⛔` —— 它在纯文本里也醒目。

    ## 没有建议时**不产出空文件**，而是产出一份说明

    与 `history.render_history_block` 同一条纪律：空文件与"没有建议"
    在读的人眼里是两件事 —— 前者像"生成失败了"。而且 `workorders: []`
    本身是个**结论**（本次复盘没有需要处置的事项），值得写下来。
    """
    lines: list[str] = []
    lines.append("# 处置建议工单")
    lines.append("")
    lines.append(
        "> ⛔ **这是建议，不是已执行的动作。** "
        "本系统没有执行任何处置操作 —— 它只生成这份清单。"
        "每一条都需要人工确认后再由对应权限的同事执行。"
    )
    lines.append("")
    if challenge_title:
        lines.append(f"- 复盘对象：**{challenge_title}**")
    if asset:
        lines.append(f"- 资产标识：`{asset}`")
    if orders:
        confirmed = sum(1 for o in orders if o.approved_grade == "confirmed")
        lines.append(
            f"- 工单数：{len(orders)}"
            f"（证据充分可立即评估 {confirmed} 条 / "
            f"需先补验证 {len(orders) - confirmed} 条）"
        )
    else:
        lines.append("- 工单数：0")
    lines.append("")

    if not orders:
        lines.append("## 本次没有需要处置的事项")
        lines.append("")
        lines.append(
            "复盘结论里没有产生任何处置建议。这**可能是正常结果**"
            "（比如只发现了尝试、没有成功），也可能是证据不足以下结论。"
            "请对照报告的「证据缺口清单」判断属于哪一种 —— "
            "如果缺口里写着「关键日志未覆盖」，"
            "那么「没有建议」的正确理解是**还判断不了**，而不是「没有事」。"
        )
        lines.append("")
        return "\n".join(lines)

    lines.append("## 工单清单")
    lines.append("")
    for index, order in enumerate(orders, start=1):
        grade = order.approved_grade
        marker = "证据充分" if grade == "confirmed" else "需先补验证"
        lines.append(f"### {index}. {order.action}")
        lines.append("")
        lines.append(f"- **置信级别**：`{grade}`（{marker}）")
        if not order.complete:
            lacking = "、".join(_FIELD_LABEL.get(n, n) for n in order.missing)
            lines.append(
                f"- ⚠️ **本工单不完整**，缺：{lacking}。"
                "缺段的工单级别被强制降为 `needs_follow_up`。"
            )
        lines.append("")
        lines.append("| 项 | 内容 |")
        lines.append("| --- | --- |")
        for name, label in _FIELD_LABEL.items():
            value = getattr(order, name)
            # ⚠️ 表格单元格里的裸 `|` 会被拆成两列 —— 与
            #    `scans/writeup.py` 的 `_cell()` 是同一类问题。
            safe = (value or "（未填写）").replace("|", "\\|").replace("\n", "<br>")
            lines.append(f"| {label} | {safe} |")
        lines.append("")

    lines.append("## 人工确认清单")
    lines.append("")
    lines.append(
        "逐条确认下面四点，全部确认后再执行。"
        "**任何一条答不上来就先别做** —— 处置动作往往不可撤销。"
    )
    lines.append("")
    lines.append("1. **依据还成立吗**：那条结论的证据是否仍然有效（时间窗、资产是否还是同一台）？")
    lines.append("2. **误做会怎样**：`risk` 那一栏写了什么？有没有办法先小范围验证？")
    lines.append("3. **谁有权限**：`permission` 那一栏写的是哪个角色？他清楚为什么要做吗？")
    lines.append("4. **做完怎么确认**：动作生效后，用什么信号判断它起了作用？")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*由 Suvex 生成 · 仅建议，不执行*")
    lines.append("")
    return "\n".join(lines)


def filename_for_order(title: str = "") -> str:
    """下载用的文件名。与 `ctf2/writeup.py::filename_for` 同一套清洗规则。"""
    raw = title or "incident"
    safe = "".join(ch if ch.isalnum() or ch in "-_（）()【】" else "_" for ch in raw)
    safe = safe.strip("_")[:80] or "incident"
    return f"IR-WorkOrders-{safe}.md"


#: 工单落盘的目录名（相对 `scan_root`）。
#:
#: 与 `_ctf2_attachments` / `_ctf2_workspaces` / `_ctf2_solves` 同级 ——
#: 沿用的就是"CTF2 的东西都在 scan_root 下的 `_ctf2_*`"这条既有布局。
WORKORDER_DIR = "_ctf2_workorders"


def order_path(challenge_id: str) -> Path:
    """这道题的工单文件路径。

    ## 为什么落盘而不是像题解那样进库

    题解进库是因为它要**在界面上读**（`GET .../writeup` 直接返回
    markdown）。工单的用法不同：它是一份**要拿走的文件** —— 复制进
    工单系统、打印出来批、作为附件发出去。所以它的"家"是磁盘上的
    一个文件，端点只负责把它读出来给你下载。

    ## ⚠️ 一题一份（覆盖写），与题解同理

    重跑一次复盘产出一份新工单；旧的留着只会让"批的是哪一版"
    变成新问题。
    """
    return get_settings().scan_root / WORKORDER_DIR / f"{challenge_id}.md"


def write_order_file(challenge_id: str, markdown: str) -> Path | None:
    """把工单写进磁盘。**失败只记日志并返回 `None`，不抛。**

    理由与 `service.save_writeup` 完全一致：工单是解题结果的**附带产物**，
    它落盘失败不该毁掉一次已经跑完的复盘。调用方拿到 `None` 时要如实
    告诉用户"工单没写成"，而不是假装成功。
    """
    if not markdown.strip():
        return None
    path = order_path(challenge_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
    except OSError:
        logger.warning("工单落盘失败：%s", challenge_id, exc_info=True)
        return None
    return path


def read_order_file(challenge_id: str) -> str | None:
    """读出已落盘的工单。没有文件返回 `None`（不是空串）。"""
    path = order_path(challenge_id)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def count_orders(markdown: str) -> int:
    """从已落盘的工单 markdown 里数出条数。

    ## ⚠️ 数 `### ` 而不是数 `## `

    工单正文里每条用 `### N. 动作` 作标题，而「本次没有需要处置的事项」
    这个**空结果说明**是 `## ` 级。数 `## ` 会把后者也算进去，于是
    交出一份"1 条工单"而实际一条都没有 —— 且这个错在界面上看起来
    完全正常（数字是 1，不像异常值）。

    ## 为什么不让端点自己去数

    工单的版式只有本模块知道（标题层级是 `render_workorders` 定的）。
    让端点再写一份"数标题"的规则，等于把版式约定复制到第二个地方 ——
    改版式时必然漏一处。
    """
    return sum(1 for line in markdown.splitlines() if line.startswith("### "))


__all__ = [
    "ALLOWED_GRADES",
    "MAX_WORKORDERS",
    "REQUIRED_FIELDS",
    "WORKORDER_DIR",
    "WorkOrder",
    "count_orders",
    "filename_for_order",
    "order_path",
    "parse_workorders",
    "read_order_file",
    "render_workorders",
    "write_order_file",
]
