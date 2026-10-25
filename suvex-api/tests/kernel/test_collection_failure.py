"""连续采集失败升格为黑板事实（v0.92，修 32231 事故缺陷 B）。

## 这个文件在防什么

32231 那次（`_tmp/诊断-32231为何61分钟未解出.md`）目标在 03:33:49 死亡，
之后 **25.5 分钟、9 次 run_shell** 全在重试，直到墙钟硬杀。模型自己在
03:34:04 说过：

> "The last sweep was all `000` — collection failure (server saturated),
>   not evidence."

**但这个认知只活了一轮**。平台没有任何机制把「连续 N 次 000」升级成一条
结构性事实 —— 而一旦 `record_fact` 进黑板，后续所有轮都能看到。

所以这里要钉住的是三件事：

1. 连续失败**会不会**触发升格（正向）
2. 升格出来的事实**措辞对不对**（不能把「采不到」说成「没漏洞」）
3. 各种**不该触发**的情形（这是重点 —— 误报比漏报更糟）

第 3 条尤其重要：把活靶子宣布成死的，会毁掉模型已经验证过的正确结论。
32231 里那条唯一站得住的结论（「GET-only 路由枚举干净」）正是靠
`gobuster → 0 results` 且靶子 HTTP 200 才成立的。如果「0 results」被判成
采集失败，这条结论就会被一条假事实推翻。

## 为什么不只测 `_looks_like_collection_failure`

那个函数只是判据。真正要保证的是**它被接进了主循环** —— 判据写得再对，
没接线就等于没做（本项目反复踩这个形状：gogo 的 MCP 配置、SARIF 的规则
id、`mcp_servers` 的 HTTP 层，全是「配了/写了但静默失效」）。
所以最后一个用例走真实的 `runner.solve_in_sandbox` 全链路。
"""

from __future__ import annotations

import json

from app.kernel import multiagent, runner
from tests.kernel.test_runner import _FakeSandbox, _run, _spec

#: 一次「连不上」的工具输出（curl 退出码 7 的真实形态）。
_REFUSED = "curl: (7) Failed to connect to 1.2.3.4 port 8080: Connection refused"

#: 一次 `curl -w '%{http_code}'` 的失败输出（32231 里遍地都是的那种）。
_HTTP_000 = "000"


# ══════════════════════════════════════════════════════════
# 一、判据本身：哪些算采集失败
# ══════════════════════════════════════════════════════════


def test_connection_refused_counts_as_collection_failure() -> None:
    assert (
        multiagent._looks_like_collection_failure(
            tool_name="run_shell",
            command="curl -sS http://1.2.3.4:8080/",
            output=_REFUSED,
        )
        is True
    )


def test_bare_http_000_counts_as_collection_failure() -> None:
    """`curl -w '%{http_code}'` 只打三位数，没有状态行。

    这是 32231 里最常见的形态（`03:40:15 sleep 45; curl ... → 000`）。
    """
    assert (
        multiagent._looks_like_collection_failure(
            tool_name="run_shell",
            command="curl -sS -m 8 -w '%{http_code}' http://1.2.3.4/",
            output=_HTTP_000,
        )
        is True
    )


def test_timeout_counts_as_collection_failure() -> None:
    assert (
        multiagent._looks_like_collection_failure(
            tool_name="run_shell",
            command="curl -sS -m 90 http://1.2.3.4/render",
            output="curl: (28) Operation timed out after 90001 milliseconds",
        )
        is True
    )


# ══════════════════════════════════════════════════════════
# 二、判据的反面：这些**不该**算采集失败
#
# 这一组比上一组重要。每条都对应 32231 里真实出现过、且会误判的形态。
# ══════════════════════════════════════════════════════════


def test_zero_results_does_not_count_as_collection_failure() -> None:
    """★★ `gobuster → 0 results` 是**靶子健康且确实没暴露路径**。

    这是 32231 里那条唯一站得住的结论（「GET-only 路由枚举干净」）的依据。
    若把它判成采集失败，就会在靶子活得好好的时候宣布它死了，并写一条假事实
    —— 模型会因此丢掉一个已经验证过的正确结论。**比不提醒更糟。**
    """
    assert (
        multiagent._looks_like_collection_failure(
            tool_name="run_shell",
            command="gobuster dir -u http://1.2.3.4/ -w common.txt",
            output="0 results",
        )
        is False
    )


def test_local_config_error_does_not_count_as_collection_failure() -> None:
    """ffuf 的 `encountered error` 是**本地配置坏了**，不是靶子死了。

    `03:01:44` 实测：`open /home/solver/.config/ffuf/scraper: no such file
    or directory` —— 靶子当时 HTTP 200 活着。
    """
    assert (
        multiagent._looks_like_collection_failure(
            tool_name="run_shell",
            command="ffuf -u http://1.2.3.4/FUZZ -w common.txt",
            output=(
                "Encountered error(s): 1 errors occurred.\n"
                "\t* open /home/solver/.config/ffuf/scraper: no such file or directory"
            ),
        )
        is False
    )


def test_local_command_with_connection_error_does_not_count() -> None:
    """容器里装包失败也带 "connection refused"，与靶子可达性无关。

    若不加 `_looks_like_acting` 这层前置，模型装个包失败就会被判「目标已死」。
    """
    assert (
        multiagent._looks_like_collection_failure(
            tool_name="run_shell",
            command="pip install requests",
            output="ERROR: connection refused by proxy",
        )
        is False
    )


def test_five_hundred_still_counts_as_alive() -> None:
    """5xx 是目标**自己**在报错 —— 它还在跑，不是采集失败。

    32231 里 `/render` 大量返回 500（`BiDi timeout`）。那些 500 说明服务活着
    但渲染挂了 —— 这是**有效信息**（"渲染失败"本身就是一条线索），
    判成「目标不可达」会把这条线索也一起丢掉。
    """
    assert (
        multiagent._looks_like_collection_failure(
            tool_name="run_shell",
            command="curl -i -X POST http://1.2.3.4/render",
            output="HTTP/1.1 500 Internal Server Error\nX-Powered-By: Express",
        )
        is False
    )


def test_mixed_batch_with_success_does_not_count() -> None:
    """一条命令里打了多次，只要有一次成功就算拿到了数据。

    `for i in 1..10; curl -w '%{http_code}'; done` 打 `200 000 200`：
    若"出现一个 000 就判失败"，模型**打得越勤越会被判靶子死了**，事实正相反。
    """
    assert (
        multiagent._looks_like_collection_failure(
            tool_name="run_shell",
            command="for i in 1 2 3; do curl -sS -w '%{http_code}' http://1.2.3.4/; done",
            output="200 000 200",
        )
        is False
    )


def test_flag_does_not_count_as_collection_failure() -> None:
    """拿到 flag 还报「目标不可达」显然是荒谬的。"""
    assert (
        multiagent._looks_like_collection_failure(
            tool_name="run_shell",
            command="curl -sS http://1.2.3.4/flag",
            output="flag{deep}",
        )
        is False
    )


def test_numbers_in_headers_do_not_look_like_success() -> None:
    """`Content-Length: 15016` / `ETag` 里的数字不能当成功标志。

    这条防的是「反面修过头」：为了让混合输出判成功，把裸 `\\d{3}` 也认了，
    结果连接失败的输出里只要有个 `15016` 就被判成功，等于这个机制永久失效。
    """
    assert (
        multiagent._SUCCESS_EVIDENCE.search(
            "curl: (7) Failed to connect; Content-Length: 15016; ETag: 3aa8"
        )
        is None
    )


def test_empty_output_does_not_count_as_collection_failure() -> None:
    """`grep` 没匹配、`ls` 空目录都是正常空输出，不是目标死了。"""
    assert (
        multiagent._looks_like_collection_failure(
            tool_name="run_shell", command="curl -sS http://1.2.3.4/", output=""
        )
        is False
    )


def test_non_acting_tool_never_counts() -> None:
    """记账/思考类工具与目标可达性无关。"""
    for name in ("record_fact", "think", "list_coverage", "write_note"):
        assert (
            multiagent._looks_like_collection_failure(
                tool_name=name, command="", output="connection refused"
            )
            is False
        ), f"{name} 不该被判成采集失败"


# ══════════════════════════════════════════════════════════
# 三、协调器计数：连续、清零、只升格一次
# ══════════════════════════════════════════════════════════


def test_streak_increments_and_resets() -> None:
    coord = multiagent.AgentCoordinator()
    for _ in range(3):
        coord.note_collection(ok=False)
    assert coord.collection_fail_streak == 3
    coord.note_collection(ok=True)
    assert coord.collection_fail_streak == 0, "一次成功就该清零"


def test_escalation_flag_resets_only_on_success() -> None:
    """「死了 → 活了 → 又死了」要能再报一次。

    若 `escalated` 只在构造时初始化，那么一场扫描里只能报一次，第二次死亡
    会被静默吞掉 —— 而"反复死"恰恰是最该提醒的形态。
    """
    coord = multiagent.AgentCoordinator()
    for _ in range(3):
        coord.note_collection(ok=False)
    coord.collection_fail_escalated = True  # 模拟已升格

    coord.note_collection(ok=False)
    assert coord.collection_fail_escalated is True, "连续失败期间不该复位"

    coord.note_collection(ok=True)
    assert coord.collection_fail_escalated is False, "靶子活过来就要允许再报"


def test_failures_from_different_agents_share_one_streak() -> None:
    """「目标可不可达」是关于**靶子**的事实，全场共享一个计数。

    与 `recent_actions`（按 agent 分）刻意不同：那个判「谁在原地打转」，
    这个判「靶子还在不在」。a1 的 3 次 000 + root 的 3 次 000 就是 6 次，
    不该因为分属两个 agent 就各算 3 次、谁都到不了阈值。
    """
    coord = multiagent.AgentCoordinator()
    coord.add_root()
    coord.add_child(name="a1", task="t", parent_id="root")
    for _ in range(3):
        coord.note_collection(ok=False)
    assert coord.collection_fail_streak == 3, "计数是全局的，不按 agent 分"


# ══════════════════════════════════════════════════════════
# 四、措辞：不能把「采不到」说成「没漏洞」
# ══════════════════════════════════════════════════════════


def test_fact_text_says_not_evidence_of_absence() -> None:
    """★ 这条事实最关键的判读边界。

    模型把「采不到」当成「干净」是这类事故的共同形态。一旦这条事实进黑板
    却不带这句边界，它反而会**强化**误判（"系统都说目标不可达了，那我扫不到
    当然说明没漏洞"）—— 那比不写更糟。
    """
    text = multiagent._collection_fact_text(streak=3)
    assert "不是「没有漏洞」的证据" in text
    assert "3" in text, "要带上量化前提，否则后续轮次读不出严重程度"


def test_fact_text_is_self_contained() -> None:
    """事实会单独出现在别的 agent、别的轮次的 prompt 里，必须能独立读懂。"""
    text = multiagent._collection_fact_text(streak=5)
    assert "不可达" in text
    assert "采集" in text
    for bad in ("上面", "刚才", "前面那条"):
        assert bad not in text, f"事实里不能有指代：{bad}"


def test_note_tells_what_to_do_next() -> None:
    """只说「目标死了」会让模型停下来等（它修不了服务器）。

    必须把它导向「把已有证据收口」，否则这 25 分钟的重试只是换成静坐。
    """
    note = multiagent.collection_fail_note(streak=3)
    assert "收口" in note
    assert "不需要你重新推导" in note, "要说明这条已持久化，否则模型会当一次性提示"


# ══════════════════════════════════════════════════════════
# 五、接线：全链路必须真的写进黑板
# ══════════════════════════════════════════════════════════


class _ScriptedGateway:
    """按调用顺序吐预设回复（与 `test_multiagent._ScriptedGateway` 同形）。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.seen_systems: list[str] = []
        self.seen_messages: list[list[dict]] = []
        self.model = "test-model"
        self.label = "test-label"

    async def chat(self, messages, *, system):  # noqa: ANN001, ANN201
        self.calls += 1
        self.seen_systems.append(system)
        self.seen_messages.append(list(messages))
        if self.calls > len(self.replies):
            raise AssertionError(f"gateway 被多调了：第 {self.calls} 次没有脚本回复了")
        text = self.replies[self.calls - 1]
        return runner.LlmReply(text=text, source="content", finish_reason="stop")


def _act(command: str) -> str:
    """一条模型回复：**`ACTION:` 标记行 + JSON**。

    ★ 标记行不能省。`runner.parse_actions` 走的是 `_MARKER`
    （`^\\s*(ACTION|FINAL)\\s*[:：]`）逐行扫，裸 JSON 一个动作都解析不出来 ——
    此时循环会把它当成"纯思考回合"，工具一次都不执行，于是整条端到端
    测试测的是一件事没发生的路径（首轮就是这么假红的：spy 命中 0 次）。
    """
    payload = json.dumps(
        {"tool": "run_shell", "args": {"command": command}}, ensure_ascii=False
    )
    return f"ACTION: {payload}"


def test_escalation_reaches_the_blackboard_and_the_prompt() -> None:
    """★ 端到端：连续 3 次失败 → 写进 facts → 下一轮 prompt 里看得到。

    这条是整个改动的**唯一有效证明**：判据可以单独测，但如果主循环没把它
    接进 `shared.add_fact`，或者说接进去了但 `render_live_state` 没渲染出
    这条事实，那么生产环境里模型的上下文**一个字节都不会变** ——
    与「配了但静默失效」是同一个形状。
    """
    gateway = _ScriptedGateway(
        [
            _act("curl -sS -w '%{http_code}' http://1.2.3.4/"),  # 第 1 次失败
            _act("curl -sS -w '%{http_code}' http://1.2.3.4/"),  # 第 2 次失败
            _act("curl -sS -w '%{http_code}' http://1.2.3.4/"),  # 第 3 次 → 升格
            'FINAL: {"summary": "目标不可达，收口", "findings": []}',
        ]
    )
    sandbox = _FakeSandbox(outputs={"curl": _HTTP_000})
    spec = _spec(multi_agent=True, max_turns=8, min_coverage=0)

    outcome = _run(
        runner.solve_in_sandbox(spec, sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )
    assert outcome.error is None, f"不该因为这次改动引入错误：{outcome.error}"

    # ① 黑板里必须有这条事实
    assert len(gateway.seen_systems) >= 4, "gateway 至少被调 4 次"
    last_system = gateway.seen_systems[-1]
    assert "不可达" in last_system, (
        "第 4 轮的 system prompt 里必须带着「目标不可达」这条事实 —— "
        "否则它只活在失败那一轮，下一轮就蒸发了（正是 32231 的形态）"
    )
    assert "不是「没有漏洞」的证据" in last_system, "判读边界必须一起推送"

    # ② 更要紧的：它必须在**已确认事实**那一段里，而不是随便什么地方
    fact_block = last_system.split("## 已确认事实")
    assert len(fact_block) == 2, (
        "这条事实必须出现在「已确认事实」段落 —— 那是每轮推送的权威位置。"
        f"实际 system prompt：\n{last_system[-2000:]}"
    )
    assert "约束" in fact_block[1], "应以 constraint 类型呈现（限定结论有效性）"


def test_success_does_not_escalate() -> None:
    """靶子健康时**绝不能**写这条事实 —— 误报会毁掉已验证的结论。"""
    gateway = _ScriptedGateway(
        [
            _act("gobuster dir -u http://1.2.3.4/ -w common.txt"),
            _act("gobuster dir -u http://1.2.3.4/ -w common.txt"),
            _act("gobuster dir -u http://1.2.3.4/ -w common.txt"),
            'FINAL: {"summary": "路由枚举干净", "findings": []}',
        ]
    )
    # `0 results` 是**健康**信号（靶子 200 且确实没路径），不该触发升格。
    sandbox = _FakeSandbox(outputs={"gobuster": "0 results"})
    spec = _spec(multi_agent=True, max_turns=8, min_coverage=0)

    outcome = _run(
        runner.solve_in_sandbox(spec, sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )
    assert outcome.error is None
    for i, system in enumerate(gateway.seen_systems):
        assert "不可达" not in system, (
            f"第 {i + 1} 轮 system prompt 里不该出现「不可达」—— "
            "`0 results` 是靶子健康且没暴露路径，不是采集失败"
        )


# ── 子 agent 也必须看得到 ─────────────────────────────────────────────
#
# 这一组是我**在自测时当场抓到的自己的漏洞**：第一版只把事实写进
# `shared.facts`，而 `render_live_state` 只给 root（`if is_root:` 守卫，
# 被 `test_live_state.py:test_only_root_gets_it` 钉着）。于是：
#
#   第 4 轮 root 升格 → 第 8 轮才孵化的子 agent → **一个字节都看不到**
#
# 而 `_collection_fact_text` 的 docstring 当时已经写着"它会在别的 agent、
# 别的轮次的 prompt 里单独出现"，`AgentCoordinator.collection_fail_streak`
# 的注释也写着"别的 agent 的成功会抵消它"—— 即计数器是**全局**的。
# 全局的判据只推到 root，就是"配了但只对一半对象生效"。


def _act_spawn(name: str) -> str:
    payload = json.dumps(
        {"tool": "create_agent", "args": {"name": name, "task": "探一下目标"}},
        ensure_ascii=False,
    )
    return f"ACTION: {payload}"


def test_a_child_spawned_after_escalation_sees_the_constraint() -> None:
    """★ 升格之后才孵化的子 agent 必须看到「目标不可达」。

    32231 那次跑的正是多智能体，且三个子 agent 都还在对已死的靶子发命令。
    如果这条不成立，`record` 出来的约束就只对 root 生效 ——
    子 agent 会继续把"没找到"当成结论交上来（假阴性）。
    """
    gateway = _ScriptedGateway(
        [
            _act("curl -sS -w '%{http_code}' http://1.2.3.4/"),  # 1
            _act("curl -sS -w '%{http_code}' http://1.2.3.4/"),  # 2
            _act("curl -sS -w '%{http_code}' http://1.2.3.4/"),  # 3 → 升格
            _act_spawn("child-late"),                            # 4 → 孵化
            _act("curl -sS -w '%{http_code}' http://1.2.3.4/"),
            'FINAL: {"summary": "收口", "findings": []}',
        ]
    )
    sandbox = _FakeSandbox(outputs={"curl": _HTTP_000})
    spec = _spec(multi_agent=True, max_turns=16, min_coverage=0)

    outcome = _run(
        runner.solve_in_sandbox(spec, sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )
    assert outcome.error is None

    # 找出"子 agent 的 system prompt"：子 agent 的首轮 user 消息里有
    # 「## 你的具体任务」（`_child_task_prompt` 拼的）。
    child_systems = [
        gateway.seen_systems[i]
        for i, msgs in enumerate(gateway.seen_messages)
        if any("## 你的具体任务" in str(m.get("content", "")) for m in msgs)
    ]
    assert child_systems, "这次运行里应该有子 agent（否则本用例没测到东西）"
    for system in child_systems:
        assert "全局约束" in system, (
            "子 agent 的 system prompt 里必须有「全局约束」那一段 —— "
            f"实际拿到的是：\n{system[-1500:]}"
        )
        assert "不可达" in system, "约束正文必须真的推送过去，不能只有一个标题"


class _RoutedGateway:
    """按"这是不是子 agent"分流给两套脚本。

    ★ 为什么必须分流：`_ScriptedGateway` 是一条共享队列，而 root 与子 agent
    是**并发**的（`asyncio.create_task`）。谁先来取不肯定的脚本会让测试
    时红时绿 —— 而这里要钉的是"子 agent 第几轮拿到约束"这种**顺序**性质，
    恰恰最怕不确定。分流之后，两个角色的轮次各自可数。
    """

    def __init__(self, *, root_replies: list[str], child_replies: list[str]) -> None:
        self.root_replies = list(root_replies)
        self.child_replies = list(child_replies)
        self.root_calls = 0
        self.child_calls = 0
        self.model = "test-model"
        self.label = "test-label"
        #: (是不是子 agent, system prompt, 当时的 messages)
        self.log: list[tuple[bool, str, list[dict]]] = []

    async def chat(self, messages, *, system):  # noqa: ANN001, ANN201
        is_child = any(
            "## 你的具体任务" in str(m.get("content", "")) for m in messages
        )
        self.log.append((is_child, system, list(messages)))
        if is_child:
            self.child_calls += 1
            script, n = self.child_replies, self.child_calls
        else:
            self.root_calls += 1
            script, n = self.root_replies, self.root_calls
        text = script[n - 1] if n <= len(script) else script[-1]
        return runner.LlmReply(text=text, source="content", finish_reason="stop")


def test_a_running_child_picks_the_constraint_up_on_its_next_turn() -> None:
    """★ 已经在跑的子 agent，**后续轮次**就要看到 —— 不能只在孵化那一刻算一次。

    首轮 prompt 由 `_child_task_prompt` **一次性**生成，之后不再重建。
    注入点若只放在那里，一个在升格**之前**就已经孵化的子 agent
    一辈子都读不到这条 —— 而它恰恰是最需要被叫停的那个
    （32231 里那三个子 agent 全都属于这种情况）。
    """
    curl = "curl -sS -w '%{http_code}' http://1.2.3.4/"
    gateway = _RoutedGateway(
        # root：先孵化（此时靶子还没判死），再连打三次把靶子判死，然后收口。
        root_replies=[
            _act_spawn("child-early"),
            _act(curl),
            _act(curl),
            _act(curl),
            'FINAL: {"summary": "收口", "findings": []}',
        ],
        # 子 agent：给自己足够的轮次，好观察"中途拿到"。
        child_replies=[
            _act(curl),
            _act(curl),
            _act(curl),
            'FINAL: {"summary": "子收口", "findings": []}',
        ],
    )
    sandbox = _FakeSandbox(outputs={"curl": _HTTP_000})
    spec = _spec(multi_agent=True, max_turns=24, min_coverage=0)

    outcome = _run(
        runner.solve_in_sandbox(spec, sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )
    assert outcome.error is None

    child_turns = [(system, msgs) for is_child, system, msgs in gateway.log if is_child]
    assert len(child_turns) >= 2, (
        "本用例需要子 agent 至少跑两轮才测得到「中途拿到」—— "
        f"实际只被调了 {len(child_turns)} 次"
    )
    # 首轮**允许**没有（它孵化在升格之前，那时确实还没有这条事实）。
    # 从这个前提反过来说明：如果注入点只在首轮算一次，下面这条必然红。
    later = child_turns[1:]
    assert any("全局约束" in system for system, _ in later), (
        "子 agent 在升格之后的轮次里必须拿到「全局约束」——"
        "它在升格之前就在跑了，注入点若只算首轮，它永远读不到。"
        f"实际各轮：{[('有' if '全局约束' in s else '无') for s, _ in child_turns]}"
    )
    # 而且必须是**每轮都在**（不是昙花一现：那说明它只在某一轮被拼进去）。
    assert "不可达" in later[-1][0], (
        "最后一轮子 agent 的 system prompt 里丢掉了约束 —— "
        "约束是持续状态，不该只报一次就消失"
    )


# ===========================================================================
# v0.92 补：判据的两处静默漏判（2026-10-25 实测发现）
# ===========================================================================
#
# 上一版判据有两个洞，都能让「靶子死了」这件事**静默不报**：
#
# 1. 失败词表只收了 `curl: (7)/(28)/(6)/(56)` 与裸 `000`，漏了
#    `curl: (52)`。它和 `000` 是**同一现象**（TCP 连上了、一个响应字节都
#    没回），区别只在模型写没写 `-w '%{http_code}'`。32231 那次恰好写了
#    `-w`，所以被识别；换一种写法就漏。
#
# 2. `_looks_like_acting` 用的是**子串匹配**：命令里只要出现 `nmap`/
#    `curl` 就算"在打靶"。于是 `cat nmap.log`、`grep curl notes.txt`、
#    `less nmap_scan.xml` 全被算成在打靶 —— 这些一条请求都没发，只是在翻
#    上一次的日志。上一版这个误判只影响"多给一句换向建议"；v0.92 把它接成
#    "写不写黑板事实"的前置之后，代价变成**往黑板写一条假事实**。
#
# 两条都直接对应"模型反复看失败日志 → 平台连续三次认定靶子不可达 →
# 黑板多一条假事实"，所以必须钉住。


def test_curl_52_is_the_same_failure_as_http_000() -> None:
    """`curl -sS` 的 exit 52 与 `-w %{http_code}` 的 `000` 是同一现象。

    同一个靶子、同一份"一个字节没回来"，只因为命令少写一个 `-w`，
    上一版就判成"没失败" —— 静默漏报。这是本次补的第一个洞。
    """
    for output in (
        "curl: (52) Empty reply from server",
        # 大小写与实际输出可能不同（curl 也会走 stderr 混排）
        "CURL: (52) Empty reply from server",
    ):
        assert (
            multiagent._looks_like_collection_failure(
                tool_name="run_shell", command="curl -sS http://1.2.3.4/", output=output
            )
            is True
        ), f"curl (52) 与 000 是同一现象，必须判失败：{output!r}"


def test_tls_handshake_failure_is_not_collection_failure() -> None:
    """`curl: (35)` 是**靶子活着但不说 TLS**，不是"没拿到字节"。

    判成不可达就是把活靶子宣布成死的 —— 正是文件开头警告的那种代价。
    `https://` 打到只跑明文 HTTP 的服务上就是这条，特常见。
    """
    assert (
        multiagent._looks_like_collection_failure(
            tool_name="run_shell",
            command="curl -sS https://1.2.3.4/",
            output="curl: (35) error:0A00010B:SSL routines::wrong version number",
        )
        is False
    )


def test_local_dns_failure_is_not_collection_failure() -> None:
    """`wget: unable to resolve host address` 是**本地解析**失败，没碰到目标。

    与已收的 `could not resolve host`（curl 的措辞）不是一回事：curl 那条
    在 `--resolve` 配错时才出现，确实是"打了但没通"；wget 这条只是本机
    DNS 坏了，靶子可能好好的。
    """
    assert (
        multiagent._looks_like_collection_failure(
            tool_name="run_shell",
            command="wget http://nope.invalid/",
            output="wget: unable to resolve host address 'nope.invalid'",
        )
        is False
    )


def test_reading_old_failure_logs_does_not_count_as_acting() -> None:
    """反复 `cat` 上次的失败日志，一次请求都没发 —— 不能算"在打靶"。

    这是 32231 之后最容易发生的形状：模型看到一堆 `000`，先去翻日志，
    翻出来的日志里当然全是 `connection refused`。若把"翻日志"算成打靶，
    平台就会在模型**完全没碰目标**的情况下连续三次判它不可达，并写进黑板。
    那条假事实会跟着每一轮的 prompt 走，把模型往更错的方向推。
    """
    for command in (
        "cat /tmp/nmap_32231.log",
        "grep curl /tmp/notes.txt",
        "less nmap_scan.xml",
        "cat ./nmap/out.txt | head",
        "vim curl.conf",
        "grep -rn ffuf docs/",
    ):
        assert (
            multiagent._looks_like_collection_failure(
                tool_name="run_shell",
                command=command,
                output="curl: (52) Empty reply from server\nconnection refused",
            )
            is False
        ), f"「{command}」没有向目标发任何请求，不该被判采集失败"


def test_acting_detection_ignores_tool_names_in_arguments() -> None:
    """工具名只在其**作为程序名被执行**时才算数。

    这是把判据从子串匹配收紧到"命令段第一个词"的直接守卫：
    `curl`/`nmap` 出现在文件名、参数、echo 的字符串里都不算。
    """
    assert multiagent._looks_like_acting(tool_name="run_shell", command="cat nmap.log") is False
    assert multiagent._looks_like_acting(tool_name="run_shell", command="grep curl notes.txt") is False
    assert multiagent._looks_like_acting(tool_name="run_shell", command='echo "nmap -p- 10.0.0.5"') is False


def test_acting_detection_still_recognises_real_invocations() -> None:
    """收紧不能收过头 —— 这些**真的**发了请求，必须仍然算在打靶。

    特别是最后几条：包装器（`sudo`/`timeout`/`xargs`）、前置赋值、
    绝对路径、管道里的执行位置。收太紧会让"靶子真死了"变成永远不报。
    """
    for command in (
        "curl -sS http://1.2.3.4/",
        "nmap -Pn -p- 10.0.0.5",
        "ffuf -u http://1.2.3.4/FUZZ -w c.txt",
        "gobuster dir -u http://t -w w.txt",
        "sqlmap -u 'http://t/x?id=1' --batch",
        "nc -zv 1.2.3.4 80",
        "echo | openssl s_client -connect 1.2.3.4:443",
        "cat targets.txt | xargs -n1 curl -s -o /dev/null",
        "curl -s http://t/ | grep -o '<title>[^<]*'",
        "sudo /usr/bin/nmap -sV 10.0.0.5",
        "timeout 30 nmap -p- 10.0.0.5",
        "FOO=1 curl -s http://t/",
        "cd /tmp && curl -s http://t/ && echo done",
    ):
        assert multiagent._looks_like_acting(tool_name="run_shell", command=command) is True, (
            f"「{command}」确实向目标发了请求，必须算行动"
        )


def test_openssl_without_s_client_is_not_acting() -> None:
    """`openssl dgst/sha256/rsa` 是本地运算，`openssl s_client` 才是发请求。"""
    assert multiagent._looks_like_acting(tool_name="run_shell", command="openssl dgst -sha256 f.txt") is False
    assert (
        multiagent._looks_like_acting(tool_name="run_shell", command="openssl s_client -connect t:443")
        is True
    )
