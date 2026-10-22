"""把一次 CTF2 解题整理成一份**题解（Writeup）**（v0.36 起，v0.38 重做版式）。

## 为什么需要它（而不是复用 `scans/writeup.py`）

`scans/writeup.py` 拼的是 **strix 报告**的重排版。而 CTF2 解题
**根本不走 strix** —— 它由 `runner.solve_in_sandbox` 自己的循环驱动，
从不产生 `penetration_test_report.md`。

所以扫描的题解端点对 CTF2 的题**永远**找不到东西，而它报的
"产物目录可能被手工清理过"在误导用户去找一个不该存在的文件。
详见契约 §3.19.5。

## ★★ v0.38 为什么重做：根因是**内容被丢弃**，不是排版

用户反馈"题解太过简陋"。查下来根因不在渲染层：

`runner.parse_final` 一直能解出完整的结论文档，但收尾时**只取了
`candidates`**，`summary` / `tried` / `ruled_out` 解析完就丢掉了 ——
`SolveOutcome` 当时根本没有对应字段。

实测（`[ACTF2020 新生赛]Upload`）：题解 5873 字符，而模型在 FINAL 里
写了「用响应长度做旁路：baseline=8628；上传 .php→8532…」这样的推理，
在题解里 grep「响应长度」「旁路」「黑名单」**全部 0 次命中**。
那份分析是整篇题解最有价值的部分，全没了。

所以 v0.38 做了三件事：
1. `SolveOutcome` 加 `summary` / `tried` / `ruled_out` 并在收尾处填上；
2. 本模块按下面的版式把它们排进去；
3. 关键步骤的**单条命令**不再截断到 160 字符（见 `STEP_CLIP`）。

## 版式（v0.38）

```
# CTF Writeup · {title}
> Flag / 结果 / 轮数 / 耗时        ← 一眼看到结论
## 一、题目信息                     ← 表格 + 题面原样
## 二、解题思路                     ← FINAL.summary（人话综述）
## 三、解题过程                     ← 关键步骤，完整命令
## 四、尝试与排除
   ### 试过的方向                  ← FINAL.tried
   ### 已排除的可能                ← FINAL.ruled_out
## 五、结论                         ← flag / 候选 / 卡在哪
## 附录 A · 用到的工具
## 附录 B · 资源消耗
## 附录 C · 完整过程纪要           ← 原始流水挪到附录
```

## 内容从哪来：`SolveOutcome`

| 来源 | 变成题解的哪一段 |
| --- | --- |
| 题目信息（标题/分类/描述） | 第一节 |
| `summary` | 第二节（★ v0.38） |
| `transcript` 的 tool 行 | 第三节（复用 `notes.key_steps_from_transcript`） |
| `tried` / `ruled_out` | 第四节（★ v0.38） |
| `confirmed_flag` / `candidates` / `error` | 第五节（**含 flag 原文**） |
| `transcript` 的 result 行 | 附录 C（复用 `runner.digest_from_transcript`） |
| `turns` / `elapsed` / `usages` | 头部与附录 B |

## ★★ flag **不脱敏** —— 与 `notes.py` 恰好相反（别"顺手统一"）

| | `notes.sanitize_for_reuse` | 本模块 |
| --- | --- | --- |
| 产物去哪 | **另一道题**的 prompt | 赛后提交给平台 / 给人看 |
| 带 flag 的后果 | **泄漏**（B 题可能把 A 题的答案交上去） | 正确 —— flag 正是要展示的 |
| 所以 | 白名单式剔除 | **原样保留** |

判据是**这份内容的去向**，不是"flag 是不是秘密"。

⚠️ 因此本模块**绝不调用** `sanitize_for_reuse` —— 那个函数会把
flag 与靶机地址一起删掉，用在题解上等于把最有价值的两样东西
（答案、环境地址）抹掉。这条写在这里，是因为"统一脱敏"看起来
像个合理的重构，而它会静默毁掉题解。

## 未解出也产出

需求原话是"这是赛后需要提交的依据"。未解出的题同样需要一份记录
——"我们试过什么、卡在哪"本身就是可提交的材料。所以 `solved`
只影响措辞（"已解出" vs "未解出"），不影响产不产出。

⚠️ 这一条在 v0.38 之后**更重要**了：未解出的题里，
`tried` / `ruled_out` 恰恰是它唯一有价值的部分
（解出的题有 flag 兜底，未解出的题只有过程）。

## 为什么过程纪要挪到附录

`transcript` 动辄 100+ KB。题解是给人读的，不是日志转储 ——
但纪要又是**证据**（每条结论都能追到过程），不能删。
所以 v0.38 把它放**附录 C**：正文留给叙事，证据留在后面可追溯。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.timeutil import now_utc

#: 题解里最多列几条关键步骤。
#:
#: ⚠️ v0.38 从 6 提到 12：题解是**给人读的完整材料**，不是喂给模型的
#: 召回片段。6 条会把"前半程怎么摸清题目"整段丢掉。
MAX_STEPS = 12

#: ★ v0.38：关键步骤里**单条命令**的长度上限。
#:
#: 原来走的是 `notes.key_steps_from_transcript` 里写死的 160 ——
#: 实测后果是一堆抄不了也读不懂的半截命令：
#:
#:     cd /work && curl -s -D headers.txt 'http://…' -o index.html;
#:     echo "exit=$?"; echo '--- headers ---'; cat headers.tx…
#:
#: 题解要的是"人照着能重跑"，所以放宽到 800。
#:
#: ⚠️ **只影响题解这条路径**（`sanitize=False` 那条）。方法卡仍用 160 ——
#:    见 `notes.key_steps_from_transcript` 的 `clip` 参数说明：
#:    判据始终按 160 那版算，所以放宽它**不会**改变卡片的挑选结果。
STEP_CLIP = 800

#: 过程纪要最多保留几行。
#:
#: ⚠️ v0.38 从 40 提到 200：纪要从**正文**挪到了**附录 C**，
#: 所以"别让题解变成日志转储"这个约束不再由行数承担 ——
#: 它由"放在附录"承担。正文里只留关键步骤。
MAX_DIGEST_LINES = 200


def _cell(value: object) -> str:
    """把值放进 markdown **表格单元格**前的转义（v0.38）。

    ## 为什么必须转义 `|`

    GFM 规范下，表格行里的裸 `|` 会被当作**列分隔符** ——
    于是 `| 标题 | a | b |` 会渲染成**多出来一列**，整张表错位。

    题解里的值确实会带 `|`：题目标题（平台给的，如 `[ACTF2020]a|b`）、
    分类、以及将来任何从外部来的字符串。**这是生成侧的责任** ——
    渲染器对此行为一致，前端转义不了（它拿到的是已经坏掉的结构）。

    ## ⚠️ 只转义**表格单元格**里的

    **正文与代码块里的 `|` 绝不能动** —— 那是 shell 管道或正则的
    或运算（`curl … | grep`、`a|b`）。转义它会**改坏命令**，
    而症状是"用户照着题解跑不通"。

    所以这个函数只被表格那几行调用，不在别处用。

    ## 反斜杠自己也要转义

    `\\` → `\\\\`：否则值里本来就有的 `\\|` 会变成"转义符 + 分隔符"，
    仍然裂成两列。顺序要紧 —— 先处理反斜杠，再处理竖线。
    """
    text = str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|")


def _fmt_duration(seconds: int | None) -> str:
    """把秒数写成人话。`None` 时返回"未记录"。"""
    if seconds is None:
        return "未记录"
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {rest} 秒" if rest else f"{minutes} 分"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"


def _usage_summary(usages: list[tuple[int, str, str, Any]]) -> tuple[str, list[str]]:
    """把逐轮用量汇总成一行 + 按模型分组的明细行。

    返回 `(汇总行, 明细行列表)`。

    ## 为什么要按模型分组

    v0.35 的换模 fallback 会让**同一次解题**用两个模型。那时
    "一共花了 3000 token"是不够的 —— 用户真正想知道的是
    "换模之后那半个题花了多少、值不值"。所以按 `model` 分组。

    ## 为什么不写"花了多少钱"

    单价随模型、供应商、时段变化，写死在代码里会误导。
    只给 token 数，换算由用户自己乘（契约 §3.19.6 记了这条取舍）。

    ## ★ 三种"没有数字"必须说成三句不同的话

    | 情况 | 措辞 | 为什么不能混 |
    | --- | --- | --- |
    | `usages` 为空 | "本次没有调用记录" | 可能是**本次改动之前**的解题（那时还没记） |
    | 有调用但全 `reported=False` | "接入点没有返回用量" | 是**接入点**的问题，不是没跑 |
    | 有部分 `reported=False` | 汇总 + "其中 N 次未返回用量" | 数字是**下限**，不能当成完整值 |

    混成一句"没有数据"会让用户无法判断该找谁 ——
    是代码没记？接入点不支持？还是真的没消耗？
    """
    if not usages:
        # ⚠️ 注意与下面那条的区别：这里是**根本没记录**（比如 v0.36
        #    之前的解题），不是"接入点不给"。
        return ("本次没有调用记录（token 统计自 v0.36 起）", [])

    reported = [u for _, _, _, u in usages if u.reported]
    total = sum(u.billable_total for _, _, _, u in usages)

    if not reported:
        # ★ 关键：区分"真的 0"与"没记录"。
        # 全部 reported=False 时不能说"消耗 0" —— 那与"没跑过"混淆。
        return (
            f"{len(usages)} 次调用，**接入点没有返回 token 用量**（无法统计）",
            [],
        )

    head = f"{len(usages)} 次调用，共 **{total:,}** token"
    if len(reported) < len(usages):
        head += f"（其中 {len(usages) - len(reported)} 次未返回用量）"

    # 按模型分组（保持首次出现的顺序，便于读"先 A 后 B"的时序）
    order: list[str] = []
    per_model: dict[str, dict[str, int]] = {}
    for _turn, model, label, u in usages:
        key = f"{model}（{label}）" if label else (model or "未标注")
        if key not in per_model:
            per_model[key] = {"calls": 0, "tokens": 0, "reported": 0}
            order.append(key)
        per_model[key]["calls"] += 1
        per_model[key]["tokens"] += u.billable_total
        if u.reported:
            per_model[key]["reported"] += 1

    details: list[str] = []
    if len(order) > 1:
        # 只有一个模型时不列明细 —— 那是冗余信息，而题解要短。
        for key in order:
            s = per_model[key]
            details.append(
                f"- `{key}`：{s['calls']} 次调用，{s['tokens']:,} token"
            )
    return head, details


def _humanize_digest(digest: list[str]) -> list[str]:
    """把纪要行里的工具参数 JSON 剥掉，让它**给人读**。

    ## 为什么要这一步

    `digest_from_transcript` 拼出来的行长这样：

        [t1] run_shell {"command": "cd /work/ex && file easyre.exe"} → PE32 executable

    那是为**续解**设计的（喂给模型看"我做过什么"），模型看得懂 JSON。
    而题解是**给人读的**，一屏 `{"command": ...}` 会让它像日志转储。

    剥完：

        [t1] file easyre.exe → PE32 executable

    ## 为什么在 writeup 这边剥，而不改 digest

    `digest_from_transcript` 有别的消费者（`resume` 续解、`notes` 提炼），
    它们要的是**原样**（JSON 里有 `command` 这个键名，模型据此知道
    这是要执行的命令）。为了一种展示需求去改公共函数的输出，
    会波及那两处 —— 所以美化只发生在**本模块的渲染层**。

    ## 剥不掉的不动

    不是 JSON 的行（比如 `[turn 1] LLM 失败：…`）原样保留。
    宁可少剥，也不要把内容弄丢。
    """
    from app.kernel import notes as notes_mod

    out: list[str] = []
    for line in digest:
        # 形如 `[t1] run_shell {"command": "..."} → 输出`
        # 按 ` → ` 切成动作与结果两半，只处理动作那一半。
        if " → " in line:
            action, _, result = line.partition(" → ")
        else:
            action, result = line, ""

        # 动作形如 `[t1] <tool> <args>`。只剥 args 里的 JSON 外壳。
        parts = action.split(" ", 2)
        if len(parts) == 3 and parts[2].lstrip().startswith("{"):
            unwrapped = notes_mod._unwrap_args(parts[2]).strip()
            if unwrapped and not unwrapped.startswith("{"):
                action = f"{parts[0]} {parts[1]} {unwrapped}"

        out.append(f"{action} → {result}" if result else action)
    return out


def build_writeup(
    *,
    challenge: Any,
    outcome: Any,
    solved: bool,
    elapsed_sec: int | None = None,
    now: datetime | None = None,
) -> str:
    """拼出题解的 markdown 全文。

    ## 参数为什么是"传进来"而不是在这里查库

    `challenge` / `outcome` 由调用方（`service`）传进来 —— 本函数是
    **纯函数**（给定输入必得同一输出），这样它能被单测直接覆盖，
    不必搭一个数据库。

    ⚠️ 这也意味着**它拿不到 `confirmed_flag` 之外的历史** ——
    那是有意的：题解描述的是**这一次**解题，不是题目的全部历史。
    """
    moment = now or now_utc()
    lines: list[str] = []

    title = getattr(challenge, "title", "") or "未命名题目"
    category = getattr(challenge, "category", "") or "未分类"
    remote_id = getattr(challenge, "remote_id", "") or ""
    description = (getattr(challenge, "description", "") or "").strip()
    turns = getattr(outcome, "turns", 0)
    flag = getattr(challenge, "confirmed_flag", None)
    candidates = list(getattr(outcome, "candidates", None) or [])
    error = getattr(outcome, "error", None)

    # ── ★ v0.41：报告口径按场景包切换 ────────────────────────
    #
    # 题解本来就是「CTF 题的解」这一种文档。应急响应复盘跑完之后
    # 拿到的却是同一个函数生成的东西 —— 于是标题写着 `CTF Writeup`、
    # 头部第一行是 `> **Flag**：未确认（这道题没有解出）`。
    # 对一次复盘来说那两句都是**错的描述**：它没有 flag，也不是「没解出」。
    #
    # 判别依据用**分类前缀**（`notes.is_ir_category`），不新增参数：
    # 多一个 `kind=` 形参意味着每个调用点都要记得传，而漏传的那处
    # 会静默退回 CTF 口径 —— 那正是要修的症状本身。
    from app.kernel import notes as notes_mod
    from app.kernel import workorder as workorder_mod

    is_ir = notes_mod.is_ir_category(category)
    # ★ 词表：一次定义，后面所有"兜底文案"都用它。
    #   `CTF 题解`里"解题/题目/Flag"是对的；`应急响应复盘`里它们都是错的 ——
    #   一份复盘报告正文写着"本次解题没有可提取的关键步骤"，
    #   读到的人第一反应是"这系统没分清自己在干什么"。
    #   与其在每个兜底句里写三目，不如把三个名词收成一处。
    _word = "复盘" if is_ir else "解题"
    _subject = "复盘对象" if is_ir else "题目"
    orders = workorder_mod.parse_workorders(
        {"workorders": getattr(outcome, "workorders", None)}
    )

    # ── 头部：一眼看到结论 ──────────────────────────────────
    lines.append(
        f"# 应急响应复盘报告 · {title}" if is_ir else f"# CTF Writeup · {title}"
    )
    lines.append("")
    if is_ir:
        # 复盘没有 flag。头部要回答的是「这次复盘产出了什么」，
        # 而不是「flag 拿到了没」—— 后者在一次复盘里根本不存在。
        graded = sum(1 for o in orders if o.approved_grade == "confirmed")
        pending = len(orders) - graded
        lines.append(
            f"> **结论**：本次复盘产出 {len(orders)} 条处置建议"
            f"（证据充分 {graded} 条 / 需先补验证 {pending} 条）；"
            "本系统**未对目标执行任何操作**。"
        )
        lines.append(
            f"> **轮数**：{turns} · **耗时**：{_fmt_duration(elapsed_sec)}"
        )
        lines.append("")
    elif solved and flag:
        lines.append(f"> **Flag**：`{flag}`")
    elif candidates:
        lines.append(f"> **Flag**：未确认（产出 {len(candidates)} 个候选，平台未判对）")
    else:
        lines.append("> **Flag**：未确认（这道题没有解出）")
    if not is_ir:
        lines.append(f"> **结果**：{'已解出' if solved else '未解出'} · "
                     f"**轮数**：{turns} · **耗时**：{_fmt_duration(elapsed_sec)}")
        lines.append("")

    # ── 一、题目信息 ────────────────────────────────────────
    lines.append("## 一、复盘对象" if is_ir else "## 一、题目信息")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("| --- | --- |")
    # ⚠️ 每个**值**都过 `_cell()`：表格单元格里的裸 `|` 会被拆成两列。
    #    标题与分类来自平台，是外部数据，可能带 `|`。
    lines.append(f"| 标题 | {_cell(title)} |")
    lines.append(f"| 分类 | {_cell(category)} |")
    if remote_id:
        lines.append(f"| 平台题目 id | `{_cell(remote_id)}` |")
    if not is_ir:
        lines.append(f"| 结果 | {'已解出' if solved else '未解出'} |")
    lines.append(f"| 轮数 | {turns} |")
    lines.append(f"| 耗时 | {_cell(_fmt_duration(elapsed_sec))} |")
    lines.append(f"| 生成时间 | {moment.strftime('%Y-%m-%d %H:%M:%S')} UTC |")
    lines.append("")

    if description:
        lines.append("### 证据包" if is_ir else "### 题面")
        lines.append("")
        # ⚠️ 题面原样贴（**不脱敏**）—— 它是题目给的，正是题解该有的部分。
        lines.append(description)
        lines.append("")

    # ⚠️ **刻意不列"环境地址"**：`Ctf2Challenge` 上没有 environment 关系
    #    （环境是单独一张表、单独查的），而 `build_writeup` 是**纯函数**
    #    （输入只有 challenge + outcome）。为一个可选展示去扩它的签名，
    #    会让"给定输入必得同一输出"这条性质变弱，也会让调用方多一次查询。
    #
    #    地址并没有丢：它出现在**题面**里（题目自己给的），也出现在
    #    **第三节的关键步骤**里（模型跑的每条 curl 都是完整 URL，
    #    且 `sanitize=False` 不脱敏）。要单独一栏的话，应该在
    #    `service` 侧查好再传进来 —— 那是另一个决定，不在本次范围。

    # ── 二、解题思路（★ v0.38 新增：FINAL.summary）──────────
    #
    # ★★ 这一段是这次重做的**核心**。`summary` 一直能被解析出来，
    #    但收尾时只取了 `candidates`，它被丢掉了 —— 于是模型写的
    #    "这道题是什么、怎么想、结论如何"从来没进过题解。
    #    题解"简陋"的根因在这里，不在排版。
    summary = (getattr(outcome, "summary", "") or "").strip()
    lines.append("## 二、复盘思路" if is_ir else "## 二、解题思路")
    lines.append("")
    if summary:
        lines.append(summary)
    else:
        lines.append(f"（模型没有留下综述。这通常是本次{_word}未走到结论阶段，"
                     "或结论里没有 `summary` 字段。）")
    lines.append("")

    # ── 三、解题过程（完整命令，不再 160 字符截断）─────────
    #
    # ⚠️ 复用 `notes.key_steps_from_transcript` —— 那个函数经过多轮实测
    #    打磨（词边界、残骸命令过滤、附件占位符…），重写一份只会更差。
    #
    # ★★ 必须传 `sanitize=False`：它默认会脱敏（把 URL 掩成
    #    `<target>`、剔除 flag 形状的串）—— 那对**方法卡**是必须的
    #    （卡片进另一道题的 prompt），对**题解**则是错的：靶机地址与
    #    flag 正是题解该有的内容。
    #
    # ★ v0.38：`clip=STEP_CLIP` 放宽单条命令的长度上限（160 → 800）。
    #    判据仍按 160 那版算，所以**方法卡的挑选结果不受影响**。
    from app.kernel import notes as notes_mod

    transcript = getattr(outcome, "transcript", "") or ""
    steps = notes_mod.key_steps_from_transcript(
        transcript, limit=MAX_STEPS, sanitize=False, clip=STEP_CLIP
    )

    lines.append("## 三、取证过程" if is_ir else "## 三、解题过程")
    lines.append("")
    if steps:
        lines.append("按时间序。前几步通常是识别题目与解包，最后几步最接近结论。")
        lines.append("")
        for i, step in enumerate(steps, 1):
            # ⚠️ 用围栏代码块而不是行内 `` ` ``：命令里有反引号或很长时，
            #    行内代码会渲染成一坨。围栏还能保留命令里的引号与换行意图。
            #
            # ⚠️ 措辞用"执行"而不是"命令"：这一步既可能是 shell 命令，
            #    也可能是 `http_request` 的 URL（`_unwrap_args` 会把
            #    `{"url": ...}` 解成 URL）。标成"命令"对后者是错的 ——
            #    实测预览时第 4 条就是一条裸 URL。
            lines.append(f"{i}. 执行：")
            lines.append("")
            # ⚠️ 3 空格缩进 = 列表项的续行（`1. ` 的宽度），
            #    这样代码块**属于**这一项，而不是把列表打断。
            lines.append("   ```bash")
            lines.append(f"   {step}")
            lines.append("   ```")
        lines.append("")
    else:
        lines.append(f"（本次{_word}没有可提取的关键步骤）")
        lines.append("")

    # ── 四、尝试与排除（★ v0.38 新增：FINAL.tried / ruled_out）──
    tried = list(getattr(outcome, "tried", None) or [])
    ruled_out = list(getattr(outcome, "ruled_out", None) or [])

    lines.append("## 四、排查与排除" if is_ir else "## 四、尝试与排除")
    lines.append("")

    lines.append("### 试过的方向")
    lines.append("")
    if tried:
        for item in tried:
            lines.append(f"- {item}")
    else:
        lines.append("（模型没有留下 `tried` 记录。）")
    lines.append("")

    lines.append("### 已排除的可能")
    lines.append("")
    if ruled_out:
        # ★ 这一节的价值在于"附反例"：`ruled_out` 的语义是"试过、不成立"，
        #   不是"感觉不行"。所以原样逐条贴，不加自己的判断。
        for item in ruled_out:
            lines.append(f"- {item}")
    else:
        lines.append("（模型没有留下 `ruled_out` 记录。）")
    lines.append("")

    # ── 五、结论 ────────────────────────────────────────────
    lines.append("## 五、根因结论" if is_ir else "## 五、结论")
    lines.append("")
    if is_ir:
        # 复盘的「结论」不是 flag，而是「处置建议有几条、级别如何」。
        # 真正的根因链在第二节（`summary`）与工单里，这里只做索引。
        if orders:
            lines.append(
                f"本次复盘给出 {len(orders)} 条处置建议，逐条见第六节与工单文件。"
            )
            lines.append("")
            lines.append("| 证据级别 | 条数 |")
            lines.append("| --- | --- |")
            for grade in ("confirmed", "needs_follow_up"):
                n = sum(1 for o in orders if o.approved_grade == grade)
                lines.append(f"| `{grade}` | {n} |")
        else:
            lines.append("本次复盘没有产出处置建议。")
    elif solved and flag:
        lines.append(f"平台判定 flag 正确：`{flag}`")
    else:
        if candidates:
            lines.append(
                f"**未能确认答案。** 本次产出 {len(candidates)} 个候选，"
                "但都没有被平台判定为正确："
            )
            lines.append("")
            for c in candidates:
                # ⚠️ 候选也**不脱敏**：题解是给自己/赛后提交看的，
                #    候选值正是"我们试过什么"的证据。
                item = f"- `{getattr(c, 'flag', c)}`"
                evidence = getattr(c, "evidence", "")
                if evidence:
                    item += f" —— {evidence}"
                lines.append(item)
        else:
            lines.append("**未能确认答案。** 本次没有产出候选 flag。")

        if error:
            lines.append("")
            lines.append(f"**卡在哪**：{error}")
    lines.append("")

    # ── 六、处置建议（★ v0.41，只有应急响应任务会有内容）─────
    #
    # ⚠️ 这里**只做索引，不做正文** —— 工单的完整内容在
    #    `workorder.render_workorders()` 产出的**独立文件**里。
    #    两处都写全文会让"改了工单该改哪一份"变成新问题，而人的
    #    实际动作是拿那份文件去逐条批。题解里只需要让人知道
    #    "这次有 N 条建议"以及去哪找。
    #
    # ⚠️ 非 IR 任务这里**一个字都不加**（`parse_workorders` 拿到
    #    `None` 就返回空列表），所以 CTF 题解版式逐字节不变。
    if orders:
        lines.append("## 六、处置建议")
        lines.append("")
        lines.append(
            f"本次复盘产出 **{len(orders)} 条**处置建议，"
            "另附独立工单文件（`IR-WorkOrders-*.md`），请以那份为准逐条确认。"
        )
        lines.append("")
        lines.append("| # | 动作 | 证据级别 |")
        lines.append("| --- | --- | --- |")
        for index, order in enumerate(orders, start=1):
            grade = order.approved_grade
            # ⚠️ 缺段的工单在题解索引里也要标出来 —— 只在这里显示
            #    `needs_follow_up` 而不说原因，读者会以为证据不足，
            #    而实际是"模型没写风险提示"。
            note = "" if order.complete else "（字段不全）"
            lines.append(f"| {index} | {_cell(order.action)} | `{grade}`{note} |")
        lines.append("")
        lines.append(
            "> ⛔ 这些是**建议**，系统没有执行任何处置动作。"
            "详见附带工单文件的人工确认清单。"
        )
        lines.append("")

    # ── 附录 A · 用到的工具 ─────────────────────────────────
    tools = notes_mod.tools_from_transcript(transcript)
    lines.append("## 附录 A · 用到的工具")
    lines.append("")
    lines.append(tools if tools else "（没有记录到工具调用）")
    lines.append("")

    # ── 附录 B · 资源消耗 ───────────────────────────────────
    lines.append("## 附录 B · 资源消耗")
    lines.append("")
    usages = list(getattr(outcome, "usages", None) or [])
    usage_line, details = _usage_summary(usages)
    lines.append(f"- LLM 调用：{usage_line}")
    if details:
        lines.append("")
        lines.append("按模型拆分（换模后会出现多个）：")
        lines.extend(details)
    lines.append("")

    # ── 附录 C · 完整过程纪要 ───────────────────────────────
    #
    # ★ v0.38：纪要从**正文**挪到这里。它是**证据**（每条结论都能追到
    #   过程），不该删；但它也不该占据正文 —— 正文是给人读的叙事。
    lines.append("## 附录 C · 完整过程纪要")
    lines.append("")
    lines.append("原始动作流水（工具调用 + 输出开头），保留供追溯。")
    lines.append("")
    from app.kernel.runner import digest_from_transcript

    digest = digest_from_transcript(transcript, limit=MAX_DIGEST_LINES)
    if digest:
        lines.append("```")
        lines.extend(_humanize_digest(digest))
        lines.append("```")
    else:
        lines.append(f"（本次{_word}没有留下动作纪要）")
    lines.append("")

    # ── 页脚 ────────────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append(
        f"*由 Suvex 自动生成 · {_subject} `{getattr(challenge, 'id', '')}`*"
    )

    return "\n".join(lines)


def filename_for(challenge: Any) -> str:
    """下载用的文件名。

    ⚠️ 与 `scans/writeup.py` 的 `filename_for` 同一套规则（去掉文件名
    里不安全的字符），但**不共用函数** —— 那边的签名吃的是 `Scan`，
    这里的吃 `Ctf2Challenge`，硬凑一个通用函数会让两边都难读。
    """
    raw = getattr(challenge, "title", "") or "writeup"
    safe = "".join(ch if ch.isalnum() or ch in "-_（）()【】" else "_" for ch in raw)
    safe = safe.strip("_")[:80] or "writeup"
    return f"CTF-Writeup-{safe}.md"


__all__ = [
    "MAX_DIGEST_LINES",
    "MAX_STEPS",
    "STEP_CLIP",
    "build_writeup",
    "filename_for",
]
