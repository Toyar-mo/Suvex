# driver_for 接线评估（羊城杯 P1）

> 结论先行：**赛前不建议把 `/ctf2` 看板那条老路（`service.sync` / `runner`）切到
> `PlatformDriver`。** 这不是偷懒，是风险/收益不划算——下面给出证据。对话页
> （比赛当天的主力路径）已经走 `competition.py → SlabMatchDriver`，不受此影响。

## 1. 问题是什么

评估报告 G1 指出：`app/kernel/platforms.py:466 driver_for()` 自称"下一轮接线的
入口…现在先放着"，全库只有测试调用它。也就是说：

- **配置驱动平台**（用户在设置页填 base_url + accessKey → `GenericDriver`）的
  能力写好了，但 **`/ctf2` 看板那条路没用上它**；
- `/ctf2` 那条路（`service.sync` 拉题、`runner` 解题、`service` 提交）一律直连
  内置的 `KernelClient`（`service.py:310 build_client_sync()` 是唯一的构造点，
  共 7 处 `async with KernelClient(token=token)` 调用）。

## 2. 两套 API 长什么样（证据）

`KernelClient`（`app/kernel/client.py`，18 个 async 方法，复合键模型）：

```
list_competitions()                     list_stages(competition_id)
competition_status(competition_id)      list_stage_challenges(stage_id)
list_stage_submissions(stage_id)        list_practice()
get_practice_challenge(practice_id, challenge_id, ...)
submit_flag(practice_id, challenge_id, flag, ...)
start_environment(practice_id, challenge_id, ...)
stop_environment(practice_id=..., challenge_id=...)
start_environment_until_ready(...)      ...
```

`PlatformDriver`（`app/kernel/platform.py:227-325`，单一不透明 handle 模型）：

```
fetch_challenges(*, known_ids: set[str]) -> list[RemoteChallenge]
submit(handle: dict, answer: str) -> SubmitOutcome
fetch_challenge(handle) / read_environment(handle) / start_environment(handle)
stop_environment(handle) / environment_prompt(env) / answer_max_length() / supports_environment()
```

差异是**结构性的**，不是改个名字能对齐的：

| 维度 | KernelClient | PlatformDriver |
|---|---|---|
| 寻址 | 复合键 `(practice_id, challenge_id)` 或 `(competition_id→stage→challenge)` 三级 | 单一 `handle: dict`（不透明，驱动自己解释） |
| 题目层级 | 有 competition / stage / practice 多层 | 扁平 `list[RemoteChallenge]` |
| 提交返回 | `dict`（raw 字段各异） | `SubmitOutcome`（`accepted` 与 `is_solved` **分离**） |
| 环境 | `start_environment(practice_id, challenge_id)` | `start_environment(handle)` |
| 写题解 / 排行 / 公告 | `list_writeups()` / `list_daily()` | 协议里**没有**这些概念 |

## 3. 要接线得改什么（工作量与风险）

要让 `service.sync` / `runner` / 提交链路吃 `PlatformDriver`，只有两条路，**都很大**：

- **路线甲：写一个 adapter**，让 `KernelClient` 的方法表面背后转发到
  `PlatformDriver`。但要为每个方法维护"复合键 → handle"的映射，还要把
  `SubmitOutcome` 翻译回 KernelClient 期望的 dict 形状，并处理
  `list_writeups`/`list_daily`/stage 层级这些 PlatformDriver **根本没有**的概念
  （只能返回空或抛未实现）。这等于在两个语义模型中间造一层永远对不齐的桥。
- **路线乙：改所有调用点**，把 7 处 `KernelClient(...)` + `service.py`(4291 行)
  + `runner.py`(8911 行) 里对复合键的假设改成 handle 模型。这是**核心解题链的
  大重构**，`runner.py` 深度耦合 `KernelChallenge`/`competition.account_id` 的
  归属模型（提交 flag 必须用"当初读到这道题的那个号"，见 `bind_account` 注释）。

无论哪条，都触碰 `/ctf2` 看板的同步、解题、提交、环境、题解、排行全链路，回归面
覆盖 `tests/kernel/**` 几十个用例。**距测试赛（11-03 14:00）不足一天、决赛（11-07）
五天**，此时大动核心链路，赌的是"接完不出新 bug"，而赢的东西是"次要 UI 也能用
配置驱动平台"——**用户主诉明确是对话页**，这个赢面用不上。

## 4. 反过来看：真正需要的能力已经通了

对话页（比赛主力）已经绕过 KernelClient，直接复用 `PlatformDriver`：

- `app/chat/competition.py:resolve_driver()` → 优先 `driver_for(db, account)`，
  取不到再直接造 `SlabMatchDriver(base_url, token)`；
- 两个新工具 `fetch_competition_challenges` / `submit_flag` 都走这条 async 驱动；
- v1.46 线上实测：链路真的打到了 `ycb-test.dasctf.com` 并收到平台真实业务回执
  （`无权操作` → 翻成可行动的 accesskey 提示），说明"取题 / 交 flag"这条路
  **端到端是通的**，只差比赛账号的 accesskey 授权。

也就是说，**比赛当天要用的能力不依赖 P1 接线**。

## 5. 建议（什么时候做、怎么做）

- **现在（赛前）**：不动 `service.sync` / `runner`。只把文档改对（G7，已改
  `drivers/__init__.py` 里"在 registry.py 注册"那句错误指引），并在
  `driver_for` 的 docstring 里写清"目前只有 chat competition 用"，避免下一个人
  再去猜。
- **赛后（不赶时间时）**：如果确实想要"/ctf2 看板也能接任意配置平台"，**不要写
  adapter**，而是把 `runner`/`service` 的题目身份模型从"复合键"重构成"handle +
  归属账号"（路线乙的正确版本），并补一条把 `/ctf2` 全链路跑在 `GenericDriver`
  上的集成测试。这是版本级重构，配一条自己的 changelog 与灰度。

## 6. 判定

| 项 | 结论 |
|---|---|
| 赛前接线是否必要 | **否**（对话页主力路径已绕过） |
| 风险 | 高（核心解题链大重构，回归面大） |
| 收益 | 低（只惠及次要的 `/ctf2` 看板） |
| 决定 | **赛前只做评估 + 文档修正，不接线** |
| 遗留 | 赛后按第 5 节路线乙重构（另立版本） |
