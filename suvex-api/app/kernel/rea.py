"""REA 逆向工程工具的内核封装。

## 为什么不是"挂个 MCP 服务器"就完事

REA 本体是个 MCP server（229 个工具），把它当普通 MCP 连接挂进来的做法
**实测行不通**，原因不是配置错误：

  · `tools/list` 返回 229 个工具，渲染成文本 **2,471,782 字符**（4.2 MB）；
  · 而内核的 `mcp.py:87 MAX_DESCRIBE_CHARS = 24_000` 只够覆盖 **4 个**
    （1%）。剩下 225 个对模型完全不可见 —— 这正是"假工具"的形状：
    界面上有 229 个，模型只知道 4 个。

所以这里走另一条路：**把 REA 当一个命令行工具来调**，与 `run_shell` /
`agent-browser` 同级。模型只需要知道 1 个工具名 `rea` 和一批子命令名。

## 为什么要有白名单（而不是"什么子命令都能跑"）

`run_shell` 已经能跑任意命令，所以"安全"不是加白名单的理由 —— 真正的
理由是**能力对账**：REA 的 177 个子命令里，绝大多数在没装
Ghidra/Hopper/IDA/JADX 的机器上会返回 `provider_unavailable`。让模型
自由试错的结果是它把"这条命令跑不了"读成"这个二进制没什么可分析的"，
而这两件事完全不同。

白名单只放**实测在无 native provider 环境下真能出结果**的命令。装不上
的命令不是被禁掉，而是不出现在工具的说明里 —— 模型不会去试一条它
没听说过的命令。

## 实测依据（2026-10-27，Kali rolling，无 Ghidra/Hopper/IDA/JADX）

逐个跑 `rea <cmd> --json` 并检查是否有 `evidence_id`：

  能出结果：inspect-elf-notes / inspect-go-binary / inspect-rust-binary
            inspect-artifact / inspect-keyed-archive / extract-artifact
            inspect-sqlite-database / inspect-pe-imports / inspect-pe-resources
            inspect-pe-certificates / inspect-pe-load-config
            inspect-python-pyc / inspect-model-artifact / inspect-devicetree
            inspect-il2cpp-metadata / inspect-byte-entropy / inspect-kernel-module
            inspect-gpu-code-objects / inspect-apple-ips / inspect-apk-signature
            inspect-erlang-beam / inspect-dart-aot / identify-flutter-build
            inspect-managed-artifact / inspect-managed-members
            inspect-managed-native-boundaries / analyze-javascript-application
            analyze-web-bundle

  需要外部工具（报错里明确点名缺什么，所以留在白名单里并写清前置）：
            inspect-binary-layout  → 要 REA_PWNTOOLS_PYTHON
            inspect-wasm-artifact  → 要 REA_WABT_BIN_DIRECTORY
            inspect-pcap-capture   → 要 tshark
            inspect-packet-capture → 要 tshark

  需要 native provider（**不入白名单**，装了 Ghidra 再放开）：
            inspect / analyze / decompile / xrefs / function / search /
            trace / list-procedures / inspect-native-* 等

## 与 `view_image` / `browser` 的关系

REA 的 `analyze-web-bundle`、`inspect-web-page` 这类命令与我们的
`browser` 工具不重叠：前者是**静态**读 JS 源码，后者是**动态**跑页面。
两者可以串起来（先用 browser 拿页面，再用 rea 静态读 bundle）。
"""

from __future__ import annotations

import json
import shlex
from typing import Any, Final

#: 可执行入口。装法见 `docker/suvex-sandbox/Dockerfile`。
#:
#: ⚠️ 为什么不是 `/usr/local/bin/rea`：REA 的 npm 包 `bin` 指向
#: `scripts/rea.mjs`，是 ESM 脚本，靠 shebang `#!/usr/bin/env node` 启动。
#: 那条路径要求 node 在 PATH 里。镜像里我们装的是官方 node tarball 到
#: `/usr/local`，所以 `env node` 能找到 —— 但**显式写全路径更稳**：
#: 容器内 `PATH` 在某些 `docker exec` 路径下不含 `/usr/local/bin`
#: （`_restricted_argv` 里 `-e` 传的 PATH 是显式设置的），走 shebang
#: 会得到 `env: 'node': No such file or directory`，而那个报错看起来
#: 像"镜像缺 node"，实际是 PATH 问题。
BIN: Final = "/opt/rea/rea.mjs"
#: node 解释器全路径。与 BIN 同理，不依赖 PATH。
NODE: Final = "/usr/local/bin/node"

#: 单次调用上限。REA 的静态分析是纯 CPU，实测一个中等 ELF 的
#: `inspect-binary-layout` 约 2-4 秒；`analyze-javascript-application`
#: 对 2 个文件约 6 秒。90 秒留足余量，又不会让模型卡死等一个挂住的调用。
DEFAULT_TIMEOUT: Final = 90

#: 输出截断。REA 的 `--json` 输出对稍大的二进制会是几百 KB 到几 MB
#: （`normalized_result` 里带完整的 section/symbol 表）。模型不需要
#: 全部 —— 需要的是"有哪些节、哪些导入、什么架构"。
#: 7000 与 `browser` 工具同值，便于模型形成一致的预期。
MAX_OUTPUT: Final = 7000

#: 白名单：实测在无 native provider 下真能出结果的子命令。
#:
#: 分组只是为了给模型一个"先看什么"的线索，不是权限边界 ——
#: `run_shell` 能跑一切，这里禁不禁都不改变真实权限。
SUBCATEGORIES: Final[dict[str, tuple[str, ...]]] = {
    "归档与容器": (
        "inspect-artifact",
        "inspect-keyed-archive",
        "extract-artifact",
        "inspect-asset-catalog",
    ),
    "可执行文件": (
        "inspect-elf-notes",
        "inspect-binary-layout",
        "inspect-pe-imports",
        "inspect-pe-resources",
        "inspect-pe-certificates",
        "inspect-pe-load-config",
        "inspect-go-binary",
        "inspect-rust-binary",
        "inspect-python-pyc",
        "inspect-byte-entropy",
        "inspect-kernel-module",
        "inspect-gpu-code-objects",
    ),
    "应用包": (
        "inspect-apk-signature",
        "inspect-dart-aot",
        "identify-flutter-build",
        "inspect-il2cpp-metadata",
        "inspect-managed-artifact",
        "inspect-managed-members",
        "inspect-managed-native-boundaries",
        "inspect-apple-ips",
        "inspect-devicetree",
        "inspect-erlang-beam",
        "inspect-model-artifact",
    ),
    "JavaScript": (
        "analyze-javascript-application",
        "analyze-web-bundle",
        "recover-javascript-sources",
        "export-web-scripts",
    ),
    "网络与信号": (
        "inspect-pcap-capture",
        "inspect-packet-capture",
        "inspect-recorded-crash",
        "inspect-web-network-capture",
    ),
    "数据与格式": (
        "inspect-sqlite-database",
        "inspect-wasm-artifact",
        "inspect-signal-decoders",
        "decode-signal-recording",
        "demangle-swift",
    ),
    "自检": (
        "doctor",
        "capabilities",
        "providers",
    ),
}

#: 扁平化后的白名单集合。
ALLOWED: Final[frozenset[str]] = frozenset(
    cmd for cmds in SUBCATEGORIES.values() for cmd in cmds
)

#: 需要外部工具的子命令 → 缺什么。写进报错里，让模型能自己判断
#: "是环境没准备好，不是这个文件没料"。
NEEDS_EXTERNAL: Final[dict[str, str]] = {
    "inspect-binary-layout": (
        "需要 REA_PWNTOOLS_PYTHON 指向一个装了 pwntools 4.15.0 / "
        "pyelftools 0.33 / Unicorn 2.1.2 的 Python。镜像里没装。"
    ),
    "inspect-wasm-artifact": (
        "需要 REA_WABT_BIN_DIRECTORY 指向含 wasm-validate / wasm-objdump / "
        "wasm2wat 的 WABT 1.0.42 目录。镜像里没装。"
    ),
    "inspect-pcap-capture": "需要 tshark。镜像里没装。",
    "inspect-packet-capture": "需要 tshark。镜像里没装。",
    "inspect-web-network-capture": "需要抓包文件（HAR / SAZ / qlog），不是活页面。",
    "inspect-recorded-crash": (
        "需要 Linux ELF64 core 文件，且仅 x86_64/aarch64 小端。"
    ),
}


def _quote(arg: str) -> str:
    """把参数塞进 shell 命令行。

    ⚠️ 必须 `shlex.quote`：路径里可能有空格、`$(...)`、反引号。不引号
    会让一个文件名变成一条命令 —— 这是最容易被忽略的注入面，而
    `run_shell` 的存在**不**构成理由（模型传进来的路径来自它自己
    读到的目录列表，不是攻击者写的；但目录列表来自目标机器）。
    """
    return shlex.quote(arg)


def _argv(command: str, path: str, extra: dict[str, Any] | None = None) -> str:
    """拼出容器内的完整命令行。

    统一加 `--json`：REA 默认输出人类可读的彩色文本（含 box-drawing
    字符），那是给终端看的；JSON 才是给模型看的 —— 它要的字段
    （provider / normalized_result / limitations）只在 JSON 里有。
    """
    parts = [NODE, BIN, command, path, "--json"]
    for key, value in (extra or {}).items():
        if value is None or value is False:
            continue
        if value is True:
            parts.append(f"--{key}")
        else:
            parts.append(f"--{key}={value}")
    return " ".join(_quote(p) for p in parts)


def _summarize(cmd: str, stdout: str, stderr: str) -> str:
    """把 REA 的 JSON 输出压成模型要读的那几句话。

    ## 为什么不直接把 JSON 丢给模型

    一份 `inspect-artifact` 的输出对中等 ASAR 是 **1-3 MB**（完整的
    node/occurrence/edge 表）。直接截断到 7000 字符的结果是：模型看到
    一个断在半截的 JSON，既读不出结论、也不知道自己没看全。

    所以这里先解析，再挑三样东西给它：
      1. 这次是什么操作、什么 provider 做的（决定可信度）
      2. limitations（REA 会诚实说"这一项我没看完"）
      3. normalized_result 的顶层键 + 小字段值

    解析失败时**原样返回**（带一句说明），不假装看懂 —— 与
    `mcp.py` 的 `_render_call_result` 同一条原则。
    """
    text = (stdout or "").strip()
    if not text:
        detail = (stderr or "").strip()
        return f"`rea {cmd}` 没有任何输出。" + (
            f"\n[stderr]\n{detail[:1500]}" if detail else ""
        )

    # REA 的 `--json` 对流式进度会先打若干行 `{"rea_progress":...}`，
    # 最后才是真正的结果对象。要跳过进度行。
    objects: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "rea_progress" not in obj:
            objects.append(obj)

    if not objects:
        # 可能是格式化过的多行 JSON（非流式命令）。
        try:
            obj = json.loads(text)
        except ValueError:
            return text[:MAX_OUTPUT]
        objects = [obj] if isinstance(obj, dict) else []

    if not objects:
        return text[:MAX_OUTPUT]

    out = objects[-1]

    # 错误对象：REA 的错误码是有意义的，要原样给模型看。
    if "code" in out and "message" in out:
        return _render_error(cmd, out)

    lines: list[str] = []
    provider = out.get("provider")
    if isinstance(provider, dict):
        lines.append(
            f"provider: {provider.get('id')}（{provider.get('name')} "
            f"v{provider.get('version')}）"
        )
    operation = out.get("operation")
    if operation:
        lines.append(f"operation: {operation}")

    limits = out.get("limitations")
    if isinstance(limits, list) and limits:
        lines.append("limitations:")
        for item in limits[:6]:
            lines.append(f"  · {item}")

    payload = out.get("normalized_result")
    if isinstance(payload, dict):
        lines.append("结果字段: " + ", ".join(sorted(payload.keys())[:20]))
        # 少量标量字段直接给值 —— 它们通常是"这次到底分析了什么"。
        scalars = {
            k: v
            for k, v in payload.items()
            if isinstance(v, (str, int, float, bool)) and len(str(v)) < 120
        }
        if scalars:
            lines.append("要点:")
            for key, value in list(scalars.items())[:10]:
                lines.append(f"  {key} = {value}")
        # 计数类字段单列（row_count / entry_count / …）
        counts = {
            k: v
            for k, v in payload.items()
            if isinstance(v, int) and not isinstance(v, bool)
        }
        if counts:
            lines.append(
                "计数: "
                + ", ".join(f"{k}={v}" for k, v in list(counts.items())[:12])
            )

    if not lines:
        lines.append(text[:MAX_OUTPUT])

    rendered = "\n".join(lines)
    if len(rendered) > MAX_OUTPUT:
        rendered = rendered[:MAX_OUTPUT] + "\n…（已截断，完整结果请加 --full-output）"
    return rendered


def _render_error(cmd: str, out: dict[str, Any]) -> str:
    """渲染 REA 的错误对象。

    ## 为什么要特判 `provider_unavailable`

    这类错误的字面意思容易读错。模型的合理推断是"这个文件分析不了"，
    而真相是"这台机器没装反编译引擎"。两者对下一步动作的指引完全
    不同：前者该换文件，后者该停止尝试这类命令。

    所以这里在原文之外补一句判读。
    """
    code = str(out.get("code") or "")
    message = str(out.get("message") or "")
    parts = [f"`rea {cmd}` 失败：{code}", message]

    if code == "provider_unavailable":
        details = out.get("details") or {}
        tried = details.get("candidate_ids") or []
        if tried:
            parts.append(
                f"（试过的引擎：{', '.join(str(x) for x in tried)} —— "
                "这台机器一个都没装。**不是这个文件没料**，是环境里没有"
                "反编译引擎。别再试 inspect/analyze/decompile/xrefs 这族命令。）"
            )
    elif code == "capability_unavailable":
        details = out.get("details") or {}
        reason = details.get("reason")
        if reason:
            parts.append(f"（原因：{reason}）")
    elif code in ("unsupported_target", "invalid_request"):
        details = out.get("details") or {}
        reason = details.get("reason") or details.get("constraint")
        if reason:
            parts.append(f"（{reason} —— 换个参数或换个文件再试。）")

    return "\n".join(parts)


#: 可写 `$HOME`。容器根是 `--read-only`，`$HOME` 不可写会让 node 的
#: 崩溃报告/缓存初始化失败 —— 与 `browser.py` 的 `HOME_ENV` 同一个坑
#: （那次症状是 Chromium 报 `Failed to create a ProcessSingleton`，
#: 看起来像权限问题，实际是只读根）。
#:
#: ⚠️ 必须在 `/work` 之下（tmpfs），不能在 persist 里：REA 的缓存是
#: 一次性的，没必要跨容器存活，而放 persist 会让它无限增长。
HOME_ENV: Final = "HOME=/work/.rea"


def run_rea_sync(args: dict[str, Any], *, sandbox: "SandboxContainer") -> str:
    """`rea` 工具的实现（同步版）。

    ## 参数

    `command`（必填）是子命令；`path`（除自检命令外必填）是要分析的
    沙箱内路径；`extra` 是其余 `--flag` 参数。

    ## 为什么 `path` 不做存在性预检

    `run_shell` 能跑 `ls`，模型完全知道文件在不在。这里再查一次只是
    多一次容器往返（约 150ms），而且会引入一个**过期的判断**：两次
    调用之间那个文件可能已经被上一条命令生成或删除。让 REA 自己报
    "文件不存在"更准 —— 它的报错里带绝对路径。
    """
    command = str(args.get("command") or args.get("cmd") or "").strip()
    if not command:
        return describe()

    # ★ 容忍 `command` 里带路径（`"inspect-artifact /work/x.asar"`）。
    #   与 `browser.py` 同一条理由：REA 自己的 CLI 形状是
    #   `rea <cmd> <path>`，模型从 REA 文档里学到的是这个形状。
    inline_path = ""
    if " " in command:
        head, _, rest = command.partition(" ")
        if head in ALLOWED:
            command, inline_path = head, rest.strip()

    if command not in ALLOWED:
        # 常见误用：把需要 Ghidra 的命令写进来了。要专门提示，
        # 否则模型会以为"命令名拼错了"，然后一直重试。
        ghidra_family = (
            "inspect", "analyze", "decompile", "decompile-all", "xrefs",
            "function", "search", "trace", "list-procedures", "decompile-jeb-item",
        )
        if command in ghidra_family or command.startswith("inspect-native-"):
            return (
                f"`{command}` 需要反编译引擎（Ghidra / Hopper / IDA / JADX），"
                "这台机器一个都没装 —— 它会直接报 provider_unavailable，"
                "而那个报错**不是说文件有问题**。\n"
                f"这台机器能用的子命令：{', '.join(sorted(ALLOWED))}"
            )
        return (
            f"未知子命令 `{command}`。\n{describe()}"
        )

    # 自检类命令不需要 path。
    if command in ("doctor", "capabilities", "providers"):
        path = ""
    else:
        path = str(args.get("path") or args.get("target") or inline_path or "").strip()
        if not path:
            return (
                f"`{command}` 需要 `path`（沙箱内文件或目录的路径）。\n"
                '例如：{"command": "inspect-artifact", "path": "/work/app.asar"}'
            )

    # 前置条件明说：这些命令的"失败"是环境造成的，不是文件造成的。
    warning = NEEDS_EXTERNAL.get(command)

    extra_raw = args.get("extra")
    extra: dict[str, Any] = extra_raw if isinstance(extra_raw, dict) else {}

    # `--json` 由 `_argv` 统一加，模型传重复的会被盖掉。
    extra.pop("json", None)

    timeout = args.get("timeout_seconds")
    try:
        timeout_s = int(timeout) if timeout is not None else DEFAULT_TIMEOUT
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT
    timeout_s = max(10, min(timeout_s, 600))

    try:
        sandbox.shell(f"mkdir -p {HOME_ENV.split('=', 1)[1]}", timeout=20)
    except Exception:  # noqa: BLE001 - 建目录失败不该让整个调用崩
        pass

    cmdline = _argv(command, path, extra)
    try:
        result = sandbox.shell(f"{HOME_ENV} {cmdline}", timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - 沙箱没了要让模型看见原因
        return (
            f"`rea {command}` 跑不起来：{type(exc).__name__}: {exc}\n"
            "这通常意味着沙箱容器已经没了 —— 重新跑一次任务即可。"
        )

    if not result.ok and not result.stdout:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip()
        if "No such file or directory" in detail and "node" in detail.lower():
            return (
                "找不到 node 解释器。镜像里 REA 依赖 `/usr/local/bin/node`，"
                "而这次它不在 —— 说明镜像是旧版（没有 REA 段）。\n"
                f"[原始报错]\n{detail[:800]}"
            )
        head = f"`rea {command}` 退出码 {result.code}。"
        if warning:
            head += f"\n⚠️ {warning}"
        return f"{head}\n[stderr]\n{detail[:2000]}"

    rendered = _summarize(command, result.stdout, result.stderr)
    if warning:
        rendered = f"⚠️ {warning}\n\n{rendered}"
    return rendered


def describe() -> str:
    """给模型看的"这个工具能干什么"。

    与 `TOOL_SPECS` 里的 `params` / `default_limitation` 分工：
    那段是给**人**看的摘要，这里是可以塞进提示词或工具说明的完整清单。
    """
    lines = ["`rea` 逆向分析工具。用法：ACTION 里写",
             '  {"tool": "rea", "args": {"command": "<子命令>", "path": "<沙箱内路径>"}}',
             "",
             "可用子命令（按用途分组）："]
    for group, cmds in SUBCATEGORIES.items():
        lines.append(f"  {group}: {', '.join(cmds)}")
    lines += [
        "",
        "先跑 `capabilities` 看这台机器哪些能力可用；跑 `doctor` 看缺什么。",
        "⚠️ 这台机器**没有装**反编译引擎（Ghidra/Hopper/IDA/JADX），所以",
        "   `inspect`/`analyze`/`decompile`/`xrefs` 这族命令不在可用列表里 ——",
        "   它们会报 provider_unavailable，报的不是文件有问题。",
    ]
    return "\n".join(lines)
