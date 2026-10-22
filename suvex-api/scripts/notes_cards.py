"""把方法卡存成文件、再按与生产**同一套规则**取出来 —— 给方法卡 A/B 实验用。

## 为什么需要这个模块

生产里方法卡躺在 `ctf2_solve_notes` 表里，由 `notes.notes_for_challenge`
按 `category` 召回。而**基线跑在有 Docker 但没有数据库的机器上**
（见提交 `55016f4`：`app/core/db.py` 那条链路在 Kali 上根本没有库），
所以 A/B 走不通数据库那条路。

这里补的就是缺的那一段：把同样的卡存成 JSON 文件，再按**同样的规则**
取出来，交给同一个渲染器 `notes.render_notes_for_prompt`。

## ★ 三条链路必须逐字一致，否则 A/B 测的是"我接对了没有"

| 环节 | 生产 | 这里 |
| --- | --- | --- |
| 提炼 | `notes.build_note_fields` | `learn_card()` 直接调它 |
| 脱敏 | `notes.sanitize_for_reuse`（在 `build_note_fields` 内部） | 同左，不另做 |
| 召回 | 同 category、排除自己、`solved` 降序、上限 3 | `cards_for()` 逐条对齐 |
| 渲染 | `notes.render_notes_for_prompt` | 直接调它 |

任何一处"顺手改写"，实验结论就变成"我的改写有没有用"，而不是
"方法卡有没有用"。

## ⚠️ 卡里绝不能有 flag

卡片会被拼进**另一道题**的 prompt。`build_note_fields` 已经在逐字段
脱敏（白名单式剔除 flag 形状 / 靶机地址 / 长随机串），但这里是
**写入磁盘前最后一道闸**：`learn_card()` 之后由调用方 `grep` 一遍
`flag{` 之类的形状。理由见 `notes.py` 模块 docstring —— 一旦漏了，
就是"A 题的答案进了 B 题的上下文"。
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.kernel import notes as notes_mod  # noqa: E402


@dataclass
class NoteCard:
    """一张方法卡（磁盘形态与内存形态是同一个）。

    字段与 `Ctf2SolveNote` 对齐，但**只保留被 prompt 用到的那些**：
    `id` / `challenge_id` 的 FK / `updated_at` 这些是数据库的事，
    文件形态不需要，多一个字段就多一处可能对不上的地方。
    """

    #: 这张卡是哪道题学出来的。召回时用它做"排除自己" ——
    #: 与生产 `Ctf2SolveNote.challenge_id` 同义。
    challenge_id: str
    category: str
    #: 题目标题。生产里由 `getattr(note, "_title", ...)` 读，缺了会
    #: 退化成"同类题"，那会让模型无法判断"是不是真的同类"。
    title: str = ""
    solved: bool = False
    approach: str = ""
    tools: str = ""
    dead_ends: str = ""

    #: 学习顺序（越大越新）。生产用 `updated_at` 做次级排序键。
    #:
    #: ⚠️ 别留全 0 图省事：`cards_for` 的 `(solved, seq)` 会全部并列，
    #: 池子一过 MAX_NOTES_IN_PROMPT 张，"最近学的优先"就变成随机丢弃。
    #: `cmd_learn` 落盘前会按学习顺序回填。
    seq: int = 0

    @property
    def _title(self) -> str:
        """`render_notes_for_prompt` 读的就是这个名字（`getattr(note, "_title")`）。

        不改渲染器去迁就本类：那个函数是生产路径，改它等于拿生产
        代码去适配一个实验脚本 —— 方向反了。
        """
        return self.title


def learn_card(
    *,
    challenge_id: str,
    category: str,
    title: str,
    transcript: str,
    error: str | None,
    candidates: list[str],
    solved: bool,
) -> NoteCard:
    """从一次解题里提炼一张卡，**实现就是生产那一份**。

    `candidates` 只用来判断"有没有产出"，绝不进卡（见
    `notes.build_note_fields` 的 docstring）。
    """
    fields = notes_mod.build_note_fields(
        transcript=transcript,
        error=error,
        candidates=candidates,
        solved=solved,
    )
    return NoteCard(
        challenge_id=challenge_id,
        category=(category or "")[:64],
        title=title,
        solved=solved,
        approach=fields["approach"],
        tools=fields["tools"],
        dead_ends=fields["dead_ends"],
    )


def save_cards(cards: list[NoteCard], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"cards": [asdict(card) for card in cards]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_cards(path: Path) -> list[NoteCard]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in NoteCard.__dataclass_fields__.values()}
    cards: list[NoteCard] = []
    for raw in payload.get("cards", []):
        # 丢掉不认识的键而不是硬塞：文件可能是旧版本写的，
        # 让它报 TypeError 只会让实验从头再来一次。
        cards.append(NoteCard(**{k: v for k, v in raw.items() if k in known}))
    return cards


def cards_for(
    cards: list[NoteCard],
    category: str,
    *,
    exclude_challenge_id: str | None = None,
    limit: int = notes_mod.MAX_NOTES_IN_PROMPT,
) -> list[NoteCard]:
    """按生产的召回规则挑卡。

    规则逐条对齐 `notes.notes_for_challenge`：

    1. **同 `category`** —— 没有分类就返回空，而不是"给全部"。
       给全部的后果是把无关题的方法灌进去，比不给更坏。
    2. **排除自己** —— 注入自己的卡等于把"上次做到哪"当成"别的题的经验"。
    3. **`solved` 降序**，同状态按 `seq` 降序（对应生产的 `updated_at.desc()`）。
    4. 上限 `MAX_NOTES_IN_PROMPT`（3）。

    ⚠️ 这里**不做标题相似度**，理由与生产一致：误召回比不召回更坏。
    """
    wanted = (category or "").strip()
    if not wanted:
        return []

    pool = [
        card
        for card in cards
        if card.category == wanted and card.challenge_id != exclude_challenge_id
    ]
    pool.sort(key=lambda card: (card.solved, card.seq), reverse=True)
    return pool[:limit]


def render_for(cards: list[NoteCard], category: str, *, exclude_challenge_id: str) -> str:
    """取 + 渲染，一步到位 —— 调用方拿到的就是能塞进 `SolveSpec.notes_text` 的串。"""
    picked = cards_for(cards, category, exclude_challenge_id=exclude_challenge_id)
    return notes_mod.render_notes_for_prompt(picked)


def describe(cards: list[NoteCard]) -> str:
    """一行摘要，给日志用（不进 prompt）。"""
    if not cards:
        return "0 张"
    by_category: dict[str, int] = {}
    for card in cards:
        by_category[card.category] = by_category.get(card.category, 0) + 1
    detail = "、".join(f"{cat}×{count}" for cat, count in sorted(by_category.items()))
    return f"{len(cards)} 张（{detail}）"


# `field` 被 dataclass 用到时才需要；显式 import 是为了让 ruff 不因
# "imported but unused" 改名。保留引用避免将来加 default_factory 时漏 import。
_ = field
