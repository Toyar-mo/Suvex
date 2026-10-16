"""目标可达性与协议预检（任务 11）。

## 为什么需要它

实测有过一次扫描跑了 **32 分钟零产出**：用户填的目标是
`ad7ad58354a66e0e4f579c92.http-ctf2.dasctf.com`（**没有协议前缀**），
ajent 自行把它补成了 `https://`，而那个靶机**只监听 http**：

    http://.../   → 404（0.06 秒，正常）
    https://.../  → 000（TLS 握手直接失败）

于是每个请求都撞在握手上，`ffuf` 目录爆破在大量连接错误里空转，
agent 反复用**空输入**轮询同一个 shell 会话（45 次
`write_stdin {"chars":""}`，等待从 15 秒涨到 60 秒），而
`--max-turns` 拦不住它 —— 回合数根本没在涨，时间却在走。

## 这里做什么

发起扫描**之前**探一次，把结果落成两部分：

1. **`effective_target`** —— 补上真正能用的协议，交给 strix。
   这是关键的一半：只提示不修正的话，扫描照样会失败。
2. **`notice`** —— 一句人话，放进创建扫描的响应里让界面能提示。
   用户填 `example.com` 时他会以为是 `https://example.com`，
   而真相可能相反。

## 不做的事

* **不阻断创建**。探测失败不等于扫描一定没用（目标可能只在内网可达、
  或者需要特定 Host 头、或者刚好那一刻在重启）。拦下来会让
  "我就是要在它恢复后立刻扫"变得做不到 —— 所以只提示。
* **不猜路径**。探的是根路径，不是"哪个 URL 有内容"。
  根路径 404 但服务活着是完全正常的（这次的目标就是）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger("suvex.scans")

# 单个协议的探测超时。**故意很短**：这是一个"顺手做的预检"，
# 不该让创建扫描这个动作等上几秒。目标不可达时也只需要知道"连不上"。
PROBE_TIMEOUT_SECONDS = 4.0


@dataclass(frozen=True, slots=True)
class Reachability:
    """预检结果。"""

    # 真正能用的地址（补好协议的）。探测全失败时是 None —— 此时
    # 调用方保留用户原本填的值，不要瞎改。
    effective_target: str | None
    # 给用户看的一句话。没有值得说的就返回 None（界面不显示任何提示）。
    notice: str | None
    # 探测细节，仅用于日志与诊断
    http_ok: bool
    https_ok: bool


def _with_scheme(url: str, scheme: str) -> str:
    """把 URL 的协议换成指定的，其余（host/port/path/query）原样保留。"""
    parts = urlsplit(url if "://" in url else f"//{url}")
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))


def _probe(url: str, timeout: float) -> bool:
    """探一次根路径。**任何 HTTP 应答都算通**（包括 4xx/5xx）。

    ★ 判据是"服务应答了"，不是"返回 200"。

    这次的目标根路径就是 404（`Target not found`），而它明明活着。
    按 2xx 判会把一个正常可打的目标标成"不可达"，然后用户就会去
    排查一个根本不存在的问题。

    ## ★ 为什么必须 `trust_env=False`（出站 SSRF 面）

    `url` 是**用户填的扫描目标**（`POST /scans` 的 `target`，经
    `service.check_reachability` 传进来），鉴权只要 `scan` scope ——
    也就是"会对真实目标发包，这正是 scan scope 的语义"。

    而 `httpx` 的 `trust_env` **默认是 `True`**，意思是它会去读环境里的
    `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`，把请求**发到那个代理**。
    于是"服务器上的环境变量"就成了一个能把出站流量改道的地方：
    实测（本机起真转发代理，判据是代理收到的请求行）默认配置下
    请求真的路由到了代理；`trust_env=False` 则不经过它。

    这里的目标是用户可控的，所以这一行是**防 SSRF**，与本文件
    "内网地址故意不拦"那一条（见 `check_reachability` 的说明）不冲突：
    那说的是"允许打内网"，这说的是"不要把请求交给一个我们没选过的代理"。

    ⚠️ `trust_env=False` 同时会关掉读 `.netrc`（同样是外部输入来源）。
       部署在需要代理才能出网的环境时，代理要显式配（而不是靠环境变量）。
    """
    try:
        import httpx

        response = httpx.get(url, timeout=timeout, follow_redirects=False, trust_env=False)
        # 有状态码 = 服务应答了
        return response.status_code > 0
    except Exception:  # noqa: BLE001
        return False


def check_reachability(
    target: str,
    *,
    origin: str | None = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> Reachability:
    """探测目标在 http / https 下哪个可用。

    只在目标是 **URL 形态**（或者看起来像域名/IP）时才有意义；
    调用方负责只对 `target_kind == "url"` 调它。

    ## 为什么要两个参数（`target` 与 `origin`）

    ⚠️ 这两个**不能合并成一个**，而第一版就是只传了一个。

    · `target` 是**归一化后**的值（`api.example.com`）—— 用它构造要探测的
      地址。归一化的定义就是剥掉协议与路径（契约 §2.8），所以它天然不含
      路径：交给 strix 的地址必须是"这台主机"，不能是"这台主机下的某个页面"
      （那会把扫描范围无声地缩小到一个路径）。
    · `origin` 是**用户的原始输入**（`https://api.example.com/admin`）——
      只用来回答一个问题："**用户自己写协议了吗**"。

    只传 `target` 的话，`has_scheme` **恒为假**（归一化已经剥掉了协议），
    于是「用户显式写了协议就绝不改写」这条规则**静默失效**。
    实机上正是这么暴露的：显式创建 `https://127.0.0.1:5173`，
    预检却回「已按 http 发起扫描」并把它改写了。
    """
    text = (target or "").strip()
    if not text:
        return Reachability(None, None, False, False)

    # 判断"用户写没写协议"只看原始输入。`origin` 没给时（直接调用本函数
    # 的场景）退回 `target` 自身，行为与单参数版本一致。
    source = (origin or target or "").strip()
    scheme = urlsplit(source).scheme.lower() if "://" in source else ""

    # 用户明确写了协议：只探那一个，不擅自改。他会写 https 通常是有理由的
    # （比如前面有反代），而"帮他换掉"可能让扫描打到一个不该打的地方。
    if scheme:
        url = _with_scheme(text, scheme)
        ok = _probe(url, timeout)
        if ok:
            return Reachability(url, None, scheme == "http", scheme == "https")
        other = "http" if scheme == "https" else "https"
        # 另一条协议通吗？通了就提示（但仍**不改**用户填的协议）
        if _probe(_with_scheme(text, other), timeout):
            return Reachability(
                url,
                f"目标在 {other}:// 下有应答，但你填的是 {scheme}:// —— "
                f"如果扫描一直没进展，先确认该用哪个协议。",
                other == "http",
                other == "https",
            )
        return Reachability(
            url,
            "目标没有应答（连接超时或被拒绝）。扫描仍会发起，但它大概率拿不到东西。",
            False,
            False,
        )

    # 没写协议：两个都试，按这个顺序
    #   https 优先 —— 现代站点多数如此，而且用户没写协议时心里想的多半是它
    https_url = _with_scheme(text, "https")
    http_url = _with_scheme(text, "http")

    https_ok = _probe(https_url, timeout)
    http_ok = _probe(http_url, timeout)

    if https_ok:
        return Reachability(https_url, None, http_ok, True)
    if http_ok:
        return Reachability(
            http_url,
            "该目标只在 http:// 下应答（https 握手失败）。已按 http 发起扫描。",
            True,
            False,
        )

    # 两个都不通：保留用户填的（补上 https 作为形式上的完整 URL），
    # 并如实说清"它没应答"。**不阻断**，理由见模块 docstring。
    return Reachability(
        https_url,
        "目标在 http 与 https 下都没有应答。扫描仍会发起，但多半不会有结果。",
        False,
        False,
    )


# ══════════════════════════════════════════════════════════
# TCP 目标预检（任务 15）
# ══════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class TcpReachability:
    """裸 TCP 目标的预检结果。

    ⚠️ 与上面的 `Reachability` 是**两个类型**，刻意不合并：
    那一份带 `http_ok` / `https_ok` 两个探针和"该用哪个协议"的结论，
    而裸 TCP **没有协议可选** —— 只有"端口通不通"。
    硬塞进一个类型会让一半字段对一半调用方永远没有意义。
    """

    # 给用户看的一句话。没有值得说的就是 None（界面不显示提示）。
    notice: str | None
    ok: bool


def check_tcp_reachability(
    host: str,
    port: int,
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> TcpReachability:
    """对裸 TCP 端口做一次 connect 探测。

    ## 只连，不写

    ★ 判据只有一条：**TCP 三次握手成不成**。连上之后**不发送任何字节**。

    为什么不发：我们不知道这个服务说什么协议，而乱发数据可能触发它的
    解析逻辑 —— CTF 靶机常常一被污染就得重开（有些还不可重开）。
    探测的收益（知道"端口开着"）远小于这个代价。

    这与 HTTP 预检那条"任何应答都算通（包括 4xx/5xx）"是同一条原则：
    **判据是"服务应答了"，不是"它给了我们想要的东西"。**

    ⚠️ **不阻断创建**：探测失败不等于扫描没用（目标可能只在内网可达、
    有 IP 白名单、或者此刻正在重启）。与 HTTP 预检同样处理。
    """
    import socket

    target = f"{host}:{port}"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return TcpReachability(None, True)
    except TimeoutError:
        return TcpReachability(
            f"{target} 在 {timeout:.0f} 秒内没有完成 TCP 握手。"
            "扫描仍会发起，但多半连不上 —— 也请留意是否需要白名单或内网可达。",
            False,
        )
    except OSError as exc:
        # 域名解析不了（gaierror）与"连接被拒"都落在这里。
        # 如实带上系统给的原因，不替它编一句更"友好"的话 ——
        # "Name or service not known" 对排查的人比"无法连接"有用得多。
        return TcpReachability(f"{target} 无法连接：{exc}。扫描仍会发起。", False)
