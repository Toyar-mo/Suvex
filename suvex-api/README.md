# Suvex API

Suvex 平台的后端。契约（唯一事实来源）在
[`../suvex-web/docs/api-contract.md`](../suvex-web/docs/api-contract.md) ——
**字段与端点以它为准**，发现遗漏先改文档再改代码。

前端是独立可部署物（`../suvex-web`），两边只通过这份契约耦合。

---

## 交付进度

| 任务 | 内容 | 状态 |
| --- | --- | --- |
| 1 | 脚手架 + PostgreSQL/Alembic + 认证（会话 Cookie / 机器令牌 / argon2id）+ 环境配置 + Compose + SSE 事件流 | ✅ |
| 2 | **LLM Provider：增删改查 + 凭据加密存储 + 真实连通性探测 + 默认唯一** | ✅ |
| 3 | **扫描生命周期：真的拉起 strix 子进程 + 状态机 + 终止（杀进程树）+ 重跑 + 并发上限** | ✅ 编排全通；**真扫描跑不起来**（缺 Docker，见「已知未验证项」第 2 条） |
| 4 | **strix 结构化事件导入（解析层 + 幂等落库 + 收尾补齐）→ 走已有 SSE 推给前端** | ✅ 链路全通；格式来自源码，**未与真实产物对账** |
| 5 | **漏洞持久化 + 状态流转（含操作人留痕与时间线）+ 批量更新 + 统计唯一数据源** | ✅ 全通（含"统计与逐条列表逐档一致"的交叉验证） |
| 6 | **报告生成：预览/生成同一份 IR + 内容冻结（成员/顺序/排除数）+ Markdown 导出 + 历史管理** | ✅ 全通；PDF 走浏览器打印（决策与实测复核见契约 §3.5.1、`docs/adr-007-report-export.md`） |
| 6（补） | **`/overview` 全局统计**（全部派生 + 活动流投影 + 两个 `null` 语义） | ✅ 全通。契约 §9 原本把它排在任务 6，而任务 6 的正式交付物是报告 —— 它的 501 消息因此过期成假话，且当时验收脚本把这个 501 断言成 PASS。详见契约 §3.6 与 v0.8 变更记录 |
| 7 | 设置（沙箱与通知） | ⏳ |

后端任务 1 交付的东西：

| 项 | 状态 |
| --- | --- |
| FastAPI 项目结构，按领域拆分（auth / providers / scans / findings / reports / events / settings / overview） | ✅ |
| PostgreSQL + SQLAlchemy 2.0 + Alembic（**从第一个迁移就版本化**） | ✅ |
| 认证：一次性初始化、登录、会话 Cookie、机器令牌、argon2id 口令哈希 | ✅ |
| `.env` + pydantic-settings；`.env.example` 入库、`.env` 忽略 | ✅ |
| Docker Compose（后端 + PostgreSQL），`docker compose up` 一步起 | ⚠️ 文件已写、**未在本机执行**（见「已知未验证项」） |
| 测试与端到端验收 | ✅ 386 个 pytest 用例 + 114 项验收，全绿；另有报告两端的常量对账脚本 17 项 |

**未实现**的契约端点是**注册了但返回 `501`** 的占位，`message` 里写明计划在哪个
后端任务交付。完整清单见契约 §9。

---

## 快速开始

### 方式 A：本机直接跑（开发用）

需要两样东西：一个 PostgreSQL 实例、一个 Python 3.12 虚拟环境。

```bash
# 1) 依赖。虚拟环境按本机约定放在 E:\Program Files (x86) 下（见下节）
python -m venv "E:\Program Files (x86)\suvex-api-venv"
& "E:\Program Files (x86)\suvex-api-venv\Scripts\python.exe" -m pip install -r requirements-dev.txt

# 2) 配置
cp .env.example .env        # 按需修改；.env 已在 .gitignore 里

# 3) 迁移
& "E:\Program Files (x86)\suvex-api-venv\Scripts\alembic.exe" upgrade head

# 4) 起服务
& "E:\Program Files (x86)\suvex-api-venv\Scripts\uvicorn.exe" app.main:app --reload
```

- API 文档：<http://127.0.0.1:8000/api/v1/docs>
- 健康检查：<http://127.0.0.1:8000/api/v1/health>

### 方式 B：docker compose

```bash
docker compose up --build
```

一步起后端 + 数据库。数据库**不映射到宿主端口**（只在内网可达），
API 只绑到 `127.0.0.1:8000`。

要用 `psql` 调试：

```bash
docker compose exec db psql -U suvex -d suvex
```

### 方式 C：只跑测试

```bash
# 需要一个可连的 PostgreSQL（默认 127.0.0.1:55432，见下节）
& "E:\Program Files (x86)\suvex-api-venv\Scripts\python.exe" -m pytest -q
& "E:\Program Files (x86)\suvex-api-venv\Scripts\python.exe" scripts\verify_acceptance.py
```

---

## 本机 PostgreSQL 实例

开发机上有一个便携版 PostgreSQL 16.4，装在 `E:\Program Files (x86)\PostgreSQL`
（按「本机安装的东西放 E 盘」的约定）。它注册成了一个 Windows 服务，所以能活过
终端会话 —— 早先直接 `Start-Process postgres` 的尝试全部失败，因为

> **PostgreSQL 拒绝以管理员权限的账号运行**
> （`Execution of PostgreSQL by a user with administrative permissions is not permitted.`）。

绕法是专用的非管理员本地账号 `suvexpg` + 把服务跑在它下面，
并给它授予「作为服务登录」权限。

```powershell
sc.exe start suvexpg      # 启动
sc.exe stop  suvexpg      # 停止
Get-Service suvexpg       # 看状态
```

| 项 | 值 |
| --- | --- |
| 端口 | `55432`（刻意避开默认的 5432，防止与将来装的正式实例冲突） |
| 监听 | `127.0.0.1`（只回环） |
| 超级用户 | `suvex` |
| 开发口令 | `suvex_dev_local`（**仅本机开发实例**，不是生产凭据） |
| 数据库 | `suvex`（开发）/ `suvex_test`（测试）/ `suvex_acceptance`（验收） |
| 认证方式 | `scram-sha-256`（已实测错误口令被拒） |
| 日志 | `E:\Program Files (x86)\PostgreSQL\run\postgres.log` |

---

## 目录结构

```
app/
  core/          跨领域公共件（不依赖任何领域包，依赖方向单向）
    config.py      .env → Settings（pydantic-settings）
    db.py          引擎 / 会话 / Base（含约束命名约定）
    security.py    口令哈希、会话令牌、机器令牌、scope 判定
    crypto.py      ★ provider 凭据的 Fernet 加密、掩码、脱敏
    logging.py     日志配置 + 脱敏兜底（Formatter + Filter 两层）
    errors.py      统一错误信封 + 错误码表
    vocab.py       契约词汇表：所有枚举 + 状态流转表（唯一实现处）
    ids.py         ULID 与带前缀主键
    timeutil.py    唯一的「现在」与唯一的时间格式
    schemas.py     camelCase ↔ snake_case 的自动映射基类
    results.py     SaveResult / ActionResult
    ratelimit.py   滑动窗口限流
    bootstrap.py   启动自检（连库 + 查表）
    stubs.py       分期交付的 501 占位
  auth/          认证（模型 / 出入参 / 业务 / 依赖 / 路由）
  providers/     ★ LLM Provider（模型 / 出入参 / 业务 / 路由 / llm.py 探测客户端）
  scans/         扫描（表已建，路由为 501；`/agents` 已实现）
  findings/      漏洞（表已建，路由为 501）
  reports/       报告（表已建，路由为 501）
  events/        实时事件流（SSE，**已实现**）
  settings/      设置（表已建；口令与令牌已实现，其余为 501）
  overview/      全局统计（无表，全部派生；路由为 501）
  health.py      探活
  main.py        应用装配
alembic/         迁移（env.py 从 Settings 读 URL，[详见 README](alembic/README.md)）
tests/           118 个用例，**对真实 PostgreSQL 跑**（不是 SQLite）
scripts/         端到端验收
docs/            决策记录（ADR）
```

---

## 环境变量

全部带 `SUPEX_` 前缀，模板见 [`.env.example`](.env.example)。几个容易错的：

| 变量 | 注意 |
| --- | --- |
| `SUPEX_SECRET_KEY` | ★ provider 凭据的加密密钥。生成：`python -m app.core.crypto`。**必须与数据库一起备份** —— 丢了它，已存的 provider 凭据全部解不开。生产环境缺少它是启动硬错误 |
| `SUPEX_DATABASE_URL` | 本机直跑用 `:55432`；容器里用 `@db:5432`（compose 已覆盖） |
| `SUPEX_COOKIE_SECURE` | 开发态是 http，**必须 false**。开着会表现为「登录成功但下一个请求仍是未登录」 |
| `SUPEX_CORS_ORIGINS` | 带凭据的跨源请求**不能用 `*`**，必须逐个列出具体源 |
| `SUPEX_SCAN_AUTHORIZATION_STATEMENT` | 服务端**逐字校验**前端送来的授权声明，前后端必须一致 |
| `SUPEX_ALLOW_PRIVATE_LLM_ENDPOINTS` | 默认 `true`（本地模型要连 `127.0.0.1:11434`）。置 false 会让本地模型完全不可用；云厂商元数据地址无论此项如何都会被拦 |

生产环境（`SUPEX_ENV=prod`）会在启动时检查并**拒绝**以不安全的配置启动
（未开 `SUPEX_COOKIE_SECURE`）；其余危险配置只打中文警告，不一上来就崩。

---

## 与任务说明的两处偏离（都是刻意的）

### 0. 先说一处**解释**（不是偏离）

任务 2 要求「返回给前端时只回显掩码（如 `sk-****abcd`），不回显完整明文」。
实现里掩码走独立字段 **`apiKeyMask`**，而 `apiKey` 恒为空串。

这不只是"换个字段放"，而是**必须**这样：前端保存时的 payload 拼装是
`apiKey: keyEdited ? draft.apiKey : account.apiKey` —— 没动 key 时会把
`account.apiKey` 原样回传。掩码若放在 `apiKey` 里，用户只改一个显示名点保存，
掩码就会被当成新 key 存下去，**真凭据被静默毁掉且没有任何报错**。

完整论证见 [`docs/adr-003-credential-storage.md`](docs/adr-003-credential-storage.md)。

### 1. 认证用**不透明令牌**，不是 JWT

任务原文要求「JWT（access token + refresh token）」，而契约 §1.2 明确写了
**不要用 JWT**。实现按契约走，理由不是"契约更大"，而是它给的那条是技术事实：

> **本平台凭据泄漏的后果是「替攻击者发包」，不是读到数据。**
> 所以「改密码后旧会话立刻失效」是安全要求，不是体验优化。
> JWT 要做到这一点必须维护一张吊销表 —— 那就已经不是无状态了，
> 等于付了 JWT 的复杂度（密钥轮换、时钟偏移、算法混淆）却没拿到它的好处。

access + refresh 的**语义**保留了：会话 12 小时有效，且每次使用时滚动续期。
令牌格式被封装在 `app/core/security.py`，将来真要换 JWT 只动那两处。

完整论证见 [`docs/adr-001-auth-credentials.md`](docs/adr-001-auth-credentials.md)。

### 2. 实时通道是 **SSE**，所以模块叫 `events` 而不是 `agents_ws`

任务清单里的模块名是 `agents_ws`（WebSocket）。契约 §0.1 已裁决为 SSE，
三条理由：事件流单向、`Last-Event-ID` 补发是内建的、前端代码已经是这个形状。
包名留在 `agents_ws` 会让后来者去找 WebSocket 握手。

见 [`docs/adr-002-realtime-sse.md`](docs/adr-002-realtime-sse.md)。

---

## 前端接线状态（如实说）

**前端目前仍然全部走 mock。** 后端已经为 Settings 页的 provider 表单
逐字对齐了字段（我把 `ProviderPanel.tsx` 与 `settings-store.ts` 读完了，
上面那条"掩码不能放进 `apiKey`"就是在这一步发现的），但**还没有把 store 接到 API 上**。

原因是接线的前置条件还不齐：`lib/api.ts` 要带 `credentials: 'include'`，
而所有请求都会先撞上 401 —— **登录界面还不存在**（契约 §8 的前端工作）。
只接 provider 一家会把 Settings 页变成"一半真一半假"：provider 报 401、
沙箱与通知还是 mock。那比全 mock 更难解释。

接线时要改的地方已经列进契约 §7 的表里，其中 provider 相关四处：

1. 密钥提示改用 `account.apiKeyMask`（它已经是成品掩码，再过一遍
   `secretPreview` 会变成 `••••••••`，反而看不出是哪一把）；
2. 保存时**永不**把 `account.apiKey` 带回服务端（用空串表示"不改凭据"）；
3. `handleReset` 同样要把 `apiKey` 清空；
4. 删默认 provider 会收到 `409 PROVIDER_IS_DEFAULT`，要提示"先去设另一个为默认"
   （与 mock 的行为不同 —— mock 会自动挑替补，后端按契约拒绝）。

---

## 已知未验证项

### 1. `docker compose up --build` 没有在本机执行过

**原因**：这台机器上没有 Docker 引擎。Docker Desktop 依赖 WSL2 或 Hyper-V，
而两者当前都是关闭的：

```
Microsoft-Windows-Subsystem-Linux   Disabled
VirtualMachinePlatform              Disabled
```

启用它们**需要重启**，而那不是我应该替你做的决定。

**已经做了的**（`scripts/verify_acceptance.py` 里的静态校验，全部通过）：

- `docker-compose.yml` 能被 YAML 解析，定义 `db` + `api` 两个服务
- `api` 用 `condition: service_healthy` 等数据库**健康**（不是"已启动"）
- `db` 用 `postgres:16-alpine`，口令可通过 `POSTGRES_PASSWORD` 覆盖
- `api` 的 `SUPEX_DATABASE_URL` 指向 `db:5432`（不是宿主的 55432）
- `db` **未**映射宿主端口；`api` 只绑 `127.0.0.1:8000`
- `Dockerfile` 的启动命令含 `alembic upgrade head`（一步到位）
- `Dockerfile` 以非 root 用户运行
- `.dockerignore` 排除了 `.env`

**没验证到的**：镜像能不能构建成功、容器能不能真的起来、
迁移在容器里能不能跑通、两个容器之间的网络与健康检查时序。

要真正验证，需要启用 WSL2 + 虚拟化平台并重启，然后：

```bash
docker compose up --build
curl http://127.0.0.1:8000/api/v1/health
```

### 2. ★ 一次**真实扫描**没有跑成功过（同一个根因：没有 Docker）

任务 3 的验收要求「提交一次真实扫描能在数据库和 strix 侧都看到对应记录」。
**这一条只做到了一半，如实说明。**

**已经验证到的**（`scripts/verify_acceptance.py` 的「真 strix 集成」一节，
用的是**真的 strix 二进制**，不是替身）：

- strix 装好了并能被执行后端找到：`E:\Program Files (x86)\strix-venv\Scripts\strix.exe`
- 命令拼装正确：`strix --target <target> --non-interactive --scan-mode quick --max-turns 1 --instruction …`
- **凭据不在 argv 里**（只在环境变量里）
- 环境变量注入正确（`STRIX_LLM`、`STRIX_TELEMETRY=0`）
- 真的把 strix 跑起来了，拿到**退出码 1**
- 它自己报告的原因是：

  ```
  +- STRIX --------------------------------------------------+
  |  DOCKER NOT INSTALLED                                    |
  |  The 'docker' CLI was not found in your PATH.            |
  +----------------------------------------------------------+
  ```

**没验证到的**：扫描真正开跑之后的行为 —— `strix_runs/<run_id>/` 目录、
里面的 `events.jsonl`、以及它产生漏洞记录的过程。

> ### ✅ 2026-09-25 更新：这一条已经补上了
>
> 换到一台**有 Docker 的 Kali** 上部署后，真扫描跑通了。观测到的证据：
>
> - `ps` 里能看到真的 strix 子进程在跑：
>   `/root/strix-venv/bin/python3 .../strix --target 127.0.0.1:5000 --non-interactive --scan-mode standard --max-turns 500 --instruction …`
> - Docker 里**为这次扫描现起了一个沙箱容器**（`ghcr.io/usestrix/strix-sandbox:1.3.0`，独立的 `127.0.0.1:32785->48080`）
> - 调度器日志：`已启动 pid=…` → `导入了 2 条外部事件`，事件数持续增长
> - `agentCount` 从 0 涨到 2 —— 这是替身执行后端做不出来的
>
> 所以「扫描编排 + 真 strix 拉起 + 事件导入」这条链路是通的，
> 之前卡住的确实只是宿主机的 Docker，不是编排代码。
>
> ⚠️ 同时暴露了一个**只有换机器才会现形**的缺陷，已修：
> `app/providers/llm.py` 运行时 `import httpx`，但 `httpx` 只写在
> `requirements-dev.txt` 里。开发机上跑过测试就装了 dev 依赖，所以永远
> 不会暴露；而任何"只装 requirements.txt"的部署（Docker 镜像、新机器）
> 都会在**启动时**直接 `ModuleNotFoundError: No module named 'httpx'`，
> 报错位置还在 `providers/llm.py` 的 import 行 —— 看起来像"某个 provider
> 出了问题"，而不是"少装了一个包"。已把它移到 `requirements.txt`。

原因与第 1 条一样：strix 的前置条件是「**Docker (running)**」
（`STRIX_RUNTIME_BACKEND` 默认 `docker`，镜像 `ghcr.io/usestrix/strix-sandbox`），
而这台机器上 WSL2 / VirtualMachinePlatform / Hyper-V 三个都是 `Disabled`，
启用需要重启。

**编排层本身是完整验证过的**：队列、并发上限、状态机、
终止（含"孙进程也被杀掉" —— 用一个持续写心跳的孙进程验证）、
重跑、孤儿回收，全部用 `SUPEX_SCAN_RUNNER=fake` 的替身后端跑通了。
替身**不是 mock**：它同样 spawn 真的子进程（含真孙进程），
只是那个进程不做真实攻击 —— 测试与 CI **绝不能**向真实目标发包。

装好 Docker 之后，同一条命令的预期变化是：`exit=0 或 2`，
`strix_runs/<runId>/` 出现，扫描状态走到 `completed`。**代码不用改。**

### 3. ★ strix 事件格式来自源码，未与真实产物对账

任务 4 要求「先去查它的文档/源码，不要凭空假设格式」。查证结果：
**官方文档写错了** —— 文档说事件在 `strix_runs/<run>/events.jsonl`，
而在 `strix-agent` 1.6.2 源码里全包搜索 `events.jsonl` **零命中**。

真实的结构化事件流在 `strix_runs/<run>/.state/agents.db`（SQLite），
解析层按源码写（表结构、列名含义、三种消息类型、`call_id` 配对规则、
内部轮前缀表 —— 依据逐条记在契约 §4.3.1 与 `app/scans/strix_events.py` 的文件头）。

**已经验证到的**：解析逻辑与源码描述的格式一致（37 个用例，
每条断言都追得到 strix 源码的某一行）；端到端链路通
（替身后端产出**真实结构**的产物 → 解析 → 落库 → API 读回，
五种事件类型都出现了）。

**没验证到的**：真实 strix 写出来的库逐字段是否就是这样。
那需要一次真实运行，而本机没有 Docker。所以：

- `discovery` 事件的 `subject` / `evidence` / `severity` 三个字段是
  **启发式**取值（按"名字最可能对得上"猜的），**取不到 `severity` 时
  不放这个键**而不是编一个默认值；
- 装好 Docker 之后第一件该做的事：跑一次真实扫描，
  对着 `vulnerabilities.json` 与 `agents.db` 核对这三个字段名。

另外，契约的 8 种事件类型里有 **4 种在 strix 侧没有对应来源**
（`poc_generated` / `verified` / `blocked` / `completed`）。
其中 `completed` 由扫描生命周期发出，另三种外部工具根本没有对应的结构化信号 ——
如实记在这里，免得后来者以为是漏了。

### strix 环境（本机）

```powershell
E:\Program Files (x86)\strix-venv\Scripts\strix.exe --help
```

装在**独立 venv** 里，与 API 的依赖隔离。理由不是洁癖：
`pip install strix-agent` 会拉进 80 多个包
（litellm、openai-agents、docker、boto3、reportlab、huggingface-hub……），
装进 API 的 venv 之后，一次 strix 升级就可能改掉 API 用的
pydantic / httpx 版本 —— 而那种问题会以"API 某个接口突然 500"的形式出现，
排查方向完全不同。

配置项 `SUPEX_STRIX_COMMAND` 指向上面的可执行文件；不设就按 PATH 找 `strix`。

---

## 安全要点（实现时逐条落实的）

| 项 | 做法 | 为什么 |
| --- | --- | --- |
| 口令哈希 | argon2id（`argon2-cffi`） | bcrypt 有 72 字节静默截断，且需自己处理盐与 cost |
| **provider 凭据** | **Fernet 加密落库**，密钥来自独立的 `SUPEX_SECRET_KEY`；掩码另存一列使读路径不必解密 | `api_key` 必须可还原（要拿去调 LLM），所以不能哈希；而"读接口不碰明文"要能被验证 |
| **凭据回显** | `apiKey` 恒为空串 + `hasKey` + `apiKeyMask` 三字段分工 | 掩码放进 `apiKey` 会被前端原样回传、覆盖真凭据 —— 静默数据损坏，见 [ADR-003](docs/adr-003-credential-storage.md) |
| **日志无明文** | 无请求体日志中间件（已审计）；外加 Formatter + Filter 两层正则脱敏兜底 | 有测试在 `Logger._log` 处拦截所有日志消息；验收脚本再打一遍真实 uvicorn 的日志 |
| 会话 Cookie | `HttpOnly` + `SameSite=Strict` + `Max-Age` + 生产加 `Secure` | 页面会渲染**不可信文本**（`detail.evidence`、`poc.code`），令牌放 localStorage 等于送给 XSS |
| CSRF | `SameSite=Strict` + 服务端 Origin 校验 + 拒绝表单编码的写请求 | 判定不能只依赖浏览器实现 |
| 令牌存储 | 只存 SHA-256 摘要；明文只在创建响应出现一次 | 「能再看一次」等于「它以可逆形式存着」 |
| 登录限流 | 滑动窗口 5 次/分钟 + 15 次/小时，与 `/auth/setup` 共用计数桶 | 口令是唯一凭据，不分桶则交替打两个端点能把尝试次数翻倍 |
| 时序攻击 | 账号不存在时也跑一次完整的 argon2 校验（用固定的假哈希） | 否则响应时间会泄露"这个系统初始化了没有" |
| 来源 IP | **不读 `X-Forwarded-For`** | 它可被客户端伪造，而它同时用于限流 key 与授权留痕 |
| 启动自检 | `assert_bootable()` 只拦"确定不安全"的组合 | prod + 未开 Secure Cookie 属于"配错了却不报错"的那类 |
| 镜像 | 非 root 用户运行；`.dockerignore` 排除 `.env` | `COPY . .` 会把凭据打进镜像层，那层永远删不掉 |

---

## 复现验收

```bash
& "E:\Program Files (x86)\suvex-api-venv\Scripts\python.exe" -m pytest -q
& "E:\Program Files (x86)\suvex-api-venv\Scripts\python.exe" scripts\verify_acceptance.py
```

验收脚本会**建一个独立的 `suvex_acceptance` 库、真的起一个 uvicorn 子进程**，
然后用 HTTP 打它。其中几条只有在这个层面才做得出来的：

* 一条**真实的 SSE 长连接**：连上之后写一条新事件，等它自己推过来；
* 起一个**真的本地假 LLM 服务**（`http.server`），让
  `POST /providers/{id}/verify` 对它发一次真实 HTTP 请求 —— 证明探测链路
  （URL 构造 → 请求头 → 响应解析 → 落库）真的通，而不是"函数被调用了"；
* 打一遍**服务端进程的真实日志**（含 uvicorn 访问日志），确认明文 API key
  不在里面（第一次跑时这里是对着空字符串断言，已修）。

上一次的运行结果存档在 [`docs/verification-report.md`](docs/verification-report.md)。
