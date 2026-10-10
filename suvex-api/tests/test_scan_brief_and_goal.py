"""任务 12：任务背景（brief）、成功判据（goal）、指令文件、题目附件。

## 这个文件盯的是四件事

1. **brief 真的到了 agent 手里**（不是存在库里就完事）；
2. **判据改变提示词**（三种 goal 的措辞不同，且未知值不炸）；
3. **指令走文件、不走命令行** —— 这是安全修复，要有回归测试；
4. **题目附件进了沙箱命令行**，且路径安全（穿越、覆写都挡住）。

第 3 条最要紧：**它是"凭据不出现在 `ps aux` 里"的唯一保证**，
而那种回归是静默的（功能照样能用，只是凭据被同机所有用户看到）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.vocab import ScanGoal
from app.scans import uploads
from app.scans.runner import FakeRunner, LaunchSpec, StrixRunner

API = "/api/v1"
AUTH_STATEMENT = "我有权对此目标进行安全测试"


def _spec(**overrides) -> LaunchSpec:  # noqa: ANN003
    """一个最小的 LaunchSpec。默认值让每个用例只写它关心的字段。"""
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
# 词表对账（防止"抄了一份"漂移）
# ══════════════════════════════════════════════════════════


def test_goal_literal_matches_vocab() -> None:
    """★ `GoalLiteral` 必须与 `vocab.ScanGoal` 的取值**完全一致**。

    这是那个"抄了一份"的隐患的机械保险。本项目在
    `findings/schemas.py` 的 `StatusLiteral` 上真的踩过：
    v0.14 把状态从 4 个扩到 6 个，改了两处、漏了第三处，
    于是「提交复测」被 pydantic 拦成 422 —— 而错误消息只有一句
    "Input should be 'new', 'confirmed', ..."，看不出是漏改。

    所以这条测试比对**两个地方**（vocab 与 Literal），而不是靠人眼。
    """
    from typing import get_args

    from app.scans.schemas import GoalLiteral

    assert set(get_args(GoalLiteral)) == {member.value for member in ScanGoal}


def test_goal_clause_covers_every_goal() -> None:
    """每种判据都要有一句话 —— 少一种就会静默退回默认。

    `_goal_clause` 对未知取值会退回 `findVulns` 并**记警告**，
    所以漏一种不会报错，只会让那个判据悄悄失效。
    这条测试把"漏了"变成红灯。
    """
    from app.scans.runner import GOAL_CLAUSE

    assert {member.value for member in ScanGoal} <= set(GOAL_CLAUSE)


# ══════════════════════════════════════════════════════════
# brief 与 goal 的持久化
# ══════════════════════════════════════════════════════════


def _create(client: TestClient, provider_id: str, **extra) -> dict:  # noqa: ANN003
    payload = {
        "target": "example.com",
        "targetKind": "url",
        "skills": ["web"],
        "providerId": provider_id,
        "authorization": {"confirmed": True, "statement": AUTH_STATEMENT},
    }
    payload.update(extra)
    response = client.post(f"{API}/scans", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_brief_and_goal_round_trip(
    ready_client: TestClient, provider_id: str, monkeypatch
) -> None:
    """★ brief 与 goal 存下来、并**原样回显**。

    回显是必需的：用户需要确认"我写的那段背景确实传上去了"。
    没有它，他只能靠"扫描结果看起来对不对"去反推 —— 而那不是验证。
    """
    from app.scans.reachability import Reachability

    monkeypatch.setattr(
        "app.scans.service.check_reachability",
        lambda target, **kw: Reachability(None, None, False, False),
    )

    brief = "题目：LogVault 日志模板引擎的污染数据流。\n注意 `%hhn` 与 varargs。"
    body = _create(ready_client, provider_id, brief=brief, goal="captureFlag")

    assert body["brief"] == brief, "brief 必须原样回显（不做改写）"
    assert body["goal"] == "captureFlag"


def test_goal_defaults_to_find_vulns(
    ready_client: TestClient, provider_id: str, monkeypatch
) -> None:
    """不传 goal 时是 `findVulns` —— 例行找漏洞是绝大多数场景。"""
    from app.scans.reachability import Reachability

    monkeypatch.setattr(
        "app.scans.service.check_reachability",
        lambda target, **kw: Reachability(None, None, False, False),
    )

    body = _create(ready_client, provider_id)
    assert body["goal"] == "findVulns"
    # 没写背景就是 null，**不是空串** ——
    # 前端要把"我没写"与"我写了个空的"渲染成不同的话
    assert body["brief"] is None


def test_blank_brief_becomes_null(
    ready_client: TestClient, provider_id: str, monkeypatch
) -> None:
    """全是空白的 brief 归一成 NULL。

    `""` 与 `"   "` 与"没填"是同一件事。存空串会让前端的
    "我没写背景"显示成"背景是空的"，读起来像出了错。
    """
    from app.scans.reachability import Reachability

    monkeypatch.setattr(
        "app.scans.service.check_reachability",
        lambda target, **kw: Reachability(None, None, False, False),
    )

    assert _create(ready_client, provider_id, brief="   \n  ")["brief"] is None


def test_overlong_brief_is_rejected(
    ready_client: TestClient, provider_id: str, monkeypatch
) -> None:
    """超长 brief → 400，而**不是静默截断**。

    ⚠️ 截断掉的可能正好是最后那句 flag 格式 —— 而用户以为交上去了。
    明确报错比悄悄丢数据好。
    """
    from app.scans.reachability import Reachability

    monkeypatch.setattr(
        "app.scans.service.check_reachability",
        lambda target, **kw: Reachability(None, None, False, False),
    )

    response = ready_client.post(
        f"{API}/scans",
        json={
            "target": "example.com",
            "targetKind": "url",
            "skills": ["web"],
            "providerId": provider_id,
            "authorization": {"confirmed": True, "statement": AUTH_STATEMENT},
            "brief": "x" * 8001,
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_unknown_goal_is_rejected(
    ready_client: TestClient, provider_id: str, monkeypatch
) -> None:
    """非法判据 → 422，不是静默按默认处理。"""
    from app.scans.reachability import Reachability

    monkeypatch.setattr(
        "app.scans.service.check_reachability",
        lambda target, **kw: Reachability(None, None, False, False),
    )

    response = ready_client.post(
        f"{API}/scans",
        json={
            "target": "example.com",
            "targetKind": "url",
            "skills": ["web"],
            "providerId": provider_id,
            "authorization": {"confirmed": True, "statement": AUTH_STATEMENT},
            "goal": "pwnIt",
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_rerun_inherits_brief_and_goal(
    ready_client: TestClient, provider_id: str, monkeypatch, db_session
) -> None:
    """★ 重跑继承任务背景与判据。

    用户重跑通常是因为"上次没跑出来"或"目标改版了再看一遍"，
    而**题面一个字都没变**。不继承的话他会得到一次完全没有背景的扫描，
    还得手工再填一遍。
    """
    from app.scans.reachability import Reachability

    monkeypatch.setattr(
        "app.scans.service.check_reachability",
        lambda target, **kw: Reachability(None, None, False, False),
    )

    created = _create(
        ready_client, provider_id, brief="复测要求：确认 /debug/env 已加鉴权。", goal="verifyFix"
    )

    response = ready_client.post(
        f"{API}/scans/{created['id']}/rerun",
        json={"authorization": {"confirmed": True, "statement": AUTH_STATEMENT}},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["id"] != created["id"], "重跑是新建一条记录"
    assert body["brief"] == "复测要求：确认 /debug/env 已加鉴权。"
    assert body["goal"] == "verifyFix"


# ══════════════════════════════════════════════════════════
# 提示词：brief 的位置与判据的措辞
# ══════════════════════════════════════════════════════════


def test_brief_comes_first_in_the_instruction() -> None:
    """★ 用户的任务背景必须排在**最前**。

    这不是排版偏好：实测的抱怨是「只给网址让他扫描太没意义了」，
    而题面里的信息量（考点、提示、诱饵、flag 格式）远大于我们能猜的。
    放在最前，agent 读到的第一件事就是"这道题在考什么"。

    同时也确认它被**明确标注**为用户提供的资料 —— 题面本身可能
    含攻击性文本（那正是题目内容），标清角色能让模型把它当资料读。
    """
    from app.scans.runner import GOAL_CLAUSE

    text = StrixRunner._instruction(_spec(brief="考点：SSTI in /hello"))

    assert "考点：SSTI in /hello" in text
    # 排在技能范围那句话之前
    assert text.index("考点：SSTI in /hello") < text.index("本次测试的侧重范围")
    # 被标注成"用户提供的资料"，而不是裸文本
    assert "用户提供的任务背景" in text
    # 判据那句话也在（默认 findVulns）
    assert GOAL_CLAUSE["findVulns"] in text


def test_goal_choices_produce_different_instructions() -> None:
    """★ 三种判据给出**不同**的提示词。

    判据改变的是 agent 的**取舍**，不只是报告格式：
    夺旗时"深挖一个点拿到 flag"比"广撒网报低危"有价值，而默认行为是后者。
    """
    texts = {
        goal: StrixRunner._instruction(_spec(goal=goal))
        for goal in ("findVulns", "captureFlag", "verifyFix")
    }

    # 两两不同
    assert len(set(texts.values())) == 3
    # 夺旗要求显式写出 flag（否则"拿到了"这件事无法核实）
    assert "flag" in texts["captureFlag"].lower()
    # 复测要求与"顺带发现的新问题"分开，否则用户要的答案会被淹掉
    assert "复测" in texts["verifyFix"]


def test_unknown_goal_falls_back_without_crashing() -> None:
    """未知判据退回默认，**不抛异常**。

    走到这里只可能是库里有别的版本的枚举值（迁移期间）。
    那时扫描应该照跑，而不是起不来。
    """
    text = StrixRunner._instruction(_spec(goal="somethingElse"))
    assert "例行安全评估" in text


def test_space_separated_instruction_is_gone() -> None:
    """★ 段落用换行分隔，**不是空格**。

    题面是多行的（题目、提示、附件清单、测试账号）。用空格拼会把
    它们揉成一整段，而段落边界正是模型区分
    "这是题面 / 这是我们的口径要求"的依据。
    """
    text = StrixRunner._instruction(_spec(brief="第一行\n第二行"))
    assert "第一行\n第二行" in text
    assert "\n\n" in text, "段落之间应当是空行"


# ══════════════════════════════════════════════════════════
# 探索与止损协议（任务 13）
# ══════════════════════════════════════════════════════════


def test_exploration_protocol_is_in_the_instruction() -> None:
    """★ 探索与止损协议真的进了提示词。

    它要治的是实测里那 61.5 分钟中"一轮没产出新信息"的空转：
    子 agent 对未知 slot 布局做无假设穷举（slot 5-12），直到 Root 发现才叫停。
    """
    from app.scans.runner import EXPLORATION_PROTOCOL

    text = StrixRunner._instruction(_spec())

    assert EXPLORATION_PROTOCOL in text
    # 三条核心约束都要在
    assert "不得连续枚举超过 4 个值" in text
    assert "needs_follow_up" in text
    assert "ruled_out" in text


def test_protocol_lands_after_goal_but_before_skill_scope() -> None:
    """★ 协议排在判据之后、技能范围之前。

    先讲清"要什么"（判据），再讲"怎么走"（协议），最后才是"往哪看"（范围）。
    顺序反了会让模型先看到范围、把协议当补充说明读。
    """
    from app.scans.runner import GOAL_CLAUSE

    text = StrixRunner._instruction(_spec(goal="captureFlag"))

    assert text.index(GOAL_CLAUSE["captureFlag"]) < text.index("探索与止损协议")
    assert text.index("探索与止损协议") < text.index("本次测试的侧重范围")


def test_protocol_does_not_promise_a_nonblocking_inbox_check() -> None:
    """★★ 协议**不得**要求"每完成一个子步骤就查收邮箱"。

    这是本协议落地时最重要的一处修正，理由是读源码得来的：

      · 全套工具里**没有**非阻塞查收邮箱的调用，只有 `wait_for_agents`，
        而它会阻塞到有消息或超时（默认 300s）—— 不存在"看一眼"这种调用；
      · 本平台以 `--non-interactive` 运行，`interrupt_on_message` 因此为
        False，停止类消息**不会**打断正在进行的 turn。
        实测残留可印证：那次运行的 `.state/agents.json` 里 `pending_counts`
        显示某子 agent 有 3 条消息直到扫描结束都没被消费。

    所以协议改为把止损前移到"自检"，并显式说明不要指望中途被叫停。
    这条断言防的是**有人照着原稿把不成立的要求加回来**。
    """
    from app.scans.runner import EXPLORATION_PROTOCOL

    # 必须点明"没有顺手查收的工具"与"不会打断"
    assert "没有" in EXPLORATION_PROTOCOL and "邮箱" in EXPLORATION_PROTOCOL
    assert "不会" in EXPLORATION_PROTOCOL and "打断" in EXPLORATION_PROTOCOL
    # 且明确交代 wait_for_agents 只在真在等人时才用
    assert "wait_for_agents" in EXPLORATION_PROTOCOL


def test_protocol_tells_root_to_brief_its_subagents() -> None:
    """★ 协议要求 Root 把约束传导给子 agent。

    子 agent 收到的是 `create_agent` 的 `task` 字符串，**看不到**这份
    instruction（源码：`--instruction-file` 只喂给根 agent 的初始输入）。
    所以"子 agent 也要遵守"这件事，只能靠 Root 转达 —— 得写进去。
    """
    from app.scans.runner import EXPLORATION_PROTOCOL

    assert "create_agent" in EXPLORATION_PROTOCOL
    assert "子 agent" in EXPLORATION_PROTOCOL


# ══════════════════════════════════════════════════════════
# ★ 指令走文件（安全修复的回归测试）
# ══════════════════════════════════════════════════════════


def test_instruction_never_appears_on_the_command_line(tmp_path: Path) -> None:
    """★★ `brief` 的内容**绝不出现在命令行里**。

    这是本次改动里最要紧的一条安全保证，而它的回归是**静默的**：
    功能照样能用，只是凭据被同机所有用户 `ps aux` 看到、
    被任何日志采集抓走。

    用户会在任务背景里写测试账号 —— strix 官方文档自己举的例子就是
    `Use the following credentials: admin:password123`。
    所以这条测试用一句"看起来像凭据"的文本，断言 argv 里**没有它**。
    """
    secret = "admin:SuperSecret123"
    spec = _spec(brief=f"测试账号：{secret}", run_dir=tmp_path)

    argv = StrixRunner().command(spec)

    joined = " ".join(argv)
    assert secret not in joined, "凭据出现在了命令行里 —— 它会被 ps aux 看到"
    # 而且**不该**再用 --instruction 那条不安全的路径
    assert "--instruction" not in argv
    assert "--instruction-file" in argv


def test_instruction_file_is_written_and_readable(tmp_path: Path) -> None:
    """指令落盘到 `instruction.txt`，内容完整。"""
    spec = _spec(brief="题面在这里", run_dir=tmp_path)
    argv = StrixRunner().command(spec)

    index = argv.index("--instruction-file")
    path = Path(argv[index + 1])

    assert path.exists(), "指令文件没写出来，扫描会拿不到任何指示"
    assert path.parent == tmp_path
    content = path.read_text(encoding="utf-8")
    assert "题面在这里" in content


def test_instruction_file_is_owner_only_on_posix(tmp_path: Path) -> None:
    """POSIX 上指令文件是 `0o600`。

    它可能含凭据，而同机其他用户没有理由读它。
    ⚠️ Windows 上 POSIX 权限位不适用，那里跳过 —— 如实标注，
    而不是假装它生效了。
    """
    import os
    import stat

    if os.name != "posix":
        pytest.skip("POSIX 权限位在 Windows 上不适用")

    spec = _spec(brief="账号 admin:x", run_dir=tmp_path)
    argv = StrixRunner().command(spec)
    path = Path(argv[argv.index("--instruction-file") + 1])

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"指令文件权限是 {oct(mode)}，应为 0o600"


def test_summary_does_not_leak_brief() -> None:
    """`summary()` 是**会进日志**的，所以不能带 brief。

    日志的保留期与可达面都远大于数据库里那一列
    （见 `core/logging.py` 的脱敏说明）。
    """
    spec = _spec(brief="admin:SuperSecret123", llm_api_key="sk-also-secret")
    summary = spec.summary()

    assert "SuperSecret123" not in summary
    assert "sk-also-secret" not in summary
    assert "brief=有" in summary, "但要说清有没有背景（排查时有用）"


# ══════════════════════════════════════════════════════════
# 题目附件
# ══════════════════════════════════════════════════════════


def test_staging_then_claim_puts_files_on_the_scan(
    ready_client: TestClient, provider_id: str, monkeypatch
) -> None:
    """★ 先暂存、创建时认领 —— 附件最终落在该扫描名下。

    这条测试是那个**顺序死结**的回归测试：第一版设计是
    「先建扫描、再往 `/{id}/attachments` 传」，而扫描创建后毫秒级
    被调度器认领，所以那条路稳定 409 —— 附件功能等于不存在。
    """
    from app.scans.reachability import Reachability

    monkeypatch.setattr(
        "app.scans.service.check_reachability",
        lambda target, **kw: Reachability(None, None, False, False),
    )

    # ① 暂存（此时还没有扫描）
    staged = ready_client.post(
        f"{API}/scans/attachments/staging",
        files={
            "files": ("chall", b"\x7fELF\x02\x01\x01", "application/octet-stream"),
        },
    )
    assert staged.status_code == 201, staged.text
    body = staged.json()
    assert body["stagingId"].startswith("stage")
    # ⚠️ 无扩展名的文件**必须被接受**：CTF 附件经常就叫 `chall`
    assert [f["name"] for f in body["files"]] == ["chall"]

    # ② 创建时带上 stagingId
    created = _create(ready_client, provider_id, attachmentStagingId=body["stagingId"])

    listed = ready_client.get(f"{API}/scans/{created['id']}/attachments")
    assert listed.status_code == 200, listed.text
    assert [r["name"] for r in listed.json()] == ["chall"]
    assert listed.json()[0]["sizeBytes"] == 7


def test_staging_is_single_use(ready_client: TestClient, provider_id: str, monkeypatch) -> None:
    """★ 一个暂存批次只能被认领一次（认领 = 移动）。

    第二次用它不该报错，但**也拿不到文件** —— 否则同一次上传会被
    复制进两次扫描，而用户只选过一次。
    """
    from app.scans.reachability import Reachability

    monkeypatch.setattr(
        "app.scans.service.check_reachability",
        lambda target, **kw: Reachability(None, None, False, False),
    )

    staged = ready_client.post(
        f"{API}/scans/attachments/staging",
        files={"files": ("a.bin", b"data", "application/octet-stream")},
    ).json()

    first = _create(ready_client, provider_id, attachmentStagingId=staged["stagingId"])
    second = _create(ready_client, provider_id, attachmentStagingId=staged["stagingId"])

    assert len(ready_client.get(f"{API}/scans/{first['id']}/attachments").json()) == 1
    assert ready_client.get(f"{API}/scans/{second['id']}/attachments").json() == []


def test_create_without_staging_just_has_no_attachments(
    ready_client: TestClient, provider_id: str, monkeypatch
) -> None:
    """不传 stagingId 是**正常情况**（大多数扫描没有附件），不是错误。"""
    from app.scans.reachability import Reachability

    monkeypatch.setattr(
        "app.scans.service.check_reachability",
        lambda target, **kw: Reachability(None, None, False, False),
    )
    created = _create(ready_client, provider_id)
    assert ready_client.get(f"{API}/scans/{created['id']}/attachments").json() == []


def test_unknown_staging_id_does_not_block_creation(
    ready_client: TestClient, provider_id: str, monkeypatch
) -> None:
    """★ 暂存 id 失效（过期/已认领）**不能**让扫描建不出来。

    附件丢了是次要问题，而"扫描建不出来"会让用户完全没法干活。
    所以 `claim` 对不存在的批次返回空列表而不是抛异常。
    """
    from app.scans.reachability import Reachability

    monkeypatch.setattr(
        "app.scans.service.check_reachability",
        lambda target, **kw: Reachability(None, None, False, False),
    )
    created = _create(ready_client, provider_id, attachmentStagingId="stage_nonexistent")
    assert created["id"].startswith("scan_")


def test_upload_to_scan_is_rejected_after_start(
    ready_client: TestClient, provider_id: str, monkeypatch, db_session
) -> None:
    """扫描启动后再往 `/{id}/attachments` 传 → 409，并说清原因。

    这条与上面的暂存流程是**互补**的：暂存解决"向导里的附件"，
    而按 id 上传只在扫描还在排队时有意义（并发满了时它确实会等一会儿）。
    启动之后传，文件会躺在磁盘上但永远不会被 agent 看到 ——
    那比拒绝更糟：用户以为交上去了。
    """
    from app.scans.reachability import Reachability

    monkeypatch.setattr(
        "app.scans.service.check_reachability",
        lambda target, **kw: Reachability(None, None, False, False),
    )
    created = _create(ready_client, provider_id)

    from app.scans.models import Scan

    scan = db_session.get(Scan, created["id"])
    scan.status = "running"
    db_session.commit()

    response = ready_client.post(
        f"{API}/scans/{created['id']}/attachments",
        files={"files": ("late.bin", b"x", "application/octet-stream")},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SCAN_ALREADY_STARTED"


def test_attachment_path_traversal_is_neutralised() -> None:
    """★★ 恶意文件名穿越不出去。

    与漏洞附件**不同**：这边必须保留原名（agent 靠名字认东西），
    所以安全靠的是**消毒文件名**而不是"用 id 当存储名"。
    """
    assert uploads.safe_filename("../../../../etc/passwd") == "passwd"
    assert uploads.safe_filename("..\\..\\windows\\system32\\cmd.exe") == "cmd.exe"
    # 控制字符与引号
    assert "\x00" not in uploads.safe_filename("a\x00b.bin")
    # 全是不安全字符时给兜底名，而不是空串
    assert uploads.safe_filename("../") == "attachment"
    assert uploads.safe_filename("") == "attachment"


def test_same_name_does_not_overwrite(tmp_path: Path, monkeypatch) -> None:
    """★ 两个都叫 `chall` 的附件不能互相覆盖。

    "覆盖掉的可能是这次分析唯一正确的那个附件"。
    """
    monkeypatch.setenv("SUPEX_SCAN_WORK_ROOT", str(tmp_path))
    from app.core.config import get_settings

    get_settings.cache_clear()

    first, _ = uploads.store(scan_id="scan_x", filename="chall", content=b"FIRST")
    second, _ = uploads.store(scan_id="scan_x", filename="chall", content=b"SECOND")

    assert first == "chall"
    assert second != first, "同名必须换一个名字，不能覆盖"
    from app.scans.runner import scan_upload_dir

    assert (scan_upload_dir("scan_x") / first).read_bytes() == b"FIRST"
    assert (scan_upload_dir("scan_x") / second).read_bytes() == b"SECOND"

    get_settings.cache_clear()


def test_workspace_files_reach_the_command_line(tmp_path: Path, monkeypatch) -> None:
    """★ 附件真的出现在 `--workspace-file` 里，落在 `attachments/` 下。

    这是"题目附件能被 agent 看到"的端到端保证：文件传上来了、
    但没有进命令行的话，agent 在沙箱里根本找不到它。
    """
    monkeypatch.setenv("SUPEX_SCAN_WORK_ROOT", str(tmp_path))
    from app.core.config import get_settings

    get_settings.cache_clear()

    uploads.store(scan_id="scan_y", filename="libc.so.6", content=b"\x7fELF")
    uploads.store(scan_id="scan_y", filename="wordlist.txt", content=b"admin\n")

    spec = _spec(scan_id="scan_y", run_dir=tmp_path / "run")
    argv = StrixRunner().command(spec)
    joined = " ".join(argv)

    assert "--workspace-file" in argv
    assert "libc.so.6" in joined
    assert "wordlist.txt" in joined
    # 目录约定：沙箱内落在 attachments/ 下（与目标目录分开）
    assert "attachments/libc.so.6" in joined

    get_settings.cache_clear()


def test_no_workspace_files_when_none_uploaded(tmp_path: Path, monkeypatch) -> None:
    """没传附件时**不加** `--workspace-file`。

    加一个指向空目录或不存在路径的参数会让 strix 报错，
    而"这次没传附件"是绝对正常的情况。
    """
    monkeypatch.setenv("SUPEX_SCAN_WORK_ROOT", str(tmp_path))
    from app.core.config import get_settings

    get_settings.cache_clear()

    argv = StrixRunner().command(_spec(scan_id="scan_empty", run_dir=tmp_path / "run"))
    assert "--workspace-file" not in argv

    get_settings.cache_clear()


def test_attachment_size_limit_uses_real_bytes(tmp_path: Path, monkeypatch) -> None:
    """上限按**真实字节数**判，不是按 Content-Length（后者可被撒谎）。"""
    import pytest as _pytest

    from app.core.errors import ApiError

    monkeypatch.setenv("SUPEX_SCAN_WORK_ROOT", str(tmp_path))
    from app.core.config import get_settings

    get_settings.cache_clear()

    monkeypatch.setattr(uploads, "MAX_BYTES", 4)
    with _pytest.raises(ApiError) as excinfo:
        uploads.store(scan_id="scan_z", filename="big.bin", content=b"12345678")
    assert "上限" in excinfo.value.message

    get_settings.cache_clear()


def test_delete_attachment_is_path_safe(tmp_path: Path, monkeypatch) -> None:
    """删除也走消毒 —— 否则 `../` 能删掉任意文件。

    路径参数是用户可控的，直接用 `directory / name` 就是经典的
    "任意文件删除"。
    """
    monkeypatch.setenv("SUPEX_SCAN_WORK_ROOT", str(tmp_path))
    from app.core.config import get_settings

    get_settings.cache_clear()

    # 放一个"目录外的"文件，确认穿越删不掉它
    outside = tmp_path / "important.txt"
    outside.write_text("must survive", encoding="utf-8")
    uploads.store(scan_id="scan_w", filename="keep.bin", content=b"x")

    from app.core.errors import ApiError

    with pytest.raises(ApiError):
        uploads.delete_for("scan_w", "../../important.txt")

    assert outside.exists(), "穿越删除必须被挡住"

    # 正常的那个能删掉
    uploads.delete_for("scan_w", "keep.bin")
    from app.scans.runner import scan_upload_dir

    assert not (scan_upload_dir("scan_w") / "keep.bin").exists()

    get_settings.cache_clear()


def test_upload_writes_an_audit_row(
    ready_client: TestClient, provider_id: str, monkeypatch
) -> None:
    """上传题目附件要留痕。

    这些文件会被送进那个**会执行任意命令**的沙箱 ——
    "谁在什么时候把哪个文件交给了哪次扫描"必须可回答。
    ⚠️ 审计里只记**文件名**，不记内容。

    ★ 走**暂存**那条路（用户实际的路径）—— 而不是"建完再传"。
    后者会 409（创建后毫秒级被调度器认领），于是这条测试会
    静默地什么都没验证到（我第一版就是这样，它红了）。
    """
    from app.scans.reachability import Reachability

    monkeypatch.setattr(
        "app.scans.service.check_reachability",
        lambda target, **kw: Reachability(None, None, False, False),
    )

    # ① 暂存（这一步就该留痕 —— 文件已经落到磁盘上了）
    staged = ready_client.post(
        f"{API}/scans/attachments/staging",
        files={"files": ("chall.bin", b"data", "application/octet-stream")},
    )
    assert staged.status_code == 201, staged.text

    # ② 认领进一次扫描
    _create(ready_client, provider_id, attachmentStagingId=staged.json()["stagingId"])

    audit = ready_client.get(f"{API}/audit?limit=5")
    actions = [row["action"] for row in audit.json()["entries"]]
    assert "scan.attach" in actions, f"审计里没有扫描附件动作：{actions}"


def test_list_attachments_404s_for_unknown_scan(ready_client: TestClient) -> None:
    """对不存在的扫描返回 404，而不是空列表。

    空列表会把一个笔误显示成"这次扫描没有附件"。
    """
    response = ready_client.get(f"{API}/scans/scan_does_not_exist/attachments")
    assert response.status_code == 404


def test_staging_sweep_removes_expired_batches_only(
    tmp_path: Path, monkeypatch
) -> None:
    """★ 过期暂存批次被清掉，**而已认领的附件绝不能被误伤**。

    ⚠️ 这两条路径只差一级：
        `_uploads/_staging/<批次>/`   ← 该清
        `_uploads/<scan_id>/`         ← 绝不该清

    所以 `sweep_staging` 必须精确匹配 `_staging` 这一级。
    写错一个 `glob("**")` 就会把用户真正在用的题目附件一起删掉 ——
    那是数据丢失，不是垃圾清理。

    同时确认：未过期的批次**不清**（用户可能还在向导里填着）。
    """
    import os
    import time

    monkeypatch.setenv("SUPEX_SCAN_WORK_ROOT", str(tmp_path))
    from app.core.config import get_settings

    get_settings.cache_clear()

    # ① 一个已认领的附件（必须活下来）
    uploads.store(scan_id="scan_alive", filename="precious.bin", content=b"keep me")

    # ② 一个过期批次 + 一个新鲜批次
    uploads.store_staged(staging_id="stage_old", filename="old.bin", content=b"old")
    uploads.store_staged(staging_id="stage_new", filename="new.bin", content=b"new")

    # 把"过期批次"的 mtime 往回拨到 TTL 之外
    old_dir = uploads.staging_dir("stage_old")
    past = time.time() - uploads.STAGING_TTL_SECONDS - 60
    os.utime(old_dir, (past, past))

    removed = uploads.sweep_staging(older_than_seconds=uploads.STAGING_TTL_SECONDS)

    assert removed == 1, "只该清掉过期的那一个"
    assert not uploads.staging_dir("stage_old").exists(), "过期批次该被清掉"
    assert uploads.staging_dir("stage_new").is_dir(), "新鲜批次不该被清"

    from app.scans.runner import scan_upload_dir

    assert (scan_upload_dir("scan_alive") / "precious.bin").exists(), (
        "已认领的附件被误删了 —— 清理范围写错了"
    )

    get_settings.cache_clear()


def test_retention_sweep_reports_staging(db_session, tmp_path: Path, monkeypatch) -> None:
    """保留期清理的返回值里带 `stagingSwept`（任务 12 接线）。

    这是"暂存目录真的会被清"的接线保证 —— 函数写好了但没被调用
    是很容易发生的事（本项目在 `_refresh_scan_rollups` 上踩过一次：
    函数写好了却没有调用点，于是列表说"未发现漏洞"而漏洞页有两条）。
    """
    monkeypatch.setenv("SUPEX_SCAN_WORK_ROOT", str(tmp_path))
    from app.core.config import get_settings

    get_settings.cache_clear()

    from app.scans import service

    result = service.sweep_retention(db_session)
    assert "stagingSwept" in result, "清理结果里必须报告暂存批次数"

    get_settings.cache_clear()


def test_fake_runner_ignores_the_new_fields() -> None:
    """FakeRunner 仍然能用 —— 它不该因为新字段而炸。

    测试大量使用 fake runner（`SUPEX_SCAN_RUNNER=fake`），
    而新增 `brief` / `goal` 让 `LaunchSpec` 的形状变了。
    """
    spec = _spec(brief="背景", goal="captureFlag")
    command = FakeRunner().command(spec)
    assert isinstance(command, list)
    assert command
