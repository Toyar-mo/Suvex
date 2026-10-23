#!/usr/bin/env python3
"""gogo MCP server —— 把 gogo 的资产测绘/指纹/POC 能力接成 MCP 工具。

## 这是什么

一个**外部进程**（stdio JSON-RPC 2.0），不是库。Suvex 侧通过
`KernelConfig.mcp_servers` 配一条（部署者在设置页填）：

```json
{"name": "gogo", "command": "python3",
 "args": ["/opt/suvex/gogo_mcp.py"], "notes": "资产测绘/指纹/POC"}
```

然后模型用 `list_mcps` / `describe_mcp` / `call_mcp` 就能用上 gogo。

## 为什么包一层而不直接让它跑 `run_shell`

三个理由，按重要性：

1. **许可证边界。** gogo 是 GPL-3.0，Suvex 是 Apache-2.0 且带
   `statement-of-originality.md` 原创性声明。**作为独立进程 exec 是干净的
   聚合**（与 `strix-agent` 同一条先例，见 `DEPENDENCIES.md` §2.2）；
   拷源码或指纹库进仓库则不干净。所以这一层只 exec 二进制、只读它的
   JSONL，不 import、不复制数据。
2. **安全档位在代码里，不在提示词里。** 默认带 `--opsec`（限速 + 跳过
   high/medium POC，见 `_run_gogo`），并把参数收成结构化字段。提示词是
   建议，代码才是约束。
3. **输出结构化。** 每个工具回一段 JSON，模型能直接拿去做证据断言；
   人读表格只能复述。

## 三个工具

  - `recon_scan`      端口 + 协议 + 状态码 + 标题 + 指纹（替代「nmap 再 whatweb」）
  - `run_poc`         跑 POC（nuclei 模板 / gogo 内嵌模板）
  - `extract_secrets` 从响应抽密钥 / 内网 IP / 桶 / 连接串

## 输出契约：一律 JSON，且「0 命中」必须被说成结论

`recon_scan` 扫到 0 个端口不是故障，`run_poc` 0 命中也不是「目标安全」。
两者都在返回里带 `note` 明说——因为历史上踩过：工具静默返空，模型
判不出是工具坏了还是真没有，于是退化成用 `http_request` 一个个猜 URL。

## 判级不在这里做

POC 命中是「单次响应匹配」，在 Suvex 的证据分级里最多 L1（机审候选）。
`run_poc` 的返回里带 `judgement_hint` 提醒这一点，但**升到 L2 要补对照
实验**，那是调用方的责任。这里绝不替调用方把命中label成 confirmed。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

SERVER_NAME = "gogo"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"

#: gogo 二进制的路径。部署侧可用环境变量指定（镜像里应在 PATH 上）。
GOGO_BIN = os.environ.get("GOGO_BIN", "gogo")

#: 单次工具的墙钟上限（秒）。比 Suvex 的 MCP 默认 60 秒宽 —— 一次 /24
#: 启发式扫描实测约 85 秒；但不能无限，卡住的扫描不能把整次解题拖死。
DEFAULT_TIMEOUT = int(os.environ.get("GOGO_TIMEOUT", "180"))

#: 允许出现在 target 里的字符。**这不是安全边界**（真正的隔离在沙箱网桥），
#: 只是挡掉明显的错误用法 —— argv 不走 shell，所以也不需要 shell 转义。
_TARGET_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:/,[]")

#: 端口表达式的字符集（数字、逗号、连字符 + gogo 的预设别名）。
_PORT_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789,_-")


# ── 可执行文件解析 ────────────────────────────────────────────────

#: 解析后的**实际可执行路径**（惰性算一次）。
#:
#: ## 为什么需要这一步（实测踩到的）
#:
#: 把 gogo 以只读卷挂进容器（`-v /tmp/gogo:/usr/local/bin/gogo:ro`）时，
#: **挂载点上的文件保留宿主侧的 mode**。宿主那份是 `0644`，于是容器里
#: `solver` 用户 exec 它得到 `PermissionError: [Errno 13] Permission denied`。
#:
#: 关键：这**不是「没装」**，是「装了但不可执行」——两者排查方向完全不同。
#: 而且 `os.access(X_OK)` 在有读权限时会返回 True，**不能**靠它判断；只能
#: 真的 exec 一次。处理方式：探测 → 失败则复制到可写目录 + `chmod 0o755`
#: → 再探测。这样部署侧无论挂载 mode 如何都正确，且代价只在启动时一次。
_RESOLVED_BIN: str | None = None


def _probe(path: str) -> bool:
    """真的 exec 一次 `-h` 判断能不能用（不靠 os.access，那会误判）。"""
    try:
        subprocess.run([path, "-h"], capture_output=True, timeout=20, check=False)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def resolve_gogo_bin() -> str:
    """返回一个**真能 exec** 的 gogo 路径；解析不了抛 RuntimeError（带中文原因）。"""
    global _RESOLVED_BIN
    if _RESOLVED_BIN is not None:
        return _RESOLVED_BIN

    candidate = GOGO_BIN
    if os.sep not in candidate and (os.altsep is None or os.altsep not in candidate):
        # `shutil.which` 在 Windows 上返回 **basename**（如 `gogo.EXE`），
        # 它仍不含分隔符，于是会被下一轮判成"不在 PATH 上"。这里记住是
        # 从 PATH 命中的，后面不要再判一次。
        found = shutil.which(candidate)
        if found is None:
            raise RuntimeError(
                f"找不到 gogo 可执行文件（GOGO_BIN={GOGO_BIN!r} 不在 PATH 上）。"
                "部署侧把 gogo 装进镜像，或用环境变量 GOGO_BIN 指定绝对路径。"
            )
        candidate = found
        from_path = True
    else:
        from_path = False

    if _probe(candidate):
        _RESOLVED_BIN = candidate
        return candidate

    # 不可执行 → 复制到自己可写的地方再 +x（只读卷上的 mode 改不了）。
    target = os.path.join(tempfile.gettempdir(), "gogo_reexec")
    try:
        shutil.copyfile(candidate, target)
        os.chmod(target, 0o755)
    except OSError as exc:
        raise RuntimeError(
            f"gogo 二进制 `{candidate}` 存在但不可执行，且无法复制到可写目录修复（{exc}）。"
            "检查挂载权限：只读卷会保留宿主侧的 mode，宿主那份需要 +x。"
        ) from exc
    if not _probe(target):
        where = "（这是从 PATH 找到的）" if from_path else ""
        raise RuntimeError(
            f"gogo 二进制 `{candidate}`{where} 既不可执行、复制后也跑不起来"
            "（架构或动态库不匹配？）。"
        )
    _RESOLVED_BIN = target
    return target


# ── 工具清单 ──────────────────────────────────────────────────────


def _tool(name: str, description: str, schema: dict) -> dict:
    return {"name": name, "description": description, "inputSchema": schema}


TOOLS: list[dict] = [
    _tool(
        "recon_scan",
        "端口测绘 + 协议 + 状态码 + 页面标题 + 服务指纹，一次拿全。用来替代"
        "「先 nmap 摸端口、再 whatweb/curl 认指纹」这一对：gogo 一次连接就同时"
        "拿到端口状态、HTTP 状态码、标题和指纹。返回 JSON（alive_count + results 数组）。"
        "⚠️ `frameworks` 为空**不代表没有技术栈**，只代表 gogo 的指纹库没认出来"
        "（它的库偏国内 CMS/OA/设备，通用 Web 框架识别不如 whatweb）——"
        "这种情况再用 http_request 看响应头/HTML。",
        {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "目标：IP、CIDR（如 10.0.0.0/24）或 host。可逗号分隔多个。",
                },
                "ports": {
                    "type": "string",
                    "description": (
                        "端口表达式。预设别名：top1(3个)/top2(148个)/common(273个)/"
                        "win(21个)/db(43个)/http；也可写 80,443,8000-8100。默认 top1。"
                    ),
                },
                "smart": {
                    "type": "string",
                    "enum": ["none", "s", "ss", "sc"],
                    "description": (
                        "启发式扫描，**大网段必用**（否则逐个端口全扫会超时）："
                        "none=逐目标逐端口全扫（只适合单机/小范围）；"
                        "s=对 /24 递归（每段先探 .1 和 .254，通了才深入）——扫内网网段用这个；"
                        "ss=对 /16 递归；sc=只做 C 段发现、不扫端口。"
                    ),
                },
                "fingerprint_level": {
                    "type": "integer",
                    "enum": [0, 1, 2],
                    "description": (
                        "0=只看被动响应（最快，默认）；1=加主动探测（多发请求确认版本）；"
                        "2=再加 FingerprintHub 全库。1 以上会明显变慢。"
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": f"本次扫描墙钟上限，默认 {DEFAULT_TIMEOUT} 秒。",
                },
            },
            "required": ["target"],
        },
    ),
    _tool(
        "run_poc",
        "对目标跑 POC（漏洞模板），返回命中的模板 id + severity + payload。"
        "POC 格式与 nuclei 兼容（同一套 YAML）。"
        "**没命中不等于没有漏洞**，只说明这批模板没匹配上。"
        "⚠️ 默认带 `--opsec`：gogo 会跳过 high/medium 风险模板、并把速率压到 10。"
        "要跑更激进的模板需要部署侧改档位，不是模型在参数里能打开的。",
        {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "目标 host:port 或 IP（如 10.1.1.5:8080）。不给端口按 80。",
                },
                "template_dirs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "模板目录（nuclei 模板目录或任意含 .yaml 的目录），会先规整成 "
                        "gogo 能吃的形状再跑。不给则用 gogo 内嵌的那批。"
                    ),
                },
                "template_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "只跑这些模板 id（最可控，建议优先于 template_dirs）。",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": f"墙钟上限，默认 {DEFAULT_TIMEOUT} 秒。",
                },
            },
            "required": ["target"],
        },
    ),
    _tool(
        "extract_secrets",
        "从目标响应里抽敏感信息：密钥（AWS AK/私钥/JWT/GitHub token/企业微信 corpkey）、"
        "内网 IP、OSS/S3 桶、JDBC 连接串、URL/JS 路径。返回 JSON 按类别分组。"
        "用来找「页面上不该出现的秘密」——比让模型肉眼读 HTML 稳。"
        "⚠️ 只看目标首屏响应，不抓 JS 文件内容、不爬子页面。",
        {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "目标 host:port 或 IP。"},
                "groups": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "抽哪些类别。预设组：pentest（jwt/ip/inter-ip/oss/s3/aws-ak/"
                        "rsa-key/windows-file/password/wecom-key/jdbc/github-token）、"
                        "info（mail/idcard/phone/ip/username）、url、js；也可直接给单个"
                        "类别名。默认 pentest。"
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": f"墙钟上限，默认 {DEFAULT_TIMEOUT} 秒。",
                },
            },
            "required": ["target"],
        },
    ),
]


# ── 参数校验 ──────────────────────────────────────────────────────


class ToolArgError(ValueError):
    """参数不合法。消息原样回给模型 —— 它据此改参数重试。"""


def _clean_target(raw: object) -> str:
    target = str(raw or "").strip()
    if not target:
        raise ToolArgError("参数 target 不能为空。给 IP、CIDR 或 host:port。")
    bad = sorted(set(target) - _TARGET_CHARS)
    if bad:
        raise ToolArgError(
            f"target 含不允许的字符 {bad!r}。只接受 IP / CIDR / host[:port] / 逗号分隔。"
        )
    return target


def _clean_ports(raw: object) -> str:
    ports = str(raw or "top1").strip()
    if not ports:
        return "top1"
    bad = sorted(set(ports) - _PORT_CHARS)
    if bad:
        raise ToolArgError(
            f"ports 含不允许的字符 {bad!r}。只接受数字、逗号、连字符、下划线和预设别名"
            "（top1/top2/common/win/db/http）。"
        )
    return ports


def _split_target(target: str) -> tuple[str, str]:
    """把 `host:port` 拆成两半；没端口时默认 80。CIDR 不会被拆坏。"""
    if "/" in target:
        return target, "80"
    host, sep, port = target.rpartition(":")
    if sep and port.isdigit():
        return host, port
    return target, "80"


# ── gogo 调用 ──────────────────────────────────────────────────────


def _run_gogo(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    """跑一次 gogo。argv 走列表传，**不做 shell 解析**。

    ## 默认 `--opsec` 的理由

    实测这个开关做两件事：

      ① 速率降到 10 —— 授权测试不等于可以把目标扫到服务不可用；
      ② 跳过所有 high/medium 风险的 POC，只跑 lowest/low。

    ②比①重要：它让「误配一个激进 POC 把目标打坏」在**引擎层**就不可能，
    而不是靠提示词请求模型克制。代价是真扫不出高危命中 —— 那是另一档设置
    的事，必须由部署者显式决定，不能由模型在参数里打开。
    """
    binary = resolve_gogo_bin()
    if "--opsec" not in argv:
        argv = [*argv, "--opsec"]
    try:
        return subprocess.run(
            [binary, *argv], capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"gogo 在 {timeout} 秒内没跑完。大网段先降档：把 ports 收窄（如 top1）、"
            "或确认 smart 参数设对了。"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"gogo 启动失败（{exc}）。检查 GOGO_BIN 与挂载权限。") from exc


def _parse_jsonl(stdout: str) -> list[dict]:
    """gogo 的 `-o jl` 是每行一个 JSON。混进来的非 JSON 行跳过（它偶尔打日志）。"""
    out: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _trim_record(rec: dict) -> dict:
    """只留调用方用得上的字段，并把 `frameworks` 压平成名字列表。

    gogo 原样输出里 `frameworks` 是嵌套很深的 dict（`froms`/`tags`/
    `attributes`），对模型是纯噪音；压平成 `["nginx"]` 才能直接写进证据。
    信息性字段（status/title/midware/protocol）都留着。
    """
    frames = rec.get("frameworks")
    names: list[str] = []
    if isinstance(frames, dict):
        for key, val in frames.items():
            name = val.get("name") if isinstance(val, dict) else None
            names.append(str(name or key))
    out: dict = {
        "target": rec.get("ip"),
        "port": rec.get("port"),
        "protocol": rec.get("protocol"),
        "status": rec.get("status"),
        "title": rec.get("title"),
        "server": rec.get("midware"),
        "frameworks": names,
    }
    vulns = rec.get("vulns")
    if isinstance(vulns, dict) and vulns:
        out["vulns"] = [
            {"id": k, "severity": v.get("severity") if isinstance(v, dict) else None}
            for k, v in vulns.items()
        ]
    extracted = rec.get("extracted")
    if isinstance(extracted, dict) and extracted:
        out["extracted"] = extracted
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def tool_recon_scan(args: dict) -> str:
    target = _clean_target(args.get("target"))
    ports = _clean_ports(args.get("ports"))
    smart = str(args.get("smart") or "none")
    level = int(args.get("fingerprint_level") or 0)
    timeout = int(args.get("timeout_seconds") or DEFAULT_TIMEOUT)

    argv = ["-i", target, "-p", ports, "-o", "jl", "-q"]
    if smart != "none":
        if smart not in ("s", "ss", "sc"):
            raise ToolArgError("smart 只能是 none / s / ss / sc。")
        argv += ["-m", smart]
    # `-v` 是 bool（开主动指纹），给两次不等于更强；强度 2 换成别的档位
    # 这件事 gogo 没有独立开关，所以这里如实降级为 1。
    if level >= 1:
        argv.append("-v")

    proc = _run_gogo(argv, timeout=timeout)
    records = _parse_jsonl(proc.stdout)
    payload: dict = {
        "ok": True,
        "target": target,
        "ports_spec": ports,
        "alive_count": len(records),
        "results": [_trim_record(r) for r in records],
    }
    if not records:
        # ★「扫到 0 个」是**结论**，不是失败。必须说清，否则模型会以为工具坏了
        #   然后改用 http_request 一个个猜 —— 这正是历史上踩过的退化路径。
        payload["note"] = (
            "没有发现存活端口。可能原因：目标不可达、端口组合不对（试 ports=top2 或 "
            "common）、或大网段需要 smart=s。这是**结论**，不是工具故障。"
        )
    if proc.returncode != 0 and not records:
        payload["stderr_tail"] = (proc.stderr or "").strip()[-400:]
    return json.dumps(payload, ensure_ascii=False)


def tool_run_poc(args: dict) -> str:
    target = _clean_target(args.get("target"))
    timeout = int(args.get("timeout_seconds") or DEFAULT_TIMEOUT)
    dirs = args.get("template_dirs") or []
    ids = args.get("template_ids") or []
    if isinstance(dirs, str):
        dirs = [dirs]
    if isinstance(ids, str):
        ids = [ids]

    host, port = _split_target(target)
    argv = ["-i", host, "-p", port, "-e", "-o", "jl", "-q"]

    if dirs:
        merged = convert_templates([str(d) for d in dirs])
        tmpl_path = _write_temp(merged)
        argv += ["--ef", tmpl_path]
    if ids:
        argv += ["-E", ",".join(str(i) for i in ids)]

    proc = _run_gogo(argv, timeout=timeout)
    hits: list[dict] = []
    for rec in _parse_jsonl(proc.stdout):
        vulns = rec.get("vulns")
        if not isinstance(vulns, dict):
            continue
        for vid, meta in vulns.items():
            detail = meta if isinstance(meta, dict) else {}
            hits.append(
                {
                    "template_id": vid,
                    "severity": detail.get("severity"),
                    "tags": detail.get("tags"),
                    "payload": detail.get("payload"),
                }
            )
    payload: dict = {
        "ok": True,
        "target": target,
        "hits": hits,
        "hit_count": len(hits),
        "templates_from": "custom" if dirs else "gogo-embedded",
        "opsec": True,
        "judgement_hint": (
            "POC 命中是「单次响应匹配」，在 Suvex 的证据分级里最多 L1（机审候选）。"
            "要升到 L2 必须补对照实验 —— 不要把这里的命中直接当 confirmed。"
        ),
    }
    if not hits:
        payload["note"] = (
            "这批模板没有命中。**这不等于目标没有漏洞** —— 只说明这批模板没匹配上。"
            "已知技术栈时用 template_ids 精确指定，或换 template_dirs。"
        )
    if proc.returncode != 0 and not hits:
        payload["stderr_tail"] = (proc.stderr or "").strip()[-400:]
    return json.dumps(payload, ensure_ascii=False)


def tool_extract_secrets(args: dict) -> str:
    target = _clean_target(args.get("target"))
    timeout = int(args.get("timeout_seconds") or DEFAULT_TIMEOUT)
    groups = args.get("groups") or ["pentest"]
    if isinstance(groups, str):
        groups = [groups]
    spec = ",".join(str(g) for g in groups)

    host, port = _split_target(target)
    argv = ["-i", host, "-p", port, "--extract", spec, "-o", "jl", "-q"]
    proc = _run_gogo(argv, timeout=timeout)

    merged: dict[str, list] = {}
    for rec in _parse_jsonl(proc.stdout):
        got = rec.get("extracted")
        if not isinstance(got, dict):
            continue
        for cat, values in got.items():
            if not isinstance(values, list):
                continue
            bucket = merged.setdefault(str(cat), [])
            for val in values:
                text = str(val).strip()
                if text and text not in bucket:
                    bucket.append(text)

    total = sum(len(v) for v in merged.values())
    payload: dict = {
        "ok": True,
        "target": target,
        "groups": spec,
        "total": total,
        "by_category": merged,
    }
    if total == 0:
        payload["note"] = (
            "没有抽到任何东西。这个工具只看目标**首屏响应**（不抓 JS 文件内容、"
            "不爬子页面）；要覆盖更多先用 recon_scan 拿到路径再逐个打。"
        )
    if proc.returncode != 0 and total == 0:
        payload["stderr_tail"] = (proc.stderr or "").strip()[-400:]
    return json.dumps(payload, ensure_ascii=False)


# ── nuclei 模板 → neutron 规整 ────────────────────────────────────

#: 这些 key 在 neutron 里要求是 string，而 nuclei 允许写成 list。
_SEQ_TO_STR = ("description", "impact", "remediation", "name", "author", "severity", "tags")


def normalise_template(doc: dict) -> dict:
    """把一个 nuclei 模板规整成 neutron 能 unmarshal 的形状。

    ## 为什么要这一步（实测数据）

    直接喂 13,619 个 nuclei 模板给 gogo，**只有 4,234 个能载入**（31%），
    且失败是**整批的**（`yaml.Unmarshal` 原子，一个形状不对就全不进）。
    错误只有四类，全在 `info` 的元数据上：

        cannot unmarshal !!str `https:/...` into []string   ← reference 写成裸串
        cannot unmarshal !!seq into string                  ← cwe-id / description 写成 list
        （另有 flow: http(1) && http(2) 这种 neutron 没有的编排语法）

    规整这几处之后 13,619 / 13,619 全部载入（实测 39 秒）。
    """
    info = doc.setdefault("info", {})
    if not isinstance(info, dict):
        doc["info"] = info = {}

    ref = info.get("reference")
    if isinstance(ref, str):
        info["reference"] = [x.strip() for x in ref.splitlines() if x.strip()]
    elif isinstance(ref, list):
        info["reference"] = [str(x) for x in ref]

    for key in _SEQ_TO_STR:
        val = info.get(key)
        if isinstance(val, list):
            info[key] = ",".join(str(x) for x in val)

    cls = info.get("classification")
    if isinstance(cls, dict):
        for key, val in list(cls.items()):
            if isinstance(val, list):
                cls[key] = ",".join(str(x) for x in val)
    elif cls is not None:
        info.pop("classification", None)

    if "variables" in doc and not isinstance(doc["variables"], dict):
        doc.pop("variables", None)

    # nuclei 的 `requests:` 就是 neutron 的 `http:`
    if "requests" in doc and "http" not in doc:
        doc["http"] = doc.pop("requests")

    # `flow`（nuclei 的执行编排 DSL）在 neutron 里没有等价物。删掉意味着多步
    # http 会顺序全执行 —— 比「按条件串行」粗，可能多打几个请求。这个偏差是
    # 刻意的：不删则整批载入失败（一个坏模板顶掉一万个）。
    doc.pop("flow", None)

    tags = str(info.get("tags", "") or "")
    if "http" not in tags:
        info["tags"] = (tags + ",http").strip(",")

    return doc


def convert_templates(dirs: list[str]) -> str:
    """把一组目录下的 nuclei 模板合并成一个 neutron 可读的 YAML 数组。

    没有可用模板时抛 ToolArgError —— **不能回空串**：空模板文件会让 gogo
    静默跑 0 个 POC，看起来像「没命中」，而真实原因是模板没读进来。
    """
    # ★ 目录校验放在 import 之前：路径写错是**调用方**的错误，要优先于
    #   「环境缺 PyYAML」报出来。顺序反了的话，模型看到的是部署问题，
    #   于是它不会去改自己写错的路径（实测踩过这个顺序）。
    for base in dirs:
        if not os.path.isdir(base):
            raise ToolArgError(f"template_dirs 里的 `{base}` 不是目录。")

    import yaml  # 局部 import：只有这个工具需要，缺依赖时另外两个仍可用

    docs: list[dict] = []
    skipped = 0
    for base in dirs:
        for root, _subdirs, files in os.walk(base):
            for fname in sorted(files):
                if not fname.endswith((".yaml", ".yml")):
                    continue
                try:
                    with open(os.path.join(root, fname), "rb") as fh:
                        doc = yaml.safe_load(fh)
                except Exception:  # noqa: BLE001 - 坏模板跳过，不让整批失败
                    skipped += 1
                    continue
                if not isinstance(doc, dict) or "id" not in doc:
                    skipped += 1
                    continue
                docs.append(normalise_template(doc))
    if not docs:
        raise ToolArgError(
            f"在 {dirs} 下没有找到可用的 POC 模板（跳过 {skipped} 个）。"
            "确认目录里是 nuclei/neutron 的 YAML 模板。"
        )
    return yaml.dump(docs, allow_unicode=True, sort_keys=False)


def _write_temp(text: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


HANDLERS = {
    "recon_scan": tool_recon_scan,
    "run_poc": tool_run_poc,
    "extract_secrets": tool_extract_secrets,
}


# ── JSON-RPC over stdio ───────────────────────────────────────────


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _text_result(text: str, *, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _call_tool(name: str, args: object) -> dict:
    """执行一个工具，把**所有**失败都转成「给模型看的一句话」。"""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return _text_result(
                f"参数 arguments 不是合法 JSON：{args[:80]!r}。用 describe_mcp 看 schema。",
                is_error=True,
            )
    if not isinstance(args, dict):
        args = {}
    handler = HANDLERS.get(name)
    if handler is None:
        return _text_result(
            f"没有名为 `{name}` 的工具。可用：{', '.join(HANDLERS)}", is_error=True
        )
    try:
        return _text_result(handler(args))
    except ToolArgError as exc:
        return _text_result(str(exc), is_error=True)
    except ModuleNotFoundError as exc:
        # 缺 PyYAML 是可修的**部署**问题，不能说成「工具执行失败」。
        # 实测踩过：sandbox 镜像的 /opt/venv 里没有 pyyaml。
        return _text_result(
            f"缺少 Python 依赖 `{exc.name}`。run_poc 需要 PyYAML 解析模板；"
            "部署侧 `pip install pyyaml` 后重试。另外两个工具不依赖它。",
            is_error=True,
        )
    except PermissionError as exc:
        return _text_result(
            f"gogo 可执行文件不可执行（{exc}）。检查 GOGO_BIN 的挂载与 +x 位。",
            is_error=True,
        )
    except Exception as exc:  # noqa: BLE001 - 工具失败不能让 server 崩
        return _text_result(f"工具执行失败（{type(exc).__name__}）：{exc}", is_error=True)


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    mid = msg.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        result = _call_tool(str(params.get("name")), params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    if mid is not None:
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "error": {"code": -32601, "message": f"unknown method {method}"},
        }
    return None


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        reply = handle(msg)
        if reply is not None:
            _send(reply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
