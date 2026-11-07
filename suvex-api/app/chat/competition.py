"""对话页对接**比赛平台**的一层薄封装（羊城杯 v1.46）。

## 为什么单独一个模块

对话页原来只有 `list_challenges` / `start_ctf_solve` 两个工具，它们操作
的是**本平台 `KernelChallenge` 题库**（`/ctf2` 看板那条链路）—— 而不是
比赛平台（`ycb-test.dasctf.com`）的题。比赛当天用户在**对话页**里让
Agent 刷题、交 flag，这条链原本是断的。

这里不做「把平台契约重新发明一遍」的事：平台差异早就被关进了
`app/kernel/platform.py` 的 `PlatformDriver` 契约，而比赛平台的实现
`app/kernel/drivers/slabmatch.py` 也早就写好了。本模块只负责：

1. **找到该用哪个驱动**（`resolve_driver`）—— 复用
   `platforms.driver_for`：账号有 `platform_id` 就造配置驱动的
   `GenericDriver`；没有就用内置 `SlabMatchDriver`（按平台地址+accesskey
   直连比赛平台）。
2. **把 flag 外壳剥掉**（`strip_flag_shell`）—— 赛事明文规定「提交只交
   花括号内的内容」，而模型经常原样交 `DASCTF{...}`。交整串会被判错、
   白扣一次不可再生的提交次数。
3. 给对话工具提供两个**异步**入口（`list_tasks` / `submit_flag`），
   直接 `await`。⚠️ 对话工具本身是 `async def`、在正在运行的事件循环里
   被 `await`（`engine._execute_tool`），所以**绝不能**在这里用
   `asyncio.run()` —— 那会抛 "asyncio.run() cannot be called from a
   running event loop"。这与 `platforms._run` 的前提**不同**：那个是给
   同步 FastAPI 端点用的，这里是给协程用的。走错这一步的症状是"工具
   永远失败、还查不出为什么"。

## 为什么内置驱动可以直接用 SlabMatchDriver

`driver_for` 在 `platform_id IS NULL` 时返回 `None`（意思是"走内置
CTF2 老路"）。但对话页的比赛场景恰恰**经常**是"没有配置 platform 行、
只有一个绑定的账号"，这时我们要的是**比赛平台驱动**而不是 CTF2 客户端。
所以本模块在 `driver_for` 返回 `None` 时，退化成直接构造
`SlabMatchDriver`（平台地址取账号里记的、或默认比赛平台地址），并把
accesskey 用 `service.decrypt_account_token` 解出来。

⚠️ 内置 CTF2（`KernelClient`）**不在**本模块的覆盖范围：它没有
`fetch_challenges`/`submit` 这种单题驱动形状（它靠 `service.sync` 全量
对账），对话页比赛工具只服务"比赛平台"这一种形状。真跑内置 CTF2 时，
本模块会明确返回"当前账号没指向比赛平台"，让模型/用户去设置页选一个。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.kernel import platforms as platform_store
from app.kernel import service as kernel_service
from app.kernel.platform import (
    PlatformDriver,
    RemoteChallenge,
    RemoteEnvironment,
    SubmitOutcome,
    render_endpoints_prompt,
)

_log = logging.getLogger(__name__)

#: 默认比赛平台地址（赛事环境规范里给的测试赛地址）。
#:
#: ★ 只是**兜底默认**：账号里若记了 `base_url`（配置驱动平台）就用那个。
#:   写死一个默认值是为了让"只在设置里绑了 accesskey、没建 platform 行"
#:   这种最常见的比赛前配置也能直接跑起来。
DEFAULT_COMPETITION_BASE_URL = "https://ycb-test.dasctf.com"

#: flag 外壳：`DASCTF{...}` / `flag{...}` / 任意 `标识{正文}`。
#:
#: ★ 与 `runner.FLAG_PATTERN` 同一个形状判据，但用途相反 —— 那个是
#:   "像不像 flag"的分类器，这里是**有则剥之**的动作。刻意不 import
#:   runner 的常量：对话侧不该依赖解题引擎的私有正则，何况两者将来
#:   可能各自演进（比赛平台也许再加一种外壳）。
_FLAG_SHELL = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}\{([^{}]{1,400})\}$")


def strip_flag_shell(value: str) -> str:
    """把 `DASCTF{xxx}` / `flag{xxx}` 剥成 `xxx`。

    ## 为什么要剥

    赛事《环境规范说明》原文：flag 格式 `DASCTF{}` 或 `flag{}`，
    **提交时只交花括号内的内容**。模型习惯性地把整串 `DASCTF{...}` 交
    上去，平台会判错 —— 而每题 flag **最多提交 50 次**，一次判错就少一次。

    ## 判据

    * 匹配 `标识{正文}` 且正文里没有嵌套花括号 → 取正文；
    * 否则原样返回（命令行、IP、时间戳、哈希这些**合法答案**不该被误剥）。
      这与 `validate_answer` 的「宁可放过，不可错杀」是同一个方向。
    """
    text = (value or "").strip()
    match = _FLAG_SHELL.match(text)
    if match is None:
        return text
    body = match.group(1).strip()
    return body or text


def friendly_driver_error(exc: Exception, action: str) -> str:
    """把驱动抛上来的异常翻成一句**可行动**的人话。

    比赛当天最常见的失败不是"网络断了"，而是"accesskey 没开通/配错" ——
    平台会用一个业务信封回 `无权操作` 之类的话。把它和真正的链路错误分开，
    用户才知道去改"设置 → 解题 / 比赛平台"，而不是去查网络。
    """
    # `KernelResponseError` 带 .status_code/.code，但更重要的是 message ——
    # 比赛平台把"没有权限"写在 message 里，状态码常常是 200。
    text = str(exc) or type(exc).__name__
    lowered = text.lower()
    if any(k in text for k in ("无权", "没有权限", "未授权", "禁止访问")) or any(
        k in lowered for k in ("forbidden", "unauthorized", "permission", "not allowed")
    ):
        return (
            f"{action}失败：比赛平台拒绝了这次请求（**accesskey 没有交易/agent 接口的权限，"
            "或者绑错了账号**）。请到「设置 → 解题 / 比赛平台」确认 AccessKey 是本队"
            "的比赛账号发的、且已在平台上启用 agent 访问。平台原话："
            f"{text}"
        )
    return f"{action}失败：{text}"


def _run(coro: Any) -> Any:
    """在同步上下文里跑协程（与 `platforms._run` 同一前提）。

    ⚠️ **只在没有正在运行的事件循环时可用**。对话工具在运行中的事件循环
    里被 await，走的是下面的 `async` 入口，**不经过这里**。留着它只为
    给未来可能的同步调用方（比如一个纯同步的 CLI）一个入口，并明确它的
    前提 —— 免得有人以为 async 工具也能用它。
    """
    import asyncio

    return asyncio.run(coro)


def resolve_driver(db: Session) -> PlatformDriver | None:
    """拿到当前账号该用的**比赛平台**驱动。

    返回 `None` 表示"当前账号没有指向一个比赛平台"（没绑账号、或账号
    走的是内置 CTF2）。调用方据此给用户一句可行动的话。

    ★ 不抛 `ApiError`：对话工具的口径是"失败变成一句人话的工具结果"，
      在这里抛会让整轮对话失败，而用户其实只是"还没配平台"。
      但**解密凭据失败**这类真异常要让它上去 —— 那是需要用户处理的。
    """
    account = kernel_service.get_account(db)
    if account is None:
        return None

    # 优先走 `driver_for`：账号指向配置驱动平台时，它造 GenericDriver。
    driver = platform_store.driver_for(db, account)
    if driver is not None:
        return driver

    # `driver_for` 返回 None = 账号没有 platform_id。对话页的比赛场景
    # 要的是**比赛平台驱动**，所以退化成直接造 SlabMatchDriver。
    if not account.token_encrypted:
        return None

    from app.kernel.drivers.slabmatch import SlabMatchDriver

    token = kernel_service.decrypt_account_token(account)
    base_url = _account_base_url(db, account)
    return SlabMatchDriver(base_url, token)


def _account_base_url(db: Session, account: Any) -> str:
    """账号记的平台地址；没记就用默认比赛平台地址。"""
    platform_id = getattr(account, "platform_id", None)
    if platform_id:
        platform = platform_store.get_platform(db, platform_id)
        if platform is not None and platform.base_url:
            return platform.base_url
    return DEFAULT_COMPETITION_BASE_URL


async def list_tasks(db: Session) -> list[RemoteChallenge]:
    """拉当前比赛平台的题库（含未解/已解、是否开放）。

    ⚠️ `known_ids=set()`：对话场景要**完整**列表（用户问"现在有哪些题"），
      不做增量跳过 —— 那是同步/落库场景的优化，这里会把题漏掉。

    抛 `LookupError`（未配置平台）/ 驱动异常（网络错误）由上层工具翻成人话。
    """
    driver = resolve_driver(db)
    if driver is None:
        raise LookupError("NO_COMPETITION_DRIVER")
    return await driver.fetch_challenges(known_ids=set())


def _driver_handle(remote_id: str) -> dict[str, str]:
    """驱动寻址用的 handle。

    ★ 两种驱动认的键不同：内置 `SlabMatchDriver` 读 `exerciseId`，
      配置驱动（GenericDriver）的 spec 模板读 `id`。两个键都带、值相同，
      同一条对话链路才能既支持"只绑 AccessKey"（内置驱动）又支持
      "建了 platform 行"（配置驱动），而不用在调用处判分支。
      `SlabMatchDriver._as_int` 接受数字字符串；`GenericDriver` 原样渲染。
    """
    rid = str(remote_id)
    return {"id": rid, "exerciseId": rid}


async def read_env(db: Session, remote_id: str) -> tuple[RemoteEnvironment, str]:
    """读一道题的靶机环境，并把连接信息渲染成给模型看的一段话。

    返回 `(env, prompt_text)`。没有环境时 `prompt_text` 为空串。
    """
    driver = resolve_driver(db)
    if driver is None:
        raise LookupError("NO_COMPETITION_DRIVER")
    handle = _driver_handle(remote_id)
    env = await driver.read_environment(handle)
    return env, render_endpoints_prompt(env)


async def submit(db: Session, remote_id: str, answer: str) -> SubmitOutcome:
    """提交一个答案到比赛平台（**调用方负责先剥外壳**）。

    `SubmitOutcome.accepted` 与 `.is_solved` 分离：accepted=False 表示平台
    没受理（限流/格式/额度），**不消耗错误次数**；accepted=True 且
    is_solved=False 才是真的判错。
    """
    driver = resolve_driver(db)
    if driver is None:
        raise LookupError("NO_COMPETITION_DRIVER")
    handle = _driver_handle(remote_id)
    return await driver.submit(handle, answer)


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def answer_max_length(db: Session) -> int:
    """平台允许的答案长度上限（驱动自报；拿不到就用 256）。"""
    driver = resolve_driver(db)
    if driver is None:
        return 256
    return int(getattr(driver, "answer_max_length", 256))


__all__ = [
    "DEFAULT_COMPETITION_BASE_URL",
    "answer_max_length",
    "friendly_driver_error",
    "list_tasks",
    "read_env",
    "resolve_driver",
    "strip_flag_shell",
    "submit",
]
