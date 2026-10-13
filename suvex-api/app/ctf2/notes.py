"""CTF2 解题方法卡：从一次解题里提炼可跨题复用的知识（v0.32）。

## ★ 这个模块的**唯一**硬约束：注入内容绝不能带 flag

卡片会被拼进**另一道题**的 prompt。一旦带上旧 flag，就变成
"把 A 题的答案泄漏到 B 题的上下文里" —— 而 B 题的模型完全可能
把它当成 B 题的答案交上去。

所以这里的每个字段都过一遍 `sanitize_for_reuse`，而那个函数是
**白名单式**的：不是"删掉我知道的敏感词"，而是"只留下我确认安全的
形状"。两者的区别在遇到没见过的东西时显现 —— 黑名单会漏，白名单会挡。

## 为什么不直接塞 transcript

transcript 动辄 60–115 KB，绝大部分是工具原始输出。塞回去会把预算
吃光，然后模型又开始"忘记题目要求" —— 那把"复用"变成了"更糟的重来"。
所以这里只提炼**方法**（几步梗概 + 工具名 + 死路），不搬原文。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("suvex.ctf2")

# ── 上限（都要能说出为什么）────────────────────────────────

#: `approach` / `dead_ends` 各自的字符上限。
#:
#: 600 字 ≈ 中文 300 字左右，够说清"用什么思路、卡在哪"，而
#: 两张卡 + 题面 + 附件清单拼起来仍在 prompt 的合理范围内。
#: 再长就会挤占解题本身需要的上下文 —— 那正是 `digest` 要避免的事。
MAX_NOTE_CHARS = 600

#: 一次召回最多注入几张卡。
#:
#: 3 张是"够用且不喧宾夺主"的量级：同类题的方法往往高度重合，
#: 给 10 张只会让 prompt 里塞满重复信息，反而稀释了当前题的重点。
MAX_NOTES_IN_PROMPT = 3

#: `tools` 字段最多留几个工具名。
MAX_TOOLS = 12


def sanitize_for_reuse(text: str, *, limit: int = MAX_NOTE_CHARS) -> str:
    """把一段文本处理成**可以安全注入到另一道题**的形状。

    ## 白名单式，不是黑名单式

    这里做的事是**只留下确定的形状**，而不是"删掉已知的敏感词"：

      1. **剔除任何看起来像 flag 的片段**（`FLAG_PATTERN`）——
         这是最要紧的一条，理由见模块 docstring；
      2. **剔除 URL 里的查询串与路径**（题面/回显里常带靶机地址，
         而那是**这一道题**的地址，注入到别的题只会误导）；
      3. **剔除看起来像凭据的长随机串**（32 位以上的 hex / base64）；
      4. 折叠空白、截断到上限。

    ## 为什么用"剔除"而不是"替换成占位符"

    替换会留下 `flag{***}` 这样的痕迹，而模型看到它会试图"还原"，
    或者把占位符当成格式提示。直接删掉更干净 —— 损失的信息是
    "这里原本有个 flag"，而那本来就不该被复用。
    """
    if not text:
        return ""

    # 延迟 import：`runner` 依赖本模块（`service` 侧），而本模块
    # 顶层 import 它会形成环。
    from app.ctf2.runner import FLAG_PATTERN

    cleaned = text

    # ① 剔除 flag 形状的片段。
    #
    # ⚠️ `FLAG_PATTERN` 是**整串匹配**（`^...$`），不能直接用来找子串。
    #    所以这里按空白切开逐个词判断 —— 那正好也是 flag 在文本里的
    #    常见形态（一个独立的词）。
    words = cleaned.split()
    kept = [w for w in words if not FLAG_PATTERN.match(w.strip("`,.;:()[]'\""))]
    cleaned = " ".join(kept)

    # ② 剔除 URL（含靶机地址）。保留"有个 URL"这个事实没意义，
    #    因为那是**别的题**的地址。
    cleaned = re.sub(r"https?://\S+", "", cleaned)

    # ③ 剔除长随机串（可能是凭据、也可能是爆破出的口令）。
    #
    # 32 位是判据：短于它的随机串太多（哈希前缀、偏移量），
    # 剔除会误伤正常内容；而 32 位以上的连续 hex/base64
    # 在方法描述里几乎总是凭据或密钥。
    cleaned = re.sub(r"\b[A-Fa-f0-9]{32,}\b", "", cleaned)
    cleaned = re.sub(r"\b[A-Za-z0-9+/]{40,}={0,2}\b", "", cleaned)

    # ④ 折叠空白（上面剔除后会留下多余空格与空行）。
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    return cleaned.strip()[:limit]


def tools_from_transcript(transcript: str, *, limit: int = MAX_TOOLS) -> str:
    """从 transcript 里提取**用过的工具名**（去重、保序）。

    只取名字，不取命令 —— 命令里常带目标地址与参数，而那些是
    这一道题特有的（见 `sanitize_for_reuse` 第 ② 条）。

    ⚠️ 用 `_TRANSCRIPT_TOOL` 那个正则（与 `digest_from_transcript`
       共用）：它是**我们自己的日志格式**，不是猜别人的。
    """
    from app.ctf2.runner import _TRANSCRIPT_TOOL

    seen: list[str] = []
    for line in transcript.splitlines():
        match = _TRANSCRIPT_TOOL.match(line)
        if not match:
            continue
        name = match.group(2).strip()
        if name and name not in seen:
            seen.append(name)
        if len(seen) >= limit:
            break
    return ", ".join(seen)


def build_note_fields(
    *, transcript: str, error: str | None, candidates: list[str], solved: bool
) -> dict[str, str]:
    """从一次解题的产物里提炼出卡片的三个字段（**全部已脱敏**）。

    ## 输入是什么、能提炼出什么

    | 输入 | 提炼出的 |
    | --- | --- |
    | `transcript` | 用过的工具（`tools`）、方法梗概（`approach`） |
    | `error` | 失败原因 → 进 `dead_ends` |
    | `candidates` | **只用来判断"有没有产出"，绝不写进卡片** |

    ⚠️ `candidates` 是 flag 原文，**一个字都不进卡片**。它出现在签名里
    只是为了区分"解出来了"与"没解出来"两种梗概措辞。
    """
    tools = tools_from_transcript(transcript)

    if solved:
        approach = (
            "这道题解出来了。可复用的要点：先看清楚手上有什么（附件类型、"
            "服务响应），再按题型选工具；不要手工解析已知格式，先找现成工具。"
        )
        dead_ends = ""
    else:
        # 失败时把错误原话（脱敏后）留下 —— 它往往是"卡在哪一步"的
        # 最精确描述。
        reason = sanitize_for_reuse(error or "", limit=300)
        approach = "这道题**没解出来**。已做到的部分见工具清单；"
        dead_ends = reason or "未记录失败原因。"
        if not candidates:
            approach += "全程没有产出候选答案。"

    return {
        "approach": sanitize_for_reuse(approach),
        "tools": tools,
        "dead_ends": dead_ends,
    }


def render_notes_for_prompt(notes: list[object], *, limit: int = MAX_NOTES_IN_PROMPT) -> str:
    """把几张卡片渲染成 prompt 里的一段（**只读、只渲染**）。

    ## 措辞上刻意强调"这是别的题的经验"

    不这么写的话，模型会把它当成**当前题**的已知条件 —— 而它是
    另一道题的方法，工具名可能对、参数一定不对。

    所以这一段：
      · 明确说"同类**其他**题的经验"；
      · 明确说"只借鉴思路，不要照搬命令"；
      · 附上题目标题，让模型能判断"是不是真的同类"。
    """
    if not notes:
        return ""

    lines = [
        "",
        "## 同类题的经验（★ 是**别的题**的，只借鉴思路）",
        "",
        "下面是本平台解过的同类题的方法记录。**它们不是当前题的已知条件** ——"
        "题目不同，命令与参数都不一样。",
        "",
        "用法：**看思路与工具选择，不要照搬命令**。"
        "如果某条「死路」和你正在试的方向相同，优先换方向。",
    ]
    for note in notes[:limit]:
        title = getattr(note, "_title", None) or getattr(note, "category", "") or "同类题"
        lines.append("")
        lines.append(f"- **{title}**（{getattr(note, 'category', '') or '未分类'}）")
        approach = getattr(note, "approach", "") or ""
        if approach:
            lines.append(f"  - 做法：{approach}")
        tools = getattr(note, "tools", "") or ""
        if tools:
            lines.append(f"  - 用到的工具：{tools}")
        dead = getattr(note, "dead_ends", "") or ""
        if dead:
            lines.append(f"  - 走不通的方向：{dead}")
    return "\n".join(lines)


def notes_for_challenge(db, challenge, *, limit: int = MAX_NOTES_IN_PROMPT) -> list:  # noqa: ANN001
    """找出与这道题**同类**的方法卡（召回）。

    ## 判据：同分类，优先已解出的，排除自己

    1. **同 `category`** —— 这是当前唯一可靠的相似度信号。
       附件指纹相似度更准，但那需要先下载完附件（同步阶段），
       而召回发生在解题开始前 —— 那时附件可能还没下完。
    2. **排除自己** —— 注入自己的卡等于把"上次做到哪"当成"别的题的经验"，
       而那是 `resume` 该做的事，两者混淆会让模型分不清。
    3. **已解出的排前面** —— 成功的卡信息量更大；失败的卡（`dead_ends`）
       仍然给，但排后面。

    ## ⚠️ 为什么不做"标题相似度"

    标题相似度会误召回：`[FlareOn3]GUI` 与 `[FlareOn4]GUI` 标题极像，
    但可能一道是 .NET、一道是 Go。而**误召回比不召回更坏** ——
    它会让模型按错误的思路走，而它还以为是"经验"。
    """
    from sqlalchemy import select

    from app.ctf2.models import Ctf2SolveNote

    category = (getattr(challenge, "category", "") or "").strip()
    if not category:
        # 没有分类就无法判断"同类" —— 返回空而不是"给全部"。
        # 给全部的后果是把无关题的方法灌进去，比不给更坏。
        return []

    rows = (
        db.execute(
            select(Ctf2SolveNote)
            .where(Ctf2SolveNote.category == category)
            .where(Ctf2SolveNote.challenge_id != challenge.id)
            # 已解出的排前面（`solved` 是 bool，降序即 True 优先）。
            .order_by(Ctf2SolveNote.solved.desc(), Ctf2SolveNote.updated_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return list(rows)
