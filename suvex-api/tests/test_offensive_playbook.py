"""越权与注入手法块（v0.42）：一个开关，两条路径，一处排除。

## 这个文件守的是五条**互相独立**的承诺

| # | 承诺 | 失效时会怎样 |
| --- | --- | --- |
| 1 | 开关关着时，**五类**解题提示词逐字节不变 | 加一个开关顺手重排了公共部分 → 所有题目的提示词都变了 |
| 2 | 开关关着时，**渗透**指令也是原样 | 只改了 CTF2 侧，扫描侧的 agent 悄悄变凶 |
| 3 | 打开时是**追加**，不是替换 | 最需要那批纪律的时候把它们丢掉 |
| 4 | `agentsec-*` / `ir-*` **被排除** | 判定类与复盘类任务拿到一套"怎么打穿"的指令 |
| 5 | 设置页漏传这一项 → **被拒**，不是静默关掉 | 用户打开过的开关被下一次保存清掉而界面上看不出 |

## 为什么第 1、2 条要用"逐字节"而不是"包含"

"包含"能通过的实现有很多种：`base.strip()`、在中间插一段、把某两段合并
—— 它们都"包含"了原文，但都改变了提示词。而这个 switch 的设计前提是
**关着就等于这段代码不存在**，那只能用相等来验。

第 2 条单独写，是因为渗透侧的拼装函数与解题侧**完全无关**
（扫描侧用 `"\n\n".join(parts)`，CTF2 侧用 `base + "\n" + block`），
只测一侧发现不了另一侧的偏差。

## 为什么第 4 条是最容易漏的一条

手法块**不按分类判**（它对五类一律生效），所以它的代码路径与
`agentsec` / `ir` 那两个块**不是同构的**——那两个是"开关 + 分类都对"
才追加，这个是"开关对、且**不是**那两族"才追加。反向条件最容易写反，
而写反之后的表现是"没报错，但那类题的提示词里多了一份攻击手册"。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.prompts import OFFENSIVE_PLAYBOOK_PATH, load_offensive_playbook
from app.ctf2.runner import build_system_prompt, load_system_prompt
from app.scans.runner import LaunchSpec, StrixRunner

API = "/api/v1"

#: 五个既有分类 —— 手法块对它们**一律生效**。
NORMAL_CATEGORIES = ("pwn", "web", "reverse", "crypto", "misc")

#: 两个场景包 —— 手法块对它们**一律排除**。
SCENARIO_CATEGORIES = (
    "agentsec-prompt-injection",
    "agentsec-tool-escalation",
    "agentsec-data-leak",
    "agentsec-multi-agent",
    "ir-root-cause",
    "ir-timeline",
    "ir-scope",
)


def _spec(**overrides) -> LaunchSpec:  # noqa: ANN003
    """一个最小的 `LaunchSpec`。

    ⚠️ 用**真** `LaunchSpec` 而不是 `SimpleNamespace` 替身：替身会在
    `_instruction()` 访问新字段时抛 `AttributeError`，而那种失败看起来
    像"指令生成坏了"，其实是测试的假对象过期了。
    """
    base = {
        "scan_id": "scan_test",
        "target": "http://example.com",
        "target_kind": "url",
        "skills": ("web",),
        "run_dir": Path("_unused"),
        "log_path": Path("_unused/scan.log"),
        "llm_model": "openai/test",
        "llm_api_key": "sk-test",
    }
    base.update(overrides)
    return LaunchSpec(**base)  # type: ignore[arg-type]


# ══════════════════════════════════════════════════════════
# 块本身
# ══════════════════════════════════════════════════════════


def test_playbook_file_is_where_the_module_says() -> None:
    """★ 路径常量指向的文件**真的存在**，且内容是那个块。

    这条看着像废话，但它挡的是一类真实故障：`prompts/` 随包发布但
    三个打包描述文件（`pyproject.toml` / `MANIFEST.in` / `setup.py`）
    里**一次都没提过它**。哪天有人改成 pip 打包，这些 `.md` 会静默
    缺在 wheel 里 —— 而症状只是"打开开关后提示词没变化"。
    """
    assert OFFENSIVE_PLAYBOOK_PATH.exists(), f"缺文件：{OFFENSIVE_PLAYBOOK_PATH}"
    text = load_offensive_playbook()
    assert text.startswith("## "), "块必须以二级标题起头，否则拼进 prompt 会并进上一节"
    assert "越权" in text.splitlines()[0]


def test_playbook_does_not_grow_unbounded() -> None:
    """块的大小有个上限 —— 它是**追加**，每轮都要占 token。"""
    text = load_offensive_playbook()
    assert len(text) < 20000, f"块涨到 {len(text)} 字符 —— 追加的成本会越来越明显"


# ══════════════════════════════════════════════════════════
# 解题侧：关着不变、开着追加
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize("category", NORMAL_CATEGORIES)
def test_normal_prompt_byte_identical_when_disabled(category: str) -> None:
    """★★ 开关关着时，五类的系统提示词**逐字节**等于原文件。

    这是整个 switch 的核心承诺：关着 = 这段代码不存在。
    """
    base = load_system_prompt()
    got = build_system_prompt(category, agentsec_enabled=False, offensive_enabled=False)
    assert got == base
    # 顺带验一次它真的没被 strip 过 —— 相等已经涵盖，但这条失败时
    # 能直接指向"是不是谁在返回前 strip 了"。
    assert got.endswith(base[-40:])


@pytest.mark.parametrize("category", NORMAL_CATEGORIES)
def test_normal_prompt_appends_playbook_when_enabled(category: str) -> None:
    """★★ 打开时：原文是**前缀**，块在**末尾**，两者之间只有一个换行。"""
    base = load_system_prompt()
    block = load_offensive_playbook()
    got = build_system_prompt(category, agentsec_enabled=False, offensive_enabled=True)

    assert got.startswith(base), "原文必须是前缀 —— 手法块是追加，不是替换"
    assert got.endswith(block), "块必须在末尾"
    assert got == f"{base}\n{block}", "分隔符必须恰好是一个换行"


@pytest.mark.parametrize("category", SCENARIO_CATEGORIES)
def test_scenario_categories_are_excluded(category: str) -> None:
    """★★ 两个场景包的七类**不吃**手法块，即使开关开着。

    手法块整篇在教"怎么把系统打穿"，而那七类题要的是
    "判定考察点是否成立"（agentsec）与"复盘过去发生了什么"（IR）。
    两段放在一起是互相矛盾的两套指令。
    """
    block = load_offensive_playbook()
    # 两个场景开关也一起打开，确保排除的是手法块本身，而不是"因为
    # 别的块的早退把它略过了"。
    got = build_system_prompt(
        category,
        agentsec_enabled=True,
        ir_enabled=True,
        offensive_enabled=True,
    )
    assert block not in got, f"{category} 不该拿到手法块"
    assert got != load_system_prompt(), "但属于它的那个场景块应当仍然追加上了"


def test_scenario_exclusion_survives_scenario_switch_off() -> None:
    """★ 场景包**开关关着**时，那几类同样不吃手法块。

    这一条与上一条不是重复：
      · 上一条验的是"分类属于场景包 → 排除"；
      · 这一条验的是"排除只看分类，不看那个开关开没开"。

    如果实现写成 `if offensive and not (agentsec_enabled and is_agentsec(...))`，
    那关掉 agentsec 开关的部署就会让 `agentsec-*` 类题目**拿到手法块** ——
    而那个部署恰恰是"我不想让 agent 干这种活"的那一个。
    """
    block = load_offensive_playbook()
    for category in SCENARIO_CATEGORIES:
        got = build_system_prompt(category, agentsec_enabled=False, offensive_enabled=True)
        assert block not in got, f"{category} 在场景开关关着时也不该拿到手法块"
        assert got == load_system_prompt(), "这时它的提示词应当与原文件一字不差"


def test_playbook_comes_after_agentsec_block() -> None:
    """★ 手法块排在别的追加块**之后**（它的顺序由代码里 append 的次序决定）。

    ⚠️ 实际使用中这两者互斥（分类不同），所以这条主要锁住"顺序稳定"
    这个性质 —— 哪天有人调换了 append 的位置，块的相对次序会变，
    而那种变化在 diff 里看着无害。
    """
    block = load_offensive_playbook()
    # 造一个既不属于 agentsec 也不属于 ir 的分类，但两个场景开关都开着。
    got = build_system_prompt("web", agentsec_enabled=True, ir_enabled=True, offensive_enabled=True)
    assert got.endswith(block)


# ══════════════════════════════════════════════════════════
# 渗透侧：同一个开关，另一条拼装路径
# ══════════════════════════════════════════════════════════


def test_scan_instruction_byte_identical_when_disabled() -> None:
    """★★ 关着时，扫描侧指令**逐字节**等于不加这个字段之前的样子。

    因为 `LaunchSpec` 的新字段带着默认值 `False`，所以"不加这个字段"
    与"显式传 False"应当完全等价 —— 这就是默认值的意义。
    """
    implicit = StrixRunner._instruction(_spec())
    explicit = StrixRunner._instruction(_spec(enable_offensive_prompt=False))
    assert implicit == explicit
    assert load_offensive_playbook() not in implicit


def test_scan_instruction_appends_playbook_when_enabled() -> None:
    """★★ 打开时：原指令是**前缀**，把块加分隔符摘掉就能还原。"""
    off = StrixRunner._instruction(_spec())
    on = StrixRunner._instruction(_spec(enable_offensive_prompt=True))
    block = load_offensive_playbook()

    assert block in on
    assert on.replace(f"\n\n{block}", "", 1) == off, (
        "摘掉「空行 + 块」之后应当与关着时一字不差 —— 否则说明拼装顺序被打乱了"
    )


def test_scan_playbook_sits_before_the_language_requirement() -> None:
    """★ 块在"要求中文输出"那句**之前**。

    理由：那句话是对**输出形式**的要求，应当是全篇最后一句（离生成最近）。
    被一大段 payload 隔开之后，它对输出语言的影响会变弱。
    """
    on = StrixRunner._instruction(_spec(enable_offensive_prompt=True))
    block = load_offensive_playbook()
    language_line = "请用中文输出你的推理过程与结论"
    assert block in on and language_line in on
    assert on.index(block) < on.index(language_line)
    assert on.rstrip().endswith("不要翻译。"), "中文那句必须是最后一段"


# ══════════════════════════════════════════════════════════
# 设置页接线
# ══════════════════════════════════════════════════════════


def _sandbox_body(**overrides) -> dict[str, object]:  # noqa: ANN003
    body: dict[str, object] = {
        "image": "suvex/sandbox:1.4",
        "cpuLimit": 2,
        "memoryLimitMb": 1024,
        "timeoutMinutes": 15,
        "networkIsolated": False,
        "allowVolumeWrite": False,
        "maxConcurrentScans": 2,
        "scanMaxTurns": 200,
        "scanMode": "standard",
        "enableOffensivePrompt": False,
    }
    body.update(overrides)
    return body


def test_sandbox_settings_expose_the_switch(ready_client: TestClient) -> None:
    """★ `GET /settings/sandbox` 里有这个字段，且默认是 `false`。"""
    response = ready_client.get(f"{API}/settings/sandbox")
    assert response.status_code == 200, response.text
    body = response.json()
    assert "enableOffensivePrompt" in body
    assert body["enableOffensivePrompt"] is False


def test_sandbox_settings_roundtrip_the_switch(ready_client: TestClient) -> None:
    """★★ 写进去再读出来还是那个值 —— 证明它真的落了库、不是算出来的。"""
    try:
        saved = ready_client.put(
            f"{API}/settings/sandbox", json=_sandbox_body(enableOffensivePrompt=True)
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["enableOffensivePrompt"] is True

        reread = ready_client.get(f"{API}/settings/sandbox")
        assert reread.status_code == 200
        assert reread.json()["enableOffensivePrompt"] is True, "读回来的值必须与写进去的一致"
    finally:
        # 还原，免得污染同一次会话里后面的用例（TestClient 共用一个库）。
        ready_client.put(f"{API}/settings/sandbox", json=_sandbox_body())


def test_sandbox_settings_reject_a_missing_switch(ready_client: TestClient) -> None:
    """★★ 请求体**漏了这一项** → 被拒，而不是静默按 `false` 存。

    这是"必填不给默认值"那条纪律的钉子。若实现给它加了默认值，
    这条会变成 200，而后果是：用户用 API 打开了开关，下一次
    在设置页改任何别的项（前端 payload 不带这个字段）都会把它
    静默关掉 —— 界面上看不出发生过什么。

    ⚠️ 状态码是 **400 不是 422**：本项目的
    `app/core/errors.py` 把 FastAPI 的 `RequestValidationError`
    统一翻译成 `400 VALIDATION_FAILED`（`_envelope(400, ...)`）。
    所以这里断言 400 + 错误码，而不是照 FastAPI 默认行为写 422 ——
    写 422 会让这条用例永远红，而失败原因看起来像"字段没校验"。
    """
    body = _sandbox_body()
    body.pop("enableOffensivePrompt")
    response = ready_client.put(f"{API}/settings/sandbox", json=body)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    # 详情里应当点名这个字段。⚠️ 这里出现的是**蛇形名**
    # `enable_offensive_prompt` 而不是驼峰 `enableOffensivePrompt` ——
    # 因为 pydantic 是在校验**模型字段**时报的错，`alias_generator`
    # 只作用于序列化/反序列化的键名。断言驼峰会永远红。
    assert "enable_offensive_prompt" in response.text
