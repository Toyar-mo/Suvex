# 大附件下载被 300s 上限 / 沙箱回收打断：提示词指引（v1.60）

日期：2026-11-03

## 现象（用户原话）
「我的问题是阻碍仅为 1.9GB 附件下载被沙箱回收打断三次」

CTF 会话 `chat_01M6GAVGDD7JTENZ2ZANK7CT4W` 里，MISC 题附件 `misc.zip` 有
1.9GB，反复下不完，被「下了一半超时 / 沙箱回收重来」打断多次。

## 根因（真库 + 代码定位）
1. **单条 `run_shell` 最多跑 300 秒**：`app/kernel/runner.py::_tool_run_shell`
   会把 `timeout_seconds` 夹到 `DEFAULT_SHELL_TIMEOUT=300`
   （`timeout = max(5, min(int(args.get("timeout_seconds") or DEFAULT_SHELL_TIMEOUT), DEFAULT_SHELL_TIMEOUT))`）。
   1.9GB 在这个链路上一条命令下不完。
2. **模型自己的命令还叠了 `curl -m 60`**（每条文件 60s 超时）——
   DB seq 10 实证：`misc.zip` 只到 482656256 字节（482MB）就 `misc rc=28`（curl 超时）；
   同批 web.zip / pwn.zip 小，`rc=0` 成了。
3. **没用 `curl -C -` 续传**：每次重下都从 0 开始，永远跨不过「一条命令能下多少」。
4. **写到 `/work/t`（容器可写层）而不是 `/work/persist/`**：`IDLE_REAP_SECONDS=30min`
   空闲回收后 `/work/t` 全丢（DB seq 684/687/734/856/859/877 实证「沙箱被回收，
   `/work/t` 里的东西全没了」；模型后来自己也改用 `/work/persist/pwn` 才止住）。

## 修法（v1.60，仅提示词层）
`app/chat/prompts.py` 在「给用户下载的产物：放 `/work/persist/`」之后，新增
一节 **「下载大附件（几十 MB ~ 数 GB）：必须断点续传 + 直接落到 `/work/persist/`」**，
教模型：
- 单命令 300s 上限 → 一条下不完的大文件别指望一次 `curl -o`；
- **断点续传** `curl -C -`，并用 `-m 280`（在 300s 前让 curl 干净退出、文件可续），
  循环重发直到下完；
- **下到 `/work/persist/dl/`**（容器回收也不丢、回收后还能续）；
- 下完用 `unzip -l` / `file` 验证真的完整再解压（半截 zip 会 "unexpected end of file"）；
- 鉴权 header 每次带全；
- **服务端不支持续传时**（无 `Accept-Ranges: bytes`）退回 `wget -c`，再不行就如实
  告知用户「无法续传、需更长时限」，绝不假装下完。

> 为什么不直接调大 `DEFAULT_SHELL_TIMEOUT`：300s 上限是有意的取舍 ——
> 一条卡死的命令不该拖垮整道题的预算。真正的缺口是「模型不知道怎么下
> 超过一条命令时限的大文件」，所以补的是行为指引，不是放宽上限。

## 验证
- 新增 `tests/chat/test_download_guidance.py`（4 条）：断言提示词含「下载大附件」
  一节、`curl -C -` 续传、`/work/persist/dl` 落点、300 上限与 `-m 280`。**4 passed**。
- 提示词相关回归：`tests/test_chat.py` + `tests/test_chat_final_marker.py` +
  `tests/chat/test_persona_expand.py` + 新测 = **71 passed**，无回归。
- `ruff check` 两文件 All checks passed。
- 真实渲染：`build_system_prompt()` 全文含该节全部要点（含 curl/wget 代码块）。

## 诚实边界
这是**行为层**修复（教模型下次怎么做），不是把 300s 上限改掉，也不是给沙箱加
自动续传的底层机制。它依赖模型读并遵循提示词 —— 与业内（CyberStrikeAI 类）
靠系统提示词约束 agent 行为是同一思路。若要更硬的保证，下一步可选：在
`run_shell` 返回里对「疑似未下完的大文件」给出显式告警，或提供专门的
`download` 工具内置续传。当前先交付提示词层修复 + 回归测试。
