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
2. 让 `app.kernel.platforms.driver_for(db, account)` 能取到它：
   **纯配置驱动**的平台（用户在设置页填 base_url + accessKey）不需要写
   新类，`driver_for` 会自动造一个 `GenericDriver(spec, key)`；
   只有协议长得差太多、`GenericDriver` 描述不了时，才需要在这里加一个
   手写驱动，并在 `driver_for` 里按 `platform.slug` 分派。
3. 数据库里那个账号行的 `platform_id` 指向平台配置（或 `NULL` = 内置 CTF2）。

> 历史注记（v1.47 修正）：早先这里写着"在 `registry.py` 的 `DRIVERS` 里注册"，
> 但 `registry.py` 从未存在过——真正的驱动选择入口是
> `app.kernel.platforms.driver_for()`。这段文档已按实际代码改正。

**不需要**动 `service.py` / `runner.py` / `scheduler.py` —— 那正是这层
存在的意义。如果发现"为了接一个平台必须改上层"，那说明这里漏了一个
抽象，该补的是契约而不是在 service 里加 if。

> ⚠️ 现状（v1.47）：`driver_for()` 目前只有 **chat 的 `competition.py`**
> 在调用（对话页的比赛工具走这条），**`/ctf2` 看板那条老路（`service.sync` /
> `runner`）仍然直连 `KernelClient`**，还没切到 `PlatformDriver`。原因与评估
> 见 `docs/yangcheng-cup-driver-wiring-assessment-20261102.md`：两套 API
> （`KernelClient` 的复合键 vs `PlatformDriver` 的单一 handle）结构不同，
> 赛前切换风险大于收益。所以上面那句"不需要动 service/runner"描述的是
> 这层抽象**终将**达到的效果，不是今天的接线状态。
"""
