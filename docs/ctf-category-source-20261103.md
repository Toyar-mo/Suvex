# CTF 页：标准分类 + 接入来源（v1.55）

## 需求

用户（m06106）：**「将这个页面完善好，对每个接入做分类」**——CTF 页上每道题
除了原来那个平台原话的分类，还要能一眼看出**标准类**（web/密码学/逆向…）与
**来自哪个接入**（内置 CTF2 / DASCTF Pro …），并能按分类筛选。

## 问题：平台给的 `category` 脏得不能直接用

线上 423 道题的 `category` 原始分布：

```
misc 119 / web 110 / crypto 94 / reverse 67 / pwn 17
第01题 web入门 6 / ir-root-cause 4 / basic 3 / 第02题 web入门 2 / real 1
```

问题有三：
- **大小写混**（`REVERSE` vs `reverse`）；
- **混了题号/中文**（`第01题 web入门`）；
- **非标准值**（`ir-root-cause` / `basic` / `real`）。

直接拿来展示是一地鸡毛，直接拿来当筛选键会变成"每个脏值一个按钮"。

## 关键约束：**绝不改写数据库里的 `category` 原文**

`category` 决定了 `sandbox_profile` 同步时选哪个镜像。改原文会让**同一道题
昨天用 re 镜像、今天用 base 镜像**（不可复现），而且**不会报错**，只在做题时
表现为"环境不对"。所以：

- 库里的 `category` 一个字不动 —— 它继续决定镜像；
- 另给一个**只读投影** `canonicalCategory`（七类之一），只用于展示与筛选。

## 后端改动

### `app/kernel/schemas.py`

- `KernelChallengeOut` 加三个字段：
  - `canonicalCategory: str` —— 归一后的标准类（`web`/`crypto`/`reverse`/`pwn`/`misc`/`ir`/`other`）；
  - `platformSlug: str` —— 来源接入的 slug（内置 CTF2 也是 `ctf2`）；
  - `platformName: str` —— 来源展示名（「CTF2（内置）」「DASCTF Pro（slab-match）」…）。
- 新增 `_CANONICAL_CATEGORY_KEYWORDS`（pwn/reverse/crypto/web/ir/misc 的关键词表，
  含中文）、`CANONICAL_CATEGORIES = ("web","crypto","reverse","pwn","misc","ir","other")`、
  `canonical_category(raw) -> str`：`lower()` → 空则 `other` → 子串命中 → 否则 `other`
  （**认不出就落 `other`，绝不猜成某个具体类**）。

### `app/kernel/router.py`

- `_resolve_platform_info(db, challenges)` —— 一次**三次 `IN` 查询**建好
  `challenge → competition → account → platform` 的索引，返回 `{challenge_id: (slug, name)}`。
  **列表页几十上百道题共用一份，避免 N+1**（逐题懒加载就是每题 3 次查询）。
- `_platform_label(slug, display_name, driver)` —— `driver=='ctf2'`（内置驱动）一律
  显示「CTF2（内置）」，其余取平台 `displayName`。
- `_challenge_out(challenge, environment, platform_info=None)` 填三新字段；
  新增 `_challenge_out_one(db, challenge, environment)` 给单题响应（详情/解题/提交/拉取）
  用（自己查一次索引）。`_challenges_with_environments` 一次查好索引批量传。

### 测试 `tests/kernel/test_challenge_category_source.py`（29 passed）

- `canonical_category` 参数化映射（标准值/大小写/脏值/中文/兜底）。
- 接口投影到位 + `category` 原文一字不动（拉两次读接口后回看库）。
- 内置来源 fallback、配置平台来源。
- **列表不 N+1**：把 `_resolve_platform_info` 包计数器，断言多题列表只调 1 次。

## 前端改动

### `src/data/kernel.ts`

- `KernelChallenge` 加 `canonicalCategory` / `platformSlug` / `platformName`。
- `CANONICAL_CATEGORIES`（七类固定顺序）、`CanonicalCategory` 类型、
  `CANONICAL_CATEGORY_LABEL`（中文标签）、`canonicalCategoryOf(value)`（**前端只兜底，
  不复制关键词表** —— 真值在服务端，重复实现迟早分叉）。

### `src/components/kernel/ChallengeCard.tsx`

- 卡片原来显示 `challenge.category || '未分类'`，改为显示
  **中文标准类** + **来源徽标**（`DASCTF Pro（slab-match）`，`title` 提示"来源：…"）。
  来源为空时不画徽标（老缓存/本地题没有这字段，空徽标比不画更困惑）。

### `src/pages/Kernel.tsx`

- 状态筛选下面加**分类筛选行**（`CATEGORY_FILTERS`，七类 + 全部类），
  与状态是**「与」关系**（先按状态收窄，再在桶里按分类收窄）。
- **分类 chip 的计数按「当前状态桶」算**：在「已解出」下点分类，看到的计数
  就是"已解出的逆向 9 道"，与列表一致（用全量算会出现"标签写 67、列表只 4 条"）。
- 搜索框也匹配标准中文类名与来源（按「逆向」或「DASCTF」都要搜得到）。
- **页脚计数修正**：原来只看状态 `filter`，只按分类筛时会显示"共 423 道"（实际只
  显示 67）。改为**任一维度收窄都报「筛选出 N 道，共 423 道」**。

### 测试

- `src/data/kernel.test.ts` +4 条（canonicalCategoryOf 七类原样/大小写变体/未知落 other/中文标签全覆盖）。
- `src/components/kernel/ChallengeCard.test.tsx` 的 `makeChallenge` 补三新字段默认值。

## 部署与线上验证（Kali `/root/suvex`）

- 后端：`router.py` + `schemas.py` 拷到 prod，sha 与本地一致
  （router `5c412acd…`、schemas `12c92fe1…`），重启 → `Application startup complete`、
  `GET /api/v1/health`=200。
- 前端：本机构建 → 同步到 `/root/suvex/suvex-web/dist`（`find -mindepth 1 -delete` +
  `cp -a`，**不删 dist 目录**保护 bind mount，`findmnt` 确认挂载完好）；index 引用新 chunk。
- **线上聚合**（423 道题）：`canonicalCategory = reverse:67 / web:118 / pwn:17 /
  crypto:94 / misc:123 / ir:4`（合计 423，`other:0` —— 每个脏值都归到了具体类）；
  `platformSlug=pro-dasctf-com` / `platformName=DASCTF Pro（slab-match）`（全部）。
  样例：`REVERSE → reverse`，来源 `DASCTF Pro（slab-match）`，原文 `REVERSE` 保留。
- **视觉复核**（Playwright，`artifacts/kernel-category-v155.png`）：
  - 两排筛选：状态（全部 423 / 未开始 376 / 已解出 40 …）+ 分类（全部类 423 / Web 118 /
    密码学 94 / 逆向 67 / Pwn 17 / 杂项 123 / 应急响应 4 / 其它 0）；
  - 卡片：`逆向 · DASCTF Pro（slab-match） · 1 分 · 逆向专用`；
  - 组合筛选：状态=已解出 → 分类 chip 计数收窄为 全部类 40 / Web 17 / 密码学 7 /
    逆向 9 / Pwn 1 / 杂项 6 / 应急响应 0 / 其它 0；再点逆向 → 「筛选出 9 道，共 423 道」，
    渲染 9 张卡（与 chip 计数一致）。

## 边界与未做

- 分类归一**只发生在展示层**；数据库 `category` 不改，`sandbox_profile` 选择不受影响。
- 前端不复制关键词表，只兜底；老缓存/本地题缺字段 → `canonicalCategoryOf` 落 `other`。
- 「来源」目前只有两种真实取值：内置 `ctf2` 与配置平台（线上是 `pro-dasctf-com`）。
  多接入时同一页会自然按 `platformName` 区分，无需另加列。
