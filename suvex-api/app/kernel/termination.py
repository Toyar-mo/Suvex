"""结束条件外置（v0.75，学 BreachWeave 的"结束条件外置"）。

## 要解决的问题

现在的循环终止条件只有三种：模型写了 `FINAL:`、轮次用尽、外部停止。
第一种是**模型主观判断**，而它会犯两类相反的错：

1. **过早收敛**——只测了 3 个接口、发现 1 个小问题就说"扫完了"。
   复杂任务里这是最常见的失败：模型上下文一重、探索一慢，就倾向于
   给自己一个体面的结束理由。
2. **不肯收口**——开着五条没跑完的线，把预算烧在最后一条上，
   结论一个字没写。

P1 对照里 agent 与 strix 各自犯过其中一种：agent 28 分钟铺了 17 个面
但其中一条是误报（证据不足就下了结论）；strix 71 分钟死磕 1 个点，
87 个端点里 80 个从未验证。

## 判定放在编排层，不放在模型侧

模型只能说"我认为可以结束了"，**能不能结束由这里的纯函数决定**。
判据只有两条，都可由代码数出来：

- **覆盖率**：`coverage` 里记过的 surface 条数少于下限 → 不许结束；
- **预算**：剩的轮次还够、但已交的结论为 0 → 不许结束。

刻意**不做**的事：不判断"结论对不对"（那需要人），不替模型决定方向
（那是 root 的活）。只回答一个问题：**现在结束，是不是太早？**

## 为什么是纯函数

`decide(observation) -> Decision` 没有任何 IO、不碰 DB、不看时间。
这样它能在单测里被穷举，也能被复述给人听：

    "你为什么不让它结束？" → "因为 coverage 只有 2 条，下限是 5。"

而不是"因为模型感觉还没完"。答辩时这个区别就是可解释性。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 允许收口前至少要看过的 surface 数。
#:
#: 5 是实测校准的：P1 那轮 agent 在 28 分钟内看了 17 个面、
#: strix 深挖 1 个点。真实目标（Pyrgos 201 个端点 / Suvex 5 个未鉴权面）
#: 都远多于 5，所以这个下限不会误伤"确实扫完了"的情况；
#: 而只看了 1~2 个面就收口的，一定是没扫完。
MIN_COVERAGE = 5

#: 允许收口前至少要有几条"有证据的结论"（positive observation）。
#:
#: 1 而不是 0：完全没有任何产出就收口，要么目标真的干净（那也该把
#: "查了哪些面、都是干净的"写成 coverage），要么根本没查（coverage 也少）。
#: 这两种情况都由 MIN_COVERAGE 兜住，所以这里只要 1。
MIN_FINDINGS = 1


@dataclass(frozen=True)
class Observation:
    """编排层能数出来的运行态事实（全部来自代码，不来自模型自述）。"""

    #: `coverage` 里记过的 surface 条数。
    coverage_count: int = 0
    #: 已登记的阳性结论条数（含 L1/L2）。
    finding_count: int = 0
    #: 已用轮次 / 总预算。
    turns_used: int = 0
    total_budget: int = 0
    #: 模型是否已经写了 `FINAL:`。
    model_claims_done: bool = False
    #: 最近若干轮里，"对目标发起的交互"（区分于 grep 本地文件、写脚本这类
    #: **不碰目标**的轮次）占多少。`None` = 调用方没统计，不参与判定。
    #:
    #: ★ 为什么需要这个：2026-10-23 的 P2 真跑里 root 用 63 轮只探了
    #:   4 个端点（scans 13 次 / login 12 次 / state 9 次 / health 6 次），
    #:   42% 的轮次花在 grep 前端 bundle 和写解析脚本上 —— **一次请求
    #:   都没发**。当时的门只说"覆盖 0/5"，模型收到后继续做它正在做的事，
    #:   于是 80 轮预算在"更用力地打同一个点"上耗尽。
    #:   所以门必须能区分"没干活"和"干错了活"：前者补动力，后者要换向。
    acting_ratio: float | None = None

    @property
    def remaining_turns(self) -> int:
        return max(0, self.total_budget - self.turns_used)

    @property
    def stalled(self) -> bool:
        """是否在原地打转：轮次花了不少，但绝大多数没落到目标身上。

        阈值 0.6 是留了余量的：正常扫描里允许有一半以下的轮次用于
        读文档、写脚本、分析响应（这些是必要劳动），但**低于四成在打目标**
        时，继续给它"还差什么"的提示只会让它更用力地打同一个点。
        """
        return self.acting_ratio is not None and self.acting_ratio < 0.6

    @property
    def budget_exhausted(self) -> bool:
        return self.remaining_turns <= 0


@dataclass(frozen=True)
class Decision:
    """能不能现在结束。`allow=False` 时 `reason` 必须能被人读懂。"""

    allow: bool
    reason: str
    #: 该给模型补的提醒（空串=不用说什么）。
    nudge: str = ""
    #: 缺什么，给日志/界面用。
    missing: list[str] = field(default_factory=list)


def decide(obs: Observation, *, min_coverage: int = MIN_COVERAGE) -> Decision:
    """判定"现在能不能收口"。

    顺序有意义：**预算用尽时一律放行**。这不是"扫完了"，而是"没预算了"——
    硬拦着只会让循环空转，最后一条结论都交不出来（v0.73 修的就是这个：
    跑满墙钟被取消，已确认发现全丢）。
    """
    if obs.budget_exhausted:
        return Decision(
            allow=True,
            reason=f"总轮次预算已用尽（{obs.turns_used}/{obs.total_budget}），必须收口交结论。",
        )

    if not obs.model_claims_done:
        # 模型还没说要结束 —— 这个门只在它想结束时才有话说。
        return Decision(allow=True, reason="模型尚未请求收口，不干预。")

    missing: list[str] = []
    if obs.coverage_count < min_coverage:
        missing.append(f"覆盖 {obs.coverage_count}/{min_coverage} 个面")
    if obs.finding_count < MIN_FINDINGS:
        missing.append(f"有证据的结论 {obs.finding_count}/{MIN_FINDINGS} 条")

    if not missing:
        return Decision(
            allow=True,
            reason=(
                f"允许收口：覆盖 {obs.coverage_count} 个面、"
                f"{obs.finding_count} 条结论，剩余 {obs.remaining_turns} 轮。"
            ),
        )

    detail = "；".join(missing)
    return Decision(
        allow=False,
        reason=f"拒绝收口：{detail}（剩余 {obs.remaining_turns} 轮还够用）。",
        nudge=_nudge(missing, obs),
        missing=missing,
    )


def _nudge(missing: list[str], obs: Observation) -> str:
    """拒绝收口时给模型的话。

    只说**还差什么**，不说"你要更努力"——后者是废话，模型只会继续
    重复它已经在做的动作。

    ⚠️ 措辞不提"覆盖率/预算"这些内部指标：模型不需要知道我们在数什么，
    也不需要学会"把 coverage 刷到 5 条就能收工"。所以这里写的是
    **具体的、要去做的动作**。

    ★ 原地打转时**先换向、再补覆盖**。2026-10-23 的 P2 真跑暴露：
    root 收到"覆盖 0/5"之后，把剩下的轮次继续花在同一个端点上
    （scans 13 次 / login 12 次），因为它把这理解成"我做得还不够多"。
    真正的问题不是不够多，是**太窄**。所以这种情况下第一条建议必须是
    "换一类动作"，而不是"再多测几个"。
    """
    lines = ["还不能收口 —— 下面这些还没做到："]
    if obs.stalled:
        # 换向提示排在最前：它改变的是做事方式，覆盖数会随之自然变化。
        lines.append(
            "- 最近的轮次里，只有不到四成真的向目标发了请求，大部分花在"
            "读本地文件、写脚本、解析响应上。**不是做得不够多，是打得太窄**："
            "同一批入口反复试探不会得到新结论。"
        )
        # ★ v0.77：换向之前先自查「漏读」（学腾讯队的顺序：漏读 → 换思路）。
        #
        # 那支队伍的纪律原文：
        #   > 优先自查"漏读"（同一思路跑 2+ 轮无果，先做这步，**不要换思路**）
        #   > ✓ 响应细节漏读：HTML 注释、HTTP header (Set-Cookie/Server/X-*/Location)、
        #   >   JS 源码、错误页堆栈、robots.txt、sitemap、favicon hash
        #   > ✓ 文件漏读：打包回来的源码/配置是否全 grep 过？
        #   > ✓ 日志里是不是忽略了某个异常响应/非预期状态码？
        #
        # 顺序很重要：直接换思路会把"已经在手里的线索"丢掉重来，
        # 而漏读的补救成本远低于重新侦察。
        lines.append(
            "  **先自查漏读**（这是最划算的一步，比换思路便宜得多）：\n"
            "  - 响应细节：HTTP 头（`Set-Cookie` / `Server` / `X-*` / `Location`）、"
            "HTML 注释、JS 源码里的路由与常量、错误页堆栈、`robots.txt`、`sitemap.xml`\n"
            "  - 已经拿到手的东西：你 curl 下来的页面/JS/配置，**真的全 grep 过了吗**\n"
            "  - 异常响应：有没有哪个状态码/响应体你觉得奇怪但跳过了（3xx 去哪了、"
            "405 说明存在但方法不对、500 说明后端崩了）"
        )
        lines.append(
            "  **补完漏读仍无进展，再换方向** ——"
            "换一个身份（已登录/未登录/另一个用户）、换一个入口（网页里"
            "点得到但你没测过的功能）、换一层（HTTP 之下的协议、文件上传、"
            "批量导入这类入口），或者去测一个你现在能访问但没测过的接口。"
        )
    if any("覆盖" in m for m in missing):
        lines.append(
            f"- 你只测了 {obs.coverage_count} 个位置。请把还没碰过的入口也走一遍，"
            "每测完一个位置用 `record_coverage` 记一条（干净也要记）。"
        )
    if any("结论" in m for m in missing):
        lines.append(
            "- 手上还没有一条带对照实验的结论。至少给出一个能复核的对比："
            "换一个身份 / 换一个源 / 改一次配置，看结果是否随之改变。"
        )
    lines.append(
        f"- 还有 {obs.remaining_turns} 轮，够用。做完上面这些再写 `FINAL:`。"
    )
    return "\n".join(lines)
