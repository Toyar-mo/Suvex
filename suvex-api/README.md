# Suvex API

Suvex 平台的后端。契约（**唯一事实来源**）在
[`../suvex-web/docs/api-contract.md`](../suvex-web/docs/api-contract.md) ——
字段与端点以它为准，**发现遗漏先改文档再改代码**。

前端是独立可部署物（`../suvex-web`），两边只通过这份契约耦合。
两者放在**同一个仓库**里，理由见根目录 `.gitignore` 的文件头。

---

## 这个后端现在是什么状态

| 项 | 实际数字 |
| --- | --- |
| 代码规模 | **47,622 行** Python（`app/` 下 118 个文件，13 个领域模块） |
| 路由 | **115 个端点**（按 `@router.*` 装饰器计） |
| 数据库迁移 | **36 个** Alembic 版本，从第一个迁移就版本化 |
| 测试 | **75 个测试文件 / 1,352 个测试函数**（参数化展开后更多），对**真实 PostgreSQL** 跑 |
| 契约文档 | **3,741 行**，含变更记录与 8 篇 ADR |
| 仍未实现 | **0 个端点返回 `501`** —— 最后六个已在 v0.22 清零 |

> ⚠️ **这些数字都是量出来的，不是估的**（2026-10-17 全部重核，`wc -l` 口径）。
> 核实命令：
>
> ```bash
> # 代码规模：只数 app/ 下的 .py 总行数（含空行与注释 —— 口径要写清，
> # 否则"3 万行"和"4 万行"能同时为真）
> find app -name '*.py' | xargs wc -l | tail -1
>
> # 端点：按装饰器计，不按路由表（路由表会把同一端点的多个方法拆开）
> grep -rE '@router\.(get|post|put|patch|delete)' app | wc -l
>
> # 迁移
> ls alembic/versions/*.py | grep -v __init__ | wc -l
>
> # 用例：pytest 自己报的逐文件数字，累加
> pytest --collect-only -q | grep -E '^tests/.*: [0-9]+$' \
>   | awk -F': ' '{s+=$2} END {print s}'
> ```
>
> **为什么把口径写出来**：这份 README 早先写过「32,483 行」，而同一时刻
> `app/` 实际是 39,163 行 —— 那个数字对不上任何口径，是估的。
> 一份"看起来精确"的错数字比"大约四万行"更有害：它让人以为核实过。


### 曾经的 6 个 `501` 已全部清零（v0.22）

历史上有六个端点注册但返回 `501`（"还没做"）。**它们现在都实现了**：

| 端点 | 当时的理由 | 现状 |
| --- | --- | --- |
| `GET /settings` | 契约要求"一次返回四块"，缺三块却回 200 会让调用方误以为拿全了 | ✅ 四块齐了，返回 200 |
| `PUT /settings/notices/rule` | 通知规则 | ✅ 已实现 |
| `POST /settings/notices/channels` | 新增通知渠道 | ✅ 已实现 |
| `PATCH /settings/notices/channels/{id}` | 修改通知渠道 | ✅ 已实现（改 `target` 会重置 `verified`） |
| `DELETE /settings/notices/channels/{id}` | 删除通知渠道 | ✅ 已实现 |
| `POST /settings/notices/channels/{id}/test` | 发测试通知 | ✅ 已实现（**只校验地址形状，不真的发送** —— 见下） |

> ⚠️ **`/test` 端点的诚实边界**：它只校验地址形状并写一条 `verifyError`，
> **不会真的发出通知**。伪造一个"发送成功"是禁止的 —— 那会让用户以为
> 通知链路是通的，而实际没有。

> **为什么当时选择"注册但返回 501"而不是不注册（404）**：404 的语义是
> 「这个资源不存在」，而真实情况是「端点还没做」—— 调用方该做的处理完全
> 不同。这条取舍在实现之后仍然成立，所以留在这里作为设计记录。

### 一条需要说清的历史

这份 README 曾经写着「scans / findings / reports 路由为 501」「386 个用例」
「前端全部走 mock」——**那些都过期了**。它停在任务 7，而代码已经走到
任务 20+（CTF2 判题引擎、题目附件、夺标判据、`service` 目标形态…）。

留这段不是自责，是提醒：**这份 README 的数字要靠命令复核，不要靠记忆**。
上面那张表就是照着下面这几条命令逐条核出来的（2026-10-13 全部重核）：

```bash
# 复核上面那张表（在仓库根跑）
find suvex-api/app -name '*.py' | xargs wc -l | tail -1        # 代码规模（含空行注释）
grep -rE '@router\.(get|post|put|patch|delete)' suvex-api/app | wc -l   # 路由数
ls suvex-api/alembic/versions/*.py | grep -v __init__ | wc -l  # 迁移数
ls suvex-api/tests/**/test_*.py suvex-api/tests/test_*.py 2>/dev/null | sort -u | wc -l
grep -rc "^def test_\|^    def test_" suvex-api/tests --include='test_*.py' \
  | awk -F: '{s+=$2} END {print "测试函数:", s}'
grep -rn "pending(_TASK" suvex-api/app/settings/router.py       # 还有几个 501
wc -l suvex-web/docs/api-contract.md
```

> ⚠️ **口径必须写出来**。"代码规模"至少有两种量法，差一万行：
>
> | 口径 | 命令 | 结果（2026-10-17） |
> | --- | --- | --- |
> | 文件总行数（含空行与注释） | `find app -name '*.py' \| xargs wc -l` | **47,622** |
> | 只算非空非纯注释行 | 逐行过滤 | **31,077** |
>
> 上面表里用的是**第一种**。这份 README 早先写过「32,483 行」，
> 而同一时刻 `app/` 实际是 39,163 行 —— 那个数字对不上**任何**口径。
> 一份"看起来精确"却对不上的数字，比"大约四万行"更有害：
> 它让人以为已经核实过了。


> ⚠️ 写这段时我自己就先踩了一次：第一版把测试函数数写成 **650**、
> 契约行数写成 **2,930** —— 前者漏数了 `tests/ctf2/` 子目录，
> 后者是把字节数当成了行数。**两个数字都是"看起来很像"的那种错**，
> 所以上面那几条命令必须真的跑一遍，不能凭印象填。

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
#    ★ SUPEX_SECRET_KEY 必须自己生成：python -m app.core.crypto

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

> ⚠️ **测试必须串行跑。** 所有测试进程共用同一个 `suvex_test` 库，
> 而每个用例开头会 `TRUNCATE` 一遍。并发跑（`-n auto`）会让它们互相
> 清掉对方的数据，症状是随机的、看起来毫不相关的失败。
> 也别在跑测试时 kill 进程 —— 残留数据会让下一次跑出现
> 一个假的 `409 SETUP_ALREADY_DONE`。

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
  scans/         扫描生命周期、strix 编排、事件导入、题解与 flag 提取
  findings/      漏洞持久化、状态流转、SLA、分组、附件、软删除
  reports/       报告生成与冻结快照
  events/        实时事件流（SSE）
  settings/      设置（沙箱与执行三件套、令牌、保留规则、SLA、我的视图）
  members/       处理人名单
  audit/         审计日志
  overview/      全局统计（无表，全部派生）
  ctf2/          CTF2 判题引擎（赛事同步、沙箱解题、候选提交）
  health.py      探活
  main.py        应用装配
alembic/         迁移（env.py 从 Settings 读 URL，[详见 README](alembic/README.md)）
tests/           75 个测试文件 / 1,352 个测试函数，**对真实 PostgreSQL 跑**（不是 SQLite）
scripts/         端到端验收与对账脚本
docs/            决策记录（8 篇 ADR）+ 安全清单 + 验收记录
docker/          CTF2 沙箱镜像的 Dockerfile
deploy/          systemd unit 与 CTF2 沙箱出站白名单脚本
prompts/         agent 系统提示词
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

容量相关的几项（**生产部署前要算一遍**，详见 [`deploy/README.md`](deploy/README.md) 第十一、十二节）：

| 变量 | 默认 | 注意 |
| --- | --- | --- |
| `SUPEX_DB_POOL_SIZE` | 5 | 每个进程的常驻连接数 |
| `SUPEX_DB_MAX_OVERFLOW` | 10 | 峰值时可临时多开的连接 |
| `SUPEX_MAX_CONCURRENT_SCANS` | 3 | 同时跑的扫描数，**每个会起一个沙箱容器** |
| `SUPEX_SCAN_ROOT` | 项目根 | 扫描产物与附件的落盘根，**建议单独挂一块盘** |

> ⚠️ **连接数是乘法，不是加法。** 若 uvicorn 开了多 worker
> （`--workers 4`），占用是 `4 × (5 + 10) = 60` 个连接。PostgreSQL
> 默认 `max_connections = 100`，再叠加别的客户端就会打满。
> 规则：`worker 数 × (POOL_SIZE + MAX_OVERFLOW) < max_connections`。
>
> ⚠️ **并发扫描数与内存**：每个沙箱容器默认 4 核 / 4096 MB（设置页可改）。
> `MAX_CONCURRENT_SCANS=3` 意味着峰值约 12 GB 内存 —— 按机器实际内存调。
>
> ⚠️ **`SUPEX_SCAN_ROOT` 只增不减**：扫描产物目前**没有自动清理**
> （见根 README 的缺口清单里「扫描产物目录只增不减」那一条）。清理命令与"哪些目录绝不能删"
> 写在 `deploy/README.md` 第十一节。

生产环境（`SUPEX_ENV=prod`）会在启动时检查并**拒绝**以不安全的配置启动
（未开 `SUPEX_COOKIE_SECURE`）；其余危险配置只打中文警告，不一上来就崩。

---

## 与任务说明的偏离（都是刻意的）

### 0. 先说一处**解释**（不是偏离）

任务要求「返回给前端时只回显掩码（如 `sk-****abcd`），不回显完整明文」。
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

**前端已经全部接真实接口，不再有 mock 数据文件。**

`src/data/` 下的 store 都走 `lib/api.ts`（带 `credentials: 'include'`），
登录界面、错误边界、主题切换都已就位。契约 §7 里列的接线清单已完成。

早期那份 README 写着"前端仍全部走 mock"，那是**任务 7 之前**的状态。

---

## 已知未验证项

### 1. `docker compose` 已实测，但**镜像跑不了扫描**

**2026-10-10 在真机上跑通了**（一台有 Docker 的 Kali，`docker-compose` 2.40.3）：

| 项 | 结果 |
| --- | --- |
| 镜像构建 | ✅ 成功 |
| 两个容器启动 | ✅ `db` 与 `api` 都 `healthy` |
| `db` 健康检查时序 | ✅ `api` 正确等到 `db` 健康才启动 |
| 容器内迁移 | ✅ `alembic upgrade head` 跑通（28 张表） |
| `/health` | ✅ `{"status":"ok","version":"0.1.0"}` |
| `/metrics` | ✅ `database_up 1`、请求计数正常 |
| 数据库不映射宿主端口 | ✅ 只有 `5432/tcp`（容器内），无 `ports:` |

**⚠️ 但实测暴露了一个之前没人知道的边界：这个镜像跑不了扫描。**

容器里**既没有 `strix`，也没有 `docker` CLI**：

```console
$ docker exec suvextest-api-1 sh -c "which strix docker"
（无输出）
```

于是 `POST /scans` 能受理，但扫描**立即失败**，日志里是：

```
扫描执行后端不可用，新的扫描会立即失败：
找不到 strix 可执行文件（SUPEX_STRIX_COMMAND='strix'）。
```

**这是刻意的取舍，不是缺陷**（理由写在 `Dockerfile` 头部）：
装 strix 会带进 80 多个包（litellm、openai-agents、docker、boto3…），
而大多数用这个镜像的场景不需要执行后端。要跑扫描，两条路：

1. **宿主机部署**（推荐，也是当前 Kali 上在跑的方式）——
   systemd + venv，见 [`deploy/README.md`](deploy/README.md)；
2. 给镜像补执行后端（装 docker CLI + 挂宿主 socket + 装 strix）——
   但挂 docker socket **等于给容器 root**，那是知情后的选择。

**所以：`docker compose up` 现在是一个"控制面"部署**（看界面、
管理历史、CTF2 平台同步都能用），**不是完整的执行环境**。

> ⚠️ 另一处如实说明：上面那次实测用的是 `docker-compose`（连字符版，
> Debian 的 apt 包），不是 `docker compose`（插件版）。
> 两者读同一份 `docker-compose.yml`，但**在没装插件的机器上
> `docker compose up` 会报 `unknown command: compose`** ——
> 而那条错误信息看起来像"compose 文件有问题"。

> ✅ **2026-10-10 更新：真扫描与 Docker 都已在另一台机器上验证过。**
>
> 换到一台**有 Docker 的 Kali**（`192.168.44.128`）上部署后：
>
> - 真扫描跑通，`ps` 里能看到真的 strix 子进程；
> - Docker 为每次扫描**现起一个沙箱容器**（`ghcr.io/usestrix/strix-sandbox:1.3.0`）；
> - 事件持续导入（单次扫描观测到 **936 条**事件），`agentCount` 从 0 涨上去；
> - CTF2 判题引擎实际解出过题目，`scans.flag` 落库
>   （例如 `CTF2{e1775a04-…}`）。
>
> 所以「扫描编排 + 真 strix 拉起 + 事件导入 + 夺旗」这条链路是通的，
> 之前卡住的确实只是宿主机的 Docker，不是编排代码。
>
> ⚠️ 同一批部署还暴露过一个**只有换机器才会现形**的缺陷（已修）：
> `app/providers/llm.py` 运行时 `import httpx`，但 `httpx` 只写在
> `requirements-dev.txt` 里。开发机上跑过测试就装了 dev 依赖，所以永远
> 不会暴露；而任何"只装 requirements.txt"的部署都会在**启动时**直接
> `ModuleNotFoundError: No module named 'httpx'`，报错位置还在
> `providers/llm.py` 的 import 行 —— 看起来像"某个 provider 出了问题"，
> 而不是"少装了一个包"。已把它移到 `requirements.txt`。
>
> **但 `docker compose` 这条路仍未在容器里实测过** —— Kali 上是
> systemd + 本机 venv 部署的，不是 compose。上面那条"没验证到的"
> 依旧成立。

> ✅ **2026-10-18 更新：CTF2 解题沙箱对 web 类题放开了网络。**
>
> 起因是一道题（`[BJDCTF 2nd]文件探测`）在 `--network none` 的沙箱里跑了
> **76 轮 / 1857 秒**，手工试了 40 多个路径全落空，模型
> 最后一句写的是"沙箱无外网，无法用 shell 侧做字典爆破，只能靠
> `http_request` 逐个 URL 请求"。
>
> 但查过镜像才发现：`gobuster` / `ffuf` / `wfuzz` / `nmap` / `sqlmap` /
> `curl` 全都在 `ctf2-sandbox` 里，连字典都有
> （`/usr/share/dirb/wordlists/`，`common.txt` 4600 词、`big.txt` 2 万词）。
> **工具齐全，是网络被关掉了** —— 原设计只给 pwn/网络类开 raw network，
> `needs_raw_network()` 的分类集合里没有 `web`。
>
> 改动见提交 `289b37b`：分类集合加上 `web`、首轮提示补上"沙箱有网 + 怎么
> 用 gobuster"、系统提示词把"目录探测"从"远端爆破"里摘出来
> （**找文件名可以，找口令不行**）。
>
> 效果（同一道题、同一个模型的对比）：
>
> | | 放开前 | 放开后 |
> | --- | --- | --- |
> | 轮数 | 76 | **55** |
> | token | 3,642,761 | **2,003,706** |
> | 结果 | 未解出 | **解出（平台 verified）** |
>
> token 降了 45%，而且解出来了。工具调用也从"每回合逐条
> `http_request` 盲试"变成"一回合跑几十个变体
> （`for f in ...; do curl ...; done`）"。
>
> ⚠️ 安全边界不在代码里，在 `DOCKER-USER` 链上一组 iptables 规则
> （已做成 systemd unit 开机自启，见 `deploy/ctf2-egress-rules.sh` 与
> `deploy/ctf2-egress.service`）。放行**两条**网段，缺一不可：
>
> - `117.21.200.0/24` —— 靶机真实 IP，容器内从 `/etc/hosts` 解析到它；
> - `198.18.0.0/15` —— dasctf 代理网段，宿主 DNS 把靶站域名解析到这里。
>
> 只放后者时容器里仍然超时（这个坑踩过一次，模型会以为是"没网"然后去
> 扫网关找代理，白烧十几个回合）。
>
> 已知局限：**挡不住"出公网"** —— Kali 的 DNS 把 `www.baidu.com` 也解析
> 到 `198.18.0.36`，与靶站同网段，IP 层面分不开。内网与宿主主机是真挡住
> 的部分。要收紧成"只准连本题靶站"，就把上面前一条换成运行时从
> `/etc/hosts` 读本次靶站 IP。

### 2. strix 事件格式：语义映射里有**启发式**取值

任务要求「先去查它的文档/源码，不要凭空假设格式」。查证结果：
**官方文档写错了** —— 文档说事件在 `strix_runs/<run>/events.jsonl`，
而在 `strix-agent` 1.6.2 源码里全包搜索 `events.jsonl` **零命中**。

真实的结构化事件流在 `strix_runs/<run>/.state/agents.db`（SQLite），
解析层按源码写（表结构、列名含义、三种消息类型、`call_id` 配对规则、
内部轮前缀表 —— 依据逐条记在契约 §4.3.1 与 `app/scans/strix_events.py` 的文件头）。

**确证的部分**：`discovery` 事件由 `tool_name == "create_vulnerability_report"`
成功触发，这是从源码读出来的，不是猜的。

**仍是启发式的部分**（`strix_events.py` 第 62 行起有清单）：
`subject` / `evidence` / `severity` 三个字段按"名字最可能对得上"取值；
取不到 `severity` 时**不放这个键**而不是编一个默认值。

另外，契约的 8 种事件类型里，**strix 流能提供 6 种**，`completed` 由生命周期发，
`blocked` 在 strix 流里**没有来源**：

| 类型 | 来源 |
| --- | --- |
| `tool_call` / `failed` | 工具调用成功 / 失败结束，直接对应 |
| `reasoning` | `think` 工具（确证）+ assistant 发言（近似，用 `detail.intent` 区分） |
| `discovery` | `create_vulnerability_report` / `create_dependency_report` 成功 |
| `poc_generated` | `update_vulnerability_report` 成功且**顶层**参数 `poc_script_code` 有值 |
| `verified` | 同上，且**顶层**参数 `confidence == "high"` |
| `completed` | 扫描生命周期发出（ADR-004），不需要 strix 提供 |
| `blocked` | ❌ **strix 流里无来源**（但本项目生命周期会发它，见下） |

`poc_generated` / `verified` 两种曾长期被记为"strix 侧没有对应信号"，
后来在 strix 源码里找到了；但**此前把它们记在 `fields` 参数上是错的**
（那个参数不存在，见下）。`blocked` 一度被记为挂在 `wait_for_user` 上，
而**那个工具在 1.6.2 里根本不存在**，现已如实退回"strix 流里无来源"。
映射依据与"为什么 `verified` 挂在 `update_` 而不是 `create_` 上"
记在 `strix_events.py` 的模块 docstring 里。

> ⚠️ **"strix 流里无来源" ≠ "这个事件类型没人发"。** 契约的 `blocked`
> 由**本项目自己的生命周期**发出，有三个真实发出点，都不依赖 strix：
> `app/scans/service.py:1387`（用户终止扫描）、
> `app/scans/scheduler.py:809`（服务重启回收，活着 → `blocked` / 已死 → `failed`）、
> `app/ctf2/events.py:431`（CTF2 平台判 flag 错 —— 受阻但可继续，故意不用
> `failed`，否则"猜错 flag"这个常态会让每题都闪一次红）。
> 所以"strix 没给这个信号"与"这个类型是空的"是两件事，别混成一句。

> ⚠️ **参数名必须以 `params_json_schema` 为准。** 2026-10-16 在
> `strix-agent` 1.6.2 上核对：`update_vulnerability_report` 的
> `required = ['report_id','update_reason']`，顶层 26 个 properties 里
> `poc_script_code` / `confidence` 都在顶层，**没有 `fields`**
> （那是内部 `_do_update` 的形参）。读错参数名的代价是**静默的**：
> 分支永远命不中，事件一路掉进 `tool_call` 兜底，而测试因为替身
> 也按同一个错误假设造数据而一直是绿的。

### 3. 契约文档与代码之间有一段**已知漂移**

契约的变更记录停在 **v0.19（任务 12）**，而代码已走到任务 20+。
中间那批（CTF2 判题引擎、题目附件、夺旗判据、`service` 目标形态）
改了契约面却没有回写。

2026-10-10 补齐了与当轮改动直接相关的四项（`targetKind` 的 `service` 与
`artifact`、`skills` 的 `protocol`、`Scan.flag`、续跑与题解两节 + 两个错误码），
**其余待专项对账**。

> ⚠️ 顺带发现：契约校验脚本（`suvex-web/scripts/contract-parity.mjs`）
> 按**子串**在整个文档里找枚举值，而 `service` 这个词在别处出现过，
> 所以这个缺失**一直没被报出来**。这是该校验的已知弱点。

### 4. 跨题方法卡：**机制已实现；五轮重复后，「少兜圈子」这个效应没能站住（n=1 的正向信号被推翻）**

CTF2 现在会把每次解题提炼成一张**方法卡**（`app/ctf2/notes.py`），
下次遇到同类题时召回进 prompt —— 目标是"同类题第二次遇到不必从零摸索"。

| 项 | 状态 |
| --- | --- |
| 提炼 / 召回 / 注入 | ✅ 已实现，**35 张**卡已用真实 transcript 回填（2026-10-18 复查，此前文档写 29 张） |
| 注入内容的安全性 | ✅ 实测：**35 张方法卡**里 flag 形状 **0 处**（2026-10-13 首测 29 张，2026-10-18 随 `35c87da` 复测 35 张） |
| 脱敏是否毁掉卡片知识 | ✅ 实测（2026-10-18）：**35 张 0 处残骸**；修复前 mock 十题学的卡里 2 张被砍断 |
| **"用卡 vs 不用卡"的对照** | ❌ **n=5 轮/臂（2026-10-18，九题）**：解出率 **45/45 vs 45/45**；剔除内置对照题后**回合合计差 0（p=1.000）**、工具调用 **+4.2%（p=0.445）** ⇒ **测不出效果**。早先 n=1 的 −21% “正向信号”已被推翻（那次跑在有环境 bug 的版本上） |

> ⚠️ **上面那个"0 处"的分母要说清**。它测的是**方法卡**（会被注入到
> **另一道题**的 prompt —— 那才是泄漏路径）。
>
> 顺带查了题面注入：131 道题里有 **2 处** flag 形状，都是题目作者写的
> 占位符（`flag{XXX}`、`flag{...}`），**不是真答案**，也不构成泄漏
> （题面本来就该原样给模型看）。但"全库 0 处"这种说法是不严谨的 ——
> 一份没有分母的"0 处"，读的人会以为覆盖了所有内容。

#### ★ 干净重跑：五轮/臂，**n=1 的正向信号没能站住**

上一小节的 n=1 结论是"工具调用 −21%"。补到**每组 5 轮**后，结论反过来了。

**为什么必须重跑**：n=1 那次用的 commit 是 `4cb0339`，而当时 mock 给每道
非 pwn 题都广告了一条 `isProxy` 的代理靶机（把 `9999` 映射到 `31001`）。
`31001` 在真 pwn 靶机启用（`78e003f`）后变成"接受连接但永不回 HTTP 响应"，
而 `_tool_http_request` 的超时是 **60 秒**（`app/ctf2/runner.py:3309`），
模型每用 `http_request` 探一次就烧满一分钟。`#1003` 那次 33 次工具调用
是干净基线（9.6 次）的 **3.4 倍** —— 那批数字混着环境摩擦，不是方法卡。

修复在 `ad0994e`（代理端口改 `31099`），之后重跑：

- 题目：九题（`#1002`–`#1010`）。**排除 `#1001`**：它历史上五次观测是
  16✓/18✓/20✗/26✓/25✗，是单题预算边界的掷硬币，拿它跑 5 轮只会烧时间
  并往均值灌噪声。
- 两臂各 5 轮，每轮**重启 mock**（`hasSolved` 在进程内存，否则第 2 轮起
  全判"该答案已提交过"），每轮独立输出目录。
- 臂 A `notes_injected=0`（五轮全 0，确认真没注入）；
  臂 B 注入 n=1 那批卡合并后的 9 张，`notes_injected=8`（见下"内置对照"）。
- 同一模型 `gzd/deepseek-v4.1-flash`，`max-turns=30`、`timeout=1800`。

| 指标 | 臂 A（无卡） | 臂 B（带卡） | 差 |
| --- | --- | --- | --- |
| 解出 | **45/45** | **45/45** | 无翻转 |
| 回合合计 | 222 | 204 | −8.1% |
| 工具调用合计 | 286 | 270 | −5.6% |

**那两个合计数不能直接读** —— 见下。

##### 内置对照题 `#1004`：−18 回合全部来自它

方法卡召回规则是"**同分类、排除当前题**"（`notes.notes_for_challenge`）。
九题里 `#1004` 是**唯一**的 Web 题，排除自己之后 Web 类没有卡可用 ⇒
它两臂都注入 **0 字符**，条件完全相同。而它正好是聚合差异的全部来源：

| | 回合合计 A | 回合合计 B | 差 |
| --- | --- | --- | --- |
| `#1004`（两臂都 0 张卡） | 51 | 33 | **−18** |
| 其余 8 题合计 | 171 | 171 | **0** |
| 全部九题 | 222 | 204 | −18 |

**其余 8 题合计差正好为 0。** 整体那个 −8.1% 是把一道**没注入卡的题**的
随机波动记到了方法卡头上。`#1004` 单题的两臂样本是
`[7,7,12,17,8]` vs `[4,5,5,4,15]` —— 题内极差 10 和 11，均值差 −3.6，
**精确置换检验 p=0.262**（5v5 枚举 252 种分组）。这点差异用随机分组
就能造出来。

##### 剔除对照题后的正式检验（8 题 × 5 轮 = 40 个观测/臂）

| 指标 | 臂 A 均值 | 臂 B 均值 | 差 | 置换检验 |
| --- | --- | --- | --- | --- |
| 回合 | 4.275 | 4.275 | **+0.0%** | **p = 1.000** |
| 工具调用 | 5.300 | 5.525 | **+4.2%** | p = 0.445 |

逐题中位回合的方向：B 更少 1 题（`#1003`）、B 更多 1 题（`#1005`）、
**其余 6 题完全相同**。工具调用上 B 反而略多。

##### n=1 那个"可定位到具体动作"的证据，n=5 下不成立

| 题 | n=1 时 | n=5 时 |
| --- | --- | --- |
| `#1007` 工具 | 13 → 5（−62%） | `[6,6,7,4,3]` → `[10,5,5,5,3]`，均值 5.2 → 5.6（**+7.7%**，p=0.897） |
| `#1003` 工具 | 33 → 17（−48%） | `[10,9,8,10,11]` → `[7,8,10,9,9]`，均值 9.6 → 8.6（−10.4%，p=0.310） |

n=1 那次 `#1007` 的"臂 A 把 `ls`/`file`/`strings` 发了 4 遍、臂 B 2 遍
就往下走"是真实存在的现象，但它**只在那一轮出现**。逐字逐条对比两臂
`#1008` 的 r1 transcript 更清楚：两臂都是 **3 条命令、3 个回合**，差别
只是臂 A 用 `ls && cat`、臂 B 用 `ls && file && wc -c` —— 同样是 3 条。

##### 耗时列这次仍然不用

两臂合计 1992s → 1690s（中位 14.7s → 12.4s），看着也是改善。但逐题极差
大到没有分辨力：`#1004` 的 A 臂极差 **264s**、B 臂 **396s**，`#1003` 的
A 臂极差 **304s**（单题最长 304s、最短 0s 量级）。聚合出来的"改善"
同样主要来自 `#1004`。**回合数由对话轮次决定、不受宿主负载影响**，
所以只报回合数与工具数。

##### 这次实验的净结论

- **解出率**：无区分度（两臂都满分）。这不能说明卡有用，只能说明
  九道 mock 题本来就在模型能力范围内。
- **过程指标**：`n=5` 下测不出方法卡的效果。回合与工具调用都没有超出
  随机波动的变化。
- **⚠️ 这不等于"方法卡没用"**。它说明的是**这套测试测不出它的作用**：
  九道 mock 题都是 3–7 回合就结束的短题，模型本来就不怎么兜圈子
  （干净基线平均 4.3 回合、5.3 次工具调用）。一个"减少重复探索"的机制，
  在**本来就没有多少重复探索**的题上没有发挥空间。要测它需要
  **长题**（几十回合、会反复试错的那种），而那种题又会撞上单题预算。
- **n=1 那次的正向信号已作废**，因为它跑在有环境 bug 的版本上。留着它
  当结论会误导 —— 这正是"每次改完环境都要重新建立基线"的例子。

#### 第二次 A/B 对照（2026-10-18，十道题 × 两臂）—— **已被上一小节推翻，保留作记录**

这次不是单题，是**整套 mock 十道题**（Web/Crypto/Pwn/Reverse/Misc 各 2 道），
每臂 `max-turns=30`、`timeout=1800`、同一模型（`gzd/deepseek-v4.1-flash`）、
同一 commit（`4cb0339`）。**唯一变量是 `spec.notes_text`**。

臂 A 跑完十题后，用 `scripts/ctf2_baseline.py learn` 从**臂 A 自己的
transcript** 提炼出 10 张卡（含 1 张"没解出来"的），臂 B 把这些卡注入
同类题。两臂之间**重启 mock**（它把 `hasSolved` 存进程内存，不重启的话
臂 B 会全被判"该答案已提交过"，那时比的是 mock 状态不是方法卡）。

| 指标 | 臂 A（关） | 臂 B（开） | 变化 |
| --- | --- | --- | --- |
| 解出 | 9/10 | 9/10 | 不变 |
| 由败转成 / 由成转败 | — | 0 / 0 | 无题翻转 |
| **工具调用总数** | **114** | **90** | **−21%** |
| 通过题平均耗时 | 75.3s | 60.6s | **−19.5%** |
| 通过题平均回合 | 4.56 | 4.56 | 完全相同 |
| 卡注入覆盖 | 0/10 | **10/10** | 召回全命中 |

**怎么读这张表**（别过度解读）：

- **解出率没有区分度**：两臂都只差 `#1001`，而且差的还是同一道题。
- **★ 正向信号在"过程"上，且能定位到具体动作**：
  `#1007 rev-crackme` 臂 A **13 次**工具调用、臂 B **5 次**。
  看 transcript：**臂 A 在第 1 回合把 `ls`/`file`/`strings` 发了 4 遍
  近乎一样的命令**（`ls -la && file && checksec && command -v …`、
  `file && checksec && strings -a`、`ls -la; file; command -v; ls -l`、
  `strings -a | head -60`、`file && strings -a`），臂 B **2 遍就往下走了**
  ——因为卡里写着这一类题的关键步骤就是 `file` + `checksec` + `strings`。
  `#1003 pwn-ret2win` 同理（33 → 17 次）。
  这正是方法卡**应该**起的作用：**不是让模型解出原本解不出的题，
  而是让它少兜圈子。**

- **回合数完全一样（4.56 vs 4.56）而工具调用少了 21%** —— 说明省下来的
  是**回合内**的重复调用，不是回合数。这个区别很重要：回合预算是
  agent 的硬成本，而每回合内多打几条命令是软成本。
- **⚠️ n=1（每组只跑一轮）**。工具调用 114→90 与耗时 75.3→60.6 都是
  单次观测，**没有分布**。方向一致（两个独立指标同向，且能定位到
  `#1007`/`#1003` 的重复命令），但仍不能排除模型采样随机性。
  要下结论需要每组 ≥5 轮。
- **⚠️ `#1001` 全程无区分度**：这题在五次观测里给出 16✓ / 18✓ / 20✗ /
  26✓ / 25✗（20 是当时的上限），臂 B 这轮是 26 回合、候选 0。
  **它是掷硬币，不是"卡没用"。** 统计里剔除它之后两臂仍是 9/9。

#### 逐题回合数（复核 41 vs 41 这个巧合）

两个臂的「通过题平均回合」精确相等（4.56 = 41/9），看起来太像巧合，
所以独立核对过一遍。**方法**：不看汇总的 `run.json`，直接读 transcript，
按行首 `[turn N]` 取每题的 turn 集合，比较「最大 turn == 不同 turn 数
== JSON 记录的回合数」——**10 题 × 2 臂全部吻合**，不是字段取错列。

| 题目 | 类别 | 臂 A | 臂 B | 差 |
| --- | --- | --- | --- | --- |
| `#1001 easy-web` | Web | 25 ✗ | 26 ✗ | +1 |
| `#1002 crypto-warmup` | Crypto | 3 | 3 | 0 |
| `#1003 pwn-ret2win` | Pwn | 9 | 9 | 0 |
| `#1004 web-lfi` | Web | 5 | 4 | −1 |
| `#1005 crypto-rsa` | Crypto | 3 | 3 | 0 |
| `#1006 pwn-rop` | Pwn | 5 | 7 | +2 |
| `#1007 rev-crackme` | Reverse | 6 | 4 | −2 |
| `#1008 rev-patchme` | Reverse | 3 | 4 | +1 |
| `#1009 misc-hex` | Misc | 3 | 4 | +1 |
| `#1010 misc-dump` | Misc | 4 | 3 | −1 |
| **九道通过题合计** | | **41** | **41** | **0** |
| 全部十题合计 | | 66 | 67 | +1（`#1001` 那 1 个） |

增减**正好抵消**：+`#1006`2 +`#1008`1 +`#1009`1 = **+4**；
−`#1004`1 −`#1007`2 −`#1010`1 = **−4**。所以 41 vs 41 是真实的抵消，
不是统计串了。全部题 66 vs 67 的差全在 `#1001`（那题是掷硬币，见下），
与 summary 里的 6.6 / 6.7 自洽。

**这个巧合反过来加强了结论**：回合数一个没省、工具调用少了 21%，
说明省的是**回合内**的重复调用 —— 如果两边回合数差得多，"少兜圈子"
就分不清是"回合内省"还是"少走弯路"了。

#### 首次 A/B 对照实验（2026-10-13，`[FlareOn3]GUI`）

同一道题、同一模型（本地 `deepseek-v4.1-flash`）、同样 40 轮，
唯一变量是 `spec.notes_text`（1846 字符的同类题经验）：

| 指标 | A（开召回） | B（关召回） |
| --- | --- | --- |
| 轮数 | 40（跑满） | 40（跑满） |
| 工具调用 / 不同命令 | 26 / 26 | 23 / 23 |
| 命令重复率 | 0.0 | 0.0 |
| 产出候选 flag | 0 | 0 |
| 解出 | ❌ | ❌ |
| 关键工具首触轮次 | monodis@3, mono@3 | monodis@6, mono@6, xvfb@6 |

**怎么读这张表**（别过度解读）：

- **两组都没解出** —— 这一项没有区分度。历史 6 次（40/80/80/13/77/80 轮）
  也全部失败，所以这道题对当前模型就是"解不出"，本实验测不出"解出率"。
- **过程指标差异很小**（命令数 26 vs 23，重复率都是 0）—— 看不出
  "有卡就不兜圈子"的效果。**这与第二次对照的结论不同**：那次是
  **同一批题**、十道、每臂都在能力范围内，所以"少兜圈子"这个效应
  才显出来。单题 + 该题本身超纲，两个条件任一成立都测不出这个效应。
- **n=1，本地模型有随机性**。单次运行既不能证明有用，**也不能证明没用**。
  要得到结论需要每组 ≥5 次重复看分布。

**能确定的**：A 组确实**用了**卡里的做法（`file`/`strings`/`unzip`+
`find -exec file`/`monodis` 都在前几轮出现，正是卡里那些步骤）。
所以"两组差不多"不是"模型没理睬注入内容"，而是**这道题本来就超出
40 轮的能力范围**。

#### 实验工具本身也修过一轮（`4cb0339`）

第一次想跑 A/B 时发现**基线链路根本没法注入方法卡** ——
`scripts/arena_e2e.py` 构造 `SolveSpec` 时没有 `notes_text=` 参数，
恒为空串。**那样跑出来的两臂是同一件事，差异全被归进采样噪声，
比不做实验更坏**（因为它给了个数字）。

补的东西：`scripts/notes_cards.py`（把卡存 JSON，召回规则与生产
`notes.py` 逐条一致）、`solve(notes_text=...)` 参数、
`ctf2_baseline.py run --notes-cards` + `learn` 子命令、
`RunResult.notes_chars` / `RunReport.notes_injected` 两个字段
（**注入链路断了要能当场看见** —— 报告里写着"方法卡无效"而实际
是"没接上"，是这类实验最典型的假阴性）、以及 `compare` 里的
`_warn_if_incomparable`（两臂 `notes_cards` 不同、或给了卡却 0 题
注入，直接警告"两臂其实等价"）。

> ⚠️ 顺带一提 `learn` 的一道闸：卡片写完先扫 flag 形状，命中就
> **整张丢弃**（不是打码，是丢）。理由：方法卡会被注入到**另一道题**
> 的 prompt，一张带 flag 的卡等于把答案递过去。

#### ★ 这次实验的真正收获：发现持久区从未生效

实验脚本第一版自己 `mkdir` 工作区，绕过了 `_workspace_dir` 的
`chown` —— 于是 A 组跑出来时模型报：

    /work/persist is not writable. Let me use /work.

顺着查下去发现**生产路径也一样**：容器以 `USER solver`（uid 1000）运行，
目录却是 root 建的 `root:root 0755`。v0.29 的持久区**从未真正生效过**。

修完（`chown 1000:1000`）重跑同一道题：持久区留下 **8 个真实产物**
（`res_layer1/2/3`、`res_share6`、`gui.il`、自写 harness），
而修复前是 **0 个**。

> 换句话说：这次"验证方法卡"的实验，最大的产出是**发现了另一个
> 声称已实现但实际没生效的功能**。而它之所以藏了这么久，是因为
> 单元测试全绿、文档写着"已实现"、代码路径都在 ——
> **但没有任何测试真的起容器写过一次**。

#### 提炼逻辑里踩过的坑（都是实测发现的，不是推演的）

这一版的方法卡返工过一轮，原因值得记下来 —— **第一版是无效的**：

| 缺陷 | 症状 | 修法 |
| --- | --- | --- |
| **模板文本** | 29 张卡内容**一字不差**（都是"先看清楚手上有什么…"） | 真的读 transcript 提炼关键步骤 |
| **换行被压平** | 6 条编号步骤挤成**一行** | 脱敏改成逐行处理 |
| **参数 JSON 没剥** | 卡片里是 `{"command": "ls -la"}` | `_unwrap_args` 剥外壳 |
| **残骸命令** | `curl -s -i --max-time 20`（URL 被删光，成了空壳） | 判据改为**先脱敏、再判断** |
| **工具名是包装层** | `用到的工具：run_shell`（零信息量） | 从命令正文里按**词边界**认真工具 |
| **子串误判** | `nc` 命中 `sync`、`file` 命中 `profile` | 词边界匹配 |
| **附件名被误掩** | `unzip -o …/<target>`（照抄找不到文件） | 长 hex + 扩展名 → `<attachment>.pcap` |
| **截断的半句** | `… EOF python3`（照抄直接语法错误） | 丢掉语法不完整的命令 |

> ⚠️ 其中**"残骸命令"和"附件名误掩"是同一个根因的两个方向**：
> 判据作用在**脱敏前**的文本上，而脱敏会改变文本。
> 判据必须看**最终会写进卡片的那个形态**（含 `_clip_command` 截断之后）。
>
> 而且这个"判据要窄"是有代价的：我原打算按"含点 + 末尾字母"认 hostname，
> 先量了一遍会误伤什么 —— **34 种真正的方法知识**（`Image.open`、
> `urllib.parse`、`gui.il`…）。照那个判据脱敏，等于把"掏空命令"
> 换个方向再犯一次。最终判据是量出来的（5 处命中、0 处误伤），不是猜的。
>
> ⚠️ **第二次对照里仍看到一个漏网的残骸**：`arena-1010 misc-dump` 的
> 卡写成 `grep -ao ']*}' /work/attachments/dump.bin` —— 脱敏把
> `flag{[^}]*}` 里的 `flag{[^` 当成"flag 形状"吃掉了，留下 `]*}`。
> 它不影响臂 B（那题 3 回合就解出，且该卡未进前 3 张召回），
> 但**是同一类根因的残留**：脱敏规则与"什么是 flag 形状"的判据
> 还没有对齐。记为待办。

### 5. pwn 工具补强：**在这两道题上测不出价值**（如实记录）

`2de97ce` 给沙箱补了 `patchelf` / `gdb` / `one_gadget` / `seccomp-tools`
（+ Ruby 层，见 `82be7a3`）。补强前镜像留了回滚标签
`ctf2-sandbox:pre-gems`，两臂只差 `CTF2_IMAGE_BASE`，六轮重复：

| 题 | before（`pre-gems`） | after（`latest`） |
| --- | --- | --- |
| `#1003 pwn-ret2win` | ✓ 6 / 10 / 10 回合 | ✓ 6 / 8 / 6 回合 |
| `#1006 pwn-rop` | ✓ 6 / 7 / 8 回合 | ✓ 7 / 7 / 8 回合 |
| 合计 | **6/6 通过**，回合中位 **7.5** | **6/6 通过**，回合中位 **7.0** |

> ⚠️ **耗时这一列我拿掉了，理由要写明**：原始耗时是
> before `53.2 / 95.5 / 78.4 / 61.0 / 251.5 / 78.2` 秒、after
> `448.8 / 157.4 / 175.4 / 54.1 / 71.4 / 81.6` 秒 —— **after 更慢**
> （中位 78.3s → 119.5s）。但那 448.8s 对应的是 6 回合
> （74.8s/回合），而同一臂另外两轮是 19.7s/回合与 29.2s/回合；
> 同一时刻宿主 `load average` 到过 **49**（六轮里两次撞上），
> 跑完降到 2.19。**这批耗时里混着宿主负载，没有分辨力**，
> 拿它当结论或当"没变慢"的证据都不成立。回合数不受负载影响
> （它由对话轮次决定），所以只用回合数。

**两臂全部通过，且四个新工具在两臂里的真实执行次数都是 0**
（排除 `command -v` 探测之后）。`#1003` 那个 10 → 6 的差异来自
**另一件事**：before 臂有三轮在命令里捎带 `command -v … gdb …`，
发现没有就退回 `objdump` 静态推偏移；after 臂直接把 `objdump`
输出重定向到文件再用 `awk` 按函数切段。**这只改"怎么读反汇编"，
与镜像里有没有 gdb 无关** —— 模型从头到尾没试过启动 gdb。

> **结论：这两道题测不出补强价值，别把它读成"补强没用"。**
> 题目需要的工具（`checksec` / `ROPgadget` / `objdump` / `nm` /
> `pwntools`）在补强前就都有。要让 A/B 有区分度，题目必须让
> `gdb` 或 `patchelf` 成为**硬性要求** —— 比如错误的 interpreter
> 路径、或给定非系统 libc 需 `patchelf --set-interpreter / --set-rpath`。

#### 顺带修掉的两个真问题

1. **附件目录跨运行残留**（`7c2d4b7`）：`download_attachments()` 用
   `mkdir(exist_ok=True)` + 按名覆盖，只清得掉同名文件；而
   `_tmp/arena-attachments/<id>/` 既不随 git 走也不自动清理，随后被
   **整个目录只读挂进容器**。mock 的 `#1003` 附件从 `baby` 改成
   `pwn_baby` 之后，`baby`（1089 字节，改 mock 之前的时间戳）在六轮里
   **每一轮都还在**，模型每次都要先分析一遍题目根本没给的垃圾文件。
   这不影响能不能解出，但**污染回合数** —— 而回合数正是这轮对照的
   因变量。已改为下载前清空目录，删了几个打进日志。
2. **mock 的两道 pwn 题原来是假的**（`78e003f`）：附件是
   `b"\x7fELF" + bytes(range(256))*2` 加一段明文注释，flag 就写在
   strings 里 —— 模型用 `strings` 就抄出来了，一行 exploit 都不用写。
   现在是 `scripts/pwn_targets/` 里编译出来的真靶机（`ret2win` 与
   `ROP`，显式 `pop rdi; ret` gadget、`win(0x4011bf)` 与 `"unlock"`
   字符串），由 mock 起在本机、沙箱经 `172.18.0.1` 直连。

> ⚠️ 造真靶机时踩到的两个坑，都值得记：
> **① 源码注释里的偏移是错的** —— 原注释写 `offset=80`，
> 实测 `objdump` 是 `0x58 = 88`（`sub $0x50,%rsp` + 返回地址在
> `rbp+8`，`read()` 不存在 `leave;ret` 的中间态）。按注释写，
> payload 只回显"你发了 88 字节"然后正常退出。
> **② `nm` 给的符号地址不是 gadget 指令地址** —— `gadget_pool`
> 符号在 `0x4011b6`，而 `pop %rdi; ret` 实际在 `0x4011ba`（+4，
> 越过了 `push %rbp; mov %rsp,%rbp`）。跳到符号地址什么都不会发生，
> 且没有任何报错。

---

## 安全要点（实现时逐条落实的）

| 项 | 做法 | 为什么 |
| --- | --- | --- |
| 口令哈希 | argon2id（`argon2-cffi`） | bcrypt 有 72 字节静默截断，且需自己处理盐与 cost |
| **provider 凭据** | **Fernet 加密落库**，密钥来自独立的 `SUPEX_SECRET_KEY`；掩码另存一列使读路径不必解密 | `api_key` 必须可还原（要拿去调 LLM），所以不能哈希；而"读接口不碰明文"要能被验证 |
| **凭据回显** | `apiKey` 恒为空串 + `hasKey` + `apiKeyMask` 三字段分工 | 掩码放进 `apiKey` 会被前端原样回传、覆盖真凭据 —— 静默数据损坏，见 [ADR-003](docs/adr-003-credential-storage.md) |
| **日志无明文** | 无请求体日志中间件（已审计）；外加 Formatter + Filter 两层正则脱敏兜底 | 有测试在 `Logger._log` 处拦截所有日志消息；验收脚本再打一遍真实 uvicorn 的日志 |
| 会话 Cookie | `HttpOnly` + `SameSite=Strict` + `Max-Age` + 生产加 `Secure` | 页面会渲染**不可信文本**（`detail.evidence`、`poc.code`），令牌放 localStorage 等于送给 XSS |
| CSRF | `SameSite=Strict` + 服务端 Origin 校验 + 拒绝表单编码的写请求（附件上传走显式登记表） | 判定不能只依赖浏览器实现 |
| 令牌存储 | 只存 SHA-256 摘要；明文只在创建响应出现一次 | 「能再看一次」等于「它以可逆形式存着」 |
| 登录限流 | 滑动窗口 5 次/分钟 + 15 次/小时，**按来源 IP 计数**，与 `/auth/setup` 共用计数桶 | 口令是登录凭据里唯一的秘密（用户名不是）。按 IP 而不是按用户名计数，是因为多用户之后攻击者可以**喷洒不同用户名**来绕过"每个用户名 5 次"这类配额；共用一个桶则避免交替打两个端点把尝试次数翻倍 |
| 时序攻击 | 账号不存在时也跑一次完整的 argon2 校验（用固定的假哈希） | 否则响应时间会泄露"这个系统初始化了没有" |
| 来源 IP | **不读 `X-Forwarded-For`** | 它可被客户端伪造，而它同时用于限流 key 与授权留痕 |
| 附件存储 | 路径完全由服务端从 id 生成，与上传文件名无关；扩展名白名单；下载强制 `Content-Disposition: attachment` | 用文件名拼路径会同时打开路径穿越与**互相覆写**两个洞；SVG 可内嵌 `<script>`，内联渲染等于让上传的文件在本站原点执行 |
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
