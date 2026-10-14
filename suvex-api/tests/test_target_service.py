"""裸 TCP 目标（目标类型 `service`）—— 任务 15。

## 为什么这个类型必须存在

CTF 里大量题目是**裸 TCP 端口**（Pwn、网络 Misc、Crypto 预言机），
平台给的是一句连接命令：

    openssl s_client -connect host:9999 -servername host -quiet

在这之前 suvex 只有 `url` / `repo` / `path` 三类，而 `url` 校验强制要求
输入像 URL —— 于是这类题目**连创建都创建不了**（实测报"不是合法的 URL"）。

## 为什么"粘贴即识别"值得写正则

让用户从那条命令里**手工抄出** `host:9999` 是白费一遍手，而抄错一个
字符的代价是"对着不存在的主机发包"，事后极难归因。

## 为什么还要一段指令

执行后端（strix 1.6.2）的类型系统里**没有 TCP 服务这一类** ——
`infer_target_type` 对"含点且不含斜杠"的输入一律判成 `web_application`
并补 `https://`，而 `--target` 没有任何写法能表达出"这是裸 TCP"。
我们改不了它的分类，只能在自己的指令里明确否定它。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.scans import targets
from app.scans.runner import LaunchSpec, StrixRunner

# ══════════════════════════════════════════════════════════
# 粘贴即识别
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.2.3.4:9999", "1.2.3.4:9999"),
        ("host.example.com:1337", "host.example.com:1337"),
        # ★ 平台给的原样命令 —— 这是这个类型存在的主要理由
        (
            "openssl s_client -connect 35b162d9cea6ede7ab1ee2cc.tcp-ctf2.dasctf.com:9999 "
            "-servername 35b162d9cea6ede7ab1ee2cc.tcp-ctf2.dasctf.com -quiet",
            "35b162d9cea6ede7ab1ee2cc.tcp-ctf2.dasctf.com:9999",
        ),
        ("nc 1.2.3.4 9999", "1.2.3.4:9999"),
        ("ncat --ssl host.example.com 443", "host.example.com:443"),
        ("socat - TCP:example.com:1337", "example.com:1337"),
    ],
)
def test_service_target_is_extracted_and_normalized(raw: str, expected: str) -> None:
    check = targets.validate("service", raw)
    assert check.ok, check.error
    assert targets.normalize("service", raw) == expected


def test_pasted_command_reports_what_it_extracted() -> None:
    """从命令里抽出来的地址要**说出来**。

    否则用户看到的是一串自己没填过的值（存储的是归一化结果），
    而无法确认抽对了没有。
    """
    check = targets.validate("service", "nc 1.2.3.4 9999")
    assert check.hint is not None
    assert "1.2.3.4:9999" in check.hint


def test_hint_says_it_is_not_http() -> None:
    """★ 提示必须点明"不是 HTTP"。

    这是用户第一次被告知该目标形态的地方，口径要与发下去的指令一致 ——
    两处不一致会让用户按 A 理解、而 agent 按 B 执行。
    """
    check = targets.validate("service", "1.2.3.4:9999")
    assert check.ok
    assert "不是 HTTP" in (check.hint or "")


# ══════════════════════════════════════════════════════════
# 拒绝
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "raw",
    [
        "这不是目标",
        "example.com:99999",  # 端口越界
        "example.com:0",  # 端口下限
        "justhost:80",  # 不含点 —— 多半是抄漏了域名
        "",
    ],
)
def test_bad_service_targets_are_rejected(raw: str) -> None:
    assert not targets.validate("service", raw).ok


def test_default_name_keeps_the_port() -> None:
    """同一主机的不同端口是**两个不同的服务** —— 名字里丢了端口就分不出来。"""
    assert targets.default_name("service", "nc a.example.com 8080") == "a.example.com:8080"


def test_localhost_is_allowed_like_the_url_kind() -> None:
    """`localhost` 明确放行 —— 本地服务是合理目标（与 url 那条同规则）。"""
    assert targets.validate("service", "localhost:1234").ok


# ══════════════════════════════════════════════════════════
# 指令：纠正后端的类型误判
# ══════════════════════════════════════════════════════════


def _spec(kind: str) -> LaunchSpec:
    return LaunchSpec(
        scan_id="scan_x",
        target="h.example.com:9999",
        target_kind=kind,
        skills=(),
        run_dir=Path("_unused"),
        log_path=Path("_unused/s.log"),
        llm_model="openai/test",
        llm_api_key="sk-t",
        goal="captureFlag",
    )


def test_service_instruction_contradicts_the_backend_label() -> None:
    """★★ 这一段是这个类型能不能用的**关键**。

    后端会把 `host:9999` 标成 `web_application` + `https://`，而且没有任何
    参数能表达"这是原始 TCP"。改不了它的分类，只能在指令里**明确否定**它。

    ⚠️ 而且必须**直说那句标注是错的**：委婉地写"注意这可能是 TCP"会被
    "系统提示词优先"这条规则压过去 —— 那正是本类型要修的故障模式
    （工具说错了话、agent 信了，与踩过的"https/http 搞反白跑 32 分钟"同类）。
    """
    text = StrixRunner._instruction(_spec("service"))

    assert "那个标注是错的" in text
    assert "不是网站" in text
    # 要给出**正确做法**，不能只说"别用 HTTP"——那会留下"那用什么"的空档
    assert "nc" in text
    assert "socket" in text


def test_other_target_kinds_do_not_get_the_tcp_clause() -> None:
    """只有 TCP 目标带这段。

    否则每一次 Web 扫描都会读到一句"忽略后端的类型标注" ——
    那会动摇它对**其它**指令的信任，代价远大于这一段的收益。
    """
    assert "那个标注是错的" not in StrixRunner._instruction(_spec("url"))
    assert "那个标注是错的" not in StrixRunner._instruction(_spec("path"))
