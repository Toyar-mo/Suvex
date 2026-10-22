"""配置解释器：把一份 `PlatformSpec` 变成一个能用的平台驱动。

## 它在整条链路里的位置

    用户填表 → probe 实测 → **builder 生成配置** → **这里解释执行** → 试跑

`GenericDriver` 是"agent 自动对接"能成立的关键：大模型产出的是**配置**，
而这一段代码负责让配置真的跑起来。如果这一段的能力不足以表达一个正常
形状的平台，那条链路就退化成"还是要人手写驱动"。

## 它**不**打算覆盖的东西

CTF2 那种畸形平台（没有列表端点、要靠公开解法侧门拿 id、题目寻址是
三元组）**永远**走手写驱动。这里不追求覆盖所有平台，只覆盖"形状正常"
的那些 —— 而正常形状恰恰是绝大多数，包括比赛平台。

一个判据：如果为了接某个平台，需要在这里加 `if slug == ...`，
那就说明它该写成一个手写驱动，而不是把特例塞进解释器。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.kernel.adapters.spec import (
    NOT_ACCEPTED_CODES,
    EnvironmentSpec,
    PlatformSpec,
    RequestSpec,
    dig,
    dig_int,
    dig_list,
    dig_str,
)
from app.kernel.adapters.transport import JsonPlatformTransport
from app.kernel.client import KernelResponseError
from app.kernel.platform import (
    RemoteAttachment,
    RemoteChallenge,
    RemoteEndpoint,
    RemoteEnvironment,
    SubmitOutcome,
    parse_millis,
    render_endpoints_prompt,
)

logger = logging.getLogger("suvex.ctf2")


class GenericDriver:
    """用一份声明式配置驱动的平台驱动。

    实现 `platform.PlatformDriver` 协议。
    """

    def __init__(
        self,
        spec: PlatformSpec,
        access_key: str,
        *,
        timeout: float = 15.0,
        min_interval: float = 0.2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.spec = spec
        self.slug = spec.slug_hint()
        self.display_name = spec.display_name
        self._transport = JsonPlatformTransport(
            spec.base_url,
            access_key,
            auth=spec.auth,
            envelope=spec.envelope,
            timeout=timeout,
            min_interval=min_interval,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def __aenter__(self) -> GenericDriver:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ══════════════════════════════════════════════════════
    # 请求
    # ══════════════════════════════════════════════════════

    async def _call(self, request: RequestSpec, *, item_id: str, answer: str = "") -> Any:
        body = request.render_body(item_id=item_id, answer=answer)
        return await self._transport.request(
            request.method,
            request.path,
            params=request.render_params(item_id=item_id),
            # ⚠️ 空 body 要传 None 而不是 `{}`：GET 带一个 `{}` 的 body
            #    会让一部分网关直接拒绝（400），而那个 400 看起来像
            #    "路径不对"，把排查方向带偏。
            body=body or None,
        )

    @staticmethod
    def _item_id(handle: dict[str, Any]) -> str | None:
        """从 handle 里取出题目 id。

        ★ handle 的键**固定是 `id`** —— 这是解释器与配置之间的约定
        （见 `RequestSpec` 的 `{id}` 占位符）。把平台自己的字段名
        （`exerciseId` / `challenge_id`）关在 handle 里面没有意义：
        handle 是**我们**造的，用哪个键由我们决定。
        """
        value = handle.get("id")
        if value is None:
            return None
        return str(value)

    # ══════════════════════════════════════════════════════
    # 题目
    # ══════════════════════════════════════════════════════

    async def fetch_challenges(self, *, known_ids: set[str]) -> list[RemoteChallenge]:
        """列出全部题目。

        `known_ids` 用来做增量：配置里如果给了 `challenge_detail`，
        调用方（`service.sync`）可以据此决定要不要逐题取详情。

        ⚠️ 这里**仍然返回**已知的题，因为列表里带着 `hasSolved` /
        `isOpen` 这些会变的字段 —— 它们是"这道题该不该做"的判据。
        """
        spec = self.spec.list_challenges
        data = await self._call(spec.request, item_id="")
        rows = dig_list(data, spec.items_path)

        out: list[RemoteChallenge] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if spec.children_path:
                # 两级列表（外层是分类，题目在 corpus 里）。
                for child in dig_list(row, spec.children_path):
                    if not isinstance(child, dict):
                        continue
                    challenge = self._to_challenge(child, group=row)
                    if challenge is not None:
                        out.append(challenge)
                continue
            challenge = self._to_challenge(row, group=None)
            if challenge is not None:
                out.append(challenge)

        logger.info(
            "配置驱动取题：%s → %d 道（本地已知 %d 道）",
            spec.request.path,
            len(out),
            len({c.remote_id for c in out} & known_ids),
        )
        return out

    def _to_challenge(
        self, row: dict[str, Any], *, group: dict[str, Any] | None
    ) -> RemoteChallenge | None:
        fields = self.spec.fields
        raw_id = dig(row, fields.id)
        if raw_id is None:
            # ⚠️ 没有 id 的条目**跳过**而不是让整次同步失败：平台偶尔会
            #    返回一条残缺记录（正在编辑中的题），而那不该让其余
            #    几十道题全部消失。
            return None
        remote_id = str(raw_id)

        # 分类来源有两个：题目自己的字段，或者展开出来的外层组名。
        # ★ 分类决定选哪个沙箱镜像（逆向题要用 re 镜像），丢掉它会让
        #   逆向题落到基础镜像上 —— 而症状是"逆向题里跑不动 gdb"，
        #   极难归因到一个"没读到的字段"。
        category = dig_str(row, fields.category) if fields.category else ""
        if not category and group is not None and self.spec.list_challenges.category_from_group:
            category = dig_str(group, self.spec.fields.title) or dig_str(group, "name")

        return RemoteChallenge(
            remote_id=remote_id,
            title=dig_str(row, fields.title) or f"#{remote_id}",
            description=dig_str(row, fields.description) if fields.description else "",
            category=category,
            group_name=category,
            score=dig_int(row, fields.score) if fields.score else None,
            attachments=self._attachments(row),
            solved=bool(dig(row, fields.solved)) if fields.solved else False,
            # ⚠️ 没配 `is_open` 时默认 **True**（开放）。默认 False 会让
            #    所有题都做不了 —— 而"配置漏了一项"的代价不该是"系统瘫痪"。
            is_open=bool(dig(row, fields.is_open)) if fields.is_open else True,
            difficulty=dig_str(row, fields.difficulty) if fields.difficulty else "",
            handle={"id": remote_id},
        )

    def _attachments(self, row: dict[str, Any]) -> list[RemoteAttachment]:
        fields = self.spec.fields
        if not fields.attachments:
            return []
        out: list[RemoteAttachment] = []
        for item in dig_list(row, fields.attachments):
            if not isinstance(item, dict):
                continue
            url = dig_str(item, fields.attachment_url)
            if not url:
                continue
            out.append(
                RemoteAttachment(
                    name=dig_str(item, fields.attachment_name),
                    url=url,
                    ext=dig_str(item, fields.attachment_ext),
                )
            )
        return out

    async def fetch_challenge(self, handle: dict[str, Any]) -> RemoteChallenge | None:
        """取单题详情。没配 `challenge_detail` 就返回 `None`。"""
        request = self.spec.challenge_detail
        item_id = self._item_id(handle)
        if request is None or item_id is None:
            return None
        data = await self._call(request, item_id=item_id)
        if not isinstance(data, dict):
            return None
        return self._to_challenge(data, group=None)

    # ══════════════════════════════════════════════════════
    # 提交
    # ══════════════════════════════════════════════════════

    async def submit(self, handle: dict[str, Any], answer: str) -> SubmitOutcome:
        """提交答案。

        `accepted` / `is_solved` 的区分见 `platform.SubmitOutcome` ——
        把"平台没受理"记成"答案错了"会烧掉不可再生的错误提交次数。
        """
        item_id = self._item_id(handle)
        if item_id is None:
            raise KernelResponseError("提交时 handle 里没有 id —— 寻址信息丢了。")
        try:
            data = await self._call(self.spec.submit, item_id=item_id, answer=answer)
        except KernelResponseError as exc:
            if exc.code and exc.code in NOT_ACCEPTED_CODES:
                return SubmitOutcome(accepted=False, is_solved=False, message=str(exc))
            logger.info(
                "平台判错（path=%s code=%s）：%s", self.spec.submit.path, exc.code, exc.message
            )
            return SubmitOutcome(accepted=True, is_solved=False, message=str(exc))

        solved = bool(dig(data, self.spec.submit_solved_path))
        already = bool(
            self.spec.submit_already_solved_path
            and dig(data, self.spec.submit_already_solved_path)
        )
        return SubmitOutcome(
            accepted=True,
            is_solved=solved or already,
            already_solved=already and not solved,
            message="答案正确。" if solved else "平台判定为错误答案。",
        )

    # ══════════════════════════════════════════════════════
    # 环境
    # ══════════════════════════════════════════════════════

    async def read_environment(self, handle: dict[str, Any]) -> RemoteEnvironment:
        """读环境状态。

        ★ 配置说 `environment.from_detail` 时，环境就在**题目详情**里
        （比赛平台就是这样：`isNeedCheck` / `endpoints` 都在 §5 的响应里，
        没有独立的状态端点）。硬套"独立状态端点"的形状会写出一个永远
        查不到东西的函数，而症状是"环境一直显示未就绪"。
        """
        spec = self.spec.environment
        item_id = self._item_id(handle)
        request = self.spec.challenge_detail
        if not spec.from_detail or request is None or item_id is None:
            return RemoteEnvironment()
        data = await self._call(request, item_id=item_id)
        if not isinstance(data, dict):
            return RemoteEnvironment()
        return self._to_environment(data, spec)

    def _to_environment(
        self, payload: dict[str, Any], spec: EnvironmentSpec
    ) -> RemoteEnvironment:
        fields = spec.fields
        endpoints: list[RemoteEndpoint] = []
        for item in dig_list(payload, spec.endpoints_path):
            if not isinstance(item, dict):
                continue
            users: list[tuple[str, str]] = []
            for user in dig_list(item, fields.users):
                if isinstance(user, dict):
                    users.append(
                        (
                            dig_str(user, fields.username),
                            dig_str(user, fields.password),
                        )
                    )
            endpoints.append(
                RemoteEndpoint(
                    expose_ips=[str(x) for x in dig_list(item, fields.expose_ips)],
                    ports=[str(x) for x in dig_list(item, fields.ports)],
                    users=users,
                    is_proxy=bool(dig(item, fields.is_proxy)),
                    proxy_ips=[str(x) for x in dig_list(item, fields.proxy_ips)],
                    port_mappings=[
                        x for x in dig_list(item, fields.port_mappings) if isinstance(x, dict)
                    ],
                    expire_time=parse_millis(dig(item, fields.expire_time)),
                )
            )

        access_urls: list[str] = []
        if spec.access_urls_path:
            access_urls = [str(x) for x in dig_list(payload, spec.access_urls_path)]

        # ★ "就绪"是**两个条件**：配置指明的那个字段成立，**且**真的拿到了
        #   连接信息。只认字段会踩到一个真实的坑（比赛平台文档 §5/§6 明写
        #   "直到 isNeedCheck=false **且** endpoints 可用"）：平台说"不检查
        #   了"，但连接信息还是空的 —— 模型会拿到一份空靶机信息，然后卡在
        #   "没有靶机地址"上，以为是自己命令写错了。
        ready = False
        if spec.ready_path:
            value = bool(dig(payload, spec.ready_path))
            ready = (not value) if spec.ready_negate else value
        if not endpoints and not access_urls:
            ready = False

        return RemoteEnvironment(
            endpoints=endpoints,
            access_urls=access_urls,
            ready=ready,
            needs_init=bool(dig(payload, "isNeedInit")),
            can_refresh=bool(dig(payload, spec.can_refresh_path)) if spec.can_refresh_path else False,
        )

    async def start_environment(self, handle: dict[str, Any]) -> None:
        request = self.spec.start_env
        item_id = self._item_id(handle)
        if request is None or item_id is None:
            return None
        await self._call(request, item_id=item_id)
        return None

    async def stop_environment(self, handle: dict[str, Any]) -> None:
        """回收环境。**容错，不抛** —— 见 `SlabMatchDriver` 里同一条理由：
        回收一个没起过的环境会报错，而那是正常的；抛出去会让"停止解题"
        这个动作失败，而它本该总是成功。"""
        request = self.spec.stop_env
        item_id = self._item_id(handle)
        if request is None or item_id is None:
            return None
        try:
            await self._call(request, item_id=item_id)
        except Exception as exc:  # noqa: BLE001 - 见 docstring
            logger.info("回收环境时平台返回错误（通常无害）：%s", exc)
        return None

    async def environment_prompt(self, env: RemoteEnvironment) -> str:
        return render_endpoints_prompt(env)

    # ══════════════════════════════════════════════════════
    # 平台元信息
    # ══════════════════════════════════════════════════════

    @property
    def answer_max_length(self) -> int:
        return self.spec.answer_max_length

    @property
    def supports_environment(self) -> bool:
        return self.spec.start_env is not None


__all__ = ["GenericDriver"]
