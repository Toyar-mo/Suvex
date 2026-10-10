"""日志配置与**脱敏**。

## 这个文件存在的直接原因

任务 2 的验收里有一条：

> 密钥全链路不出现明文落盘或明文日志（检查一下日志中间件有没有把请求体
> 整个打出来，那样会把明文 api_key 打进日志，这是常见的安全事故点，务必检查）

**先说审计结论**：本后端**没有请求体日志中间件**。
唯一的请求日志是 uvicorn 的 access log，它只记
`方法 路径 HTTP状态`（见 `uvicorn.config.LOGGING_CONFIG` 的
`"%(client_addr)s - \"%(request_line)s\" %(status_code)s"`），不含 header 与 body。
FastAPI / Starlette 自身也不打印请求体。

**但"现在没有"不等于"以后不会有"**。所以这里做两件事：

1. 把审计结论**写成测试**（`tests/test_secret_hygiene.py`）——
   它捕获整个请求周期内的全部日志，断言明文 key 一次都没出现。
   哪天有人加了一个打印 body 的中间件，那条测试当场变红。
2. 加一层**脱敏兜底**：即使真的漏进去了，落盘的也是掩码。

## 为什么兜底不等于可以放松

正则脱敏只能识别**长得像密钥**的东西。一个刚好不长那样的凭据会漏过去；
一个被拆成两半打印的也会漏过去。所以它挡的是"手滑"，不是"设计错误"。
真正的规矩仍然是：**不打印凭据，不打印请求体**。

## 为什么用 Formatter 而不是 Filter 做主要防线

`logging.Filter` 在 **record 阶段**运行，拿到的是 `record.msg` 与 `record.args`；
而 `Formatter` 在**最终字符串**阶段运行，能看到格式化后的 traceback。

异常是最容易漏掉明文的地方：`raise RuntimeError(f"auth failed: {key}")`
的密钥在 traceback 里，而它在 `record.msg` 里根本不存在。
所以两个都装：Formatter 管格式化输出，Filter 管那些绕过我们 formatter 的
handler（比如 uvicorn 自己的）。
"""

from __future__ import annotations

import logging
import sys

from app.core.crypto import redact

# 我们的日志格式。不带 logger 名会让"这条是谁打的"变成猜谜，
# 而排查凭据泄漏时第一个要问的就是"谁打的"。
LOG_FORMAT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class RedactingFormatter(logging.Formatter):
    """格式化**之后**再脱敏 —— 这样 traceback 也被覆盖。"""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class RedactingFilter(logging.Filter):
    """给那些用自己 formatter 的 handler 兜底（主要是 uvicorn 的两个 logger）。

    ⚠️ 这里改写 `record.msg` 之后把 `args` 清空，是必须的：
    原消息是 `"key=%s"` 加一个 args，只改 msg 不改 args 的话，
    `%` 格式化会拿到一个参数但找不到占位符 ——
    结果是 `TypeError: not all arguments converted`，
    而那个异常会被 logging 模块自己吞掉（打印 "--- Logging error ---"），
    于是这条日志**彻底消失**。改日志的代码把日志弄丢，是最讽刺的一类 bug。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - 格式化失败就让原始 record 过去
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


def configure_logging(level: str) -> None:
    """配置根 logger。启动时调用一次。"""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    # force 语义：拿走已有 handler。
    # 不加的话，重复进入 lifespan（TestClient 多次启动）会累积 handler，
    # 同一条日志被打印 N 次 —— 看起来像"请求被处理了 N 遍"。
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # uvicorn 的两个 logger 有自己的 handler 且 propagate=False，
    # 所以根 handler 上的脱敏对它们无效 —— 显式挂 filter。
    # access log 只记请求行（不含 body），但 URL 里的查询串是会进日志的，
    # 而"把 key 放进查询串"是很多 LLM SDK 的默认行为，这里兜住它。
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactingFilter) for f in logger.filters):
            logger.addFilter(RedactingFilter())

    # httpx 在 INFO 级别记请求行。调用 LLM 时请求行里只有 URL，
    # 但把它的级别压到 WARNING 更稳妥 —— 少一类可能泄漏的输出。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
