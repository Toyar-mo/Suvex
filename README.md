# Suvex

**一个自治的 AI 安全测试平台** —— 给它一个目标（网站、代码仓库、TCP 服务、
题目附件），它自己规划、调用工具、验证结论，最后交付一份可直接给人看的报告
或 CTF 题解。

```
suvex-api/    后端：Python 3.12 · FastAPI · PostgreSQL 16 · 32,483 行
suvex-web/    前端：React 19 · TypeScript 6 · Vite 8 · 35,272 行
```

> **契约是唯一事实来源**：[`suvex-web/docs/api-contract.md`](suvex-web/docs/api-contract.md)
> （2,379 行）。前后端只通过它耦合，规矩是「**发现遗漏先改文档再改代码**」。
> 两者放在同一个仓库，就是为了让契约与两侧实现的同步改动落在**同一个提交**里。

---

## 它解决什么问题

传统扫描器输出的是**漏洞清单**：几百条按规则匹配的结果，其中大量是误报，
而且没人知道哪条真的能打通。人工渗透测试输出的是**结论**，但一个人一周只能
做一两个目标。

Suvex 的取舍是：**让 agent 像人一样去验证**，然后只交付它真的确认过的东西。
所以它的核心不是"扫得更多"，而是三件配套的事：

| 机制 | 解决的问题 |
| --- | --- |
| **执行留痕** | 每一次工具调用、推理、发现都进事件流并落库，报告的每条结论都能追到过程 |
| **证据分级** | 区分"确证"与"启发式"，拿不准的标 `needs_follow_up`，**不把猜测写成结论** |
| **判据可配** | `findVulns`（找漏洞）/ `captureFlag`（夺旗）/ `verifyFix`（验证已修），决定"怎样算成功" |

---

## 两个入口

### 1. 渗透评估

给一个目标 + 任务背景（测试范围、测试账号、注意事项），产出漏洞报告。
漏洞有完整的处置工作流：状态流转（`new` → `confirmed` → `fixed` → `closed`）、
指派负责人、SLA 修复时限、按类型分组、附件存证、误报标记。

### 2. CTF 夺旗

给题目附件与题面，agent 去解题。解出后交付的是**题解（Writeup）**而不是漏洞报告 ——
因为一次夺旗任务的产物是"怎么想出来的"，不是"哪里有洞"。

> 判据是 `captureFlag` 时，服务端会把 agent 写的报告**重新分节**成题解版式，
> 并把 flag 提到最前面。执行后端的报告标题与分节标题都是写死的渗透口径，
> 所以这一步只能由我们做（详见 `suvex-api/app/scans/writeup.py` 的模块说明）。

---

## 跑起来

### 后端

```bash
cd suvex-api
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env                 # 然后生成 SUPEX_SECRET_KEY：python -m app.core.crypto
alembic upgrade head
uvicorn app.main:app --reload        # http://127.0.0.1:8000/api/v1/docs
```

### 前端

```bash
cd suvex-web
npm install
npm run dev                          # http://localhost:5173
```

前端默认请求**当前页面的 origin**，由 Vite 的 `server.proxy` 把 `/api`
转发到后端。所以换地址访问、换机器部署都不需要改配置，而且因为是同源请求，
既没有 CORS 问题，Cookie 也一定带得上。

### 一次跑起来（docker compose）

```bash
cd suvex-api && docker compose up --build
```

---

## 当前状态（如实说）

### 已经做到的

| 项 | 数字 |
| --- | --- |
| 后端代码 | 32,483 行，11 个领域模块 |
| 后端测试 | **55 个文件 / 903 个测试函数**，对**真实 PostgreSQL** 跑（不是 SQLite） |
| 数据库迁移 | 30 个 Alembic 版本，从第一个迁移就版本化 |
| API 端点 | 103 个 |
| 前端代码 | 35,272 行，140 个文件，**0 个 `any` / 0 个 `@ts-ignore`** |
| 决策记录 | 8 篇 ADR，每篇写**为什么**而不是**是什么** |

真机验证过的（一台有 Docker 的 Kali 上部署后）：

* 扫描编排 → 真 strix 子进程 → Docker 沙箱容器 → 事件导入，链路全通；
* 单次扫描观测到 **936 条**事件，`agentCount` 从 0 涨上去；
* CTF2 判题引擎实际解出过题目，flag 落库（如 `CTF2{e1775a04-…}`）。

### 还没有做到的

**这是给评估方看的清单，不是"待办事项"的美化版。**

| # | 缺口 | 影响 |
| --- | --- | --- |
| 1 | **前端零自动化测试** | 35k 行只有类型检查与 lint 兜底，运行时行为无人守 |
| 2 | **两侧都没有 CI** | 903 个后端测试全靠人手跑，且必须串行（10–15 分钟） |
| 3 | **6 个端点仍是 `501`** | 全部集中在通知与设置快照（`GET /settings` + 5 个通知端点） |
| 4 | **`docker compose` 未在容器里实测** | Kali 上是 systemd + venv 部署的，不是 compose |
| 5 | **契约文档有一段漂移** | 变更记录停在 v0.19，代码已到任务 20+ |
| 6 | **无生产部署物** | 有 `Dockerfile`，但没有 k8s / nginx / systemd 模板 |
| 7 | **无 LICENSE** | 默认"保留所有权利"，企业法务无法放行 |
| 8 | **无可观测性** | 没有 metrics / tracing / 错误上报，线上问题只能翻日志 |
| 9 | **单管理员模型** | `admin_accounts` 是单账号设计，没有多租户与角色体系 |

---

## 设计原则

这个项目里反复出现的几条判断标准，写在最前面免得后来者推翻它们：

**1. 宁可少说，不说错的。**
拿不到 flag 就写"本次没有确认到 flag"，而不是猜一个；证据分级里
`ruled_out` 必须附具体反例，"感觉应该没问题"一律记 `needs_follow_up`。

**2. 失败要说人话，并且给出下一步。**
续跑失败不能只说"会话文件已不在" —— 必须补上"请改用「重跑」"。
用户要的是结果，"不能做"本身不解决问题。

**3. 同一件事实只能有一个出处。**
筛选条件、状态流转表、枚举词汇表都只有一份实现。两份口径迟早漂移，
而漂移的症状（"列表 3 条、角标 7 条"）用户无法解释，只能不再相信界面。

**4. 凭据只走环境变量。**
`.env` / `_uploads/` / `_scan_runs/` 全部不入库，见 `.gitignore` 的分节说明。
`_uploads/` 那条尤其重要：附件是**用户上传的业务数据**（内网截图、抓包），
可能含拓扑甚至演示时粘贴过的令牌。

**5. 注释写"为什么"，不写"是什么"。**
代码本身说清了是什么；注释要留下的是**当时的取舍与踩过的坑**。
所以你会看到大量"⚠️ 这里曾经……"的记录 —— 那是给下一个改这段代码的人看的。

---

## 目录导航

```
suvex-api/
  app/core/       跨领域公共件（依赖方向单向，不依赖任何领域包）
  app/{auth,providers,scans,findings,reports,events,settings,members,audit,overview,ctf2}/
                  按领域拆分，各自 模型/出入参/业务/路由
  alembic/        30 个迁移
  tests/          903 个测试函数，对真实 PostgreSQL 跑
  docs/           8 篇 ADR + 安全清单 + 验收记录
  docker/         CTF2 沙箱镜像

suvex-web/
  src/data/       数据模型与数据源（全部走真实 API）
  src/pages/      页面（Landing 同步引入，其余 lazy 分包）
  src/components/ 布局壳 / 各功能页组件 / 无状态基础件
  src/design-system.md   配色 / 字体 / 间距 / 动效的决策与理由
  docs/api-contract.md   ★ 契约，唯一事实来源
```

各子项目自己的 README 里有更细的说明：
[`suvex-api/README.md`](suvex-api/README.md) ·
[`suvex-web/README.md`](suvex-web/README.md)
