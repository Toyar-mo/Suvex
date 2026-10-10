"""每种目标形态都必须至少有一个可用技能（任务 20）。

## 这个文件在防什么

向导第 2 步「检测范围」要求**至少勾一项**，而技能列表是按目标形态过滤的。
于是有一个必然会踩的坑：

    新增一种目标形态 → 忘了把任何技能挂到它上面
    → 第 2 步六项全灰、一项都勾不了
    → 而"下一步"要求至少勾一项
    → **整个目标类型是一条死路，用户卡在那里出不去**

## 它已经被踩过两次

| 时间 | 形态 | 症状 |
| --- | --- | --- |
| 任务 17 | `artifact` | `dependencies` / `secrets` 的注释里留了警告 |
| 任务 20 | `service` | 又踩一遍 —— 六项全灰，用户截图反馈「没法下一步」 |

第二次踩的时候，第一次留下的警告**就在同一个文件里**，仍然没挡住。
所以这条约束不能靠注释，得靠测试。

## 为什么要连后端一起测

技能的实质是**改变 agent 的侧重**：前端 `SKILLS[].id` 会被后端
`runner.SKILL_FOCUS` 翻成指令里的一句话。两边是两份字面量，
而它们**必须逐字一致**：

  · 前端有、后端没有 → 用户勾了它，但侧重那句话不会出现在指令里
    （静默失效：扫描照跑，只是没按用户选的方向）；
  · 后端有、前端没有 → 用户根本勾不到，那个技能形同不存在。

而且后端 `schemas._known_skills` 是**白名单校验**：前端多送一个
未知技能名会直接 400。所以这条对账是硬需求，不是洁癖。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.core.vocab import SkillId, TargetKind

# 前端数据层。路径从本文件推 —— 不写死盘符，
# 这样在开发机与部署机上跑的是同一份逻辑。
_WEB_SCAN_TS = (
    Path(__file__).resolve().parents[2] / "suvex-web" / "src" / "data" / "scan.ts"
)


def _frontend_skills() -> list[dict[str, object]]:
    """从 `scan.ts` 里解析出 `SKILLS` 数组。

    ## 为什么用正则解析而不是 import

    那是 TypeScript，Python 读不了。而**只读这一小段结构**是可行的：
    `SKILLS` 是字面量数组，`id` 与 `applicableKinds` 都写成
    `id: 'xxx',` 与 `applicableKinds: ['a', 'b'],` 的形状。

    ⚠️ 它确实是脆的 —— 所以解析失败时**明确报错**（见下），
       而不是返回空列表让"没有技能"这种假象通过测试。
       一个静默返回空的解析器，会让这个文件变成永远绿灯的摆设。
    """
    if not _WEB_SCAN_TS.is_file():
        raise AssertionError(
            f"找不到前端数据层：{_WEB_SCAN_TS}\n"
            "（这个测试要对账前后端的技能定义，两边都得在）"
        )
    text = _WEB_SCAN_TS.read_text(encoding="utf-8")

    block = re.search(r"export const SKILLS[^=]*=\s*\[(.*?)\n\]", text, re.S)
    if block is None:
        raise AssertionError(
            "在 scan.ts 里找不到 `export const SKILLS = [...]` 字面量。\n"
            "如果它被重构成了别的形状，请同步改这个测试 —— 不要让它静默跳过。"
        )

    skills: list[dict[str, object]] = []
    for entry in re.finditer(r"\{(.*?)\n  \}", block.group(1), re.S):
        body = entry.group(1)
        ident = re.search(r"id:\s*'([a-z_]+)'", body)
        kinds = re.search(r"applicableKinds:\s*\[([^\]]*)\]", body)
        if ident is None or kinds is None:
            continue
        skills.append(
            {
                "id": ident.group(1),
                "kinds": re.findall(r"'([a-z_]+)'", kinds.group(1)),
            }
        )

    if not skills:
        raise AssertionError("SKILLS 解析出 0 个技能 —— 正则与源文件已经对不上了。")
    return skills


# ══════════════════════════════════════════════════════════
# 一、★ 核心约束：没有死路
# ══════════════════════════════════════════════════════════


def test_every_target_kind_has_at_least_one_applicable_skill() -> None:
    """★★ 每一种目标形态都至少要有一个技能可用。

    这是本文件存在的理由。新增 `TargetKind` 取值时如果忘了挂技能，
    这条会当场变红 —— 而不是等用户在第 2 步卡住、发一张"没法下一步"的截图。
    """
    skills = _frontend_skills()

    dead: list[str] = []
    for kind in TargetKind:
        usable = [s["id"] for s in skills if kind.value in s["kinds"]]
        if not usable:
            dead.append(kind.value)

    assert not dead, (
        "这些目标形态在向导第 2 步**一项技能都勾不了**，"
        "而「下一步」要求至少勾一项 —— 用户会卡死在那里：\n"
        f"  {', '.join(dead)}\n\n"
        "修法：给某个技能（或新增一个语义相符的技能）的 applicableKinds "
        "加上该形态。⚠️ 注意别把语义不符的技能硬挂上去 —— "
        "技能的实质是改变 agent 的侧重，配错方向比不配更糟。"
    )


def test_service_has_the_protocol_skill() -> None:
    """`service`（裸 TCP）靠 `protocol` 技能兜住。

    单独一条是因为它是**最近一次踩坑的现场**（任务 20）：
    六个技能全是 Web 或代码文件向的，裸端口上一个都不适用。
    """
    skills = {s["id"]: s["kinds"] for s in _frontend_skills()}

    assert "protocol" in skills, "`protocol` 技能不见了 —— service 又会变成死路"
    assert "service" in skills["protocol"]


def test_protocol_skill_is_not_offered_for_http_targets() -> None:
    """★ 反向：`protocol` **不能**适用于 url / repo / path / artifact。

    它翻出来的侧重是"对着裸端口做协议逆向、构造二进制输入"。
    挂到 url 上会把 agent 往"找二进制协议"的方向带 ——
    而那是个 HTTP 站点，这个方向根本不存在。
    """
    skills = {s["id"]: s["kinds"] for s in _frontend_skills()}
    kinds = set(skills["protocol"])

    for wrong in ("url", "repo", "path", "artifact"):
        assert wrong not in kinds, (
            f"`protocol` 被挂到了 {wrong} 上 —— 它的侧重（裸端口二进制交互）"
            "对这类目标不成立，会把 agent 带偏"
        )


# ══════════════════════════════════════════════════════════
# 二、前后端技能定义必须对得上
# ══════════════════════════════════════════════════════════


def test_frontend_skill_ids_match_the_backend_enum() -> None:
    """★★ 前端 `SKILLS[].id` 与后端 `SkillId` 必须**一个不多一个不少**。

    两处是各自抄了一份的字面量，而它们的耦合是硬的：
      · 后端 `schemas._known_skills` 是白名单 —— 前端多送一个就 400；
      · 后端 `runner.SKILL_FOCUS` 是映射 —— 前端送了但这里没有，
        侧重那句话就不会出现在指令里，**静默失效**（扫描照跑，
        只是没按用户选的方向）。
    """
    frontend = {s["id"] for s in _frontend_skills()}
    backend = {member.value for member in SkillId}

    assert frontend == backend, (
        "前后端技能定义不一致：\n"
        f"  只有前端有：{sorted(frontend - backend) or '（无）'}\n"
        f"  只有后端有：{sorted(backend - frontend) or '（无）'}\n"
        "（后端有白名单校验，前端多送会被 400 打回）"
    )


def test_every_skill_has_a_focus_clause() -> None:
    """★★ 每个技能在后端都要有对应的侧重措辞。

    缺一条的后果是**静默**的：用户勾了那个技能，提交也成功，
    但 `SKILL_FOCUS` 取不到它，于是指令里不会出现那句侧重 ——
    扫描照跑，只是完全没按用户选的方向走。界面上看不出任何异常。
    """
    from app.scans.runner import SKILL_FOCUS

    missing = [member.value for member in SkillId if member.value not in SKILL_FOCUS]
    assert not missing, (
        f"这些技能没有侧重措辞，勾了等于没勾（静默失效）：{missing}\n"
        "修法：在 app/scans/runner.py 的 SKILL_FOCUS 里补一条。"
    )


def test_skill_focus_has_no_orphan_entries() -> None:
    """反向：`SKILL_FOCUS` 里不能有已经不是技能的键。

    它不会报错，只会永远取不到 —— 属于"删技能时忘了删映射"的残留。
    """
    from app.scans.runner import SKILL_FOCUS

    valid = {member.value for member in SkillId}
    orphans = sorted(set(SKILL_FOCUS) - valid)
    assert not orphans, f"SKILL_FOCUS 里有已不存在的技能：{orphans}"


# ══════════════════════════════════════════════════════════
# 三、解析器本身要能发现"对不上"
# ══════════════════════════════════════════════════════════


def test_parser_finds_all_six_plus_one_skills() -> None:
    """解析器得真的读到东西。

    ⚠️ 这条防的是"解析器静默返回空列表"：那样
    `test_every_target_kind_has_at_least_one_applicable_skill` 会因为
    **每一种形态都没有技能**而变红（看起来像真问题），
    但 `test_protocol_skill_is_not_offered_for_http_targets` 那种
    "断言不包含"的用例会**全部通过** —— 一个空的解析器会让一半用例假绿。
    """
    skills = _frontend_skills()

    assert len(skills) >= 7, f"只解析出 {len(skills)} 个技能：{[s['id'] for s in skills]}"
    ids = {s["id"] for s in skills}
    # 抽查几个必须存在的
    assert {"web", "api", "secrets", "protocol"} <= ids


def test_parser_reports_a_missing_file(tmp_path, monkeypatch) -> None:
    """源文件不在时要**明确报错**，不能静默通过。"""
    import tests.test_skill_coverage as mod

    monkeypatch.setattr(mod, "_WEB_SCAN_TS", tmp_path / "nope.ts")
    try:
        mod._frontend_skills()
    except AssertionError as exc:
        assert "找不到前端数据层" in str(exc)
    else:
        raise AssertionError("源文件不存在时应当明确报错")
