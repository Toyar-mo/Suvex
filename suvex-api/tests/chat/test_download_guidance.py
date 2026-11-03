"""对话系统提示词里的「大附件下载」指引（v1.60）。

## 这个模块在钉什么

线上真事（2026-11-03，CTF 会话 chat_01M6GAVGDD7JTENZ2ZANK7CT4W）：
MISC 题的附件 `misc.zip` 有 **1.9GB**，模型用一条 `run_shell` 里的
`curl -o misc.zip …` 去下，`timeout_seconds` 被夹到 300 秒上限
（`app/kernel/runner.py::_tool_run_shell`：`min(timeout, DEFAULT_SHELL_TIMEOUT)`，
`DEFAULT_SHELL_TIMEOUT=300`），结果 **只下了 482MB 就被杀**（curl rc=28），
而且：
  - 命令里还叠了 `curl -m 60`，60 秒就超时；
  - 没有用 `curl -C -` 续传，每次重下都从 0 开始；
  - 写到 `/work/t`（容器可写层，回收即丢）而不是 `/work/persist/`。

于是这一题被"下了一半超时 / 沙箱回收重来"打断了好几次。root cause 不在
工具本身（300 秒上限是合理的，一条卡死的命令不该拖垮整道题），而在于
**提示词没教模型怎么下超过一条命令时限的大文件**。

所以这里断言：系统提示词里必须有一段明确教「大文件下载 = 断点续传 +
落到持久目录 + 单命令给一次机会」。删掉这段就会让模型重新踩同一个坑，
这条测试就是那个坑的守门员。
"""

from __future__ import annotations

from app.chat.prompts import build_system_prompt


def _prompt() -> str:
    # 不带参数调用 = 最常见路径（没选人格/没挂技能/没开审批），
    # 得到的仍是 SYSTEM_PROMPT_TEMPLATE 渲染后的全文。
    return build_system_prompt()


def test_large_file_download_guidance_present() -> None:
    """提示词必须有「下载大附件」这一节。"""
    p = _prompt()
    assert "下载大附件" in p, "系统提示词缺少『下载大附件』一节（v1.60 回归）"


def test_guidance_teaches_resumable_download() -> None:
    """必须真的教 `curl -C -` 断点续传，而不是泛泛说『注意超时』。"""
    p = _prompt()
    assert "curl -C -" in p, "缺少 `curl -C -` 断点续传指引 —— 超时后无法续传会从 0 重下"
    assert "断点续传" in p


def test_guidance_points_download_to_persist_dir() -> None:
    """大文件下载必须落到 `/work/persist/`（容器回收不丢），而非 /work/t。"""
    p = _prompt()
    # 段落里应出现「下到持久目录」且示例路径在 /work/persist 下
    assert "/work/persist/dl" in p, "下载示例应落到 /work/persist/ 下（回收不丢）"
    assert "持久" in p


def test_guidance_mentions_single_command_time_cap() -> None:
    """必须点明单条命令 300 秒上限 —— 这是『一条下不完』的根因。"""
    p = _prompt()
    assert "300" in p
    # `-m 280` 的用意：在 300s 上限前让 curl 自己先干净退出，文件可续
    assert "-m 280" in p
