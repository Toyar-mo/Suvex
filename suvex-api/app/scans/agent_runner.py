"""自研引擎后端：用 `ctf2` 那套内核跑渗透扫描。

## 这是什么，为什么要有它

`scans/runner.py` 的 `StrixRunner` 把一次扫描交给**外部 strix CLI**。
而这个模块把同一件事交给**我们自己的引擎**（`app/kernel/runner.py` 的
`SandboxContainer` + `LlmGateway` + `solve_in_sandbox`）。

两者是**同一个内核的两个抽屉**，不是两套系统：

| 能力 | ctf2 | 本模块 |
| --- | --- | --- |
| LLM 调用（含换模 fallback） | `LlmGateway` | ✅ 直接用 |
| 沙箱容器生命周期 | `SandboxContainer` | ✅ 直接用 |
| 三个原子工具 | `dispatch_tool` | ✅ 直接用 |
| ReAct 主循环 + 三段式上下文 | `solve_in_sandbox` | ✅ 直接用 |
| 空转台账 / 截断提醒 / 收尾提醒 | 主循环内置 | ✅ 直接用 |
| 判据 | 「拿到 flag」 | ❌ 换成「已验证的发现」 |
| 产出 | 候选 flag | ❌ 换成结构化发现 |

所以它**不是**一份复制粘贴 —— 循环、工具、沙箱、LLM 全部复用，
真正新写的只有"判据与产出"这两件事。

## ★ 为什么它比 strix 那条路更值（不是"造个轮子"）

`scans/strix_events.py` 那一整层（约 700 行）之所以存在，是因为
**strix 的事件格式要翻译成我们的契约**，而且那个翻译是猜的：

* `discovery` 的三个载荷字段靠"名字最可能对得上"取
  （见那个模块 docstring 的「启发式的部分」一节）；
* `poc_generated` / `verified` / `blocked` 的依据参数名**曾经全部猜错**，
  三条都标着"✅ 确证"，其中两条的真实分支从来没命中过；
* `blocked` 在 strix 流里根本没有来源。

而自研引擎**自己知道它做了什么**：它知道这一轮是在侦察还是在校验、
它知道那条命令的原始输出、它知道某个结论有没有证据撑。
所以事件不再需要翻译 —— 可以直接写成契约形状，
`discovery` / `verified` / `blocked` 从"猜参数名"变成"引擎自报"。

## 进程模型：线程 + 事件循环，不是子进程

`LaunchResult.process` 在 strix 那条路上是一个 `subprocess.Popen`，
调度器靠 `poll()` 判存活。而本模块的工作单元是一个**线程里跑的
asyncio 事件循环**（与 `app/kernel/scheduler.py` 完全同构 —— 那里的
`_Running.thread` + `thread.is_alive()` 就是这个形状）。

所以 `launch()` 返回的不是 `Popen`，而是一个满足
`ProcessLike` 协议的轻量对象（见 `_TaskHandle`）：

* `poll()` → `None`（还在跑）或退出码（跑完了）；
* `pid` → 线程 id（仅用于日志，**不可**用于 `terminate_tree`）；
* `terminable` → `False`（线程杀不掉，见下）。

### ⚠️ 线程杀不掉 —— 这个限制必须写在明处

Python **没有**安全的杀线程手段（`PyThreadState_SetAsyncExc` 是未定义
行为，且对正在 `await` 的协程无效）。这条限制与 `ctf2/scheduler.py`
docstring 里那段完全一样，处理方式也一样：

* `terminate` 的语义是"**设置停止标志，让它在下一个检查点自己退出**"，
  不是"立刻全停"；
* 超时之后留下的线程是 daemon，随进程消失；
* 但**沙箱容器不会随线程消失**，所以主循环的 `finally` 里那个
  `sandbox.destroy()` 是唯一的清理路径。

所以 `_TaskHandle.terminable` 为 `False` 时，调度器那边**必须**跳过
`terminate_tree(pid)` —— 拿一个线程 id 去 `kill` 会打到不相干的进程上。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path

from app.scans.runner import (
    LaunchResult,
    LaunchSpec,
    RunnerUnavailable,
)

logger = logging.getLogger("suvex.scans.agent")

#: 本后端在 `scans.runner._RUNNERS` 里的注册名，也是落进 `Scan.runner` 的值。
RUNNER_NAME = "agent"

#: 退出码。**刻意只用 0 与非 0**，不复用 strix 的 `EXIT_FINDINGS = 2`。
#:
#: ## 为什么不用 2
#:
#: strix 的 `2` 表示"发现了漏洞"，而 argparse 的用法错误**也是 2** ——
#: 这正是 `service.finalize` 要拿 `strix_runs/` 目录存在性去消歧的那件事
#: （见 `runner.did_strix_run` 与模块 docstring）。
#:
#: 自研引擎**没有这个二义性**：它要么正常跑完（有发现或没发现都是 0），
#: 要么抛异常。所以它不需要一个"发现了东西"的特殊退出码 ——
#: 发现本身走的是**结构化产出**（`vulnerabilities.json`），不是退出码。
#:
#: 而如果这里也返回 2，`finalize` 会去问 `did_strix_run()`，那个函数
#: 找的是 `strix_runs/` 目录 —— 自研引擎永远不建它，于是每条扫描都被
#: 判成 failed。这个 bug 会**静默地**把所有成功说成失败。
EXIT_OK = 0
EXIT_ERROR = 1

#: 引擎过程的**结构化**事件流，随跑随写，一行一个 JSON。
#:
#: 与 `scan.log`（给人 `tail -f` 看的纯文本）分开：那个是日志，这个是
#: **可回放的审计事件**，导入器按 `seq` 去重后灌进 `activity_events`。
#:
#: ★ 为什么需要它：strix 后端会自己写 sqlite（`agents.db`），导入器
#:   （`importer.read_events`）读那个库，所以 strix 扫描的活动流、智能体
#:   名册、实时 SSE 全都活着。自研引擎不写 sqlite —— 如果只写 scan.log，
#:   那么每次 agent 扫描在界面上都是「0 个智能体 · 活动流空白」，而这正好
#:   抹掉 Suvex 最核心的差异点（可解释审计 + 实时过程）。实测撞到过：
#:   引擎在跑、scan.log 里 tool_call 一条条出，界面却一直是「0 个智能体」。
#:
#: 格式（每行）：`{"seq": int, "kind": str, "summary": str, "payload": {...}, "at": iso8601}`
#: `seq` 是**幂等键**（对应 `ActivityEvent.source_row_id`），只增不减。
EVENTS_FILENAME = "events.jsonl"


@dataclass
class _TaskHandle:
    """一个在跑的引擎任务。满足调度器需要的 `ProcessLike` 形状。

    ⚠️ 它**不是** `subprocess.Popen`。任何拿它当 Popen 用的地方
    （`terminate_tree(handle.pid)`、`handle.wait(timeout=…)`）都是错的，
    见模块 docstring 的「线程杀不掉」一节。
    """

    thread: threading.Thread
    #: 线程标识，仅用于日志与去重。**不是** OS pid，绝不能传给 `kill`。
    pid: int
    started_at: float
    #: 让线程自己退出的标志。主循环在检查点上看它。
    stop_event: threading.Event = field(default_factory=threading.Event)
    #: 线程结束后的退出码，`poll()` 读它。
    exit_code: int | None = None

    @property
    def terminable(self) -> bool:
        """能不能用 `terminate_tree(pid)` 杀掉。**恒为 `False`**。

        调度器与 `service` 的所有终止路径都必须先问这个 —— 见模块
        docstring 的「线程杀不掉」一节。做成属性而不是常量，是为了让
        调用点写成 `if handle.terminable:` 而不是 `if isinstance(...)`：
        将来若有第三种后端（比如真的回到子进程），它不必改调用点。
        """
        return False

    def poll(self) -> int | None:
        """`None` = 还在跑；非 `None` = 已结束（值即退出码）。

        语义与 `subprocess.Popen.poll()` 一致 —— 调度器 `_reap_finished`
        正是按这个语义写的，所以它不必改。
        """
        if self.thread.is_alive():
            return None
        # 线程已经结束。退出码由线程体在 finally 里写；如果它连写都没来得及
        # （进程被杀、解释器退出），那就是异常终止 —— 报非 0 而不是 0。
        return self.exit_code if self.exit_code is not None else EXIT_ERROR

    def request_stop(self) -> None:
        """请它自己退出。**不是**立刻生效，见模块 docstring。"""
        self.stop_event.set()


class AgentRunner:
    """用自研引擎跑一次扫描。

    ## 能力现状（v0.66 复核，别再写旧话）

    ★ 历史包袱提醒：本段曾写「多智能体分工：这里是单 agent 单循环」——
      那是 v0.58 之前的真话，v0.58 落了完整多智能体编排之后**已经过时**。
      凡是看到「做不到多智能体」的说法，以本段为准。

    已具备：
    1. **多智能体分工** —— 由 `enable_multi_agent` 开关控制（与 CTF 侧同一个
       配置项）。开启后 `solve_in_sandbox` 走 `app.kernel.multiagent.run_multi_agent`
       （root 只拆不亲自动手、子 agent 并行），关闭时才走单循环。开关经
       `service.launch_scan` 读配置写进 `LaunchSpec.multi_agent` → `SolveSpec.multi_agent`。
       ⚠️ 默认**关**（那是一套不同纪律，见开关注释），开不开是配置问题，不是能力有无。

    仍**没有**（如实写在这里，不在别处再声明一遍）：
    1. **断点续跑**（完整 LLM 历史与 agent 拓扑）—— 这里只有
       `resume_digest` 的动作纪要，上下文是重新拼的；
    2. **攻击面自动扩展**（strix 的 `--scope-mode diff`）—— 没有；
    3. **沙箱内交互式长驻 shell** —— 这里每条命令独立起进程
       （`SandboxContainer.exec`），没有可保持的 shell 会话。

    续跑在 `LaunchSpec` 上的表现是：`resume_run_id` 被**忽略**。
    刻意不静默忽略 —— `launch()` 会打一条 warning 说明"这个后端不支持续跑"，
    因为用户点了「续跑」却得到一次全新的扫描，是必须被告知的事。
    """

    name = RUNNER_NAME

    def ensure_ready(self) -> None:
        """启动前自检。

        ## 检查什么、不检查什么

        **检查**：引擎关心的三条硬前置 ——
          1. LLM 接入点配好了没有（`LlmGateway` 需要在构造时就有 key 与 base）；
          2. 沙箱镜像在不在本地（`suvex-sandbox` 那一族，见 `image_for_profile`）；
          3. docker 能不能用（`SandboxContainer.start` 会起容器）。

        第 2 条尤其重要：strix 那条路上实测踩过"等 197 秒才发现镜像拉不下来"
        （`ghcr.io/usestrix/strix-sandbox:1.3.0`，11 层全卡在 `Pulling fs layer`）。
        本模块的镜像是**本地构建**的（`docker/suvex-sandbox/Dockerfile`），
        不走网络，所以这一条只是"在不在"，不是"拉得到吗"。

        **不检查**：目标可达性。那是 `scans/reachability.py` 的事，
        而且它**刻意不阻断建任务**（见那个模块的说明）。
        """
        problems: list[str] = []

        # ① 引擎能否 import（这条最容易在系统升级后静默失效）
        try:
            from app.kernel import runner as ctf2_runner  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            # ⚠️ 用 `except Exception` 而不是 `ImportError`：一个坏掉的
            # 依赖可能在 import 期抛任何东西（`KeyError`、`RuntimeError`、
            # 甚至 `SystemExit`）。这里要的是"能不能用"这个答案，
            # 不是"是不是 ImportError"这个分类。
            problems.append(f"自研引擎无法导入：{type(exc).__name__}: {exc}")

        # ② docker 可用（`ensure_docker_available` 内部会抛 KernelError）
        if not problems:
            try:
                ctf2_runner.ensure_docker_available()
            except Exception as exc:  # noqa: BLE001
                problems.append(str(exc))

        if problems:
            raise RunnerUnavailable(
                "自研引擎不可用：\n" + "\n".join(f"  · {p}" for p in problems)
            )

    def command(self, spec: LaunchSpec) -> list[str]:
        """**永远返回空列表。**

        ## 为什么这个协议方法在这里是空的

        `ScanRunner` 协议里 `command()` 的存在理由是 strix 那条路需要
        拼一个 argv 交给子进程，而"拼出来的东西"要能被展示与审计。
        本模块没有子进程、没有 argv —— 它是进程内的一线程。

        刻意**返回空列表而不是抛 NotImplementedError**：`service.launch_scan`
        会把 `runner.command(spec)` 的结果记进 `LaunchResult.command`，
        而那里只是拿来记日志；抛异常会让整条启动路径失败，
        为了一个诊断字段而让功能不可用是错的。

        ⚠️ 但也**不要**拿它当成"这个后端没有配置"的证据 ——
        配置在 `env()` 与引擎自己的 settings 里。
        """
        return []

    def env(self, spec: LaunchSpec) -> dict[str, str]:
        """**永远返回空字典。** 理由与 `command()` 完全一样。

        本模块不向任何子进程传环境变量，因为不启子进程。
        引擎自己的配置（模型、超时）走 `SolveSpec` 与 `app.core.config`。
        """
        return {}

    def detect_run_id(self, run_dir: Path) -> str | None:
        """引擎自己写的运行标识。

        ## 为什么要有一个

        `Scan.run_id` 是契约 §2.1 的对外字段（前端「运行 ID」那一栏），
        而 `service.launch_scan` 拿的是 `LaunchResult`，里面没有 run_id ——
        它是 `scheduler._reap_finished` 与 `recover_orphans` 调
        `detect_run_id()` 补上的。

        所以这里必须真的返回点什么，否则界面上那一栏对自研引擎永远是空的。

        实现：本模块在 `run_dir/agent_run/` 下写一个 `run.json`，
        取它的 `run_id` 字段。文件不存在 → 返回 `None`（还没开始跑）。
        """
        meta = run_dir / "agent_run" / "run.json"
        if not meta.is_file():
            return None
        try:
            import json

            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 读不到或格式坏了都不是致命错误 —— run_id 是诊断字段，
            # 为它让整条回收路径失败是本末倒置。
            logger.warning("读取运行标识失败：%s", meta, exc_info=True)
            return None
        value = data.get("run_id")
        return str(value) if value else None

    def launch(self, spec: LaunchSpec) -> LaunchResult:
        """起一个线程跑引擎，立刻返回句柄。"""
        spec.run_dir.mkdir(parents=True, exist_ok=True)
        (spec.run_dir / "agent_run").mkdir(exist_ok=True)
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)

        # 续跑：本后端没有这个能力（见类 docstring 的第 2 条）。
        # **明说而不是静默忽略** —— 用户点了「续跑」却得到全新扫描，
        # 那是必须被告知的事。
        if spec.resume_run_id:
            logger.warning(
                "扫描 %s 请求续跑（run_id=%s），但自研引擎后端不支持续跑，"
                "本次将从头开始",
                spec.scan_id,
                spec.resume_run_id,
            )

        handle = _TaskHandle(
            thread=threading.Thread(target=lambda: None),  # 占位，下面重建
            pid=0,
            started_at=time.time(),
        )

        def body() -> None:
            exit_code = EXIT_ERROR
            try:
                exit_code = asyncio.run(_run_engine(spec, handle.stop_event))
            except BaseException:  # noqa: BLE001 - 包括 CancelledError
                logger.exception("扫描 %s 的引擎线程异常退出", spec.scan_id)
                exit_code = EXIT_ERROR
            finally:
                handle.exit_code = exit_code

        thread = threading.Thread(
            target=body,
            name=f"suvex-agent-scan-{spec.scan_id[-8:]}",
            daemon=True,
        )
        handle.thread = thread
        handle.pid = thread.ident or 0  # start() 之后才有 ident，先占位
        thread.start()
        handle.pid = thread.ident or 0

        return LaunchResult(
            # ⚠️ `pid=0`，**不是** handle.pid。
            #
            # `LaunchResult.pid` 会被 `service.launch_scan` 存进 `Scan.pid`，
            # 而 `Scan.pid` 在 `recover_orphans` 里会被喂给 `is_alive()` /
            # `terminate_tree()`。线程标识不是 OS pid —— 拿它去 `os.kill`
            # 可能探到、甚至杀掉不相干的进程（线程 id 往往是个小整数，
            # 与真实 pid 撞车的概率不低）。
            #
            # 传 0 之后：`is_alive(0)` 恒 False（见那里的 `pid <= 0` 守卫），
            # 于是孤儿回收会把它判成"进程已不在"，走 failed 分支而不是
            # 去杀一个不存在的 pid —— 这正是我们要的安全行为。
            # 线程的真实 ident 留在 `handle.pid` 里，只用于日志。
            pid=0,
            run_dir=spec.run_dir,
            log_path=spec.log_path,
            command=(),
            process=handle,  # type: ignore[arg-type]
        )


#: LiteLLM 认的 provider 前缀（`deepseek/`、`openai/`、`anthropic/`、`ollama/`）。
#: 只在**剥前缀**时用；真前缀表在 `app.providers.llm.LITELLM_PREFIXES`，
#: 这里刻意写一份字面量而不是 import 那张表 —— 这张表只增不减的风险很低，
#: 而且剥前缀是"尽力而为"，认不出就原样用（见 `_bare_model_name`）。
_KNOWN_ROUTE_PREFIXES: tuple[str, ...] = ("openai", "deepseek", "anthropic", "ollama")


def _now_iso() -> str:
    """当前时刻的 ISO 8601（UTC，带时区）—— 事件 `at` 字段用。

    与 `strix_events` 产出的事件同一口径（都是 `at` = 事件**发生**时刻，
    不是导入时刻），否则活动流按时间排序时两种后端的事件会错位。
    """
    from datetime import datetime

    return datetime.now(UTC).isoformat()


def _bare_model_name(model: str) -> str:
    """把 LiteLLM 路由前缀去掉，得到接入点真正认的裸 model 名。

    ## 为什么需要这一步（这是真跑踩出来的）

    `scans/service.build_spec` 给 `LaunchSpec.llm_model` 填的是 **LiteLLM 格式**
    （`openai/deepseek-v4.1-flash`），因为 strix 后端要把这个值原样塞进
    环境变量 `STRIX_LLM`，litellm 靠前缀路由。

    但自研引擎的 `LlmGateway` **不走 litellm**：它把 model 原样 POST 到
    接入点的 `/chat/completions`。带前缀就相当于对一个 OpenAI 兼容网关
    说"我要一个叫 `openai/deepseek-v4.1-flash` 的模型"，网关回
    `model_not_found`（HTTP 503）。实测原话：
    `No available channel for model openai/deepseek-v4.1-flash under group default`。

    ## 只剥一层，且只剥已知前缀

    `openai/gpt` → `gpt`；但一个本身带斜杠的模型名（某些自建网关的
    `org/model`）不能乱剥 —— 所以只认 `_KNOWN_ROUTE_PREFIXES` 里那几个，
    认不出就原样返回。这样对 `anthropic/claude-…` 也安全：LlmGateway 判断
    Anthropic 用的是 `is_anthropic(endpoint)`（看地址），不看 model 前缀，
    所以剥掉前缀不影响它走 Messages API。
    """
    head, sep, tail = model.partition("/")
    if sep and tail and head.strip().lower() in _KNOWN_ROUTE_PREFIXES:
        return tail
    return model


async def _run_engine(spec: LaunchSpec, stop_event: threading.Event) -> int:
    """一次扫描的引擎主流程。返回退出码。

    ⚠️ 这个函数在**独立线程的事件循环**里跑，不碰数据库 ——
    与 `ctf2.runner.solve_in_sandbox` 同一条纪律（那边按设计不碰 DB，
    因为它是 async 而 Session 是同步的）。事件的落库由调度器
    `_import_for` 那条路负责。

    ## 它做什么（就四步）

    1. `build_solve_spec_from_scan` 把 `LaunchSpec` 翻成引擎的 `SolveSpec`；
    2. 起沙箱容器（`SandboxContainer`），`finally` 里必定 destroy；
    3. 跑 `solve_in_sandbox`，用一个**旁路线程轮询 `stop_event`** 的方式
       实现"请求停止"（`notify` 吞异常，所以不能靠回调抛异常来停）；
    4. 把 `outcome.findings` 翻成 `vulnerabilities.json`（strix 记录形状），
       落在 `run_dir` 根 —— 这样既有的 `findings.importer` 一行不用改就能读。

    ## 为什么 findings 要落成 strix 的形状

    `findings/service.import_from_run` 读的是 `run_dir/vulnerabilities.json`
    （或 `run_dir/strix_runs/*/vulnerabilities.json`）。只要我们把引擎的
    `findings` 写成那个形状，**整条发现入库链路（含 SLA、时间线、CWE 映射）
    都复用现成代码**。这是"只加场景包、不动核心"在产出侧的具体体现。
    """
    # 延迟 import：`app.kernel.runner` 会拉起 openai/httpx 一大坨依赖，
    # 而本模块在 `scans.runner` 的模块级 `_RUNNERS` 里被引用 ——
    # 顶层 import 会让"只是想看看有哪些后端"变成"必须装好整条 LLM 栈"。
    from app.kernel import runner as ctf2_runner

    logger.info("自研引擎开跑：%s", spec.summary())

    try:
        # ★ v0.73：活收集器必须在**建 spec 之前**就存在，并挂在 spec 上 ——
        # 超时取消时 `_timeout_outcome(solve_spec)` 只能看到 spec，
        # 看不到这里函数的局部变量之外的任何东西。
        finding_sink: list[object] = []
        solve_spec = build_solve_spec_from_scan(spec, finding_sink=finding_sink)
    except Exception:  # noqa: BLE001
        logger.exception("扫描 %s：构造 SolveSpec 失败", spec.scan_id)
        return EXIT_ERROR

    # 沙箱名：与 ctf2 的命名规则区分（`ctf2-` 前缀），避免混淆清理逻辑。
    sandbox_name = f"suvex-scan-{spec.scan_id[-12:]}"
    sandbox = ctf2_runner.SandboxContainer(
        name=sandbox_name,
        image=ctf2_runner.image_for_profile(solve_spec.profile),
        network_isolated=solve_spec.network_isolated,
        # 渗透评估走专用网桥（全 NAT 出站、与平台库/CTF 靶站隔离），
        # **不走** `suvex-ctf2` 的靶站白名单 —— 那个会把任意授权目标挡在
        # 外面。判据与实测见 `PENTEST_NETWORK` 与 `SandboxContainer.network_name`。
        # 注意：`network_isolated=True`（本地 repo/path 目标）时这一项被
        # `_network_arg()` 忽略，容器仍走 `--network none`。
        network_name=ctf2_runner.PENTEST_NETWORK,
        cpus=solve_spec.cpus,
        memory_mb=solve_spec.memory_mb,
        workspace_dir=spec.run_dir / "agent_run" / "workspace",
        # ★ v0.64：设置页「允许写入挂载卷」。透传到 SandboxContainer，
        #   由它决定 /work/persist 挂 :rw 还是 :ro。
        volume_write=bool(getattr(spec, "allow_volume_write", True)),
    )

    log_lines: list[str] = []

    # 结构化事件流的旁路状态：`seq` 只增不减（幂等键），文件句柄开一次。
    events_path = spec.run_dir / "agent_run" / EVENTS_FILENAME
    event_seq = 0

    def on_progress(kind: str, summary: str, payload: dict[str, str]) -> None:
        """过程落盘。

        **不碰 DB** —— 见函数 docstring（引擎跑在线程里、没有可控的 db 会话）。
        但除了给人看的 `scan.log`，**还要**写一行结构化事件到
        `agent_run/events.jsonl`：导入器按 `seq` 去重后灌进 `activity_events`，
        活动流 / 智能体名册 / 实时 SSE 才活得起来（详见 `EVENTS_FILENAME`）。

        ⚠️ 这里**只追加、不查重**：去重交给导入器靠 `source_row_id` 唯一索引做，
        同 `importer._persist` 的理由 —— 先查后写在并发下是有洞的。
        """
        nonlocal event_seq
        log_lines.append(f"[{kind}] {summary}")
        # 边跑边写，便于 `tail -f` 看进度。
        with contextlib.suppress(OSError):
            spec.log_path.write_text("\n".join(log_lines), encoding="utf-8")

        # ★ 结构化事件（写失败不影响引擎跑，但记 warning 而不是静默）。
        event_seq += 1
        record = {
            "seq": event_seq,
            "kind": kind,
            "summary": summary,
            "payload": payload,
            "at": _now_iso(),
        }
        try:
            events_path.parent.mkdir(parents=True, exist_ok=True)
            with events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("扫描 %s：写结构化事件失败（不影响引擎继续跑）", spec.scan_id, exc_info=True)

    gateway = ctf2_runner.LlmGateway(
        model=_bare_model_name(solve_spec.llm_model),
        api_key=solve_spec.llm_api_key,
        base_url=solve_spec.llm_api_base,
        label=solve_spec.llm_label,
        kind=solve_spec.llm_kind,
    )
    system_prompt = ctf2_runner.build_system_prompt_by_spec(solve_spec)

    # ★ v0.57 P2 人机协同：每轮开始时问一次「人有没有补线索」。
    #
    # 引擎主循环不碰 DB（见本函数 docstring），而 `hint_provider` 是个
    # 同步无参回调 —— 它在本线程里被 `solve_in_sandbox` await 期间同步调用。
    # 这个线程**独占**自己的事件循环（不是服务的那个），所以在这里同步查库
    # 不会阻塞任何人；但会话必须**每次新开、用完即关**：跨轮复用一个
    # 长会话会把连接钉住一整次扫描（可能几十分钟），把连接池耗干。
    def _hint_provider() -> str | None:
        from app.core.db import get_session_factory
        from app.scans import service as scans_service

        factory = get_session_factory()
        session = factory()
        try:
            hint = scans_service.pending_hint(session, spec.scan_id)
            if hint is None:
                return None
            session.commit()
            return hint.text
        except Exception:  # noqa: BLE001 - 取线索失败不该让跑了三十轮的扫描停摆
            session.rollback()
            logger.warning("扫描 %s：读取人工线索失败，本轮跳过", spec.scan_id, exc_info=True)
            return None
        finally:
            session.close()

    try:
        sandbox.start()
    except Exception:  # noqa: BLE001
        logger.exception("扫描 %s：沙箱启动失败", spec.scan_id)
        return EXIT_ERROR

    try:
        # ⚠️ `await`，不是 `asyncio.run`。
        #
        # `_run_engine` 本身是 `async def`，它的调用方
        # （`launch` 里的 `body()`）已经用 `asyncio.run(_run_engine(...))`
        # 把它放进一个事件循环里了。这里再套一层 `asyncio.run` 会抛
        # `RuntimeError: asyncio.run() cannot be called from a running event loop`。
        #
        # ★ 这个 bug 真实存在过：单测只覆盖了纯函数（spec 翻译 / 写文件），
        # 从没真执行过 `_run_engine`，所以全绿；直到第一次真跑
        # （对 Kali 本机 nginx）才炸出来。教训同那条「一份声明不是一条检查」——
        # **纯函数测试通过不等于这条路径跑得通**。
        outcome = await _solve_with_stop(
            solve_spec,
            sandbox=sandbox,
            gateway=gateway,
            system_prompt=system_prompt,
            stop_event=stop_event,
            on_progress=on_progress,
            hint_provider=_hint_provider,
            timeout_seconds=getattr(spec, "timeout_seconds", None),
        )
    finally:
        # ★ 唯一的容器清理路径（见模块 docstring「线程杀不掉」一节）。
        try:
            sandbox.destroy()
        except Exception:  # noqa: BLE001
            logger.warning("扫描 %s：沙箱销毁失败", spec.scan_id, exc_info=True)

    # 写运行标识（`detect_run_id` 读它）。
    _write_run_json(spec, solve_spec, outcome)

    # 写发现（strix 记录形状）—— 既有 importer 直接能读。
    _write_vulnerabilities(spec, outcome)

    if outcome.error and not outcome.findings:
        # 有错误、且一条发现都没有 → 报错退出。注意：跑了但真的没发现
        # **不是**错误（那是一次干净的评估），所以只在 error 非空时才算。
        #
        # ★ v0.64：把这句话**写进 scan.log**。`_failure_reason` 是从日志里
        #   取可行动的那一行的（见它的 docstring），如果只 `logger.warning`
        #   到服务日志，用户界面上就只剩一句空泛的"没有产出"——超时/引擎
        #   报错这些真正的原因反而看不到。写成 `error: <人话>` 形状，
        #   正好能被 `_ERROR_LINE` 认出来提到最前。
        with contextlib.suppress(OSError):
            log_lines.append(f"error: {outcome.error}")
            spec.log_path.write_text("\n".join(log_lines), encoding="utf-8")
        logger.warning("扫描 %s：引擎无产出（%s）", spec.scan_id, outcome.error)
        return EXIT_ERROR
    return EXIT_OK


async def _solve_with_stop(
    solve_spec: object,
    *,
    sandbox: object,
    gateway: object,
    system_prompt: str,
    stop_event: threading.Event,
    on_progress,
    hint_provider=None,
    timeout_seconds: int | None = None,
) -> object:
    """跑 `solve_in_sandbox`，并在 `stop_event` 被设置或墙钟超时时取消它。

    ## 为什么不能只在回调里检查 stop_event

    `solve_in_sandbox` 里的 `notify()` **吞掉回调抛的异常**（见那里
    `except Exception: logger.warning`），所以从 `on_progress` 抛异常
    停不下来。把协程包成一个 task，用旁路 `while` 轮询 `stop_event`，
    一旦置位就 `task.cancel()` —— 这是 Python 里对 async 循环唯一
    可靠的"请求停止"手段（线程本身杀不掉，见模块 docstring）。

    ## 墙钟超时（v0.64）

    `timeout_seconds` 非空时按单调时钟设一个 deadline。到点就取消引擎
    循环，并把 `solve_spec.timeout_reason` 置成一句人话（供上层写进
    运行结果）。用 `time.monotonic()` 而不是 wall-clock：设备改时间 /
    校时不该影响"给了它半小时"这个承诺。
    """
    import time as _time

    from app.kernel import runner as ctf2_runner

    deadline = (_time.monotonic() + timeout_seconds) if timeout_seconds else None

    task = asyncio.ensure_future(
        ctf2_runner.solve_in_sandbox(
            solve_spec,  # type: ignore[arg-type]
            sandbox=sandbox,  # type: ignore[arg-type]
            gateway=gateway,  # type: ignore[arg-type]
            system_prompt=system_prompt,
            on_progress=on_progress,
            hint_provider=hint_provider,
        )
    )
    timed_out = False
    while not task.done():
        # 每 0.5s 查一次停止标志/超时，顺便把控制权交还给事件循环。
        await asyncio.sleep(0.5)
        if stop_event.is_set() and not task.done():
            logger.info("收到停止请求，取消引擎循环")
            task.cancel()
            break
        if deadline is not None and _time.monotonic() >= deadline and not task.done():
            logger.info("扫描超时（%ss），取消引擎循环", timeout_seconds)
            timed_out = True
            # 记一句人话到 spec 上，供 `_run_engine` 写进运行结果。
            with contextlib.suppress(Exception):  # 记账失败不该影响停止动作
                solve_spec.timeout_reason = (  # type: ignore[attr-defined]
                    f"扫描超过设置页指定的单次超时（{timeout_seconds // 60} 分钟），已被引擎停止。"
                )
            task.cancel()
            break
    try:
        return await task
    except asyncio.CancelledError:
        # 超时导致的取消是"预期内的干净收尾"：不把 CancelledError 上抛
        # （否则 `_run_engine` 会走异常分支、拿不到已产出的部分发现），
        # 而是交回一个最小 outcome，让上层按"超时但可能有部分产出"处理。
        if timed_out:
            logger.info("引擎循环因墙钟超时被取消，按超时收尾")
            return _timeout_outcome(solve_spec)
        logger.info("引擎循环已被取消")
        raise
    except Exception:
        # 引擎内部异常：如实上抛，由 `_run_engine` 的调用方记日志。
        raise


def _timeout_outcome(solve_spec: object) -> object:
    """超时被取消时返回的 outcome：人话原因 + **已确认发现的部分收割**。

    ## ★ v0.73：这里以前是"空的发现集合"，那是一个 0 入库事故的元凶

    旧版注释写的是"已经跑出来的部分发现尚未归拢到返回值里，这里如实
    不假装有"。出发点（不编造）是对的，但结论错了：不假装有 ≠ 必须丢掉。

    实测（2026-10-22，P1 对照 agent 侧）：一次 500 轮上限的渗透扫描跑到
    60 分钟墙钟超时被取消，事件流里 337 次 tool_call、子智能体已经确认
    6 条漏洞，而入库结果 **findings=0**。P0 那次 1802 秒被 30 分钟墙截断
    导致 findings=0，是**同一个机制**——不是"没找到"，是"找到了没交出去"。

    根因是收尾形状：`outcome.findings` 只在模型写出 `FINAL: {...}` 那一刻
    才被赋值，而跑满几百轮的多智能体编排几乎收不到 FINAL（root 一直在等
    子智能体回报）。`task.cancel()` 之后局部 `outcome` 就没了，于是所有
    已经结构化的发现一起消失。

    现在改成从 `solve_spec.finding_sink` 里收割。它**只**包含模型自己用
    `FINAL.findings` 明确交出来的结构化对象（见 `SolveSpec.finding_sink`
    的说明与 `_sink_findings`），不从自然语言、笔记、命令输出里猜 ——
    所以"不编造"这条纪律没有被牺牲，只是不再白白丢弃。
    """
    from app.kernel import runner as ctf2_runner

    reason = getattr(
        solve_spec,
        "timeout_reason",
        "扫描超过设置页指定的单次超时，已被引擎停止。",
    )
    harvested = list(getattr(solve_spec, "finding_sink", None) or [])
    outcome = ctf2_runner.SolveOutcome(error=reason)
    if harvested:
        # 去重靠标题，与 `multiagent._merge_outcomes` 同一条口子：
        # 同一个子智能体可能重复提交同一条，合并时只留第一条。
        seen: set[str] = set()
        unique: list[object] = []
        for item in harvested:
            title = ""
            if isinstance(item, dict):
                title = str(item.get("title") or item.get("name") or "")
            if not title:
                title = repr(item)[:120]
            if title in seen:
                continue
            seen.add(title)
            unique.append(item)
        outcome.findings = unique
        logger.info(
            "扫描超时收尾：从活收集器收割到 %d 条已确认发现（去重后）", len(unique)
        )
    return outcome


def _write_run_json(spec: LaunchSpec, solve_spec: object, outcome: object) -> None:
    """写 `run_dir/agent_run/run.json`（`detect_run_id` 读它）。"""
    import json

    meta = {
        "run_id": f"agent-{spec.scan_id}",
        "model": getattr(solve_spec, "llm_model", ""),
        "turns": getattr(outcome, "turns", 0),
        "tool_calls": getattr(outcome, "tool_calls", 0),
        "llm_calls": getattr(outcome, "llm_calls", 0),
        "error": getattr(outcome, "error", None),
    }
    path = spec.run_dir / "agent_run" / "run.json"
    try:
        # 目录可能不存在（`run_dir/agent_run/` 由谁创建不归这里管），
        # 先建再写 —— 否则真跑时 FileNotFoundError 被 `except OSError` 吞成
        # 一句 warning，run.json 静默缺失，下游靠它判断「引擎真的跑了」的
        # 逻辑就全落空。
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        logger.warning("写运行标识失败：%s", path, exc_info=True)


#: 严重度归一：模型可能给中文或大小写混用，统一到 strix 用的英文小写词。
_SEVERITY_ALIASES: dict[str, str] = {
    "critical": "critical",
    "严重": "critical",
    "致命": "critical",
    "high": "high",
    "高": "high",
    "高危": "high",
    "medium": "medium",
    "中": "medium",
    "中危": "medium",
    "low": "low",
    "低": "low",
    "低危": "low",
    "info": "info",
    "信息": "info",
    "提示": "info",
}


def _normalize_severity(value: object) -> str:
    """把 `severity` 归一到 strix 认的六档。认不出 → `info`（宁低不高）。"""
    text = str(value or "").strip().lower()
    return _SEVERITY_ALIASES.get(text, "info")


def _normalize_verification(value: object) -> str:
    """把 `verification` 归一到三档证据分级。认不出 → `needs_follow_up`。

    与场景包通用规则一致：拿不准就归待跟进，不为了给结论而硬判。
    """
    text = str(value or "").strip().lower()
    if text in ("confirmed", "ruled_out", "needs_follow_up"):
        return text
    if text in ("确认", "已确认", "成立"):
        return "confirmed"
    if text in ("排除", "已排除", "不成立"):
        return "ruled_out"
    return "needs_follow_up"


def _write_vulnerabilities(spec: LaunchSpec, outcome: object) -> None:
    """把引擎的 `findings` 写成 `run_dir/vulnerabilities.json`（strix 记录形状）。

    ## 形状来源

    只写 `findings.importer` 真正会读的那些字段（见那个模块的解析）：
    `title` / `severity` / `confidence` / `cwe` / `cvss` / `target` /
    `description` / `technical_analysis` / `evidence` / `poc_script_code` /
    `remediation_steps` / `code_locations` / `verification`。

    多写的字段 importer 会忽略，所以不追求"一个字节不差"；但**一条
    发现一个对象**、`severity` 与 `verification` 用对词，是必须的 ——
    否则它们会被默认值吞掉（`info` / `needs_follow_up`），而"我的高危
    怎么变成 info 了"是很难回查的。

    ## 只写 `findings`，不写 `candidates`

    `candidates` 是 flag（CTF 判据用的），渗透评估里没有它。
    """
    import json

    raw = getattr(outcome, "findings", None)
    records: list[dict[str, object]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            target = str(item.get("target") or "").strip()
            if not title and not target:
                # 一条既没标题又没位置的"发现"不是发现 —— 丢掉它比
                # 造一条空壳更诚实（空壳会让统计里多一条无意义记录）。
                continue
            verification = _normalize_verification(item.get("verification"))
            records.append(
                {
                    "title": title,
                    "severity": _normalize_severity(item.get("severity")),
                    # ⚠️ `confidence` 的合法取值是 high|medium|low（见
                    # `findings/strix_vulns.py` 的 `_CONFIDENCE_TO_VERIFICATION`），
                    # 它会被翻成证据强度轴 `verification`：
                    #   confirmed → high（verified）
                    #   其余（ruled_out / needs_follow_up）→ medium（partial）
                    # 不从 needs_follow_up 直接落 low：我们确实跑过验证，
                    # 只是没跑出结论，不是"没验证过"。
                    "confidence": "high" if verification == "confirmed" else "medium",
                    "cwe": str(item.get("cwe") or ""),
                    "cvss": None,
                    "target": target,
                    "description": str(item.get("reasoning") or ""),
                    "technical_analysis": str(item.get("evidence") or ""),
                    "evidence": str(item.get("evidence") or ""),
                    "poc_script_code": "",
                    "remediation_steps": str(item.get("remediation") or ""),
                    "code_locations": "",
                    # 自带一份归一后的分级，便于日志/对账（importer 不读它）。
                    "verification": verification,
                    # 记录一条来源标记，便于将来对账"这条来自哪个后端"。
                    "discovered_by": "agent",
                }
            )
    path = spec.run_dir / "vulnerabilities.json"
    try:
        path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        logger.warning("写发现文件失败：%s", path, exc_info=True)


def build_solve_spec_from_scan(
    spec: LaunchSpec, *, finding_sink: list[object] | None = None
) -> object:
    """把 `LaunchSpec` 翻译成引擎的 `SolveSpec`。

    `finding_sink`（v0.73）是可选的活收集器：传进来就挂在 `SolveSpec` 上，
    引擎每解析出一批 `FINAL.findings` 就登记进去，超时取消时由
    `_timeout_outcome()` 收割 —— 见那个函数与 `SolveSpec.finding_sink` 的说明。
    默认 `None` = 不收集，本函数对其余字段的翻译**逐字节不变**。

    ## 为什么不直接让引擎吃 `LaunchSpec`

    两者形状相近但语义不同：

    | 概念 | `LaunchSpec`（scans） | `SolveSpec`（引擎） |
    | --- | --- | --- |
    | 目标 | `target` + `target_kind`（5 种形态） | `access_urls`（只认 URL） |
    | 判据 | `goal`（3 种） | 隐含"拿到 flag" |
    | 附件 | 无（走 `--workspace-file`） | `attachments: tuple[Path, ...]` |
    | 场景开关 | `enable_offensive_prompt` | `agentsec` / `ir` / `offensive` / `pentest` |

    强行合并会让引擎那一侧长出五个扫描专属字段，而它还有两道自己的
    入口（CTF2 平台题、IR 本地复盘）。所以这里是**翻译**，不是合并。

    ⚠️ 这个函数是纯函数（只读 `LaunchSpec`），不碰数据库 ——
    调用点在 `_run_engine` 里，那里同样没有 session。
    """
    from app.core.vocab import PentestCategory, ScanMode
    from app.kernel import runner as ctf2_runner

    # ── 目标 → access_urls ────────────────────────────────
    #
    # 引擎的 `http_request` 工具靠 `access_urls` 做白名单（`guard_tool_url`
    # → `allowed_hosts`）。同时 `allowed_hosts` 会把 target 加进"允许访问
    # 的主机"里，所以只要 target 是 URL 形态，http_request 就能打它。
    #
    # ⚠️ 非 URL 形态（repo / path / artifact）目前**不进 access_urls** ——
    #    repo/path 是本地代码，artifact 是附件，它们靠 `run_shell` 在容器
    #    里处理，不需要 http_request 白名单。service（TCP）形态也先不加：
    #    裸 host:port 的白名单校验依赖 `_with_scheme` 补 http://，而
    #    `nc`/`nmap` 走 run_shell 不受这条限制 —— 加进去反而可能误挡。
    access_urls: tuple[str, ...] = ()
    if spec.target_kind in ("url", "service") and spec.target:
        access_urls = (spec.target,)

    # ── 是否隔离网络 ──────────────────────────────────────
    #
    # 渗透评估**必须**能连到目标 —— 这不是可选项，是前提。但这里有两个
    # 独立的问题，原来混成了一个：
    #
    #   ① settings 里 `sandbox.network_isolated` 说要不要隔离；
    #   ② 这条路径的**场景**本身能不能在没有网的情况下完成。
    #
    # 原实现只看了 ①（`network_isolated = bool(spec.sandbox_network_isolated)`），
    # 于是和 ctf2 侧不一致：ctf2 的 `build_spec` 用的是
    # `network_isolated = not needs_raw_network(category)` —— **按场景判**，
    # web/pwn 一律开网。扫描侧漏了这道判断（真 bug）。
    #
    # 后果（实测）：settings 一旦配了隔离，或环境像 Kali 那样被 egress
    # 白名单卡住，一次 web 渗透就被关进 `--network none`，镜像里 98 个
    # 工具（nmap/ffuf/gobuster/sqlmap…）全连不上目标，96% 的动作只能退化
    # 成宿主侧 http_request 逐个猜 URL。工具宽度被物理废掉。
    #
    # 修法：**渗透评估一律要联网**（`pentest-evaluation` 不在
    # `needs_raw_network` 的 CTF 题型白名单里，但它的语义比任何 CTF 题都
    # 更依赖真实网络）。所以这里取"settings 想隔离也不能隔离"——
    # 只有当目标根本不是网络目标（repo/path/artifact 这些本地形态）时才
    # 允许隔离。url/service 形态强制开网。
    #
    # ⚠️ 安全性不靠这一行：容器放开网络后，出站边界仍由 iptables 的
    #    `DOCKER-USER` 白名单收窄（见 `deploy/ctf2-egress-rules.sh`）。
    #    这一行的作用是"别把已经配好的网络又关回去"。
    is_network_target = spec.target_kind in ("url", "service")
    network_isolated = bool(spec.sandbox_network_isolated) and not is_network_target

    # ── 判据/场景开关 ─────────────────────────────────────
    #
    # 分类固定 `pentest-evaluation`（前缀 `pentest-`）——这样
    # `build_system_prompt_by_spec` 会走 `is_pentest_category` 分支，
    # 追加 `kernel_pentest.md`，而不是 CTF 解题块。
    #
    # `offensive` 也打开：渗透评估要的不是"排除"手法，而是"用全手法
    # 打一遍再判"。手法块对 pentest 分类**照常生效**（它不是被排除的
    # 场景包 —— 见 `build_system_prompt` 的排除只针对 agentsec/ir）。
    # ── 回合预算：由「扫描深度」与「回合上限」两个设置共同决定 ──────
    #
    # 原来这里写死 `min(scan_max_turns, 80)`，于是：
    #   ① 用户在设置页把「回合上限」调到 200/500，实际永远只有 80 ——
    #      界面说了算的是个假数（填了不生效，是静默截断）；
    #   ② 「扫描深度」（quick/standard/deep）对自研引擎**完全没被读取**，
    #      是个死配置（只在 strix 的 argv 里用过）。
    #
    # 现按「深度定档、上限定细」两条分工：
    #   · 扫描深度给出**档位天花板**（quick 40 / standard 80 / deep 150）——
    #     这是"这次要挖多深"的粗粒度语义，也是成本护栏；
    #   · 回合上限是用户更细的旋钮，取二者较小值 —— 用户可以说"标准深度
    #     但只给我 50 轮"，但不能靠填 500 突破 deep 也才 150 的天花板。
    # 这样两个旋钮都真有行为，界面上写的与实际一致（不再需要免责声明）。
    _DEPTH_CEILING = {
        ScanMode.QUICK.value: 40,
        ScanMode.STANDARD.value: 80,
        ScanMode.DEEP.value: 150,
    }
    # 认不出的深度取值退回 standard 的天花板（不静默当成 deep 烧钱）。
    depth = (spec.scan_mode or ScanMode.STANDARD.value).strip().lower()
    ceiling = _DEPTH_CEILING.get(depth, _DEPTH_CEILING[ScanMode.STANDARD.value])
    requested = int(spec.scan_max_turns or ceiling)
    # 下限 10 轮（少于它基本做不完任何评估），上限取档位天花板。
    max_turns = max(10, min(requested, ceiling))

    # ── 续跑：从磁盘上的留痕还原"做过什么" ────────────────
    #
    # 与 strix 的 run_id 断点续跑不同：这里**不接内存态**，而是读上一次跑
    # 留下的 `agent_run/events.jsonl`，把 tool_call 事件的 summary（形如
    # `run_shell curl ...`）抽成一份"动作纪要"，作为 `resume_digest` 注入初始
    # 上下文（`solve_in_sandbox` 会把它拼成一条"这是续解，不是从头开始"的
    # 消息）。代价：丢了精确对话史；收益：真的能从留痕接着做，而不是白扔半小时。
    resume_digest: tuple[str, ...] = ()
    if getattr(spec, "resume", False):
        resume_digest = _digest_from_events(spec.run_dir)

    return ctf2_runner.SolveSpec(
        challenge_id=spec.scan_id,
        title=f"对 {spec.target} 做安全评估",
        category=PentestCategory.EVALUATION.value,
        description=spec.brief or f"对目标 {spec.target}（{spec.target_kind}）执行一次授权安全评估。",
        profile=ctf2_runner.image_for_profile(ctf2_runner.KernelSandboxProfile.BASE.value),
        access_urls=access_urls,
        attachments=(),
        llm_model=spec.llm_model,
        llm_api_key=spec.llm_api_key,
        llm_api_base=spec.llm_api_base,
        llm_kind="cloud",
        mode="safe",
        max_turns=max_turns,
        network_isolated=network_isolated,
        cpus=spec.sandbox_cpus,
        memory_mb=spec.sandbox_mem_limit_mb,
        brief=spec.brief or "",
        pentest=True,
        offensive=bool(spec.enable_offensive_prompt),
        # ★ v0.70：续跑的动作纪要（空 = 全新开始，行为逐字节不变）。
        resume_digest=resume_digest,
        # ★ v0.58：多智能体编排开关（与 CTF 侧同一个配置项）。这个函数是
        # 纯函数（不碰库），所以值来自 `LaunchSpec.multi_agent`，由调用方
        # `_run_engine` 在建 spec 前从配置读出并写进 LaunchSpec。
        multi_agent=bool(getattr(spec, "multi_agent", False)),
        # ★ v0.73：活收集器（None = 不收集，行为与旧版一致）。
        finding_sink=finding_sink,
    )


#: 续跑纪要最多取多少条工具动作。与 kernel 的 `MAX_DIGEST_LINES` 同量级 ——
#: 纪要是**索引**不是正文，太多会把预算吃掉、反而让模型"忘记题目要求"。
MAX_RESUME_DIGEST_LINES = 60


def _digest_from_events(run_dir: Path, *, limit: int = MAX_RESUME_DIGEST_LINES) -> tuple[str, ...]:
    """从 `agent_run/events.jsonl` 还原动作纪要（跑过哪些工具 + 参数开头）。

    ⚠️ 只读 tool_call 事件。reasoning 不进纪要 —— 纪要是"我做过什么"，
    不是"我想过什么"；把推理混进去会让模型以为那些结论已被验证过。

    读失败/文件不在 → 返回空 tuple（= 当成全新扫描，**不抛**）。这与
    `latest_transcript` 的容错一致：续跑读不到留痕是"降级为从头开始"，
    不是"这次扫描失败"。
    """
    path = run_dir / "agent_run" / "events.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        logger.info("续跑：读不到 %s，本次按全新扫描处理", path)
        return ()

    actions: list[str] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except (ValueError, TypeError):
            # 半行/坏行跳过 —— 引擎可能在两次写之间被杀，最后一行常常是断的。
            continue
        if not isinstance(record, dict):
            continue
        if record.get("kind") != "tool_call":
            continue
        summary = str(record.get("summary") or "").strip()
        if summary:
            actions.append(" ".join(summary.split())[:140])

    # 取**最后** `limit` 条：续跑最关心"刚跑到哪"，早期动作价值已沉淀进证据包。
    return tuple(actions[-limit:])
