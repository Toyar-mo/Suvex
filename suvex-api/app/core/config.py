"""配置：`.env` → `Settings`（pydantic-settings）。

三条设计约束：

1. **`.env.example` 入库，`.env` 不入库**（`.gitignore` 已覆盖）。
   模板里只有开发态默认值，真实口令只存在于 `.env`。
2. **环境变量名统一 `SUPEX_` 前缀**，避免与系统里已有的 `PATH`/`HOME` 之类撞名 ——
   `env_prefix` 是全局的，所以字段名本身不用带前缀。
3. **生产环境的危险配置要出声**，但不能一上来就崩：单机部署的用户改错一个开关
   就得到「容器反复重启、日志里只有一行 pydantic 校验错」是糟糕的体验。
   所以 `startup_warnings()` 返回一份中文警告清单，启动时全部打出来。
   只有**确定不安全**的组合（prod + 未开 Secure Cookie）才升级为硬错误。

`cors_origins` 刻意是 `str` 而不是 `list[str]`：
pydantic-settings 对复杂类型会先尝试 JSON 解析，于是 `a,b` 这种写法会报
「不是合法 JSON」—— 而逗号分隔是环境变量里最自然的写法。
这里收字符串、由 `cors_origin_list` 切开，两种写法（单个源 / 逗号分隔）都能用。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（app/core/config.py → app/core → app → 项目根）。
# 用它定位 .env，而不是靠 CWD —— 否则从别的目录启动（比如 alembic 在别处跑）
# 会静默读不到配置，然后以「连不上数据库」的形式表现，排查方向完全跑偏。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix="SUPEX_",
        extra="ignore",
        case_sensitive=False,
    )

    # ── 基本信息 ──────────────────────────────────────────
    env: Literal["dev", "prod"] = "dev"
    log_level: str = "info"
    # 健康检查要报版本（契约 §3.8）。与 pyproject.toml 的 version 同步维护。
    app_version: str = "0.1.0"
    api_prefix: str = "/api/v1"

    # ── 数据库 ────────────────────────────────────────────
    # 开发态默认值 = docker-compose 里的开发值，且与 .env.example 一致。
    database_url: str = "postgresql+psycopg://suvex:suvex_dev_local@127.0.0.1:55432/suvex"
    db_echo: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # ── 凭据加密（provider 的 API key）────────────────────
    # ★ 单独的密钥，**不复用任何其他密钥**。
    # 本项目没有 JWT 密钥（见 docs/adr-001-auth-credentials.md），
    # 所以这条约束的等价物是：这条密钥只做加密，不签名、不派生会话。
    # 生成：python -m app.core.crypto
    # ⚠️ 丢了它 = 已存的 provider 凭据全部解不开，必须与数据库一起备份。
    secret_key: str = ""

    # ── LLM 连通性探测（POST /providers/{id}/verify）──────
    # 探测要真的发一次请求，所以必须有超时。20 秒是"云端慢模型也能答完
    # 一句问候"的量级；再长会让设置页的按钮转到用户以为卡死。
    llm_probe_timeout_seconds: float = 20.0
    # 探测只问一句话，输出限制在几个 token —— 这是"最小化请求"的字面实现，
    # 目的是验证凭据与连通性，不是拿它当聊天用。
    # ★ 256，不是 8（v0.11 由真实配置暴露后调大）。
    #
    # 原值 8 的意图是"探测要最小化"，但那个数字对**推理型模型**是致命的：
    # 这类模型的产出分两段 —— `reasoning_content`（想的过程）与
    # `content`（最终回答），而预算先被前一段吃掉。
    #
    # 实测一个推理型模型（`deepseek-flash`，prompt 只有一句 `ping`）：
    #
    # | max_tokens | content | reasoning_tokens | finish_reason |
    # | ---------- | ------- | ---------------- | ------------- |
    # | 8          | `""`    | 8                | length        |
    # | 64         | `""`    | 64               | length        |
    # | **256**    | `"Pong! 🏓 …"` | 36        | **stop**      |
    #
    # 也就是说 8 与 64 下模型**根本没机会回答**，而探测会把它读成
    # "响应结构不对"、反过来叫用户去查 baseUrl。
    #
    # 256 的代价仍然可以忽略（一次探测几十个 output token，是用户点按钮触发的），
    # 而"最小化"的本意（一句 prompt、不走工具、不流式）一条都没丢。
    llm_probe_max_tokens: int = 256
    # 探测只要求模型回一句话，别的什么都不做。
    llm_probe_prompt: str = "ping"
    # 是否允许 baseUrl 指向内网/回环地址。
    # **默认允许**，因为本地模型（Ollama 在 127.0.0.1:11434）是明确支持的一类。
    # 置 false 会让本地模型完全不可用。云厂商的元数据地址
    # （169.254.169.254）无论这个开关如何都会被拦 —— 见 providers/llm.py。
    allow_private_llm_endpoints: bool = True

    # ── 扫描执行（任务 3）──────────────────────────────────
    #
    # ★ 同时运行的扫描数上限。超过上限的新扫描**排队**，而不是直接拒绝，
    # 也不是无限制地并发。
    #
    # 默认 **1** 是刻意的：一次扫描会拉起一个沙箱容器（默认镜像
    # `ghcr.io/usestrix/strix-sandbox`）加大量 LLM 调用。单机上并发跑多个，
    # 既抢 CPU/内存，又让费用不可控 —— 而这是**会对真实目标发包**的进程，
    # 失控的并发不只是性能问题。要并行就显式调大，并且知道自己为什么调。
    max_concurrent_scans: int = 1

    # 用哪个 runner 实现：
    #   `agent` —— 自研 Suvex 内核引擎（进程内跑，**当前默认**）
    #   `strix` —— 真跑外部 strix CLI 子进程（**手动回退用，不再是默认**）
    #   `fake`  —— 一个替身，按脚本化的时间线改状态，**不执行任何真实攻击**
    #
    # ★ v0.66：默认从 `strix` 翻成 `agent`。理由不是"自研引擎更好用"，
    #   而是**部署安全网**：终审实战是在赛方受控环境里现场部署，
    #   如果走一次干净部署（不带开发机上那份现成 .env），默认落到
    #   `strix` 就等于现场起不来 —— 实测 strix 的独立 venv 已损坏
    #   （`import strix` 报 ModuleNotFoundError）。`agent` 是进程内实现，
    #   不依赖任何外部可执行文件与 venv，干净部署必然能起。
    #
    #   `strix` 那条路**保留不删**：需要时用 `SUPEX_SCAN_RUNNER=strix`
    #   显式切回去（`_RUNNERS` 里仍登记着它）。这是"留手动回退"而不是
    #   "彻底移除"，与 m07335 的最终目标（超越后移除）不冲突 —— 移除是
    #   另一件事，得等自研引擎先证明自己不输。
    #
    # 有 `fake` 不是"为了方便"：测试与 CI **绝不能**跑真实渗透测试
    # （它们会向真实目标发包，那是违法的）。所以必须有一个可替换的执行后端。
    scan_runner: str = "agent"

    # strix 可执行文件。
    #
    # ⚠️ 只有在 `scan_runner == "strix"`（手动回退）时才会用到。默认后端
    #   是 `agent`，它不读这个值。保留它是因为回退路径仍需要它。
    #
    # ⚠️ 本机把它指向**独立 venv** 里的 strix.exe（见 README「strix 环境」）——
    # strix 依赖 litellm 等一大坨包，装进 API 的 venv 里，
    # 它的一次依赖升级就可能改掉 API 用的 pydantic / httpx 版本。
    # 两者跑在**不同的解释器**里，这个问题就不存在了。
    strix_command: str = "strix"

    # 扫描的工作目录根。每次扫描在其中建一个子目录，
    # strix 的 `strix_runs/<run_name>/` 就落在那儿。空 = `<项目根>/_scan_runs`
    scan_work_root: str = ""

    # 漏洞附件的落盘根（任务 10 P6-d）。空 = `<项目根>/_uploads`。
    # ⚠️ 环境变量名是全局 `env_prefix` 拼出来的：`SUPEX_ATTACHMENT_ROOT`。
    # 见 `attachment_root` 属性：它与 `scan_root` 必须是两个目录，
    # 因为附件的生命周期跟着漏洞走，而扫描产物会被保留期清理。
    attachment_root: str = ""

    # 单个附件的字节上限（任务 10 P6-d）。默认 10 MiB。
    #
    # ★ 必须有上限：没有它，一个 2 GB 的上传就能把磁盘写满，
    #   而磁盘满是**整个服务**不可用（不只是这个请求失败）。
    # 10 MiB 足够放一张高清截图或一份 PDF 复测报告。
    attachment_max_bytes: int = 10 * 1024 * 1024

    # 一次请求内允许的附件数量上限。防止"一次传 500 个"占满磁盘与请求时间。
    attachment_max_count: int = 20

    # 传给 strix 的 `--scan-mode`：quick | standard | deep
    #
    # 默认取 `standard` 而不是 strix 自己的默认 `deep`：
    # `deep` 是"尽可能彻底"，在一次真实扫描上意味着更长的运行时间与更高的费用。
    # 一个默认值应该是"通常够用"，把最贵的选项设成默认等于替用户做了花钱的决定。
    scan_mode: str = "standard"

    # 单个 agent 的回合上限（strix 的 `--max-turns`）。
    # 500 是 strix 自己的默认值，这里跟它保持一致 —— 不另立一套。
    scan_max_turns: int = 500

    # 整次扫描的 LLM 费用上限（美元）。null = 不限。
    #
    # **默认不限**（与 strix 一致）：本平台是自托管工具，
    # 费用由用户自己的 key 承担，而擅自设一个上限会让扫描在中途被停掉、
    # 且用户不知道为什么。要控制成本请显式设它。
    scan_max_budget_usd: float | None = None

    # 调度器轮询间隔（秒）。它决定"状态变更多久被写回库"的上限延迟。
    # 2 秒是一个折中：再小会让空转查询变密，再大会让界面上的状态明显发顿。
    scan_poll_seconds: float = 2.0

    # 等 strix 创建 `strix_runs/<run_name>/` 的时间上限（秒）。
    # 超过就先把 run 标识留空 —— 它只是诊断信息，不该拦住扫描启动。
    scan_run_detect_seconds: float = 20.0

    # 终止时的宽限期（秒）：先 terminate，等这么久还不退就 kill。
    scan_abort_grace_seconds: float = 10.0

    # ── 卡死检测（任务 11）────────────────────────────────
    #
    # "多久没有新事件就认为这个扫描卡住了"（秒）。
    #
    # ★ 为什么需要它：实测有过一次扫描跑了 32 分钟零产出 ——
    #   agent 把 https 当成了目标协议（目标其实只有 http），于是每个请求
    #   都在 TLS 握手上失败，而它反复用**空输入**轮询同一个 shell 会话
    #   （45 次 `write_stdin {"chars":""}`，等待从 15 秒涨到 60 秒）。
    #
    #   那是"一个命令永远不返回"的形态，而 `--max-turns` **拦不住它**：
    #   回合数根本没在涨，时间却在走。用户只能看着一个转圈的界面，
    #   既不知道该等还是该停。
    #
    # 判据用**事件时间戳**而不是 CPU/输出量：`_import_for` 每轮都把
    # 外部工具新写出的事件导进来（这正是实时性的来源），所以
    # "事件停了" == "agent 确实没在产出"，与它在忙什么无关。
    #
    # 600 秒是个折中：正常的目录爆破/长命令会安静几分钟，
    # 而 10 分钟没有任何事件基本只剩"卡住了"这一种解释。
    # 0 = 关闭检测。
    scan_stall_seconds: float = 600.0

    # 检测到卡死时的动作：
    #   `flag` —— 只标记（界面上提示"疑似卡死"），**不终止**。
    #   `abort` —— 直接终止这次扫描。
    #
    # 默认 `flag` 而不是 `abort`：这是一个**启发式**判断，而终止一个
    # 正在跑真扫描的进程是不可逆的。宁可让用户看到提示后自己决定，
    # 也不要系统替他杀掉一个可能只是在跑长任务的过程。
    #
    # ⚠️ 只接受这两个值，写别的会在**启动时**报错而不是静默退化成 flag：
    #    一个拼错的 `abort`（比如 `abortt`）如果被静默忽略，运维会以为
    #    自动终止已经开着，而实际上它从来没生效过。
    scan_stall_action: Literal["flag", "abort"] = "flag"

    # ★ 沙箱容器所在的 docker 网络（任务 8 的隔离手段）。
    #
    # 空 = 用 docker 默认 bridge。**生产建议显式设一个专用网络**，
    # 理由是 strix 会给沙箱加 `host.docker.internal → host-gateway`
    # （见 docs/security-checklist.md 的 F-02），于是沙箱能回连宿主机。
    # 把它放进一个 `internal: true` 的网络、并且不加入业务网络，
    # 是当前能在配置层面做的最有效的收窄。
    #
    # 透传给 strix 的 `STRIX_DOCKER_SANDBOX_NETWORK`
    # （依据：strix_agent 1.6.2 的 runtime/docker_client.py）。
    sandbox_network: str = ""

    # strix 的遥测开关。**默认关**（上游默认是开）。
    #
    # 这是产品取向而不是洁癖：本平台跑的是**客户目标的渗透测试**，
    # 把目标地址与用量上报给第三方，是一件需要用户明确同意才能做的事。
    # 想开就显式设 true（strix 的 `STRIX_TELEMETRY`）。
    strix_telemetry: bool = False

    # ── 替身执行后端（`SUPEX_SCAN_RUNNER=fake`）的脚本参数 ──
    # 它们只在测试/CI 里用。**绝不能**把它们当成"模拟扫描"的功能开关 ——
    # 替身进程不做任何真实攻击，它的用途是让队列与进程监督可被验证。
    fake_scan_seconds: float = 3.0
    fake_scan_exit_code: int = 0
    fake_scan_run_id: str = ""
    # 是否让替身起一个孙进程（用来验证"杀进程树"而不是只杀直接子进程）。
    fake_scan_grandchild: bool = True
    # 是否让替身产出模拟的 strix 产物
    # （`strix_runs/<run>/.state/agents.db` + `agents.json` + `run.json`）。
    # 打开它，"strix 事件 → 本地库 → SSE" 这条链路才能端到端被验证；
    # 关掉它可以验证"没有产物时导入器不炸"。
    fake_scan_events: bool = True

    # ── 会话 Cookie（契约 §1.2）───────────────────────────
    session_cookie_name: str = "suvex_session"
    session_ttl_hours: int = 12
    cookie_secure: bool = False

    # ── CORS（契约 §8）────────────────────────────────────
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ── 扫描授权声明（契约 §5.3）──────────────────────────
    # 服务端逐字校验前端送来的 statement，防止「顺手勾一个默认值」。
    scan_authorization_statement: str = "我有权对此目标进行安全测试"

    # ── 登录限流（契约 §3.1）──────────────────────────────
    login_limit_per_minute: int = 5
    login_limit_per_hour: int = 15

    # ── 扫描创建限流（契约 §3.3，任务 8 补）────────────────
    # 比登录松得多：登录是防在线爆破，而这是防"脚本把队列刷爆"。
    # 见 `scans.service._create_limiter` 的说明。
    scan_create_limit_per_minute: int = 30
    scan_create_limit_per_day: int = 200

    # ── SSE（契约 §4）────────────────────────────────────
    sse_heartbeat_seconds: int = 20
    sse_replay_max_events: int = 1000

    # ── 可观测性（v0.23）──────────────────────────────────
    #
    # ★ 默认**开**。理由：指标的价值在"出事时已经有数据了" ——
    #   一个默认关闭的指标端点，在真正需要它的那一刻才开始收集，
    #   而那时你已经错过了出事前后的那段曲线。
    #
    # 它的暴露面很小（只有数字，没有业务数据 —— 见 `core/metrics.py`），
    # 而关掉的方式很明确（配 `SUPEX_METRICS_ENABLED=false`，
    # 或在反代上把 `/metrics` 限到监控网段）。
    # 这两点合起来让"默认开"是个安全的选择。
    metrics_enabled: bool = True

    # ── ★★ 公网暴露面开关（v0.39）─────────────────────────
    #
    # ## 为什么必须是**独立开关**，不能绑在 `env` 上
    #
    # 直觉写法是"prod 就关掉文档"，但那样会**直接坏掉登录**：
    #
    #   · `assert_bootable()` 要求 `env=prod` 时 `cookie_secure=true`；
    #   · 而线上是 **http**（还没有证书），`Secure` cookie 在 http 下
    #     浏览器**根本不回传** → 登录接口 200，下一个请求仍然 401。
    #
    # 也就是说"想关文档"会逼出一个"必须开 Secure cookie"的要求，
    # 而后者在 http 下是错的。两件事**必须解耦**：
    # `env` 决定 cookie/密钥这类**部署安全属性**，
    # 这两个开关只决定**要不要把文档/指标挂出去**。
    #
    # ## 默认值的方向
    #
    # * `expose_docs` 默认 **False**：文档描述的是**全部端点与出入参**，
    #   匿名可读等于把攻击面清单送出去。开发时要看就显式打开。
    # * `expose_metrics` 跟随既有的 `metrics_enabled`（默认 True）——
    #   它的暴露面确实小（只有数字），且项目原本的注释就写了
    #   "或在反代上把 /metrics 限到监控网段"。
    #   ⚠️ 但公网部署**应当**在反代层限掉它（见 `deploy/`）。
    #
    # ⚠️ 这两个开关**不是**安全边界：它们只影响"挂不挂路由"。
    #    真正的边界是反代（谁能连上来）与鉴权。
    expose_docs: bool = False
    #: `/metrics` 是否对外可读。**独立于** `metrics_enabled`：
    #: 后者决定"要不要采集"，这个决定"要不要暴露"。
    #: 采集关掉而暴露开着是自相矛盾的，所以有效值取两者的与。
    expose_metrics: bool = True

    #: `/health/sandbox` 是否返回**镜像名**（v0.39）。
    #:
    #: 默认 **False**：那个字段原本是为了让 Settings 页面显示，
    #: 而该页面本身要登录 —— 公网匿名可读一个具体镜像 tag
    #: 等于给出"可以定向哪个已知 CVE"的线索（见 `health.py` 的说明）。
    #: 健康检查本身仍然可用（回 `status`），只是不再回镜像名。
    expose_sandbox_image: bool = False

    @field_validator("cors_origins")
    @classmethod
    def _strip_origins(cls, value: str) -> str:
        return value.strip()

    @property
    def cors_origin_list(self) -> list[str]:
        """逗号分隔 → 列表，去空去重保序。"""
        seen: list[str] = []
        for raw in self.cors_origins.split(","):
            origin = raw.strip().rstrip("/")
            if origin and origin not in seen:
                seen.append(origin)
        return seen

    @property
    def dev_cors_origins(self) -> list[str]:
        """`cors_origin_list` 里属于**本地开发前端**的那些源。

        ★ v0.73 新增：P1 对照扫描（打自家平台前端）实测报出
        「CORS 白名单含开发源 http://localhost:5173 且 allow_credentials=True」。
        回代码核对确认这是**真实配置缺陷**：`.env` 在部署副本里原样带着
        `SUPEX_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173`，
        而 `main.py` 是 `allow_credentials=True` —— 生产环境把 Vite 开发端口
        留在凭据白名单里毫无必要（攻击者要利用需要受害者本机开着该端口，
        影响有限，但没有任何理由保留）。

        识别口径：host 是 `localhost` / `127.0.0.1` / `0.0.0.0` / `[::1]`
        **且**端口是 Vite 默认的 5173（或其常见 dev 端口）。只认这几项，
        不做模糊匹配 —— 误杀一个真实生产源比漏报更难排查。
        """
        dev_ports = {"5173", "5174", "3000", "4173"}
        dev_hosts = {"localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1"}
        out: list[str] = []
        for origin in self.cors_origin_list:
            # origin 形如 scheme://host:port，只拆 host 与 port，不看 scheme（两者都算）。
            rest = origin.split("://", 1)[-1]
            host, _, port = rest.rpartition(":")
            if not host:  # 没有端口（如 http://localhost），整个 rest 就是 host
                host, port = rest, ""
            if host.lower() in dev_hosts and (port == "" or port in dev_ports):
                out.append(origin)
        return out

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def scan_root(self) -> Path:
        """扫描工作目录根。空配置 → 项目根下的 `_scan_runs`。

        放在项目根（而不是系统临时目录）是刻意的：扫描产物里可能有
        `events.jsonl` 与报告，排查问题时要能找到它们；
        临时目录会被系统清理，而"扫描日志过了几天自己没了"
        对事后复盘是致命的。
        """
        if self.scan_work_root.strip():
            return Path(self.scan_work_root.strip())
        return PROJECT_ROOT / "_scan_runs"

    @property
    def attachments_dir(self) -> Path:
        """漏洞附件的落盘根（任务 10 P6-d）。空配置 → 项目根下的 `_uploads`。

        与 `scan_root` 同一个思路（项目根下、不用系统临时目录），
        但**必须是不同的目录**：扫描产物会被保留期清理掉
        （`sweep_retention` 按 `scan_work_root` 下的子目录删），
        而附件是**用户上传的证据**（处置截图、复测报告），
        它们的生命周期跟着漏洞走，不该被扫描的保留期顺手清掉。

        ⚠️ 真实存储路径由**服务端**从 id 生成，绝不用上传时带的文件名
        （见 `attachments.py` 的说明）。
        """
        if self.attachment_root.strip():
            return Path(self.attachment_root.strip())
        return PROJECT_ROOT / "_uploads"

    @property
    def sync_database_url(self) -> str:
        """Alembic 与 SQLAlchemy 同步引擎用的 URL。

        本项目的 ORM 是**同步**的：这个后端的负载形态是「单机、几十次扫描、
        几个浏览器标签」，异步驱动带来的复杂度（greenlet 上下文、
        async session 生命周期、sync 库不可用）换不回等价的收益。
        FastAPI 的同步 `def` 端点会跑在线程池里，不会阻塞事件循环 ——
        唯一需要真异步的是 SSE 长连接，那个用 async 端点单独实现。
        """
        return self.database_url

    def startup_warnings(self) -> list[str]:
        """启动时该说的话。中文、具体、给出怎么做。"""
        warnings: list[str] = []
        if not self.secret_key.strip():
            # 不硬失败（dev 要能起来看文档、跑 /health），但必须非常显眼：
            # 少了它，所有 provider 的写入接口都会报错，
            # 而那个报错只有在用户点"保存"的时候才出现。
            warnings.append(
                "未配置 SUPEX_SECRET_KEY —— provider 的 API key 无法加密存储，"
                "相关接口会返回错误。生成一条并写进 .env：\n"
                "        python -m app.core.crypto\n"
                "    ⚠️ 这条密钥必须与数据库一起备份：丢了它，已存的 provider 凭据就再也解不开。"
            )
        if self.is_prod:
            if not self.cookie_secure:
                warnings.append(
                    "生产环境（SUPEX_ENV=prod）未开启 SUPEX_COOKIE_SECURE —— "
                    "会话 Cookie 会在明文信道上传输。请设为 true 并用 https 提供服务。"
                )
            if "suvex_dev_local" in self.database_url:
                warnings.append(
                    "生产环境仍在用 .env.example 里的示例数据库口令 —— "
                    "它已经公开在仓库里，必须更换。"
                )
            if any(o.startswith("http://") for o in self.cors_origin_list):
                warnings.append(
                    "生产环境的 SUPEX_CORS_ORIGINS 里含 http:// 源 —— "
                    "带凭据的跨源请求应只允许 https。"
                )
        else:
            if self.cookie_secure:
                # 开发态是 http，开了 Secure 浏览器就不会回传 Cookie，
                # 现象是「登录接口 200 但下一个请求仍然 401」——
                # 这个现象很容易被误判成 CORS 或 SameSite 的问题，所以要点名。
                warnings.append(
                    "开发态开启了 SUPEX_COOKIE_SECURE —— 浏览器在 http 下不会回传该 Cookie，"
                    "会表现为「登录成功但紧接着的请求仍是未登录」。设为 false。"
                )
        if not self.cors_origin_list:
            warnings.append("SUPEX_CORS_ORIGINS 为空 —— 前端将无法携带凭据访问本 API。")
        return warnings

    def assert_bootable(self) -> None:
        """只有确定跑不起来 / 确定不安全的组合才抛。

        目前两条：

        1. **生产环境关闭 Secure Cookie。** 这属于「配置错了却不会报错、
           只是悄悄不安全」的那类，必须在启动时拦住 ——
           它不像端口占用那样会自己暴露。
        2. **生产环境没有 `SUPEX_SECRET_KEY`。** 同样不会在启动时暴露，
           只会等到有人去保存一个 provider 时才报错。而那一刻的
           正确反应不是"让用户看到一条错误"，是"这个部署根本没准备好"。
           开发态只警告不拦 —— 本机起来看文档、跑 /health 是合理需求。
        """
        if self.is_prod and not self.cookie_secure:
            raise RuntimeError(
                "拒绝以不安全的配置启动：SUPEX_ENV=prod 时必须设置 SUPEX_COOKIE_SECURE=true。"
            )
        if self.is_prod and not self.secret_key.strip():
            raise RuntimeError(
                "拒绝以不完整的配置启动：SUPEX_ENV=prod 时必须设置 SUPEX_SECRET_KEY，"
                "否则 provider 的 API key 无法加密存储。生成：python -m app.core.crypto"
            )
        # ★ v0.73：生产环境不许把本地开发前端源留在 CORS 凭据白名单里。
        # 依据见 `dev_cors_origins` 的注释（P1 对照扫描实测 + 代码核对）。
        dev_origins = self.dev_cors_origins
        if self.is_prod and dev_origins:
            raise RuntimeError(
                "拒绝以不安全的配置启动：SUPEX_ENV=prod 时 SUPEX_CORS_ORIGINS 不能包含"
                f"本地开发前端源 {dev_origins}（allow_credentials=True 下等于把生产凭据"
                "白名单开放给开发端口）。请只填真实生产前端源。"
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。

    用 `lru_cache` 而不是模块级常量：测试里需要能 `cache_clear()` 换配置，
    模块级常量一旦导入就再也改不动，只能靠 reload 模块 —— 那是另一个坑。
    """
    return Settings()


# 便捷别名。注意：需要可替换的场合请直接调用 `get_settings()`。
settings = get_settings()
