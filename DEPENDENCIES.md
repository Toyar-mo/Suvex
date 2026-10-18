# 依赖与许可证清单

> 用途：参赛方案**原创性及保密性声明**的附件 —— 如实列出本项目**自研部分**与**第三方组件**的边界。
> 生成方式：全部由命令实测（见每节的"取证方式"），**不含估计值**。
> 生成时间：2026-10-15

---

## 一、结论摘要（先看这里）

| 项 | 事实 |
| --- | --- |
| 本项目自身许可证 | **Apache-2.0**（仓库根 `LICENSE`，11.1 KB） |
| 版权归属 | `NOTICE`：`Suvex / Copyright 2026 Suvex` |
| 后端第三方依赖 | **44 个**（含 dev 工具）；全部为宽松许可证 |
| 前端第三方依赖 | **553 个**（含传递依赖，实测自 `package-lock.json`）；宽松为主，**1 处需留意**（见 §4） |
| 外部工具依赖 | **1 个**：`strix-agent` 1.6.2（**Apache-2.0**） |
| 源码拷贝的第三方代码 | **1 处**：ReactBits 4 个组件（**MIT + Commons Clause**，已在 `NOTICE` 披露） |
| 自研代码量 | 后端 `app/` **47,622 行** / 前端 `src/` **45,624 行** / 后端 `tests/` **36,677 行**（`wc -l` 口径，见 §5） |

**一句话**：本项目**没有抄任何第三方源码**（唯一的源码拷贝是 4 个前端动效组件，已披露）；`strix` 是**作为外部 CLI 集成的可替换执行后端**，不是被复制的代码。

---

## 二、自研部分 vs 第三方边界（声明函的核心）

### 2.1 本项目自研

| 部分 | 位置 | 说明 |
| --- | --- | --- |
| 平台后端 | `suvex-api/app/` | FastAPI + PostgreSQL，13 个领域模块 |
| 平台前端 | `suvex-web/src/` | React 19 + TS 6 |
| 接口契约 | `suvex-web/docs/api-contract.md` | 唯一事实来源，前后端唯一耦合点 |
| **CTF2 自主解题循环** | `app/ctf2/runner.py` | ★ **完全不依赖 strix**，自研的 agent 循环 |
| strix 适配层 | `app/scans/strix_events.py`、`runner.py`、`app/findings/strix_vulns.py` | 共 **3,245 行**（`wc -l` 口径，含空行与注释；纯代码 2,084 行），自研（详见 2.3） |
| 测试 | `suvex-api/tests/`（36,677 行） | 后端 **1,352 个测试函数 / 75 个测试文件**（参数化展开后实际执行 2,432） |

### 2.2 外部工具（集成，非复制）

| 组件 | 版本 | 许可证 | 集成方式 |
| --- | --- | --- | --- |
| `strix-agent` | 1.6.2 | **Apache-2.0** | ★ **作为外部 CLI 子进程调用**（`subprocess.Popen`），**未复制其源码** |

**取证方式**：
- 版本与许可证：读 `/root/strix-venv/.../strix_agent-1.6.2.dist-info/METADATA` → `License-Expression: Apache-2.0`；
- 集成方式：`app/scans/runner.py` 用 `subprocess.Popen` 拉起 `strix_command`（默认 `"strix"`），且该命令**可配置**；
- **仓库内无 strix 源码副本**：`find /root/suvex -iname "*strix*"` 的命中全部是①自研适配层文件 ②运行产物目录（`_scan_runs/*/strix_runs/`）。

### 2.3 为什么适配层算自研（不是"包装"）

`strix` 官方文档称结构化事件在 `strix_runs/<run>/events.jsonl`，而**在 `strix-agent` 1.6.2 源码里全包搜索 `events.jsonl` 零命中**（见 `app/scans/runner.py:1354-1357` 的实测记录）。真实事件流在 `.state/agents.db` 这个 SQLite 里，本项目**自行逆向**其读法（照 `interface/tui/history.py` 的查询）。

适配层做的语义映射是自研设计：

| 自研内容 | 位置 |
| --- | --- |
| strix 的结构化事件 → 本项目契约的 8 种事件 | `strix_events.py:45-73`（strix 侧 **6 种**有来源；`completed` 由扫描生命周期发；`blocked` 在 strix 流里**无来源**、由本项目生命周期的三个发出点发；`verified` 用的是本项目自己的置信度口径 —— 映射表逐行标注了依据强度） |
| strix severity/confidence → 本项目 5 档 + CVSS 语义轴 | `strix_vulns.py:90-105` |
| agent 名 → 5 种角色的启发式、运行状态映射 | `strix_events.py:122-137` |
| 稳定 agent id 生成（≤64 字符，sha1 兜底） | `strix_events.py:538-548` |

### 2.4 ★ 可替换性（工程主张）

`scan_runner` 是配置项，支持 **`strix | fake`** 两个执行后端（`app/scans/runner.py:1585`）。`fake` 是一个**真子进程**（含真孙进程）的替身后端，用于测试与 CI —— **绝不能跑真实攻击**（`app/core/config.py:116`）。

这条设计的意义：**执行后端是可替换的**，`strix` 的升级或替换不会绑死整个 API（`config.py:118` 明写）。这正是评分维度「系统架构与工程实现」与「工具协同与扩展能力」要考的能力。

---

## 三、后端依赖（44 个，实测自 venv）

**取证方式**：遍历 `/root/suvex/suvex-api/.venv/lib/python3.13/site-packages/*.dist-info/METADATA`，读 `Name` / `Version` / `License-Expression`。

**44 = 运行时 13 + 传递 25 + 开发/测试 5 + `pip` 1**。`pip` 不是本方案的依赖，它随 venv 创建而来，列在这里只是因为遍历目录会数到它；把它算进来是为了让"44"这个数与取证命令的输出**逐一对得上**。

### 3.1 运行时依赖（`requirements.txt` 声明）

| 包 | 版本 | 许可证 |
| --- | --- | --- |
| fastapi | 0.141.1 | MIT |
| uvicorn | 0.53.0 | BSD-3-Clause |
| SQLAlchemy | 2.0.54 | MIT |
| alembic | 1.20.0 | MIT |
| psycopg | 3.3.6 | ⚠️ **LGPL-3.0-only** |
| psycopg-binary | 3.3.6 | ⚠️ **LGPL-3.0-only** |
| pydantic | 2.13.5 | MIT |
| pydantic-settings | 2.15.0 | MIT |
| argon2-cffi | 25.1.0 | MIT |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| idna | 3.20 | BSD-3-Clause |

> `idna` 原先只是 httpx 的传递依赖。2026-10-16 修 IDNA 逃逸回归时，适配层要
> `import idna` 才能精确接住 `idna.IDNAError`，于是在 `requirements.txt` 里
> **显式声明**（`idna>=3.6,<4.0`）—— 直接 import 的包不能靠别人的传递依赖兜着。

### 3.2 传递依赖（全部宽松）

`annotated-doc`(MIT) · `annotated-types`(MIT) · `anyio`(MIT) · `argon2-cffi-bindings`(MIT) · `certifi`(MPL-2.0) · `cffi`(MIT-0) · `click`(BSD-3-Clause) · `greenlet`(MIT AND PSF-2.0) · `h11`(MIT) · `httpcore`(BSD-3-Clause) · `httptools`(MIT) · `Mako`(MIT) · `MarkupSafe`(BSD-3-Clause) · `packaging`(Apache-2.0 OR BSD-2-Clause) · `pycparser`(BSD-3-Clause) · `pydantic_core`(MIT) · `Pygments`(BSD-2-Clause) · `python-dotenv`(BSD-3-Clause) · `PyYAML`(MIT) · `starlette`(BSD-3-Clause) · `typing_extensions`(PSF-2.0) · `typing-inspection`(MIT) · `uvloop`(MIT) · `watchfiles`(MIT) · `websockets`(BSD-3-Clause)

### 3.3 开发/测试依赖

| 包 | 版本 | 许可证 |
| --- | --- | --- |
| pytest | 8.4.2 | MIT |
| pytest-asyncio | 1.4.0 | Apache-2.0 |
| ruff | 0.16.8 | MIT |
| iniconfig | 2.3.0 | MIT |
| pluggy | 1.6.0 | MIT |

> **锁文件**：`requirements.lock`（37 包，带 sha256）与 `requirements-dev.lock`（43 包，带 sha256）提供可复现构建。两份锁文件的分工与理由见 `requirements-dev.txt` 头部；它们是否与声明同步，由 `suvex-api/scripts/verify_lockfiles.py` 校验（CI 里也跑）。

---

## 四、前端依赖（553 个，实测自 `package-lock.json`）

**取证方式**：读 `suvex-web/package-lock.json` 的 `packages` 字段（不含根条目），逐条取 `version` / `license`。选锁文件而不是 `node_modules` 目录，是因为**依赖图与平台无关、且随仓库提交**，任何人 checkout 后都能复现同一个数字；而 `node_modules` 的实际内容随操作系统变化（平台专属的 optional 包只装匹配的那一个）。

### 4.1 直接依赖

| 包 | 版本 | 许可证 |
| --- | --- | --- |
| react | 19.3.0 | MIT |
| react-dom | 19.3.0 | MIT |
| react-router-dom | 7.18.4 | MIT |
| react-markdown | 10.1.0 | MIT |
| remark-gfm | 4.0.1 | MIT |
| lucide-react | 1.47.0 | ISC |
| motion | 13.4.0 | MIT |
| clsx | 2.1.1 | MIT |
| tailwind-merge | 3.7.0 | MIT |
| ogl | 1.0.11 | Unlicense |
| @fontsource-variable/geist | 5.3.0 | OFL-1.1 |
| @fontsource-variable/jetbrains-mono | 5.3.0 | OFL-1.1 |
| @fontsource-variable/noto-sans-sc | 5.3.0 | OFL-1.1 |

### 4.2 传递依赖里需留意的

| 包 | 许可证 | 说明 |
| --- | --- | --- |
| `lightningcss` / `lightningcss-linux-x64-gnu` | MPL-2.0 | 弱 copyleft，**文件级**；作构建依赖使用，不修改其源码 → 无义务传导 |
| `caniuse-lite` | CC-BY-4.0 | 数据文件，署名即可 |
| `@csstools/color-helpers` | MIT-0 | 等同 MIT，无需署名 |
| 字体（3 个 fontsource 包） | OFL-1.1 | 字体许可证，**随应用分发需保留 OFL 声明** |

其余 500 余个传递依赖为 MIT / ISC / BSD / Apache-2.0 等宽松许可证。

### 4.3 ⚠️ 一处需你决策的风险

**① GSAP —— 已移除（原先唯一的非 OSI 许可）**

`gsap@3.15.0` 的 `package.json` 写着 `Standard "no charge" license: https://gsap.com/standard-license`。这是 **GreenSock 自有许可证**（允许免费商用，但**不满足"开源"的定义**），不是 OSI 认可的开源许可。

- **它原先的用处**：`gsap` 在本项目**只被 `DotGrid.tsx`**（ReactBits 拷贝件）依赖，而 `DotGrid` 经 `AgentField` / `HeroBackdrop` 懒加载，不进首屏关键路径 —— 是纯装饰。
- **处置**：**已移除**（`chore(web): 移除 gsap 依赖与 DotGrid 组件`）。删除 `gsap` 依赖项与两处调用后，`gsap` 整条依赖出树（`package.json`、`package-lock.json` 均无残留）。
- **★ 2026-10-16 复核：`DotGrid.tsx` 已恢复，但 `gsap` 仍然是移除状态。** 用户要求把点阵加回来（那处鼠标交互是首页的一处体验），并明确选了"加回来但不依赖 gsap" —— 要的是**效果**，gsap 只是它顺手用的工具。于是 `DotGrid` 的物理改成**自研**（惯性滑出用指数逼近的闭式解、弹性回位用欠阻尼弹簧，积分放进该文件已有的那个 rAF 循环），`gsap` 的依赖项**没有回来**：`package.json` / `package-lock.json` 里仍然没有它，`src/` 下也没有任何 `import ... from 'gsap'`。详见 `src/components/effects/README.md` 与 `DEPENDENCIES.md` §六。
- **留下的结论**：前端依赖现在**没有非 OSI 许可的包**。但**仍不要写"全部依赖均为开源许可证"** —— 见下方 ②，以及 §4.2 里 MPL/CC-BY/OFL 这几类需要留意的许可；建议措辞始终是"依赖许可证见清单"。

**② psycopg 是 LGPL-3.0-only**

LGPL 是弱 copyleft。**作为未修改的库依赖使用**（本项目就是如此，通过 `psycopg[binary]` 安装）**不产生传导义务**。但如果将来**修改 psycopg 源码**或**静态链接**，义务会变。当前无风险，记录备查。

---

## 五、代码行数（README 口径，用于声明函与 PPT）

**口径**：README 自己声明的「文件总行数（含空行与注释），用 `find … | xargs wc -l` 量」——即**数换行符 `\n`**。

| 目录 | 文件数 | 行数 |
| --- | --- | --- |
| 后端 `suvex-api/app/`（`.py`） | 118 | **47,622** |
| 前端 `suvex-web/src/`（`.ts`/`.tsx`） | 162 | **45,624** |
| 后端 `suvex-api/tests/`（`.py`） | 80 | **36,677** |
| **合计（含测试）** | 360 | **129,923** |

前端细分：`.ts` 64 文件 15,944 行 · `.tsx` 98 文件 29,680 行（本机工作区口径）。

> **本表为 2026-10-17 快照**（本机工作区，HEAD `50b4376`）。上一版是同日 `1a213d1` 的
> 47,612 / 45,584 / 36,674 = 129,870。两次之间 `app/` +10 行、`src/` +40 行、`tests/` +3 行，
> 来自状态条接真实 token 用量（`0111e2c`，`fleet.ts` +39/−9、`overview-store.ts` +25、
> `TopStatusBar.tsx` +29/−48、`Layout.tsx` +8/−4）与 R-4 收口（`9337752`，`redirect.py`）。

> ⚠️ **这组数字的取数口径要说清楚**：上表的行数是在**本机工作副本**上量的
> （`find … | xargs wc -l`，即数换行符 `\n`）。Kali 上的工作副本可能落后若干提交，
> 量之前先确认两边的 HEAD 一致。**"行数"这类指标必须连同取数位置与提交号一起说**，
> 否则两边对不上账。

> 前端数字的最近一次变化（2026-10-16）：恢复 `DotGrid.tsx`（708 行）
> 并新增它的物理单测 `DotGrid.test.ts`（309 行）。
> 该组件 2026-10-15 曾因 `gsap` 的非 OSI 许可被移除（当时 −401 行），
> 这次以**自研物理**恢复，所以 `gsap` 的移除状态不变。

**⚠️ 口径警告**：另有两种算法会得到不同数字，**不要混用**：

| 算法 | 后端 `app/` | 前端 `src/` | 差异原因 |
| --- | --- | --- | --- |
| `wc -l`（数 `\n`）**← README 口径** | 47,622 | **45,624** | — |
| Python `splitlines()`（数逻辑行） | 47,624 | **45,626** | `app/` 与 `src/` 各有文件末行无换行符，`wc -l` 少算 |

`README.md` 里更早写过的 `43,256` / `41,941`、以及 `32,483` 这类数字都是**过期快照**，代码演进后已漂移 —— 这正是 README 自己警告的坑（`README.md:263-264` 记着"曾写 32,483 而实际 39,163"）。**上表是唯一权威口径**，其余位置引用时请照抄这里。

**给参赛材料的建议**：写「**约 13 万行（含测试，含空行与注释）**」，或按上表精确写并注明口径。**不要写一个没有口径的精确数**。

---

## 六、第三方源码拷贝（已在 NOTICE 披露）

| 组件 | 数量 | 许可证 | 位置 | 处置 |
| --- | --- | --- | --- | --- |
| ReactBits 动效组件 | 4 个 | **MIT + Commons Clause v1.0** | `suvex-web/src/components/effects/` | 已披露，逐文件记录了上游路径与本地改动 |

**取证方式**：读 `suvex-web/src/components/effects/README.md`（该文件本身即完整披露：来源、取用日期、上游路径、本地改动、已知坑）。

- 上游：<https://github.com/DavidHDev/react-bits>，取用日期 2026-02-26；
- 4 个文件：`Threads.tsx`、`LetterGlitch.tsx`、`CountUp.tsx`、`DotGrid.tsx`；
- ⚠️ **`DotGrid.tsx` 的依赖不同**：上游它依赖 `gsap`（GreenSock 自有许可，非 OSI），本项目 2026-10-16 把它的物理改成**自研**，于是它与其他三个一样**零额外依赖**。这条也意味着：`gsap` 的移除状态没有被这次恢复推翻（见 §4.3 ①）；
- Commons Clause 限制"把组件本身单独作为产品售卖"，**本项目是应用内使用，符合条件**；
- 分发时需保留版权声明（`Copyright (c) 2026 David Haz`）。

> 这一处**已经在 `NOTICE` 里如实披露**，措辞是"ReactBits 组件的**源码拷贝**（MIT + Commons Clause），不是 npm 依赖"。**这份清单只是把它展开**。

---

## 七、声明函措辞建议

基于以上事实，建议声明函采用如下口径（**分三层，如实、不含糊**）：

1. **自研**：平台本体（后端 / 前端 / 契约体系 / 测试）、**CTF2 自主解题循环**、strix 适配层（3,245 行）均为本项目自行设计与实现；
2. **集成**：扫描执行内核集成开源项目 `strix-agent`（Apache-2.0），**作为可替换的外部 CLI 后端调用，未复制、未修改其源码**；平台通过 `scan_runner` 配置支持 `strix | fake` 两种后端，不绑定任何单一实现；
3. **披露**：前端 `effects/` 下有 4 个 ReactBits 组件源码拷贝（MIT + Commons Clause），出处与本地改动见 `NOTICE` 与 `effects/README.md`；其余第三方依赖及其许可证见本清单。

**不要写"全部自研"或"未使用任何第三方代码"** —— 那与事实不符，且 `NOTICE`、`LICENSE`、依赖清单、`node_modules` 都能反证。如实分层披露**不扣分**（工程规范是加分项），虚假声明才是致命风险。

---

## 八、复现方式

本清单全部数字可复现：

| 数据 | 命令 |
| --- | --- |
| 后端依赖许可证 | 遍历 `.venv/lib/python3.13/site-packages/*.dist-info/METADATA` |
| 前端依赖许可证 | 读 `suvex-web/package-lock.json` 的 `packages`（`npm ls --all` 只列已落盘的树，含平台差异） |
| strix 版本与许可证 | 读 `strix_agent-1.6.2.dist-info/METADATA` 的 `License-Expression` |
| 行数 | `find suvex-api/app -name "*.py" \| xargs wc -l`（同 README 口径） |
| 源码拷贝清单 | `suvex-web/src/components/effects/README.md` |
| 锁文件是否与声明同步 | `cd suvex-api && python3 scripts/verify_lockfiles.py`（CI 的 `hygiene` job 也跑它） |
