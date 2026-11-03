# 来源标注修正 + 比赛平台同步（v1.57）

2026-11-03

## 一、用户发现的真 bug：407 道老题被冒充成比赛平台

用户问「https://pro.dasctf.com/slab-match/api/v1/agent 不是只有 5 道题吗？」——
一查，线上比赛平台确实只有 **5 道题**（id 12452–12456：WEB/PWN/MISC/REVERSE/CRYPTO），
而 CTF 页那 423 道题全是老的 CTF2 题库（BUUCTF 407 / N1BOOK 8 / IR-Lab 4 / …）。

**根因**：v1.55/v1.56 的 `platformSlug`/`platformName` 是按
`challenge → competition → account → platform` 链路推的。而账号的 `platform_id`
指向了比赛平台，于是**所有老题被贴上「DASCTF Pro（slab-match）」的标签** ——
它们根本不在那个平台上。那一排「按接入来源筛选」在说谎。

**修法（`app/kernel/router.py`）**：来源改按**题目真实出处**，即 `competition`
这一级：
- 显示名用 `KernelCompetition.name`（「BUUCTF」「N1BOOK」…）；
- slug 用 `src-<competition_id>`（稳定、不随平台改名漂移，且与真正的
  platform slug 区分开）；
- `kind='local'`（本地复盘案例）显示「本地案例」。

线上实测（修复后）：
```
407  slug=src-ctf2comp_01M3PG846CV457JAFVBRPCG29M  name='BUUCTF'
  8  slug=src-ctf2comp_01M3PG846AKCC7DF8M9ZS7ZXYA  name='N1BOOK'
  4  slug=src-ctf2comp_01M5CZ9AWK4VQGP934JS373CC5  name='IR-Lab'
  3  slug=src-ctf2comp_01M3PG8467K2M0F5NPWRR871ZA  name='BUUCTF-Basic'
  1  slug=src-ctf2comp_01M3PG84635HP6NMA0D8X6VFVJ  name='BUUCTF-Real'
```

## 二、新增「同步比赛题」（把那 5 道真·比赛题拉进页面）

原来 `kernel.service.sync()` 只调 `KernelClient`（CTF2 自己的接口），比赛平台的
题从来没进过库。新增：

- `app/kernel/service.py::sync_competition(db)`：用比赛平台驱动
  （`resolve_driver`）`fetch_challenges` + 逐题 `fetch_challenge`，落进一个
  `remote_id='slab-match'` 的 `KernelCompetition`（名字默认「羊城杯 AI Agent
  解题赛」）。幂等（重复同步走更新）、单题失败记 `errors` 继续走、
  **不删题**、**不动解题状态**。
- `app/kernel/router.py::POST /kernel/competition/sync`（scan 权限，与
  `/ctf2/sync` 同级理由：改数据 + 一串外部请求）。
- 前端 `suvex-web/src/data/kernel.ts::syncCompetition()` +
  `pages/Kernel.tsx` 工具条加「同步比赛题」按钮。

## 三、验证与一个阻断

**代码侧全绿**：
- `tests/kernel/test_competition_sync.py`（4 条，假驱动端到端：落库/幂等/来源正确/无账号 409）
- `tests/kernel/test_challenge_category_source.py`（29 passed，含"老题不许冒名比赛平台"的回归）
- 两文件合计 **33 passed, 1 skipped**；`tests/kernel` 全量 1871 passed。
- 前端 23 vitest passed、tsc/eslint 干净、build 成功（`index-BVTzwyQr.js`）。
- 部署 Kali：`service.py` sha `9899be86…`、`router.py` sha `30e93efe…`（与本地相等），
  `Application startup complete`，health 200；前端 dist 已同步、bind mount 完好。

**阻断（需要用户处理，不是代码问题）**：线上跑真实同步时，比赛平台返回
`{"code":"40403","message":"无权操作"}`（HTTP 200）——
**那个 AccessKey 现在没有 agent 接口的权限了**（`ak_****XyhT`）。
一小时前同一个 key 还能列出 5 道题，中途权限被收回/切换。
原始复现：
```
GET https://pro.dasctf.com/slab-match/api/v1/agent/ctf/exercise-list
X-Agent-AccessKey: ak_****XyhT
→ HTTP 200 {"code":"40403","message":"无权操作","data":{"redirectUrl":null}}
```
所以「同步比赛题」这条功能的**代码链路是通的**（无平台时给 409 可行动提示、
有平台时打真接口、把 `无权操作` 翻成「accesskey 没有 agent 接口权限」），
只差平台重新给这个 key 开权限（或在「设置 → 解题 / 比赛平台」换成有权限的 key）。
