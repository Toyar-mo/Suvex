"""平台驱动。

每个平台一个模块，实现 `app.kernel.platform.PlatformDriver` 协议。

* `ctf2.py` —— CTF2（`ctf2.dasctf.com`）。**它在解题目录下的位置有些
  尴尬**：`app/kernel/client.py` 才是它真正的实现，那个文件比这个包还早。
  留在这里的是一条薄适配（把 `KernelClient` 的方法翻译成契约里的动作），
  不是重新实现。
* `slabmatch.py` —— 比赛平台（`slab-match/api/v1/agent`，文档 `api_doc.md`）。

## ★ 加一个新平台要做什么

1. 照 `slabmatch.py` 写一个类：填 `slug` / `display_name`，
   实现 `fetch_challenges` 与 `submit`，其余方法按需覆盖。
2. 在 `registry.py` 的 `DRIVERS` 里注册。
3. 数据库里那个账号行的 `platform` 列填新的 slug。

**不需要**动 `service.py` / `runner.py` / `scheduler.py` —— 那正是这层
存在的意义。如果发现"为了接一个平台必须改上层"，那说明这里漏了一个
抽象，该补的是契约而不是在 service 里加 if。
"""
