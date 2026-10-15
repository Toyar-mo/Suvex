"""通用 HTTP 传输：认证 + 统一响应包装 + 限速 + 错误映射。

## 为什么单独一层

`SlabMatchDriver` 和 `GenericDriver` 要做的事**完全一样**：带认证头发
请求、判断这次算不算成功、拆掉响应包装、把 429/401 映射成领域错误。

复制两份的后果不是"多六十行"，而是**将来只会修其中一份** —— 而那种
漂移的表现是"两个平台里有一个会正确重试限流、另一个直接失败"，
看起来像平台的问题。

## ★ 成功判据由配置决定，这不是过度设计

两个真实平台的差别就落在这里：

* CTF2 —— 裸 JSON，**看 HTTP 状态码**（`success_path` 留空）
* 比赛平台 —— **失败也返回 HTTP 200**，只看 `code == "00000"`

后者不处理的话，"答案错误"会被读成成功、`data` 是空的，最终表现成
一道题反复提交、永远不结束 —— 而那看起来像调度器的 bug。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.ctf2.adapters.redirect import request_same_host
from app.ctf2.adapters.spec import AuthSpec, EnvelopeSpec, dig, dig_str
from app.ctf2.client import (
    Ctf2AuthExpired,
    Ctf2RateLimited,
    Ctf2ResponseError,
    Ctf2Unavailable,
)


def parse_retry_after(raw: str | None) -> float | None:
    """`Retry-After` 只认秒数形式。

    HTTP-date 形式（`Wed, 21 Oct 2026 07:28:00 GMT`）在这里不解析 ——
    CTF2 那边的客户端已经有一份完整的实现（含日期解析），而这一层是
    给"简单平台"用的。解析不出来就返回 `None`，上层退回常规退避，
    那是安全的默认。
    """
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


class JsonPlatformTransport:
    """按 `AuthSpec` + `EnvelopeSpec` 发请求。"""

    def __init__(
        self,
        base_url: str,
        access_key: str,
        *,
        auth: AuthSpec | None = None,
        envelope: EnvelopeSpec | None = None,
        timeout: float = 15.0,
        min_interval: float = 0.2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = (base_url or "").rstrip("/")
        self._key = access_key
        self._auth = auth or AuthSpec()
        self._envelope = envelope or EnvelopeSpec()
        # ★ 限速不是可选项：一次全量同步要**逐题取详情**，题量可能是
        #   几十上百道。平台的额度与"提交答案"共享，把它打到限流就等于
        #   把提交也拖下水。
        self._min_interval = min_interval
        self._last_request = 0.0
        # ★ `follow_redirects=False` —— 跳转由 `redirect.request_same_host`
        #   自己跟，因为**白名单必须逐跳生效**。开 `True` 的话 httpx 会自己
        #   跟到任意主机上，而我们的校验只发生在用户填 URL 的那一刻：
        #   实测能把内网服务的响应体读出来（见 `redirect.py` 的说明）。
        self._client = httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, transport=transport
        )
        #: 用户填的那个主机（小写、已剥端口）—— 跳转的唯一合法目标。
        self._base_host = (httpx.URL(self._base).host or "").lower()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_request = time.monotonic()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """发一次请求，返回**拆包之后**的数据。

        抛出的异常全部是领域异常（`Ctf2RateLimited` / `Ctf2AuthExpired` /
        `Ctf2Unavailable` / `Ctf2ResponseError`），上层不需要认识 httpx。
        """
        await self._throttle()
        query = {str(k): str(v) for k, v in (params or {}).items()}
        query.update(self._auth.query(self._key))
        headers = {"Accept": "application/json", **self._auth.headers(self._key)}

        try:
            response = await request_same_host(
                self._client,
                method,
                f"{self._base}{path}",
                base_host=self._base_host,
                params=query,
                json=body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise Ctf2Unavailable(f"平台请求失败：{exc}") from exc

        if response.status_code == 429:
            raise Ctf2RateLimited(
                "平台限流，稍后重试。",
                retry_after=parse_retry_after(response.headers.get("retry-after")),
            )
        if response.status_code in (401, 403):
            raise Ctf2AuthExpired("凭据无效或已过期，请在设置里重新绑定。")
        if response.status_code >= 500:
            raise Ctf2Unavailable(f"平台返回 {response.status_code}，稍后重试。")
        if response.status_code >= 400:
            raise Ctf2ResponseError(
                f"平台返回 {response.status_code}。", status_code=response.status_code
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise Ctf2ResponseError(
                "平台响应不是 JSON（接口形状可能已经变了）。",
                status_code=response.status_code,
            ) from exc

        # 没配包装 = 裸 JSON，成功与否由状态码决定（已经判过了）。
        if not self._envelope.success_path:
            return payload

        if not isinstance(payload, dict):
            raise Ctf2ResponseError(
                f"配了响应包装，但响应不是一个对象（是 {type(payload).__name__}）—— "
                "配置里的 envelope 可能与真实响应不符。",
                status_code=response.status_code,
            )

        code = dig_str(payload, self._envelope.success_path)
        if code != self._envelope.success_value:
            message = dig_str(payload, self._envelope.message_path) or f"平台返回 code={code}"
            raise Ctf2ResponseError(message, status_code=response.status_code, code=code)
        return dig(payload, self._envelope.data_path)


__all__ = ["JsonPlatformTransport", "parse_retry_after"]
