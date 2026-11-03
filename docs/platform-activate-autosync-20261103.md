# 启用新平台即刻拉题 + 同步用平台真名（v1.58）

2026-11-03

## 用户要的效果

> 我要的效果是这里新增加平台 apikey，ctf 页面就要立刻同步拉题

即：在「设置 → 平台接入 → 识别一个新平台」走完、点「启用」之后，
**不需要再手动点「同步比赛题」**，CTF 页就应该已经有这个平台的题。

## 原来为什么没有

1. `POST /kernel/platforms/{id}/activate` 只做「验证凭据 → 写账号 → 指向平台」
   三件事，**完全不碰题目**。题要靠用户到 CTF 页手动点「同步比赛题」
   （v1.57 才加的那个按钮）。所以"接入了却没题"是真实现象。
2. `sync_competition()` 的批次名**写死**默认「羊城杯 AI Agent 解题赛」，
   与"接入的是哪个平台"无关 —— 用户接入的不是羊城杯时，来源名也是错的。

## 改了什么

### 1. 启用即触发后台同步（`app/kernel/router.py`）

`activate_platform` 端点，在审计记录之后、返回之前，加：

```python
_kick_off_competition_sync(row.display_name)
```

`_kick_off_competition_sync` 起一个 **daemon 线程**：
- 线程体自己 `get_session_factory()()` 开会话 —— **不能复用请求的 db**，
  它在响应返回后就被关了（跨线程用它会炸）。
- 调 `service.sync_competition(db, competition_name=display_name)` 后 commit。
- **任何异常只记日志、不上抛** —— 启用（凭据已验证入库）已经成功，
  同步是附加便利；同步失败不该让「启用」报错。用户下次点
  「同步比赛题」还能重试。

⚠️ 为什么用后台线程而不是同步跑：同步是 O(题数) 次外部请求（逐题取详情），
在请求里跑会让「启用」按钮转圈几十秒。启用本身已经成功，没理由让用户等。

### 2. 同步的批次名取平台真名（`app/kernel/service.py`）

```python
batch_name = competition_name
if account.platform_id:
    platform = platforms.get_platform(db, account.platform_id)
    if platform is not None and (platform.display_name or "").strip():
        batch_name = platform.display_name.strip()
```

于是 CTF 页的来源名 = 用户接入的那个平台的名字（如「DASCTF Pro（slab-match）」
或将来接入的别的平台），而不是写死的「羊城杯」。来源 slug 仍是
`src-<competition_id>`（v1.57 改的 competition 级来源，未受影响）。

> 为什么不从驱动对象取 display_name：`resolve_driver` 在 `platform_id` 指空时
> 会造一个**没有** `display_name` 的内置驱动，只有配置驱动才有。所以读平台行
> 更稳。

### 3. 前端启用后刷新题目列表（`suvex-web/src/components/settings/PlatformPanel.tsx`）

`PlatformDialog` 的 `onDone` 里，除了原来的 `refresh()`（重读平台列表），
加一次 `loadKernel({ silent: true })` —— 用户点「启用」关掉抽屉后，
再切到 CTF 页看到的就是新平台的数据，而不是旧列表。

## 验证

- `suvex-api/tests/kernel/test_competition_sync.py` 现 **5 条**：
  - 原来 4 条（落库 / 幂等 / 来源正确 / 无账号 409）
  - 新增 `test_activating_a_platform_kicks_off_a_sync`：启用端点确实触发了
    后台同步，且用的是**这个平台自己的名字**（`DASCTF Pro（slab-match）`），
    不是写死的默认名。
- 相关 kernel 测试（competition/category/platform）：**164 passed, 1 skipped**。
- 前端：`tsc -b` 干净、`eslint` 干净、`kernel.test.ts` 23 passed、
  `settings` 20 passed、`npm run build` 成功（`index-D1xz458X.js`）。
- 部署 Kali：`service.py` sha `e4d2f1c6…`、`router.py` sha `de51ff58…`（与本地
  相等），`Application startup complete`，health 200；dist 已同步、bind mount 完好。

## 如实说明：平台侧 accesskey 仍无权限

真实跑一次同步，平台仍回 `{"code":"40403","message":"无权操作"}` —— 这个
AccessKey（`ak_****XyhT`）在平台侧**没有 agent 接口的权限**（v1.57 就发现了，
一小时内从"能列 5 题"变成"无权操作"）。

所以「启用即拉题」这条链路**代码是通的**（测试证明接线正确；真跑能打到平台、
把它的错误如实反映出来），但**能不能真拉到题，取决于这个 key 是否被平台
授权**。要真正看到题：到平台重新给这个 key 开 agent 权限，或在「平台接入」
里换一个已授权的 key 再启用一次 —— 一旦 key 有效，点「启用」后 CTF 页就会
自动出现题目，无需再点「同步比赛题」。
