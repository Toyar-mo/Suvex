"""LLM 连通性探测：用保存的配置真的发一次最小化请求。

## "最小化"是字面意思

一次探测只做一件事：让模型回一句话。所以请求体是
`{"messages":[{"role":"user","content":"ping"}], "max_tokens":8}` ——
输出上限 8 个 token，不流式，不传工具，不传 system prompt。
目的是验证**凭据与连通性**，不是拿它当聊天用，也不该产生可观的费用。

## 协议判定：按接入点而不是按"类型"

前端 `PROVIDER_PRESETS` 里的四家分两种协议：

| 服务商            | baseUrl 形态                     | 协议                     |
| ----------------- | -------------------------------- | ------------------------ |
| DeepSeek / OpenAI | `https://api.deepseek.com`       | OpenAI `/chat/completions` |
| Ollama（本地）    | `http://127.0.0.1:11434`         | OpenAI 兼容的 `/v1/chat/completions` |
| Anthropic         | `https://api.anthropic.com`      | Messages API `/v1/messages` |

`ProviderAccount.kind` 只有 `cloud` / `local`，**区分不出 Anthropic 与 OpenAI** ——
两家都是 cloud。所以判定只能落在接入点上：host 里含 `anthropic` 就走 Messages API，
其余走 OpenAI 兼容。

这条规则不完美（自建网关转发 Anthropic 协议时会判错），但它是**唯一**能在
不改前端类型的前提下工作的规则。真要做对，得给 `ProviderAccount` 加一个
`protocol` 字段 —— 那是前端类型变更，属于契约范畴，记在契约 §7 里。

## 密钥怎么走

**只在请求头里**，绝不进 URL。很多 SDK 支持 `?api_key=` 这种写法，
一旦用了，密钥就会出现在服务端访问日志、反代的 access log、以及任何
抓包记录里 —— 而 access log 是最常被复制粘贴分享的东西。
"""

from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core import hostguard, netguard
from app.core.config import get_settings

# ── 官方默认接入点 ────────────────────────────────────────
# 契约 §2.9：`baseUrl` 空串 = 用官方默认地址。所以服务端必须知道这些默认值 ——
# 这份表与前端 `data/settings.ts` 的 `PROVIDER_PRESETS.defaultBaseUrl` **镜像**，
# 改一边必须改另一边。键是归一化后的 label（小写、去空格）。
# 与授权声明那句话一样，这类"两端必须一致"的常量需要在契约里留痕，
# 见 §2.9 的说明。
DEFAULT_BASE_URLS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "本地模型（ollama）": "http://127.0.0.1:11434",
    "ollama": "http://127.0.0.1:11434",
}

# 云厂商的元数据地址。无论 `allow_private_llm_endpoints` 怎么设都拦 ——
# 打这些地址的唯一用途就是取实例凭据，而本平台没有任何理由去取。
# 这是一道便宜的护栏，不是完整的 SSRF 防御：本平台的管理员本来就
# 有权让服务器去访问内网（那正是它的用途），所以这里只挡"明显不该碰的"。
#
# ★ 判据本身在 `app/core/hostguard.py`，**不再在这里维护一份清单**。
#   原先这里有一份自己的 `BLOCKED_HOSTS`（比 CTF2 那份多一个
#   `169.254.170.2`），两份清单已经开始漂移 —— 同一类地址在一条路径上
#   被拦、在另一条上放行。现在只有一份，两边都从那里取。
#
# ⚠️ 关键是它**不是字符串比对**：`169.254.169.254` 有 `2852039166`、
#   `0xA9FEA9FE`、`0251.0376.0251.0376`、`[::ffff:169.254.169.254]`
#   这些 `getaddrinfo` 全认、字符串一个都不等的写法。原判据是
#   `host in BLOCKED_HOSTS`，实测这五种写法**全部放行**（见
#   `hostguard` 的模块说明与 `tests/ctf2/test_hostguard.py`）。

# provider 品牌 → LiteLLM 的模型前缀（任务 3：要把模型名交给 strix）。
#
# strix 用 LiteLLM 表达模型，格式是 `<provider>/<model>`，例如
# `deepseek/deepseek-chat`。而我们的 `ProviderAccount` 里只有品牌名与模型名，
# 所以这层映射只能落在品牌名上 —— 与 `DEFAULT_BASE_URLS` 一样，
# **与前端 PROVIDER_PRESETS 镜像**，改一边必须改另一边。
#
# 认不出来的一律走 `openai`：绝大多数自建网关都是 OpenAI 兼容形状，
# 而配合 `LLM_API_BASE`（我们总是传）它能连上任意兼容端点。
# 这是"最常见的合理猜测"，而不是"随便挑一个"——
# 猜错的表现是认证/路径错误，日志里能看出来。
LITELLM_PREFIXES: dict[str, str] = {
    "deepseek": "deepseek",
    "openai": "openai",
    "anthropic": "anthropic",
    "本地模型（ollama）": "ollama",
    "ollama": "ollama",
}

ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """一次探测的结果。

    `duration_ms` 是**真实往返耗时**（从发出到收到响应头与体），
    不是估算 —— 任务要求返回耗时，而估算值对这个用途毫无意义：
    用户看它就是为了判断"这条链路到底慢不慢"。
    """

    ok: bool
    duration_ms: int
    # 失败时给用户看的中文一句话（可直接展示）。
    error: str | None = None
    # 成功时模型的回话，截断后保留。它同时是"真的打通了"的证据。
    reply: str | None = None
    # 实际请求的完整 URL。不含凭据，可以安全展示 ——
    # 它对排查"baseUrl 填错了"极有价值。
    endpoint: str = ""
    http_status: int | None = None
    # `reply` 是从哪儿取的："content"（正常回答）还是
    # "reasoning"（推理型模型：预算被推理吃掉，没有最终回答）。
    # **刻意不进 API 响应体**（那会改契约形状）；它给日志与测试用。
    reply_source: str = ""


def resolve_base_url(base_url: str, label: str) -> str:
    """空 baseUrl → 按 label 查官方默认。查不到就返回空串（调用方报错）。"""
    cleaned = (base_url or "").strip().rstrip("/")
    if cleaned:
        return cleaned
    return DEFAULT_BASE_URLS.get((label or "").strip().lower(), "")


def is_anthropic(base_url: str) -> bool:
    return "anthropic" in (urlsplit(base_url).hostname or "").lower()


def build_endpoint(base_url: str, *, anthropic: bool, local: bool) -> str:
    """`baseUrl` → 实际请求 URL。

    两条归一化规则，都不是随手加的：

    * **本地模型补 `/v1`**：Ollama 的 OpenAI 兼容端点是
      `/v1/chat/completions`，而预设里给的 baseUrl 是裸的
      `http://127.0.0.1:11434`（那是它的原生 API 根）。不补就会打到
      `/chat/completions` 上，得到 404 —— 而 404 会被用户理解成
      "Ollama 不支持"，方向完全错了。
    * **云端的 `/v1` 由用户填**：`https://api.openai.com/v1` 里已经带了，
      再补一次会变成 `/v1/v1/chat/completions`。所以仅当路径为空或 `/`
      且是本地服务时才补。
    """
    parts = urlsplit(base_url)
    path = parts.path.rstrip("/")

    if anthropic:
        # Messages API 固定带 /v1
        path = f"{path}/v1/messages" if not path.endswith("/v1") else f"{path}/messages"
    else:
        if local and path in ("", "/"):
            path = "/v1"
        path = f"{path}/chat/completions"

    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _local_address_guard(base_url: str) -> str | None:
    """返回拒绝原因，或 None 表示放行。

    两层，**互相独立、都要在**：

    1. **云元数据地址一律拦**（与 `allow_private_llm_endpoints` 无关）——
       判据是 `app/core/hostguard.is_blocked_host`，与 CTF2 那条路径
       **同一份实现**。它不是字符串比对：等价写法（十进制 / 十六进制 /
       八进制点分 / IPv4-mapped / 尾点）会先归一化成 IP 再判网段。
    2. `allow_private_llm_endpoints` 关时，拦**字面量**内网 / 回环 / link-local。

    ⚠️ 第 2 层与第 1 层是**两件事**，别合并：第 1 层挡的是"地址的等价
       写法绕过"（任何开关下都拦），第 2 层挡的是"直接写内网 IP"
       （受开关控制，因为本地 Ollama 就是这个形状）。把等价写法
       归一化之后**不要**顺手把它接到第 2 层 —— 那会变成"关闭开关就
       连元数据地址也能打"，把第 1 层整个废掉。
    """
    host = (urlsplit(base_url).hostname or "").lower()
    if hostguard.is_blocked_host(host):
        return (
            "该地址是云厂商的实例元数据服务，本平台不会向它发起请求。"
            "请填写真正的模型接入点。"
        )

    settings = get_settings()
    if settings.allow_private_llm_endpoints:
        return None

    # 第 2 层判"是不是内网"，也要用归一化后的地址 —— 否则
    # `http://2130706433/`（= 127.0.0.1）这种写法会绕过这一层。
    # `hostguard.normalize_host` 对域名返回 None，此时按原语义放行
    # （主机名是否解析到内网要靠 DNS 才知道，见下面的说明）。
    address = hostguard.normalize_host(host)
    if address is None:
        # 不是字面 IP（可能是主机名）。allow_private 关闭时只放行公网主机名，
        # 而主机名是否解析到内网要靠 DNS 才能知道 —— 那一步这里不做，
        # 因为它需要真正的解析，而解析本身就可能被指向内网。
        # 记下这个边界：这个开关挡的是"直接写内网 IP"，不是完整的 SSRF 防御。
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        # IPv4-mapped（`::ffff:127.0.0.1`）先映射回 IPv4，否则
        # `is_private` / `is_loopback` 判的是 IPv6 那一侧，结论会不对。
        address = address.ipv4_mapped
    if address.is_private or address.is_loopback or address.is_link_local:
        return (
            "当前配置不允许把 baseUrl 指向内网或回环地址"
            "（SUPEX_ALLOW_PRIVATE_LLM_ENDPOINTS=false）。"
        )
    return None


def _payload(*, anthropic: bool, model: str, prompt: str, max_tokens: int) -> dict:
    if anthropic:
        return {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
    return {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }


def _headers(*, anthropic: bool, api_key: str) -> dict[str, str]:
    """★ 凭据只在这里出现，且只进请求头。

    空 key 时不发认证头 —— 本地模型（Ollama）本来就不需要凭据，
    发一个空的 `Authorization: Bearer ` 反而会让某些实现直接 400。
    """
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if not api_key:
        return headers
    if anthropic:
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = ANTHROPIC_VERSION
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _extract_reply(anthropic: bool, body: object) -> tuple[str, str]:
    """从响应里取模型那句话。返回 `(文本, 来源)`；取不到就是 `("", "")`。

    ## ★ 为什么必须认 `reasoning_content`（v0.11）

    推理型模型（`deepseek-reasoner`、带 `-reasoning`/`-think` 的网关型号、
    以及本例里的 `deepseek-flash`）会把产出分成两段：
    `reasoning_content` 是它"想"的过程，`content` 是最终给他的回答。
    而我们的探测只给**个位数**的 `max_tokens` ——
    预算在"想"的阶段就用完了，`content` 必然是空串
    （对方同时给 `finish_reason: "length"` 与
    `usage.completion_tokens_details.reasoning_tokens`）。

    原来只看 `content`，于是把一个**完全正常的推理型模型**判成
    "响应结构不对"，还叫用户去查 `baseUrl` —— 一句会把人带偏的假指控。
    `reasoning_content` 同样是模型真实产出的东西，
    它证明凭据有效、接入点兼容、模型在线，这三件事正是探测要问的。

    来源用两个字符串区分（`"content"` / `"reasoning"`），
    而不是一个布尔：调用方需要据此给出不同的文案 ——
    "模型回了 pong" 与 "模型把预算用在推理上了" 是两件事。
    """
    if not isinstance(body, dict):
        return "", ""
    if anthropic:
        blocks = body.get("content")
        if isinstance(blocks, list):
            texts = [
                block.get("text", "")
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            joined = " ".join(t for t in texts if t).strip()
            return (joined, "content") if joined else ("", "")
        return "", ""
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            content = str(message.get("content") or "").strip()
            if content:
                return content, "content"
            # 推理型模型：content 空但它在"想"。取它，并标出来源。
            reasoning = str(message.get("reasoning_content") or "").strip()
            if reasoning:
                return reasoning, "reasoning"
    return "", ""


def _provider_message(body: object) -> str:
    """从错误响应里取一句可读的原因，**脱敏并截断**。

    取它是因为"model 不存在"这类信息只有对方能给，而它对用户非常有价值。
    但它同时是**不受信文本**（对方返回的任意内容），所以：

    * 只取结构化的 `error.message` / `message` 字段，不整段回显 body；
    * 过一遍 `redact()` —— 有些服务会在错误里复述收到的 key；
    * 截断到 200 字符。
    """
    from app.core.crypto import redact

    text = ""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            text = str(error.get("message") or "")
        elif isinstance(error, str):
            text = error
        if not text:
            text = str(body.get("message") or "")
    return redact(text.strip())[:200]


def _describe_http_error(status: int, reason: str) -> str:
    """HTTP 状态码 → 可直接展示的中文一句话。"""
    suffix = f"（对方返回：{reason}）" if reason else ""
    if status in (401, 403):
        return f"凭据被拒绝（HTTP {status}）。请确认 API key 是否正确、是否已过期。{suffix}"
    if status == 404:
        return (
            f"接入点路径不存在（HTTP 404）。请检查 baseUrl 是否写对 —— "
            f"云端服务通常要带上 /v1。{suffix}"
        )
    if status == 429:
        return f"被对方限流（HTTP 429）。凭据本身有效，稍后重试即可。{suffix}"
    if status >= 500:
        return f"对方服务异常（HTTP {status}）。这不是你的配置问题，稍后重试。{suffix}"
    return f"请求被拒绝（HTTP {status}）。{suffix}"


def probe(*, base_url: str, label: str, model: str, api_key: str, kind: str) -> ProbeOutcome:
    """发一次最小化请求。**任何异常都变成 `ProbeOutcome`，不往外抛。**

    不往外抛是有意的：一次探测失败是**正常结果**，不是服务端错误。
    让它变成 500 的话，前端只能显示"服务器内部错误"，
    而用户需要知道的是"连不上"还是"凭据不对"。
    """
    settings = get_settings()
    started = time.perf_counter()

    resolved = resolve_base_url(base_url, label)
    if not resolved:
        return ProbeOutcome(
            ok=False,
            duration_ms=0,
            error=(
                "没有可用的接入点地址：baseUrl 为空，且无法从名称推断官方默认地址。"
                "请填写 baseUrl。"
            ),
        )

    endpoints = resolved if "://" in resolved else f"https://{resolved}"
    parts = urlsplit(endpoints)
    if parts.scheme not in ("http", "https"):
        return ProbeOutcome(
            ok=False,
            duration_ms=0,
            error="baseUrl 的协议只支持 http 或 https。",
        )

    blocked = _local_address_guard(endpoints)
    if blocked:
        return ProbeOutcome(ok=False, duration_ms=0, error=blocked)

    anthropic = is_anthropic(endpoints)
    url = build_endpoint(endpoints, anthropic=anthropic, local=kind == "local")
    body = _payload(
        anthropic=anthropic,
        model=model,
        prompt=settings.llm_probe_prompt,
        max_tokens=settings.llm_probe_max_tokens,
    )

    try:
        # ★ `trust_env=False` —— 出站 SSRF 面。`url` 由管理员填的 `baseUrl`
        #   推出，而 httpx 的 `trust_env` 默认 `True`：它会读环境里的
        #   `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`，把这次请求发给那个
        #   代理（实测默认配置下请求真的路由到了代理，`False` 则不经过）。
        #
        # ⚠️ **一处取舍（有意为之，不是漏了）**：如果用户确实在**公司代理
        #    后面**跑本地 Ollama（`http://127.0.0.1:11434`），
        #    `trust_env=False` 会让那次探测**连不上** —— 因为探测不再走
        #    `HTTP_PROXY`。这与 `allow_private_llm_endpoints` 是同一类取舍：
        #    我们选了"不接受来自环境变量的改道"，代价是这类部署要显式配代理。
        #    要支持那种场景，应在配置里加一个显式的 proxy 项传给 `proxy=`，
        #    而不是把 `trust_env` 打开（那等于把"谁来收这次请求"交给环境）。
        #
        # ★★ `GuardedHTTPTransport` —— **连接层**判据（2026-10-16 补）。
        #    `_local_address_guard` 那两层都只能看字符串，而
        #    `169.254.169.254.nip.io` 这类**域名写法**要解析之后才知道指向哪
        #    —— 那正是上面那段"主机名是否解析到内网要靠 DNS 才知道"所记的
        #    边界。连接层把它堵上，见 `app/core/netguard.py`。
        with httpx.Client(
            timeout=httpx.Timeout(settings.llm_probe_timeout_seconds),
            trust_env=False,
            transport=netguard.GuardedHTTPTransport(),
        ) as client:
            # fail-closed：确认 guard 装在了所有池上（含代理池）。
            netguard.ensure_guarded(client)
            response = client.post(url, json=body, headers=_headers(anthropic=anthropic, api_key=api_key))
            duration_ms = int((time.perf_counter() - started) * 1000)

            try:
                parsed = response.json()
            except ValueError:
                parsed = None

            if response.status_code >= 400:
                return ProbeOutcome(
                    ok=False,
                    duration_ms=duration_ms,
                    error=_describe_http_error(response.status_code, _provider_message(parsed)),
                    endpoint=url,
                    http_status=response.status_code,
                )

            reply, reply_source = _extract_reply(anthropic, parsed)
            if not reply:
                # 2xx 但既没有回答、也没有推理内容。这**算失败** ——
                # 一个返回 200 但结构不对的端点，拿去跑扫描会在半路出错。
                # 报成成功会让用户以为配好了。
                #
                # ⚠️ 文案**不再**叫用户去查 baseUrl：走到这里的原因通常是
                # "探测预算太小，模型还没开口就没了"，
                # 而 baseUrl 错的话前面早就以 404 / 非 JSON 的形式失败了。
                # 把责任推给一个大概率没问题的配置项，会让人白折腾半天。
                return ProbeOutcome(
                    ok=False,
                    duration_ms=duration_ms,
                    error=(
                        "对方接受了请求（HTTP "
                        f"{response.status_code}），但模型没有产出任何内容 —— "
                        "既没有回答，也没有推理过程。"
                        "探测只给个位数的 token，推理型模型可能还没开始回答就用完了；"
                        "可以先调大 SUPEX_LLM_PROBE_MAX_TOKENS 再试。"
                    ),
                    endpoint=url,
                    http_status=response.status_code,
                )

            return ProbeOutcome(
                ok=True,
                duration_ms=duration_ms,
                reply=reply[:200],
                # 来源进日志（不进响应体 —— 那会改契约形状）。
                # 排查"回复看起来像思考过程"时，这一行能省一次翻源码。
                endpoint=url,
                http_status=response.status_code,
                reply_source=reply_source,
            )

    except httpx.TimeoutException:
        return ProbeOutcome(
            ok=False,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=f"请求超时（超过 {settings.llm_probe_timeout_seconds:g} 秒）。"
            "请确认接入点可达，或调大 SUPEX_LLM_PROBE_TIMEOUT_SECONDS。",
            endpoint=url,
        )
    except httpx.ConnectError as exc:
        # 只取异常消息的第一行：完整内容里可能带 DNS 细节与重试链，
        # 而用户需要的只是"连不上"以及连的是哪个地址。
        first_line = (str(exc).splitlines() or [""])[0].strip()
        detail = f"（{first_line}）" if first_line else ""
        return ProbeOutcome(
            ok=False,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=f"无法连接到接入点{detail}。请确认 baseUrl 与网络可达性。",
            endpoint=url,
        )
    except httpx.HTTPError as exc:
        return ProbeOutcome(
            ok=False,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=f"请求失败：{type(exc).__name__}。请检查接入点地址后重试。",
            endpoint=url,
        )
