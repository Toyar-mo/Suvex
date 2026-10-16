"""平台探测：**在生成配置之前，先拿到硬证据**。

## 为什么不能让大模型只读文档

文档会过期、可能是写给网页端而不是 API 的、可能有个别错字。而"某个
端点到底存不存在"这件事有唯一权威的答案：**打一次**。

所以喂给模型的是**文档 + 实测**两份材料，冲突时以实测为准。这个模块
产出实测的那一半。

## ★ 什么算"端点活着"：200 **且是 JSON**

这条判据是拿真实平台撞出来的。第一版只看状态码，结果在一个已知平台上
报了 **20 个"活着的端点"**，其中 15 个根本不存在 —— 根因是 **SPA 的
catch-all 路由**：前端单页应用对任意未知路径都返回 `200 + text/html`。

后果很严重：照着那份报告生成的配置会全盘指向不存在的端点，而失败要到
"提交答案"那一步才暴露 —— 中间所有步骤看起来都正常。

## 与 `scripts/probe_platform.py` 的关系（如实说明）

那个脚本是**给人看的**：三十秒摸清一个陌生平台，带叙述性输出。
这个模块是**给程序用的**：结构化结果，喂给 `builder`。

两者目前是**两份实现**，判据相同（同一套常量与"200+JSON"的定义）。
没有合并是因为输出形态差得远，而搬迁的风险（一个已经开始工作的脚本）
大于收益。**如果将来只改一处，请改这里**，并把脚本改成调用它。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core import netguard
from app.ctf2.adapters.redirect import request_same_host
from app.ctf2.client import Ctf2Error

logger = logging.getLogger("suvex.ctf2")

# ── 认证方式的候选 ─────────────────────────────────────────
#
# ★ 认证是接入失败最常见的第一个原因，而症状极具迷惑性（401 看起来像
#   "key 不对"，实际是"key 没带对地方"）。一次把常见几种都试一遍，
#   能把这一整类问题在几十秒内排掉。
AUTH_STYLES: tuple[tuple[str, str], ...] = (
    ("x-agent-accesskey", "X-Agent-AccessKey"),
    ("x-api-key", "X-API-Key"),
    ("api-key", "api-key"),
    ("bearer", "Authorization"),
    ("token", "Authorization"),
    ("plain-auth", "Authorization"),
)


def auth_header(style: str, name: str, key: str) -> dict[str, str]:
    if style == "bearer":
        return {"Authorization": f"Bearer {key}"}
    if style == "token":
        return {"Authorization": f"Token {key}"}
    if style == "plain-auth":
        return {"Authorization": key}
    return {name: key}


# ── 候选端点 ───────────────────────────────────────────────
#
# ★ 短路径与长路径混着放是刻意的：不同平台给的 base_url 层级不同
#   （CTF2 给的是 `https://host/api/open/v1/user`，别的可能只给域名）。
#   每个候选会按 `base + path` 与 `origin + path` 各试一次。
CANDIDATE_ENDPOINTS: tuple[str, ...] = (
    "/me",
    "/profile/",
    "/whoami",
    "/status/",
    "/challenges/",
    "/challenges",
    "/tasks/",
    "/problems/",
    "/exercise-list",
    "/ctf/exercise-list",
    "/practice/",
    "/writeups/",
    "/daily/",
    "/competitions/",
    "/events/",
    "/stages/",
    "/scoreboard/",
    "/teams/",
    "/users/me",
    "/user/",
    "/answer-panel/overview",
    "/match/notice/match-info",
    "/api/",
    "/api/v1/",
    "/api/v1/challenges/",
    "/api/v1/competitions/",
    "/api/v1/scoreboard/",
    "/api/v1/users/me",
    "/api/v1/status/",
    "/slab-match/api/v1/agent/ctf/exercise-list",
)

# 认证自检用的候选（**匿名会被拒**的那些最有用）。
AUTH_PROBE_PATHS: tuple[str, ...] = (
    "/practice/",
    "/competitions/",
    "/challenges/",
    "/ctf/exercise-list",
    "/writeups/",
    "/me",
    "/api/v1/users/me",
    "/api/v1/challenges/",
    "/user/",
    "/profile/",
)

ID_KEYS = ("id", "challenge_id", "challengeId", "exerciseId", "uuid", "slug", "code")
TITLE_KEYS = ("title", "name", "challenge_name")

FINGERPRINTS: tuple[tuple[str, str], ...] = (
    ("ctf_version", "CTFd"),
    ("ctfd", "CTFd"),
    ("dasctf", "DASCTF/CTF2"),
    ("slab-match", "slab-match（比赛平台）"),
    ("00000", "统一 {code,message,data} 响应包装"),
)


@dataclass(frozen=True, slots=True)
class Finding:
    """一个探测到的端点。"""

    path: str
    url: str
    status: int
    content_type: str = ""
    #: 响应的**字段骨架**（不是原始数据）—— 模型要的是"字段叫什么"。
    shape: Any = None
    hints: dict[str, str] = field(default_factory=dict)

    @property
    def is_json(self) -> bool:
        return "json" in self.content_type.lower()

    @property
    def alive(self) -> bool:
        return self.status == 200 and self.is_json


@dataclass(frozen=True, slots=True)
class ProbeReport:
    """一次探测的全部结论。**这就是喂给大模型的"实测"那一半。**"""

    base_url: str
    origin: str
    auth_style: str = ""
    auth_name: str = ""
    fingerprint: str = ""
    endpoints: list[Finding] = field(default_factory=list)
    challenge_source: str = ""
    challenge_sample: Any = None
    challenge_count: int = 0
    notes: list[str] = field(default_factory=list)
    #: 各认证方式在自检端点上的状态码（401 → 200 的那个就是答案）。
    auth_attempts: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """认证与题目来源都摸到了 —— 这才算"可以开始写配置"。"""
        return bool(self.auth_style) and bool(self.challenge_source)

    def summary_text(self, *, max_endpoints: int = 20) -> str:
        """压成给大模型看的紧凑文本。

        ★ 为什么不是直接把 JSON 塞进去：端点的 `shape` 可能很大，而
        模型的注意力是稀缺的。这里只保留**决策需要的那部分**：哪些路径
        活着、题目样例的字段名、认证方式、以及不确定性（notes）。
        """
        lines = [f"实测平台：{self.base_url}"]
        if self.fingerprint:
            lines.append(f"平台指纹：{self.fingerprint}")
        if self.auth_style:
            lines.append(
                f"认证方式：{self.auth_style}（header 名 {self.auth_name}）；"
                f"各方式在自检端点上的状态码 {self.auth_attempts}"
            )
        else:
            lines.append("认证方式：**没有试出来** —— 见下面的注意事项")
        alive = [f for f in self.endpoints if f.alive][:max_endpoints]
        lines.append(f"活着的端点（{len(alive)} 个，路径 → 字段骨架）：")
        for finding in alive:
            shape = json.dumps(finding.shape, ensure_ascii=False) if finding.shape else "?"
            if len(shape) > 300:
                shape = shape[:300] + "…"
            lines.append(f"  {finding.path} → {shape}")
        if self.challenge_source:
            lines.append(f"题目列表端点：{self.challenge_source}（{self.challenge_count} 条）")
            sample = json.dumps(self.challenge_sample, ensure_ascii=False) if self.challenge_sample else "?"
            lines.append(f"题目样例字段：{sample[:400]}")
        else:
            lines.append("题目列表：**没有找到** —— 它可能在别的路径下，或需要先选赛事/阶段")
        for note in self.notes:
            lines.append(f"⚠️ {note}")
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# 解析工具（与 scripts/probe_platform.py 里的同源）
# ══════════════════════════════════════════════════════════


def shape_of(value: Any, depth: int = 0) -> Any:
    """把 JSON 压成"字段名骨架" —— 模型要的是字段名，不是数据。"""
    if depth > 3:
        return "…"
    if isinstance(value, dict):
        return {k: shape_of(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return [shape_of(value[0], depth + 1)] if value else []
    return type(value).__name__


def items_of(payload: Any) -> list[dict[str, Any]]:
    """从各种"包了一层"的响应里取出条目数组。"""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("items", "data", "results", "list", "challenges", "records", "corpus"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
        for value in payload.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return [x for x in value if isinstance(x, dict)]
    return []


def flatten_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把"分类 → corpus"的两级列表展平。

    ★ 比赛平台的题目列表就是两级的。不展平的话，探测报告会把**分类名**
    当成题目标题 —— 而那个结论是错的，还会让人以为题面就在列表里
    （其实题面、分值、附件都在详情端点）。
    """
    flat: list[dict[str, Any]] = []
    for item in items:
        corpus = item.get("corpus")
        if isinstance(corpus, list) and corpus:
            group = str(item.get("name") or "")
            for sub in corpus:
                if isinstance(sub, dict):
                    merged = dict(sub)
                    merged.setdefault("__group", group)
                    flat.append(merged)
            continue
        flat.append(item)
    return flat


def looks_like_challenges(items: list[dict[str, Any]]) -> bool:
    if not items:
        return False
    return any(any(k in item for k in TITLE_KEYS) for item in items[:5])


def pick_id(item: dict[str, Any]) -> str | None:
    for key in ID_KEYS:
        value = item.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value)
    return None


# ══════════════════════════════════════════════════════════
# 探测
# ══════════════════════════════════════════════════════════


class Prober:
    """对某个 base_url 做一次结构化探测。"""

    def __init__(
        self,
        base_url: str,
        key: str,
        *,
        timeout: float = 12.0,
        min_interval: float = 0.25,
        transport: httpx.AsyncBaseTransport | None = None,
        max_endpoints: int = 60,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        parsed = httpx.URL(self.base_url)
        self.origin = f"{parsed.scheme}://{parsed.host}" + (
            f":{parsed.port}" if parsed.port else ""
        )
        self.key = key
        self.max_endpoints = max_endpoints
        # ★ 限速不是可选项：一次探测要打几十个候选端点，而真实平台几乎
        #   都有速率限制。不限速的后果不是"探测失败"，而是把用户这个
        #   账号短暂打进 429 —— 而比赛期间那是要用来提交答案的额度。
        self._min_interval = min_interval
        self._last = 0.0
        # ★ `follow_redirects=False` —— 理由同 `transport.py`：跳转必须
        #   逐跳过白名单，而 httpx 自己跟的话只在入口校验一次。
        #   探测阶段打的端点最多，这条尤其要紧。
        # ★ `trust_env=False` —— 理由同 `transport.py`：目标是管理员填的
        #   平台地址，而 `trust_env` 默认为 `True` 会让请求走环境变量里的
        #   代理。探测打的端点最多，这条更要紧。
        #
        # ★★ `GuardedAsyncTransport` —— 连接层判据，理由同 `transport.py`：
        #   判据层只能看字符串，`169.254.169.254.nip.io` 这类域名写法要
        #   在解析之后才拦得住（见 `app/core/netguard.py`）。
        #   ⚠️ 只在调用方没传 transport 时用默认值（测试会传 MockTransport）。
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            transport=transport if transport is not None else netguard.GuardedAsyncTransport(),
            trust_env=False,
        )
        # fail-closed：确认 guard 装在了所有池上（含代理池）。
        netguard.ensure_guarded(self._client)
        #: 用户填的那个主机（小写、已剥端口）—— 跳转的唯一合法目标。
        self._base_host = (parsed.host or "").lower()
        self._findings: dict[str, Finding] = {}
        self._last_url = self.base_url
        self._requests = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last = time.monotonic()

    def _urls_for(self, path: str) -> list[str]:
        primary = f"{self.base_url}{path}"
        secondary = f"{self.origin}{path}"
        return [primary] if secondary == primary else [primary, secondary]

    def _rank(self, response: httpx.Response) -> int:
        """200+JSON（2） > 401/403（1） > 其他（0）。

        ⚠️ 200 只在**带 JSON** 时才算命中 —— 见模块 docstring 里那段
        SPA catch-all 的说明。
        """
        if response.status_code == 200 and "json" in response.headers.get(
            "content-type", ""
        ).lower():
            return 2
        if response.status_code in (401, 403):
            return 1
        return 0

    async def _get(
        self, path: str, *, headers: dict[str, str] | None = None
    ) -> httpx.Response | None:
        """两种拼法都试，取最好的那个。"""
        if self._requests >= self.max_endpoints:
            return None
        best: httpx.Response | None = None
        best_url = ""
        best_rank = -1
        for url in self._urls_for(path):
            if self._requests >= self.max_endpoints:
                break
            await self._throttle()
            self._requests += 1
            try:
                response = await request_same_host(
                    self._client,
                    "GET",
                    url,
                    base_host=self._base_host,
                    headers=headers if headers is not None else {},
                )
            except httpx.HTTPError:
                continue
            except Ctf2Error:
                # 跨 host 跳转 / 跳进被拒地址：这个端点不可用。
                # ⚠️ 探测是**尽力而为**的（几十个候选端点里挑活的），
                #    所以这里不往上抛 —— 抛出去会让一次探测整体失败，
                #    而正确的语义是"这个候选端点不算数，换下一个"。
                continue
            rank = self._rank(response)
            if rank > best_rank:
                best, best_url, best_rank = response, url, rank
            if rank == 2:
                break
        if best is not None:
            self._last_url = best_url
        return best

    def _record(self, path: str, response: httpx.Response) -> Finding:
        content_type = response.headers.get("content-type", "").split(";")[0]
        shape: Any = None
        hints: dict[str, str] = {}
        if "json" in content_type.lower():
            try:
                payload = response.json()
                shape = shape_of(payload)
                if isinstance(payload, dict):
                    for key in ("detail", "error", "message", "msg", "code", "params"):
                        if key in payload:
                            hints[key] = str(payload[key])[:120]
            except ValueError:
                pass
        finding = Finding(
            path=path,
            url=self._last_url,
            status=response.status_code,
            content_type=content_type,
            shape=shape,
            hints=hints,
        )
        self._findings[path] = finding
        return finding

    async def _discover_auth(self) -> tuple[str, str, dict[str, int]]:
        """找出平台认哪种认证方式。

        判据不能是"某个端点返回 200"：公开端点匿名也 200，SPA 更是对
        任意路径返回 200。**只能**先用一个"匿名被拒（401/403）"的端点
        确认"这个端点确实要认证"，再逐个方式试它。
        """
        attempts: dict[str, int] = {}
        baseline: str | None = None
        fallback: str | None = None

        for path in AUTH_PROBE_PATHS:
            response = await self._get(path, headers={})
            if response is None:
                continue
            finding = self._record(path, response)
            if response.status_code in (401, 403) and baseline is None:
                baseline = path
                break
            if fallback is None and finding.alive:
                fallback = path

        if baseline is None:
            if fallback is None:
                return "", "", attempts
            baseline = fallback

        for style, name in AUTH_STYLES:
            response = await self._get(baseline, headers=auth_header(style, name, self.key))
            if response is None:
                continue
            attempts[style] = response.status_code
            if response.status_code == 200 and "json" in response.headers.get(
                "content-type", ""
            ).lower():
                return style, name, attempts
        return "", "", attempts

    async def _scan(self) -> list[Finding]:
        # 认证阶段探过的端点记的是**匿名**结果；这里用认证头重探一次 ——
        # 否则 `/practice/` 这种"匿名 401、带 key 200"的核心端点会被漏掉，
        # 而报告里只剩几个无关的公开端点。
        if self._findings:
            for path, finding in list(self._findings.items()):
                if finding.status in (401, 403):
                    response = await self._get(path)
                    if response is not None:
                        self._record(path, response)

        for path in CANDIDATE_ENDPOINTS:
            finding = self._findings.get(path)
            if finding is not None:
                continue
            response = await self._get(path)
            if response is None:
                continue
            if response.status_code >= 400 and response.status_code not in (401, 403):
                continue
            self._record(path, response)
        return list(self._findings.values())

    async def _discover_challenges(self) -> tuple[str, Any, int]:
        """找出"题目列表"在哪。

        判据是**形状**而不是路径名：条目里有 `title`/`name` 且有 `id`
        就当它是题目列表。这比维护一张"哪些路径是题目列表"的表可靠 ——
        平台可以把它叫 challenges / tasks / problems / questions。
        """
        best: tuple[str, list[dict[str, Any]]] | None = None
        for path, finding in list(self._findings.items()):
            if not finding.alive:
                continue
            response = await self._get(path)
            if response is None:
                continue
            try:
                payload = response.json()
            except ValueError:
                continue
            items = flatten_groups(items_of(payload))
            if not looks_like_challenges(items):
                continue
            if best is None or len(items) > len(best[1]):
                best = (path, items)
        if best is None:
            return "", None, 0
        path, items = best
        return path, shape_of(items[0]) if items else None, len(items)

    def _fingerprint(self) -> str:
        blob = json.dumps(
            [{"path": f.path, "shape": f.shape, "hints": f.hints} for f in self._findings.values()],
            ensure_ascii=False,
        ).lower()
        for needle, name in FINGERPRINTS:
            if needle.lower() in blob:
                return name
        return ""

    async def run(self) -> ProbeReport:
        notes: list[str] = []
        auth_style, auth_name, attempts = await self._discover_auth()
        if not auth_style:
            notes.append(
                "没有试出可用的认证方式。可能：key 无效、平台用签名认证（HMAC），"
                "或者 base_url 不对。**不要凭文档猜一个认证方式填进配置** —— "
                "那会让每一次请求都 401，而症状看起来像 key 错。"
            )
        await self._scan()
        source, sample, count = await self._discover_challenges()
        if not source:
            writeups_alive = any("writeup" in p for p in self._findings)
            hint = (
                "。但 `/writeups/` 是活的 —— 试试「解法侧门」：公开解法每条"
                "带着 challenge_id，拿 id 逐个容器试详情端点（CTF2 就是这么取的，"
                "98 个 id 全中）"
                if writeups_alive
                else ""
            )
            notes.append(
                "没有找到「看起来像题目列表」的端点，它可能要求先选赛事/阶段，"
                "或者题目藏在别的路径下" + hint
            )
        return ProbeReport(
            base_url=self.base_url,
            origin=self.origin,
            auth_style=auth_style,
            auth_name=auth_name,
            fingerprint=self._fingerprint(),
            endpoints=list(self._findings.values()),
            challenge_source=source,
            challenge_sample=sample,
            challenge_count=count,
            notes=notes,
            auth_attempts=attempts,
        )


async def probe_platform(
    base_url: str,
    key: str,
    *,
    timeout: float = 12.0,
    min_interval: float = 0.25,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ProbeReport:
    """探测一个平台的形状。**只读**，不发任何写请求。"""
    prober = Prober(
        base_url, key, timeout=timeout, min_interval=min_interval, transport=transport
    )
    try:
        return await prober.run()
    finally:
        await prober.aclose()


__all__ = [
    "AUTH_PROBE_PATHS",
    "AUTH_STYLES",
    "CANDIDATE_ENDPOINTS",
    "Finding",
    "ProbeReport",
    "Prober",
    "flatten_groups",
    "items_of",
    "looks_like_challenges",
    "pick_id",
    "probe_platform",
    "shape_of",
]
