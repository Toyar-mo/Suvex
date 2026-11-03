# CTF 页：题目按「接入来源」筛选（v1.56）

2026-11-03

## 背景

用户连着问了两个问题：

- 「接入之后题目在哪」—— 平台接入页显示 `CTF2（内置）` 和 `DASCTF Pro（slab-match）` 都「可用」，
  但题目列表里完全看不出某道题是从哪个接入同步来的。
- 「还有我一开始说的」—— 指最早那句「对每个接入做分类」。

v1.55 已经做了「标准分类」（把库里脏 `category` 原文在展示层归一到 web/crypto/reverse/pwn/misc/ir/other，
并加分类筛选行）。这一版补上**第三个维度：接入来源**。

## 改了什么

只动前端一个文件：`suvex-web/src/pages/Kernel.tsx`。

1. **`sourceFilter` state** 与 `SourceFilter = string`（存 `platformSlug`，`'all'` 为不限）。
2. **`sourceOptions` 从真实题目现算**：遍历 `challenges`，用 `Map` 按 `platformSlug` 去重，
   label 取 `platformName`。不写死任何平台名 —— 以后新识别一个平台，它同步下来的题一进来，
   筛选行就自动多一格。
3. **筛选行 `[role="group"][aria-label="按接入来源筛选"]`**：`全部来源` + 每个真实来源，
   计数按 `challengesInStatusBucket`（当前状态桶）算，与分类行同一口径。
4. **与状态/分类都是「与」关系**：`filtered` 里先过来源、再过分类、再匹配搜索文本。
5. **页脚**：`filter === 'all' && categoryFilter === 'all' && sourceFilter === 'all'` 才显示「共 N 道题」，
   任一收窄都显示「筛选出 M 道，共 N 道」。
6. **搜索也搜来源与标准类名**：按「逆向」「DASCTF」搜都能命中。

### 显示门槛的一个修正（值得记）

第一版写的是 `sourceOptions.length > 1`（多于一个接入才显示这一行），理由是"只有一个来源时
每个按钮都等于全部，是噪音"。**线上实测暴露了问题**：当前 423 道题全部来自 `DASCTF Pro（slab-match）`
一个来源，于是整行被隐藏 —— 而这恰恰是用户问「题目在哪」的原因：他要看到"这些题是从哪来的"。
改成 `sourceOptions.length > 0`（只要有题带来源就显示，哪怕只有一个），这一行本身就在回答那个问题。
只有全都没有来源信息时才隐藏（此时确实纯噪音）。

## 验证（线上真实数据，423 道题）

Playwright 实测 `http://192.168.44.128:8080/kernel`：

| 操作 | 结果 |
|---|---|
| 初始 | 三个筛选组都在：按状态筛选 / 按分类筛选 / 按接入来源筛选 |
| 点「已解出」 | 状态行 40；分类行 Web17/密码学7/逆向9/Pwn1/杂项6（合计 40）；来源行「全部来源 40 / DASCTF Pro（slab-match） 40」；列表 40 篇 |
| 再点「逆向」 | 列表 9 篇全为「逆向」；页脚「筛选出 9 道，共 423 道」 |
| 卡片 | 副标题显示「逆向 · DASCTF Pro（slab-match） · 1 分 · 逆向专用」 |

截图：`artifacts/kernel-source-filter-v156.png`（已 read_image 复核）。

控制台 5 条 403 = `settings/{tokens,sandbox,notices/channels,notices/rule}`、`kernel/settings` ——
只读账号访问管理员接口的**既有**现象，与本页无关。

## 部署

后端本版无改动（v1.55 的 router.py/schemas.py 已在线上）。前端：

```
npm run build                          # → index-BC551C11.js
tar czf /tmp/dist-v156b.tgz -C dist .
scp /tmp/dist-v156b.tgz kali@192.168.44.128:/tmp/
# Kali 上（sudo）：
rm -rf /tmp/dist-new && mkdir /tmp/dist-new && tar xzf /tmp/dist-v156b.tgz -C /tmp/dist-new
find /root/suvex/suvex-web/dist -mindepth 1 -delete
cp -a /tmp/dist-new/. /root/suvex/suvex-web/dist/
chown -R kali:kali /root/suvex/suvex-web/dist
```

`find -mindepth 1 -delete` + `cp -a` 而不是 `rm -rf dist` —— 后者会拆掉 nginx 的 bind mount
（`/opt/suvex/suvex-web/dist` ← `/root/suvex/suvex-web/dist`，已在 `/etc/fstab`）。
部署后 `findmnt /opt/suvex/suvex-web/dist` 确认挂载在，`grep index-…js .../dist/index.html` 确认版本。

## 限制

- 来源筛选的选项是**从当前已同步的题**里算的，没题的平台不会出现在筛选行（合理：筛了也没结果）。
- `platformSlug` 由后端 `_resolve_platform_info` 从 `challenge.competition_id → account.platform_id → platform.slug`
  链路推导；内置 CTF2 的题落回 `ctf2` / 「CTF2（内置）」。
