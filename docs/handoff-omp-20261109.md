# Suvex 交接文档（给 OMP）· 2026-11-09

> 这份文档是**一次完整会话的交接**：把这段时间做过的事、做过的决定、验证到哪一步、
> 哪些东西不能碰，都写清楚。读完之后应当能直接接着开发，不需要再问"之前为什么这么做"。
>
> - 仓库：`https://github.com/Toyar-mo/Suvex.git`（私有）
> - 分支：`main`
> - 交接时的**代码**提交：`eee19de3 feat(overview): 实时活动改成 canvas 演出舞台`
> - 交接文档本身在其后的 `docs(handoff)` 提交里（仅文档，不改代码）
> - 远端 `origin/main` 与本地一致（已 push）
> - 真机：`http://192.168.44.128:8080`（Kali）
> - 前端测试：**1257 个全部通过**（交接时）
> - 最后部署：由提交 `eee19de3` 重新构建并部署，入口 JS 为 `index-DKjHscDy.js`

---

## 0. 先读这几条硬规矩

这些不是建议，是项目的硬约束，违反会直接造成事故。

### 0.1 提交身份与署名（`AGENTS.md`）

- 提交者**只能**是仓库配置的身份：`Suvex Dev <dev@suvex.local>`。
- 不改 `user.name` / `user.email`，不用 `--author` 换成别人。
- **禁止任何形式的共同作者署名**：提交信息、PR 描述、tag 注释里不得出现
  `Co-Authored-By:`（包括 Claude / Anthropic / 其他模型或工具）。
- **禁止 AI/工具署名行**：不得写 "Generated with Claude Code"、"🤖 Generated with …" 等。
- 禁止伪造第二作者（`--author`、`GIT_AUTHOR_*`、`.mailmap` 都不行）。
- 确需说明生成方式时，只在正文写事实（如"由脚本批量生成"），**不加署名 trailer**。

提交前自查：

```bash
git log -1 --format=%B
git log -1 --format="%an <%ae>"
```

### 0.2 推送

- 改完**先同步到 Kali 让用户真机验证**；用户明确说"提交并推送"之后才 push。
- 这次用户已经明确确认过，所以 `eee19de3` 已 push。
- push 走本机代理，端口会变（2026-11-09 实测是 **7890**）。先查：

```bash
netstat -ano | grep -E "7890|7897"
git -c http.proxy=http://127.0.0.1:7890 -c https.proxy=http://127.0.0.1:7890 push origin main
```

- push 后必须比对：

```bash
git rev-parse HEAD
git rev-parse origin/main
```

出现过 `RPC failed; curl 52` + `Everything up-to-date` 的**假成功**，不要只看 push 输出。

### 0.3 测试与金钱

- 用户明确说过：**测试不要花他的钱**。不要为了验收去新建扫描、不要跑会消耗云端模型额度的任务。
  这次验收只观察了用户原本就在跑的一次 CTF 扫描。
- 破坏性操作（删数据、清库、重启服务、删镜像）先问用户。

### 0.4 全量测试跑着时不要改它正在测的源文件

`suvex-api/tests` 里有依赖 `inspect.getsource(函数)` 的断言。全量测试跑着时改行号
（加 docstring、格式化、切分支）会让断言切到别人家的代码上，出现"代码没错但全红"。
2026-10-25 实际踩过一次。要改就等跑完，改完重跑。

---

## 1. 项目、环境与部署

### 1.1 技术栈

- 后端：FastAPI + SQLAlchemy + PostgreSQL + Alembic，目录 `suvex-api/`
- 前端：React 19 + TypeScript + Vite + Tailwind，目录 `suvex-web/`
- 沙箱：Docker 镜像 `suvex-sandbox` / `suvex-sandbox-re` / `suvex-sandbox-ir`
- 产品叙事：不是"一次渗透任务的报告"，而是**一条持续流动的 agent 行为流**

### 1.2 两台机器

| 机器 | 角色 | 说明 |
| --- | --- | --- |
| Windows `C:\Users\Administrator\Desktop\新项目` | 编辑 + git | 没有可用的本地 Postgres；前端测试可跑 |
| Kali `kali@192.168.44.128` | 真机 + Docker + 后端测试 | 部署副本，不是 git 仓库 |

- SSH 已配好密钥（`~/.ssh/id_ed25519`），可以直接 `ssh kali`（Host 别名指向 192.168.44.128）。
- Kali 的 sudo 口令由部署者提供（**不要写进仓库**）；非交互执行用
  `echo <口令> | sudo -S -p "" <cmd>`。
- 前端生产入口：`http://192.168.44.128:8080`
- nginx 把 `/opt/suvex/suvex-web/dist` 挂到 `/root/suvex/suvex-web/dist`（fstab），
  **不要 `rm -rf` 这个目录**，会把挂载点拆掉。用 `find -mindepth 1 -delete` + `cp -a`。
- 后端代码：`/root/suvex/suvex-api`，API 由 systemd 管理。

### 1.3 前端构建与部署（完整命令）

```bash
cd /c/Users/Administrator/Desktop/新项目/suvex-web
npm run build                         # tsc -b + vite build
tar czf /tmp/dist.tgz -C dist .
scp -o BatchMode=yes /tmp/dist.tgz kali@192.168.44.128:/tmp/

ssh -o BatchMode=yes kali@192.168.44.128 '
  echo kali | sudo -S -p "" rm -rf /tmp/dist-new &&
  mkdir -p /tmp/dist-new &&
  tar xzf /tmp/dist.tgz -C /tmp/dist-new &&
  echo kali | sudo -S -p "" sh -c "
    find /root/suvex/suvex-web/dist -mindepth 1 -delete &&
    cp -a /tmp/dist-new/. /root/suvex/suvex-web/dist/ &&
    chown -R kali:kali /root/suvex/suvex-web/dist
  "
'
```

验证：

```bash
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://192.168.44.128:8080/
curl -s http://192.168.44.128:8080/ | tail -c 300   # 看 index-*.js / css hash
ssh kali 'findmnt /opt/suvex/suvex-web/dist'        # 确认挂载仍在
```

> `/tmp/dist-new` 可能被上一次部署留成 root 所有；再部署前要 `sudo rm -rf /tmp/dist-new`。

### 1.4 测试命令

前端（Windows 或 Kali 均可）：

```bash
cd suvex-web
npm run typecheck
npx vitest run                 # 全量；交接时 1257 passed
npx eslint <改过的文件> --max-warnings 0
```

后端（Kali，必须带 Postgres）：

```bash
cd /root/suvex/suvex-api
./.venv/bin/python -m pytest tests/ -q
```

- `tests/conftest.py` 在没显式设 `SUPEX_TEST_DATABASE_URL` 时会自动建一个本次运行
  唯一的库，并在结束时删掉；多个会话并发跑全量不会互相踩。
- 本地 Windows 没有可用 Postgres，所以后端全量只在 Kali 跑。

### 1.5 前端验收方式

- 这次用的是 Playwright MCP 的**持久化浏览器会话**，打开
  `http://192.168.44.128:8080` 时已经是 `admin` 登录态。
- **仓库里没有写 admin 口令**。换环境/换浏览器需要向部署者索取登录凭据。
- 主题存在 `localStorage['suvex-theme']`（`system` / `light` / `dark`）；
  验收时曾临时改成 `dark`/`light`，最后已 `removeItem` 恢复"跟随系统"。
- 截图产物都在 `artifacts/`（未提交，见 §5.1）。

---

## 2. 这次会话做了什么（时间线）

按发生顺序，把整段会话都列出来。每条都对应一次用户请求或一次明确修正。

### 2.1 数据语义批次（批次 2）

- 报告/漏洞/处理人三处"看得见但不生效"的修复：
  - `suvex-web/src/lib/report-build.ts`：`applyOptions` 的纳入条件从白名单
    `confirmed+fixed` 改为排除 `new` / `false_positive`，与后端 `document.py` 对齐，
    修掉 retesting/closed 被算成 0 的问题。
  - `ReportHistory` 增加 `canDelete`，非 admin 不显示删除。
  - `findings-store` 的 `loadFindingGroups` 带筛选参数与 in-flight guard；
    `lib/finding-query.ts` 新增 `narrowGroupsToFindings`，让分组计数/严重度/目标
    与当前可见列表一致（多处理人、SLA 这类后端表达不了的筛选）。
  - `FindingsPage` 的 URL intent 改成渲染期消费；导出带 `deleted` 范围。
  - 新增 `settings/MembersPanel.tsx`（`/members` 增删改停用、两步删除、未分配计数），
    接进 `SettingsPage` 的「处理人」分区。
- 提交：`1a99c7da fix: 报告/漏洞/处理人三处「看得见但不生效」`
- 提交：`196b81f9 fix: 资产/文件/应急/技能四页的「数」与「入口」不再失真`
  （资产 `total` 被循环变量遮蔽，改名为 `target_total`）

### 2.2 性能批次

- `scans-store.reconcileScans`：轮询时按内容复用行对象；完全没变时返回同一数组、
  不触发 store 提交。
- `ScanRow` / `FindingRow` / `AssetRow` 用 `memo`，回调改为稳定 `useCallback`。
- `useScansPolling` 在文档隐藏时不轮询，`visibilitychange` 时补一次。
- 提交：`2e59b829 perf: 轮询不再整表重渲染；列表行按引用复用`

### 2.3 对话（Chat）审计与修复

- 删除 MCP MultiSelect：引擎根本没有 MCP 客户端，留着是假的。
- 批量弹窗：`__none__` 筛选修好（原来永远 0 条）；失败不再静默，失败项保持选中并汇总；
  Markdown 下载加 toast；消息数措辞改对。
- `ChallengeProcess`：扫描记录已清理时显示中性说明，不再报错。
- 后端 `chat/resumer.py`：重试耗尽/不可重试时追加 `GIVE_UP_NOTE`，不再把
  "我在后台接着做"留成最后一句假话。
- 新增 `kernel/skill_usage.py`，`list_skills` 支持 `includeUsage`；系统提示词加
  「问事实，还是做任务」的分流，避免问答类请求自己给自己派活。
- 提交：`122c40c6`、`e7b66434`、`b14d6f62`、`36ece31a`、`2e6ffa6b`

### 2.4 工具管理页与镜像补缺

用户要求：重排工具管理页 UI/文案、调研成熟项目、**补真实缺口**（不是做缺口标签展示）。
中途明确修正过："我是让你去补缺口，不是让你写个标签展示"。

- `ToolsPage` 重做：三张摘要卡（内置工具 / MCP 连接 / 命令行配方）、三个 Tab、
  hash 深链、工具分组全开/全关、可折叠 `ToolRow`、配方 scenario 筛选（全部/CTF/渗透，
  `both` 同时算）。
- 真实镜像审计（`docker run command -v`）后补装：`wpscan`、`searchsploit`、
  `ghidra` + 无头 `ghidra-decompile` 包装器、`impacket`、`uncompyle6`、`pycdc`。
- **`msfconsole` 按用户决定保持不装**，只做"明确不装 + 替代方案"。
- 修正配方里的镜像事实：nuclei 模板 `/opt/nuclei-templates`、gdb 通过 `/etc/gdb/gdbinit`
  加载 GEF（`-nx` 会关掉）、masscan 非 root 无 NET_RAW、ffuf/gobuster 字典路径、
  hydra 无字典等。
- 提交：`f4bc8164`、`0a710826`、`d042867f`、`bf89cc78`

### 2.5 工具技能（Agent Skills）

用户要求："能装也要指导模型会用，怎么用的好，去调研 skill"。

- 新增 12 篇镜像级技能（`suvex-api/prompts/skills/tooling/`）：
  `ghidra-headless`、`pyc-decompile`、`android-reverse`、`pcap-analysis`、
  `volatility3`、`stego-toolkit`、`wpscan`、`searchsploit`、`impacket`、
  `crypto-toolkit`、`ropgadget`、`port-scanning`。
- `tests/kernel/test_tool_skills.py` 钉住：技能被发现/能加载/出现在模型清单，
  正文含镜像特有事实，不把 upx / rockyou / sox 当可用工具。
- 系统提示词加"配方只讲参数，坑在技能里"。
- 提交：`78c1b153`

### 2.6 数据清理（用户确认过）

用户选择"所有对话 + 回收站 + 归档"。

- 38 条对话全部先归档（销毁沙箱、清附件、删工作区）再彻底删除。
- 20 条回收站扫描 `DELETE /scans/{id}/purge`；2 条归档扫描先 `batch/delete` 再 purge。
- 保留：正常扫描 104 条、漏洞 121 条、资产/报告。
- `chat-workspaces` 3.9G → 60K；`_scan_runs` 1.2G / 6 个目录未动（属于活跃扫描）。
- 磁盘可用 2.3G → 6.1G。
- 归档后留下 9 个空工作区目录（60K），用户没有要求删。

### 2.7 本次最后一项：总览「实时活动」→ canvas 演出舞台

这是用户本轮最关注的工作，详细设计见 §3。

用户中途连续两次纠偏：

1. 第一版做成了一条阶段轨道 → "我要动画，你就给我搞一条线？"
2. 第二版做成 DOM 流程板（节点 + 签子 + 脉冲）→ "是不是对动画有什么误解，我要的是有演出效果"
3. 第三版改成 canvas 舞台 → 用户确认："是这个方向，提交并推送"

提交：`eee19de3 feat(overview): 实时活动改成 canvas 演出舞台`（已 push、已部署）。

---

## 3. 最后一项的详细设计（OMP 接手重点）

### 3.1 用户要的是什么

不是"信息图"，是**演出**：打开总览，第一屏要像一块正在运行的任务舞台，
能持续看到东西在动；新事件到达时要有明确的"节目"（信号飞过去、炸开），
而不是一条静止的线或几个小点。

### 3.2 数据流

```
GET /overview
  └─ activity: 最近 8 条投影事件（种子，覆盖订阅之前）
        │
        ▼
useOverviewLiveActivity(seed, enabled)
  ├─ enabled = 有活跃扫描（/overview 的 activeScans 或 scans-store 的 running/queued）
  ├─ enabled 时接全局 SSE：/api/v1/events（withCredentials）
  ├─ 完整事件 → Overview 投影（只留 6 种，reasoning/failed 丢掉）
  ├─ 按 id 去重、按 at 倒序、最多 40 条
  ├─ latestBatchIds：只标记**订阅之后真正新到**的事件
  └─ connection：connecting / live / disconnected（断线时界面必须说）
        │
        ▼
ActivityFeed (presentational)
  ├─ ActivityFlowStage（canvas + DOM 覆盖层）
  └─ ActivityFeedItem × 6（新行播 stream-in）
```

关键文件：

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `suvex-web/src/hooks/useOverviewLiveActivity.ts` | 265 | 种子 + SSE 合并、投影、去重、连接状态 |
| `suvex-web/src/components/overview/ActivityFlowStage.tsx` | 736 | canvas 舞台 + DOM 标签/计数/签子 |
| `suvex-web/src/components/overview/stage-model.ts` | 108 | 纯数据模型（可单测） |
| `suvex-web/src/components/overview/ActivityFeed.tsx` | 156 | 卡片壳、断线提示、行入场 |
| `suvex-web/src/components/overview/ActivityFeedItem.tsx` | — | `isNew` 行入场 |
| `suvex-web/src/pages/OverviewPage.tsx` | 439 | `OverviewActivityFeed` 数据壳、`isLive` 口径 |

### 3.3 为什么把 SSE 状态下沉到卡片里

`useOverviewLiveActivity` 每收到一批事件就 `setState`。如果它长在 `OverviewContent`，
每来一条事件会把风险分、严重度图、指标卡、token 卡全部重渲染一遍。
现在它在一个只包住活动卡片的 `OverviewActivityFeed` 组件里，变化只重渲染这张卡。

### 3.4 舞台画了什么

- **五个阶段节点**：信息收集 / 扫描 / 利用 / 验证 / 报告，各带 lucide 图标。
  当前阶段的节点有主色描边 + `bg-accent-wash` + 呼吸环 + 一层很淡的舞台光。
- **粒子流**：一条贯穿卡片、在节点处为 0、节点之间上下摆动的正弦流线；
  约 220 颗粒子，64 颗/秒生成，从左侧流到右侧。实时流连着时才生成。
- **智能体**：每个智能体是一颗绕着自己当前阶段节点公转的光点（椭圆轨道 30×22），
  节点下面挂签子（最多 2 枚，多出的 +N）。阶段变化时沿流线滑到新节点。
- **新事件**：一颗信号从左侧飞到目标节点（0.85–1.05s），落地炸开 24 颗放射粒子
  + 一圈扩散环；颜色按事件类型（verified/completed=ok、discovery=warn、
  blocked=risk、poc_generated=accentStrong、tool_call=accent）。
- **扫描线**：实时流连着时，一条 1px 亮线 + 34px 很淡的余晖缓慢掠过。
- **点阵底纹**：26px 间距的点阵，给舞台一点纵深。

### 3.5 诚实性约束（重要，不要为了"好看"破坏）

| 动效 | 触发源 | 断线/无事件时 |
| --- | --- | --- |
| 粒子流、扫描线 | 实时连接存活 | 立即停止，粒子排空 |
| 信号 + 爆炸 | **一条真实的新事件** | 不动 |
| 智能体公转 | 智能体当前阶段（最近 12 条） | 冻结 |

- 首批历史（种子）**不放信号**；只有 `latestBatchIds` 里的新事件才放。
- 计数窗口是**最近 12 条事件**（`stage-model.ts` 的 `WINDOW`），不是全量。
- 多 agent 并行时只表达**分布**，不画"已完成 → 当前 → 未开始"的单线进度。
- `prefers-reduced-motion` 下**整块 canvas 不挂载**；DOM 层的阶段、计数、签子照常可读。
- 亮色主题用 `strength = 0.45` 统一压透明度（暗色 1）；否则浅底上 220 颗粒子会糊成实心带。

### 3.6 可调参数（都在 `ActivityFlowStage.tsx` / `stage-model.ts`）

```ts
WINDOW = 12            // 计数窗口
MAX_CHIPS = 2          // 每节点最多几枚签
STAGE_HEIGHT = 156
NODE_TOP = 34
NODE_SIZE = 40
PATH_Y = 54
WAVE_AMP = 10
MAX_PARTICLES = 220
MAX_COMETS = 8
MAX_BURSTS = 10
spawnAcc += dt * 64    // 粒子生成率
speed: 0.1..0.22       // 穿越整条流线的速度（4.5–10s）
bright 概率 0.12       // 领头彗星
burst: 24 颗 / 900ms
orbit: 30 × 22，0.55 rad/s
```

如果用户要"更夸张"，优先调：`spawnAcc`、`MAX_PARTICLES`、burst 速度/半径、
bright 概率；不要改成循环播放的假事件。

### 3.7 测试与测试环境改动

- `src/components/overview/ActivityFlowStage.test.tsx`：
  - `buildStageModel` 的计数/签子/最新阶段/窗口；
  - DOM 层五个阶段标签、计数、智能体签；
  - 减少动效时不挂载 canvas，但信息仍在。
- `src/hooks/useOverviewLiveActivity.test.ts`：
  - 无活跃扫描不连；
  - 增量合并到种子前面；
  - 过滤 reasoning/failed；
  - 轮询重复事件不重复、不再当新事件；
  - 连接状态如实上报；
  - 环境不支持 EventSource 时不连、不冒充实时。
- `src/test/setup.ts` 新增：`HTMLCanvasElement.prototype.getContext` 在 jsdom 统一返回 `null`，
  避免 jsdom 的 "Not implemented" 刷屏；组件必须把 `null` 当正常降级。
- `src/pages/OverviewPage.test.tsx` 仍是 13 条，全部通过。

### 3.8 验证到哪一步

- `npm run typecheck` 通过。
- `npx vitest run`：**86 个文件 / 1257 个用例全部通过**。
- 改动文件的 ESLint 零告警（全仓 lint 仍有历史错误，见 §5）。
- 已构建、部署到 Kali；Playwright 实测：
  - `http://192.168.44.128:8080/overview` HTTP 200；
  - 页面存在 canvas；五个阶段的 aria-label 正常；
  - 控制台 0 error（有一次观察到两次 `/api/v1/scans` 的
    `ERR_CONNECTION_TIMED_OUT`，是 VM 网络瞬断，和舞台无关）。
- 截图：
  - `artifacts/overview-stage-final-dark-1440.png`（暗色 1440，最终版）
  - `artifacts/overview-stage3-light-1440-b.png`（亮色 1440）
  - `artifacts/overview-stage-390.png`（移动端 390）
  - 更早的 `artifacts/overview-stage*.png` 是迭代过程，可忽略。

### 3.9 没验证到的（OMP 要知道）

1. **真实事件的爆炸没有截到图**。交接时那次 CTF 扫描在「利用」已经 13+ 分钟没有新事件，
   只看到持续粒子流/公转光点，没有等到一次真实的信号+爆炸。
   代码路径有单测覆盖（`latestBatchIds` → incoming → comet → burst），但没有视觉证据。
   下次有真实事件时，盯一眼舞台即可确认。
2. **真实可见浏览器里的帧率没测**。Playwright headless 下 rAF 被压到 1fps，
   量不出真实性能。按 220 颗粒子 + 330 个点阵点估算，桌面浏览器应无压力；
   如果要在低端集显上确认，用真实窗口跑一次。
3. **亮色主题的强度**（0.45）是截图调的，用户可以再提"太淡/太浓"。
4. **`ActivityFlowStage.tsx` 736 行**：目前内聚（一个 canvas 循环 + 一个 DOM 层）。
   如果再往里加功能，考虑把 canvas 循环拆成 `useStageCanvas`，但不要为了拆而拆。

---

## 4. 代码/设计约定（接着写之前先看）

- **React 19**：优先"渲染期状态调整"而不是 `useEffect` + `setState`
  （项目里有 `react-hooks/set-state-in-effect` 规则）；store 用模块级状态 +
  `useSyncExternalStore`；快照引用稳定。
- **中文注释要写"为什么"**，不是"是什么"。每个非显然的决定都要能回答
  "不这么做会怎样"。
- **设计令牌**：颜色/间距/字阶只从 `tailwind.config.js` 与 `src/index.css` 取；
  不要裸 hex；不要紫色渐变、不要零偏移彩色光晕、不要 >1px 彩色左边框。
- **动效**：时长四档（instant 80 / fast 120 / base 200 / slow 420）+
  固定例外；动效服务于信息，不服务于装饰；"先隐藏、等 JS 放行"的写法一律否决；
  `prefers-reduced-motion` 下必须真的停。
- **诚实 UI**：空值不能写成 0；数据旧了要标出来；断线不能假装实时；
  没有事件不能编事件。这次舞台的"粒子流=连接、爆炸=真实事件"就是这条的延伸。
- **组件契约**：`ActivityFeed` 不认识数据来源；`ActivityFlowStage` 只吃
  `events / latestBatchIds / flowing`；页面负责取数。
- **提交信息**：中文、`feat/fix/perf/chore(scope): ...`，正文说明取舍；
  不加任何 AI 署名。

---

## 5. 仓库当前状态与未完成事项

### 5.1 工作区未提交文件（不要 `git add -A`）

`git status --short` 里还有 32 项未跟踪内容，主要是：

- `.playwright-mcp/`：Playwright 的页面快照/控制台日志
- `artifacts/`：历次设计验收截图（包括这次舞台的截图）
- `docs/赛后总结-平台问题与两个解题对话-20261107.md`
- `suvex-api/scripts/_diag_solving_list.py`（临时诊断脚本）

这些都**没有**进 `eee19de3`。不要为了"干净"把它们提交进去；
截图如果要留，问用户放哪（`artifacts/` 目前是本地验收目录）。

### 5.2 未合并的 MCP 分支

- 分支：`claude/busy-euclid-4a774c`
- 提交：`009bef35`
- 状态：**未合并、未推送**（不在 main 上）
- 这是更早一个会话的 MCP 功能分支。用户没有要求合并；**不要擅自 merge 或 push**。
  如果用户问起，先说明它与当前 main 的关系，再等指示。

### 5.3 已故意不做的事

- **`msfconsole` 不安装**：用户明确决定，当前替代品够用（nmap/nuclei/sqlmap 等）。
  不要再"顺手补上"。
- 总览页仍保留 60 秒轮询：指标卡回答"积累了什么"，不追实时；
  只有活动卡片走 SSE。不要为了"更实时"把整页改成 SSE，成本和收益不匹配。
- 之前评估过"扫描/漏洞列表服务端查询参数化"，已明确推迟；没有做。

### 5.4 已知的历史问题（不是这次引入的）

- 全仓 `npm run lint` 仍有错误/告警：
  - `src/components/chat/GroupNameDialog.tsx`：`react-hooks/set-state-in-effect`
  - `src/pages/SkillsPage.tsx`：同样规则
  - 若干 `react-refresh/only-export-components` 告警
  这次只保证**改动文件**零告警，没有顺手改无关文件。
- 真机上偶发 `/api/v1/scans` 超时（VM 网络瞬断），页面会保留旧数据；
  总览页有 `FreshnessNote` 说明数据新鲜度。
- 归档清理后留下 9 个空工作区目录（共 60K），没删。

---

## 6. 常用命令速查

```bash
# 前端
cd /c/Users/Administrator/Desktop/新项目/suvex-web
npm run dev
npm run typecheck
npx vitest run
npx vitest run src/components/overview/ActivityFlowStage.test.tsx
npx eslint src/components/overview/ActivityFlowStage.tsx --max-warnings 0
npm run build

# 提交（身份已配置，不要改）
cd /c/Users/Administrator/Desktop/新项目
git add <明确列出的文件>
git commit -m "feat(scope): ..." -m "正文..."
git log -1 --format="%an <%ae>"
git log -1 --format=%B

# 推送（代理端口先查）
netstat -ano | grep -E "7890|7897"
git -c http.proxy=http://127.0.0.1:7890 -c https.proxy=http://127.0.0.1:7890 push origin main
git rev-parse HEAD && git rev-parse origin/main

# 后端测试（Kali）
ssh kali
cd /root/suvex/suvex-api
./.venv/bin/python -m pytest tests/ -q

# 真机页面
http://192.168.44.128:8080/overview
```

---

## 7. 关键文件索引

### 7.1 本次最后一项

```
suvex-web/src/hooks/useOverviewLiveActivity.ts        种子 + 全局 SSE 合并
suvex-web/src/hooks/useOverviewLiveActivity.test.ts   合并/去重/过滤/连接状态
suvex-web/src/components/overview/ActivityFlowStage.tsx      canvas 舞台 + DOM 层
suvex-web/src/components/overview/stage-model.ts             纯数据模型 + WINDOW
suvex-web/src/components/overview/ActivityFlowStage.test.tsx 模型与 DOM 测试
suvex-web/src/components/overview/ActivityFeed.tsx           卡片壳 + 断线提示
suvex-web/src/components/overview/ActivityFeedItem.tsx       isNew 入场
suvex-web/src/pages/OverviewPage.tsx                         OverviewActivityFeed 数据壳
suvex-web/src/test/setup.ts                                  canvas getContext 测试桩
suvex-web/src/design-system.md                               §5.1 实时流程舞台（v1.82）
```

### 7.2 会话中涉及的其它关键位置

```
suvex-web/src/lib/report-build.ts                 报告范围语义
suvex-web/src/lib/finding-query.ts                分组收窄
suvex-web/src/data/findings-store.ts              漏洞列表/分组
suvex-web/src/data/scans-store.ts                 扫描 reconcile
suvex-web/src/hooks/useScansPolling.ts            全局轮询（隐藏暂停）
suvex-web/src/components/settings/MembersPanel.tsx 处理人 CRUD
suvex-web/src/pages/ToolsPage.tsx                 工具管理页
suvex-web/src/components/tools/ToolRow.tsx        工具行折叠/限制
suvex-web/src/data/tools.ts                       配方 scenario / coverage
suvex-api/app/chat/resumer.py                     放弃注释
suvex-api/app/kernel/skill_usage.py               技能使用统计
suvex-api/app/chat/engine.py                      list_skills(includeUsage)
suvex-api/app/kernel/recipes.py                   53 条配方 + scenario
suvex-api/docker/suvex-sandbox/Dockerfile         新装工具 + 自检
suvex-api/prompts/skills/tooling/*.md             12 篇工具技能
suvex-api/tests/kernel/test_tool_skills.py        技能发现/镜像事实测试
```

---

## 8. 给 OMP 的建议下一步

按优先级：

1. **先看真机**：打开 `/overview`，确认粒子流、公转光点、当前节点舞台光都在；
   如果恰好有事件，确认"信号飞过去 + 爆炸"的视觉效果。没有事件就等，
   不要为了看效果去新建消耗模型额度的扫描。
2. **问用户是否要调强度**：当前是"持续流动 + 事件爆发"的克制版。
   如果用户觉得还不够演出，优先调 §3.6 的参数，而不是改架构。
3. **如果要补视觉证据**：在开发环境加一个只用于截图的 burst 预览（不要进生产），
   或者在真实事件到达时截图；不要把假事件写进产品。
4. **MCP 分支**：除非用户明确说合并，否则不动 `claude/busy-euclid-4a774c`
   （`009bef35`）。
5. **不要再动已清理的数据**：对话/回收站/归档已经是 0；扫描和漏洞是用户要保留的。
6. **提交前**：只 add 明确列出的源码文件；确认作者是 `Suvex Dev`；
   确认没有 `Co-Authored-By` / AI 署名；push 后比对 HEAD 与 origin/main。

---

## 9. 用户在这次会话里明确表达过的偏好

- 要"演出效果"，不是"信息图"；不要用一条线糊弄。
- 改前端/设计前，先联网看成熟项目的做法（不要闭门造车）。
- 工具页要能真正管理工具；缺口要真的补进镜像，不是做个标签展示。
- 装了工具还要教模型怎么用（skill），不是装完就算。
- `msfconsole` 保持不装。
- 测试不要花他的钱（不要新建消耗额度的扫描/云端模型任务）。
- 提交/推送不要任何 AI 共同作者署名。
- 改动先上 Kali 真机看，确认后再 push。
