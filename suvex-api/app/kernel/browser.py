"""`browser` 工具：在沙箱里驱动一个真实 Chromium（宿主侧调度）。

## 它补的是什么缺口

没有这个工具时，模型只有 `run_shell` / `http_request`，而有一整类
Web 题的东西**原理上**用它们看不到：

  · SPA / 前端路由 —— `curl` 只拿到 `<div id="app"></div>`；
  · 客户端加密后再提交的口令、token 生成；
  · XSS 是否真的触发（要真渲染才知道 payload 有没有执行）；
  · 靠 JS 做的跳转、`document.write` 拼出来的隐藏端点。

这些不是"慢一点"，而是"拿不到"。

## ★ 为什么是 agent-browser，而不是自己写 CDP

上一版（同一轮里先写的）是 `browser_driver.py`：纯标准库手写 CDP。
它被**删掉了**，理由有两条，都很实在：

1. **它在本机一次都没跑过。** 宿主没有 docker，所以那份驱动交付时是
   零验证状态。而 CDP 的会话初始化 / target 附着 / 事件流都有非平凡
   时序 bug —— 没跑过的浏览器驱动**必定**有 bug。
2. **`agent-browser` 已经实测跑通。** 在本机用 Chrome 跑过完整链路：
   `open` → `snapshot -i` → `fill @e3` → `click @e2` → `get text`
   真的拿到了表单提交结果（`submitted:a@b.com`），说明 JS 真执行了。

判断依据是"哪个真的被验证过"，不是"哪个是我们自己写的"。

## ★ 三个已实测确认的约束（任何一条被忽略都会做出假工具）

### 1. `screenshot` 不是主路径，但配上 `view_image` 之后**是一条真路径**

历史上这里写的是"模型没有视觉能力，所以截图只能存文件"。那是当时的
事实（`dispatch_tool` 的返回值恒为 `str`，消息 `content` 恒为 `str`），
代价是截图这个能力等于不存在。

v1.04 起内核有了 `view_image`：图片走**旁路**（`ImagePayload` 收集器）
挂到下一轮消息上，`content` 变成 `[{type:text}, {type:image_url}]`。
Kali 实测该端点支持视觉（base64 送图 → 200，`prompt_tokens` 31 → 223）。

即便如此 `snapshot` 仍是**默认**：它 200-400 token 就给出整页可操作
结构，而一张图要一千多 token。看结构用 snapshot，看**外观**（错位、
重叠、图表、验证码、UI 是否真的渲染成那样）才截图 + `view_image`。

### 2. ref（`@eN`）每次快照重新分配，页面一变就失效

这是 agent-browser 的硬约束，也是模型最容易踩的坑：点了按钮触发导航后
再 `click @e2`，那个 ref 可能指向别的元素、也可能报 `Unknown ref`。
所以 ref 失效时要明确说"重新 snapshot"，而不是含糊的"元素找不到"。

### 3. 退出码是权威判据（实测）

成功 → `0`；`✗ Unknown ref: e99` / `✗ Element not found` → **`1`**。
所以宿主侧靠 `result.ok` 判断成败，不用去解析文本里的 `✓`/`✗`。
⚠️ 但**错误消息走 stderr**（实测：`2>/dev/null` 之后 stdout 是空的），
所以取消息必须用 `combined()`。

## 为什么每次调用是个独立进程，状态却还在

`agent-browser` 是 **daemon + client** 结构：第一次 `open` 会拉起
Chrome 并在 `AGENT_BROWSER_SOCKET_DIR` 下留一个 socket，后面每条命令
都是新进程连那个 socket。所以"开页面 → 看 → 点 → 再看"能接着做。

⚠️ 这带来一个必须处理的事：`/work` 是 tmpfs，容器重启后 socket 与
profile 一起消失。所以每次调用都 `mkdir -p` 那几个目录（幂等、几十毫秒），
换掉"有时候能跑有时候不能"的整类故障。

## 输出契约（给模型的话必须能指导下一步）

工具返回的每一句都要回答"那我该干什么"：
  · ref 失效 → 明说"重新 `snapshot -i`"；
  · 截图 → 给出 `view_image` 的**完整调用**（路径写进去，不要让它去猜）；
  · 浏览器没起 → 明说"先 `browser open <url>`"。
"""

from __future__ import annotations

import itertools
import logging
import shlex
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from app.kernel.runner import SandboxContainer

logger = logging.getLogger(__name__)

#: 容器内可执行名（Dockerfile 里 `ln -sf` 到 `/usr/local/bin`）。
BIN: Final = "agent-browser"

#: 所有可写目录的根。必须是 `/work`（tmpfs）之下 —— 容器根是
#: `--read-only`，往别处写会报 "Failed to create a ProcessSingleton"，
#: 那一句看起来像权限问题，实际是"只读根"。
WORK_ROOT: Final = "/work/.browser"

#: 子命令表。**与 agent-browser 自己的 CLI 对齐**，不是我们发明的名字 ——
#: 这样模型从它的 `skills get core` 里学到的用法可以直接迁移。
#:
#: 只暴露"解题常用"的那一部分。它完整 CLI 有 60+ 个子命令
#: （`dblclick` / `hover` / `drag` / `upload` / `pdf` / `find` / `is` …），
#: 全部转出去有两个代价：工具的 params 说明会膨胀到没人读，而模型
#: 90% 的时间只需要这几条。少暴露不等于做不到 —— `run_shell` 里
#: 直接敲 `agent-browser <任何子命令>` 一样能跑，那是留给少数派的后门。
SUBCOMMANDS: Final[tuple[str, ...]] = (
    "open",
    "snapshot",
    "click",
    "fill",
    "type",
    "press",
    "get",
    "eval",
    "wait",
    "screenshot",
    "back",
    "close",
    "skills",
)

#: 默认超时（秒）。比 `run_shell`（300）短：浏览器命令应该很快，
#: 慢的是首次启动 Chrome（冷启动实测几秒）。
DEFAULT_TIMEOUT: Final = 60

#: 首次启动 Chrome 要更久（要建 profile、初始化渲染进程）。
#: `open` 单独给这个值 —— 实测本机冷启动约 2-4 秒，容器里按 3 倍留。
OPEN_TIMEOUT: Final = 90

#: 交给模型的输出上限。与 `runner.MAX_TOOL_OUTPUT_CHARS`（8000）保持
#: 一个量级：`snapshot` 在复杂页面上会很啰嗦，而页面正文本来就该
#: 用 `get text` 单独取，不该挤在快照里。
MAX_OUTPUT: Final = 7000

#: 截图的默认目录。**必须在 `/work` 之下**（`view_image` 的白名单是
#: `/work/*`），又**不能**在 `WORK_ROOT/profile` 里（那是 Chrome 的
#: user-data-dir，往里塞东西会被当成配置读）。
SHOTS_DIR: Final = f"{WORK_ROOT}/shots"

#: 截图文件名的自增序号。同一容器里连续截图不能互相覆盖 ——
#: 覆盖了的话"截第二张"和"第一张还在不在"就分不清，而模型经常
#: 截完一张再截一张去比对。
_shot_counter = itertools.count(1)

#: 调 `agent-browser` 时前置的 `HOME` 赋值（见 `_invoke` 的说明）。
#: 单独提成常量是因为**测试要按它认命令**：前缀一变，
#: `command.startswith(BIN)` 这类断言就会静默失配。
HOME_ENV: Final = f"HOME={WORK_ROOT}/home"


def _shot_path() -> str:
    """给这次截图挑一个**可预测**的文件名。

    为什么不让 agent-browser 自己命名：它不给路径时会在自己的默认目录
    里造一个形如 `screenshot-1729....png` 的名字，再把路径打一行到
    stdout。对人够用，对模型不够 —— 它得从一坨输出里把路径抠出来
    再拼进 `view_image` 的 `path`，抠错一次就白烧一轮。

    ⚠️ 序号是**进程级**自增，不是"扫描目录取最大"。理由：扫盘要一次
    `docker exec`（几十毫秒）只为取个名字；而容器重启后目录是空的
    （`/work` 是 tmpfs），进程级序号从 1 重来也不会撞上 —— 撞上也无害，
    覆盖的是上一辈子容器的文件。
    """
    return f"{SHOTS_DIR}/shot-{next(_shot_counter)}.png"


def _ensure_dirs(sandbox: "SandboxContainer") -> None:
    """把 agent-browser 需要的可写目录建出来（幂等）。

    ★ 为什么每次都建：`/work` 是 tmpfs，镜像里 `mkdir` 出来的内容
    在容器启动挂 tmpfs 的那一刻就被盖掉了。所以"镜像里建过"不等于
    "现在存在"。这一步 20-40ms，换掉一整类"profile 建不出来"的故障。

    ⚠️ 不 `chown`：`--tmpfs /work` 带 `uid=1000,gid=1000`（见 runner
    的 `_argv`），所以 `solver` 建出来的目录属主天然就对了。

    ## ★★ `home` 那一项不是"顺手建的"，是 Chrome 能不能起来的前提

    容器是 `--read-only` 起的（见 `runner._restricted_argv`），所以
    `$HOME=/home/solver` **整个不可写**。Kali 实测：

        $ touch $HOME/.wtest   → Read-only file system
        $ ls -ld $HOME/.config → 存在（镜像里建的），但不可写
        $ chromium --headless --dump-dom … 
          → Failed to create headless user data directory container

    Dockerfile 里**已经**设了 `AGENT_BROWSER_PROFILE=/work/.browser/profile`，
    但那只管 agent-browser 自己的 profile，管不到 Chromium 的
    `$HOME/.config/chromium`、`$HOME/.cache` 与 crashpad 数据库 ——
    它仍然往 `$HOME` 里写，然后失败。

    所以 `_invoke()` 会把 `HOME` 指到 `{WORK_ROOT}/home`（tmpfs 里）。
    实测这一处改动就让整条链路通了：

        open       → ✓ http://127.0.0.1:18080/
        snapshot   → ✓
        screenshot → ✓ Screenshot saved to /work/.browser/shots/home-1.png
        view_image → 取回 9928 字节，与容器内 stat 一致，PNG magic 正确

    ⚠️ 为什么不在 Dockerfile 里 `ENV HOME=/work/.browser/home`：
       `/work` 是**运行时**挂的 tmpfs，镜像构建时它还不是那个目录；而且
       把所有进程的 `HOME` 都改掉会影响别的工具（`pip install --user`
       的落点、`~/.local/bin` 的 PATH 约定）。只在调 `agent-browser`
       这一条命令上覆盖，影响面最小且意图最清楚。
    """
    sandbox.shell(
        f"mkdir -p {WORK_ROOT}/home {WORK_ROOT}/profile {WORK_ROOT}/sock {WORK_ROOT}/shots",
        timeout=20,
    )


def _invoke(
    sandbox: "SandboxContainer",
    argv: list[str],
    *,
    timeout: int = DEFAULT_TIMEOUT,
):
    """在容器里跑一条 `agent-browser <argv>`。

    用 `shell()` 而不是 `exec()`：需要 shell 来展开 `AGENT_BROWSER_*`
    环境变量（它们来自 Dockerfile 的 `ENV`，`docker exec` 会继承，
    但显式走 login shell 更稳 —— `/etc/profile.d` 里若有脚本改写
    PATH 也能生效）。

    ## `HOME` 必须显式覆盖（否则 Chrome 起不来）

    见 `_ensure_dirs` 的 ★★ 段：容器 `--read-only`，`$HOME` 不可写，
    Chrome 建不了 `$HOME/.config/chromium` 就崩在
    `Failed to create headless user data directory container`。
    这里把 `HOME` 指到 tmpfs 里的 `{WORK_ROOT}/home`。

    ⚠️ 覆盖写在这里（每条命令）而不是 `_ensure_dirs` 里：
       `mkdir` 可以只建一次目录，但环境变量是**每条命令**的事 ——
       没有"进程级环境"，每次 `docker exec` 都是新的 shell。

    ⚠️ 参数一律 `shlex.quote`：模型给的选择器可能含空格、引号、
    `$`（`text=a b` 这类语义定位器很常见）。不 quote 会让 shell
    把它们拆开，而症状是"选择器写对了却找不到元素"。
    """
    quoted = " ".join(shlex.quote(a) for a in argv)
    return sandbox.shell(f"{HOME_ENV} {BIN} {quoted}", timeout=timeout)


def _hint_for(detail: str) -> str:
    """按错误类型给「下一步」。空串表示这条错误不需要额外提示。

    ★ 为什么必须有这个：模型拿到的每一句都是它唯一的上下文。
    返回"Unknown ref: e99"它会重试同一条命令（注定还失败）；
    返回"ref 已失效，重新 snapshot 拿新的"它会换路子。这个差别在
    `_tool_run_shell` 的"二进制文件请用 run_shell 分析"那句上
    已经验证过一次（同款理由）。
    """
    low = detail.lower()
    if "unknown ref" in low:
        return (
            "\n→ 这个 ref 已经失效了（页面变过，ref 会重新分配）。"
            "先执行 `snapshot -i` 拿新的 @eN 再操作。"
        )
    if "element not found" in low:
        return (
            "\n→ 页面上没有匹配的元素。可能是还没渲染完（先 `wait` 或重试），"
            "也可能选择器写错了 —— 先 `snapshot -i` 看实际有哪些元素。"
        )
    if "connection refused" in low or "not connected" in low or "no browser" in low:
        return (
            "\n→ 浏览器进程没在跑（容器可能重启过）。"
            "直接 `browser open <url>` 重开一次即可。"
        )
    if "timeout" in low or "timed out" in low:
        return (
            "\n→ 超时。若在等一个慢页面，用 `browser wait <毫秒>` 显式等，"
            "而不是反复重试同一条命令。"
        )
    if "executable" in low and ("not found" in low or "no such file" in low):
        return (
            "\n→ 镜像里没找到 chromium。这是部署问题，不是你的用法问题 —— "
            "请把这个错误原文报告给用户，改用 `http_request` / `run_shell` 继续。"
        )
    return ""


async def run_browser(args: dict[str, Any], *, sandbox: "SandboxContainer") -> str:
    """`browser` 工具的实现。

    ## 参数

    `command`（必填）是子命令，其余按子命令解释 —— 形状与 agent-browser
    自己的 CLI 一致（`open <url>` / `snapshot -i` / `click @e3`），
    所以模型从内置技能文档里学到的用法可以直接用。
    """
    command = str(args.get("command") or args.get("cmd") or "").strip()

    # ★ 容忍 `command` 里带参数（`"open https://example.com"`）。
    #
    #   为什么必须容忍：内置技能文档（`browser skills core`）教的用法是
    #   agent-browser 自己的 CLI 形状 —— `open <url>` / `click @e3`。模型
    #   照着学，于是把整串塞进 `command`，而实现读的是 `args["url"]`，
    #   取到空串 → 回「`open` 需要 `url`」。2026-10-27 实机就是这样：
    #   模型连着失败两次，第三次才猜对字段名。
    #
    #   这不是"模型笨"，是**两份契约漂了**：技能文档说 CLI 形状，工具
    #   schema 说 JSON 字段。让工具这边多走一步，两边就都对。
    #
    #   拆分只是把尾串存进 `inline_rest`，**不等于**接受它：每个分支自己
    #   决定要不要用它（`snapshot -i` 的 `-i` 就没分支读它，交互性只由
    #   `interactive` 字段决定）。所以 `snapshot -i` 不会报错，但也不会
    #   因为那个 `-i` 变成只看可交互元素 —— 想那样得传 `interactive: true`。
    inline_rest = ""
    if command and " " in command:
        head, _, rest = command.partition(" ")
        if head in SUBCOMMANDS:
            command = head
            inline_rest = rest.strip()

    #: 这次截图落到哪儿（只有 `screenshot` 分支会赋值）。
    #: 放在这里而不是分支里，是因为后面的成功提示要用它。
    screenshot_path = ""

    if not command:
        return (
            "参数错误：需要 `command`。可用子命令：\n"
            "  open <url>                打开页面（第一次会自动启动浏览器）\n"
            "  snapshot [-i]             看页面；-i 只看可交互元素（最省 token）\n"
            "  click <@eN|选择器>         点击\n"
            "  fill <@eN|选择器> <文本>   填写输入框（先清空）\n"
            "  type <@eN|选择器> <文本>   逐字输入（触发按键事件，适合实时校验的输入框）\n"
            "  press <键名>               按键（Enter / Tab / Escape / Control+a）\n"
            "  get <what> [选择器]        what = title|url|text|html|value|count|attr\n"
            "  eval <javascript>          在页面里执行 JS 并取回结果\n"
            "  wait <选择器|毫秒>          等元素出现或等一段时间\n"
            "  screenshot [路径]           截图；不给路径时自动命名为 shots/shot-N.png\n"
            "  back                       后退\n"
            "  close                      关闭浏览器\n"
            "  skills [名字]              读内置使用手册（core / electron / slack …）\n"
            "\n典型流程：`open <url>` → `snapshot -i` → 用返回的 @eN 去 click/fill "
            "→ 页面变化后**重新** `snapshot -i`。"
        )

    if command not in SUBCOMMANDS:
        return (
            f"未知子命令 `{command}`。可用：{', '.join(SUBCOMMANDS)}。\n"
            "最常见的用法是 `open <url>` 然后 `snapshot -i`。"
            "（其余 agent-browser 子命令可以直接用 `run_shell` 敲 "
            f"`{BIN} <子命令>`。）"
        )

    # ── skills：纯读内置文档，不碰浏览器 ──────────────────
    #
    # 放在最前面、且**跳过 `_ensure_dirs`**：这是纯文本查询，
    # 即使浏览器起不来也该能读到（排查时正好需要）。
    if command == "skills":
        name = str(
            args.get("name") or args.get("target") or inline_rest or "core"
        ).strip()
        full = bool(args.get("full"))
        argv = ["skills", "get", name] + (["--full"] if full else [])
        result = _invoke(sandbox, argv, timeout=30)
        if result.ok:
            return result.combined(limit=MAX_OUTPUT)
        available = _invoke(sandbox, ["skills", "list"], timeout=20)
        return (
            f"读不到内置技能 `{name}`：{result.combined(limit=600)}\n"
            f"可用清单：\n{available.combined(limit=800)}"
        )

    try:
        _ensure_dirs(sandbox)
    except Exception as exc:  # noqa: BLE001 - 沙箱没了不该让循环崩
        return (
            f"浏览器不可用：准备可写目录失败（{type(exc).__name__}: {exc}）。\n"
            "这通常意味着沙箱容器已经没了 —— 重新跑一次任务即可。"
        )

    # ── close：先让 agent-browser 自己收尾，再兜底杀进程 ──
    #
    # 双重收尾是刻意的：`agent-browser close` 走的是它的正常关闭路径
    # （会清 socket、关 Chrome）。但若那个 daemon 已经坏了（比如上次
    # 调用超时被 SIGKILL），它自己关不掉 —— 那时 pkill 兜住。
    # 用的是 `--all`：它会关掉所有 session，而我们的容器里只有一个。
    if command == "close":
        _invoke(sandbox, ["close", "--all"], timeout=25)
        sandbox.shell(
            "pkill -f 'remote-debugging-port' >/dev/null 2>&1; "
            f"rm -rf {WORK_ROOT}/profile {WORK_ROOT}/sock; echo done",
            timeout=20,
        )
        return "浏览器已关闭（下次 `open` 会重新启动）。"

    # ── 组装参数 ──────────────────────────────────────────
    argv: list[str] = [command]

    if command == "open":
        url = str(args.get("url") or args.get("target") or inline_rest or "").strip()
        if not url:
            return (
                "参数错误：`open` 需要 `url`。\n"
                '例如：{"command": "open", "url": "https://example.com"}'
            )
        if not (url.startswith("http://") or url.startswith("https://")):
            return (
                f"`url` 必须以 http:// 或 https:// 开头（收到 {url!r}）。\n"
                "浏览器只能打开网页；本地文件用 run_shell 读，"
                "`file://` 也在 agent-browser 里默认被禁。"
            )
        argv.append(url)

    elif command == "snapshot":
        # `-i` 是最重要的开关：只要可交互元素。整棵树在真实站点上
        # 轻松上千行，而模型真正需要点的通常不到 20 个。
        #
        # 内联开关（`snapshot -i` / `snapshot -u`）也要认：技能文档写的
        # 就是这种形状，不认的话 `-u` 会被静默丢掉 —— 而"静默丢掉一个
        # 开关"比报错更坏，模型会以为它拿到了带 url 的元素清单。
        inline_flags = {tok for tok in inline_rest.split() if tok.startswith("-")}
        # 默认就是 -i（省 token 是常态）；只有显式 interactive=false 才关。
        if args.get("interactive", True) or "-i" in inline_flags:
            argv.append("-i")
        if args.get("url") or "-u" in inline_flags:
            # agent-browser 的 `-u` 是给每个元素附上 url，不是"打开 url"
            argv.append("-u")

    elif command in ("click", "fill", "type"):
        # 内联形式（`fill @e2 hello world`）里第一个词是目标，其余是文本。
        # 先拆好，再走下面统一的"取参数"逻辑 —— 不在这里提前执行，
        # 否则 `timeout` 的规范化与失败提示都会被绕过。
        text_from_inline = ""
        if inline_rest and command in ("fill", "type"):
            head, _, tail = inline_rest.partition(" ")
            inline_rest = head
            text_from_inline = tail

        target = str(
            args.get("target") or args.get("selector") or inline_rest or ""
        ).strip()
        if not target:
            return (
                f"参数错误：`{command}` 需要 `target`（@eN 或 CSS 选择器）。\n"
                "先用 `snapshot -i` 看页面上有哪些元素。"
            )
        argv.append(target)
        if command in ("fill", "type"):
            argv.append(
                str(args.get("text") or args.get("value") or text_from_inline)
            )

    elif command == "press":
        key = str(
            args.get("key") or args.get("target") or inline_rest or ""
        ).strip()
        if not key:
            return "参数错误：`press` 需要 `key`（如 Enter / Tab / Escape / Control+a）。"
        argv.append(key)

    elif command == "get":
        # `get text h1` 的内联形式：第一个词是 what，第二个是选择器。
        what_inline, _, sel_inline = inline_rest.partition(" ")
        what = str(args.get("what") or args.get("target") or what_inline or "").strip()
        if not what:
            return (
                "参数错误：`get` 需要 `what`（title|url|text|html|value|count|attr）。\n"
                '例如：{"command": "get", "what": "text", "target": "h1"}'
            )
        argv.append(what)
        # `get attr` 要额外给属性名：`get attr <name> <selector>`
        if what == "attr":
            attr = str(
                args.get("attr") or args.get("name") or sel_inline or ""
            ).strip()
            if not attr:
                return '参数错误：`get attr` 还需要 `attr`（属性名）。例如 {"command":"get","what":"attr","attr":"href","target":"a"}'
            argv.append(attr)
        selector = str(
            args.get("target2") or args.get("selector") or ""
        ).strip()
        if not selector and what != "attr":
            selector = sel_inline.strip()
        if selector:
            argv.append(selector)

    elif command == "eval":
        expr = str(
            args.get("expression")
            or args.get("text")
            or args.get("target")
            or inline_rest
            or ""
        )
        if not expr.strip():
            return "参数错误：`eval` 需要 `expression`（要执行的 JS 源码）。"
        argv.append(expr)

    elif command == "wait":
        spec = str(
            args.get("target")
            or args.get("selector")
            or args.get("ms")
            or inline_rest
            or ""
        ).strip()
        if not spec:
            return (
                "参数错误：`wait` 需要 `target`（选择器）或 `ms`（毫秒）。\n"
                '例如：{"command": "wait", "ms": 2000} 或 '
                '{"command": "wait", "target": "table"}'
            )
        argv.append(spec)

    elif command == "screenshot":
        path = str(args.get("path") or "").strip()
        # ★ v1.04：不给路径时**我们自己定**，并把它记下来交给模型。
        #   理由见 `_shot_path` 的 docstring。
        screenshot_path = path or _shot_path()
        argv.append(screenshot_path)
        if args.get("full"):
            argv.append("--full")

    timeout = int(args.get("timeout_seconds") or DEFAULT_TIMEOUT)
    timeout = max(10, min(timeout, 180))
    if command == "open":
        timeout = max(timeout, OPEN_TIMEOUT)

    result = _invoke(sandbox, argv, timeout=timeout)

    # ── 成功 ──────────────────────────────────────────────
    if result.ok:
        out = result.combined(limit=MAX_OUTPUT)
        if command == "screenshot":
            # ★ v1.04：这句文案在 v1.04 之前是「你无法看到图片内容」——
            #   当时是事实（消息 content 恒为 str，没有图片通道），代价是
            #   "截图"这个能力等于不存在：页面错位、元素重叠、验证码、
            #   图表全都只能靠 snapshot 的结构文本去猜。
            #
            #   现在 `view_image` 把图真送进对话了，所以这句必须改。**留着
            #   旧的会让模型明明能看却不去看**，比没有这个工具更糟。
            #
            #   ⚠️ 措辞要点：把 `view_image` 和这条路径**写死在返回值里**。
            #   不能只说"你可以看图"——模型需要的是"下一步调哪个工具、传什么"。
            out += (
                f"\n\n这张图存在 `{screenshot_path}`。"
                f'要真的看到它，调 `view_image`：{{"path": "{screenshot_path}"}}'
                "\n（图在下一轮才可见。只想看结构不必截图，用 `snapshot -i` 更省；"
                "要看文字用 `get text <选择器>`，要看渲染后的 HTML 用 `get html`。）"
            )
        if command == "open":
            out += (
                "\n\n→ 下一步通常是 `snapshot -i` 看这个页面有哪些可操作元素"
                "（它会给出 @e1/@e2… 这样的 ref，后面 click/fill 就用它们）。"
            )
        return out

    # ── 失败 ──────────────────────────────────────────────
    detail = result.combined(limit=2000)
    hint = _hint_for(detail)
    if not hint and command == "open":
        # `open` 失败最常见的是 Chrome 起不来。这个我们没有更好的
        # 分类方式，但**必须**把原始输出给模型 —— 里面通常有 Chromium
        # 自己的报错（缺库、profile 只读、架构不匹配）。所以只补一句
        # 指向排查方向的话。
        hint = (
            "\n→ 若反复失败，用 `run_shell` 跑 "
            f"`{BIN} --version` 和 `command -v chromium` 确认两者都在。"
        )
    return f"浏览器命令 `{command}` 失败：\n{detail}{hint}"
