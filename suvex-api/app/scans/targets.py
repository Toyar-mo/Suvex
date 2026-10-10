"""目标归一化与校验（契约 §2.8）。

**这份实现与前端 `src/lib/validate-target.ts` 是同一套规则**，
逐条对照写出来的。为什么必须一致（契约 §2.8）：

> `api.example.com`、`https://api.example.com`、`https://api.example.com/`
> 是同一个目标的三种写法。原样存下来，列表里会出现三条看起来不同的记录 ——
> 而它们其实是同一个东西。

前端做了一遍校验，后端**仍然要做**：前端可以绕过（改 JS、直接 curl），
而"目标写错"在这里的代价是对着错误的主机发包。

## 三条刻意"不做得太严"的规则

这是安全测试工具，用户会填内网 IP、带端口的主机、非标准 TLD。
过严的正则会拦掉合法输入，而用户**无法绕过**，只能改工具 ——
那比漏校验更烦人。所以只拦"明显不对"的：

* `url`：解析不了、或者主机名既不含 `.` 又不是 `localhost`；
* `repo`：既不是 `https://…/…` 也不是 `git@host:…`；
* `path`：不是绝对路径。

## 以及两条**刻意不做**的归一化

`repo` 与 `path` 原样保留，不小写、不去尾斜杠。路径大小写在 Windows 上不敏感、
在 Linux 上敏感；仓库地址的 `.git` 后缀会影响克隆行为。
这两类"看起来不统一"是**正确的**，不要去"修"它。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.vocab import TargetKind

# 与前端 `TARGET_KIND_HINT` 的 example 保持一致 —— 错误提示里给出的格式示例
# 前后端不一致，会让用户在两处看到两种"正确写法"。
_HINTS = {
    TargetKind.URL: "api.example.com",
    TargetKind.REPO: "https://github.com/acme/backend.git",
    TargetKind.PATH: "D:\\projects\\acme",
    TargetKind.SERVICE: "1.2.3.4:9999",
    # `artifact` 没有"正确写法"可举例 —— 它要的恰恰是**什么都不填**。
    # 这里放一句话而不是一个示例值，免得有人把它当成占位符照抄进输入框。
    TargetKind.ARTIFACT: "不需要地址，只用附件",
}

_URL_SCHEME = re.compile(r"^https?://", re.IGNORECASE)
_GIT_HTTPS = re.compile(r"^https?://\S+/\S+")
_GIT_SSH = re.compile(r"^git@[\w.-]+:\S+")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")

# ── TCP 目标的「粘贴即识别」（任务 15）────────────────────
#
# ## 为什么值得为它写正则
#
# CTF 平台给的就是一条连接命令：
#
#     openssl s_client -connect 35b162d9...tcp-ctf2.dasctf.com:9999 -servername ... -quiet
#
# 让用户从里面**手工抄出** `host:9999` 再填进来，是白费一遍手 ——
# 而抄错一个字符的代价是"对着不存在的主机发包"，事后极难归因。
# 直接接受粘贴并归一化成 `host:port`，是这里唯一合理的交互。
#
# ⚠️ 抽不出来就**如实报错**，不做模糊猜测。猜错的方向是
# "把非法输入当合法"，正是最不该错的那一边（与 `_parse_url` 同一条原则）。
_BARE_SERVICE = re.compile(r"^([\w.\-]+):(\d{1,5})$")

_PASTE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # openssl s_client -connect H:P -servername H
    re.compile(r"-connect\s+([\w.\-]+):(\d{1,5})", re.IGNORECASE),
    # socat - TCP:H:P
    re.compile(r"\bTCP:([\w.\-]+):(\d{1,5})", re.IGNORECASE),
    # nc / ncat / netcat [选项] H P —— 端口是独立的一个参数
    re.compile(
        r"\b(?:nc|ncat|netcat)\s+(?:-\S+\s+)*([\w.\-]+)\s+(\d{1,5})(?![\d.])",
        re.IGNORECASE,
    ),
)

_MIN_PORT = 1
_MAX_PORT = 65535


def extract_service(value: str) -> tuple[str, int] | None:
    """从用户输入里抽出 `(host, port)`。抽不出返回 `None`。

    先试裸 `host:port`，再试平台那几种连接命令。顺序不能反：
    裸形式是精确匹配（`^...$`），命令形式是搜索（`search`），
    先搜索会让 `host:9999` 这种输入也被命令正则碰运气。
    """
    text = (value or "").strip()
    if not text:
        return None

    bare = _BARE_SERVICE.match(text)
    if bare:
        return bare.group(1), int(bare.group(2))

    for pattern in _PASTE_PATTERNS:
        found = pattern.search(text)
        if found:
            return found.group(1), int(found.group(2))
    return None


@dataclass(frozen=True, slots=True)
class TargetCheck:
    """校验结果。

    形状与前端 `TargetValidation` 对齐：`ok` + `error` + `hint`。
    `hint` 是**提示而不是错误** —— 它告诉用户"会发生什么"
    （"将按 https:// 访问"、"将以只读方式克隆"），
    这类信息在确认页上有用，不该塞进 error 里。
    """

    ok: bool
    error: str | None = None
    hint: str | None = None


def normalize(kind: str, raw: str) -> str:
    """归一化目标。`Scan.target` 存的是这个结果，不是用户原始输入。"""
    value = (raw or "").strip()

    if kind == TargetKind.ARTIFACT:
        # ★ 这类目标**没有地址可归一化**（任务 17）。
        #
        # 存进 `scans.target` 的最终是**附件名**（`tryre.zip`），
        # 而那要等 `create_scan` 看到暂存附件之后才知道 —— 那一步在
        # 这个纯函数里做不到（它没有数据库、也没有暂存区）。
        # 所以这里返回空串，由 `create_scan` 负责填上。
        #
        # ⚠️ 这条必须放在下面"空值早退"**之前**：早退会把非法的非空输入
        #    原样返回，而 `artifact` 下任何地址都不该被存下来。
        return ""

    if not value:
        return value

    if kind == TargetKind.URL:
        parsed = _parse_url(value)
        if parsed is None:
            return value
        # ⚠️ 用 `host` 而不是 `hostname`：**保留端口**。
        # 同一主机的不同端口是两个不同目标（`:8443` 是另一个服务）。
        # 也刻意不加尾斜杠。
        return parsed["host"]

    if kind == TargetKind.SERVICE:
        # ★ 粘贴进来的连接命令在这里被归一化成 `host:port`。
        #   抽不出来就原样返回 —— 那时 `validate` 已经报错了，
        #   归一化不该为一次注定失败的提交编一个值出来。
        found = extract_service(value)
        return f"{found[0]}:{found[1]}" if found else value

    # 仓库与路径保留原样 —— 大小写与结尾斜杠都可能有意义（尤其 Windows）。
    return value


def default_name(kind: str, raw: str) -> str:
    """从目标推导一个可读的扫描名（用户没显式填名字时用它）。

    不直接用原文是因为 URL 会带上协议前缀，作为列表里的一行标签太长且噪音大。
    """
    value = (raw or "").strip()
    if not value:
        return "未命名扫描"

    if kind == TargetKind.ARTIFACT:
        # 这里拿到的是**附件名**：`create_scan` 会把第一个附件的文件名
        # 写进 `scans.target`，然后按惯例拿它推导名字。
        #
        # 去掉扩展名（`tryre.zip` → `tryre`）：列表里一行标签越长越难扫，
        # 而 `.zip` 这个后缀对"这是哪道题"不提供任何信息。
        #
        # ⚠️ 刻意不特殊处理多扩展名（`chall.tar.gz` → `chall.tar`）。
        #    多扩展名没有统一答案 —— `libc.so.6` 该去掉什么？—— 而猜错的
        #    代价只是名字难看一点，不是错。为此写一套规则不划算。
        stem = value.rsplit(".", 1)[0] if "." in value else value
        return stem or value

    if kind == TargetKind.URL:
        parsed = _parse_url(value)
        return parsed["host"] if parsed else value

    if kind == TargetKind.SERVICE:
        # 带上端口：同一主机的不同端口是两个完全不同的服务，
        # 只留主机名会让列表里两行看起来是同一个目标。
        found = extract_service(value)
        return f"{found[0]}:{found[1]}" if found else value

    if kind == TargetKind.REPO:
        stripped = re.sub(r"\.git$", "", value)
        stripped = re.sub(r"^git@[\w.-]+:", "", stripped)
        stripped = re.sub(r"^https?://[^/]+/", "", stripped)
        return stripped or value

    segments = [part for part in re.split(r"[\\/]", value) if part]
    return segments[-1] if segments else value


def validate(kind: str, raw: str) -> TargetCheck:
    """校验。`Scan` 创建时必须先过这一关。"""
    value = (raw or "").strip()

    # ★ `artifact` 必须在"空值早退"**之前**判（任务 17）。
    #
    # 这是本函数里唯一一个**空值是合法状态**的分支。下面那条早退挡的是
    # "用户忘了填地址"，而对 `artifact` 来说，"没填地址"正是**正确**的 ——
    # 顺序反了的话，逆向题会在建扫描的第一步被判「请填写目标」，
    # 而且报错完全说不出该怎么办。
    if kind == TargetKind.ARTIFACT:
        return _validate_artifact(value)

    if not value:
        return TargetCheck(False, "请填写目标", None)

    if kind == TargetKind.URL:
        return _validate_url(value)
    if kind == TargetKind.REPO:
        return _validate_repo(value)
    if kind == TargetKind.PATH:
        return _validate_path(value)
    if kind == TargetKind.SERVICE:
        return _validate_service(value)
    return TargetCheck(False, f"不认识的目标类型：{kind}", None)


def normalize_and_validate(kind: str, raw: str) -> tuple[str, TargetCheck]:
    """归一化 + 校验。校验用的是**原始输入**，归一化在前端语义之后。

    顺序很重要：先校验再归一化。反过来会把
    `https://api.example.com/` 归一化成 `api.example.com` 之后再校验，
    而那时"有没有写协议"这条信息已经没了 —— 而提示语正是靠它决定的。
    """
    check = validate(kind, raw)
    return normalize(kind, raw), check


# ── 内部 ──────────────────────────────────────────────────


def _parse_url(value: str) -> dict[str, str] | None:
    """尽量贴近 JS 的 `new URL()` 行为。

    用 `urllib.parse` 而不是正则：URL 的解析规则比看起来复杂得多
    （userinfo、IPv6 字面量、百分号编码），手写的正则会在这些边角上出错，
    而出错的方向是"把非法输入当合法"，正好是最不该错的那一边。

    ⚠️ 与 JS 的一处已知差异：`new URL('https://a b')` 在 JS 里抛异常，
    而 `urlsplit` 会接受。所以这里额外拦掉空白字符 —— 一个带空格的主机名
    一定是误输入，放过去只会得到一次莫名其妙的连接失败。
    """
    candidate = value if _URL_SCHEME.match(value) else f"https://{value}"
    if any(ch.isspace() for ch in candidate):
        return None

    from urllib.parse import urlsplit

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None

    if not parts.scheme or not parts.hostname:
        return None
    # 端口写错（`:abc`、`:99999`）会在这里抛 —— JS 的 URL 同样会抛。
    try:
        port = parts.port
    except ValueError:
        return None

    host = parts.hostname
    if port is not None:
        host = f"{host}:{port}"
    return {"scheme": parts.scheme, "hostname": parts.hostname, "host": host}


def _validate_url(value: str) -> TargetCheck:
    parsed = _parse_url(value)
    if parsed is None:
        return TargetCheck(
            False, "不是合法的 URL", f"格式应为 {_HINTS[TargetKind.URL]}"
        )

    hostname = parsed["hostname"]
    # `localhost` 明确放行：本地服务是合理目标（也是本平台自己的部署形态）。
    if "." not in hostname and hostname != "localhost":
        return TargetCheck(
            False,
            "主机名看起来不完整",
            "需要可解析的域名或 IP，例如 api.example.com",
        )

    has_scheme = bool(_URL_SCHEME.match(value))
    return TargetCheck(
        True,
        None,
        # 没写协议时预告将要使用的协议 —— 这是有用的信息，不是错误。
        None if has_scheme else f"将按 {parsed['scheme']}:// 访问",
    )


def _validate_repo(value: str) -> TargetCheck:
    if _GIT_HTTPS.match(value) or _GIT_SSH.match(value):
        return TargetCheck(True, None, "将以只读方式克隆到沙箱。")
    return TargetCheck(
        False,
        "不是合法的仓库地址",
        f"支持 https 或 git@ 形式，例如 {_HINTS[TargetKind.REPO]}",
    )


def _validate_path(value: str) -> TargetCheck:
    is_absolute = value.startswith("/") or bool(_WINDOWS_DRIVE.match(value))
    if not is_absolute:
        return TargetCheck(
            False,
            "需要绝对路径",
            f"相对路径无法确定指向哪里，例如 {_HINTS[TargetKind.PATH]}",
        )
    return TargetCheck(True, None, "将直接读取该目录，不会修改文件。")


def _validate_service(value: str) -> TargetCheck:
    """校验裸 TCP 目标（任务 15）。

    ⚠️ `hint` 里那句"**不是 HTTP**"不是装饰：执行后端（strix）的类型系统里
    **没有 TCP 服务这一类**，`host:9999` 会被它归类成 `web_application`
    并补上 `https://`（见 `interface/utils.py` 的 `infer_target_type`）。
    我们改不了它的分类，只能靠指令把这件事说清楚 —— 而这里正是
    用户第一次被告知"这是 TCP 不是网站"的地方，口径必须与指令一致。
    """
    found = extract_service(value)
    if found is None:
        return TargetCheck(
            False,
            "不是合法的 TCP 目标",
            f"格式应为 {_HINTS[TargetKind.SERVICE]}，"
            "也可以直接粘贴平台的连接命令（openssl s_client / nc / socat）",
        )

    host, port = found
    if not (_MIN_PORT <= port <= _MAX_PORT):
        return TargetCheck(
            False,
            f"端口 {port} 超出范围",
            f"端口应在 {_MIN_PORT}-{_MAX_PORT} 之间",
        )
    # 与 URL 那条同规则：`localhost` 放行（本地服务是合理目标），
    # 其余必须像域名或 IP —— 一个不含点的单段主机名多半是抄漏了。
    if "." not in host and host != "localhost":
        return TargetCheck(
            False,
            "主机名看起来不完整",
            "需要可解析的域名或 IP，例如 " + _HINTS[TargetKind.SERVICE],
        )

    canonical = f"{host}:{port}"
    if value.strip() != canonical:
        return TargetCheck(
            True,
            None,
            f"已从粘贴的命令里提取 {canonical}；将按原始 TCP 连接（不是 HTTP）。",
        )
    return TargetCheck(True, None, "将按原始 TCP 连接（不是 HTTP）。")


def _validate_artifact(value: str) -> TargetCheck:
    """校验「只有附件」这类目标（任务 17）。

    ## 它校验的其实是"目标地址必须**空着**"

    反直觉，但必要：这个分支里**唯一的错误是"用户填了地址"**。

    ## ★ 为什么不干脆把用户填的地址忽略掉

    因为 `artifact` 模式下我们**不会**把任何地址交给执行后端 ——
    交给 strix 的 target 是后端从附件解压出来的那个目录（见
    `service.build_spec`）。所以用户填的地址会被**静默丢弃**。

    而"我明明填了地址，它却没去扫"是最难归因的一类问题：用户会认为
    扫过了、只是没发现问题。当场说清楚，比让他事后从行为流里
    慢慢察觉"agent 好像压根没连过网"要好得多。

    ## 为什么不像其他四种那样给"将会发生什么"的 hint

    其他几条的 hint 都是预告（将按 https 访问、将以只读方式克隆到沙箱）。
    这条没有"将会" —— 真正会发生什么取决于**用户接下来传的附件**，
    而那一步还没发生。所以这里给的是**下一步该做什么**：
    一句"去哪传文件"比一句"本模式不需要地址"有用得多。
    """
    if value:
        return TargetCheck(
            False,
            "这类题目不需要目标地址",
            "「题目附件」模式只用附件，不连任何地址。"
            "如果要扫描一个地址，请改选对应的目标类型。",
        )
    return TargetCheck(
        True,
        None,
        # 措辞用「题目附件」而不是「题目文件」—— 与界面上那一步、以及
        # 错误信息里用的词**同一个**。同一个东西在一处叫一个名字，
        # 用户才知道"提示里说的那个"就是"我界面上看到的那个"。
        "请在下一步「任务背景」里上传题目附件（zip / ELF / 流量包 / 源码都可以）。",
    )
