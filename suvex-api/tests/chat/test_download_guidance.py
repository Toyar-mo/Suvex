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


def test_windows_only_tool_fallback_guidance_present() -> None:
    """提示词必须有「Windows 专用工具？找 Linux 等价方案，别一句环境不支持就放弃」。

    线上真事（2026-11-04，用户 m01118）：模型面对路由器备份配置，回
    「需要 RouterPassView 才能解析，在 Linux 沙箱里无法解密，我主动放弃。」
    —— 而正确答案是 binwalk 拆包 + 按格式写 Python 解析器。这条守门员钉住
    那段反放弃指引，防止它被删后模型又回到"跑不了那个 GUI 就放弃"。
    """
    p = _prompt()
    assert "RouterPassView" in p, "缺少 Windows 工具反放弃的具体反例（RouterPassView）"
    assert "找 Linux 的等价方案" in p or "Linux 的等价" in p, "缺少『找 Linux 等价方案』指引"
    assert "别一句\"环境不支持\"就放弃" in p or "别" in p  # 软断言
    # 必须给可执行路径，而不是"放弃"：至少提到自写解析器 / binwalk
    assert ("自己写解析器" in p) or ("写解析器" in p), "缺少『自己写解析器』这条替代路径"


def test_userspace_emulation_guidance_present() -> None:
    """提示词必须教「需要动态分析 ≠ 必须起模拟器」——给 userspace 模拟的替代路。

    线上真事（2026-11-06，CTF 会话 chat_01M6P1TAYTASS6P64P85PED8MB）：Android
    原生 `.so` 逆向，模型把「动态分析」窄化成「起 Android 模拟器」，发现没有
    `/dev/kvm` 就反复换算法穷尽、烧了 12 小时。正确认知是：userspace 指令级模拟
    （unicorn / qemu-user / unidbg）不需要 KVM，沙箱里就能跑。这条守门员钉住
    这段指引，防止它被删后模型又回到「没 KVM = 做不了动态分析」。
    """
    p = _prompt()
    assert "userspace" in p.lower() or "用户态" in p, "缺少 userspace 模拟的指引"
    assert "unicorn" in p.lower(), "缺少 unicorn（userspace CPU 模拟）指引"
    assert "unidbg" in p.lower() or "qemu-aarch64" in p.lower() or "qemu-user" in p.lower(), (
        "缺少 unidbg / qemu-user 的指引"
    )
    # 必须点破核心误区：没有 KVM 不等于做不了动态分析
    assert "KVM" in p, "缺少对 KVM（硬件虚拟化）缺失的澄清"
