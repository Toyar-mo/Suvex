"""配置生成：**文档 + 实测 → 配置 → 试跑 → 失败回喂重试**。

这就是"填个表，agent 自动对接"里 agent 干活的那一段。

## 一次生成的全过程

    ① 探测（可选，没给报告就自己跑）
    ② 提示词 = 接口文档 + 实测报告 + 用户填的 base_url
    ③ chat → 模型输出 JSON
    ④ 严格校验（`PlatformSpec.from_dict`）
    ⑤ **试跑**：真的用它去取一次题目
    ⑥ 任何一步失败 → 把错误原样回喂 → 回到 ③

## ★ 第 ⑤ 步是整套设计的安全阀

"配置生成成功"**不是**证据。模型可以产出一份语法完全正确、而端点全是
幻觉的配置 —— 它看起来完美，直到比赛当天提交答案时才发现。

所以这里不信任任何没跑过的配置：生成完必须真的取一次题目，取到了才算
通过。这也是唯一能在比赛**之前**发现问题的地方。

## 为什么 `chat` 是注入的

`LlmGateway` 住在 `runner.py`（一个两千多行的重模块，依赖沙箱、调度、
prompt 加载）。让 `adapters` 反过来依赖它，会把这一层和整个解题器绑死，
而且立刻制造一个循环导入。

注入一个 `chat(messages, system=...) -> str` 之后，这一层既不知道也不
关心用的是哪个 provider —— 测试里塞一个假的就行。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.kernel.adapters.generic import GenericDriver
from app.kernel.adapters.probe import ProbeReport, probe_platform
from app.kernel.adapters.spec import PlatformSpec, SpecError
from app.kernel.platform import RemoteChallenge

logger = logging.getLogger("suvex.ctf2")

#: 提示词。与 `ctf2_system.md` 放在一起 —— 都是"给模型看的东西"。
PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "platform_spec.md"

#: 一次对话调用。签名对齐 `runner.LlmGateway.chat`，但这一层不依赖它。
ChatFn = Callable[..., Awaitable[str]]

_FENCE = re.compile(r"```(?:jsonc?|JSON)?\s*(.*?)```", re.S)

#: 文档超过这个长度就截断。一份 OpenAPI 导出可能有几百 KB，而模型的
#: 上下文是稀缺资源 —— 前 4 万字符足够覆盖"接口总览 + 字段表"，
#: 而那正是生成配置需要的部分。
MAX_DOCUMENT_CHARS = 40_000


@dataclass(frozen=True, slots=True)
class BuildAttempt:
    """一次尝试的账。**留着它**，因为"模型试了几次、每次都错在哪"
    是排查"为什么它总也生成不对"的唯一线索。"""

    index: int
    raw: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class BuildResult:
    spec: PlatformSpec | None
    attempts: list[BuildAttempt] = field(default_factory=list)
    #: 生成**且**试跑都过了。`ok=False` 时 `spec` 可能是 `None`（没生成
    #: 出合法配置），也可能非空（配置合法但取不到题）—— 后者更有价值，
    #: 因为它代表"差一点就成了"，用户可以看到那份配置并手工修。
    ok: bool = False
    challenge_count: int = 0
    sample: RemoteChallenge | None = None
    error: str = ""

    @property
    def last_raw(self) -> str:
        return self.attempts[-1].raw if self.attempts else ""


@dataclass(frozen=True, slots=True)
class VerifyResult:
    ok: bool
    error: str = ""
    challenge_count: int = 0
    sample: RemoteChallenge | None = None


# ══════════════════════════════════════════════════════════
# 输出解析
# ══════════════════════════════════════════════════════════


def extract_json(text: str) -> Any:
    """从模型的输出里抠出 JSON。

    ★ 宽容的是**包装**（Markdown 代码块、前后废话），**不**宽容的是
    内容 —— 缺字段、字段类型不对，交给 `PlatformSpec.from_dict` 严格
    校验，并给出"哪一项、期望什么"的可操作错误。

    这个区分很重要：包一层代码块是模型的排版习惯，不是错误；而少一个
    `list_challenges` 是真的没写出来，必须当场拒绝。
    """
    candidate = (text or "").strip()
    match = _FENCE.search(candidate)
    if match:
        candidate = match.group(1).strip()
    for attempt in (candidate, _between_braces(candidate)):
        if not attempt:
            continue
        try:
            return json.loads(attempt)
        except ValueError:
            continue
    raise SpecError("模型输出里没有找到合法的 JSON 对象")


def _between_braces(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else ""


def _pin_base_url(data: dict[str, Any], base_url: str) -> None:
    """把 `base_url` 钉死在用户填的那个主机上。

    ★ 为什么不能全信模型：它可能"顺手"把地址改成文档里出现的示例域名
    （`https://example.com`）。而那份配置会带着一个**别人的域名**跑起来 ——
    取题取不到、提交提交不了，而错误信息看起来像网络问题。

    允许模型做的是**补前缀**（用户填域名、真实根在 `/api/v1/agent` 下），
    所以这里只核对主机，不要求完全一致。
    """
    given = str(data.get("base_url") or "").strip()
    if not given:
        data["base_url"] = base_url
        return
    try:
        given_host = httpx.URL(given).host
        want_host = httpx.URL(base_url).host
    except Exception as exc:  # noqa: BLE001 - httpx 对畸形 URL 抛的异常类型不固定
        raise SpecError(f"base_url 不是一个合法的地址：{given!r}") from exc
    if given_host != want_host:
        raise SpecError(
            f"配置里的 base_url 指向 {given_host}，而用户填的是 {want_host} —— "
            "不要改成别的站点，请只补路径前缀"
        )
    data["base_url"] = given.rstrip("/")


def parse_spec(raw: str, *, base_url: str) -> PlatformSpec:
    """模型输出 → 校验过的配置。"""
    data = extract_json(raw)
    if not isinstance(data, dict):
        raise SpecError(f"配置的顶层应该是一个对象，收到 {type(data).__name__}")
    _pin_base_url(data, base_url)
    return PlatformSpec.from_dict(data)


# ══════════════════════════════════════════════════════════
# 试跑
# ══════════════════════════════════════════════════════════


async def verify_spec(
    spec: PlatformSpec,
    access_key: str,
    *,
    transport: Any = None,
) -> VerifyResult:
    """用这份配置**真的取一次题目**。

    ## ★ 为什么只试"取题"，不试"提交"

    提交会**消耗一次错误提交次数** —— 那是不可再生的资源，而试跑时
    手上根本没有正确答案。拿一道题去试提交，等于开赛前先烧掉一次机会。

    取题则完全安全：它是只读的，而且它顺便验证了配置里最难对的部分
    （认证、响应包装、列表路径、字段映射）。
    """
    driver = GenericDriver(spec, access_key, transport=transport)
    try:
        challenges = await driver.fetch_challenges(known_ids=set())
    except Exception as exc:  # noqa: BLE001 - 要把原始错误回喂给模型
        return VerifyResult(ok=False, error=f"取题目列表失败：{type(exc).__name__}: {exc}")
    finally:
        await driver.aclose()

    if not challenges:
        return VerifyResult(
            ok=False,
            error=(
                "请求成功了，但题目列表是**空的**。可能：`items_path` 指错了位置、"
                "`children_path` 该填却没填、或者这个端点在当前赛事下确实没有题目。"
                "请对照实测报告里的字段骨架检查路径。"
            ),
        )

    first = challenges[0]
    problems: list[str] = []
    if not first.title or first.title.startswith("#"):
        problems.append("标题读不到（`fields.title` 指错了字段）")
    if not first.remote_id.strip():
        problems.append("id 读不到（`fields.id` 指错了字段）")
    if problems:
        return VerifyResult(
            ok=False,
            error=(
                f"取到了 {len(challenges)} 条，但字段映射有问题："
                + "；".join(problems)
                + f"。第一条的原始读取结果：id={first.remote_id!r} title={first.title!r}"
            ),
            challenge_count=len(challenges),
        )

    if not first.description and not first.attachments:
        # ⚠️ 只是提醒，不算失败：列表端点本来就可能不带题面（要逐题取
        #    详情）。真有问题的话，实测报告里能看出来。
        logger.info(
            "试跑通过，但第一条题没有题面也没有附件 —— 如果列表端点不带详情，"
            "需要在配置里填 challenge_detail"
        )

    return VerifyResult(ok=True, challenge_count=len(challenges), sample=first)


# ══════════════════════════════════════════════════════════
# 提示词
# ══════════════════════════════════════════════════════════


def load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 只会在部署缺文件时发生
        raise SpecError(f"读不到平台接入提示词（{PROMPT_PATH}）：{exc}") from exc


def build_initial_prompt(*, base_url: str, document: str, report: ProbeReport) -> str:
    parts = [f"## 用户填写的 base_url\n\n{base_url}"]
    parts.append(f"## 实测结果（硬证据，优先于文档）\n\n{report.summary_text()}")
    text = (document or "").strip()
    if text:
        if len(text) > MAX_DOCUMENT_CHARS:
            text = text[:MAX_DOCUMENT_CHARS] + "\n\n…（文档过长已截断）"
        parts.append(f"## 接口文档\n\n{text}")
    else:
        parts.append(
            "## 接口文档\n\n（本次没有提供文档，只能依据实测结果推断。"
            "凡是实测没有覆盖的地方，**宁可留空**也不要猜。）"
        )
    parts.append("现在请输出配置 JSON。只输出 JSON，不要有任何解释文字。")
    return "\n\n".join(parts)


def build_retry_prompt(error: str) -> str:
    return (
        "这份配置没有通过验证：\n\n"
        f"{error}\n\n"
        "请修正后**重新输出完整的** JSON 配置（不要只输出改动的那几行）。\n"
        "如果某个能力确认做不到，就把它整段省略，并在 `_notes` 里写明原因 —— "
        "省略一个不支持的端点是可以接受的，填一个不存在的路径不行。"
    )


# ══════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════


async def build_spec(
    *,
    base_url: str,
    access_key: str,
    chat: ChatFn,
    document: str = "",
    report: ProbeReport | None = None,
    max_attempts: int = 3,
    transport: Any = None,
    verify: bool = True,
) -> BuildResult:
    """生成一份能用的平台配置。

    `chat` 的签名是 `chat(messages, system=...) -> str`，与
    `runner.LlmGateway.chat` 一致；测试里传一个假的即可。
    """
    if report is None:
        report = await probe_platform(base_url, access_key, transport=transport)

    system = load_system_prompt()
    messages: list[dict[str, str]] = [
        {"role": "user", "content": build_initial_prompt(
            base_url=base_url, document=document, report=report
        )}
    ]

    attempts: list[BuildAttempt] = []
    last_error = ""
    last_spec: PlatformSpec | None = None

    for index in range(1, max_attempts + 1):
        raw = await chat(messages, system=system)
        try:
            spec = parse_spec(raw, base_url=base_url)
        except SpecError as exc:
            last_error = f"配置校验没通过：{exc}"
            attempts.append(BuildAttempt(index=index, raw=raw, error=last_error))
            logger.info("平台配置第 %d 次尝试失败：%s", index, last_error)
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": build_retry_prompt(last_error)})
            continue

        last_spec = spec
        if not verify:
            attempts.append(BuildAttempt(index=index, raw=raw))
            return BuildResult(spec=spec, attempts=attempts, ok=True)

        result = await verify_spec(spec, access_key, transport=transport)
        if result.ok:
            attempts.append(BuildAttempt(index=index, raw=raw))
            logger.info(
                "平台配置生成成功（第 %d 次尝试，%d 道题）", index, result.challenge_count
            )
            return BuildResult(
                spec=spec,
                attempts=attempts,
                ok=True,
                challenge_count=result.challenge_count,
                sample=result.sample,
            )

        last_error = result.error
        attempts.append(BuildAttempt(index=index, raw=raw, error=last_error))
        logger.info("平台配置第 %d 次试跑没通过：%s", index, last_error)
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": build_retry_prompt(last_error)})

    # ★ 失败时仍然把**最后那份合法配置**带回去：它离成功最近，用户可以
    #   在界面上看到它、手工改一处就成。返回 `None` 等于把模型做出来的
    #   全部工作扔掉。
    return BuildResult(
        spec=last_spec, attempts=attempts, ok=False, error=last_error or "生成失败"
    )


__all__ = [
    "MAX_DOCUMENT_CHARS",
    "PROMPT_PATH",
    "BuildAttempt",
    "BuildResult",
    "ChatFn",
    "VerifyResult",
    "build_initial_prompt",
    "build_retry_prompt",
    "build_spec",
    "extract_json",
    "load_system_prompt",
    "parse_spec",
    "verify_spec",
]
